"""main-health must OBSERVE type health on every push to main, and must never
be able to gate, halt, or delay that push.

88 of the last 100 commits on main are single-parent commits pushed directly
by the gate-loop daemon; only a minority arrive via a GitHub PR merge. The
PR-only `type` job in ci.yml grades a candidate BEFORE it merges, but never
grades the daemon's direct write to main — main can carry type errors
indefinitely with nothing observing it (measured: main was type-red at HEAD
000000000, 11 errors in omniagentos/intake/deliverable_checks.py, for 49
consecutive commits, undetected).

These tests assert the structural properties that make the `type-health` job
in main-health.yml an *observation*, not a gate: it triggers on push to main,
it invokes the repo's own canonical type-check invocation (the same command
`make type` and ci.yml's `type` job run), and it is wired so that a red
result can never fail the job/workflow or be promoted into a required
context.
"""

import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "main-health.yml"
MAKEFILE = REPO_ROOT / "Makefile"

# The four contexts GitHub branch protection can require, per the proposal
# and ci.yml's own job names. A job eligible to gate a landing would use one
# of these names.
REQUIRED_CONTEXT_NAMES = {"merge-gate", "lint", "type", "dashboard"}

# Splits a shell run block on `;`, `&&`, `||`, `|` so a chained/suffixed
# invocation (`uv run mypy omniagentos; exit 1`) can't hide from the
# unshielded-invocation check by never appearing as a whole line.
_COMMAND_SEPARATORS = re.compile(r"[;&|]+")

# Matches the `${{ steps.<id>.outcome }}` expression form the summary step
# uses to read the type-check step's result.
_STEPS_OUTCOME_REF = re.compile(r"steps\.(\w+)\.outcome")


def _load_workflow() -> dict:
    assert WORKFLOW.is_file(), f"expected {WORKFLOW} to exist"
    text = WORKFLOW.read_text()
    doc = yaml.safe_load(text)
    assert isinstance(doc, dict), "main-health.yml must parse as a YAML mapping"
    return doc


def _canonical_type_check_command() -> str:
    """The repo's own canonical type-check invocation, read from the
    Makefile's `type` target rather than hand-rolled, so this test tracks
    the real command instead of asserting an independently-invented one."""
    lines = MAKEFILE.read_text().splitlines()
    for i, line in enumerate(lines):
        if line.rstrip() == "type:":
            recipe = lines[i + 1]
            assert recipe.startswith("\t"), "expected a tab-indented recipe line"
            return recipe[1:].strip()
    raise AssertionError("no `type:` target found in Makefile")


def _type_health_job() -> dict:
    jobs = _load_workflow()["jobs"]
    assert "type-health" in jobs, "expected a `type-health` job in main-health.yml"
    return jobs["type-health"]


def _locate_unique_step_index(steps: list[dict], *, id: str) -> int:  # noqa: A002
    """Finds the index of the ONE step carrying `id: <id>`. Unlike a bare
    `next(... )` scan (round 3's bug, MH-004), this REQUIRES uniqueness:
    GitHub Actions requires step ids to be unique within a job, and an
    ambiguous id makes `steps.<id>.outcome` refer to who-knows-which step.
    A duplicate id must fail loudly here, not silently resolve to the
    first match.
    """
    matches = [i for i, s in enumerate(steps) if s.get("id") == id]
    assert matches, f"expected exactly one step with id: {id!r}, found none"
    assert len(matches) == 1, (
        f"expected exactly one step with id: {id!r}, found {len(matches)} "
        "(GitHub Actions requires step ids to be unique within a job; an "
        "ambiguous id makes `steps.<id>.outcome` refer to an unspecified step)"
    )
    return matches[0]


