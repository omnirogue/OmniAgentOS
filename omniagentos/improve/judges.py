from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol, TypedDict

from omniagentos.lab.eval.blind import BlindPair, build_blind_pairs

Tier = Literal["T0", "T1", "T2"]
Stage = Literal["cheap", "premium", "final"]
Vote = Literal["candidate", "main"]        # PAIRWISE: which side the judge prefers

SOL_JUDGE_MODEL: str = "gpt-5.6-sol"
CHEAP_PANEL: tuple[str, str, str] = ("gpt-4o-mini", "claude-3-haiku", "llama-3-8b")
PREMIUM_PANEL: tuple[str, str, str] = ("gpt-4o", "claude-3-opus", "gemini-1.5-pro")
PANEL_SIZE: int = 3
T1_MAX_LOC: int = 300


@dataclass(frozen=True, slots=True)
class TierAllowlist:
    """The REGISTERED allowlist tier routing is derived from."""
    docs_suffixes: frozenset[str]        # ".md", ".rst", ".txt"
    test_prefixes: frozenset[str]        # "tests/"
    config_paths: frozenset[str]         # exact T0-safe config files
    agent_core_prefixes: frozenset[str]  # "omniagentos/swarm/", "omniagentos/agents/", "omniagentos/dispatch/", "omniagentos/adapters/"
    gate_infra_prefixes: frozenset[str]  # "omniagentos/gates/", "omniagentos/improve/", "omniagentos/lab/"
    dependency_files: frozenset[str]     # "pyproject.toml", "uv.lock", "package.json", "package-lock.json"


DEFAULT_ALLOWLIST = TierAllowlist(
    docs_suffixes=frozenset({".md", ".rst", ".txt"}),
    test_prefixes=frozenset({"tests/"}),
    config_paths=frozenset({
        "configs/accounts.yaml",
        "configs/cascade.yaml",
        "configs/concurrency.yaml",
        "configs/configtest.yaml",
        "configs/connectors.yaml",
        "configs/coverage_policy.yaml",
        "configs/dispatch.yaml",
        "configs/formations.yaml",
        "configs/governance.yaml",
        "configs/lease.yaml",
        "configs/llm.yaml",
        "configs/longhaul.yaml",
        "configs/metacog.yaml",
        "configs/modelintel.yaml",
        "configs/mounts.yaml",
        "configs/parallelism.yaml",
        "configs/policy.yaml",
        "configs/reflection.yaml",
        "configs/reliability.yaml",
        "configs/roots.yaml",
        "configs/routing.yaml",
        "configs/self_improvement.yaml",
        "configs/steward.yaml",
        "configs/swarm.yaml",
        "configs/test-profile.yaml",
        "configs/toolplane.yaml",
    }),
    agent_core_prefixes=frozenset({
        "omniagentos/swarm/",
        "omniagentos/agents/",
        "omniagentos/dispatch/",
        "omniagentos/adapters/",
    }),
    gate_infra_prefixes=frozenset({
        "omniagentos/gates/",
        "omniagentos/improve/",
        "omniagentos/lab/",
    }),
    dependency_files=frozenset({
        "pyproject.toml",
        "uv.lock",
        "package.json",
        "package-lock.json",
    }),
)


@dataclass(frozen=True, slots=True)
class ChangeSet:
    attempt_id: str
    base_sha: str
    head_sha: str
    tree_hash: str
    diff_hash: str
    files: tuple[str, ...]
    changed_loc: int
    diff_text: str = ""
    summary: str = ""
    base_text: str = ""
    base_summary: str = ""


@dataclass(frozen=True, slots=True)
class Blocker:
    """A claimed defect. `citation` MUST be concrete: `path/to/file.py:123`
    or a repro command. `reproduced` means an independent repro was run and it
    reproduced."""
    severity: int
    citation: str
    reproduced: bool = False
    detail: str = ""


@dataclass(frozen=True, slots=True)
class Attestation:
    attested: bool
    statement: str


