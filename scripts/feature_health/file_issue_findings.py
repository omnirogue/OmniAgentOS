#!/usr/bin/env python3
"""File feature-health diagnostic failures into the loop queue as `kind: finding`.

The feature-health lane (``scripts/feature_health/fh.py``) appends ledger
records per (feature, tier) every run; this adapter turns each UNEXPECTED
failing TEST still standing in the newest record of each record stream into
ONE loop-queue finding, so a red cell in the feature-health grid becomes work
the planning loop can pick up mechanically instead of a grid nobody reads.

Modeled directly on ``scripts/northstar_cert/file_gap_findings.py`` — same
identity discipline, same dedup discipline, same two-key arming, same
repair-on-read — with the scope narrowed to what feature-health actually
needs (no resolution/inquiry lifecycle: fh.py has no "resolved_run" concept,
a fixed cell simply stops producing unexpected failures on its next run and
this filer has nothing to say about it).

**Selection is UNEXPECTED-only, and it is never an instrument reading.** A
ledger record already carries the sidecar/known-issues verdict per failing
test (``failures[].expected``, computed by ``fh.py append`` at write time from
``KNOWN-ISSUES.yaml`` via the sidecar). This filer trusts that pre-computed
flag rather than re-reading the sidecar itself — filing only the failures
where ``expected`` is falsy. A record made ENTIRELY of known-issue failures
files nothing.

A record whose ``status`` is ``"error"`` (missing/unparseable report,
did-not-run, or an explicit ``fh.py abort`` record) is an INSTRUMENT ERROR,
not a product defect: the lane could not measure, so there is nothing to say
about the code. Those records never become envelopes and never emit a
``found`` event; they are counted and named in ``instrument_errors`` in the
result (JSON on stdout, one line each on stderr) so an operator sees a broken
lane as a broken lane. Filing them would put "the JUnit writer died" on the
product backlog, where nothing can ever resolve it.

INCOMPLETE COVERAGE (``missing_paths``: a declared matrix path was not on
disk) is reported the same way and suppresses NOTHING. The run measured less
than it claims to, which an operator must know — but the failures it did
observe are real, and dropping them would turn a missing matrix path into a
way to make a red cell disappear. Such a record files its findings AND emits
an instrument error.

**One finding per failing nodeid.** The hashed identity is
``{feature, tier, kind, nodeid}`` — never the whole companion failure set.
Keying on the set means a run that observes A followed by a run that observes
A+B mints a SECOND artifact that also contains A, and with no supersession
lifecycle both stand forever. Per-nodeid identity makes a grown failure set
file exactly the new nodeids and leaves the standing ones deduped.

**The envelope id is hashed over run-STABLE fields only**, exactly the
``file_gap_findings.py`` discipline: ``sha256:`` over the RFC-8785
canonicalization of ``payload`` and nothing else. ``git_sha``, ``ts``,
``report_path``, the pytest failure MESSAGE and every other field that
changes between two runs observing the *same* breakage live OUTSIDE
``payload`` — in the top-level ``base_sha``/``evidence``/``feature_health``
fields — never inside it. Get this wrong and every run mints a new id for the
same red cell and the queue floods.

**Reduction is per record STREAM, not per (feature, tier).** One cell can
have several independent sub-runs — api_ui/tier3 is the isolated suite, the
live probes AND Playwright — and reducing them all to "newest record for the
cell" lets a clean Playwright append mask a standing live-probe failure.
The stream key mirrors ``fh._latest_per_run`` exactly: (feature, tier, env,
report basename with its run stamp stripped).

**Two-key arming**: a live queue write requires BOTH ``--live`` and
``FH_ISSUES_LIVE=1`` in the environment, checked at the WRITE BOUNDARY
(:func:`file_issue_findings`) so no in-process caller can arm itself with one
key. ``--live`` WITHOUT the environment key is a REFUSAL OF AUTHORITY
(:class:`IssueFilingNotArmed`, exit 2) exactly as ``file_gap_findings.py``
treats it — a caller that asked to write live and silently got a dry-run
"success" cannot tell, from an exit code, that nothing was filed. The
environment key WITHOUT ``--live`` is not a request to write at all: that
stays the ordinary dry run (preview envelopes under
``var/feature-health/findings-dryrun/``, exit 0).

**Divergences from file_gap_findings.py** (deliberate, not oversights):

* ``producer.role`` is ``"external"``, not ``"diagnostics"`` as an early
  brief for this filer named it — ``producer.role`` and the ledger event's
  ``role`` are both closed enums (``planner``/``reviewer``/``implementer``/
  ``external``) in ``pipeline/schema/{envelope,ledger-event}.schema.json``,
  and every other mechanical filer in ``pipeline/bridge/`` (``github_bridge``,
  ``integrity``, ``pr_reconcile``) uses ``external`` for exactly this reason.
  ``actor``/``model``/``lineage`` on ``producer`` carry the more specific
  identity instead (the schema's ``producer.additionalProperties: true``
  allows this).
* Findings here DO carry a top-level ``paths`` (the feature's matrix paths
  from ``configs/feature-health.yaml``) — file_gap_findings.py's findings
  deliberately omit ``paths`` ("the planning loop is what turns one into a
  proposal that does"), but a feature-health red cell already names the exact
  test paths that must change, so carrying them here saves the planning loop
  a lookup and is schema-legal (``paths`` has no ``finding``-only
  restriction).
* No resolution/inquiry/regression lifecycle: fh.py's ledger has no
  "resolved_run" stamp to react to, and no identity chain — a fixed cell just
  stops producing unexpected failures on its next run, so there is nothing
  standing in the queue for a later pass to have any standing over.

Fail-closed: a missing ledger, an unreadable matrix, or an unusable queue is
exit 2 (do not retry this input) — never a degraded write path, and never an
instrument error reported as though it were a finding.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys as _sys
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent

_sys.path.insert(0, str(_REPO_ROOT / "pipeline"))
from bridge.ledger_write import append_event  # noqa: E402

try:  # imported as a package member: `scripts.feature_health.file_issue_findings`
    from scripts.northstar_cert.canonical import content_identity
except ImportError:  # pragma: no cover - exercised when run as a bare script
    # A launchd-style bare-script invocation has no `scripts` package on
    # sys.path (sys.path[0] is this file's own directory) — the same trap
    # file_gap_findings.py works around for its own `.canonical` import.
    _sys.path.insert(0, str(_REPO_ROOT / "scripts" / "northstar_cert"))
    from canonical import content_identity  # type: ignore[no-redef]

# fh.py is not a package member of `scripts.feature_health` (no __init__.py
# there — see the divergence note above) and is also run directly by run.sh,
# so it is loaded by path exactly as scripts/feature_health/system_ledger.py
# already does, rather than duplicating its ledger/matrix parsing here.
_fh_spec = importlib.util.spec_from_file_location("fh", _HERE / "fh.py")
assert _fh_spec and _fh_spec.loader
fh = importlib.util.module_from_spec(_fh_spec)
_fh_spec.loader.exec_module(fh)

DEFAULT_QUEUE = Path("/Users/youruser/OmniAgentOS/var/loopqueue")
LIVE_ENV_FLAG = "FH_ISSUES_LIVE"
PRODUCER_ROLE = "external"
PRODUCER_ACTOR = "feature-health-filer"
PRODUCER_MODEL = "mechanical"
PRODUCER_LINEAGE = "none"
CONTRACT = "v1.1"
TITLE_MAX = 200
# Where an id may already be known. `findings/` is the destination; `rejected/`
# and `parked/` are terminal states a re-detection must not walk back.
TERMINAL_DIRS = ("findings", "rejected", "parked")
KIND_UNEXPECTED_FAILURE = "unexpected_failure"
# How much of a pytest failure message is carried (OUTSIDE the hashed payload).
MESSAGE_MAX = 400


class IssueFilingError(RuntimeError):
    """The input or the queue is not fit to file against. Do not retry unchanged."""


class IssueFilingNotArmed(IssueFilingError):
    """``live=True`` was requested without ``FH_ISSUES_LIVE=1`` in the environment.

    A distinct type because this is a REFUSAL OF AUTHORITY, not a bad input:
    the caller asked for a live write it is not armed to make. It is an
    ``IssueFilingError`` so the CLI still exits 2 (do not retry this input) —
    an unarmed live request that exits 0 is indistinguishable, to every
    ordinary shell caller, from a live pass that filed nothing.
    """


@dataclass
class FilingResult:
    """What one pass did — counted, because "0 filed" has several causes.

    ``requested_live`` is what the caller asked for; ``live`` is whether the
    pass was ARMED (both keys present) and therefore wrote to the real queue.
    Since an unarmed ``--live`` is now refused outright, a RETURNED result has
    them equal; both are still reported so consumers of the JSON keep reading
    the same shape.
    """

    requested_live: bool
    live: bool
    queue: Path
    var_dir: Path
    cells_read: int = 0
    filed: list[str] = field(default_factory=list)
    would_file: list[str] = field(default_factory=list)
    skipped_existing: int = 0
    lost_race: int = 0
    #: a `found` event that never landed for an already-published artifact and
    #: was appended by this pass — repaired, NOT filed (see repair-on-read).
    ledger_repaired: int = 0
    #: what the INSTRUMENT has to say: records the lane could not measure
    #: (status error / abort — those file nothing) and records whose coverage
    #: was incomplete (missing_paths — those still file what they observed).
    #: Reported so a broken instrument is visible as a broken instrument.
    instrument_errors: list[dict[str, Any]] = field(default_factory=list)
    shards_read: int = 0
    records_read: int = 0
    #: per-shard read/parse damage, named. Empty when every shard parsed clean.
    shard_errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "requested_live": self.requested_live,
            "live": self.live,
            "queue": str(self.queue),
            "var_dir": str(self.var_dir),
            "cells_read": self.cells_read,
            "filed": self.filed,
            "would_file": self.would_file,
            "skipped_existing": self.skipped_existing,
            "lost_race": self.lost_race,
            "ledger_repaired": self.ledger_repaired,
            "instrument_errors": self.instrument_errors,
            "shards_read": self.shards_read,
            "records_read": self.records_read,
            "shard_errors": self.shard_errors,
        }


def _utcnow() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _valid_sha(value: Any) -> str | None:
    """A well-formed 40-hex commit SHA, or ``None``. Never a placeholder string."""
    if isinstance(value, str) and len(value) == 40 and all(c in "0123456789abcdef" for c in value):
        return value
    return None


def record_provenance_defect(record: dict[str, Any]) -> str | None:
    """Why this record's provenance is unsafe to bind a product finding to,
    or ``None`` when it is safe.

    ``base_sha`` used to come from the FILER's own current checkout at filing
    time (``git -C _REPO_ROOT rev-parse HEAD``), never from the tree that was
    actually measured — a filer running an hour, a day, or a deploy after the
    record was written stamped every finding with a commit that never saw the
    test run. This binds the envelope to the RECORD's own measured SHA
    instead, and refuses when that binding is not provably safe.

    A record with no ``provenance`` key at all (every record written before
    this contract existed) is unsafe by construction — absence must never
    read as clean, which is exactly the defect this function exists to close.
    """
    provenance = record.get("provenance")
    if not isinstance(provenance, dict):
        return "missing_provenance"
    if not provenance.get("eligible"):
        return str(provenance.get("ineligible_reason") or "ineligible")
    sha = _valid_sha(record.get("git_sha"))
    if sha is None:
        return "invalid_git_sha"
    end = provenance.get("end")
    if not isinstance(end, dict) or _valid_sha(end.get("sha")) != sha:
        return "provenance_sha_mismatch"
    return None


def _require_queue(queue: Path) -> None:
    """Refuse anything that is not a writable loop queue.

    Checked in dry-run too: a dry run that cannot read `findings/`/`rejected/`/
    `parked/` cannot honestly say what it WOULD skip.
    """
    if not queue.is_dir():
        raise IssueFilingError(f"queue root is not a directory: {queue}")
    for name in TERMINAL_DIRS:
        directory = queue / name
        if not directory.is_dir():
            raise IssueFilingError(f"queue is missing {name}/: {directory}")
    if not os.access(queue, os.W_OK | os.X_OK):
        raise IssueFilingError(f"queue root is not writable (ledger append): {queue}")
    if not os.access(queue / "findings", os.W_OK | os.X_OK):
        raise IssueFilingError(f"queue findings/ is not writable: {queue / 'findings'}")
    ledger = queue / "ledger.jsonl"
    if ledger.exists() and not os.access(ledger, os.W_OK):
        raise IssueFilingError(f"queue ledger is not writable: {ledger}")


def _require_ledger(var_dir: Path) -> list[Path]:
    """Refuse a missing/empty feature-health ledger. Returns the shard list."""
    if not var_dir.is_dir():
        raise IssueFilingError(f"feature-health ledger directory is missing: {var_dir}")
    shards = sorted(var_dir.glob("ledger-*.jsonl"))
    if not shards:
        raise IssueFilingError(f"no feature-health ledger shards found in {var_dir}")
    return shards


def _load_matrix() -> dict[str, dict[str, object]]:
    try:
        return fh.load_matrix()
    except (OSError, yaml.YAMLError) as exc:
        raise IssueFilingError(f"feature-health matrix is unreadable: {exc}") from exc


@dataclass
class LedgerHealth:
    """What reading the shards actually cost — damage is counted, never swallowed."""

    shards: int = 0
    records: int = 0
    errors: list[str] = field(default_factory=list)


#: The statuses ``fh.py`` ever writes. Anything else is a record this filer does
#: not understand, and a record it does not understand is damage, not data.
KNOWN_STATUSES = ("ok", "error")


def record_defect(value: dict[str, Any]) -> str | None:
    """Why this ``feature-health.v1``-tagged record is unusable, or ``None`` if it is fine.

    The schema TAG is not the schema. A line can be valid JSON, carry
    ``"schema": "feature-health.v1"`` and still be semantically empty — no
    feature, no tier, no timestamp — and accepting it produces a `cells_read`
    that counts nothing and an exit 0 that means nothing. Everything this
    module later reduces, sorts or renders on is required HERE.
    """
    feature = value.get("feature")
    if not isinstance(feature, str) or not feature.strip():
        return "no feature"
    tier = value.get("tier")
    if tier not in fh.TIERS:
        return f"tier {tier!r} is not one of {list(fh.TIERS)}"
    ts = value.get("ts")
    if not isinstance(ts, str) or fh._parse_ts(ts) is None:
        return f"unparseable ts {ts!r}"
    status = value.get("status")
    if status not in KNOWN_STATUSES:
        return f"status {status!r} is not one of {list(KNOWN_STATUSES)}"
    return _evidence_defect(value)


COUNT_FIELDS = ("passed", "failed", "errors", "skipped")


def _is_count(value: Any) -> bool:
    """A usable count: a non-negative int, and NOT a bool.

    ``"3"``, ``3.0``, ``None`` and ``True`` are all things a broken writer
    produces; every one of them would be read as a count by a duck-typed check
    and then compared, summed or rendered as if it were a measurement.
    """
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _evidence_defect(value: dict[str, Any]) -> str | None:
    """Whether this record can say anything about tests at all, and say it honestly.

    A record is gradable when it carries at least one usable count or a usable
    ``failures`` list. Every count that IS present must be usable, and a
    ``failures`` that IS present must be a list of objects — a malformed field
    is damage, not a reason to fall back on the other one, because falling back
    is exactly how a broken writer's record reads as a clean cell.
    """
    for field_name in COUNT_FIELDS:
        if field_name in value and not _is_count(value[field_name]):
            return f"{field_name} {value[field_name]!r} is not a non-negative int"
    failures = value.get("failures")
    if "failures" in value:
        if not isinstance(failures, list):
            return f"failures {type(failures).__name__} is not a list"
        if any(not isinstance(item, dict) for item in failures):
            return "failures contains a non-object entry"
    has_count = any(field_name in value for field_name in COUNT_FIELDS)
    if has_count:
        return None
    # No counts at all: a failures LIST is still evidence, but only a non-empty
    # one. An empty list with no counts cannot distinguish "nothing failed"
    # from "nothing ran", and the favourable reading of that ambiguity is the
    # bug this check exists to stop.
    if isinstance(failures, list) and failures:
        return None
    return "no counts and no non-empty failures list"


def _read_records(shards: list[Path]) -> tuple[list[tuple[dict[str, Any], Path]], LedgerHealth]:
    """Every ``feature-health.v1`` record across ``shards``, paired with its shard.

    Re-implements ``fh._iter_ledger_records`` rather than importing it as-is,
    because that helper does not retain which shard a record came from and
    this filer needs the shard PATH for evidence — fh.py is not owned by this
    module and must not be edited to expose it.

    A torn/unparseable line is skipped (house decoder rule — an append-only log
    read under a concurrent writer always may have one), but it is COUNTED and
    NAMED, and a shard set that yields no records at all is a refusal in
    :func:`file_issue_findings`, never a healthy zero.
    """
    records: list[tuple[dict[str, Any], Path]] = []
    health = LedgerHealth(shards=len(shards))
    for shard in shards:
        try:
            lines = shard.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            health.errors.append(f"{shard.name}: unreadable ({exc})")
            continue
        bad = 0
        kept = 0
        defects: list[str] = []
        for line in lines:
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                bad += 1
                continue
            if not (isinstance(value, dict) and value.get("schema") == fh.SCHEMA):
                continue  # some other schema sharing the shard: not ours, not damage
            defect = record_defect(value)
            if defect is not None:
                # Tagged as ours and unusable: that IS damage. Silently dropping
                # it is how a semantically empty shard reads as a healthy zero.
                defects.append(defect)
                continue
            records.append((value, shard))
            kept += 1
        if bad:
            health.errors.append(f"{shard.name}: {bad} unparseable line(s)")
        if defects:
            health.errors.append(
                f"{shard.name}: {len(defects)} invalid {fh.SCHEMA} record(s) "
                f"({'; '.join(sorted(set(defects))[:3])})"
            )
        if kept == 0 and not bad and not defects:
            health.errors.append(f"{shard.name}: no {fh.SCHEMA} records")
    health.records = len(records)
    return records, health


def _latest_per_stream(
    records: list[tuple[dict[str, Any], Path]],
) -> dict[tuple[str, str, str, str], tuple[dict[str, Any], Path]]:
    """Newest record per :func:`fh.stream_key` — the ONE reduction, from fh.py.

    This module cannot call ``fh.latest_per_stream()`` directly (it needs each
    record paired with the SHARD it came from, for evidence, and it validates
    records as it reads), so it reuses fh.py's KEY rather than re-deriving it:
    the reduction that masks sibling streams must have exactly one definition.
    (">=" so the later record in file order wins a tie, as fh.py does.)
    """
    latest: dict[tuple[str, str, str, str], tuple[dict[str, Any], Path]] = {}
    for record, shard in records:
        key = fh.stream_key(record)
        prior = latest.get(key)
        if prior is None or str(record.get("ts") or "") >= str(prior[0].get("ts") or ""):
            latest[key] = (record, shard)
    return latest


def is_unmeasurable(record: dict[str, Any]) -> bool:
    """True when the lane produced NO usable measurement at all.

    ``status == "error"`` covers every did-not-run shape fh.py writes —
    missing/unparseable report, zero observed testcases, an explicit ``fh.py
    abort`` record, and a run the caller stamped ``--incomplete``. Such a
    record's counts are not evidence of anything, so nothing may be filed from
    it.
    """
    return str(record.get("status")) == "error" or bool(record.get("aborted"))


def has_incomplete_coverage(record: dict[str, Any]) -> bool:
    """True when the run measured only PART of the cell it claims to cover.

    ``missing_paths`` means some declared matrix path was not on disk, so the
    tests that ran are a subset nobody chose. This is worth reporting loudly —
    but it must NOT suppress the failures the run did observe: an incomplete
    measurement that found real red is still real red, and swallowing it would
    make partial coverage a way to hide breakage.
    """
    return bool(record.get("missing_paths"))


def is_instrument_error(record: dict[str, Any]) -> bool:
    """True when this record has something to say about the INSTRUMENT.

    Both classes are reported as instrument errors; only
    :func:`is_unmeasurable` suppresses filing (see the filing loop).
    """
    return is_unmeasurable(record) or has_incomplete_coverage(record)


def instrument_error(
    feature: str, tier: str, record: dict[str, Any], shard: Path
) -> dict[str, Any]:
    """The operator-facing description of one unmeasurable cell. Never an envelope."""
    aborted = bool(record.get("aborted"))
    missing = sorted(str(p) for p in (record.get("missing_paths") or []))
    if aborted:
        reason = "aborted"
    elif record.get("did_not_run"):
        reason = "did_not_run"
    elif str(record.get("status")) == "error":
        reason = "error"
    else:
        # Counts present, coverage incomplete — the quiet member of the class.
        reason = "missing_paths"
    detail_bits = []
    if record.get("abort_reason"):
        detail_bits.append(f"reason class {record['abort_reason']}")
    if record.get("incomplete"):
        detail_bits.append(f"incomplete: {record['incomplete']}")
    if missing:
        detail_bits.append(f"missing matrix paths: {', '.join(missing)}")
    detail = f" ({'; '.join(detail_bits)})" if detail_bits else ""
    verb = (
        "covered only part of its declared paths"
        if reason == "missing_paths"
        else "produced no usable result"
    )
    return {
        "feature": feature,
        "tier": tier,
        "reason": reason,
        "abort_reason": record.get("abort_reason"),
        "missing_paths": missing,
        "ts": record.get("ts"),
        "report_path": record.get("report_path"),
        "shard": str(shard),
        "message": (
            f"feature-health {tier} for feature {feature} {verb} "
            f"({reason}){detail} — instrument error, not filed"
        ),
    }


def provenance_instrument_error(
    feature: str, tier: str, record: dict[str, Any], shard: Path, defect: str
) -> dict[str, Any]:
    """The operator-facing description of one cell with real failures this
    filer refuses to bind to a commit. Never an envelope.

    Distinct from :func:`instrument_error`: the LANE measured something real
    here (``is_unmeasurable`` is false), but the binding between that
    measurement and a commit SHA is not provably safe, so filing would send
    the next agent to debug a tree that was never actually measured.
    """
    return {
        "feature": feature,
        "tier": tier,
        "reason": f"unsafe_provenance:{defect}",
        "abort_reason": None,
        "missing_paths": [],
        "ts": record.get("ts"),
        "report_path": record.get("report_path"),
        "shard": str(shard),
        "message": (
            f"feature-health {tier} for feature {feature} has real failures but unsafe "
            f"provenance ({defect}) — the tree that produced this evidence cannot be "
            "bound to a commit, so no finding was filed. Instrument error, not filed."
        ),
    }


def unexpected_nodeids(record: dict[str, Any]) -> list[str]:
    """Sorted nodeids of the failures this record marks as NOT known issues."""
    failures = record.get("failures") or []
    return sorted(
        str(item.get("nodeid") or "unknown-nodeid")
        for item in failures
        if isinstance(item, dict) and not item.get("expected")
    )


def _failure_message(record: dict[str, Any], nodeid: str) -> str | None:
    for item in record.get("failures") or []:
        if isinstance(item, dict) and str(item.get("nodeid") or "") == nodeid:
            message = item.get("message")
            if isinstance(message, str) and message:
                return message[:MESSAGE_MAX]
    return None


def build_payload(feature: str, tier: str, nodeid: str) -> dict[str, Any]:
    """The hashed body — and therefore the dedup identity of ONE failing test.

    EVERY field here must be identical across two runs that observe the same
    breakage. Nothing derived from a clock, a git sha, a report path, a
    host-load reading — and deliberately not the pytest failure MESSAGE
    either: messages carry values, timings and addresses that move run to run,
    so hashing one would mint a fresh id for a breakage that never changed.
    The message rides on the envelope instead (``feature_health.message``).

    ``failing_tests`` is the single-element list form of ``nodeid``, kept so
    every consumer that reads the finding's failing-test set keeps working
    across the per-set -> per-nodeid change. Both are run-stable.
    """
    symptom = (
        f"feature-health {tier} for feature {feature} has an unexpected failing test: {nodeid}"
    )
    return {
        "symptom": symptom,
        "source": PRODUCER_ACTOR,
        "source_ref": f"feature-health/{feature}/{tier}",
        "feature": feature,
        "tier": tier,
        "kind": KIND_UNEXPECTED_FAILURE,
        "nodeid": nodeid,
        "failing_tests": [nodeid],
    }


def _priority(kind: str) -> int:
    return 2 if kind == KIND_UNEXPECTED_FAILURE else 3


def _title(feature: str, tier: str, nodeid: str) -> str:
    """Names the cell and the nodeid TAIL — the file::test, not the whole path."""
    tail = nodeid.rsplit("/", 1)[-1]
    return f"feature-health: {feature}/{tier} — unexpected failure {tail}"[:TITLE_MAX]


def _evidence(record: dict[str, Any], shard: Path, nodeid: str) -> list[dict[str, Any]]:
    """One entry for the ledger shard, one for the cited junit/playwright report.

    ``verified_by: reading`` is the honest value: this adapter read a recorded
    ledger entry, it did not execute the tests. The shard was just read to
    build this envelope (always present); the report path is stat-ed
    separately, since it may since have been cleaned up.
    """
    entries: list[dict[str, Any]] = [
        {
            "claim": (
                f"feature-health ledger shard {shard.name} recorded {nodeid} as an "
                f"unexpected {record.get('tier')} failure for feature "
                f"{record.get('feature')} at {record.get('ts')}"
            ),
            "verified_by": "reading",
            "receipt": str(shard),
            "result": "receipt-present",
        }
    ]
    report_path = record.get("report_path")
    if isinstance(report_path, str) and report_path:
        candidate = Path(report_path)
        if not candidate.is_absolute():
            candidate = _REPO_ROOT / candidate
        entries.append(
            {
                "claim": f"junit/playwright report cited by the ledger record: {report_path}",
                "verified_by": "reading",
                "receipt": report_path,
                "result": "receipt-present" if candidate.is_file() else "receipt-missing",
            }
        )
    return entries


def _validate_envelope(envelope: dict[str, Any]) -> None:
    """The checks the schema would make, made here because the schema lives in
    another repository (`~/.omniagentos/ops/ThreeLoops`) this tool must not import."""
    for key in ("id", "kind", "title", "created_at", "producer", "payload"):
        if key not in envelope:
            raise IssueFilingError(f"envelope is missing required field {key!r}")
    if envelope["kind"] != "finding":
        raise IssueFilingError(f"envelope kind must be 'finding', got {envelope['kind']!r}")
    if not 1 <= len(envelope["title"]) <= TITLE_MAX:
        raise IssueFilingError(f"envelope title length {len(envelope['title'])} is out of range")
    if "priority" in envelope and not 0 <= envelope["priority"] <= 3:
        raise IssueFilingError(f"envelope priority {envelope['priority']} is out of range")
    symptom = envelope["payload"].get("symptom")
    if not isinstance(symptom, str) or not symptom:
        raise IssueFilingError("finding payload requires a non-empty symptom")
    recomputed = content_identity(envelope["payload"])
    if recomputed != envelope["id"]:
        raise IssueFilingError(f"envelope id {envelope['id']} does not match its payload")


def build_finding(
    feature: str,
    tier: str,
    record: dict[str, Any],
    shard: Path,
    matrix: dict[str, dict[str, object]],
    nodeid: str,
    *,
    created_at: str | None = None,
    base_sha: str | None = None,
) -> dict[str, Any]:
    """A v1.1 finding envelope for ONE failing nodeid in one cell.

    Pure given ``created_at``/``base_sha``: same record + nodeid in, same id
    out. Callers that want the whole record's set use :func:`build_findings`.
    """
    payload = build_payload(feature, tier, nodeid)
    envelope: dict[str, Any] = {
        "contract": CONTRACT,
        "id": content_identity(payload),
        "kind": "finding",
        "title": _title(feature, tier, nodeid),
        "created_at": created_at or _utcnow(),
        "priority": _priority(KIND_UNEXPECTED_FAILURE),
        "producer": {
            "role": PRODUCER_ROLE,
            "actor": PRODUCER_ACTOR,
            "model": PRODUCER_MODEL,
            "lineage": PRODUCER_LINEAGE,
        },
        "paths": list(matrix.get(feature, {}).get(tier) or []),
        "evidence": _evidence(record, shard, nodeid),
        "payload": payload,
        # Run-specific provenance, kept OUT of the payload on purpose: none of
        # it may participate in the identity, or dedup breaks on the next run.
        "feature_health": {
            "feature": feature,
            "tier": tier,
            "ts": record.get("ts"),
            "git_sha": record.get("git_sha"),
            "git_dirty": record.get("git_dirty"),
            "report_path": record.get("report_path"),
            "runner": record.get("runner"),
            "env": record.get("env"),
            "message": _failure_message(record, nodeid),
        },
    }
    if base_sha:
        envelope["base_sha"] = base_sha
    _validate_envelope(envelope)
    return envelope


def build_findings(
    feature: str,
    tier: str,
    record: dict[str, Any],
    shard: Path,
    matrix: dict[str, dict[str, object]],
    *,
    created_at: str | None = None,
    base_sha: str | None = None,
) -> list[dict[str, Any]]:
    """One envelope per unexpected failing nodeid; ``[]`` when there is nothing to file.

    ``[]`` covers three different situations on purpose — a clean record, a
    record made entirely of known issues, and an UNMEASURABLE record (error /
    abort / incomplete run: the caller reports those via
    :func:`instrument_error`; they are never findings).

    A record with ``missing_paths`` is NOT one of them. Its coverage is partial
    and that is reported separately, but the failures it did observe are real
    and get filed — otherwise a missing matrix path becomes a way to make a red
    cell disappear.
    """
    if is_unmeasurable(record):
        return []
    return [
        build_finding(
            feature,
            tier,
            record,
            shard,
            matrix,
            nodeid,
            created_at=created_at,
            base_sha=base_sha,
        )
        for nodeid in unexpected_nodeids(record)
    ]


def _stem(identifier: str) -> str:
    return identifier.replace(":", "_")


def _already_known(queue: Path, identifier: str) -> Path | None:
    """The dedup pre-check. `rejected/` and `parked/` count: a finding a human
    parked must not be re-filed by tomorrow's run."""
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

    A torn final line is SKIPPED, never guessed at. Skipping errs toward
    appending a duplicate event, which an append-only log survives; inferring
    an event that may not be there is what leaves an artifact orphaned forever.
    """
    ledger = queue / "ledger.jsonl"
    try:
        raw = ledger.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise IssueFilingError(f"queue ledger is unreadable: {ledger}: {exc}") from exc
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


def _found_event(envelope: dict[str, Any], *, repaired: bool = False) -> dict[str, Any]:
    payload = envelope["payload"]
    detail: dict[str, Any] = {
        "title": envelope["title"],
        "feature": payload["feature"],
        "tier": payload["tier"],
        "kind": payload["kind"],
        "nodeid": payload["nodeid"],
        "priority": envelope["priority"],
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


def _repair_orphans(queue: Path, result: FilingResult) -> None:
    """Append the missing ``found`` event for every ORPHANED artifact we authored.

    The sweep is over the QUEUE, not over the current ledger records, and that
    is the whole point. An artifact is published before its event is appended,
    so a crash between the two leaves an orphan — and the orphan's cell may go
    green on the very next run, at which point nothing rebuilds that envelope
    and a repair driven by "the findings this pass would file" never looks at
    it again. The artifact itself is the durable evidence that the publish
    happened; its id is its own name.

    Only artifacts whose ``producer.actor`` is this filer are touched: another
    producer's missing event is not this module's to invent. Unreadable or
    unrecognisable artifacts are skipped, never guessed at.
    """
    findings = queue / "findings"
    try:
        candidates = sorted(findings.glob("*.json"))
    except OSError:
        return
    events: list[dict[str, Any]] | None = None
    for path in candidates:
        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(envelope, dict):
            continue
        producer = envelope.get("producer")
        if not isinstance(producer, dict) or producer.get("actor") != PRODUCER_ACTOR:
            continue
        identifier = envelope.get("id")
        if not isinstance(identifier, str) or not identifier:
            continue
        if events is None:  # read once, and only if we own something here
            events = _ledger_events(queue)
        if _has_found_event(events, identifier):
            continue
        try:
            event = _found_event(envelope, repaired=True)
        except KeyError:
            # An artifact we cannot describe is one we cannot honestly announce.
            continue
        _append_ledger(queue, event)
        events.append(event)
        result.ledger_repaired += 1


def _write_dryrun_preview(var_dir: Path, envelope: dict[str, Any]) -> None:
    """Best-effort preview artifact under ``findings-dryrun/`` for a dry run.

    Never raises: a preview write failing must not turn an honest dry run
    into a refusal, and it is never read back by this module (no dedup, no
    ledger event) — it exists purely for a human to inspect what a live pass
    would file.
    """
    try:
        directory = var_dir / "findings-dryrun"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{_stem(envelope['id'])}.json"
        path.write_text(
            json.dumps(envelope, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass


def file_issue_findings(
    *,
    queue: Path = DEFAULT_QUEUE,
    live: bool = False,
) -> FilingResult:
    """File every unexpected feature-health failing test as a queue finding.

    ``live=True`` requires ``FH_ISSUES_LIVE=1`` in the environment — checked
    HERE, at the write boundary, not only in the CLI, so no in-process caller
    can arm a live write with one key. An unarmed live request raises
    :class:`IssueFilingNotArmed` (exit 2 from the CLI): a caller that asked to
    write and got a silent dry run cannot tell, from an exit code, that
    nothing was filed. Not asking for ``--live`` at all is the ordinary dry
    run — preview envelopes under ``var/feature-health/findings-dryrun/``,
    nothing written to the real queue, exit 0.

    Records the lane could not MEASURE (status error / abort) never become
    findings: they land in ``result.instrument_errors``.
    """
    if live and os.environ.get(LIVE_ENV_FLAG) != "1":
        raise IssueFilingNotArmed(
            f"live filing refused: {LIVE_ENV_FLAG}=1 is not set (two-key arming)"
        )
    armed = bool(live)
    _require_queue(queue)
    var_dir = fh.var_dir()
    shards = _require_ledger(var_dir)
    records, health = _read_records(shards)
    if not records:
        detail = "; ".join(health.errors) or "every shard was empty"
        raise IssueFilingError(
            f"no parseable feature-health ledger records in {health.shards} shard(s) "
            f"under {var_dir}: {detail}"
        )
    matrix = _load_matrix()

    result = FilingResult(requested_live=bool(live), live=armed, queue=queue, var_dir=var_dir)
    result.shards_read = health.shards
    result.records_read = health.records
    result.shard_errors = health.errors
    cells = _latest_per_stream(records)
    for key in sorted(cells):
        feature, tier = key[0], key[1]
        record, shard = cells[key]
        result.cells_read += 1
        if is_instrument_error(record):
            # Something is wrong with the INSTRUMENT here: report it by name,
            # always. Filing it as a finding would put "the JUnit writer died"
            # on the product backlog, where nothing can ever resolve it.
            result.instrument_errors.append(instrument_error(feature, tier, record, shard))
        if is_unmeasurable(record):
            # ...and when there is no usable measurement at all, that is the
            # whole story: no envelope, no `found` event. Incomplete COVERAGE
            # (missing_paths) falls through on purpose — the failures it did
            # observe are real, and suppressing them would make a missing
            # matrix path a way to hide a red cell.
            continue
        # The lane measured something real. Before it can become a finding,
        # the record must prove it is bound to the tree it claims — dirty,
        # executable-untracked, missing, or start/end-mutated provenance is
        # reported as its own instrument error and files NOTHING, exactly the
        # unmeasurable path above; the difference is only in WHY.
        defect = record_provenance_defect(record)
        if defect is not None:
            result.instrument_errors.append(
                provenance_instrument_error(feature, tier, record, shard, defect)
            )
            continue
        base_sha = record["git_sha"]
        for envelope in build_findings(feature, tier, record, shard, matrix, base_sha=base_sha):
            known = _already_known(queue, envelope["id"])
            if known is not None:
                # Already filed, parked or rejected. The orphan repair for
                # published artifacts is the queue-wide sweep below, not this
                # branch: an orphan whose cell has since gone green is never
                # rebuilt here, and would be skipped forever.
                result.skipped_existing += 1
                continue
            if not armed:
                result.would_file.append(envelope["id"])
                _write_dryrun_preview(var_dir, envelope)
                continue
            destination = queue / "findings" / f"{_stem(envelope['id'])}.json"
            if not _write_new(destination, envelope):
                result.lost_race += 1
                continue
            _append_ledger(queue, _found_event(envelope))
            result.filed.append(envelope["id"])
    if armed:
        # Repair-on-read, driven by what is PUBLISHED rather than by what this
        # pass would file. A dry run never repairs: it writes nothing.
        _repair_orphans(queue, result)
    return result


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queue", type=Path, default=DEFAULT_QUEUE)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="the default: report what would be filed and write nothing to the queue",
    )
    mode.add_argument(
        "--live",
        action="store_true",
        help=f"actually file into the queue. Refused unless {LIVE_ENV_FLAG}=1 is set.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        result = file_issue_findings(queue=args.queue, live=args.live)
    except IssueFilingError as exc:
        # Two shapes reach here, both exit 2 (do not retry this input): a
        # could-not-run condition (missing/unusable ledger, unreadable matrix,
        # unusable queue), and an unarmed `--live` — a refusal of authority.
        print(json.dumps({"requested_live": args.live, "live": False, "error": str(exc)}))
        return 2
    for entry in result.instrument_errors:
        # stderr as well as the JSON: an instrument error that only exists in a
        # field nobody parses is an instrument error nobody fixes.
        print(f"[feature-health-filer] instrument error: {entry['message']}", file=_sys.stderr)
    for problem in result.shard_errors:
        print(f"[feature-health-filer] ledger shard damage: {problem}", file=_sys.stderr)
    print(json.dumps(result.as_dict()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
