"""curator — scan the run ledger for recently-COMPLETED runs and capture a
reusable skill for each one that passed and has not yet been captured.

`omniagentos.selfimprove` (Wave 4) shipped `capture_skill()` /
`capture_skill_from_run_dir()` as a library only — nothing in the live
system actually called it (see the package docstring in `__init__.py`: "NOT
yet wired into the live fusion-gate"). This module is that missing consumer:
a standalone, idempotent, read-the-ledger-then-write-the-vault process, run
via ``python -m omniagentos.selfimprove.curator`` (by hand, cron, or the
render-not-load launchd template in `scripts/selfimprove/`).

Deliberately NOT a hook inside `omniagentos.runner.core.Runner._finalize_body`:
that state machine's own terminal state (`RunState.COMPLETED`, as opposed to
`RunState.FAILED`/`RunState.CANCELLED`) is the only "did this run succeed"
signal it carries today — there is no first-class notion of a verification
*gate* distinct from that terminal state in the runner, so this module has to
make that same "COMPLETED means passed" judgment call regardless of whether
it lives inside the runner or beside it. Making that call, and synthesizing
per-run `SkillMetadata`, on the runner's fenced hot path — exercised by every
terminal transition in `tests/runner/test_state_machine.py` — is real, new
surface this package should not force onto a state machine it does not own.
Reading the ledger's already-durable, append-only `RunManifest` records after
the fact gets the same "capture on completion" outcome with
`omniagentos/runner/core.py` left completely untouched.

HARD RULE (unchanged): every manifest is routed through a real
`VerificationGate` and `capture_skill`'s own ``gate.status is
GateStatus.PASSED`` check (see `skills.py`) — this module does not
special-case around that check, it only decides what evidence goes into the
gate (`_gate_from_manifest`, the ledger-manifest analogue of
`gate.gate_from_status`). A FAILED/CANCELLED (or otherwise non-COMPLETED)
manifest is refused by that same check and captures nothing.

Idempotent: a run whose computed skill note already exists on disk is
skipped without writing again (and without re-invoking `capture_skill`), so
running this repeatedly — by hand, cron, or the launchd template — never
double-captures the same run_id.

Safe: a per-manifest error (a malformed metadata field, a write failure, an
unexpected exception anywhere in `capture_skill`) is caught, recorded, and
never aborts the rest of the scan.
"""

from __future__ import annotations

import argparse
import difflib
import importlib
import json
import logging
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, cast

from omniagentos.contracts import (
    HarnessType,
    RunManifest,
    RunState,
    default_ledger_dir,
    default_vault_dir,
)
from omniagentos.ledger import read_manifests
from omniagentos.selfimprove.errors import UnverifiedCaptureError
from omniagentos.selfimprove.models import (
    CapabilityLearning,
    GateStatus,
    SkillMetadata,
    VerificationGate,
)
from omniagentos.selfimprove.paths import skill_relpath
from omniagentos.selfimprove.skills import capture_skill
from omniagentos.sessions.manifest import SessionManifest

LOG = logging.getLogger(__name__)

_DEFAULT_SCAN_LIMIT = 200
_MATCH_THRESHOLD = 0.70


class SkillsAPI(Protocol):
    """Subset of the skills service used by the daily learning loop."""

    def list_tree(self) -> list[dict[str, Any]]: ...

    def get_skill(self, skill_id: str) -> dict[str, Any]: ...

    def propose_update(
        self,
        skill_id: str,
        *,
        proposed_content: str | None = None,
        proposed_diff: str | None = None,
        evidence_files: list[str] | None = None,
        linked_execution: str | None = None,
        risk: str = "low",
        created_by: str,
    ) -> str: ...

    def decide_proposal(
        self, proposal_id: str, *, approve: bool, note: str | None = None, decided_by: str
    ) -> dict[str, Any]: ...

    def upsert_skill(self, data: dict[str, Any]) -> str: ...


