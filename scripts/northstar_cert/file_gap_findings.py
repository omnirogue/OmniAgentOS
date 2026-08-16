#!/usr/bin/env python3
"""File North Star certification gaps into the loop queue as `kind: finding`.

The certification run produces gap artifacts (``scripts/northstar_cert/emit_gaps.py``);
this adapter turns each open gap into ONE loop-queue finding envelope, so a
certification breakage becomes work the planning loop can pick up instead of a
file nobody reads.

Three rules carry the design:

**A gap is a FINDING, never a proposal.** A certification gap is observed
breakage with no implementation plan attached. Findings need no ``paths``; the
planning loop is what turns one into a proposal that does.

**The envelope id is hashed over run-STABLE fields only.** ``id`` is
``sha256:`` over the RFC-8785 canonicalization of ``payload`` and nothing else,
so anything that changes between two identical runs — run ids, receipt paths,
timestamps, how many times the gap has been seen — lives OUTSIDE ``payload``,
in top-level ``evidence`` and ``northstar_cert``. Get this wrong and every
daily run mints a brand-new id for the same breakage and floods the queue.
The gap artifact's own ``signature`` hashes a DIFFERENT object (the emitter's
identity payload); the two ids are deliberately not interchangeable, and the
gap's signature is recorded outside the payload as provenance, not as identity.

**A re-detected gap that was already parked or rejected must not error, and
must not re-enter the queue.** The dedup pre-check reads ``findings/``,
``rejected/`` and ``parked/`` before writing, exactly as the house filer does.
A daily job that re-files a parked item every morning is how a queue becomes
unreadable. The ONE exception is a REGRESSION: a gap that was closed and then
broke again is new information, and it files under a new identity that names
the occurrence it regressed from — the previous member of the gap's identity
CHAIN (see :func:`_identity_chain`).

**A resolution raises a QUESTION; it never closes another loop's work.** When
certification observes the check passing, the evidence stays where it is (the
``resolved_run`` stamp on the gap artifact, which this module never rewrites)
and an ``inquiry`` goes to the queue asking the owning loop to reconcile the
still-open occurrence (:func:`_inquiry_envelope`). This producer read a receipt;
it did not apply a repair, so it has no standing to mint a terminal event.

Two-key arming: ``--live`` writes nothing unless ``NSCERT_GAPS_LIVE=1`` is also
in the environment — enforced at the WRITE BOUNDARY (:func:`file_gap_findings`),
not just in the CLI, so no Python caller can arm itself with one key. The
default is a dry run that prints what it would file.

Fail-closed: a missing or unwritable queue is exit 2 (do not retry this input),
never a degraded write path and never a silent "nothing to file".
"""

from __future__ import annotations

import argparse
import json
import os
import sys as _sys
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
_sys.path.insert(0, str(_REPO_ROOT / "pipeline"))
from bridge.ledger_write import append_event  # noqa: E402

try:  # imported as a package member: `scripts.northstar_cert.file_gap_findings`
    from .canonical import content_identity
except ImportError:  # pragma: no cover - exercised when run as a bare script
    # launchd runs this file as a SCRIPT, so sys.path[0] is this directory and
    # there is no parent package for the relative import to resolve against
    # (the same fallback scripts/*/launchd.py uses).
    _sys.path.insert(0, str(Path(__file__).resolve().parent))
    from canonical import content_identity

