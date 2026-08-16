"""TN.1 — root policy, mode inference (with negation), and task splitting.

The two tests that carry the most weight:

``test_negated_signal_does_not_classify``
    "not a report" must not classify as ``report``. A keyword classifier without
    negation handling is worse than none, because it is confidently wrong and
    nobody re-reads a classifier that has an accuracy number attached.

``test_split_rewires_dependents_to_both_halves``
    A dependent of a split unit must depend on BOTH halves. Depending only on
    the terminal half is equivalent ONLY if the scheduler computes the transitive
    closure, and "correct as long as someone else is transitive" is the kind of
    assumption that fails silently the day a scheduler is rewritten.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from omniagentos.contracts import TaskMode
from omniagentos.workmodes import modes
from omniagentos.workmodes.modes import (
    ARTIFACTS_ROOT_NAME,
    INPUTS_ROOT_NAME,
    WORKSPACE_ROOT_NAME,
    ModeInference,
    WorkModeError,
    WorkUnit,
    infer_task_mode,
    policy_for,
    provision_roots,
    reconcile_task_mode,
    resolve_roots,
    split_mixed_unit,
    split_units,
    validate_artifact_outputs,
)

# --- root policy -----------------------------------------------------------


def test_every_task_mode_has_a_policy() -> None:
    """A new TaskMode without a policy row must not silently get "no roots"."""
    assert set(modes.MODE_POLICIES) == set(TaskMode)


def test_code_gets_repo_scope_and_no_artifact_roots() -> None:
    roots = resolve_roots(TaskMode.CODE, "tsk_1", base="/var/x")
    assert policy_for(TaskMode.CODE).repo_scope is True
    assert roots.artifacts is None
    assert roots.workspace is None
    assert roots.inputs is None
    assert roots.write_roots == ()


@pytest.mark.parametrize(
    "mode", [TaskMode.REPORT, TaskMode.CONTENT, TaskMode.IMAGE, TaskMode.VIDEO]
)
def test_artifact_modes_get_artifacts_only(mode: TaskMode) -> None:
    roots = resolve_roots(mode, "tsk_1", base="/var/x")
    assert roots.artifacts == os.path.join("/var/x", ARTIFACTS_ROOT_NAME, "tsk_1")
    assert roots.workspace is None
    assert roots.inputs is None
    assert roots.write_roots == (roots.artifacts,)
    assert roots.read_only_roots == ()
    assert policy_for(mode).repo_scope is False


def test_intake_processing_inputs_are_read_only() -> None:
    roots = resolve_roots(TaskMode.INTAKE_PROCESSING, "tsk_9", base="/var/x")
    assert roots.inputs == os.path.join("/var/x", INPUTS_ROOT_NAME, "tsk_9")
    assert roots.workspace == os.path.join("/var/x", WORKSPACE_ROOT_NAME, "tsk_9")
    assert roots.artifacts == os.path.join("/var/x", ARTIFACTS_ROOT_NAME, "tsk_9")
    # inputs is granted but NOT writable; workspace and artifacts are.
    assert roots.read_only_roots == (roots.inputs,)
    assert roots.write_roots == (roots.workspace, roots.artifacts)


def test_scope_slug_rejects_traversal() -> None:
    with pytest.raises(WorkModeError):
        resolve_roots(TaskMode.REPORT, "../../etc", base="/var/x")


def test_resolve_roots_creates_nothing(tmp_path: Path) -> None:
    """Resolution is pure; only provision_roots touches the filesystem."""
    base = tmp_path / "var"
    resolve_roots(TaskMode.INTAKE_PROCESSING, "tsk_1", base=str(base))
    assert not base.exists()

    roots = resolve_roots(TaskMode.INTAKE_PROCESSING, "tsk_1", base=str(base))
    created = provision_roots(roots)
    assert set(created) == set(roots.all_roots)
    for path in created:
        assert os.path.isdir(path)
    # Idempotent: a retried task re-uses what it had.
    assert provision_roots(roots) == created


def test_var_root_honors_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path / "elsewhere"))
    assert modes.var_root() == str(tmp_path / "elsewhere")


def test_validate_artifact_outputs_rejects_escapes() -> None:
    assert validate_artifact_outputs(["a/b.md", "./a/b.md"]) == ("a/b.md",)
    for bad in ("/abs/path.md", "~/home.md", "../escape.md", "."):
        with pytest.raises(WorkModeError):
            validate_artifact_outputs([bad])


# --- inference -------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("write ad copy for the webinar funnel", TaskMode.CONTENT),
        ("generate a thumbnail and a logo for the launch", TaskMode.IMAGE),
        ("cut a 30 second reel from the keynote", TaskMode.VIDEO),
        ("produce a competitive analysis report on the top 3 rivals", TaskMode.REPORT),
        ("refactor the runner and add unit tests", TaskMode.CODE),
        ("extract the totals from these files I uploaded", TaskMode.INTAKE_PROCESSING),
    ],
)
def test_inference_happy_paths(text: str, expected: TaskMode) -> None:
    assert infer_task_mode(text).mode is expected


def test_negated_signal_does_not_classify() -> None:
    """'not a report' must not classify as report -- the headline negation case."""
    inference = infer_task_mode("I want a punchy landing page, not a report")
    assert inference.mode is TaskMode.CONTENT
    assert any("report" in label for label in inference.suppressed)
    assert TaskMode.REPORT not in inference.scores


@pytest.mark.parametrize(
    "text",
    [
        "not a report",
        "no report please",
        "instead of a report, just the headline",
        "rather than a report, give me the numbers",
        "do not write a report",
        "without a report",
    ],
)
def test_negation_forms(text: str) -> None:
    assert TaskMode.REPORT not in infer_task_mode(text).scores


def test_negation_does_not_reach_across_a_clause() -> None:
    """A trailing prohibition must not silently kill the deliverable."""
    inference = infer_task_mode("write a report; do not touch the repo")
    assert inference.mode is TaskMode.REPORT


def test_no_signals_is_none_not_a_default() -> None:
    inference = infer_task_mode("finish the thing we talked about")
    assert inference.mode is None
    assert inference.confident is False


def test_inference_is_deterministic_and_repeat_proof() -> None:
    """A repeated word does not outvote two distinct signals from another mode."""
    text = "image image image image; cut a reel from the b-roll"
    first = infer_task_mode(text)
    assert first.mode is TaskMode.VIDEO
    assert infer_task_mode(text) == first


def test_empty_text() -> None:
    assert infer_task_mode(None).mode is None
    assert infer_task_mode("   ").mode is None


# --- reconciliation --------------------------------------------------------


def test_confident_inference_takes_repo_access_away_from_a_code_label() -> None:
    """The TN.2 bug in one test: a copywriting task labelled 'code'."""
    decision = reconcile_task_mode(
        TaskMode.CODE, "write the ad copy and headlines for the webinar landing page"
    )
    assert decision.mode is TaskMode.CONTENT
    assert decision.source == "inferred"
    assert decision.conflict is True
    assert policy_for(decision.mode).repo_scope is False


def test_unconfident_inference_never_overrides_a_code_label() -> None:
    decision = reconcile_task_mode(TaskMode.CODE, "add a caption to the chart")
    assert decision.mode is TaskMode.CODE
    assert decision.source == "declared"
    assert decision.conflict is True  # recorded, not acted on


def test_a_keyword_scan_never_widens_scope_to_the_repo() -> None:
    decision = reconcile_task_mode(TaskMode.CONTENT, "migrate the copy to the new endpoint")
    assert decision.mode is TaskMode.CONTENT
    assert policy_for(decision.mode).repo_scope is False


def test_two_non_code_modes_keep_the_declared_deliverable_type() -> None:
    decision = reconcile_task_mode(TaskMode.IMAGE, "a thumbnail for the report")
    assert decision.mode is TaskMode.IMAGE


def test_missing_label_falls_back_to_code_when_nothing_is_inferred() -> None:
    decision = reconcile_task_mode(None, "do the next thing")
    assert decision.mode is TaskMode.CODE
    assert decision.source == "fallback"
    assert decision.conflict is False


def test_missing_label_uses_the_inference() -> None:
    decision = reconcile_task_mode(None, "design a poster for the summit")
    assert decision.mode is TaskMode.IMAGE
    assert decision.source == "inferred"


def test_garbage_label_is_not_trusted() -> None:
    decision = reconcile_task_mode("SPREADSHEET", "write a market analysis report")
    assert decision.mode is TaskMode.REPORT
    assert decision.declared is None


def test_agreement_is_not_a_conflict() -> None:
    decision = reconcile_task_mode(TaskMode.REPORT, "write the quarterly report")
    assert decision.conflict is False
    assert decision.source == "declared"
    assert isinstance(decision.inference, ModeInference)


# --- splitting -------------------------------------------------------------


def test_pure_units_pass_through_unchanged() -> None:
    """IDENTITY over a code-only plan: the ship-dark guarantee for the planner."""
    units = (
        WorkUnit(key="a", repo_paths=("src/a.py",)),
        WorkUnit(key="b", repo_paths=("src/b.py",), depends_on=("a",)),
    )
    result = split_units(units)
    assert result.units == units
    assert result.replaced == {}
    assert result.changed is False


def test_mixed_unit_splits_into_two_with_a_dependency() -> None:
    unit = WorkUnit(
        key="t1",
        mode=TaskMode.CODE,
        repo_paths=("src/api.py",),
        artifact_outputs=("summary.md",),
        title="ship the endpoint and write a report on it",
    )
    code_half, artifact_half = split_mixed_unit(unit)
    assert code_half.key == "t1::code"
    assert code_half.mode is TaskMode.CODE
    assert code_half.repo_paths == ("src/api.py",)
    assert code_half.artifact_outputs == ()
    assert artifact_half.key == "t1::artifacts"
    assert artifact_half.mode is TaskMode.REPORT
    assert artifact_half.repo_paths == ()
    assert artifact_half.artifact_outputs == ("summary.md",)
    assert artifact_half.depends_on == ("t1::code",)


def test_split_rewires_dependents_to_both_halves() -> None:
    units = (
        WorkUnit(key="a", repo_paths=("src/a.py",)),
        WorkUnit(
            key="b",
            repo_paths=("src/b.py",),
            artifact_outputs=("report.md",),
            depends_on=("a",),
        ),
        WorkUnit(key="c", repo_paths=("src/c.py",), depends_on=("b",)),
    )
    result = split_units(units)
    by_key = {unit.key: unit for unit in result.units}
    assert result.replaced == {"b": ("b::code", "b::artifacts")}
    assert by_key["b::code"].depends_on == ("a",)
    assert by_key["b::artifacts"].depends_on == ("b::code", "a")
    # The dependent depends on BOTH halves, never on the terminal one alone.
    assert set(by_key["c"].depends_on) == {"b::code", "b::artifacts"}


def test_split_never_creates_a_self_dependency() -> None:
    units = (
        WorkUnit(key="a", repo_paths=("src/a.py",), artifact_outputs=("a.md",), depends_on=("a",)),
    )
    result = split_units(units)
    for unit in result.units:
        assert unit.key not in unit.depends_on


def test_split_of_a_non_code_declared_unit_keeps_its_mode() -> None:
    unit = WorkUnit(
        key="t",
        mode=TaskMode.CONTENT,
        repo_paths=("landing/index.html",),
        artifact_outputs=("ads.json",),
    )
    _code, artifact = split_mixed_unit(unit)
    assert artifact.mode is TaskMode.CONTENT


def test_split_infers_the_artifact_half_from_the_title() -> None:
    unit = WorkUnit(
        key="t",
        mode=TaskMode.CODE,
        repo_paths=("src/x.py",),
        artifact_outputs=("hero.png",),
        title="build the page and a hero image for it",
    )
    _code, artifact = split_mixed_unit(unit)
    assert artifact.mode is TaskMode.IMAGE


def test_split_refuses_a_key_collision() -> None:
    units = (
        WorkUnit(key="t", repo_paths=("src/x.py",), artifact_outputs=("a.md",)),
        WorkUnit(key="t::code", repo_paths=("src/y.py",)),
    )
    with pytest.raises(WorkModeError, match="collide"):
        split_units(units)


def test_duplicate_keys_are_refused() -> None:
    with pytest.raises(WorkModeError, match="duplicate"):
        split_units((WorkUnit(key="a"), WorkUnit(key="a")))


def test_splitting_a_pure_unit_is_an_error() -> None:
    with pytest.raises(WorkModeError, match="not mixed"):
        split_mixed_unit(WorkUnit(key="a", repo_paths=("src/a.py",)))