def _resolve_type_health_structure(
    steps: list[dict] | None = None,
) -> tuple[list[dict], int, dict, int, dict]:
    """Locates the canonical type-check step and the step that reads its
    outcome, and validates every structural precondition MH-004 requires so
    every other assertion in this file is built on an unambiguous
    instrument, not a `next(...)` scan that silently accepts the first
    match:

      - `steps` is non-empty
      - EXACTLY ONE step runs the canonical type-check command
      - EXACTLY ONE step carries `id: type_check`, and it IS that step
        (not two different steps, one satisfying each requirement)
      - at least one step exists AFTER the canonical step (a non-empty
        downstream suffix to guard at all)
      - EXACTLY ONE step references `steps.type_check.outcome`
      - that outcome-reading step's index is STRICTLY GREATER than the
        canonical step's index — on real GitHub Actions a step earlier in
        the job cannot see an outcome that has not been produced yet, so a
        misordered "summary" step is structurally incapable of reporting
        anything meaningful, however its script reads

    Accepts an explicit `steps` list for testability (constructing the
    three MH-004 bypass shapes without touching the real workflow file);
    defaults to the real `type-health` job's steps.

    Returns (steps, type_check_idx, type_check_step, summary_idx, summary_step).
    """
    if steps is None:
        steps = _type_health_job()["steps"]
    assert steps, "expected a non-empty `steps:` list in the type-health job"

    canonical = _canonical_type_check_command()
    by_command = [
        (i, s) for i, s in enumerate(steps) if s.get("run", "").strip() == canonical
    ]
    assert len(by_command) == 1, (
        "expected exactly one step running the canonical type-check command "
        f"{canonical!r}, found {len(by_command)}"
    )
    command_idx, type_check_step = by_command[0]

    id_idx = _locate_unique_step_index(steps, id="type_check")
    assert id_idx == command_idx, (
        "the step with id: type_check must be the SAME step that runs the "
        f"canonical type-check command (id at index {id_idx}, command at "
        f"index {command_idx})"
    )
    type_check_idx = command_idx

    suffix = steps[type_check_idx + 1 :]
    assert suffix, (
        "expected at least one step AFTER the canonical type-check step — "
        "a type-check step with nothing downstream cannot report a verdict"
    )

    target_ref = re.compile(r"steps\.type_check\.outcome")
    outcome_refs = [
        (i, s) for i, s in enumerate(steps) if target_ref.search(s.get("run", ""))
    ]
    assert len(outcome_refs) == 1, (
        "expected exactly one step referencing steps.type_check.outcome, "
        f"found {len(outcome_refs)}"
    )
    summary_idx, summary_step = outcome_refs[0]
    assert summary_idx > type_check_idx, (
        "the step reading steps.type_check.outcome must run AFTER the "
        f"canonical type-check step — found it at index {summary_idx}, but "
        f"the type-check step is at index {type_check_idx}; a step earlier "
        "in the job cannot see an outcome that has not been produced yet"
    )

    return steps, type_check_idx, type_check_step, summary_idx, summary_step


def _type_health_type_check_step() -> dict:
    _, _, step, _, _ = _resolve_type_health_structure()
    return step


def _type_health_summary_step() -> dict:
    """The step that reads back the type-check step's outcome (via a
    `${{ steps.type_check.outcome }}` expression in its `run:` block).
    Guaranteed unique and guaranteed to run after the type-check step —
    see `_resolve_type_health_structure`."""
    _, _, _, _, step = _resolve_type_health_structure()
    return step


def _steps_after_type_check() -> list[dict]:
    """Every step that runs after the type-check step, in job order — the
    set of steps that could turn a genuine RED (or an instrument failure)
    into a job/workflow failure if left unshielded. Non-emptiness and
    id-uniqueness are enforced by `_resolve_type_health_structure`."""
    steps, idx, _, _, _ = _resolve_type_health_structure()
    return steps[idx + 1 :]


def _type_health_summary_fallback_step() -> dict:
    """The step (added for MH-005) that makes a failure of the summary
    step itself VISIBLE — it reads `steps.<summary step's id>.outcome` and
    must run after the summary step."""
    steps = _type_health_job()["steps"]
    summary_step = _type_health_summary_step()
    summary_id = summary_step.get("id")
    assert summary_id, (
        "expected the summary step to carry a stable `id:` so a fallback "
        "step can reference its outcome"
    )
    summary_idx = steps.index(summary_step)
    target_ref = re.compile(rf"steps\.{re.escape(summary_id)}\.outcome")
    matches = [
        (i, s)
        for i, s in enumerate(steps)
        if i > summary_idx and target_ref.search(s.get("run", ""))
    ]
    assert len(matches) == 1, (
        f"expected exactly one step after the summary step referencing "
        f"steps.{summary_id}.outcome, found {len(matches)}"
    )
    return matches[0][1]


