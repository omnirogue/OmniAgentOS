"""The launch seam: what the lease does to a real adapter spawn, per mode.

The single most important test in this module is
``test_flag_off_launch_is_byte_identical``. Track D is only safe to merge because
the flag being off is indistinguishable from the code not existing, and that claim
has to be pinned by capturing the ACTUAL Popen arguments rather than by reading the
diff and believing it.
"""

from __future__ import annotations

import dataclasses
import json
import time
from pathlib import Path
from typing import Any

import pytest

from omniagentos.adapters.common import CliAdapter, ParsedResponse, _scrubbed_env
from omniagentos.contracts import AgentInput, AgentUsage, BudgetSpec, ResultStatus
from omniagentos.lease import models, seam
from omniagentos.lease.issue import issue_lease
from omniagentos.lease.models import Lease, LeaseCeilings, LeaseSubject
from omniagentos.runner import sandbox


class _StubAdapter(CliAdapter):
    """The smallest adapter that exercises ``_invoke`` end to end."""

    name = "stub"
    cli = "/bin/echo"

    def _command(self, input: AgentInput, prompt: str, session_ref: str | None) -> list[str]:
        return ["/bin/echo", "hi"]

    def _parse(self, stdout: str) -> ParsedResponse:
        return ParsedResponse(text=stdout.strip(), session_ref=None, payload={})

    def _usage(self, input: AgentInput, parsed: ParsedResponse, wall_ms: int) -> AgentUsage:
        return AgentUsage(wall_ms=wall_ms, estimated=True, source="estimator")


class _Recorder:
    """Captures every Popen call so a launch can be compared field by field."""

    calls: list[dict[str, Any]] = []

    def __init__(self, argv: list[str], **kwargs: Any) -> None:
        _Recorder.calls.append({"argv": argv, **kwargs})
        self.pid = 4242
        self.returncode = 0

    def communicate(self, _: str | None = None, timeout: float | None = None) -> tuple[str, str]:
        return "ok", ""

    def poll(self) -> int | None:
        return 0


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> type[_Recorder]:
    """Offline Popen + a proven-available sandbox, so only the lease varies."""
    from omniagentos.adapters import common

    _Recorder.calls = []
    monkeypatch.setattr(common.subprocess, "Popen", _Recorder)
    monkeypatch.setattr("omniagentos.runner.sandbox.sandbox_available", lambda: True)
    monkeypatch.setattr(
        "omniagentos.runner.sandbox.wrap_available", lambda argv, workspace_dir: True
    )
    return _Recorder


@pytest.fixture(autouse=True)
def isolated_lease_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Every test starts from a known-off lease and its own ledger directory."""
    monkeypatch.delenv("OMNIAGENTOS_AUTONOMY_LEASE", raising=False)
    empty = tmp_path / "no-lease.yaml"
    empty.write_text("autonomy_lease:\n  mode: off\n", encoding="utf-8")
    monkeypatch.setenv("OMNIAGENTOS_LEASE_CONFIG", str(empty))


def _agent_input(tmp_path: Path) -> AgentInput:
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    return AgentInput(
        run_id="run_seam",
        task_id="task_seam",
        prompt="hello",
        working_dir=str(workspace),
        model="test-model",
        budget=BudgetSpec(wall_ms_max=60_000),
        metadata={
            "sandbox": {"level": "workspace_write"},
            "cli_unattended_elevated": True,
        },
    )


def _write_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> None:
    path = tmp_path / "lease.yaml"
    path.write_text(body, encoding="utf-8")
    monkeypatch.setenv("OMNIAGENTOS_LEASE_CONFIG", str(path))


# --------------------------------------------------------------------------- #
# OFF -- the additive guarantee
# --------------------------------------------------------------------------- #


def test_lease_is_off_by_default() -> None:
    """The shipped default is dark; nothing is issued and nothing is enforced."""
    assert seam.lease_active() is False


def test_flag_off_launch_is_byte_identical(
    recorder: type[_Recorder], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the flag off the spawn is EXACTLY today's spawn, argv and env included.

    The comparison baseline is a launch taken with ``plan_launch`` monkeypatched to
    explode: if the seam were reachable at all with the flag off, this test would
    error rather than merely differ, which is the failure mode worth having.
    """

    def _boom(**_: Any) -> seam.LaunchPlan:
        raise AssertionError("plan_launch must never run with the lease off")

    monkeypatch.setattr(seam, "plan_launch", _boom)

    agent_input = _agent_input(tmp_path)
    result = _StubAdapter().run(agent_input)

    assert result.status is ResultStatus.OK
    assert len(recorder.calls) == 1
    call = recorder.calls[0]

    # The COMPLETE spawn, asserted field by field against what main does -- not a
    # sample of three properties, which would still pass if something unrelated
    # drifted (security review finding #14).
    assert set(call) == {
        "argv",
        "stdin",
        "stdout",
        "stderr",
        "text",
        "cwd",
        "start_new_session",
        "env",
        "preexec_fn",
    }
    assert call["cwd"] == agent_input.working_dir
    assert call["text"] is True
    assert call["start_new_session"] is True
    assert call["preexec_fn"] is None
    assert call["env"] == _scrubbed_env()
    assert call["argv"] == _StubAdapter()._sandboxed_launch(
        ["/bin/echo", "hi"],
        agent_input.working_dir,
        [],
        workspace_writable=True,
    )
    assert "network" not in " ".join(call["argv"])
    assert not {k for k in call["env"] if "PROXY" in k.upper()}