@dataclass(slots=True)
class CurateResult:
    """Summary of one `curate()` pass. Every run_id the scan looked at ends
    up in `captured`, `proposals`, `already_captured`, `unverified`,
    `not_real_work`, or `errors` — useful both as a return value and as the
    JSON the CLI prints."""

    scanned: int = 0
    captured: list[str] = field(default_factory=list)
    already_captured: list[str] = field(default_factory=list)
    unverified: list[str] = field(default_factory=list)
    # Runs refused before any gate because the run itself is not evidence of a
    # reusable pattern (today: the mock harness). Distinct from `unverified`,
    # which means a real run that did not pass its gate.
    not_real_work: list[str] = field(default_factory=list)
    proposals: dict[str, str] = field(default_factory=dict)
    pending_proposals: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "scanned": self.scanned,
            "captured": self.captured,
            "already_captured": self.already_captured,
            "unverified": self.unverified,
            "not_real_work": self.not_real_work,
            "proposals": self.proposals,
            "pending_proposals": self.pending_proposals,
            "errors": self.errors,
        }


def _skill_id_for(run_id: str) -> str:
    """The one place the run_id -> skill_id mapping is defined, so the
    idempotency check (`curate`) and the captured metadata
    (`_skill_metadata_from_manifest`) can never drift apart."""
    return f"run-{run_id}"


def _is_capturable_harness(manifest: RunManifest) -> bool:
    """False when the run cannot be evidence of a reusable pattern.

    A ``mock`` harness run never dispatched work to an agent: nothing was
    attempted, so a COMPLETED state records only that the state machine
    advanced. Capturing it as a "skill" is how 25 of the live corpus's 41
    entries came to be ``skill: Workflow for task tsk_* … harness=mock`` with
    "No step-level detail was recorded" as their entire Steps section — and
    because they land under category ``General`` they outrank the real skills
    on every domain query. This is the capture-side half of the U-16 fix; the
    quarantine of the already-captured rows is the storage-side half.
    """
    return manifest.harness.harness is not HarnessType.MOCK


def _gate_from_manifest(manifest: RunManifest) -> VerificationGate:
    """Ledger-manifest analogue of `gate.gate_from_status`: builds a
    `VerificationGate` straight off a `RunManifest` — the runner's own
    durable terminal-state record — rather than a Fusion worker's
    `status.json`.

    `RunState.COMPLETED` is this runner's one first-class "succeeded" signal;
    `RunState.FAILED` and `RunState.CANCELLED` (the only other members of
    `contracts.TERMINAL_RUN_STATES`) both map to FAILED here, never PASSED —
    mirroring `gate.gate_from_status`'s treatment of a Fusion worker's
    "partial" as FAILED, not a soft pass.
    """
    status = GateStatus.PASSED if manifest.state is RunState.COMPLETED else GateStatus.FAILED
    evidence_bits = [
        f"runner_state={manifest.state.value}",
        f"harness={manifest.harness.harness.value}",
        "measured=false",
    ]
    if manifest.receipts:
        evidence_bits.append(f"{len(manifest.receipts)} receipt(s)")
    if manifest.artifacts:
        evidence_bits.append(f"{len(manifest.artifacts)} artifact(s)")

    kwargs: dict[str, object] = {
        "status": status,
        "source_run_id": manifest.run_id,
        "evidence": "; ".join(evidence_bits),
    }
    if manifest.finished_at:
        kwargs["checked_at"] = manifest.finished_at
    return VerificationGate(**kwargs)  # type: ignore[arg-type]


