"""Deterministically distill operational history into quarantined lesson facts.

This module only stages candidates through :func:`knowledge.ingest.ingest_episode`.
Promotion remains the metacognition consolidator's responsibility; no lesson produced
here is made active.  Library readers require explicit paths so tests and callers can
keep operator state out of scope.  The CLI supplies the conventional ``var/`` paths.
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from omniagentos.knowledge.config import admin_dsn
from omniagentos.knowledge.contracts import (
    SOURCE_TRUST,
    CandidateFact,
    ExtractionResult,
    IngestResult,
    Provenance,
)
from omniagentos.knowledge.extract import FixtureExtractor
from omniagentos.knowledge.ingest import ingest_episode
from omniagentos.knowledge.store import KnowledgeStore

LessonRecord = dict[str, Any]

TERMINAL_STATUSES = frozenset({"merged", "rejected", "parked"})
REVERSAL_STATUSES = {"unparked": "parked", "unrejected": "rejected"}
SOURCE_NAMES = ("ledger", "gates", "memories")
REQUIRED_FIELDS = (
    "situation",
    "attempt",
    "why",
    "corrective_action",
    "evidence",
    "conditions",
    "provenance",
    "kind",
)
_ID_FIELDS = REQUIRED_FIELDS[:4] + ("conditions", "kind")
_MEMORY_LINE = re.compile(r"^- (20\d{2}-\d{2}-\d{2})(?:\s*\(([^)]+)\))?\s*[:—-]\s*(\S.*)$")
_REFUSAL_SLUG = re.compile(r"(?:^|;\s*)([a-z][a-z0-9_-]*):", re.IGNORECASE)
_SHA256 = re.compile(r"^(?:sha256[:_])?([0-9a-fA-F]{64})$")
_EXIT_CLASS = re.compile(r"^exit-\d+$")
_MECHANICS_CLASSES = frozenset(
    {
        "dirty-workspace",
        "unpinned-workspace",
        "stale-gate-script",
        "gate-workspace-missing",
        "signed-receipt-missing",
    }
)
_RENDERED_FIELD_CAPS = {
    "situation": 320,
    "attempt": 500,
    "why": 400,
    "corrective_action": 500,
    "conditions": 300,
}
LESSON_STATEMENT_MAX_CHARS = 2_300


def _empty_stats() -> dict[str, int]:
    return {
        "ledger_lines_seen": 0,
        "ledger_lines_skipped": 0,
        "ledger_events_without_id": 0,
        "memory_lines_seen": 0,
        "memory_lines_harvested": 0,
        "memory_lines_skipped": 0,
        "gate_receipts_seen": 0,
        "gate_receipts_skipped": 0,
    }


def _bump(stats: dict[str, int] | None, key: str, amount: int = 1) -> None:
    if stats is not None:
        stats[key] = stats.get(key, 0) + amount


def _truncate(value: str, cap: int) -> str:
    if len(value) <= cap:
        return value
    return value[: max(0, cap - 1)].rstrip() + "…"


def _sanitize(value: str, *, cap: int) -> str:
    """Neutralize renderer delimiters and deterministically bound producer text."""
    value = value.replace("```", "`\u200b``").replace("~~~", "~\u200b~~")
    value = re.sub(
        r"(?i)</?recalled-knowledge[^>]*>",
        lambda match: match.group(0).replace("recalled-knowledge", "recalled\u200b-knowledge"),
        value,
    )
    return _truncate(value, cap)


def _sanitize_value(value: object) -> object:
    if isinstance(value, str):
        return _sanitize(_text(value), cap=500)
    if isinstance(value, Mapping):
        return {str(key): _sanitize_value(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_sanitize_value(item) for item in value]
    return value


def _text(value: object) -> str:
    """Return a compact deterministic string for a scalar or structured value."""
    if value is None:
        return ""
    if isinstance(value, str):
        return " ".join(value.split())
    if isinstance(value, (int, float, bool)):
        return str(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def lesson_id(lesson: Mapping[str, Any]) -> str:
    """Hash the stable semantic identity of a lesson, excluding observations/counts."""
    identity = lesson.get("identity")
    payload = (
        identity if isinstance(identity, Mapping) else {key: lesson[key] for key in _ID_FIELDS}
    )
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


def _lesson(*, source: str, **fields: Any) -> LessonRecord:
    identity = fields.pop("identity", None)
    fields = {key: _sanitize_value(value) for key, value in fields.items()}
    for field, cap in _RENDERED_FIELD_CAPS.items():
        if field in fields:
            fields[field] = _sanitize(_text(fields[field]), cap=cap)
    lesson: LessonRecord = {"source": source, **fields}
    missing = [key for key in REQUIRED_FIELDS if not lesson.get(key)]
    if missing:
        raise ValueError(f"lesson is missing required fields: {', '.join(missing)}")
    if identity is not None:
        lesson["identity"] = identity
    lesson["id"] = lesson_id(lesson)
    return lesson


def _artifact_for(identifier: object, queue_root: Path) -> tuple[Path | None, dict[str, Any]]:
    match = _SHA256.fullmatch(_text(identifier))
    if match is None:
        return None, {}
    filename = f"sha256_{match.group(1).lower()}.json"
    for directory in ("candidates", "proposals", "rejected", "parked"):
        path = queue_root / directory / filename
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            return path, value
    return None, {}


def _first_text(*values: object) -> str:
    return next((text for value in values if (text := _text(value))), "")


def _ledger_lesson(
    event: Mapping[str, Any],
    *,
    status: str,
    ledger_path: Path,
    line_number: int,
    queue_root: Path,
) -> LessonRecord:
    identifier = _first_text(event.get("id"), event.get("sha"))
    raw_detail = event.get("detail")
    detail: Mapping[str, Any] = raw_detail if isinstance(raw_detail, Mapping) else {}
    artifact_path, artifact = _artifact_for(identifier, queue_root)
    raw_payload = artifact.get("payload")
    artifact_payload: Mapping[str, Any] = (
        raw_payload if isinstance(raw_payload, Mapping) else {}
    )
    title = _first_text(
        artifact.get("title"),
        detail.get("title"),
        artifact_payload.get("title"),
        f"Loopqueue item {identifier}",
    )
    event_class = _first_text(detail.get("class"), detail.get("classification"))
    attempt = _first_text(
        artifact.get("summary"),
        artifact.get("proposal"),
        artifact_payload.get("summary"),
        detail.get("attempt"),
        detail.get("action"),
        f"Processed loopqueue item {identifier} to terminal status {status}.",
    )
    reason = _first_text(
        detail.get("reason"),
        detail.get("result"),
        artifact.get("reason"),
        artifact.get("rationale"),
    )
    if not reason:
        reason = {
            "merged": "The recorded verification and merge path accepted the change.",
            "rejected": "The recorded review or gate path rejected the attempted change.",
            "parked": "The item reached a terminal wait state instead of being retried.",
        }[status]
    corrective_action = (
        _first_text(detail.get("remedy"))
        or {
            "merged": "Reuse this verified approach when the same conditions recur.",
            "rejected": "Address the recorded rejection cause before submitting a changed input.",
            "parked": "Resolve the blocking condition or change the input before retrying.",
        }[status]
    )
    evidence: list[object] = [f"{ledger_path}:{line_number}"]
    if artifact_path is not None:
        evidence.append(str(artifact_path))
    return _lesson(
        source="ledger",
        situation=(
            f"{title} reached terminal status {status} in class {event_class}."
            if event_class
            else f"{title} reached terminal status {status}."
        ),
        attempt=attempt,
        why=reason,
        corrective_action=corrective_action,
        evidence=evidence,
        conditions=(
            f"When loopqueue class {event_class} reaches {status} under comparable conditions."
            if event_class
            else f"When a loopqueue item reaches {status} under comparable conditions."
        ),
        provenance=f"{ledger_path}:{line_number}; event={hashlib.sha256(identifier.encode()).hexdigest()}",
        kind="success" if status == "merged" else "failure" if status == "rejected" else "trap",
        identity={"source": "ledger", "event_id": identifier, "status": status},
    )


def lessons_from_ledger(
    ledger_path: Path | str,
    *,
    artifacts_root: Path | str | None = None,
    since: str | None = None,
    stats: dict[str, int] | None = None,
) -> list[LessonRecord]:
    """Fold ledger history to the last effective terminal state for each item."""
    path = Path(ledger_path)
    queue_root = Path(artifacts_root) if artifacts_root is not None else path.parent
    decoder = json.JSONDecoder()
    states: dict[str, tuple[str, Mapping[str, Any], int] | None] = {}
    try:
        handle = path.open(encoding="utf-8")
    except OSError:
        return []
    with handle:
        for line_number, raw_line in enumerate(handle, 1):
            _bump(stats, "ledger_lines_seen")
            try:
                event, _ = decoder.raw_decode(raw_line.lstrip())
            except (json.JSONDecodeError, UnicodeDecodeError):
                _bump(stats, "ledger_lines_skipped")
                continue
            if not isinstance(event, Mapping):
                _bump(stats, "ledger_lines_skipped")
                continue
            identifier = _first_text(event.get("id"), event.get("sha"))
            if not identifier:
                _bump(stats, "ledger_events_without_id")
                _bump(stats, "ledger_lines_skipped")
                continue
            event_timestamp = _first_text(event.get("ts"), event.get("timestamp"))
            if since:
                if not event_timestamp or not _at_or_after(event_timestamp, since):
                    continue
            statuses = [
                _text(event.get("event")).lower(),
                _text(event.get("status")).lower(),
            ]
            status = next((value for value in statuses if value in TERMINAL_STATUSES), "")
            reversal = next((value for value in statuses if value in REVERSAL_STATUSES), "")
            if reversal:
                current = states.get(identifier)
                if current is not None and current[0] == REVERSAL_STATUSES[reversal]:
                    states[identifier] = None
                continue
            if status:
                states[identifier] = (status, event, line_number)
    return [
        _ledger_lesson(
            event,
            status=status,
            ledger_path=path,
            line_number=line_number,
            queue_root=queue_root,
        )
        for status, event, line_number in (state for state in states.values() if state is not None)
    ]


def _at_or_after(value: str, since: str) -> bool:
    """Compare ISO timestamps, accepting a date-only value from MEMORY.md."""
    try:
        left = datetime.fromisoformat(value.replace("Z", "+00:00"))
        right = datetime.fromisoformat(since.replace("Z", "+00:00"))
        if left.tzinfo is None and right.tzinfo is not None:
            left = left.replace(tzinfo=right.tzinfo)
        if right.tzinfo is None and left.tzinfo is not None:
            right = right.replace(tzinfo=left.tzinfo)
        return left >= right
    except ValueError:
        return value >= since


def _nested_refusal_code(receipt: Mapping[str, Any]) -> str:
    refusal = receipt.get("refusal")
    if not isinstance(refusal, Mapping):
        return ""
    return _first_text(refusal.get("code"), refusal.get("class"), refusal.get("reason_code"))


def _refusal_classes(receipt: Mapping[str, Any]) -> list[str]:
    explicit = _first_text(
        receipt.get("refusal_code"),
        receipt.get("refusal_class"),
        receipt.get("reason_code"),
        _nested_refusal_code(receipt),
    )
    if explicit:
        return [explicit.lower()]
    reason = _text(receipt.get("refusal_reason"))
    slugs = list(dict.fromkeys(match.lower() for match in _REFUSAL_SLUG.findall(reason)))
    if slugs:
        return slugs
    return [f"exit-{receipt.get('exit_code', 'unknown')}"]


def _truthy(value: object) -> bool:
    return (
        value is True
        or (isinstance(value, int) and not isinstance(value, bool) and value != 0)
        or (isinstance(value, str) and value.lower() in {"1", "true", "yes"})
    )


def _explicit_verdict(receipt: Mapping[str, Any]) -> bool:
    verdicts = {"accepted", "candidate-defect", "failed", "passed", "refused", "rejected"}
    return any(
        _text(receipt.get(field)).lower() in verdicts for field in ("verdict", "result", "outcome")
    )


def _mechanics_class(refusal_class: str) -> bool:
    return refusal_class in _MECHANICS_CLASSES or refusal_class.startswith("counterfeit-")


def _gate_why(receipt: Mapping[str, Any]) -> str:
    reason = _text(receipt.get("refusal_reason"))
    if not reason or re.search(
        r"\bgate\s+(?:failed|refused)\s+because\s+gate\s+(?:failed|refused)\b",
        reason,
        re.IGNORECASE,
    ):
        return "The receipt contains no causal detail beyond the gate verdict."
    return reason


def lessons_from_gate_receipts(
    gates_dir: Path | str,
    *,
    since: str | None = None,
    stats: dict[str, int] | None = None,
) -> list[LessonRecord]:
    """Collapse merge-gate refusals while separating instrument failures."""
    root = Path(gates_dir)
    groups: dict[str, list[tuple[Path, Mapping[str, Any], bool]]] = defaultdict(list)
    for path in sorted(root.glob("*.run-*.json")):
        _bump(stats, "gate_receipts_seen")
        try:
            receipt = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            _bump(stats, "gate_receipts_skipped")
            continue
        if not isinstance(receipt, Mapping):
            _bump(stats, "gate_receipts_skipped")
            continue
        receipt_timestamp = _first_text(
            receipt.get("timestamp"), receipt.get("started_at"), receipt.get("ts")
        )
        if not receipt_timestamp:
            match = re.search(r"\.run-(.+)-\d+\.json$", path.name)
            receipt_timestamp = match.group(1) if match else ""
        if since:
            if not receipt_timestamp or not _at_or_after(receipt_timestamp, since):
                continue
        exit_code = receipt.get("exit_code")
        if exit_code == 0 and not receipt.get("refusal_reason"):
            continue
        for refusal_class in _refusal_classes(receipt):
            is_instrument = (
                _truthy(receipt.get("instrument_error"))
                or _mechanics_class(refusal_class)
                or (
                    _EXIT_CLASS.fullmatch(refusal_class) is not None
                    and not _explicit_verdict(receipt)
                )
            )
            groups[refusal_class].append((path, receipt, is_instrument))

    lessons: list[LessonRecord] = []
    for refusal_class, observations in sorted(groups.items()):
        is_instrument = any(observation[2] for observation in observations)
        examples = [
            {
                "sha": _first_text(receipt.get("candidate_sha"), path.name.split(".run-", 1)[0]),
                "path": str(path),
            }
            for path, receipt, _ in observations[:5]
        ]
        evidence: list[object] = []
        for path, receipt, _ in observations[:5]:
            evidence.extend(
                [
                    str(path),
                    {
                        "command": "scripts/merge-gate.sh",
                        "exit_code": receipt.get("exit_code")
                        if isinstance(receipt.get("exit_code"), int)
                        else 1,
                    },
                ]
            )
        shas = [example["sha"] for example in examples if example["sha"]]
        first_reason = _gate_why(observations[0][1])
        lesson = _lesson(
            source="gates",
            situation=(
                f"Merge-gate instrumentation reported class {refusal_class}."
                if is_instrument
                else f"Merge gate repeatedly refused candidate class {refusal_class}."
            ),
            attempt=f"Ran the merge gate on inputs that produced refusal class {refusal_class}.",
            why=first_reason,
            corrective_action=(
                f"Repair and verify the gate instrument for {refusal_class}; do not blame or change "
                "the candidate diff on this evidence."
                if is_instrument
                else f"Apply the receipt's candidate remedy for {refusal_class} before retrying."
            ),
            evidence=evidence,
            conditions=f"When merge-gate refusal class {refusal_class} recurs.",
            provenance=f"{observations[0][0]}; sha={','.join(shas) or 'unknown'}",
            kind="instrument" if is_instrument else "trap",
            identity={
                "source": "gates",
                "refusal_class": refusal_class,
                "kind": "instrument" if is_instrument else "candidate",
            },
        )
        lesson["refusal_class"] = refusal_class
        lesson["count"] = len(observations)
        lesson["examples"] = examples
        lessons.append(lesson)
    return lessons


def _memory_kind(text: str) -> str:
    lowered = text.lower()
    if any(word in lowered for word in ("never ", "avoid ", "trap", "do not ", "must not ")):
        return "trap"
    if any(word in lowered for word in ("fail", "error", "reject", "refus", "broken")):
        return "failure"
    return "success"


def lessons_from_memories(
    memories_glob: Path | str,
    *,
    since: str | None = None,
    stats: dict[str, int] | None = None,
) -> list[LessonRecord]:
    """Read only dated single-line learnings from explicitly selected MEMORY.md files."""
    pattern = str(memories_glob)
    paths = sorted(Path(value) for value in glob.glob(pattern, recursive=True))
    lessons: list[LessonRecord] = []
    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for line_number, line in enumerate(lines, 1):
            _bump(stats, "memory_lines_seen")
            match = _MEMORY_LINE.fullmatch(line)
            if match is None:
                _bump(stats, "memory_lines_skipped")
                continue
            date, scope, text = match.groups()
            if since and not _at_or_after(date, since):
                _bump(stats, "memory_lines_skipped")
                continue
            _bump(stats, "memory_lines_harvested")
            lessons.append(
                _lesson(
                    source="memories",
                    situation=f"Operational learning recorded on {date}.",
                    attempt=text,
                    why="The project memory recorded this as a repeatable operational learning.",
                    corrective_action=f"Apply the recorded learning: {text}",
                    evidence=[f"{path}:{line_number}"],
                    conditions=(
                        f"When the recorded scope recurs: {scope}."
                        if scope
                        else "When the recorded project conditions recur."
                    ),
                    provenance=f"{path}:{line_number}",
                    kind=_memory_kind(text),
                    identity={
                        "source": "memories",
                        "path": str(path),
                        "date": date,
                        "scope": scope or "",
                        "text": text,
                    },
                )
            )
    return lessons


def _deduplicate(lessons: Iterable[LessonRecord]) -> list[LessonRecord]:
    """Keep one record per semantic id while preserving deterministic source order."""
    unique: dict[str, LessonRecord] = {}
    for lesson in lessons:
        unique.setdefault(str(lesson["id"]), lesson)
    return list(unique.values())


def consolidate_lessons(
    *,
    ledger_path: Path | str,
    gates_dir: Path | str,
    memories_glob: Path | str,
    sources: Sequence[str] = SOURCE_NAMES,
    since: str | None = None,
    limit: int | None = None,
    stats: dict[str, int] | None = None,
) -> list[LessonRecord]:
    """Collect selected explicit sources and return deterministic lesson records."""
    if limit is not None and limit < 1:
        raise ValueError("limit must be a positive integer")
    selected = tuple(dict.fromkeys(sources))
    unknown = sorted(set(selected) - set(SOURCE_NAMES))
    if unknown:
        raise ValueError(f"unknown lesson sources: {', '.join(unknown)}")
    lessons: list[LessonRecord] = []
    if "ledger" in selected:
        lessons.extend(lessons_from_ledger(ledger_path, since=since, stats=stats))
    if "gates" in selected:
        lessons.extend(lessons_from_gate_receipts(gates_dir, since=since, stats=stats))
    if "memories" in selected:
        lessons.extend(lessons_from_memories(memories_glob, since=since, stats=stats))
    consolidated = _deduplicate(lessons)
    return consolidated[:limit] if limit is not None else consolidated


def lesson_statement(lesson: Mapping[str, Any]) -> str:
    """Render the compact theory-doc statement staged as the candidate fact."""
    rendered = {
        field: _sanitize(_text(lesson[field]), cap=cap)
        for field, cap in _RENDERED_FIELD_CAPS.items()
    }
    kind = _sanitize(_text(lesson["kind"]), cap=32)
    statement = (
        f"Lesson [{kind}]: {rendered['situation']} "
        f"Attempt: {rendered['attempt']} Why: {rendered['why']} "
        f"Corrective action: {rendered['corrective_action']} "
        f"Conditions: {rendered['conditions']}"
    )
    if len(statement) > LESSON_STATEMENT_MAX_CHARS:
        raise AssertionError("constructed lesson statement exceeds the recall-safe limit")
    return statement


def _previous_staging(store: KnowledgeStore, identifier: str) -> IngestResult | None:
    """Return an already staged lesson; the frozen store has no source-ref lookup."""
    if not isinstance(store, KnowledgeStore):
        # The isolated InMemoryKnowledgeStore intentionally exposes only point reads.
        episodes = getattr(store, "_episodes", {})
        facts = getattr(store, "_facts", {})
        for episode in episodes.values():
            if episode.source == "curator" and episode.source_ref == identifier:
                fact_ids = sorted(
                    fact.id for fact in facts.values() if fact.episode_id == episode.id
                )
                return IngestResult(episode_id=episode.id, fact_ids=fact_ids)
        return None
    with store._lock:  # noqa: SLF001 - frozen store has no source-ref read helper
        with store._conn().cursor() as cursor:  # noqa: SLF001
            cursor.execute(
                """
                SELECT id FROM episodes
                WHERE source = %s AND source_ref = %s
                ORDER BY id DESC LIMIT 1
                """,
                ("curator", identifier),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            episode_id = int(row[0])
            cursor.execute("SELECT id FROM facts WHERE episode_id = %s ORDER BY id", (episode_id,))
            fact_ids = [int(fact_row[0]) for fact_row in cursor.fetchall()]
    return IngestResult(episode_id=episode_id, fact_ids=fact_ids)


def stage_lessons(
    store: KnowledgeStore, lessons: Iterable[Mapping[str, Any]]
) -> list[IngestResult]:
    """Stage unique lessons through the existing curator ingestion API."""
    results: list[IngestResult] = []
    seen: set[str] = set()
    for lesson in lessons:
        identifier = str(lesson["id"])
        if identifier in seen:
            continue
        seen.add(identifier)
        previous = _previous_staging(store, identifier)
        if previous is not None:
            results.append(previous)
            continue
        statement = lesson_statement(lesson)
        extraction = ExtractionResult(
            facts=[
                CandidateFact(
                    statement=statement,
                    provenance=Provenance.EXTRACTED,
                )
            ]
        )
        results.append(
            ingest_episode(
                store,
                source="curator",
                content=statement,
                source_ref=identifier,
                discipline="operations",
                extractor=FixtureExtractor(result=extraction),
                trust=SOURCE_TRUST["curator"],
            )
        )
    return results


def run_consolidator(
    *,
    ledger_path: Path | str,
    gates_dir: Path | str,
    memories_glob: Path | str,
    sources: Sequence[str] = SOURCE_NAMES,
    since: str | None = None,
    limit: int | None = None,
    stats: dict[str, int] | None = None,
    apply: bool = False,
    store: KnowledgeStore | None = None,
) -> tuple[list[LessonRecord], list[IngestResult]]:
    """Collect lessons and optionally stage them; dry runs never touch ``store``."""
    lessons = consolidate_lessons(
        ledger_path=ledger_path,
        gates_dir=gates_dir,
        memories_glob=memories_glob,
        sources=sources,
        since=since,
        limit=limit,
        stats=stats,
    )
    if not apply:
        return lessons, []
    if store is None:
        raise ValueError("apply mode requires a configured knowledge store")
    return lessons, stage_lessons(store, lessons)


def _print_summary(
    lessons: Sequence[Mapping[str, Any]], stats: Mapping[str, int] | None = None
) -> None:
    per_source = {name: 0 for name in SOURCE_NAMES}
    per_kind = {name: 0 for name in ("failure", "instrument", "success", "trap")}
    refusal_classes: set[str] = set()
    for lesson in lessons:
        per_source[str(lesson["source"])] += 1
        per_kind[str(lesson["kind"])] += 1
        if lesson.get("refusal_class"):
            refusal_classes.add(str(lesson["refusal_class"]))
    summary = {
        "total": len(lessons),
        "per_source": per_source,
        "per_kind": per_kind,
        "distinct_refusal_classes_collapsed": len(refusal_classes),
        **(dict(stats) if stats is not None else {}),
    }
    print(json.dumps(summary, sort_keys=True, indent=2))
    print(json.dumps(list(lessons[:5]), sort_keys=True, indent=2, ensure_ascii=False))


def _parse_sources(value: str) -> tuple[str, ...]:
    sources = tuple(part.strip() for part in value.split(",") if part.strip())
    unknown = sorted(set(sources) - set(SOURCE_NAMES))
    if not sources or unknown:
        detail = f": {', '.join(unknown)}" if unknown else ""
        raise argparse.ArgumentTypeError(f"sources must be ledger,gates,memories{detail}")
    return sources


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="print only (default)")
    mode.add_argument("--apply", action="store_true", help="stage quarantined curator facts")
    parser.add_argument("--sources", type=_parse_sources, default=SOURCE_NAMES)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--ledger-path", type=Path)
    parser.add_argument("--gates-dir", type=Path)
    parser.add_argument("--memories-glob")
    parser.add_argument("--since", help="include records at or after this ISO timestamp")
    parser.add_argument("--limit", type=int, help="maximum lessons to print or stage")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    root = args.repo_root.expanduser().resolve()
    ledger_path = args.ledger_path or root / "var" / "loopqueue" / "ledger.jsonl"
    gates_dir = args.gates_dir or root / "var" / "gate-evidence" / "records" / "merge-gate"
    memories_glob = args.memories_glob or str(root / "var" / "memories" / "*" / "MEMORY.md")
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be a positive integer")
    stats = _empty_stats()

    if not args.apply:
        lessons, _ = run_consolidator(
            ledger_path=ledger_path,
            gates_dir=gates_dir,
            memories_glob=memories_glob,
            sources=args.sources,
            since=args.since,
            limit=args.limit,
            stats=stats,
        )
        _print_summary(lessons, stats)
        return 0

    configured_dsn = admin_dsn()
    if not configured_dsn:
        parser.error("OMNIAGENTOS_KNOWLEDGE_ADMIN_DSN is required for --apply")
    with KnowledgeStore(dsn=configured_dsn) as store:
        lessons, staged = run_consolidator(
            ledger_path=ledger_path,
            gates_dir=gates_dir,
            memories_glob=memories_glob,
            sources=args.sources,
            since=args.since,
            limit=args.limit,
            stats=stats,
            apply=True,
            store=store,
        )
    _print_summary(lessons, stats)
    print(f"staged={len(staged)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