DEFAULT_QUEUE = Path("/Users/youruser/OmniAgentOS/var/loopqueue")
DEFAULT_GAPS_DIR = Path("var/northstar-cert/gaps")
LIVE_ENV_FLAG = "NSCERT_GAPS_LIVE"
PRODUCER_ACTOR = "northstar-cert"
PRODUCER_ROLE = "external"
CONTRACT = "v1.1"
TITLE_MAX = 200
# Where an id may already be known. `findings/` is the destination; `rejected/`
# and `parked/` are terminal states a re-detection must NOT walk back.
TERMINAL_DIRS = ("findings", "rejected", "parked")
# An inquiry lives in `inquiries/`; the same three-directory pre-check applies
# to it (bridge/integrity.py uses exactly this set for the inquiries it raises).
INQUIRY_DIR = "inquiries"
INQUIRY_DIRS = (INQUIRY_DIR, "rejected", "parked")
# What one attempt to publish an inquiry did. `repaired` is NOT `filed`: the
# artifact was already on disk and only its missing ledger event was appended,
# and counting a repair as a fresh question would overstate what the pass did.
INQUIRY_FILED = "filed"
INQUIRY_REPAIRED = "repaired"
INQUIRY_SKIPPED = "skipped"
# Where a resolution may still be reconciled. `rejected/` is deliberately absent:
# there is nothing to close about an occurrence the loop already refused.
RECONCILABLE_DIRS = ("findings", "parked")
# Every field the emitter must have written. An absent one is refused rather
# than defaulted: a gap missing its capability is an emitter defect, and a
# defaulted "unknown" would silently fork the identity of every later run.
REQUIRED_IDENTITY_FIELDS = (
    "check_id",
    "capability",
    "project",
    "scope",
    "verdict",
    "reason_class",
)
# ``schema``, ``dry_run`` and ``signature`` are REQUIRED, not optional: an absent
# ``dry_run`` used to read as "not a dry run" (None is falsy), so a file with no
# mode at all was interpreted as live input. Absence is never favorable.
REQUIRED_GAP_FIELDS = (
    "identity_payload",
    "check_id",
    "cause_class",
    "severity",
    "hard_gate",
    "schema",
    "dry_run",
    "signature",
)
# Mirrors emit_gaps._SCHEMA_LIVE. Kept as a literal rather than an import (this
# module is also run as a bare script by launchd); the two constants are pinned
# equal by tests/scripts/test_nscert_file_gap_findings.py.
LIVE_SCHEMA = "omniagentos.northstar-gap.v1"
# Terminal ledger events, per ThreeLoops CONTRACT.md §5: an item gets exactly
# one of these, and NONE of them may be minted by this producer (see
# `_inquiry_envelope`); they are read to tell an already-closed occurrence from
# an open one.
TERMINAL_EVENTS = frozenset({"merged", "completed", "rejected", "closed"})
# The envelope schema's inquiry branch requires exactly these three.
INQUIRY_PAYLOAD_FIELDS = ("area", "observation", "why_not_a_fix")


class GapFilingError(RuntimeError):
    """The input or the queue is not fit to file against. Do not retry unchanged."""


class GapFilingNotArmed(GapFilingError):
    """``live=True`` was requested without ``NSCERT_GAPS_LIVE=1`` in the environment.

    A distinct type because this is a REFUSAL OF AUTHORITY, not a bad input: the
    caller asked for a live write it is not armed to make. It is a
    ``GapFilingError`` so the CLI still exits 2 (do not retry this input).
    """


@dataclass
class FilingResult:
    """What one pass did — counted, because "0 filed" has two very different causes."""

    live: bool
    queue: Path
    gaps_dir: Path
    gaps_read: int = 0
    filed: list[str] = field(default_factory=list)
    would_file: list[str] = field(default_factory=list)
    skipped_existing: int = 0
    skipped_resolved: int = 0
    lost_race: int = 0
    #: `found` events reconstructed for artifacts that were published before an
    #: earlier ledger append failed — an orphan is repaired, never skipped.
    ledger_repaired: int = 0
    #: the same repair for INQUIRY artifacts: the question was published and its
    #: `inquired` event never landed. Counted apart from `inquiries_filed`
    #: because no new question was asked — an old one was made visible again.
    inquiries_ledger_repaired: int = 0
    #: inquiries raised asking the owning loop to reconcile/close a queued
    #: finding whose check certification now observes PASSING. This producer
    #: does NOT terminalize other loops' work — see :func:`_inquiry_envelope`.
    inquiries_filed: int = 0
    #: regressions filed under a new identity because the previous occurrence in
    #: the chain is already known and must not be walked back.
    regressions_filed: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "live": self.live,
            "queue": str(self.queue),
            "gaps_dir": str(self.gaps_dir),
            "gaps_read": self.gaps_read,
            "filed": self.filed,
            "would_file": self.would_file,
            "skipped_existing": self.skipped_existing,
            "skipped_resolved": self.skipped_resolved,
            "lost_race": self.lost_race,
            "ledger_repaired": self.ledger_repaired,
            "inquiries_filed": self.inquiries_filed,
            "inquiries_ledger_repaired": self.inquiries_ledger_repaired,
            "regressions_filed": self.regressions_filed,
        }


def _utcnow() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _require_queue(queue: Path) -> None:
    """Refuse anything that is not a writable loop queue.

    Checked in dry-run too: a dry run that cannot read `findings/`/`rejected/`/
    `parked/` cannot honestly say what it WOULD skip, and "would file 12" from a
    run that could not see the queue is worse than a refusal.
    """
    if not queue.is_dir():
        raise GapFilingError(f"queue root is not a directory: {queue}")
    for name in TERMINAL_DIRS:
        directory = queue / name
        if not directory.is_dir():
            raise GapFilingError(f"queue is missing {name}/: {directory}")
    if not os.access(queue, os.W_OK | os.X_OK):
        raise GapFilingError(f"queue root is not writable (ledger append): {queue}")
    if not os.access(queue / "findings", os.W_OK | os.X_OK):
        raise GapFilingError(f"queue findings/ is not writable: {queue / 'findings'}")
    ledger = queue / "ledger.jsonl"
    if ledger.exists() and not os.access(ledger, os.W_OK):
        raise GapFilingError(f"queue ledger is not writable: {ledger}")


