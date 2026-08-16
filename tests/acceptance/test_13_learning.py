"""AT4 area 13 — Learning system.

Acceptance claims under test:

  13.1  Successful patterns are captured to the ledger/skill corpus.
  13.2  Failures land in the failure ledger and are readable back.
  13.3  Hypotheses are created (and are informed by prior failures).
  13.4  New tests are suggested from failures.       <- NOT IMPLEMENTED (xfail)
  13.5  **Nothing is promoted without repeated evidence.**

13.5 is the load-bearing one and it is asserted by DRIVING THE REAL GATE, not by
reading a constant: the same challenger, the same champion, the same suite and
the same faked model responses are run twice, changing only
``Experiment.budgets.replicates``. At ``replicates=1`` the campaign produces an
*identical* scorecard (same ``primary_delta``, same ``utility``, no audit flags)
and still REJECTS, because a single observation cannot establish a confidence
interval and ``_stable`` therefore returns ``False``. Delete the reproducibility
term from ``_finish`` and this file goes red.

Hermetic: ``LabStore(":memory:")``, ``ProtectedGrader(":memory:")``, every
filesystem root pinned under ``tmp_path`` by the ``isolated_roots`` fixture, and
``dry_run=True`` through ``MockAdapter``. No network, no real model call.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import omniagentos.lab.campaign as campaign
from omniagentos.contracts import (
    AgentUsage,
    HarnessProfile,
    HarnessType,
    RunManifest,
    RunState,
)
from omniagentos.lab.contracts import (
    Budgets,
    Disposition,
    Experiment,
    ExperimentStatus,
    PromotionThreshold,
    Scorecard,
    SurfaceKind,
)
from omniagentos.ledger import append_manifest, read_manifests

from .conftest import make_experiment

# ---------------------------------------------------------------------------
# 13.5 — nothing is promoted without repeated evidence  (THE assertion)
# ---------------------------------------------------------------------------


@pytest.mark.acceptance_smoke
def test_promotion_is_blocked_without_repeated_evidence(
    offline_lab: tuple[Any, Any, str],
) -> None:
    """A single replicate CANNOT promote, however good the numbers look.

    This drives the production ``run_experiment`` gate end to end. The
    challenger is genuinely better (``primary_delta == 0.25`` against a
    ``primary_delta_min`` of 0.03) and carries no audit flags, so every numeric
    threshold passes -- the ONLY thing standing between it and promotion is that
    the improvement was observed once rather than replicated.
    """
    store, evaluator, suite_id = offline_lab
    exp_id = make_experiment(
        store,
        suite_id,
        challenger_prompt="WINNING",
        exp_id="exp_single_replicate",
        replicates=1,
    )

    disposition = campaign.run_experiment(store, evaluator, exp_id, dry_run=True)

    assert disposition is Disposition.REJECT, (
        f"a one-replicate experiment must never promote; got {disposition!r}"
    )

    row = store.get_experiment(exp_id)
    assert row is not None
    scorecard = json.loads(row["scorecard_json"])
    # The rejection is NOT a numeric miss and NOT an integrity flag: prove it,
    # otherwise this test could pass for the wrong reason.
    assert scorecard["primary_delta"] == pytest.approx(0.25)
    assert scorecard["utility"] == pytest.approx(0.25)
    assert scorecard["safety_regression"] is False
    assert scorecard["audit_flags"] == []
    # The actual cause: no interval could be estimated from one observation.
    assert scorecard["confidence_interval"] is None, (
        "a single replicate must not yield a confidence interval; a zero-width "
        "interval here would silently satisfy the reproducibility gate"
    )
    # The experiment never reached the held-out split (the dev gate stopped it).
    assert {row["split"] for row in store.eval_results(exp_id)} == {"dev"}


@pytest.mark.acceptance_smoke
def test_promotion_succeeds_once_the_evidence_repeats(
    offline_lab: tuple[Any, Any, str],
) -> None:
    """The control for the test above: replicate it and the SAME change promotes.

    Without this pair, ``test_promotion_is_blocked_without_repeated_evidence``
    would also pass if the gate rejected everything unconditionally.
    """
    store, evaluator, suite_id = offline_lab
    exp_id = make_experiment(
        store,
        suite_id,
        challenger_prompt="WINNING",
        exp_id="exp_replicated",
        replicates=2,
    )

    disposition = campaign.run_experiment(store, evaluator, exp_id, dry_run=True)

    assert disposition is Disposition.PROMOTE
    row = store.get_experiment(exp_id)
    assert row is not None
    scorecard = json.loads(row["scorecard_json"])
    # Same quality signal as the single-replicate run -- only the evidence differs.
    assert scorecard["primary_delta"] == pytest.approx(0.25)
    assert scorecard["utility"] == pytest.approx(0.25)
    assert scorecard["confidence_interval"] is not None
    # Held-out scoring only unlocks after the dev gate passes.
    assert {row["split"] for row in store.eval_results(exp_id)} == {"dev", "held_out"}


@pytest.mark.acceptance_daily
def test_reproducibility_term_is_load_bearing_in_the_disposition_gate() -> None:
    """``_finish`` must refuse PROMOTE on ``reproducible=False`` alone.

    Unit-level companion to the end-to-end pair above: everything else is held
    passing (clean numerics, no audit flags, a valid rollback target) so the
    reproducibility flag is the single variable.
    """
    experiment = Experiment(
        id="exp_finish",
        hypothesis="reproducibility is enforced at the disposition gate",
        discipline="at4",
        mutable_surface_kind=SurfaceKind.PROMPT,
        champion_surface_id="srf_champion",
        challenger_surface_id="srf_challenger",
        eval_suite_id="evs_at4",
        primary_metric="accuracy",
        budgets=Budgets(replicates=2),
    )
    store = _GateStore()

    def card() -> Scorecard:
        return Scorecard(primary_delta=0.2, utility=0.2, cost_delta=0.0, complexity_delta=0.0)

    assert campaign._finish(store, experiment, card(), True) is Disposition.PROMOTE
    assert campaign._finish(store, experiment, card(), False) is Disposition.REJECT


def test_underpowered_replicates_are_flagged_by_methodology_review() -> None:
    """The campaign names the defect rather than silently rejecting."""
    experiment = Experiment(
        id="exp_underpowered",
        hypothesis="one replicate is not evidence",
        discipline="at4",
        mutable_surface_kind=SurfaceKind.PROMPT,
        champion_surface_id="srf_champion",
        challenger_surface_id="srf_challenger",
        eval_suite_id="evs_at4",
        primary_metric="accuracy",
        budgets=Budgets(replicates=1),
    )
    suite = {"metrics": [{"name": "accuracy", "role": "primary"}]}

    review = campaign.review_methodology(experiment, suite)

    assert "methodology:underpowered-replicates" in review.audit_flags
    # And two replicates clears it -- otherwise the flag would be unconditional.
    replicated = experiment.model_copy(update={"budgets": Budgets(replicates=2)})
    assert (
        "methodology:underpowered-replicates"
        not in campaign.review_methodology(replicated, suite).audit_flags
    )


@pytest.mark.acceptance_daily
def test_audit_flags_can_never_reach_promote(offline_lab: tuple[Any, Any, str]) -> None:
    """Any integrity flag routes to HUMAN_REVIEW, never PROMOTE.

    A flagged metric jump is the classic reward-hack signature; it must not be
    able to buy a promotion by also clearing the numeric thresholds.
    """
    store, _evaluator, _suite_id = offline_lab
    experiment = Experiment(
        id="exp_flagged",
        hypothesis="a flagged scorecard cannot promote",
        discipline="at4",
        mutable_surface_kind=SurfaceKind.PROMPT,
        champion_surface_id="srf_champion",
        challenger_surface_id="srf_challenger",
        eval_suite_id="evs_at4",
        primary_metric="accuracy",
        budgets=Budgets(replicates=4),
    )
    gate_store = _GateStore()
    flagged = Scorecard(
        primary_delta=0.9,
        utility=0.9,
        cost_delta=0.0,
        complexity_delta=0.0,
        audit_flags=["metric-jump-implausible"],
    )

    assert campaign._finish(gate_store, experiment, flagged, True) is Disposition.HUMAN_REVIEW


def test_numeric_gate_rejects_an_empty_scorecard() -> None:
    """Missing measurements are a rejection, never a default pass (fail-closed)."""
    assert campaign._meets_numeric_gate(Scorecard(), PromotionThreshold()) is False
    assert (
        campaign._meets_numeric_gate(
            Scorecard(primary_delta=0.2, utility=0.2, cost_delta=0.0, complexity_delta=0.0),
            PromotionThreshold(),
        )
        is True
    )


@pytest.mark.parametrize(
    ("agreement", "validity", "expected"),
    [
        (0.90, 0.90, True),
        (campaign.MIN_JUDGE_AGREEMENT, campaign.MIN_JUDGE_VALIDITY, True),
        (campaign.MIN_JUDGE_AGREEMENT - 0.01, 0.90, False),
        (0.90, campaign.MIN_JUDGE_VALIDITY - 0.01, False),
        (None, 0.90, False),
        (0.90, None, False),
        (float("nan"), 0.90, False),
        (0.90, float("inf"), False),
    ],
)
@pytest.mark.acceptance_daily
def test_evidence_floor_is_fail_closed(
    agreement: float | None, validity: float | None, expected: bool
) -> None:
    """The always-on judge-evidence backstop rejects missing/NaN/infinite input.

    Asserting the boundary in BOTH directions is what makes this a real test:
    a floor that always returned ``False`` would fail the first two rows, and a
    floor that always returned ``True`` would fail the rest.
    """
    assert campaign._evidence_floor_met(agreement, validity) is expected


# ---------------------------------------------------------------------------
# 13.1 / 13.2 — successful patterns to the ledger, failures to the failure ledger
# ---------------------------------------------------------------------------


def _manifest(run_id: str, state: RunState, *, task_id: str = "task-at4") -> RunManifest:
    return RunManifest(
        run_id=run_id,
        task_id=task_id,
        discipline="at4",
        harness=HarnessProfile(harness=HarnessType.FUSION, version="1", env_hash="env-at4"),
        state=state,
        started_at="2026-07-27T09:00:00Z",
        finished_at="2026-07-27T09:05:00Z",
        usage=AgentUsage(wall_ms=1000, input_tokens=10, output_tokens=20, cost_usd=0.01),
        output_digest=f"sha256:{run_id}",
    )


@pytest.mark.acceptance_daily
def test_successful_runs_and_failures_both_reach_the_append_only_ledger(
    tmp_path: Path,
) -> None:
    """One ledger, two outcomes: a success and a failure are both readable back.

    The ledger is the failure ledger -- it is append-only and terminal-state
    keyed, so a failed run is retained with its state rather than dropped.
    """
    ledger_dir = str(tmp_path / "ledger")
    append_manifest(ledger_dir, _manifest("run-ok", RunState.COMPLETED))
    append_manifest(ledger_dir, _manifest("run-bad", RunState.FAILED))

    states = {m.run_id: m.state for m in read_manifests(ledger_dir, limit=50)}

    assert states == {"run-ok": RunState.COMPLETED, "run-bad": RunState.FAILED}
    # Failures must be retrievable on their own -- that is what makes the ledger
    # usable as a failure corpus.
    failures = [m for m in read_manifests(ledger_dir, limit=50) if m.state is RunState.FAILED]
    assert [m.run_id for m in failures] == ["run-bad"]


def test_ledger_append_is_idempotent_and_never_rewrites_history(tmp_path: Path) -> None:
    """Re-appending the same run_id must not duplicate or mutate the record."""
    ledger_dir = str(tmp_path / "ledger")
    path = Path(append_manifest(ledger_dir, _manifest("run-once", RunState.COMPLETED)))
    first = path.read_text(encoding="utf-8")

    # Same run_id, DIFFERENT terminal state: the ledger must keep the original.
    append_manifest(ledger_dir, _manifest("run-once", RunState.FAILED))

    assert path.read_text(encoding="utf-8") == first
    manifests = read_manifests(ledger_dir, limit=50)
    assert len(manifests) == 1
    assert manifests[0].state is RunState.COMPLETED


@pytest.mark.acceptance_smoke
def test_only_verified_runs_are_captured_as_learned_skills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Success -> skill corpus; failure -> recorded as unverified, nothing written.

    This is the "successful patterns are captured" claim with its guard intact:
    ``curate`` routes EVERY manifest through a real ``VerificationGate``, and a
    non-PASSED gate must produce no vault note at all.
    """
    from omniagentos.selfimprove.curator import curate

    ledger_dir = str(tmp_path / "ledger")
    vault_dir = tmp_path / "vault"
    skills_dir = tmp_path / "skills"
    append_manifest(ledger_dir, _manifest("run-pass", RunState.COMPLETED, task_id="passing-task"))
    append_manifest(ledger_dir, _manifest("run-fail", RunState.FAILED, task_id="failing-task"))

    result = curate(
        ledger_dir=ledger_dir,
        vault_dir=str(vault_dir),
        skills_dir=str(skills_dir),
        autocommit=False,
        skills_api=None,
    )

    assert result.scanned == 2
    assert result.captured == ["run-pass"], f"expected only the passing run captured: {result}"
    assert result.unverified == ["run-fail"], f"expected the failing run rejected: {result}"
    notes = [p.name for p in vault_dir.rglob("*.md")]
    assert notes, "a verified run must leave a durable vault note"
    assert not any("run-fail" in name for name in notes)


