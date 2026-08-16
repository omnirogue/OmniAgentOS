"""Context Capsule v1 — shadow manifest facade over the delivered prompt.

SHADOW MEANS SHADOW: this module observes the final assembled worker brief and
writes a deterministic evidence manifest. It never assembles, reassigns, wraps,
or returns a prompt. Enabling the flag must not change a single delivered byte.

V1 ``enforce`` behaves exactly as ``shadow`` (observe + write; change nothing).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

LOG = logging.getLogger(__name__)

CONTEXT_CAPSULE_ENV = "OMNIAGENTOS_CONTEXT_CAPSULE"
ContextCapsuleMode = Literal["off", "shadow", "enforce"]
CONTEXT_CAPSULE_MODES: frozenset[str] = frozenset({"off", "shadow", "enforce"})
DEFAULT_CONTEXT_CAPSULE_MODE: ContextCapsuleMode = "off"

# V1 reason codes (frozen enum of strings).
REASON_INCLUDED = "included"
REASON_EXCLUDED_BUDGET = "excluded_budget"
REASON_EXCLUDED_SCOPE = "excluded_scope"
REASON_EXCLUDED_SECRET = "excluded_secret"
REASON_TRUNCATED_BUDGET = "truncated_budget"
REASON_CODES: frozenset[str] = frozenset(
    {
        REASON_INCLUDED,
        REASON_EXCLUDED_BUDGET,
        REASON_EXCLUDED_SCOPE,
        REASON_EXCLUDED_SECRET,
        REASON_TRUNCATED_BUDGET,
    }
)

_BLOCK_PREFIX = "OMNIAGENTOS_DATA_NOT_INSTRUCTIONS"
_FENCE_OPEN_RE = re.compile(rf"<<<{_BLOCK_PREFIX} label=([A-Z0-9_]+) delimiter=([0-9a-f]{{24}})>>>")

__all__ = [
    "CONTEXT_CAPSULE_ENV",
    "CONTEXT_CAPSULE_MODES",
    "DEFAULT_CONTEXT_CAPSULE_MODE",
    "REASON_CODES",
    "CapsuleContractTruncated",
    "CapsuleManifest",
    "CapsuleSecretSource",
    "CapsuleSlice",
    "CapsuleSource",
    "ContextCapsuleMode",
    "build_capsule_manifest",
    "context_capsule_mode",
    "observed_sources_from_prompt",
    "write_capsule_manifest",
    "persist_capsule_manifest",
]


class CapsuleContractTruncated(ValueError):
    """Raised when a contract-kind source would be truncated or cannot fit.

    Spec: "Complete TaskContract is never truncated".
    """


class CapsuleSecretSource(ValueError):
    """Raised when a secret-kind source is offered to the capsule.

    Spec: "Secret content is not a source". Do not digest or store it.
    """


def context_capsule_mode(value: str | None = None) -> ContextCapsuleMode:
    """Resolve the capsule rollout gate, defaulting invalid or absent values off.

    V1 note: ``enforce`` is accepted as a mode token but MUST behave exactly as
    ``shadow`` (observe + write manifest, change nothing). There is no enforcing
    behaviour in V1.
    """

    raw = os.environ.get(CONTEXT_CAPSULE_ENV, "") if value is None else value
    normalized = str(raw).strip().lower()
    if normalized in CONTEXT_CAPSULE_MODES:
        return cast(ContextCapsuleMode, normalized)
    return DEFAULT_CONTEXT_CAPSULE_MODE


@dataclass(frozen=True, slots=True)
class CapsuleSource:
    """One named context slice observed or declared for the capsule."""

    name: str
    kind: str
    content: str
    rank: int = 0


@dataclass(frozen=True, slots=True)
class CapsuleSlice:
    """One source as recorded in the manifest (measured, never re-derived)."""

    name: str
    kind: str
    rank: int
    digest: str
    bytes: int
    included: bool
    reason_code: str
    present_in_brief: bool
    # Test-only mirror of content for present_in_brief assertions; stripped from JSON.
    content_for_test: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "bytes": self.bytes,
            "digest": self.digest,
            "included": self.included,
            "kind": self.kind,
            "name": self.name,
            "present_in_brief": self.present_in_brief,
            "rank": self.rank,
            "reason_code": self.reason_code,
        }


@dataclass(frozen=True, slots=True)
class CapsuleManifest:
    """Deterministic shadow description of a delivered worker brief."""

    task_id: str
    run_id: str
    project_id: str
    contract_version: str
    repo_sha: str
    preset_digest: str
    brief_digest: str
    brief_bytes: int
    byte_cap: int
    per_slice_cap: int
    compression_mode: str
    slices: tuple[CapsuleSlice, ...]
    lease_snapshot: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        lease: Any
        if self.lease_snapshot is None:
            lease = None
        else:
            # Stable key order for nested mapping.
            lease = json.loads(json.dumps(dict(self.lease_snapshot), sort_keys=True))
        return {
            "brief_bytes": self.brief_bytes,
            "brief_digest": self.brief_digest,
            "byte_cap": self.byte_cap,
            "compression_mode": self.compression_mode,
            "contract_version": self.contract_version,
            "lease_snapshot": lease,
            "per_slice_cap": self.per_slice_cap,
            "preset_digest": self.preset_digest,
            "project_id": self.project_id,
            "repo_sha": self.repo_sha,
            "run_id": self.run_id,
            "slices": [s.to_dict() for s in self.slices],
            "task_id": self.task_id,
        }

    def to_json(self) -> str:
        """Deterministic JSON: sorted keys, indent 2, UTF-8 friendly."""
        return json.dumps(self.to_dict(), sort_keys=True, indent=2, ensure_ascii=False)


def _kind_from_label(label: str) -> str:
    if label == "PROJECT_CONTRACT":
        return "contract"
    return "fenced"


def observed_sources_from_prompt(prompt: str) -> tuple[CapsuleSource, ...]:
    """Extract fenced data blocks and the unfenced remainder from a delivered prompt.

    Ranking is by first byte offset in the prompt (stable, deterministic).
    """

    occupied: list[tuple[int, int]] = []
    found: list[tuple[int, CapsuleSource]] = []

    for match in _FENCE_OPEN_RE.finditer(prompt):
        label = match.group(1)
        delimiter = match.group(2)
        closing = f"<<<END_{_BLOCK_PREFIX} delimiter={delimiter}>>>"
        close_at = prompt.find(closing, match.end())
        if close_at < 0:
            continue
        body_start = match.end()
        if body_start < len(prompt) and prompt[body_start] == "\n":
            body_start += 1
        body_end = close_at
        if body_end > body_start and prompt[body_end - 1] == "\n":
            body_end -= 1
        content = prompt[body_start:body_end]
        block_end = close_at + len(closing)
        occupied.append((match.start(), block_end))
        found.append(
            (
                match.start(),
                CapsuleSource(
                    name=label,
                    kind=_kind_from_label(label),
                    content=content,
                    rank=0,
                ),
            )
        )

    occupied.sort(key=lambda pair: pair[0])
    # Each unfenced gap is a true substring of the delivered prompt (so
    # present_in_brief can use a real containment check). Multiple gaps become
    # base_brief_0..N; a single gap keeps the canonical name base_brief.
    gaps: list[tuple[int, str]] = []
    cursor = 0
    for start, end in occupied:
        if cursor < start:
            gaps.append((cursor, prompt[cursor:start]))
        cursor = max(cursor, end)
    if cursor < len(prompt):
        gaps.append((cursor, prompt[cursor:]))
    non_empty_gaps = [(off, text) for off, text in gaps if text.strip()]
    for gap_index, (offset, text) in enumerate(non_empty_gaps):
        name = "base_brief" if len(non_empty_gaps) == 1 else f"base_brief_{gap_index}"
        found.append(
            (
                offset,
                CapsuleSource(
                    name=name,
                    kind="base_brief",
                    content=text,
                    rank=0,
                ),
            )
        )

    found.sort(key=lambda pair: (pair[0], pair[1].name))
    ranked: list[CapsuleSource] = []
    for index, (_offset, source) in enumerate(found):
        ranked.append(
            CapsuleSource(
                name=source.name,
                kind=source.kind,
                content=source.content,
                rank=index,
            )
        )
    return tuple(ranked)


def _content_digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _content_bytes(content: str) -> int:
    return len(content.encode("utf-8"))


def _present_in_brief(content: str, prompt: str) -> bool:
    """Measure whether slice content appears in the delivered prompt.

    Always inspects ``prompt``. Empty content is not treated as verified present
    (vacuous ``"" in prompt`` would always be True and would conflate
    "nothing to check" with a favourable measurement).
    """
    if not content:
        return False
    return content in prompt


def build_capsule_manifest(
    *,
    prompt: str,
    task_id: str,
    run_id: str,
    project_id: str,
    contract_version: str,
    repo_sha: str,
    preset_digest: str,
    sources: Sequence[CapsuleSource],
    byte_cap: int,
    per_slice_cap: int,
    compression_mode: str,
    lease_snapshot: Mapping[str, Any] | None = None,
) -> CapsuleManifest:
    """Build a shadow manifest measured from the delivered ``prompt``.

    Every byte-valued field is computed by measuring ``prompt`` / real slice
    content — never by re-deriving from request ids alone.
    """

    # Secret content is not a source — reject before any digest/storage.
    for source in sources:
        if source.kind == "secret":
            raise CapsuleSecretSource(f"secret-kind source {source.name!r} is not a capsule source")

    brief_digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    brief_bytes = len(prompt.encode("utf-8"))

    # Stable order: rank ascending, then name, then original index.
    indexed = list(enumerate(sources))
    indexed.sort(key=lambda pair: (pair[1].rank, pair[1].name, pair[0]))

    # Contracts first: complete TaskContract is never truncated.
    contract_items = [pair for pair in indexed if pair[1].kind == "contract"]
    optional_items = [pair for pair in indexed if pair[1].kind != "contract"]

    decisions: dict[int, CapsuleSlice] = {}
    included_total = 0

    for orig_index, source in contract_items:
        nbytes = _content_bytes(source.content)
        if nbytes > per_slice_cap:
            raise CapsuleContractTruncated(
                f"contract source {source.name!r} is {nbytes} bytes; per_slice_cap={per_slice_cap}"
            )
        if included_total + nbytes > byte_cap:
            raise CapsuleContractTruncated(
                f"contract source {source.name!r} cannot fit in byte_cap={byte_cap} "
                f"(already included {included_total} bytes; need {nbytes})"
            )
        included_total += nbytes
        decisions[orig_index] = CapsuleSlice(
            name=source.name,
            kind=source.kind,
            rank=source.rank,
            digest=_content_digest(source.content),
            bytes=nbytes,
            included=True,
            reason_code=REASON_INCLUDED,
            present_in_brief=_present_in_brief(source.content, prompt),
            content_for_test=source.content,
        )

    # Optional slices: include highest priority (lowest rank) first.
    for orig_index, source in optional_items:
        nbytes = _content_bytes(source.content)
        if included_total + nbytes > byte_cap:
            decisions[orig_index] = CapsuleSlice(
                name=source.name,
                kind=source.kind,
                rank=source.rank,
                digest=_content_digest(source.content),
                bytes=nbytes,
                included=False,
                reason_code=REASON_EXCLUDED_BUDGET,
                # Budget model exclusion is not absence from the delivered brief.
                present_in_brief=_present_in_brief(source.content, prompt),
                content_for_test=source.content,
            )
            continue
        if nbytes > per_slice_cap:
            reason = REASON_TRUNCATED_BUDGET
        else:
            reason = REASON_INCLUDED
        included_total += nbytes
        decisions[orig_index] = CapsuleSlice(
            name=source.name,
            kind=source.kind,
            rank=source.rank,
            digest=_content_digest(source.content),
            bytes=nbytes,
            included=True,
            reason_code=reason,
            present_in_brief=_present_in_brief(source.content, prompt),
            content_for_test=source.content,
        )

    # Emit slices sorted by rank, then name (deterministic; independent of input order).
    ordered_slices = tuple(
        decisions[i]
        for i, _ in sorted(
            decisions.items(),
            key=lambda item: (item[1].rank, item[1].name, item[0]),
        )
    )

    # Invariants.
    total_included = sum(s.bytes for s in ordered_slices if s.included)
    assert total_included <= byte_cap, (
        f"included slice bytes {total_included} exceed byte_cap {byte_cap}"
    )
    for slice_rec in ordered_slices:
        if slice_rec.included and slice_rec.bytes > per_slice_cap:
            assert slice_rec.reason_code == REASON_TRUNCATED_BUDGET, (
                f"slice {slice_rec.name!r} exceeds per_slice_cap without truncated_budget flag"
            )

    return CapsuleManifest(
        task_id=task_id,
        run_id=run_id,
        project_id=project_id,
        contract_version=contract_version,
        repo_sha=repo_sha,
        preset_digest=preset_digest,
        brief_digest=brief_digest,
        brief_bytes=brief_bytes,
        byte_cap=byte_cap,
        per_slice_cap=per_slice_cap,
        compression_mode=compression_mode,
        slices=ordered_slices,
        lease_snapshot=dict(lease_snapshot) if lease_snapshot is not None else None,
    )


def write_capsule_manifest(
    manifest: CapsuleManifest,
    *,
    evidence_dir: Path,
) -> Path | None:
    """Write ``context-capsule.json`` under ``evidence_dir`` when mode is not off.

    Returns None when mode is off. Never raises into the caller — IO failures
    are logged and swallowed so capsule failure cannot block a spawn.

    V1: ``enforce`` writes the same way as ``shadow``.
    """

    mode = context_capsule_mode()
    if mode == "off":
        return None
    # shadow and enforce both observe + write; neither mutates the brief.
    try:
        path = Path(evidence_dir) / "context-capsule.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(manifest.to_json(), encoding="utf-8")
        return path
    except Exception:  # noqa: BLE001 -- observation must never block spawn
        LOG.warning(
            "context capsule manifest write failed under %s",
            evidence_dir,
            exc_info=True,
        )
        return None


def persist_capsule_manifest(
    manifest: CapsuleManifest,
    *,
    store: Any,
) -> int | None:
    """Persist capsule manifest to events table (context.injected type).

    Payload per-slice rows: name, kind, rank, digest, bytes, included, reason_code, present_in_brief.
    Never raises into the caller — persistence failures are logged and swallowed
    so capsule recording cannot block a spawn.

    Returns the event id on success, None on mode off or on any error.
    """

    mode = context_capsule_mode()
    if mode == "off":
        return None

    try:
        # Build payload: array of slice dicts from manifest.to_dict()
        slices_payload = [slice_obj.to_dict() for slice_obj in manifest.slices]
        payload = {
            "task_id": manifest.task_id,
            "run_id": manifest.run_id,
            "project_id": manifest.project_id,
            "contract_version": manifest.contract_version,
            "repo_sha": manifest.repo_sha,
            "preset_digest": manifest.preset_digest,
            "brief_digest": manifest.brief_digest,
            "brief_bytes": manifest.brief_bytes,
            "byte_cap": manifest.byte_cap,
            "per_slice_cap": manifest.per_slice_cap,
            "compression_mode": manifest.compression_mode,
            # The rung this row was recorded under. Without it a reader cannot
            # tell a shadow observation from an enforcing one, and V1's
            # "enforce behaves exactly as shadow" makes the two indentical on
            # the wire — so the only way to date the rollout later is to have
            # written the mode down at the time.
            "capsule_mode": mode,
            "slices": slices_payload,
        }
        if manifest.lease_snapshot is not None:
            payload["lease_snapshot"] = dict(manifest.lease_snapshot)

        event_id = store.insert_event(
            type="context.injected",
            actor="capsule",
            action="observe_and_manifest",
            target_type="run",
            target_id=manifest.run_id,
            payload=payload,
        )
        return event_id
    except Exception:  # noqa: BLE001 -- observation must never block spawn
        LOG.warning(
            "context capsule manifest persistence failed for run_id=%s",
            manifest.run_id,
            exc_info=True,
        )
        return None