def _require_intact_identity(gap: dict[str, Any], path: Path) -> None:
    """RECOMPUTE the emitter's signature, and refuse a self-contradicting gap.

    A ``signature`` that is only checked for being a non-empty string is not a
    signature — an all-zero one was accepted, and with it a ``check_id`` that
    disagreed with the identity the finding is actually built from (R1-011).
    The queue would then carry an envelope describing one check under the name
    of another, and the gap's own provenance field would vouch for it.

    Both halves are refusals, not repairs: recomputing the signature FROM the
    payload and storing it would make every forgery self-consistent, which is
    the favourable-absence shape one level up.
    """
    identity = gap["identity_payload"]
    recomputed = content_identity(identity)
    if gap["signature"] != recomputed:
        raise GapFilingError(
            f"gap artifact {path} signature {gap['signature']} does not match its "
            f"identity_payload (recomputed {recomputed}); refusing a forged or "
            "hand-edited identity"
        )
    for key in ("check_id", "capability", "project"):
        if key in gap and gap[key] != identity.get(key):
            raise GapFilingError(
                f"gap artifact {path} top-level {key} {gap[key]!r} contradicts its "
                f"identity_payload {key} {identity.get(key)!r}"
            )


def _read_gap(path: Path, *, live: bool) -> dict[str, Any]:
    """Parse and VALIDATE one gap artifact, refusing anything ambiguous.

    Mode is validated here, before any caller can act on the file: a gap that
    does not state ``dry_run`` is refused rather than assumed live, and under
    ``live`` the artifact must carry the exact live schema. A forged or
    truncated file is an input defect (exit 2), never a queue write.

    The declared ``signature`` is RECOMPUTED (:func:`_require_intact_identity`),
    in dry run as well as live: a dry run that reports "would file" for a gap the
    live pass will refuse is not a preview of anything.
    """
    try:
        gap = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GapFilingError(f"gap artifact {path} is unreadable: {exc}") from exc
    if not isinstance(gap, dict):
        raise GapFilingError(f"gap artifact {path} is not an object")
    missing = [key for key in REQUIRED_GAP_FIELDS if key not in gap]
    if missing:
        raise GapFilingError(f"gap artifact {path} is missing {', '.join(missing)}")
    identity = gap["identity_payload"]
    if not isinstance(identity, dict):
        raise GapFilingError(f"gap artifact {path} has a non-object identity_payload")
    absent = [key for key in REQUIRED_IDENTITY_FIELDS if key not in identity]
    if absent:
        raise GapFilingError(f"gap artifact {path} identity_payload is missing {', '.join(absent)}")
    if not isinstance(gap["dry_run"], bool):
        raise GapFilingError(
            f"gap artifact {path} has a non-boolean dry_run {gap['dry_run']!r}; "
            "the emit mode must be stated, never inferred"
        )
    for key in ("schema", "signature"):
        if not isinstance(gap[key], str) or not gap[key]:
            raise GapFilingError(f"gap artifact {path} has an empty or non-string {key}")
    _require_intact_identity(gap, path)
    if live:
        if gap["dry_run"]:
            # A dry-run artifact filed into the live queue is a WIRING error
            # (live filing pointed at the dry-run corpus). Skipping silently
            # would make the mistake look like "no gaps today".
            raise GapFilingError(
                f"refusing to file dry-run artifact {path} into the live queue; "
                "point --gaps-dir at the live emit directory"
            )
        if gap["schema"] != LIVE_SCHEMA:
            raise GapFilingError(
                f"gap artifact {path} declares schema {gap['schema']!r}, "
                f"not the live gap schema {LIVE_SCHEMA!r}"
            )
    return gap


