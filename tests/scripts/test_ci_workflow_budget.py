"""The test job's dynamic budget only beats its job cap if the numbers agree.

ci.yml's `test` job computes ``budget = JOB_BUDGET - elapsed_since_first_step
- 120`` and runs the suite under ``timeout -k 60``, so its forced exit lands
no later than JOB_START + JOB_BUDGET - 60.  A job-level `timeout-minutes`
cancellation is NOT shielded by continue-on-error, and the cap starts
charging at job start while JOB_START is only recorded by the first step —
the runner's "Set up job"/action-download phase runs before any step.  The
cap must therefore exceed JOB_BUDGET by a pre-step reserve.  Cap, budget,
and the in-step arithmetic are independent YAML literals: an isolated edit
to any one of them silently recreates the green-runs-conclude-`cancelled`
race this arrangement exists to prevent.
"""

from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"
PRE_STEP_RESERVE_SECONDS = 600


def _test_job() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())["jobs"]["test"]


def test_job_cap_covers_budget_plus_pre_step_reserve() -> None:
    job = _test_job()
    cap_seconds = int(job["timeout-minutes"]) * 60
    job_budget = int(job["env"]["JOB_BUDGET"])
    assert cap_seconds - job_budget >= PRE_STEP_RESERVE_SECONDS


def test_first_step_records_job_start() -> None:
    first_run = _test_job()["steps"][0].get("run", "")
    assert first_run.strip() == 'echo "JOB_START=$(date +%s)" >> "$GITHUB_ENV"'


def test_both_test_steps_run_under_the_computed_budget() -> None:
    runs = [step.get("run", "") for step in _test_job()["steps"]]
    budgeted = [r for r in runs if "make test-pr" in r]
    assert len(budgeted) == 2
    for run in budgeted:
        # Exact formula including the 120s in-step reserve — a weakened
        # reserve or a deleted guard must fail here, not slip through on
        # substring luck.
        assert "budget=$(( JOB_BUDGET - ($(date +%s) - JOB_START) - 120 ))" in run
        assert '[ "$budget" -lt 300 ]' in run
        assert "exit 1" in run
        assert 'timeout -k 60 "${budget}s"' in run


def test_test_job_is_not_shielded_by_continue_on_error() -> None:
    """Ratchet (review 2026-08-14): the `test` job is BLOCKING — a red lane must
    fail the run. `continue-on-error: true` re-added at the job level or on any
    step would silently restore the accumulate-reds-in-the-dark class this
    removal closed, so pin its absence here rather than trust a code comment."""
    job = _test_job()
    assert job.get("continue-on-error") is not True, (
        "the `test` job carries continue-on-error at the job level — it is meant "
        "to block; a red test lane must fail the run, not be shielded"
    )
    for step in job.get("steps", []):
        assert step.get("continue-on-error") is not True, (
            f"step {step.get('name')!r} in the `test` job carries "
            "continue-on-error: true — an unshielded blocking job must have none"
        )