def _assert_steps_after_type_check_shielded(
    steps: list[dict], type_check_id: str = "type_check"
) -> None:
    """The structural invariant: every step after the type-check step MUST
    carry `continue-on-error: true`, whether it is a `run:` step or a
    `uses:` step, and regardless of HOW (or whether) it reads the outcome —
    a bare shell re-raise, env indirection, an action's `if:` condition
    calling `core.setFailed`, or any other expression form. Checked purely
    from the parsed YAML: no execution, no emulation of GitHub Actions'
    env/expression/shell/action semantics, so it cannot be dodged by any
    bypass shape that emulation would have to chase one at a time. A step
    that cannot fail the job cannot turn this observation job into a gate,
    whatever it contains.

    MH-004: locating the type-check step REQUIRES a unique id (not the
    first match) and REQUIRES a non-empty downstream suffix to guard —
    both enforced by `_locate_unique_step_index`.
    """
    idx = _locate_unique_step_index(steps, id=type_check_id)
    suffix = steps[idx + 1 :]
    assert suffix, (
        "expected at least one step after the type-check step to guard — "
        "an empty suffix means nothing here is actually being checked"
    )
    for step in suffix:
        assert step.get("continue-on-error") is True, (
            f"step {step.get('name')!r} runs after the type-check step and does not "
            "carry continue-on-error: true — an unshielded step here can turn this "
            "observation job into a blocking gate, however it reads the outcome."
        )


def _render_run_script(run: str, outcome: str) -> str:
    """Textually substitute `${{ steps.<id>.outcome }}` and `${{ github.sha }}`
    the way GitHub Actions does BEFORE the shell ever runs, so executing the
    result runs the REAL script the runner would run for a given outcome."""
    script = re.sub(r"\$\{\{\s*steps\.\w+\.outcome\s*\}\}", outcome, run)
    script = re.sub(r"\$\{\{\s*github\.sha\s*\}\}", "deadbeefcafe", script)
    return script


def _execute_step_script(run: str, outcome: str, tmp_path) -> tuple[int, str]:
    """Actually EXECUTES a step's `run:` script (rendered for the given
    type-check outcome) under bash, with GITHUB_STEP_SUMMARY pointed at a
    temp file, and returns (exit_code, summary_file_contents)."""
    script = _render_run_script(run, outcome)
    summary_file = tmp_path / "step_summary.txt"
    summary_file.write_text("")
    env = {**os.environ, "GITHUB_STEP_SUMMARY": str(summary_file)}
    result = subprocess.run(
        ["bash", "-c", script], env=env, capture_output=True, text=True, timeout=30
    )
    return result.returncode, summary_file.read_text()


def test_workflow_file_exists_and_parses_as_yaml() -> None:
    doc = _load_workflow()
    assert "jobs" in doc


def test_main_health_triggers_on_push_to_main() -> None:
    doc = _load_workflow()
    on = doc[True] if True in doc else doc["on"]  # PyYAML may key bare `on` as bool True
    assert "push" in on
    push = on["push"]
    assert "main" in push["branches"]


def test_type_health_job_exists() -> None:
    job = _type_health_job()
    assert job["runs-on"] == "ubuntu-latest"


def test_type_health_invokes_repo_canonical_type_check() -> None:
    # Confirms the job runs the SAME command as `make type` / ci.yml's `type`
    # job, not a hand-rolled mypy invocation that could drift from it.
    step = _type_health_type_check_step()
    assert step["run"].strip() == _canonical_type_check_command()


def test_type_health_type_check_step_cannot_fail_the_job() -> None:
    # The load-bearing non-blocking guarantee: whatever mypy reports, this
    # step's outcome is never treated as a job failure.
    step = _type_health_type_check_step()
    assert step.get("continue-on-error") is True


def test_type_health_type_check_step_has_a_stable_id() -> None:
    # The summary step reads ${{ steps.type_check.outcome }}. If this id were
    # ever renamed or dropped, that expression resolves to empty,
    # `[ "" = "success" ]` is false, and the job reports RED permanently no
    # matter what mypy actually did — a false-permanent-alarm.
    step = _type_health_type_check_step()
    assert step.get("id") == "type_check"