def build_payload(
    gap: dict[str, Any],
    *,
    regression_of: str | None = None,
    regression_run: str | None = None,
) -> dict[str, Any]:
    """The hashed body — and therefore the dedup identity of the finding.

    EVERY field here must be identical across two runs that observe the same
    breakage. Nothing derived from a run id, a clock, a path or a counter.

    ``regression_of``/``regression_run`` are the deliberate exception: a
    regression is a DIFFERENT occurrence from the one that was closed, so it
    hashes differently on purpose. Both are stable for as long as that
    regression stands (the regressed run id does not change between daily
    runs), so the new identity still dedups run over run.
    """
    identity = gap["identity_payload"]
    check_id = str(identity["check_id"])
    capability = str(identity["capability"])
    project = str(identity["project"])
    scope = str(identity["scope"])
    verdict = str(identity["verdict"])
    reason_class = str(identity["reason_class"])
    cause_class = str(gap["cause_class"])
    hard_gate = bool(gap["hard_gate"])
    gate_note = "hard certification gate" if hard_gate else "non-gating certification check"
    symptom = (
        f"North Star certification check {check_id} ({capability}, scope {scope}) "
        f"is {verdict} in project {project} — reason class {reason_class}, "
        f"cause class {cause_class}. This is a {gate_note} and it is unsatisfied."
    )
    if regression_of:
        symptom += (
            f" It REGRESSED: an earlier occurrence ({regression_of}) was observed "
            f"resolved and the check broke again in run {regression_run or 'unknown'}."
        )
    payload: dict[str, Any] = {
        "symptom": symptom,
        "source": PRODUCER_ACTOR,
        "source_ref": f"northstar-cert/{check_id}",
        "check_id": check_id,
        "capability": capability,
        "project": project,
        "scope": scope,
        "verdict": verdict,
        "reason_class": reason_class,
        "cause_class": cause_class,
        "severity": str(gap["severity"]),
        "hard_gate": hard_gate,
        "recommended_next_step": str(
            gap.get(
                "recommended_next_step",
                f"review evidence for {check_id} before planning a fix",
            )
        ),
    }
    if regression_of:
        payload["regression_of"] = regression_of
        payload["regression_run"] = str(regression_run or "unknown")
    return payload


def _priority(verdict: str, hard_gate: bool) -> int:
    if hard_gate and verdict in ("FAIL", "NOT_EVALUABLE"):
        return 1
    if verdict == "FAIL":
        return 2
    return 3


def _evidence(gap: dict[str, Any]) -> list[dict[str, Any]]:
    """One entry per observed run — run-specific, so deliberately NOT hashed.

    ``verified_by: reading`` is the honest value: this adapter read a recorded
    certification receipt, it did not execute the check. Claiming ``execution``
    would require a command and an exit code the gap artifact does not carry,
    and an invented exit code is exactly the kind of favourable detail that
    makes evidence worthless.

    ``result`` states whether the cited receipt WAS THERE TO READ. The path is
    stat-ed (relative paths against this process's cwd, which is how the
    emitter wrote them); a missing receipt is reported as ``receipt-missing``
    rather than dropped, because an evidence entry that quietly disappears is
    how a citation with nothing behind it passes for a verified one.
    """
    entries: list[dict[str, Any]] = []
    actual = str(gap.get("actual", "unknown"))
    for reference in gap.get("evidence_refs") or []:
        if not isinstance(reference, dict):
            continue
        run_id = str(reference.get("run_id", "unknown"))
        entry: dict[str, Any] = {
            "claim": f"northstar-cert run {run_id} recorded {actual} for {gap['check_id']}",
            "verified_by": "reading",
        }
        receipt = reference.get("receipt")
        present = False
        if isinstance(receipt, str) and receipt:
            entry["receipt"] = receipt
            try:
                present = Path(receipt).is_file()
            except OSError:  # pragma: no cover - unreadable parent, still "missing"
                present = False
        entry["result"] = "receipt-present" if present else "receipt-missing"
        entry["recorded"] = actual
        bundle = reference.get("bundle")
        if isinstance(bundle, str):
            entry["bundle"] = bundle
        entries.append(entry)
    if not entries:
        raise GapFilingError(f"gap {gap['check_id']} carries no evidence_refs to cite")
    return entries


def build_finding(
    gap: dict[str, Any],
    *,
    created_at: str | None = None,
    regression_of: str | None = None,
    regression_run: str | None = None,
) -> dict[str, Any]:
    """A v1.1 envelope for one gap. Pure: same gap in, same id out."""
    payload = build_payload(gap, regression_of=regression_of, regression_run=regression_run)
    identifier = content_identity(payload)
    title = f"northstar-cert: {payload['check_id']} {payload['verdict']} ({payload['cause_class']})"
    if regression_of:
        title += f" regression in {payload['regression_run']}"
    envelope = {
        "contract": CONTRACT,
        "id": identifier,
        "kind": "finding",
        "title": title[:TITLE_MAX],
        "created_at": created_at or _utcnow(),
        "priority": _priority(payload["verdict"], payload["hard_gate"]),
        "producer": {"role": PRODUCER_ROLE, "actor": PRODUCER_ACTOR},
        "evidence": _evidence(gap),
        "payload": payload,
        # Run-specific provenance, kept OUT of the payload on purpose: none of
        # it may participate in the identity or dedup breaks on the second run.
        "northstar_cert": {
            "gap_id": gap.get("gap_id"),
            "gap_signature": gap.get("signature"),
            "schema": gap.get("schema"),
            "frequency": gap.get("frequency"),
            "first_seen_run": gap.get("first_seen_run"),
            "latest_run": gap.get("latest_run"),
            "observed_runs": gap.get("observed_runs"),
            "scenario": gap.get("scenario"),
            "regressions": gap.get("regressions"),
        },
    }
    _validate_envelope(envelope)
    return envelope