def test_flag_off_profile_matches_the_pre_lease_profile(tmp_path: Path) -> None:
    """The wrapped argv carries a profile with no egress directives at all."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    wrapped = sandbox.wrap_command(["/bin/echo", "hi"], str(workspace))
    if wrapped[0] != "/usr/bin/sandbox-exec":
        pytest.skip("OS sandbox unavailable on this host")
    assert wrapped[2] == sandbox.build_profile(str(workspace))
    assert "network" not in wrapped[2]


# --------------------------------------------------------------------------- #
# SHADOW -- records a decision, changes nothing
# --------------------------------------------------------------------------- #


def test_shadow_records_a_lease_and_changes_nothing(
    recorder: type[_Recorder], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Shadow writes the row it WOULD have enforced and leaves the launch alone."""
    ledger = tmp_path / "ledger"
    _write_config(
        tmp_path,
        monkeypatch,
        f"autonomy_lease:\n  mode: shadow\n  ledger_dir: {ledger}\n"
        "  ceilings:\n    cpu_s: 900\n  net:\n    default_policy: deny\n",
    )

    result = _StubAdapter().run(_agent_input(tmp_path))

    assert result.status is ResultStatus.OK
    call = recorder.calls[0]
    # Unchanged launch: no rlimits, no proxy env, and the profile has no egress
    # directives even though the config asked for deny.
    assert call["preexec_fn"] is None
    assert "HTTPS_PROXY" not in call["env"]
    assert "network" not in " ".join(call["argv"])

    rows = [
        json.loads(line)
        for line in (ledger / f"leases-{time.strftime('%Y%m', time.gmtime())}.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    assert rows, "shadow mode must write a ledger row"
    row = rows[-1]
    assert row["event"] == "issued"
    assert row["mode"] == "shadow"
    assert row["enforced"] is False
    assert row["signed"] is True
    assert "signature" not in row
    assert row["subject"]["run_id"] == "run_seam"


def test_shadow_never_breaks_a_launch_when_issuance_fails(
    recorder: type[_Recorder], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broken lease subsystem must degrade to today's behavior in shadow."""
    _write_config(tmp_path, monkeypatch, "autonomy_lease:\n  mode: shadow\n")

    def _explode(**_: Any) -> Lease:
        raise RuntimeError("issuance is broken")

    monkeypatch.setattr(seam, "issue_lease", _explode)

    result = _StubAdapter().run(_agent_input(tmp_path))

    assert result.status is ResultStatus.OK
    assert recorder.calls[0]["preexec_fn"] is None


# --------------------------------------------------------------------------- #
# ENFORCE -- the lease drives the existing mechanisms
# --------------------------------------------------------------------------- #


def test_enforce_drives_write_roots_and_ceilings(
    recorder: type[_Recorder], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under enforce the profile is built from the lease's own recorded roots."""
    ledger = tmp_path / "ledger"
    _write_config(
        tmp_path,
        monkeypatch,
        f"autonomy_lease:\n  mode: enforce\n  ledger_dir: {ledger}\n  ceilings:\n    cpu_s: 900\n",
    )

    result = _StubAdapter().run(_agent_input(tmp_path))

    assert result.status is ResultStatus.OK
    call = recorder.calls[0]
    assert call["preexec_fn"] is not None, "a cpu_s ceiling must produce a preexec"
    profile = call["argv"][2] if call["argv"][0] == "/usr/bin/sandbox-exec" else ""
    if profile:
        assert str((tmp_path / "ws").resolve()) in profile


def test_enforce_clamps_the_timeout_to_the_remaining_ttl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A short TTL shortens the existing timeout; it never lengthens it."""
    _write_config(tmp_path, monkeypatch, "autonomy_lease:\n  mode: enforce\n  ttl_seconds: 5\n")
    monkeypatch.setattr("omniagentos.runner.sandbox.wrap_available", lambda a, w: True)

    plan = seam.plan_launch(
        provider="stub",
        argv=["/bin/echo"],
        working_dir=str(tmp_path),
        workspace_writable=True,
        write_roots=[str(tmp_path)],
        timeout_ms=600_000,
        run_id="run_1",
    )

    assert plan.refusal is None
    assert plan.timeout_ms is not None
    assert 0 < plan.timeout_ms <= 5_000


def test_enforce_refuses_an_expired_lease(
    recorder: type[_Recorder], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An expired lease refuses the launch and NOTHING is spawned."""
    _write_config(tmp_path, monkeypatch, "autonomy_lease:\n  mode: enforce\n")

    def _expired(**kwargs: Any) -> Lease:
        now = time.time()
        lease = Lease(
            lease_id=models.new_lease_id(),
            subject=LeaseSubject(run_id="run_seam"),
            issued_at=now - 120.0,
            expires_at=now - 60.0,
            generation=models.current_generation(),
            fs_write_roots=(str(tmp_path),),
            ceilings=LeaseCeilings(),
        )
        return models.sign_lease(lease)

    monkeypatch.setattr(seam, "issue_lease", _expired)

    result = _StubAdapter().run(_agent_input(tmp_path))

    assert result.status is ResultStatus.ERROR
    assert result.error is not None
    assert result.error.startswith("autonomy_lease_refused:cli-stub")
    assert "lease_expired:" in result.error
    assert recorder.calls == [], "a refused launch must spawn nothing"


def test_enforce_refuses_a_revoked_lease(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Bumping the generation revokes every outstanding lease at once."""
    _write_config(tmp_path, monkeypatch, "autonomy_lease:\n  mode: enforce\n")
    monkeypatch.setattr("omniagentos.runner.sandbox.wrap_available", lambda a, w: True)

    stale = issue_lease(run_id="run_1", workspace_dir=str(tmp_path))
    models.bump_generation()
    monkeypatch.setattr(seam, "issue_lease", lambda **_: stale)

    plan = seam.plan_launch(
        provider="stub",
        argv=["/bin/echo"],
        working_dir=str(tmp_path),
        workspace_writable=True,
        write_roots=[str(tmp_path)],
        timeout_ms=1000,
        run_id="run_1",
    )

    assert plan.refusal is not None
    assert "lease_generation:" in plan.refusal


def test_enforce_refuses_a_tampered_lease(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Widening the write roots after signing invalidates the lease."""
    _write_config(tmp_path, monkeypatch, "autonomy_lease:\n  mode: enforce\n")
    monkeypatch.setattr("omniagentos.runner.sandbox.wrap_available", lambda a, w: True)

    honest = issue_lease(run_id="run_1", workspace_dir=str(tmp_path))
    forged = dataclasses.replace(honest, fs_write_roots=(*honest.fs_write_roots, "/etc"))
    monkeypatch.setattr(seam, "issue_lease", lambda **_: forged)

    plan = seam.plan_launch(
        provider="stub",
        argv=["/bin/echo"],
        working_dir=str(tmp_path),
        workspace_writable=True,
        write_roots=[str(tmp_path)],
        timeout_ms=1000,
        run_id="run_1",
    )

    assert plan.refusal is not None
    assert "lease_signature:" in plan.refusal


def test_enforce_refuses_when_the_outer_wrap_would_not_engage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lease promising confinement no layer can deliver must refuse, not proceed.

    ``wrap_command`` silently returns the argv UNWRAPPED when the sandbox is
    unavailable. Without this refusal an enforce-mode ``net_policy: deny`` would
    produce a completely unconfined process while the ledger recorded a denial.
    """
    _write_config(tmp_path, monkeypatch, "autonomy_lease:\n  mode: enforce\n")
    monkeypatch.setattr("omniagentos.runner.sandbox.wrap_available", lambda a, w: False)

    plan = seam.plan_launch(
        provider="stub",
        argv=["/bin/echo"],
        working_dir=str(tmp_path),
        workspace_writable=True,
        write_roots=[str(tmp_path)],
        timeout_ms=1000,
        run_id="run_1",
    )

    assert plan.refusal is not None
    assert "OS sandbox wrap would not engage" in plan.refusal


def test_enforce_proxy_mode_injects_proxy_env_and_opens_only_its_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A proxy lease starts the proxy, injects HTTPS_PROXY, and opens one port."""
    _write_config(
        tmp_path,
        monkeypatch,
        "autonomy_lease:\n  mode: enforce\n  net:\n    default_policy: proxy\n"
        "    allow_domains:\n      - api.anthropic.com\n",
    )
    monkeypatch.setattr("omniagentos.runner.sandbox.wrap_available", lambda a, w: True)

    plan = seam.plan_launch(
        provider="stub",
        argv=["/bin/echo"],
        working_dir=str(tmp_path),
        workspace_writable=True,
        write_roots=[str(tmp_path)],
        timeout_ms=1000,
        run_id="run_1",
    )
    try:
        assert plan.refusal is None
        assert plan.net_mode == "proxy"
        assert len(plan.net_loopback_ports) == 1
        port = plan.net_loopback_ports[0]
        assert plan.env_additions["HTTPS_PROXY"] == f"http://127.0.0.1:{port}"
        assert plan.env_additions["NO_PROXY"] == ""
        rules = sandbox.network_rules("proxy", plan.net_loopback_ports)
        assert "(deny network*)" in rules
        assert f'"localhost:{port}"' in rules
    finally:
        seam.close_plan(plan)

    assert plan.proxy is None, "close_plan must release the proxy"


def test_enforce_records_the_decision(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every enforce decision lands in the append-only ledger, signature excluded."""
    ledger = tmp_path / "ledger"
    _write_config(
        tmp_path,
        monkeypatch,
        f"autonomy_lease:\n  mode: enforce\n  ledger_dir: {ledger}\n",
    )
    monkeypatch.setattr("omniagentos.runner.sandbox.wrap_available", lambda a, w: True)

    plan = seam.plan_launch(
        provider="stub",
        argv=["/bin/echo"],
        working_dir=str(tmp_path),
        workspace_writable=True,
        write_roots=[str(tmp_path)],
        timeout_ms=1000,
        run_id="run_1",
    )
    seam.close_plan(plan)

    assert plan.lease is not None
    text = (ledger / f"leases-{time.strftime('%Y%m', time.gmtime())}.jsonl").read_text(
        encoding="utf-8"
    )
    assert plan.lease.signature not in text
    row = json.loads(text.splitlines()[-1])
    assert row["mode"] == "enforce"
    assert row["enforced"] is True
    assert row["limits"]["rss_mb"].startswith(("unset", "declared"))


def test_non_proxy_lease_carrying_domains_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Signature-coverage invariant: an unsigned field may never ride along.

    ``net_allow_domains`` only enters the canonical payload under proxy, so a
    deny/open lease carrying domains would hold an UNSIGNED field. Nothing reads
    it today, but "inert because no caller looks" expires the moment a caller is
    added — so the combination is refused outright.
    """
    _write_config(tmp_path, monkeypatch, "autonomy_lease:\n  mode: enforce\n")
    monkeypatch.setattr("omniagentos.runner.sandbox.wrap_available", lambda a, w: True)

    smuggled = models.sign_lease(
        Lease(
            lease_id=models.new_lease_id(),
            subject=LeaseSubject(run_id="run_1"),
            issued_at=time.time(),
            expires_at=time.time() + 60,
            generation=models.current_generation(),
            fs_write_roots=(str(tmp_path),),
            net_mode="deny",
            net_allow_domains=("evil.test",),
        )
    )
    assert models.signature_matches(smuggled), "the smuggled field really is unsigned"
    monkeypatch.setattr(seam, "issue_lease", lambda **_: smuggled)

    plan = seam.plan_launch(
        provider="stub",
        argv=["/bin/echo"],
        working_dir=str(tmp_path),
        workspace_writable=True,
        write_roots=[str(tmp_path)],
        timeout_ms=1000,
        run_id="run_1",
    )

    assert plan.refusal is not None
    assert "lease_net_policy:" in plan.refusal


def test_close_plan_is_safe_on_a_no_op_plan() -> None:
    """Teardown must tolerate every plan shape, including None."""
    seam.close_plan(None)
    seam.close_plan(seam.no_op_plan())