def _skill_metadata_from_manifest(manifest: RunManifest) -> SkillMetadata:
    """Synthesize a `SkillMetadata` from the fields a `RunManifest` actually
    carries (no step list, no prompt text — those live in the run's vault
    note, referenced here via `manifest.vault_note` when present)."""
    discipline = manifest.discipline or "general"
    harness_name = manifest.harness.harness.value

    steps: list[str] = []
    seen: set[str] = set()
    for receipt in manifest.receipts:
        if receipt.step_name and receipt.step_name not in seen:
            steps.append(receipt.step_name)
            seen.add(receipt.step_name)
    if not steps:
        steps = [
            "No step-level detail was recorded in the ledger manifest for this run; "
            "see the run's vault note (if any) for the full step-by-step record."
        ]

    output_bits: list[str] = []
    if manifest.artifacts:
        output_bits.append(
            f"{len(manifest.artifacts)} artifact(s): " + ", ".join(manifest.artifacts[:5])
        )
    if manifest.output_digest:
        output_bits.append(f"output digest {manifest.output_digest}")
    if manifest.vault_note:
        output_bits.append(f"full run note: {manifest.vault_note}")
    output_structure = (
        "; ".join(output_bits) or "No artifacts or output digest were recorded for this run."
    )

    agent_bits = harness_name
    if manifest.agent:
        agent_bits += f"/{manifest.agent}"
    if manifest.model:
        agent_bits += f"/{manifest.model}"

    raw_capabilities = manifest.extra.get("capabilities", [])
    capabilities = (
        [CapabilityLearning.model_validate(item) for item in raw_capabilities]
        if isinstance(raw_capabilities, list)
        else []
    )
    company_id = str(manifest.extra.get("company_id") or "").strip() or None
    if capabilities:
        from omniagentos.knowledge.capabilities import normalize_company_id

        company_id = normalize_company_id(company_id)
    brand = (
        str(manifest.extra.get("brand") or manifest.extra.get("company_slug") or "").strip() or None
    )

    return SkillMetadata(
        skill_id=_skill_id_for(manifest.run_id),
        title=f"Workflow for task {manifest.task_id} ({discipline})",
        discipline=discipline,
        summary=f"Reusable pattern captured from a COMPLETED run of task {manifest.task_id} ({agent_bits}).",
        input_format=(
            f"Agent input dispatched to the {harness_name} harness"
            + (f" (agent={manifest.agent})" if manifest.agent else "")
            + (f" (model={manifest.model})" if manifest.model else "")
            + "; see the run's task input for the exact prompt/spec."
        ),
        steps=steps,
        output_structure=output_structure,
        validation_rules=[
            "Run reached RunState.COMPLETED in the durable runner state machine "
            "(omniagentos.runner.core), recorded in the append-only run ledger."
        ],
        tags=[tag for tag in (discipline, harness_name) if tag],
        model=manifest.model,
        capabilities=capabilities,
        company_id=company_id,
        brand=brand,
    )


def _slug(value: str) -> str:
    """Return a conservative comparable slug for title/slug matching."""
    return "-".join(part for part in re.split(r"[^a-z0-9]+", value.lower()) if part)


def _normalised_tokens(value: str) -> set[str]:
    return set(_slug(value).split("-")) - {""}


def _match_learning_to_skill(
    metadata: SkillMetadata, existing_skills: list[dict[str, Any]]
) -> str | None:
    """Return the single best skill match when confidence is above 70 percent.

    Matching intentionally favours precision: a discipline must align for all
    non-slug matches, and a tie is resolved by stable skill id ordering.  That
    makes repeated runs choose the same target rather than double-matching a
    learning to whichever tree ordering happens to be returned.
    """
    metadata_discipline = (metadata.discipline or "").casefold()
    metadata_title = metadata.title.casefold()
    metadata_tokens = _normalised_tokens(metadata.title)
    metadata_tags = {tag.casefold() for tag in metadata.tags}
    candidates: list[tuple[float, str]] = []

    for skill in existing_skills:
        skill_id = skill.get("id") or skill.get("skill_id")
        if not isinstance(skill_id, str) or not skill_id:
            continue
        discipline = str(skill.get("discipline") or "").casefold()
        same_discipline = bool(metadata_discipline and metadata_discipline == discipline)
        slug = skill.get("slug")
        if (
            same_discipline
            and isinstance(slug, str)
            and slug
            in {
                metadata.skill_id,
                _slug(metadata.title),
            }
        ):
            candidates.append((1.0, skill_id))
            continue
        if not same_discipline:
            continue

        title = str(skill.get("title") or "").casefold()
        title_tokens = _normalised_tokens(title)
        token_overlap = (
            len(metadata_tokens & title_tokens) / len(metadata_tokens | title_tokens)
            if metadata_tokens or title_tokens
            else 0.0
        )
        sequence_ratio = difflib.SequenceMatcher(None, metadata_title, title).ratio()
        title_confidence = max(token_overlap, sequence_ratio)
        skill_tags = {str(tag).casefold() for tag in skill.get("tags", [])}
        tag_overlap = (
            len(metadata_tags & skill_tags) / len(metadata_tags | skill_tags)
            if metadata_tags or skill_tags
            else 0.0
        )
        # Tags only reinforce a reasonably similar title; shared discipline
        # alone is far too broad to turn routine chatter into an update.
        confidence = max(title_confidence, (title_confidence + tag_overlap) / 2)
        if confidence > _MATCH_THRESHOLD:
            candidates.append((confidence, skill_id))

    if not candidates:
        return None
    return sorted(candidates, key=lambda candidate: (-candidate[0], candidate[1]))[0][1]


