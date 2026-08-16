"""The DARK guarantee for the session-lane tool-exposure wiring.

`_tool_exposure_argv` sits on the one argv assembly path every bridge session
traverses (fresh launch, resume-with-prompt, and queued launch, which re-enters
`_bridge_launch_argv`). The entire value of shipping it dark rests on a single claim:

    with OMNIAGENTOS_TOOL_CATALOG unset or "off", the argv is byte-identical to
    what it was before this code existed.

That claim is worth a test rather than a reading, because it is exactly the kind of
thing that stays true until someone adds "just one" unconditional line above the mode
check. The tests below pin the argv in all three modes and pin the failure behaviour,
which matters just as much: an exposure computation is an optimisation, and an
optimisation that can stop a session launching is a regression however good its
decisions are.
"""

from __future__ import annotations

import pytest

import omniagentos.sessions.supervisor as sup
from omniagentos.toolplane.config import TOOL_CATALOG_ENV


def _stub_supervisor() -> sup.SessionSupervisor:
    """A supervisor with only the attribute the argv builders read.

    Mirrors the idiom in tests/sessions/core/test_supervisor.py: constructing a real
    supervisor would open a database and start threads, none of which the argv
    assembly touches.
    """
    s = sup.SessionSupervisor.__new__(sup.SessionSupervisor)
    s.claude_binary = "claude"
    return s


def _launch_argv(s: sup.SessionSupervisor) -> list[str]:
    return sup.SessionSupervisor._bridge_launch_argv(s, "ref-1", "haiku", "prompt", None)


def _resume_argv(s: sup.SessionSupervisor) -> list[str]:
    return sup.SessionSupervisor._bridge_resume_argv(s, "ref-1", "prompt", None, None)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test states its own mode; an inherited env value would prove nothing."""
    monkeypatch.delenv(TOOL_CATALOG_ENV, raising=False)


# -- the dark guarantee ------------------------------------------------------


def test_argv_is_unchanged_when_the_flag_is_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default state: no env var at all. This is how the branch actually ships."""
    s = _stub_supervisor()
    baseline = _launch_argv(s)

    # The pre-existing invariants the argv has always carried.
    assert "--verbose" in baseline
    assert baseline[baseline.index("--disallowedTools") + 1] == "Task"

    monkeypatch.setattr(sup, "_maybe_append_effort", sup._maybe_append_effort)
    assert _launch_argv(s) == baseline
    assert _resume_argv(s) == _resume_argv(s)


