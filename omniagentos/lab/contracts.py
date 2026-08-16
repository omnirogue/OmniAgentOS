"""Frozen H2 (self-improvement lab) domain contracts.

Single source of truth for the empirical engine + tournament + curation + UI.
Owned by the lead; lab packages import from here and MUST NOT edit it. Builds on
omniagentos/contracts.py (H1). Methods: blueprint Section 11 + /improveswarm.

Reward-hacking invariant (Section 11.7), enforced by types + the eval package:
held-out expected values live ONLY in EvalCase.expected for split=HELD_OUT and are
NEVER serialized into any candidate-facing payload (CandidateEvalCase strips them).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from omniagentos.contracts import new_id, utc_now_iso

LAB_SCHEMA_VERSION = 3  # additive migration 003 on top of H1's 001/002


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class SurfaceKind(StrEnum):
    """A mutable surface a challenger may change (blueprint Section 11.1)."""

    PROMPT = "prompt"  # vault/prompts/<role>/*.md
    ORCHESTRATION_GENOME = "orchestration_genome"  # configs/genomes/*.json (improveswarm)
    MODEL_ASSIGNMENT = "model_assignment"
    ROUTING_POLICY = "routing_policy"
    REVIEW_RUBRIC = "review_rubric"


class SurfaceStatus(StrEnum):
    CHAMPION = "champion"
    CHALLENGER = "challenger"
    ARCHIVED = "archived"
    DRAFT = "draft"


class EvalSplit(StrEnum):
    DEV = "dev"  # candidate-visible
    HELD_OUT = "held_out"  # candidate NEVER sees expected values


class ExperimentStatus(StrEnum):
    PROPOSED = "proposed"
    RUNNING_BASELINE = "running_baseline"
    RUNNING_CHALLENGER = "running_challenger"
    EVALUATING = "evaluating"
    REPLICATING = "replicating"
    DECIDED = "decided"
    CANARY = "canary"
    PROMOTED = "promoted"
    ROLLED_BACK = "rolled_back"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Disposition(StrEnum):
    """Section 11.5 keep/reject/invalid/review."""

    PROMOTE = "promote"
    REJECT = "reject"
    INVALID = "invalid"
    HUMAN_REVIEW = "human_review"


class ExplorePolicy(StrEnum):
    """Section 11.6 explore/exploit portfolio."""

    EXPLOIT = "exploit"  # 60% incremental
    EXPLORE = "explore"  # 25% different model/tool/structure
    SIMPLIFY = "simplify"  # 10% remove steps/prompts/agents
    RED_TEAM = "red_team"  # 5% adversarial / failure-seeking


# ---------------------------------------------------------------------------
# Mutable surfaces + champion registry (Section 11.1)
# ---------------------------------------------------------------------------


class Surface(BaseModel):
    id: str = Field(default_factory=lambda: new_id("srf"))
    kind: SurfaceKind
    discipline: str
    version: int = 1
    path: str  # vault/prompts/... or configs/genomes/...
    content_hash: str  # sha256 of the surface content (reproducibility)
    parent_version: int | None = None
    status: SurfaceStatus = SurfaceStatus.DRAFT
    label: str = ""
    # Governance (design review HD-003): a surface that changes how safety/permissions/
    # consequential behavior is decided ALWAYS requires human approval to promote, and
    # can never mutate away a core safety check. safety_relevant is forced True for the
    # kinds in SURFACE_REQUIRES_HUMAN regardless of what a caller passes.
    safety_relevant: bool = False
    created_at: str = Field(default_factory=utc_now_iso)


# Surfaces that redefine how the system judges itself → promotion ALWAYS needs a human
# (blueprint §11.1 "core safety checks remain immutable"; design review HD-003).
SURFACE_REQUIRES_HUMAN: frozenset[SurfaceKind] = frozenset(
    {SurfaceKind.REVIEW_RUBRIC, SurfaceKind.ROUTING_POLICY}
)


def surface_requires_human(kind: SurfaceKind, safety_relevant: bool) -> bool:
    return kind in SURFACE_REQUIRES_HUMAN or safety_relevant


class ChampionEntry(BaseModel):
    discipline: str
    surface_kind: SurfaceKind
    surface_id: str
    surface_version: int
    # Reversibility invariant (design review HD-011): every non-seed promotion sets
    # rollback_to to the OUTGOING champion's surface_id, which must exist and be
    # ARCHIVED-immutable. cas_version guards against the curator/campaign both writing
    # the champion pointer concurrently (HD-006) — set_champion is a compare-and-swap.
    promoted_from_experiment: str | None = None
    rollback_to_surface_id: str | None = None  # None ONLY for the seed champion #0
    cas_version: int = 0
    promoted_at: str = Field(default_factory=utc_now_iso)


# ---------------------------------------------------------------------------
# Protected evaluator (Section 11.2, 11.7)
# ---------------------------------------------------------------------------


class MetricSpec(BaseModel):
    name: str
    role: str  # "primary" | "guardrail" | "hard_constraint" | "efficiency" | "regularizer"
    direction: str = "maximize"  # "maximize" | "minimize"
    threshold: float | None = None  # hard-constraint / promotion minimum


class EvalCase(BaseModel):
    """An eval case's PUBLIC record (shared DB `eval_cases`). For split=HELD_OUT this
    carries input/rubric but its `expected` is stored ONLY in the L02 protected store
    (var/eval_protected.db), NEVER in the shared DB (design review HD-001/HD-ALT) —
    because challenger/judge/curator subprocesses can read the shared DB file. `expected`
    here is populated ONLY for split=DEV (candidate-visible anyway) or when L02 loads a
    held-out case internally for grading."""

    id: str = Field(default_factory=lambda: new_id("evc"))
    suite: str
    split: EvalSplit
    input: dict[str, Any]
    expected: dict[str, Any] | None = (
        None  # DEV only in shared DB; HELD_OUT expected lives in the protected store
    )
    rubric: str = ""
    weight: float = 1.0


class CandidateEvalCase(BaseModel):
    """The ONLY shape an eval case may take inside a candidate/challenger agent's
    context. `expected` is structurally absent — held-out answers never leak
    (Section 11.7). Structural defense (HD-001): held-out expected is not merely
    stripped here — it does not live in any store a candidate subprocess can open."""

    id: str
    split: EvalSplit
    input: dict[str, Any]
    rubric: str = ""


class EvalSuite(BaseModel):
    id: str = Field(default_factory=lambda: new_id("evs"))
    discipline: str
    version: int = 1
    metrics: list[MetricSpec]
    protected: bool = True  # held-out expected inaccessible to candidates
    dataset_hash: str = ""  # version pin for reproducibility
    created_at: str = Field(default_factory=utc_now_iso)


class JudgeRecord(BaseModel):
    """A blind judgment (Section 11.7): the judge saw output WITHOUT candidate identity."""

    id: str = Field(default_factory=lambda: new_id("jdg"))
    experiment_id: str | None = None
    tournament_id: str | None = None
    case_id: str
    arm: str  # "champion" | "challenger" — recorded AFTER judging, not shown to judge
    judge_lineage: str  # which judge model lineage
    blind_token: str  # opaque id the judge saw instead of the arm
    score: float
    notes: str = ""
    rubric_dimension: str = ""


class EvalResult(BaseModel):
    id: str = Field(default_factory=lambda: new_id("evr"))
    experiment_id: str
    arm: str  # "champion" | "challenger"
    suite_id: str
    suite_version: int
    split: EvalSplit
    metrics: dict[str, float]  # aggregate per metric name
    per_case: dict[str, dict[str, float]] = Field(default_factory=dict)
    replicate: int = 0
    deterministic_passed: bool = True
    created_at: str = Field(default_factory=utc_now_iso)


# ---------------------------------------------------------------------------
# Experiment (Section 11.2/11.3) — champion vs challenger
# ---------------------------------------------------------------------------


class Budgets(BaseModel):
    wall_minutes: float | None = None
    tokens: int | None = None
    cost_usd: float | None = None
    replicates: int = 1

    def live_ceiling_errors(self) -> list[str]:
        """Return missing/invalid ceilings for a paid lab execution.

        The ordinary runtime may still use advisory or partial budgets. The
        simulation profile calls this method to enforce the stricter contract
        before any provider invocation.
        """

        errors: list[str] = []
        for field in ("wall_minutes", "tokens", "cost_usd", "replicates"):
            value = getattr(self, field)
            if field not in self.model_fields_set:
                errors.append(f"{field} must be explicitly specified")
            elif (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or float(value) <= 0.0
            ):
                errors.append(f"{field} must be greater than zero")
        return errors


class PromotionThreshold(BaseModel):
    primary_delta_min: float = 0.03
    no_safety_regressions: bool = True
    cost_increase_max: float = 0.10
    min_utility: float = 0.0  # complexity-regularized utility floor (HD-010; §11.6)
    max_complexity_delta: float = 0.25  # cap step/agent/prompt-size growth (ties → simpler)
    require_human: bool = False  # forced True for SURFACE_REQUIRES_HUMAN / safety_relevant


class Scorecard(BaseModel):
    """The multi-objective comparison (Section 11.2). utility drives keep-the-simpler ties."""

    champion: dict[str, float] = Field(default_factory=dict)
    challenger: dict[str, float] = Field(default_factory=dict)
    primary_delta: float | None = None
    utility: float | None = None  # quality_gain - cost - latency - complexity - risk
    cost_delta: float | None = None
    complexity_delta: float | None = None
    confidence_interval: list[float] | None = None
    safety_regression: bool = False
    # Metric-jump audit (HD-005): non-empty → disposition is forced to HUMAN_REVIEW and the
    # flags are persisted + shown in the UI/vault. A flagged jump is the classic reward-hack
    # signature; it must never pass silently through the numeric gates.
    audit_flags: list[str] = Field(default_factory=list)


class Experiment(BaseModel):
    id: str = Field(default_factory=lambda: new_id("exp"))
    hypothesis: str
    discipline: str
    explore_policy: ExplorePolicy = ExplorePolicy.EXPLOIT
    mutable_surface_kind: SurfaceKind
    champion_surface_id: str
    challenger_surface_id: str
    eval_suite_id: str
    dataset_hash: str = ""
    primary_metric: str = ""
    budgets: Budgets = Field(default_factory=Budgets)
    promotion_threshold: PromotionThreshold = Field(default_factory=PromotionThreshold)
    status: ExperimentStatus = ExperimentStatus.PROPOSED
    disposition: Disposition | None = None
    scorecard: Scorecard | None = None
    snapshot_hash: str = ""  # code/config/prompt/tool/dataset hashes at snapshot time
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)


# ---------------------------------------------------------------------------
# Tournament + ELO (improveswarm split-testing) — "beat the orchestration"
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Orchestration genome (FROZEN schema; design review HD-002/HD-013) — the
# multi-agent structure a challenger evolves. config_id everywhere below IS a
# Surface.id (srf_) of an ORCHESTRATION_GENOME surface (HD-004): one identity, so
# leaderboard → champion → surface joins resolve. The genome executor (L04x) turns
# this into a run; roles[].model is the SINGLE source of truth for a role's model
# (MODEL_ASSIGNMENT/ROUTING_POLICY surfaces are H2-deferred to avoid dual sources).
# ---------------------------------------------------------------------------


class GenomeRole(BaseModel):
    name: str
    agent: str  # a registered lineage/agent (e.g. 'claude', 'cli-codex', 'mock')
    model: str | None = None
    effort: str | None = None


class GenomeStage(BaseModel):
    stage: str  # human label
    kind: str  # 'generate' | 'review' | 'judge' | 'iterate' | 'synthesize'
    role: str  # references a GenomeRole.name
    iterations: int = 1
    inputs_from: list[str] = Field(default_factory=list)  # prior stage labels


class GenomeSpec(BaseModel):
    """FROZEN orchestration genome. Fully specified (no elided '...') so L03/L04x/L05
    all validate against the same shape. Persisted as configs/genomes/<surface_id>.json."""

    id: str  # == the ORCHESTRATION_GENOME Surface.id (srf_)
    archetype: str = "pipeline"  # 'solo' | 'pipeline' | 'panel' | 'debate' | 'tournament'
    roles: list[GenomeRole]
    flow: list[GenomeStage]
    judge: dict[str, Any] = Field(
        default_factory=dict
    )  # {panel:[lineage], blind:true, dimensions:[...]}
    review: dict[str, Any] = Field(default_factory=dict)  # {adversarial:bool, categories:[...]}
    budget: dict[str, Any] = Field(default_factory=dict)  # {wall_min, tokens, cost_usd}


class Elo(BaseModel):
    subject: str  # discipline or arena subject
    config_id: str  # == Surface.id (srf_) of an ORCHESTRATION_GENOME surface
    rating: float = 1000.0
    matches: int = 0
    wins: int = 0
    losses: int = 0
    draws: int = 0
    updated_at: str = Field(default_factory=utc_now_iso)


class MatchResult(BaseModel):
    id: str = Field(default_factory=lambda: new_id("mch"))
    tournament_id: str
    config_a: str
    config_b: str
    winner: str | None = None  # config id or None for draw
    score_a: float = 0.0
    score_b: float = 0.0
    judge_notes: str = ""  # blind judge panel notes (the "notes from the judges")
    blind: bool = True


class Tournament(BaseModel):
    id: str = Field(default_factory=lambda: new_id("tnm"))
    subject: str
    discipline: str
    arena_task_hash: str  # the shared, fair task all configs ran (fairness pin)
    config_ids: list[str] = Field(default_factory=list)
    winner_config_id: str | None = None
    status: str = "running"  # running | judged | done
    created_at: str = Field(default_factory=utc_now_iso)


# ---------------------------------------------------------------------------
# Validated-traits playbook + leaderboard / log-book (the curated record)
# ---------------------------------------------------------------------------


class PlaybookEntry(BaseModel):
    id: str = Field(default_factory=lambda: new_id("pbk"))
    trait: (
        str  # a validated orchestration trait (model choice/routing/structure/judging/adversarial)
    )
    discipline: str
    evidence_experiments: list[str] = Field(default_factory=list)
    evidence_tournaments: list[str] = Field(default_factory=list)
    scope: str = ""
    confidence: str = "medium"  # low | medium | high
    elo_support: float | None = None
    status: str = "validated"  # validated | provisional | retired
    created_at: str = Field(default_factory=utc_now_iso)


class LeaderboardEntry(BaseModel):
    """A row in the curated log-book of TOP orchestrations per subject, plus judge notes.
    Written/refreshed by the 2x-daily Sonnet-high curation loop."""

    subject: str
    discipline: str
    rank: int
    config_id: str
    elo: float
    summary: str  # human-readable description of the orchestration
    judge_notes: str = ""  # distilled notes from the judges
    source_experiments: list[str] = Field(default_factory=list)
    updated_by: str = "curator"
    updated_at: str = Field(default_factory=utc_now_iso)


# ---------------------------------------------------------------------------
# Promotion utility (Section 11.6 complexity regularization)
# ---------------------------------------------------------------------------


def promotion_utility(
    quality_gain: float,
    cost_penalty: float,
    latency_penalty: float,
    complexity_penalty: float,
    risk_penalty: float,
) -> float:
    """utility = quality_gain − cost − latency − complexity − risk. Ties → simpler."""
    return quality_gain - cost_penalty - latency_penalty - complexity_penalty - risk_penalty


def elo_update(
    rating_a: float, rating_b: float, score_a: float, k: float = 24.0
) -> tuple[float, float]:
    """Standard ELO. score_a in {1 win, 0.5 draw, 0 loss}. Returns (new_a, new_b)."""
    expected_a = 1.0 / (1.0 + 10.0 ** ((rating_b - rating_a) / 400.0))
    new_a = rating_a + k * (score_a - expected_a)
    new_b = rating_b + k * ((1.0 - score_a) - (1.0 - expected_a))
    return new_a, new_b


class LabEvents:
    EXPERIMENT_UPDATED = "experiment.updated"
    TOURNAMENT_UPDATED = "tournament.updated"
    MATCH_DECIDED = "match.decided"
    SURFACE_PROMOTED = "surface.promoted"
    LEADERBOARD_UPDATED = "leaderboard.updated"
    PLAYBOOK_UPDATED = "playbook.updated"
    CURATION_RAN = "curation.ran"

    ALL: tuple[str, ...] = (
        EXPERIMENT_UPDATED,
        TOURNAMENT_UPDATED,
        MATCH_DECIDED,
        SURFACE_PROMOTED,
        LEADERBOARD_UPDATED,
        PLAYBOOK_UPDATED,
        CURATION_RAN,
    )