def _load_skills_api() -> SkillsAPI | None:
    """Load S-A's service only when installed; this package ships independently."""
    try:
        return cast(SkillsAPI, importlib.import_module("omniagentos.skills"))
    except ModuleNotFoundError as exc:
        if exc.name == "omniagentos.skills":
            return None
        raise


def _learning_content(metadata: SkillMetadata) -> str:
    """Stable text used both for a proposal diff and a future skill version."""
    return "\n".join(
        [
            f"# {metadata.title}",
            f"Discipline: {metadata.discipline or 'general'}",
            f"Preferred method: {metadata.model or 'unspecified'}",
            f"Summary: {metadata.summary}",
            f"Input format: {metadata.input_format}",
            "Steps:",
            *[f"- {step}" for step in metadata.steps],
            f"Output structure: {metadata.output_structure}",
        ]
    )


def _current_skill_content(skill: dict[str, Any]) -> str:
    for key in ("current_content", "content", "skill_md", "body"):
        value = skill.get(key)
        if isinstance(value, str):
            return value
    return ""


def _current_structured_field(skill: dict[str, Any], field_name: str) -> str | None:
    """Read an interface field from structured service data or rendered content."""
    key = field_name.casefold().replace(" ", "_")
    direct_value = skill.get(key)
    if isinstance(direct_value, str):
        return direct_value
    match = re.search(
        rf"^{re.escape(field_name)}:\\s*(.+)$",
        _current_skill_content(skill),
        flags=re.IGNORECASE | re.MULTILINE,
    )
    return match.group(1) if match else None


def _proposed_diff(current_content: str, learning_content: str) -> str:
    return "".join(
        difflib.unified_diff(
            current_content.splitlines(keepends=True),
            learning_content.splitlines(keepends=True),
            fromfile="current",
            tofile="proposed",
        )
    )


def _risk_for_update(metadata: SkillMetadata, skill: dict[str, Any], proposed_diff: str) -> str:
    """Route only model and interface changes to review; wording is safe.

    Unknown / unreadable ``preferred_method`` is never treated as "same model".
    When the learning carries a model and the skill's current preferred method
    cannot be measured (missing, None, blank, or non-string), risk is ``model``
    so the proposal stays pending rather than auto-approving. Scoring that
    non-result as ``low`` was a favourable default over an empty baseline —
    the same defect class as recording unknown cost as 0.0.
    """
    preferred_method = skill.get("preferred_method")
    if metadata.model:
        if not isinstance(preferred_method, str) or not preferred_method.strip():
            return "model"
        if metadata.model != preferred_method:
            return "model"
    current_input = _current_structured_field(skill, "Input format")
    current_output = _current_structured_field(skill, "Output structure")
    if (current_input is not None and current_input != metadata.input_format) or (
        current_output is not None and current_output != metadata.output_structure
    ):
        return "major"
    if len(proposed_diff.strip()) < 50:
        return "low"
    return "low"


def _upsert_payload(metadata: SkillMetadata) -> dict[str, Any]:
    return {
        "id": metadata.skill_id,
        "slug": _slug(metadata.title),
        "title": metadata.title,
        "discipline": metadata.discipline,
        "tags": metadata.tags,
        "preferred_method": metadata.model,
        "content": _learning_content(metadata),
    }