def _validate_envelope(envelope: dict[str, Any], *, kind: str = "finding") -> None:
    """The checks the schema would make, made here because the schema lives in
    another repository this tool must not import.

    ``kind`` selects the payload branch, mirroring the envelope schema's own
    ``allOf``: a finding needs ``symptom``; an inquiry needs ``area``,
    ``observation`` and ``why_not_a_fix``.
    """
    for key in ("id", "kind", "title", "created_at", "producer", "payload"):
        if key not in envelope:
            raise GapFilingError(f"envelope is missing required field {key!r}")
    if envelope["kind"] != kind:
        raise GapFilingError(f"envelope kind must be {kind!r}, got {envelope['kind']!r}")
    if not 1 <= len(envelope["title"]) <= TITLE_MAX:
        raise GapFilingError(f"envelope title length {len(envelope['title'])} is out of range")
    if "priority" in envelope and not 0 <= envelope["priority"] <= 3:
        raise GapFilingError(f"envelope priority {envelope['priority']} is out of range")
    required_payload = ("symptom",) if kind == "finding" else INQUIRY_PAYLOAD_FIELDS
    for key in required_payload:
        value = envelope["payload"].get(key)
        if not isinstance(value, str) or not value:
            raise GapFilingError(f"{kind} payload requires a non-empty {key}")
    recomputed = content_identity(envelope["payload"])
    if recomputed != envelope["id"]:
        raise GapFilingError(f"envelope id {envelope['id']} does not match its payload")


def _stem(identifier: str) -> str:
    return identifier.replace(":", "_")


def _already_known(queue: Path, identifier: str) -> Path | None:
    """The pre-check. `rejected/` and `parked/` count: a gap a human parked must
    not be re-filed by tomorrow's run, and re-detecting it is NOT an error."""
    name = f"{_stem(identifier)}.json"
    for directory in TERMINAL_DIRS:
        candidate = queue / directory / name
        if candidate.exists():
            return candidate
    return None


def _write_new(path: Path, envelope: dict[str, Any]) -> bool:
    """Create ``path`` atomically and exclusively.

    ``os.link`` is the exclusive half: it fails with EEXIST rather than
    clobbering, so a lost race is detected instead of overwriting an artifact
    another writer just created. Returns False when the race was lost.
    """
    fd, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(envelope, stream, indent=2, sort_keys=True, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o644)  # mkstemp is 0600; queue artifacts are readable
        try:
            os.link(temporary, path)
        except FileExistsError:
            return False
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)
    return True


def _append_ledger(queue: Path, event: dict[str, Any]) -> None:
    """Append one event through the queue's shared locked transport."""
    append_event(queue, event)


def _ledger_events(queue: Path) -> list[dict[str, Any]]:
    """Every parseable event in the queue ledger; a missing ledger is no events.

    A torn final line is SKIPPED, never guessed at (CONTRACT.md §5 makes
    tolerating it a reader obligation). Skipping errs toward appending a
    duplicate event, which an append-only log survives; inferring an event that
    may not be there is what leaves an artifact orphaned forever.
    """
    ledger = queue / "ledger.jsonl"
    try:
        raw = ledger.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise GapFilingError(f"queue ledger is unreadable: {ledger}: {exc}") from exc
    events: list[dict[str, Any]] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _has_found_event(events: list[dict[str, Any]], identifier: str) -> bool:
    return any(event.get("event") == "found" and event.get("id") == identifier for event in events)


def _has_terminal_event(events: list[dict[str, Any]], identifier: str) -> bool:
    return any(
        event.get("event") in TERMINAL_EVENTS and event.get("id") == identifier for event in events
    )


def _found_event(
    envelope: dict[str, Any], gap_path: Path, *, repaired: bool = False
) -> dict[str, Any]:
    payload = envelope["payload"]
    detail: dict[str, Any] = {
        "title": envelope["title"],
        "check_id": payload["check_id"],
        "capability": payload["capability"],
        "priority": envelope["priority"],
        "hard_gate": payload["hard_gate"],
        "gap": gap_path.name,
    }
    if repaired:
        # Say so: this event was reconstructed after the artifact was already
        # published, so its `ts` is the repair, not the original detection.
        detail["repaired"] = True
    return {
        "ts": envelope["created_at"],
        "role": PRODUCER_ROLE,
        "event": "found",
        "id": envelope["id"],
        "actor": PRODUCER_ACTOR,
        "detail": detail,
    }