def test_summary_step_outcome_reference_matches_type_check_steps_id() -> None:
    # Assert the LINKAGE, not just the literal id: the id the summary step
    # reads via `steps.<id>.outcome` must be the SAME id actually set on the
    # type-check step. This stays green if both are renamed together, and
    # fails if only one side drifts — the property that actually matters.
    type_check_step = _type_health_type_check_step()
    summary_step = _type_health_summary_step()
    referenced_ids = set(_STEPS_OUTCOME_REF.findall(summary_step["run"]))
    assert referenced_ids, "expected at least one steps.<id>.outcome reference"
    assert referenced_ids == {type_check_step.get("id")}


def test_type_health_job_name_is_not_a_required_context() -> None:
    # A job named one of the four required contexts would be eligible for
    # GitHub branch protection to require it, turning observation into a
    # gate. type-health must stay outside that set.
    job = _type_health_job()
    assert job.get("name", "type-health") not in REQUIRED_CONTEXT_NAMES
    assert "type-health" not in REQUIRED_CONTEXT_NAMES


def test_main_health_workflow_has_no_pull_request_trigger() -> None:
    # This workflow observes commits that already landed on main; it must
    # never run as a PR check that a required-context rule could point at.
    doc = _load_workflow()
    on = doc[True] if True in doc else doc["on"]
    assert "pull_request" not in on


def test_no_step_in_type_health_can_fail_the_workflow_on_red_types() -> None:
    # Every step besides the (shielded) type-check step and the always()
    # summary/upload steps in this job must not itself depend on the type
    # check's result in a way that would fail the run. Concretely: nothing
    # in the job uses `exit 1`/`set -e`-style failure keyed to mypy output,
    # and the only step allowed to fail is the continue-on-error-shielded
    # type-check step itself.
    canonical = _canonical_type_check_command()
    job = _type_health_job()
    for step in job["steps"]:
        run = step.get("run", "")
        if run.strip() == canonical:
            continue
        # Only the canonical, continue-on-error-shielded step may invoke the
        # type checker as a command; a bare reference inside a comment/echo
        # string (e.g. this file's own summary text) is not an invocation.
        # Split each line on shell separators first so a chained/suffixed
        # variant (`uv run mypy omniagentos; exit 1`, `... && exit 1`)
        # can't dodge detection by never matching a whole line.
        invokes_type_check = any(
            piece.strip() == canonical or piece.strip().startswith(canonical + " ")
            for line in run.splitlines()
            for piece in _COMMAND_SEPARATORS.split(line)
        )
        assert not invokes_type_check, (
            f"unexpected unshielded type-check invocation in step {step.get('name')!r}"
        )


def _status_line(summary: str) -> str:
    """The single `- Type check: ...` verdict line, isolated from the
    always-present explanatory footer (which legitimately mentions both
    GREEN and RED in prose describing the mechanism, not the verdict)."""
    for line in summary.splitlines():
        if line.strip().startswith("- Type check:"):
            return line
    raise AssertionError(f"no `- Type check:` verdict line found in: {summary!r}")


def test_summary_reports_green_only_on_a_genuine_success(tmp_path) -> None:
    exit_code, summary = _execute_step_script(
        _type_health_summary_step()["run"], "success", tmp_path
    )
    # MH-005: the normal script must actually exit 0 — a script that
    # non-zero-exits without a test noticing is exactly the gap that lets a
    # summary failure hide behind continue-on-error with nothing written.
    assert exit_code == 0
    line = _status_line(summary)
    assert "GREEN" in line
    assert "RED" not in line


def test_summary_reports_red_only_on_a_genuine_failure(tmp_path) -> None:
    # A genuine mypy failure (the type-check step actually ran and found
    # errors) is the ONLY outcome allowed to claim main carries type errors.
    exit_code, summary = _execute_step_script(
        _type_health_summary_step()["run"], "failure", tmp_path
    )
    assert exit_code == 0
    line = _status_line(summary)
    assert "RED" in line
    assert "carries type errors" in line
    assert "GREEN" not in line