@dataclass(frozen=True, slots=True)
class OwnerPing:
    sent_at: datetime
    response: Literal["approve", "reject"] | None = None
    responded_at: datetime | None = None
    deadline_hours: int = 24


@dataclass(frozen=True, slots=True)
class Opinion:
    """What a judge returns. `preferred_token` is a BLIND token from the view —
    the judge never names an arm."""
    preferred_token: str
    rationale: str = ""
    blocker: Blocker | None = None
    critique: str = ""
    attestation: Attestation | None = None


class PairwiseView(TypedDict):
    attempt_id: str
    tier: Tier
    stage: Stage
    rubric: str
    sides: list[BlindPair]


class PairwiseJudgeFn(Protocol):
    def __call__(self, judge_model: str, view: PairwiseView) -> Opinion: ...


CITATION_FILE_LINE = re.compile(r"^\S+\.[A-Za-z0-9_]+:\d+$")
REPRO_COMMANDS = {"pytest", "uv", "python", "python3", "make", "ruff", "mypy", "npm", "bash", "sh"}


def is_substantiated(blocker: Blocker | None) -> bool:
    """True iff blocker is not None and blocker.citation.strip() matches file:line
    OR is a repro command. Anything else (empty, prose like "feels wrong",
    "src/foo.py" with no line) is NOT substantiated."""
    if blocker is None:
        return False
    citation = blocker.citation.strip()
    if not citation:
        return False
    if CITATION_FILE_LINE.match(citation):
        return True
    parts = citation.split()
    if parts and parts[0] in REPRO_COMMANDS:
        return True
    return False


def is_veto(blocker: Blocker | None) -> bool:
    """True iff is_substantiated(blocker) AND blocker.severity == 1 AND blocker.reproduced."""
    if blocker is None:
        return False
    return is_substantiated(blocker) and blocker.severity == 1 and blocker.reproduced


def get_module(file_path: str) -> str:
    """Extract the top-level module name for a file path.
    module = first 2 path segments for omniagentos/x/... -> omniagentos/x;
    otherwise the first segment."""
    parts = file_path.split("/")
    if len(parts) >= 3 and parts[0] == "omniagentos":
        return f"omniagentos/{parts[1]}"
    return parts[0]


def classify_tier(changeset: ChangeSet, allowlist: TierAllowlist = DEFAULT_ALLOWLIST) -> Tier:
    """Evaluate in this order (first match wins), default-deny upward."""
    if not changeset.files:
        return "T2"

    # T2 if ANY of:
    # - any file starts with an agent_core_prefixes entry
    # - any file starts with a gate_infra_prefixes entry
    # - any file is in dependency_files
    # - the changed files span more than one top-level module across NON-doc/NON-test files -> T2.
    for file in changeset.files:
        if any(file.startswith(prefix) for prefix in allowlist.agent_core_prefixes):
            return "T2"
        if any(file.startswith(prefix) for prefix in allowlist.gate_infra_prefixes):
            return "T2"
        if file in allowlist.dependency_files:
            return "T2"

    non_doc_test_files = []
    for file in changeset.files:
        is_doc = any(file.endswith(suffix) for suffix in allowlist.docs_suffixes)
        is_test = any(file.startswith(prefix) for prefix in allowlist.test_prefixes)
        if not (is_doc or is_test):
            non_doc_test_files.append(file)

    modules = {get_module(file) for file in non_doc_test_files}
    if len(modules) > 1:
        return "T2"

    # T0 if EVERY file is docs (suffix in docs_suffixes) OR under a test_prefixes prefix OR in config_paths.
    every_file_is_t0 = True
    for file in changeset.files:
        is_doc = any(file.endswith(suffix) for suffix in allowlist.docs_suffixes)
        is_test = any(file.startswith(prefix) for prefix in allowlist.test_prefixes)
        is_config = file in allowlist.config_paths
        if not (is_doc or is_test or is_config):
            every_file_is_t0 = False
            break

    if every_file_is_t0:
        return "T0"

    # T1 if all remaining code files live in exactly ONE module AND changed_loc < T1_MAX_LOC (=300).
    if len(modules) == 1 and changeset.changed_loc < T1_MAX_LOC:
        return "T1"

    # Otherwise T2
    return "T2"