def _resolution_receipt(gap: dict[str, Any]) -> str:
    """The receipt path this resolution cites, or ``"unrecorded"``.

    The LAST evidence ref is the most recently observed run. It is stable for as
    long as the gap stands resolved (``emit_gaps`` only appends a ref when the
    check FAILS again, and that same write demotes the resolution), so hashing
    it into the inquiry identity does not break dedup between two filer passes.
    """
    for reference in reversed(gap.get("evidence_refs") or []):
        if isinstance(reference, dict) and reference.get("receipt"):
            receipt = reference["receipt"]
            if isinstance(receipt, str):
                return receipt
    return "unrecorded"


def _inquiry_envelope(
    target: dict[str, Any], gap: dict[str, Any], gap_path: Path, state: str
) -> dict[str, Any]:
    """An ``inquiry`` asking the OWNING loop to reconcile a finding that passes.

    This producer must not terminalize the item itself. ``completed`` means work
    that was verified AND APPLIED with no merge sha (CONTRACT.md §5); an external
    observer noticing that a symptom disappeared applied nothing, and `parked`
    work is waiting on a human, not on a certification run. Minting `completed`
    from here also raced: the ledger was read and then appended, so two passes
    could write competing terminal events for one id.

    So the resolution is preserved where it belongs — the ``resolved_run`` stamp
    stays on the gap artifact, which is never rewritten here — and the queue gets
    a QUESTION. The reverse edge is deliberately cheap (envelope schema, inquiry
    branch); the loop that owns the finding decides whether it closes.

    The payload is run-STABLE *and* queue-location-STABLE for one resolution, so
    the identity dedups: two passes over the same resolved gap produce the same
    inquiry id, and a later resolution (a different ``resolved_run``) is a
    different question.

    ``state`` is deliberately NOT in the hashed payload. The occurrence can move
    ``findings/`` -> ``parked/`` between two passes while the resolution it is
    about does not change, and hashing the location minted a SECOND question for
    the same resolution the morning after a human parked the item (observed
    ``inquiry_count=2``). Only resolution-stable facts are hashed: which member
    of the gap's identity chain is open, and which run/receipt observed the PASS.
    The location is real information, so it is recorded — in ``northstar_cert``
    (provenance, like every other run-specific field) and in the ledger event.
    """
    payload = target["payload"]
    check_id = payload["check_id"]
    resolved_run = str(gap.get("resolved_run"))
    receipt = _resolution_receipt(gap)
    question = (
        f"North Star certification check {check_id} now PASSES in run {resolved_run} "
        f"(receipt {receipt}), but finding {target['id']} is still open in the queue. "
        f"Please reconcile and close it if the work is genuinely done."
    )
    inquiry_payload = {
        "area": f"{PRODUCER_ACTOR}/{check_id}",
        "observation": (
            f"{PRODUCER_ACTOR} run {resolved_run} recorded {check_id} PASS and stamped "
            f"the gap artifact resolved, while finding {target['id']} "
            f"({payload['capability']}, scope {payload['scope']}) is still open in the queue."
        ),
        "why_not_a_fix": (
            "This producer only READ a certification receipt. It did not apply or "
            "verify a repair, so it may not mint a terminal event for work it did "
            "not do, and it must not close an item that is waiting on a human. "
            "Whether the finding is actually done — and whether the check passing "
            "means the same thing as the breakage being fixed — is the loop's call."
        ),
        "question": question,
        "finding_id": target["id"],
        "check_id": check_id,
        "resolved_run": resolved_run,
        "evidence_refs": [receipt],
        "source": PRODUCER_ACTOR,
        "urgency": "normal",
    }
    envelope = {
        "contract": CONTRACT,
        "id": content_identity(inquiry_payload),
        "kind": "inquiry",
        "title": f"northstar-cert: {check_id} now passes — close finding?"[:TITLE_MAX],
        "created_at": _utcnow(),
        "producer": {"role": PRODUCER_ROLE, "actor": PRODUCER_ACTOR},
        "payload": inquiry_payload,
        # Run-specific and location-specific provenance, kept OUT of the hashed
        # payload: `queue_state` is where the occurrence happened to be sitting
        # when this pass read the queue, which is not part of the question.
        "northstar_cert": {
            "gap": gap_path.name,
            "gap_signature": gap.get("signature"),
            "resolved_at": gap.get("resolved_at"),
            "queue_state": state,
        },
    }
    _validate_envelope(envelope, kind="inquiry")
    return envelope