def test_summary_does_not_claim_type_errors_when_the_step_was_skipped(
    tmp_path,
) -> None:
    # Finding 1: if checkout/setup/`uv sync` fails upstream, GitHub's
    # default success() condition SKIPS the type-check step. The summary
    # step is always()-conditioned so it still runs — it must render that
    # as a DISTINCT, clearly-labelled instrument gap, never as "main
    # carries type errors", which conflates instrument failure with a
    # genuine type-red result.
    exit_code, summary = _execute_step_script(
        _type_health_summary_step()["run"], "skipped", tmp_path
    )
    assert exit_code == 0
    line = _status_line(summary)
    assert "carries type errors" not in line
    assert "GREEN" not in line
    assert re.search(r"UNKNOWN|INSTRUMENT", line), (
        f"expected a clearly distinct non-red label for a skipped step, got: {line!r}"
    )


def test_summary_does_not_claim_type_errors_when_the_step_was_cancelled(
    tmp_path,
) -> None:
    exit_code, summary = _execute_step_script(
        _type_health_summary_step()["run"], "cancelled", tmp_path
    )
    assert exit_code == 0
    line = _status_line(summary)
    assert "carries type errors" not in line
    assert "GREEN" not in line
    assert re.search(r"UNKNOWN|INSTRUMENT", line), (
        f"expected a clearly distinct non-red label for a cancelled step, got: {line!r}"
    )


def test_no_step_after_type_check_can_propagate_a_failure(tmp_path) -> None:
    # Finding 2: the load-bearing non-blocking guarantee, tested by actually
    # EXECUTING every downstream step's script with the type-check outcome
    # forced to "failure" (the worst case for triggering a downstream
    # re-raise) and checking whether the script itself would exit nonzero.
    # A step whose rendered script can fail the job on a genuine type-red
    # result MUST be shielded by continue-on-error: true, or it silently
    # turns this observation job back into a blocking gate.
    for step in _steps_after_type_check():
        run = step.get("run")
        if run is None:
            continue  # a `uses:` step; nothing of ours to execute
        exit_code, _ = _execute_step_script(run, "failure", tmp_path)
        if exit_code != 0:
            assert step.get("continue-on-error") is True, (
                f"step {step.get('name')!r} exits {exit_code} when steps.type_check.outcome "
                "is 'failure' and is NOT shielded by continue-on-error: true — this can "
                "turn the observation job into a blocking gate on a genuine type-red result."
            )


def test_every_step_after_type_check_carries_continue_on_error() -> None:
    # This is the guard that actually holds the line (round 3): a structural
    # property read straight from the parsed YAML, immune to bypasses the
    # execution-based test above can't chase (env indirection, a `uses:`
    # action, or any other way of reading the outcome).
    _assert_steps_after_type_check_shielded(_type_health_job()["steps"])


# --- Negative controls: the three bypass shapes a reviewer demonstrated
# defeat run:-only emulation but must be rejected by the structural guard.
# Each is an UNSHIELDED synthetic step appended after a minimal type_check
# step; the structural assertion must FAIL for all three.

_SYNTHETIC_TYPE_CHECK_STEP = {
    "id": "type_check",
    "run": "uv run mypy omniagentos",
    "continue-on-error": True,
}


def test_structural_guard_rejects_unshielded_plain_run_reraise() -> None:
    steps = [
        _SYNTHETIC_TYPE_CHECK_STEP,
        {
            "name": "plain run re-raise",
            "run": 'if [ "${{ steps.type_check.outcome }}" = "failure" ]; then exit 1; fi',
        },
    ]
    with pytest.raises(AssertionError):
        _assert_steps_after_type_check_shielded(steps)


def test_structural_guard_rejects_unshielded_env_indirection() -> None:
    # Bypass 1 from round 3: the outcome is read via an `env:` mapping
    # rather than inline in `run:`, which execution-based emulation that
    # never renders/exports a step's own env: never catches.
    steps = [
        _SYNTHETIC_TYPE_CHECK_STEP,
        {
            "name": "env-indirected re-raise",
            "env": {"TYPE_CHECK_OUTCOME": "${{ steps.type_check.outcome }}"},
            "run": 'if [ "$TYPE_CHECK_OUTCOME" = "failure" ]; then exit 23; fi',
        },
    ]
    with pytest.raises(AssertionError):
        _assert_steps_after_type_check_shielded(steps)