@dataclass(frozen=True, slots=True)
class TierPolicy:
    tier: Tier
    cheap_required: int          # T0 2, T1 2, T2 3   (of PANEL_SIZE=3)
    premium_required: int | None # T0 None (NO premium panel), T1 2, T2 3
    requires_forced_critique: bool   # T0 False, T1 True, T2 True
    requires_attestation: bool       # T0 False, T1 True, T2 True
    requires_full_attestation: bool  # T2 only: every premium panelist must attest too
    requires_owner_ping: bool        # T2 only


def tier_policy(tier: Tier) -> TierPolicy:
    """Return the policy for a given risk tier."""
    if tier == "T0":
        return TierPolicy(
            tier="T0",
            cheap_required=2,
            premium_required=None,
            requires_forced_critique=False,
            requires_attestation=False,
            requires_full_attestation=False,
            requires_owner_ping=False,
        )
    elif tier == "T1":
        return TierPolicy(
            tier="T1",
            cheap_required=2,
            premium_required=2,
            requires_forced_critique=True,
            requires_attestation=True,
            requires_full_attestation=False,
            requires_owner_ping=False,
        )
    elif tier == "T2":
        return TierPolicy(
            tier="T2",
            cheap_required=3,
            premium_required=3,
            requires_forced_critique=True,
            requires_attestation=True,
            requires_full_attestation=True,
            requires_owner_ping=True,
        )
    raise ValueError(f"Unknown tier: {tier}")


@dataclass(frozen=True, slots=True)
class JudgeVerdict:
    judge_model: str
    stage: Stage
    vote: Vote
    blocker: Blocker | None
    substantiated: bool
    rationale: str
    critique: str
    attestation: Attestation | None


@dataclass(frozen=True, slots=True)
class CascadeVerdict:
    attempt_id: str
    tier: Tier
    approved: bool
    reason: str
    verdicts: tuple[JudgeVerdict, ...]
    veto_by: str | None          # judge_model that vetoed, else None
    judge_config_hash: str