def curate(
    *,
    ledger_dir: str | None = None,
    vault_dir: str | None = None,
    skills_dir: str | None = None,
    limit: int = _DEFAULT_SCAN_LIMIT,
    autocommit: bool | None = None,
    skills_api: SkillsAPI | None = None,
    capability_store: Any | None = None,
) -> CurateResult:
    """Scan up to `limit` recent manifests and mine each verified, unseen run.

    A new learning is captured and registered as a skill; a sufficiently
    similar existing skill receives an update proposal instead.  Both paths
    first write the same passed-gate vault note, which supplies proposal
    evidence and is the durable idempotency marker.

    Every manifest that COULD be evidence — see `_is_capturable_harness`; a
    mock-harness run never is, and lands in `CurateResult.not_real_work`
    without a note, a row, or a proposal — is routed through a real
    `VerificationGate` and `capture_skill`'s HARD RULE; a non-PASSED gate
    raises `UnverifiedCaptureError`, which this function catches and records
    in `CurateResult.unverified` rather than writing anything (never trust a
    shortcut boolean here — see `_gate_from_manifest`'s docstring).
    """
    resolved_ledger_dir = ledger_dir or default_ledger_dir()
    resolved_vault_dir = vault_dir or default_vault_dir()
    result = CurateResult()
    api = skills_api if skills_api is not None else _load_skills_api()
    existing_skills = api.list_tree() if api is not None else []

    manifests = read_manifests(resolved_ledger_dir, limit=limit)
    result.scanned = len(manifests)

    for manifest in manifests:
        try:
            if not _is_capturable_harness(manifest):
                # Refused BEFORE the vault note is written: a mock run must not
                # leave a skill note, a DB row, or an update proposal behind.
                result.not_real_work.append(manifest.run_id)
                continue

            relpath = skill_relpath(_skill_id_for(manifest.run_id))
            if (Path(resolved_vault_dir) / relpath).is_file():
                result.already_captured.append(manifest.run_id)
                continue

            gate = _gate_from_manifest(manifest)
            metadata = _skill_metadata_from_manifest(manifest)
            # Capture a durable, passed-gate evidence note for both new skills
            # and updates.  The result category stays "proposal" for a match.
            active_capability_store = capability_store
            owns_capability_store = False
            if metadata.capabilities and active_capability_store is None:
                from omniagentos.knowledge.config import admin_dsn
                from omniagentos.knowledge.embeddings import OllamaEmbedding
                from omniagentos.knowledge.store import KnowledgeStore

                configured_dsn = admin_dsn()
                if configured_dsn:
                    active_capability_store = KnowledgeStore(
                        dsn=configured_dsn, embedder=OllamaEmbedding()
                    )
                    owns_capability_store = True
            try:
                try:
                    capture_result = capture_skill(
                        metadata,
                        gate,
                        resolved_vault_dir,
                        skills_dir=skills_dir,
                        autocommit=autocommit,
                        capability_store=active_capability_store,
                    )
                finally:
                    if owns_capability_store and active_capability_store is not None:
                        active_capability_store.close()
            except UnverifiedCaptureError:
                result.unverified.append(manifest.run_id)
                continue
            except Exception as exc:  # noqa: BLE001 -- isolate every manifest capture
                LOG.exception(
                    "selfimprove curator: failed to capture skill for run %s", manifest.run_id
                )
                result.errors[manifest.run_id] = f"{type(exc).__name__}: {exc}"
                continue
            matched_skill_id = _match_learning_to_skill(metadata, existing_skills)
            if matched_skill_id is None:
                if api is not None:
                    api.upsert_skill(_upsert_payload(metadata))
                    existing_skills.append(_upsert_payload(metadata))
                result.captured.append(manifest.run_id)
                continue

            if api is None:
                raise RuntimeError("matched existing skill but omniagentos.skills is unavailable")
            current_skill = api.get_skill(matched_skill_id)
            diff = _proposed_diff(
                _current_skill_content(current_skill), _learning_content(metadata)
            )
            risk = _risk_for_update(metadata, current_skill, diff)
            proposal_id = api.propose_update(
                matched_skill_id,
                proposed_diff=diff,
                evidence_files=[capture_result.note_path],
                linked_execution=manifest.run_id,
                risk=risk,
                created_by="curator",
            )
            result.proposals[manifest.run_id] = proposal_id
            if risk == "low":
                api.decide_proposal(proposal_id, approve=True, decided_by="curator")
            else:
                result.pending_proposals.append(proposal_id)
        except UnverifiedCaptureError:
            result.unverified.append(manifest.run_id)
        except Exception as exc:  # noqa: BLE001 -- one bad manifest must never abort the scan
            LOG.exception(
                "selfimprove curator: failed to capture skill for run %s", manifest.run_id
            )
            result.errors[manifest.run_id] = f"{type(exc).__name__}: {exc}"

    return result


# ---------------------------------------------------------------------------
# Session transcript mining (Wire 2 of the session loop): the same
# "scan a ledger for recently-COMPLETED, verified, unseen work and capture a
# reusable skill" pattern as `curate()` above, applied to the Session Bridge's
# OWN terminal artifact -- `<ledger_dir>/sessions/<session_id>.jsonl`
# (`omniagentos.sessions.manifest.SessionManifest`) -- instead of the runner's
# RunManifest ledger. Routes through the EXACT SAME skills path
# (`capture_skill`'s HARD RULE, `_match_learning_to_skill`,
# `propose_update`/`upsert_skill` below) rather than a parallel pipeline.
# ---------------------------------------------------------------------------