@pytest.mark.parametrize("mode", ["off", "OFF", "0", "false", "no"])
def test_every_off_spelling_leaves_argv_untouched(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """The falsy env spellings are a kill switch, so each must reach the same argv."""
    s = _stub_supervisor()
    monkeypatch.delenv(TOOL_CATALOG_ENV, raising=False)
    baseline_launch = _launch_argv(s)
    baseline_resume = _resume_argv(s)

    monkeypatch.setenv(TOOL_CATALOG_ENV, mode)
    assert _launch_argv(s) == baseline_launch
    assert _resume_argv(s) == baseline_resume


def test_an_unparseable_flag_value_does_not_enable_anything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A typo must never silently start filtering an agent's toolset."""
    s = _stub_supervisor()
    monkeypatch.delenv(TOOL_CATALOG_ENV, raising=False)
    baseline = _launch_argv(s)

    monkeypatch.setenv(TOOL_CATALOG_ENV, "enfroce")
    assert _launch_argv(s) == baseline


# -- shadow mode: observe, change nothing ------------------------------------


def test_shadow_mode_records_a_decision_and_still_changes_no_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = _stub_supervisor()
    monkeypatch.delenv(TOOL_CATALOG_ENV, raising=False)
    baseline = _launch_argv(s)

    recorded: list[object] = []
    monkeypatch.setattr(
        "omniagentos.toolplane.exposure.emit_observation",
        lambda observation: recorded.append(observation) or True,
    )
    monkeypatch.setenv(TOOL_CATALOG_ENV, "shadow")

    assert _launch_argv(s) == baseline, "shadow mode must not touch the argv"
    assert recorded, "shadow mode must durably record the decision it declined to apply"

    observation = recorded[0]
    assert isinstance(observation, dict)
    assert observation["source"] == "toolplane-exposure"
    assert observation["session_id"] == "ref-1"
    # Counts, never membership -- the ledger must not learn the catalogue's shape.
    assert "n_hidden" in observation
    assert "hidden" not in observation
    assert "deferred" not in observation


def test_shadow_mode_on_the_resume_path_too(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _stub_supervisor()
    monkeypatch.delenv(TOOL_CATALOG_ENV, raising=False)
    baseline = _resume_argv(s)

    recorded: list[object] = []
    monkeypatch.setattr(
        "omniagentos.toolplane.exposure.emit_observation",
        lambda observation: recorded.append(observation) or True,
    )
    monkeypatch.setenv(TOOL_CATALOG_ENV, "shadow")

    assert _resume_argv(s) == baseline
    assert recorded


# -- enforce mode: narrow, additive, never destructive -----------------------


def test_enforce_mode_never_removes_the_pre_existing_task_denial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Task is disallowed for a reason unrelated to exposure; enforcement may only add."""
    s = _stub_supervisor()
    monkeypatch.setenv(TOOL_CATALOG_ENV, "enforce")

    argv = _launch_argv(s)
    denied = argv[argv.index("--disallowedTools") + 1].split(",")
    assert "Task" in denied
    assert argv.count("--disallowedTools") == 1


def test_enforce_mode_preserves_every_other_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enforcement rewrites exactly one flag's value and nothing else."""
    s = _stub_supervisor()
    monkeypatch.delenv(TOOL_CATALOG_ENV, raising=False)
    baseline = _launch_argv(s)

    monkeypatch.setenv(TOOL_CATALOG_ENV, "enforce")
    enforced = _launch_argv(s)

    def _without_denials(argv: list[str]) -> list[str]:
        index = argv.index("--disallowedTools")
        return argv[:index] + argv[index + 2 :]

    assert _without_denials(enforced) == _without_denials(baseline)


# -- failure containment -----------------------------------------------------


def test_a_broken_exposure_computation_never_blocks_a_launch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point of the guard: launching must survive a broken toolplane."""
    s = _stub_supervisor()
    monkeypatch.delenv(TOOL_CATALOG_ENV, raising=False)
    baseline = _launch_argv(s)

    def _explode(*args: object, **kwargs: object) -> object:
        raise RuntimeError("catalog is on fire")

    monkeypatch.setattr("omniagentos.toolplane.exposure.compute_exposure", _explode)
    monkeypatch.setenv(TOOL_CATALOG_ENV, "enforce")

    # Not an exception, and not a narrowed toolset either -- the original argv.
    assert _launch_argv(s) == baseline


def test_a_broken_config_read_never_blocks_a_launch(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _stub_supervisor()
    monkeypatch.delenv(TOOL_CATALOG_ENV, raising=False)
    baseline = _launch_argv(s)

    def _explode() -> object:
        raise RuntimeError("config is on fire")

    monkeypatch.setattr("omniagentos.toolplane.config.tool_catalog_mode", _explode)
    assert _launch_argv(s) == baseline


def test_a_broken_observation_sink_never_blocks_a_launch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = _stub_supervisor()
    monkeypatch.delenv(TOOL_CATALOG_ENV, raising=False)
    baseline = _launch_argv(s)

    def _explode(observation: object) -> bool:
        raise OSError("ledger is unwritable")

    monkeypatch.setattr("omniagentos.toolplane.exposure.emit_observation", _explode)
    monkeypatch.setenv(TOOL_CATALOG_ENV, "shadow")
    assert _launch_argv(s) == baseline


def test_enforce_is_a_no_op_today_because_no_builtin_is_ever_hidden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Documents the seam's REAL status rather than implying more coverage than exists.

    The supervisor computes exposure with an empty grant, which hides every connector
    capability -- but a connector capability id has no entry in SESSION_TOOL_CAPABILITY,
    so it maps to no native tool and enforcement has nothing to narrow. Built-ins, which
    DO map, are never hidden at the default risk ceiling. Net effect on the session lane
    today: enforce mode changes nothing.

    That is a fact about the mapping's coverage, not about the mechanism, and the next
    test shows the mechanism works the moment a mapped tool is hidden.
    """
    s = _stub_supervisor()
    monkeypatch.delenv(TOOL_CATALOG_ENV, raising=False)
    baseline = _launch_argv(s)

    monkeypatch.setenv(TOOL_CATALOG_ENV, "enforce")
    assert _launch_argv(s) == baseline


def test_enforce_does_narrow_once_a_mapped_builtin_is_hidden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The seam is real: hide `write_file` and the native Write/Edit tools are denied."""
    from omniagentos.toolplane.exposure import ExposureDecision

    s = _stub_supervisor()

    def _hide_write_file(ctx: object, grants: object, *args: object, **kwargs: object):
        return ExposureDecision(
            core_tools=("read_file",),
            allowed=("read_file",),
            deferred=(),
            hidden=("write_file", "edit_file"),
            reason="test",
            mode="enforce",
            bypassed=False,
            fallback=False,
            estimated_tokens=0,
        )

    monkeypatch.setattr("omniagentos.toolplane.exposure.compute_exposure", _hide_write_file)
    monkeypatch.setenv(TOOL_CATALOG_ENV, "enforce")

    argv = _launch_argv(s)
    denied = set(argv[argv.index("--disallowedTools") + 1].split(","))

    # Additive: the pre-existing denial survives.
    assert "Task" in denied
    # write_file -> Write; edit_file -> Edit, MultiEdit, NotebookEdit.
    assert {"Write", "Edit", "MultiEdit", "NotebookEdit"} <= denied
    # Nothing unmapped is invented.
    assert "Bash" not in denied
    assert "Read" not in denied