# ---------------------------------------------------------------------------
# 13.3 — hypotheses are created, and prior failures inform them
# ---------------------------------------------------------------------------


@pytest.mark.acceptance_daily
def test_experiment_proposals_carry_a_hypothesis_and_a_policy_mix(
    offline_lab: tuple[Any, Any, str],
) -> None:
    """``propose_experiments`` creates persisted, hypothesis-bearing experiments."""
    store, _evaluator, suite_id = offline_lab
    # Seed a champion + one prior experiment so the proposal loop has context.
    make_experiment(store, suite_id, challenger_prompt="LOSING", exp_id="exp_prior", replicates=2)

    proposals = campaign.propose_experiments(store, "at4")

    assert proposals, "a discipline with a champion and a suite must yield proposals"
    for proposal in proposals:
        assert proposal.hypothesis.strip(), "every proposal must state a hypothesis"
        assert proposal.status is ExperimentStatus.PROPOSED
        assert proposal.eval_suite_id == suite_id
        # Persisted, not just returned in memory.
        assert store.get_experiment(proposal.id) is not None
    # The portfolio explores rather than only exploiting.
    assert len({p.explore_policy for p in proposals}) > 1


@pytest.mark.acceptance_daily
def test_a_rejected_experiment_is_visible_to_the_proposal_loop(
    offline_lab: tuple[Any, Any, str],
) -> None:
    """Failures feed hypothesis generation instead of being discarded."""
    store, evaluator, suite_id = offline_lab
    exp_id = make_experiment(
        store, suite_id, challenger_prompt="LOSING", exp_id="exp_rejected", replicates=2
    )
    assert campaign.run_experiment(store, evaluator, exp_id, dry_run=True) is Disposition.REJECT

    failed = campaign._failed_experiments(store, "at4")

    assert exp_id in {str(row["id"]) for row in failed}