# Markers a transcript's free text is scanned for (case-insensitive substring
# match). Deliberately conservative and explicit -- a live session transcript
# is far noisier than a completed run's manifest, so (unlike `curate()`, which
# treats every COMPLETED run as reusable) a session is only ever captured when
# one of these appears: "only reusable, ignore routine chatter", enforced here
# by finding nothing to capture rather than by a confidence score.
_FIX_MARKERS: tuple[str, ...] = (
    "the fix was",
    "fixed by",
    "root cause",
    "the issue was",
    "resolved by",
    "the bug was",
    "turned out to be",
)
_CORRECTION_MARKERS: tuple[str, ...] = (
    "instead of",
    "don't use",
    "do not use",
    "never use",
    "always use",
    "make sure to",
    "remember to",
    "should have",
    "the correct way",
    "the right way",
    "prefer using",
    "avoid using",
    "next time,",
)


@dataclass(slots=True)
class SessionDiscovery:
    """One reusable nugget mined from a session transcript's free text --
    either an explicit failure -> fix, or an explicit correction / preferred
    method. `kind` drives the synthesized skill's title/tags; `snippet` is the
    matched text itself, kept as evidence."""

    kind: str  # "fix" | "correction"
    snippet: str


def _skill_id_for_session(session_id: str) -> str:
    """The one place the session_id -> skill_id mapping is defined (mirrors
    `_skill_id_for` for runs). The `session-` prefix can never collide with
    `_skill_id_for`'s `run-` prefix, so the two miners never fight over the
    same skill id / idempotency marker."""
    return f"session-{session_id}"


def _session_manifest_dir(ledger_dir: str) -> Path:
    """`<ledger_dir>/sessions/` via `SessionManifest` itself (not a hand-rolled
    path) so this reader can never drift from where the Session Bridge
    actually writes a session's terminal artifact."""
    return SessionManifest(ledger_dir).directory


def _read_jsonl_events(path: Path) -> list[dict[str, Any]]:
    """Read every JSON-object line of a session transcript file.

    Tolerant like `omniagentos.ledger.read_manifests`: a corrupt line is
    logged and skipped rather than failing the whole scan. Works for BOTH
    today's SessionManifest schema (exactly one summary line) and a richer,
    multi-line per-turn transcript, so this reader never has to change if a
    future wave starts persisting fuller session transcripts under the same
    `<session_id>.jsonl` path.
    """
    events: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    LOG.warning("skipping corrupt session transcript line in %s", path)
                    continue
                if isinstance(event, dict):
                    events.append(event)
    except OSError as error:
        LOG.warning("unable to read session transcript %s: %s", path, error)
    return events