def _inquired_event(envelope: dict[str, Any], *, repaired: bool = False) -> dict[str, Any]:
    payload = envelope["payload"]
    detail: dict[str, Any] = {
        "reason": payload["question"],
        "check_id": payload["check_id"],
        "finding_id": payload["finding_id"],
        "resolved_run": payload["resolved_run"],
        "queue_state": envelope["northstar_cert"]["queue_state"],
        "gap": envelope["northstar_cert"]["gap"],
    }
    if repaired:
        # Say so, exactly as `_found_event` does: this event was reconstructed
        # after the artifact was already published, so its `ts` is the repair.
        detail["repaired"] = True
    return {
        "ts": envelope["created_at"],
        "role": PRODUCER_ROLE,
        "event": "inquired",
        "id": envelope["id"],
        "actor": PRODUCER_ACTOR,
        "detail": detail,
    }


def _regression_runs(gap: dict[str, Any], path: Path) -> list[str]:
    """The run ids in which this gap regressed, oldest first."""
    history = gap.get("regressions")
    if not isinstance(history, list) or not history:
        return []
    runs = [
        entry["regressed_run"]
        for entry in history
        if isinstance(entry, dict)
        and isinstance(entry.get("regressed_run"), str)
        and entry["regressed_run"]
    ]
    if not runs:
        raise GapFilingError(
            f"gap artifact {path} carries a regressions list with no regressed_run to cite"
        )
    return runs


def _identity_chain(gap: dict[str, Any], path: Path) -> list[dict[str, Any]]:
    """Every identity this gap has ever had, oldest first.

    One gap artifact does not have ONE queue identity, it has a CHAIN of them:
    the original occurrence, then one per resolve→regress cycle. Each regression
    names the occurrence it regressed FROM, which is the previous member of the
    chain — not, as the first repair had it, always the original.

    Keying anything on the original alone is what broke the lifecycle (R1-009): a
    PASS after the first regression looked up the original, found it already
    terminal and closed nothing, and the next real recurrence then collided with
    the still-open first regression and dropped as ``skipped_existing``. The last
    member is always the LIVE occurrence; every earlier member is history that
    resolution still has to be able to find.

    The chain is stable between runs: ``regressions`` only grows when a resolved
    gap is observed failing again, so the daily re-read produces the same ids.
    """
    chain = [build_finding(gap)]
    for run in _regression_runs(gap, path):
        chain.append(build_finding(gap, regression_of=chain[-1]["id"], regression_run=run))
    return chain


def _latest_reconcilable(
    queue: Path, chain: list[dict[str, Any]]
) -> tuple[dict[str, Any] | None, str]:
    """The newest chain member still sitting in ``findings/`` or ``parked/``.

    Newest first, because that is the occurrence a resolution is about; an older
    member may legitimately have been closed cycles ago. ``(None, "")`` when the
    whole chain is absent or already terminal.
    """
    for envelope in reversed(chain):
        known = _already_known(queue, envelope["id"])
        if known is not None and known.parent.name in RECONCILABLE_DIRS:
            return envelope, known.parent.name
    return None, ""


def _has_inquired_event(events: list[dict[str, Any]], identifier: str) -> bool:
    return any(
        event.get("event") == "inquired" and event.get("id") == identifier for event in events
    )


def _file_inquiry(queue: Path, envelope: dict[str, Any], events: list[dict[str, Any]]) -> str:
    """Publish one inquiry, exclusively. One of the ``INQUIRY_*`` outcomes.

    The artifact is the arbiter (``os.link`` fails EEXIST), not a read of the
    ledger: a read-then-append dedup is the race that made the previous design
    able to write two competing events for one resolution. The ledger read is a
    second SUPPRESSOR only — it covers a question already asked whose artifact
    the owning loop has since archived — and it can never cause a duplicate.

    Repair-on-read, mirroring the finding path: publish-then-append is two
    writes, so a crash between them leaves an inquiry artifact whose ``inquired``
    event never landed. Treating the artifact alone as "already asked" made that
    orphan permanent — the caller saw the favourable-looking ``inquiries_filed=0``
    while every ledger-derived view of the queue was missing the question. The
    missing event is APPENDED instead, and counted separately from a fresh ask.
    """
    directory = queue / INQUIRY_DIR
    name = f"{_stem(envelope['id'])}.json"
    for candidate in INQUIRY_DIRS:
        if (queue / candidate / name).exists():
            if not _has_inquired_event(events, envelope["id"]):
                _append_ledger(queue, _inquired_event(envelope, repaired=True))
                return INQUIRY_REPAIRED
            return INQUIRY_SKIPPED
    if _has_inquired_event(events, envelope["id"]):
        return INQUIRY_SKIPPED
    # `inquiries/` is not in the queue contract's must-exist set for this filer
    # (`_require_queue` pins the three directories a FINDING can already live
    # in), so a queue that has never carried one still gets its question.
    directory.mkdir(parents=True, exist_ok=True)
    if not _write_new(directory / name, envelope):
        # Lost the race: the writer that won owns the event for this id.
        return INQUIRY_SKIPPED
    _append_ledger(queue, _inquired_event(envelope))
    return INQUIRY_FILED