@pytest.mark.acceptance_daily
def test_stall_check_reports_a_campaign_with_no_promotions(
    offline_lab: tuple[Any, Any, str],
) -> None:
    """The learner notices when it has stopped learning."""
    store, evaluator, suite_id = offline_lab
    rejected = make_experiment(
        store, suite_id, challenger_prompt="LOSING", exp_id="exp_stall", replicates=2
    )
    campaign.run_experiment(store, evaluator, rejected, dry_run=True)

    assert campaign.stall_check(store, "at4") is True

    promoted = make_experiment(
        store, suite_id, challenger_prompt="WINNING", exp_id="exp_unstall", replicates=2
    )
    assert campaign.run_experiment(store, evaluator, promoted, dry_run=True) is Disposition.PROMOTE

    assert campaign.stall_check(store, "at4") is False


# ---------------------------------------------------------------------------
# 13.4 — new tests suggested from failures: NOT IMPLEMENTED
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason=(
        "AT4 gap: nothing in omniagentos proposes NEW eval cases (or pytest tests) "
        "from a failure. `propose_experiments` only re-versions existing surfaces "
        "against an existing suite (`_proposal_eval_context` reuses a prior "
        "`eval_suite_id`); no code path calls `LabStore.add_eval_case` in response "
        "to a rejection. See docs/acceptance/gaps-AT4.md."
    ),
)
@pytest.mark.acceptance_daily
def test_failures_suggest_new_eval_cases(offline_lab: tuple[Any, Any, str]) -> None:
    store, evaluator, suite_id = offline_lab
    exp_id = make_experiment(
        store, suite_id, challenger_prompt="LOSING", exp_id="exp_suggests", replicates=2
    )
    before = len(store.candidate_cases(suite_id, "dev"))

    campaign.run_experiment(store, evaluator, exp_id, dry_run=True)
    campaign.propose_experiments(store, "at4")

    after = len(store.candidate_cases(suite_id, "dev"))
    assert after > before, "a rejection should widen the eval suite with a new case"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class _GateStore:
    """The minimum store surface ``campaign._finish`` reads.

    Deliberately NOT a mock of the gate: the gate logic under test runs for
    real. This only supplies the surrounding rows (suite, challenger surface,
    champion registry entry) so a valid rollback target exists and the
    disposition turns purely on the arguments the test varies.
    """

    def __init__(self) -> None:
        self.updates: list[dict[str, Any]] = []

    def get_eval_suite(self, suite_id: str) -> dict[str, Any]:
        return {"id": suite_id, "metrics": [{"name": "accuracy", "role": "primary"}]}

    def get_surface(self, surface_id: str) -> dict[str, Any]:
        return {
            "id": surface_id,
            "kind": SurfaceKind.PROMPT.value,
            "safety_relevant": False,
            "status": "champion",
        }

    def get_champion(self, discipline: str, kind: str) -> dict[str, Any]:
        return {
            "discipline": discipline,
            "surface_kind": kind,
            "surface_id": "srf_champion",
            "rollback_to_surface_id": None,
        }

    def update_experiment(self, exp_id: str, fields: dict[str, Any]) -> None:
        self.updates.append({"id": exp_id, **fields})