def _iter_session_transcripts(
    ledger_dir: str, limit: int
) -> list[tuple[str, list[dict[str, Any]]]]:
    """Enumerate up to `limit` session transcripts, newest first.

    Each `<session_id>.jsonl` under the sessions manifest dir is one
    completed/idle session (`SessionManifest.write` only ever writes once a
    session reaches a TERMINAL state -- see its docstring) -- there is no
    separate "is this session done" check to make here, existence of the file
    already means it is.
    """
    if limit <= 0:
        return []
    directory = _session_manifest_dir(ledger_dir)
    if not directory.is_dir():
        return []
    try:
        paths = sorted(
            (p for p in directory.glob("*.jsonl") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError as error:
        LOG.warning("unable to list session transcripts in %s: %s", directory, error)
        return []
    results: list[tuple[str, list[dict[str, Any]]]] = []
    for path in paths[:limit]:
        events = _read_jsonl_events(path)
        if events:
            results.append((path.stem, events))
    return results


def _session_summary(events: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """The session's own descriptive fields (source/final_state/model/...),
    taken from its first transcript event -- today's SessionManifest schema
    writes exactly one such record per session."""
    return dict(events[0]) if events else {}


def _gate_from_session(session_id: str, summary: dict[str, Any]) -> VerificationGate:
    """Session-transcript analogue of `_gate_from_manifest`: PASSED only when
    the session's own recorded `final_state` is "completed" -- every other
    terminal state (failed/cancelled/killed) maps to FAILED, never a soft
    pass, preserving the same HARD RULE `curate()` already enforces for runs.
    """
    final_state = str(summary.get("final_state") or "").strip().lower()
    status = GateStatus.PASSED if final_state == "completed" else GateStatus.FAILED
    evidence_bits = [f"session_state={final_state or 'unknown'}"]
    requested = summary.get("approvals_requested")
    if isinstance(requested, int) and requested:
        evidence_bits.append(f"{summary.get('approvals_granted', 0)}/{requested} approvals granted")
    kwargs: dict[str, object] = {
        "status": status,
        "source_run_id": session_id,
        "evidence": "; ".join(evidence_bits),
    }
    finished_at = summary.get("finished_at")
    if finished_at:
        kwargs["checked_at"] = finished_at
    return VerificationGate(**kwargs)  # type: ignore[arg-type]


def _iter_text_values(node: Any) -> Iterator[str]:
    """Recursively yield every string value nested anywhere inside a parsed
    JSON transcript event.

    Deliberately schema-agnostic: today's terse SessionManifest summary line
    and a richer per-turn Claude Code transcript (nested user/assistant
    message content blocks) nest their free text completely differently, and
    this reader must find "the fix was..." wherever it happens to live without
    hard-coding either shape.
    """
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _iter_text_values(value)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_text_values(item)


def _find_discovery(events: Sequence[dict[str, Any]]) -> SessionDiscovery | None:
    """Scan a transcript's text for the FIRST explicit failure->fix or
    correction/preferred-method marker; None when nothing matches (routine
    chatter -- the caller captures nothing)."""
    for event in events:
        for text in _iter_text_values(event):
            lowered = text.lower()
            for marker in _FIX_MARKERS:
                if marker in lowered:
                    return SessionDiscovery(kind="fix", snippet=text.strip())
            for marker in _CORRECTION_MARKERS:
                if marker in lowered:
                    return SessionDiscovery(kind="correction", snippet=text.strip())
    return None


def _skill_metadata_from_session(
    session_id: str, summary: dict[str, Any], discovery: SessionDiscovery
) -> SkillMetadata:
    """Synthesize a `SkillMetadata` from a session's manifest fields + the one
    discovery mined from its transcript (evidence, kept short: this is a live
    agent transcript, not a controlled internal string, so it is truncated
    rather than trusted to be well-formed prose)."""
    project_dir = str(summary.get("project_dir") or "unknown project")
    model = summary.get("model")
    kind_label = "fix" if discovery.kind == "fix" else "correction"
    snippet = discovery.snippet[:500]

    return SkillMetadata(
        skill_id=_skill_id_for_session(session_id),
        title=f"Session learning ({kind_label}) for {project_dir}"[:2000],
        discipline="session",
        summary=(
            f"Reusable {kind_label} captured from a COMPLETED live session in "
            f"{project_dir}: {snippet}"
        )[:2000],
        input_format=f"A live, monitored Claude Code session dispatched to {project_dir}.",
        steps=[snippet],
        output_structure=f"Session {session_id} completed; see the transcript for full detail.",
        validation_rules=[
            "Session reached a COMPLETED terminal state, recorded in the sessions "
            "manifest ledger (omniagentos.sessions.manifest.SessionManifest)."
        ],
        tags=["session", kind_label],
        model=str(model) if model else None,
    )


def curate_sessions(
    *,
    ledger_dir: str | None = None,
    vault_dir: str | None = None,
    skills_dir: str | None = None,
    limit: int = _DEFAULT_SCAN_LIMIT,
    autocommit: bool | None = None,
    skills_api: SkillsAPI | None = None,
) -> CurateResult:
    """Scan up to `limit` recent bridge session transcripts and mine each
    verified, unseen one for a reusable discovery.

    Only a session that is BOTH (a) sourced from the Session Bridge itself
    (`source == "bridge"`, never an externally-reported session) and (b) has
    an explicit correction / preferred-method / failure->fix marker somewhere
    in its transcript text is captured; every other session -- including every
    COMPLETED one with no such marker -- is routine chatter and produces
    nothing (`result.scanned` still counts it, exactly like `curate()`).

    A discovery found in a session whose own `final_state` was not
    "completed" still goes through the SAME `VerificationGate`/HARD RULE as
    `curate()` -- it lands in `unverified`, never captured -- preserving the
    verification-gate rule for sessions too.

    Idempotent (repeated calls never double-mine the same session): a session
    whose computed skill note already exists on disk is skipped via the same
    `skill_relpath`-on-disk check `curate()` uses for runs.
    """
    resolved_ledger_dir = ledger_dir or default_ledger_dir()
    resolved_vault_dir = vault_dir or default_vault_dir()
    result = CurateResult()
    api = skills_api if skills_api is not None else _load_skills_api()
    existing_skills = api.list_tree() if api is not None else []

    for session_id, events in _iter_session_transcripts(resolved_ledger_dir, limit):
        result.scanned += 1
        try:
            summary = _session_summary(events)
            if str(summary.get("source") or "") != "bridge":
                continue  # never mine externally-reported (non-bridge) sessions

            relpath = skill_relpath(_skill_id_for_session(session_id))
            if (Path(resolved_vault_dir) / relpath).is_file():
                result.already_captured.append(session_id)
                continue

            discovery = _find_discovery(events)
            if discovery is None:
                continue  # routine chatter: nothing reusable found, capture nothing

            gate = _gate_from_session(session_id, summary)
            metadata = _skill_metadata_from_session(session_id, summary, discovery)
            capture_result = capture_skill(
                metadata,
                gate,
                resolved_vault_dir,
                skills_dir=skills_dir,
                autocommit=autocommit,
            )
            matched_skill_id = _match_learning_to_skill(metadata, existing_skills)
            if matched_skill_id is None:
                if api is not None:
                    api.upsert_skill(_upsert_payload(metadata))
                    existing_skills.append(_upsert_payload(metadata))
                result.captured.append(session_id)
                continue

            if api is None:
                raise RuntimeError("matched existing skill but omniagentos.skills is unavailable")
            current_skill = api.get_skill(matched_skill_id)
            diff = _proposed_diff(
                _current_skill_content(current_skill), _learning_content(metadata)
            )
            risk = _risk_for_update(metadata, current_skill, diff)
            proposal_id = api.propose_update(
                matched_skill_id,
                proposed_diff=diff,
                evidence_files=[capture_result.note_path],
                linked_execution=session_id,
                risk=risk,
                created_by="curator",
            )
            result.proposals[session_id] = proposal_id
            if risk == "low":
                api.decide_proposal(proposal_id, approve=True, decided_by="curator")
            else:
                result.pending_proposals.append(proposal_id)
        except UnverifiedCaptureError:
            result.unverified.append(session_id)
        except Exception as exc:  # noqa: BLE001 -- one bad transcript must never abort the scan
            LOG.exception("selfimprove curator: failed to capture skill for session %s", session_id)
            result.errors[session_id] = f"{type(exc).__name__}: {exc}"

    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m omniagentos.selfimprove.curator",
        description=(
            "Scan the run ledger for recently-COMPLETED, verified runs, AND the "
            "Session Bridge's completed session transcripts, capturing a reusable "
            "skill note for each not-yet-captured, verified discovery (idempotent). "
            "Both scans share this one daily cadence -- no separate scheduler."
        ),
    )
    parser.add_argument("--ledger-dir", default=None, help="override the run ledger directory")
    parser.add_argument("--vault-dir", default=None, help="override the vault directory")
    parser.add_argument("--skills-dir", default=None, help="optional SKILL.md mirror dir")
    parser.add_argument(
        "--limit",
        type=int,
        default=_DEFAULT_SCAN_LIMIT,
        help="max recent ledger manifests / session transcripts to scan (default: %(default)s)",
    )
    parser.add_argument("--autocommit", action="store_true")
    parser.add_argument(
        "--skip-sessions",
        action="store_true",
        help="skip mining Session Bridge transcripts (run-ledger mining only)",
    )
    args = parser.parse_args(argv)

    result = curate(
        ledger_dir=args.ledger_dir,
        vault_dir=args.vault_dir,
        skills_dir=args.skills_dir,
        limit=args.limit,
        autocommit=True if args.autocommit else None,
    )
    output: dict[str, Any] = {"runs": result.as_dict()}
    if not args.skip_sessions:
        session_result = curate_sessions(
            ledger_dir=args.ledger_dir,
            vault_dir=args.vault_dir,
            skills_dir=args.skills_dir,
            limit=args.limit,
            autocommit=True if args.autocommit else None,
        )
        output["sessions"] = session_result.as_dict()
    print(json.dumps(output, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