def record_verdict(
    connection: sqlite3.Connection,
    changeset: ChangeSet,
    tier: Tier,
    verdict: JudgeVerdict,
    judge_config_hash: str,
    *,
    now: datetime | None = None,
) -> str:
    """INSERTs one improve_verdicts row and returns the verdict_id."""
    verdict_id = uuid.uuid4().hex
    blocker_cited = 1 if verdict.substantiated else 0
    blocker_reproduced = 1 if (verdict.blocker is not None and verdict.blocker.reproduced) else 0

    if now is None:
        now_dt = datetime.now(UTC)
    else:
        now_dt = now
    created_at_str = now_dt.isoformat()

    connection.execute(
        """
        INSERT INTO improve_verdicts (
            verdict_id, attempt_id, tier, judge_model, stage, vote,
            blocker_cited, blocker_reproduced, rationale,
            base_sha, head_sha, tree_hash, diff_hash,
            judge_config_hash, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            verdict_id,
            changeset.attempt_id,
            tier,
            verdict.judge_model,
            verdict.stage,
            verdict.vote,
            blocker_cited,
            blocker_reproduced,
            verdict.rationale,
            changeset.base_sha,
            changeset.head_sha,
            changeset.tree_hash,
            changeset.diff_hash,
            judge_config_hash,
            created_at_str,
        )
    )
    return verdict_id


def _blind_sides(
    changeset: ChangeSet, seed: int | None
) -> tuple[list[BlindPair], dict[str, tuple[str, str]]]:
    pairs, token_map, _ = build_blind_pairs(
        {changeset.attempt_id: {
            "candidate": {"text": changeset.diff_text, "summary": changeset.summary},
            "main": {"text": changeset.base_text, "summary": changeset.base_summary}
        }},
        seed=seed,
    )
    return pairs, token_map


def run_cascade(
    changeset: ChangeSet,
    *,
    judge_fn: PairwiseJudgeFn,
    allowlist: TierAllowlist = DEFAULT_ALLOWLIST,
    owner_ping: OwnerPing | None = None,
    connection: sqlite3.Connection | None = None,
    now: datetime | None = None,
    seed: int | None = None,
    skip_stages: frozenset[Stage] = frozenset(),   # TEST HOOK: omit a stage to prove it is required
) -> CascadeVerdict:
    """Run the cascade of risk-tiered judges."""
    tier = classify_tier(changeset, allowlist)
    policy = tier_policy(tier)

    # Compute config hash
    config_dict = {
        "tier": tier,
        "cheap_required": policy.cheap_required,
        "premium_required": policy.premium_required,
        "panels": {
            "cheap": CHEAP_PANEL,
            "premium": PREMIUM_PANEL,
        },
        "SOL_JUDGE_MODEL": SOL_JUDGE_MODEL,
        "T1_MAX_LOC": T1_MAX_LOC,
    }
    config_json = json.dumps(config_dict, sort_keys=True)
    judge_config_hash = hashlib.sha256(config_json.encode("utf-8")).hexdigest()

    if now is None:
        now = datetime.now(UTC)

    verdicts_list: list[JudgeVerdict] = []

    # 1. Cheap panel stage
    if "cheap" not in skip_stages:
        cheap_substantiated_against = 0
        for judge_model in CHEAP_PANEL:
            pairs, token_map = _blind_sides(changeset, seed)
            view = PairwiseView(
                attempt_id=changeset.attempt_id,
                tier=tier,
                stage="cheap",
                rubric="Compare candidate changes to main",
                sides=pairs,
            )
            opinion = judge_fn(judge_model, view)

            if opinion.preferred_token not in token_map:
                raise LookupError(f"Judge {judge_model} returned unknown token {opinion.preferred_token}")
            case_id, arm = token_map[opinion.preferred_token]
            cheap_vote: Vote = "candidate" if arm == "candidate" else "main"

            substantiated = is_substantiated(opinion.blocker)

            verdict = JudgeVerdict(
                judge_model=judge_model,
                stage="cheap",
                vote=cheap_vote,
                blocker=opinion.blocker,
                substantiated=substantiated,
                rationale=opinion.rationale,
                critique=opinion.critique,
                attestation=opinion.attestation,
            )
            verdicts_list.append(verdict)

            if connection is not None:
                record_verdict(connection, changeset, tier, verdict, judge_config_hash, now=now)

            if is_veto(opinion.blocker):
                return CascadeVerdict(
                    attempt_id=changeset.attempt_id,
                    tier=tier,
                    approved=False,
                    reason="vetoed_severity1_blocker",
                    verdicts=tuple(verdicts_list),
                    veto_by=judge_model,
                    judge_config_hash=judge_config_hash,
                )

            if cheap_vote == "main" and substantiated:
                cheap_substantiated_against += 1

        in_favour = PANEL_SIZE - cheap_substantiated_against
        if in_favour < policy.cheap_required:
            return CascadeVerdict(
                attempt_id=changeset.attempt_id,
                tier=tier,
                approved=False,
                reason="cheap_panel_failed",
                verdicts=tuple(verdicts_list),
                veto_by=None,
                judge_config_hash=judge_config_hash,
            )

    # 2. Premium panel stage
    if policy.premium_required is not None and "premium" not in skip_stages:
        premium_substantiated_against = 0
        for judge_model in PREMIUM_PANEL:
            pairs, token_map = _blind_sides(changeset, seed)
            view = PairwiseView(
                attempt_id=changeset.attempt_id,
                tier=tier,
                stage="premium",
                rubric="Compare candidate changes to main",
                sides=pairs,
            )
            opinion = judge_fn(judge_model, view)

            if opinion.preferred_token not in token_map:
                raise LookupError(f"Judge {judge_model} returned unknown token {opinion.preferred_token}")
            case_id, arm = token_map[opinion.preferred_token]
            premium_vote: Vote = "candidate" if arm == "candidate" else "main"

            substantiated = is_substantiated(opinion.blocker)

            verdict = JudgeVerdict(
                judge_model=judge_model,
                stage="premium",
                vote=premium_vote,
                blocker=opinion.blocker,
                substantiated=substantiated,
                rationale=opinion.rationale,
                critique=opinion.critique,
                attestation=opinion.attestation,
            )
            verdicts_list.append(verdict)

            if connection is not None:
                record_verdict(connection, changeset, tier, verdict, judge_config_hash, now=now)

            if is_veto(opinion.blocker):
                return CascadeVerdict(
                    attempt_id=changeset.attempt_id,
                    tier=tier,
                    approved=False,
                    reason="vetoed_severity1_blocker",
                    verdicts=tuple(verdicts_list),
                    veto_by=judge_model,
                    judge_config_hash=judge_config_hash,
                )

            if premium_vote == "main" and substantiated:
                premium_substantiated_against += 1

        in_favour = PANEL_SIZE - premium_substantiated_against
        if in_favour < policy.premium_required:
            return CascadeVerdict(
                attempt_id=changeset.attempt_id,
                tier=tier,
                approved=False,
                reason="premium_panel_failed",
                verdicts=tuple(verdicts_list),
                veto_by=None,
                judge_config_hash=judge_config_hash,
                )

    # 3. Final review stage (always)
    if "final" not in skip_stages:
        pairs, token_map = _blind_sides(changeset, seed)
        view = PairwiseView(
            attempt_id=changeset.attempt_id,
            tier=tier,
            stage="final",
            rubric="Compare candidate changes to main",
            sides=pairs,
        )
        opinion = judge_fn(SOL_JUDGE_MODEL, view)

        if opinion.preferred_token not in token_map:
            raise LookupError(f"Judge {SOL_JUDGE_MODEL} returned unknown token {opinion.preferred_token}")
        case_id, arm = token_map[opinion.preferred_token]
        final_vote: Vote = "candidate" if arm == "candidate" else "main"

        substantiated = is_substantiated(opinion.blocker)

        verdict = JudgeVerdict(
            judge_model=SOL_JUDGE_MODEL,
            stage="final",
            vote=final_vote,
            blocker=opinion.blocker,
            substantiated=substantiated,
            rationale=opinion.rationale,
            critique=opinion.critique,
            attestation=opinion.attestation,
        )
        verdicts_list.append(verdict)

        if connection is not None:
            record_verdict(connection, changeset, tier, verdict, judge_config_hash, now=now)

        if is_veto(opinion.blocker):
            return CascadeVerdict(
                attempt_id=changeset.attempt_id,
                tier=tier,
                approved=False,
                reason="vetoed_severity1_blocker",
                verdicts=tuple(verdicts_list),
                veto_by=SOL_JUDGE_MODEL,
                judge_config_hash=judge_config_hash,
            )

        if final_vote == "main":
            return CascadeVerdict(
                attempt_id=changeset.attempt_id,
                tier=tier,
                approved=False,
                reason="final_review_failed",
                verdicts=tuple(verdicts_list),
                veto_by=None,
                judge_config_hash=judge_config_hash,
            )

    # Post-stage validations
    if not any(v.stage == "cheap" for v in verdicts_list):
        return CascadeVerdict(
            attempt_id=changeset.attempt_id,
            tier=tier,
            approved=False,
            reason="missing_cheap_screen",
            verdicts=tuple(verdicts_list),
            veto_by=None,
            judge_config_hash=judge_config_hash,
        )

    if policy.premium_required is not None and not any(v.stage == "premium" for v in verdicts_list):
        return CascadeVerdict(
            attempt_id=changeset.attempt_id,
            tier=tier,
            approved=False,
            reason="missing_premium_review",
            verdicts=tuple(verdicts_list),
            veto_by=None,
            judge_config_hash=judge_config_hash,
        )

    if not any(v.stage == "final" for v in verdicts_list):
        return CascadeVerdict(
            attempt_id=changeset.attempt_id,
            tier=tier,
            approved=False,
            reason="missing_premium_review",
            verdicts=tuple(verdicts_list),
            veto_by=None,
            judge_config_hash=judge_config_hash,
        )

    # Forced critique validations
    final_verdicts = [v for v in verdicts_list if v.stage == "final"]
    sol_verdict = final_verdicts[0]
    if policy.requires_forced_critique:
        if not sol_verdict.critique or not sol_verdict.critique.strip():
            return CascadeVerdict(
                attempt_id=changeset.attempt_id,
                tier=tier,
                approved=False,
                reason="missing_forced_critique",
                verdicts=tuple(verdicts_list),
                veto_by=None,
                judge_config_hash=judge_config_hash,
            )

    premium_verdicts = [v for v in verdicts_list if v.stage == "premium"]
    for v in premium_verdicts:
        if v.vote == "main":
            if not v.critique or not v.critique.strip():
                return CascadeVerdict(
                    attempt_id=changeset.attempt_id,
                    tier=tier,
                    approved=False,
                    reason="missing_forced_critique",
                    verdicts=tuple(verdicts_list),
                    veto_by=None,
                    judge_config_hash=judge_config_hash,
                )

    # Attestation validations
    if policy.requires_attestation:
        sol_att = sol_verdict.attestation
        if sol_att is None or not sol_att.attested or not sol_att.statement or not sol_att.statement.strip():
            return CascadeVerdict(
                attempt_id=changeset.attempt_id,
                tier=tier,
                approved=False,
                reason="missing_attestation",
                verdicts=tuple(verdicts_list),
                veto_by=None,
                judge_config_hash=judge_config_hash,
            )

    if policy.requires_full_attestation:
        for v in premium_verdicts:
            att = v.attestation
            if att is None or not att.attested or not att.statement or not att.statement.strip():
                return CascadeVerdict(
                    attempt_id=changeset.attempt_id,
                    tier=tier,
                    approved=False,
                    reason="missing_attestation",
                    verdicts=tuple(verdicts_list),
                    veto_by=None,
                    judge_config_hash=judge_config_hash,
                )

    # Owner ping check
    if policy.requires_owner_ping:
        if owner_ping is None:
            return CascadeVerdict(
                attempt_id=changeset.attempt_id,
                tier=tier,
                approved=False,
                reason="owner_response_required",
                verdicts=tuple(verdicts_list),
                veto_by=None,
                judge_config_hash=judge_config_hash,
            )
        if owner_ping.response == "reject":
            return CascadeVerdict(
                attempt_id=changeset.attempt_id,
                tier=tier,
                approved=False,
                reason="owner_rejected",
                verdicts=tuple(verdicts_list),
                veto_by=None,
                judge_config_hash=judge_config_hash,
            )
        if owner_ping.response is None:
            elapsed = now - owner_ping.sent_at
            if elapsed >= timedelta(hours=owner_ping.deadline_hours):
                return CascadeVerdict(
                    attempt_id=changeset.attempt_id,
                    tier=tier,
                    approved=False,
                    reason="owner_no_response",
                    verdicts=tuple(verdicts_list),
                    veto_by=None,
                    judge_config_hash=judge_config_hash,
                )
            else:
                return CascadeVerdict(
                    attempt_id=changeset.attempt_id,
                    tier=tier,
                    approved=False,
                    reason="awaiting_owner",
                    verdicts=tuple(verdicts_list),
                    veto_by=None,
                    judge_config_hash=judge_config_hash,
                )
        # If "approve", we continue

    return CascadeVerdict(
        attempt_id=changeset.attempt_id,
        tier=tier,
        approved=True,
        reason="approved",
        verdicts=tuple(verdicts_list),
        veto_by=None,
        judge_config_hash=judge_config_hash,
    )