def test_structural_guard_rejects_unshielded_uses_action() -> None:
    # Bypass 2 from round 3: a `uses:` action step (no `run:` at all) with
    # an `if:` gated on the outcome, calling core.setFailed — execution-
    # based emulation that only walks `run:` steps skips this entirely.
    steps = [
        _SYNTHETIC_TYPE_CHECK_STEP,
        {
            "name": "action re-raise",
            "if": "steps.type_check.outcome == 'failure'",
            "uses": "actions/github-script@v7",
            "with": {"script": "core.setFailed('type check failed')"},
        },
    ]
    with pytest.raises(AssertionError):
        _assert_steps_after_type_check_shielded(steps)


# --- MH-004 (round 5): the location logic itself could pass vacuously —
# a `next(...)` scan silently accepts the FIRST match without requiring
# uniqueness, an empty downstream suffix, or correct ordering. These
# negative controls construct all three demonstrated bypass shapes and
# assert `_resolve_type_health_structure` (which now backs every lookup
# helper in this file) rejects each one.


def test_structure_guard_rejects_summary_step_before_canonical_type_check_step() -> (
    None
):
    # Shape (a): the outcome-reading step runs BEFORE the canonical
    # type-check step. On real Actions it cannot see an outcome that has
    # not been produced yet, so it is structurally incapable of reporting
    # anything meaningful — however its script reads the (nonexistent, at
    # that point) outcome. A trailing unrelated step keeps the downstream
    # suffix non-empty, so this fails on ORDERING specifically, not on the
    # separate empty-suffix check below.
    canonical = _canonical_type_check_command()
    steps = [
        {
            "name": "Summarize type-health (misordered)",
            "run": 'echo "${{ steps.type_check.outcome }}"',
            "continue-on-error": True,
        },
        {
            "name": "Check Python types",
            "id": "type_check",
            "continue-on-error": True,
            "run": canonical,
        },
        {
            "name": "Upload artifact",
            "uses": "actions/upload-artifact@v4",
            "continue-on-error": True,
        },
    ]
    with pytest.raises(AssertionError):
        _resolve_type_health_structure(steps)


def test_structure_guard_rejects_canonical_type_check_step_with_no_downstream_steps() -> (
    None
):
    # Shape (a)'s degenerate twin: the canonical type-check step is
    # literally the LAST step in the job — nothing runs after it at all,
    # so there is no possible verdict step and nothing to guard.
    canonical = _canonical_type_check_command()
    steps = [
        {
            "name": "Check Python types",
            "id": "type_check",
            "continue-on-error": True,
            "run": canonical,
        }
    ]
    with pytest.raises(AssertionError):
        _resolve_type_health_structure(steps)


def test_structure_guard_rejects_duplicate_type_check_id() -> None:
    # Shape (b): GitHub Actions requires step ids to be unique within a
    # job; a duplicate `id: type_check` makes `steps.type_check.outcome`
    # refer to an unspecified one of the two steps. The prior `next(...)`
    # scan silently accepted the first match; this must fail loudly.
    canonical = _canonical_type_check_command()
    steps = [
        {
            "name": "Check Python types",
            "id": "type_check",
            "continue-on-error": True,
            "run": canonical,
        },
        {
            "name": "Duplicate id step",
            "id": "type_check",
            "run": "echo hi",
            "continue-on-error": True,
        },
        {
            "name": "Summarize type-health",
            "run": 'echo "${{ steps.type_check.outcome }}"',
            "continue-on-error": True,
        },
    ]
    with pytest.raises(AssertionError):
        _resolve_type_health_structure(steps)


def test_structure_guard_accepts_a_well_formed_synthetic_job() -> None:
    # Positive control: the same shape as the three negative controls
    # above, but correctly ordered with a unique id — must NOT raise.
    canonical = _canonical_type_check_command()
    steps = [
        {
            "name": "Check Python types",
            "id": "type_check",
            "continue-on-error": True,
            "run": canonical,
        },
        {
            "name": "Summarize type-health",
            "run": 'echo "${{ steps.type_check.outcome }}"',
            "continue-on-error": True,
        },
    ]
    _resolve_type_health_structure(steps)  # must not raise