def file_gap_findings(
    *,
    gaps_dir: Path,
    queue: Path = DEFAULT_QUEUE,
    live: bool = False,
) -> FilingResult:
    """File every open gap in ``gaps_dir`` as a queue finding.

    ``live=False`` (the default) reads everything and writes nothing.

    ``live=True`` is refused unless ``NSCERT_GAPS_LIVE=1`` is in the environment.
    The check lives HERE, at the write boundary, not only in the CLI: a two-key
    arming that any in-process caller can bypass with one key is one key.
    """
    if live and os.environ.get(LIVE_ENV_FLAG) != "1":
        raise GapFilingNotArmed(
            f"live filing refused: {LIVE_ENV_FLAG}=1 is not set (two-key arming)"
        )
    _require_queue(queue)
    if not gaps_dir.is_dir():
        raise GapFilingError(f"gaps directory is not a directory: {gaps_dir}")
    result = FilingResult(live=live, queue=queue, gaps_dir=gaps_dir)
    for path in sorted(gaps_dir.glob("*.json")):
        gap = _read_gap(path, live=live)
        result.gaps_read += 1
        chain = _identity_chain(gap, path)
        if gap.get("resolved_run"):
            # The check passed again and the emitter stamped it closed. Filing it
            # would put a fixed breakage back on the loop's desk — but the queue
            # must not keep showing, with no annotation at all, a breakage that
            # certification has watched pass. So the newest occurrence still on
            # the desk gets a QUESTION, not a terminal event this producer has no
            # standing to write.
            result.skipped_resolved += 1
            target, state = _latest_reconcilable(queue, chain)
            if live and target is not None:
                events = _ledger_events(queue)
                if not _has_terminal_event(events, target["id"]):
                    outcome = _file_inquiry(
                        queue, _inquiry_envelope(target, gap, path, state), events
                    )
                    if outcome == INQUIRY_FILED:
                        result.inquiries_filed += 1
                    elif outcome == INQUIRY_REPAIRED:
                        result.inquiries_ledger_repaired += 1
            continue
        # The LIVE occurrence is always the last member of the chain: the
        # original when nothing ever regressed, otherwise the newest regression.
        envelope = chain[-1]
        regression = len(chain) > 1
        known = _already_known(queue, envelope["id"])
        if known is not None:
            result.skipped_existing += 1
            if live and known.parent.name == "findings":
                # Already published. Repair-on-read: an artifact whose `found`
                # event never landed (append failed after publish) is an ORPHAN,
                # invisible to every ledger-derived view. Skipping it forever is
                # the favourable-looking outcome; appending the missing event is
                # the honest one.
                if not _has_found_event(_ledger_events(queue), envelope["id"]):
                    _append_ledger(queue, _found_event(envelope, path, repaired=True))
                    result.ledger_repaired += 1
            continue
        if not live:
            result.would_file.append(envelope["id"])
            if regression:
                result.regressions_filed += 1
            continue
        destination = queue / "findings" / f"{_stem(envelope['id'])}.json"
        if not _write_new(destination, envelope):
            result.lost_race += 1
            continue
        _append_ledger(queue, _found_event(envelope, path))
        result.filed.append(envelope["id"])
        if regression:
            result.regressions_filed += 1
    return result


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gaps-dir", type=Path, default=DEFAULT_GAPS_DIR)
    parser.add_argument("--queue", type=Path, default=DEFAULT_QUEUE)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="the default: report what would be filed and write nothing",
    )
    mode.add_argument(
        "--live",
        action="store_true",
        help=f"actually file into the queue. Refused unless {LIVE_ENV_FLAG}=1 is set.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if args.live and os.environ.get(LIVE_ENV_FLAG) != "1":
        # Two-key arming. One key is the flag, the other is the environment, and
        # a scheduled job must not be able to arm itself with the flag alone.
        print(
            json.dumps(
                {
                    "live": False,
                    "error": f"--live refused: {LIVE_ENV_FLAG}=1 is not set",
                }
            )
        )
        return 2
    try:
        result = file_gap_findings(gaps_dir=args.gaps_dir, queue=args.queue, live=args.live)
    except GapFilingError as exc:
        print(json.dumps({"live": args.live, "error": str(exc)}))
        return 2
    print(json.dumps(result.as_dict()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