def test_type_health_structure_resolves_cleanly_on_real_workflow() -> None:
    # The real workflow must satisfy every MH-004 precondition on its own,
    # independent of any other test exercising it indirectly.
    _resolve_type_health_structure()  # must not raise


# --- MH-005 (round 5): continue-on-error: true on the summary step hides a
# genuine failure of the summary step's OWN script — its outcome is
# "failure" but its conclusion is "success" (the job stays green) and
# NOTHING is written to GITHUB_STEP_SUMMARY. A shielded fallback step must
# make that failure VISIBLE (an ::error annotation plus a minimal verdict)
# without ever being able to fail the run itself.


def test_summary_step_has_a_stable_id() -> None:
    step = _type_health_summary_step()
    assert step.get("id"), "expected the summary step to carry a stable `id:`"


def test_fallback_step_exists_after_summary_and_is_shielded() -> None:
    fallback = _type_health_summary_fallback_step()
    assert fallback.get("if") == "always()", (
        "the fallback must run regardless of the summary step's outcome"
    )
    assert fallback.get("continue-on-error") is True, (
        "the fallback step itself must never be able to fail the run — it "
        "exists to make a failure VISIBLE, not to become a new way to fail"
    )


def test_fallback_step_reports_instrument_red_when_summary_failed(tmp_path) -> None:
    fallback = _type_health_summary_fallback_step()
    exit_code, summary = _execute_step_script(fallback["run"], "failure", tmp_path)
    assert exit_code == 0, "the fallback step must never itself exit nonzero"
    line = _status_line(summary)
    assert "carries type errors" not in line
    assert re.search(r"UNKNOWN|INSTRUMENT", line), (
        f"expected a distinct instrument-failure label, got: {line!r}"
    )


def test_fallback_step_emits_an_error_annotation_when_summary_failed(
    tmp_path,
) -> None:
    fallback = _type_health_summary_fallback_step()
    script = _render_run_script(fallback["run"], "failure")
    summary_file = tmp_path / "step_summary.txt"
    summary_file.write_text("")
    env = {**os.environ, "GITHUB_STEP_SUMMARY": str(summary_file)}
    result = subprocess.run(
        ["bash", "-c", script], env=env, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0
    assert "::error::" in result.stdout, (
        "expected the fallback to emit a GitHub ::error annotation when the "
        f"summary step failed; got stdout: {result.stdout!r}"
    )


def test_fallback_step_is_silent_when_summary_succeeded(tmp_path) -> None:
    # The fallback must be a no-op when there is nothing to report — it
    # must not overwrite or duplicate a genuine verdict the summary step
    # already wrote.
    fallback = _type_health_summary_fallback_step()
    script = _render_run_script(fallback["run"], "success")
    summary_file = tmp_path / "step_summary.txt"
    summary_file.write_text("")
    env = {**os.environ, "GITHUB_STEP_SUMMARY": str(summary_file)}
    result = subprocess.run(
        ["bash", "-c", script], env=env, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0
    assert "::error::" not in result.stdout
    assert summary_file.read_text() == ""


def test_structural_guard_accepts_shielded_variants_of_all_three_shapes() -> None:
    # A positive control: the same three shapes, each correctly shielded
    # with continue-on-error: true, must NOT trip the guard.
    steps = [
        _SYNTHETIC_TYPE_CHECK_STEP,
        {
            "name": "plain run re-raise (shielded)",
            "run": 'if [ "${{ steps.type_check.outcome }}" = "failure" ]; then exit 1; fi',
            "continue-on-error": True,
        },
        {
            "name": "env-indirected re-raise (shielded)",
            "env": {"TYPE_CHECK_OUTCOME": "${{ steps.type_check.outcome }}"},
            "run": 'if [ "$TYPE_CHECK_OUTCOME" = "failure" ]; then exit 23; fi',
            "continue-on-error": True,
        },
        {
            "name": "action re-raise (shielded)",
            "if": "steps.type_check.outcome == 'failure'",
            "uses": "actions/github-script@v7",
            "with": {"script": "core.setFailed('type check failed')"},
            "continue-on-error": True,
        },
    ]
    _assert_steps_after_type_check_shielded(steps)  # must not raise
