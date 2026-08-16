#!/usr/bin/env python3
"""Emit failed North Star eval rows as deduplicated gap artifacts.

Dry-run by default: gaps land in ``var/northstar-cert/gaps-dryrun`` labelled
``dry_run: true``, and nothing downstream consumes them. ``--live`` (which also
requires ``NSCERT_GAPS_LIVE=1``) writes the same artifacts to the live emit
directory for ``file_gap_findings.py`` to file into the loop queue.

VOID rows (instrument faults: vacuous bindings, pytest ``<error>``, and rows
whose verdict metrics contradict each other or will not decode) are NEVER
emitted as gaps — every one of them is counted into the ONE summary field
``voided`` instead. An instrument error reported as a candidate defect sends the
next agent to debug the wrong thing.

Whenever any row is VOID, this script also names each one on stderr, lists
them in the JSON summary as ``voided_rows``, and exits 2 rather than 0 — a
summary field alone is invisible to every consumer but the exit status. The
normal cadence never reaches this: the recorder aborts with its own rc 70
before emission if ANY check VOIDs. This is for every OTHER caller (manual
runs, recovery, future callers) that can reach emission with a voided row the
recorder never got a chance to gate on. That path is deliberately kept
distinct from rc 70: 70 means nothing could be emitted at all, 2 means
emission otherwise completed but at least one row was an instrument fault.

``--resolve`` closes the loop: a check that PASSES in this run gets its existing
gap artifact stamped ``resolved_run``/``resolved_at``. The file is kept, never
deleted — and if the check fails again the stamp is demoted to a ``regressions``
entry rather than left standing over live breakage.
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import json
import os
import sys
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from omniagentos.lab.db import LabStore

try:  # imported as a package member: `scripts.northstar_cert.emit_gaps`
    from .canonical import canonicalize, content_identity
except ImportError:  # pragma: no cover - exercised by `python scripts/.../emit_gaps.py`
    # The Makefile runs this file as a SCRIPT, so sys.path[0] is this directory
    # and there is no parent package for the relative import to resolve against
    # (the same fallback scripts/*/launchd.py uses). Derive the directory from
    # this file rather than trusting the caller's cwd.
    import sys as _sys

    _sys.path.insert(0, str(Path(__file__).resolve().parent))
    from canonical import canonicalize, content_identity

__all__ = [
    "CheckVerdict",
    "EmittedGaps",
    "GapEmissionError",
    "canonicalize",
    "content_identity",
    "emit_gaps",
    "resolve_gaps",
]

DEFAULT_RESULTS_DB = Path("var/northstar-cert/results.sqlite3")
DEFAULT_OUTPUT_DIR = Path("var/northstar-cert/gaps-dryrun")
DEFAULT_LIVE_OUTPUT_DIR = Path("var/northstar-cert/gaps")
DEFAULT_EVIDENCE_ROOT = Path("var/gate-evidence")
LIVE_ENV_FLAG = "NSCERT_GAPS_LIVE"
_REASON_PREFIX = "__nsc_reason__:"
#: Reason strings carrying this prefix describe the INSTRUMENT, not the product.
#: Any row that decodes to one of them is VOID — counted in the summary's
#: ``voided`` field and never emitted as a gap.
_INSTRUMENT_ERROR_PREFIX = "instrument_error"
_SCHEMA = "omniagentos.northstar-gap-dryrun.v1"
_SCHEMA_LIVE = "omniagentos.northstar-gap.v1"


class CheckVerdict(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    NOT_EVALUABLE = "NOT_EVALUABLE"
    #: The recorder could not MEASURE this check (vacuous binding, pytest
    #: ``<error>``, ...).  It is an instrument fault, never a product defect,
    #: so it is counted and reported but NEVER emitted as a gap.
    VOID = "VOID"


class GapEmissionError(RuntimeError):
    """Eval evidence could not be translated without favorable assumptions."""


class EmittedGaps(list[Path]):
    """The gap artifacts this run wrote, plus the VOID rows it refused to write.

    A plain ``list`` subclass on purpose: every caller (and the review repros)
    treats the return value as the list of emitted paths, and the instrument
    count rides alongside instead of changing that contract.
    """

    def __init__(
        self,
        paths: Iterable[Path] = (),
        *,
        voided: int = 0,
        voided_rows: Iterable[str] = (),
        undescribable: Iterable[str] = (),
    ) -> None:
        super().__init__(paths)
        self.voided = voided
        #: Named ``"<check_id>:<reason>"`` for every VOID row counted above.
        #: The count alone is invisible to every consumer but the JSON reader;
        #: this is what lets `main` name them on stderr and fail closed.
        self.voided_rows = tuple(voided_rows)
        #: Named reasons for rows this run could not describe from their own
        #: suite's case metadata. Counted, never guessed at (see `_read_evals`).
        #: The same fail-closed treatment as ``voided_rows``, for a different
        #: instrument fault class in the same function.
        self.undescribable = tuple(undescribable)


@dataclass(frozen=True)
class FailedEval:
    check_id: str
    verdict: CheckVerdict
    reason: str
    metadata: dict[str, Any]


def _utcnow() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _decode_reason(per_case: dict[str, Any]) -> str:
    encoded = next(
        (key[len(_REASON_PREFIX) :] for key in per_case if key.startswith(_REASON_PREFIX)), ""
    )
    if not encoded:
        return ""
    try:
        return base64.urlsafe_b64decode(encoded.encode("ascii")).decode("utf-8")
    except (ValueError, UnicodeError):
        return "instrument_error:invalid_reason_encoding"


def _row_check_id(row: dict[str, Any], per_case: dict[str, Any]) -> str:
    """The one check id a result row is about, or a row-unique ``unknown:`` marker.

    One definition, used by both the metadata JOIN and the decode, so a row can
    never be looked up under one id and then reported under another.
    """
    check_ids = [key for key in per_case if not key.startswith(_REASON_PREFIX)]
    return check_ids[0] if len(check_ids) == 1 else f"unknown:{row['id']}"


def _row_payloads(row: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        metrics = json.loads(row["metrics_json"])
        per_case = json.loads(row["per_case_json"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise GapEmissionError(f"eval row {row.get('id')} has invalid JSON: {exc}") from exc
    if not isinstance(metrics, dict) or not isinstance(per_case, dict):
        raise GapEmissionError(f"eval row {row.get('id')} has invalid result shapes")
    return metrics, per_case


def _decode_eval(row: dict[str, Any], metadata: dict[str, dict[str, Any]]) -> FailedEval:
    metrics, per_case = _row_payloads(row)
    check_id = _row_check_id(row, per_case)
    flags = tuple(float(metrics.get(name, 0.0)) > 0 for name in ("pass", "fail", "not_evaluable"))
    reason = _decode_reason(per_case)
    if float(metrics.get("void", 0.0)) > 0:
        # The recorder writes VOID as void=1 AND not_evaluable=1 (so three-metric
        # readers still see "not a measurement").  Decode void FIRST: reading the
        # row by its not_evaluable bit alone is exactly how an instrument fault
        # becomes a candidate product defect.
        return FailedEval(
            check_id,
            CheckVerdict.VOID,
            reason or "instrument_error:void_reason_absent",
            metadata.get(check_id, {}),
        )
    if flags == (True, False, False) and bool(row["deterministic_passed"]):
        verdict = CheckVerdict.PASS
    elif flags == (False, True, False):
        verdict = CheckVerdict.FAIL
        reason = reason or "pytest_failure:no_detail_recorded"
    elif flags == (False, False, True):
        verdict = CheckVerdict.NOT_EVALUABLE
        reason = reason or "no_writer_evidence:reason_absent"
    else:
        # Contradictory (pass AND fail), absent, or otherwise undecodable verdict
        # metrics.  The row does not say what happened, so nothing was MEASURED:
        # this is VOID, not NOT_EVALUABLE.  Labelling it `instrument_error` and
        # then decoding it to an emittable verdict is how an instrument fault
        # reached the queue as a candidate product defect (R1-005).
        verdict = CheckVerdict.VOID
        reason = "instrument_error:invalid_or_absent_verdict_metrics"
    if verdict is not CheckVerdict.VOID and reason.startswith(_INSTRUMENT_ERROR_PREFIX):
        # Same rule one level down: a row whose own reason blob would not decode
        # (`instrument_error:invalid_reason_encoding`) is an instrument fault
        # whatever its metric flags happen to say.  VOID it rather than emit a
        # gap — or, for a PASS, rather than let it close somebody else's gap.
        verdict = CheckVerdict.VOID
    return FailedEval(check_id, verdict, reason, metadata.get(check_id, {}))


#: Reason class for a row this reader refuses to describe: its suite identity is
#: absent/empty, or its own suite holds no case row for its check. Named, counted
#: and surfaced — never repaired with a guess.
UNKNOWN_SUITE_REASON = "unknown_suite_identity"


class DecodedEvals(list[FailedEval]):
    """The decodable rows, plus the named reasons for the rows that were not.

    A ``list`` subclass for the same reason :class:`EmittedGaps` is one: every
    caller treats the return value as the list of decoded rows, and the
    instrument detail rides alongside instead of changing that contract.
    """

    def __init__(self, items: Iterable[FailedEval] = (), *, skipped: Iterable[str] = ()) -> None:
        super().__init__(items)
        self.skipped = tuple(skipped)


def _read_evals(db_path: Path, run_id: str) -> DecodedEvals:
    """Every decodable eval row for the run, PASS rows included.

    The resolution pass needs the passes, so the store read and the verdict
    filter are separate: one reader, and exactly one place where a verdict is
    allowed to drop out of the walk (``_read_failed_evals`` below).

    Case metadata is joined per (suite_id, check_id). Eval-case ids are
    suite-scoped (``record_results._ensure_case``), so once two suite versions
    both own a row for one check_id a check_id-only map is decided by database
    traversal order — and the metadata it hands over supplies ``capability``
    and ``scope`` to ``_identity_payload``, i.e. the gap's durable IDENTITY.
    A v1 row must be described by v1's case, always, even in a run that
    legitimately carries rows from both.

    There is deliberately NO global fallback. A row whose suite identity is
    empty/unknown, or whose own suite holds no case for its check, is SKIPPED
    with a named reason rather than described from whatever else the database
    happens to contain: a durable finding id minted from guessed metadata is
    wrong in the one field nobody re-checks. If that leaves nothing decodable,
    the run refuses (it measured nothing this reader can honestly report).
    """
    store = LabStore(str(db_path))
    try:
        rows = store.eval_results(run_id)
        if not rows:
            raise GapEmissionError(f"run {run_id!r} has zero eval_results rows")
        suite_ids = sorted({str(row["suite_id"] or "") for row in rows} - {""})
        metadata: dict[tuple[str, str], dict[str, Any]] = {}
        if suite_ids:
            placeholders = ",".join("?" for _ in suite_ids)
            case_rows = store._connection.execute(
                f"SELECT suite_id, input_json FROM eval_cases WHERE suite_id IN ({placeholders})",
                tuple(suite_ids),
            ).fetchall()
            for case_row in case_rows:
                try:
                    item = json.loads(case_row["input_json"])
                except (TypeError, json.JSONDecodeError):
                    continue
                if isinstance(item, dict) and isinstance(item.get("check_id"), str):
                    metadata[(str(case_row["suite_id"]), item["check_id"])] = item
        decoded: list[FailedEval] = []
        skipped: list[str] = []
        for row in rows:
            suite_id = str(row["suite_id"] or "")
            check_id = _row_check_id(row, _row_payloads(row)[1])
            case = metadata.get((suite_id, check_id))
            if not suite_id or case is None:
                skipped.append(f"{UNKNOWN_SUITE_REASON}:{check_id}")
                continue
            # The per-row map is this row's SUITE's view and nothing else.
            decoded.append(_decode_eval(row, {check_id: case}))
    finally:
        store._store.close()
    if skipped:
        print(
            f"[emit-gaps] run {run_id}: skipped {len(skipped)} eval row(s) with no case "
            f"metadata in their own suite ({', '.join(sorted(set(skipped)))})",
            file=sys.stderr,
        )
    if not decoded:
        raise GapEmissionError(
            f"run {run_id!r} has no eval_results row with usable suite-scoped case metadata "
            f"({', '.join(sorted(set(skipped))) or 'no rows decoded'})"
        )
    return DecodedEvals(decoded, skipped=sorted(skipped))


def _read_failed_evals(
    db_path: Path, run_id: str
) -> tuple[list[FailedEval], int, tuple[str, ...], tuple[str, ...]]:
    """The emittable rows, plus every instrument fault this run must fail closed on.

    Two independent instrument-fault classes flow through the ONE decode here,
    and both must reach the exit code — a summary field alone is invisible:

    * VOID rows (theirs, proposal 3fad877c): PASS is not a gap and VOID is not a
      MEASUREMENT, so a VOID row is counted and NAMED, never emitted. This used
      to be split into a separate ``_voided_row_names`` re-decode to keep this
      function's return shape byte-stable for the counterfeit corpus; that
      corpus is re-rebased against this reconciled shape, so the second decode
      is gone and both fault classes ride one read.
    * UNDESCRIBABLE rows (this lane): a row whose own suite holds no case for its
      check is skipped by ``_read_evals`` rather than described from another
      suite's metadata (which would mint a wrong durable gap id); its named
      reason rides along too.
    """
    decoded = _read_evals(db_path, run_id)
    voided_items = [item for item in decoded if item.verdict is CheckVerdict.VOID]
    voided_rows = tuple(f"{item.check_id}:{item.reason}" for item in voided_items)
    emittable = [
        item
        for item in decoded
        if item.verdict is not CheckVerdict.PASS and item.verdict is not CheckVerdict.VOID
    ]
    return emittable, len(voided_items), voided_rows, getattr(decoded, "skipped", ())


def _cause_class(item: FailedEval) -> str:
    if item.verdict is CheckVerdict.FAIL:
        return "broken"
    if item.reason.startswith(("no_writer_evidence", "precondition_missing:writer:")):
        return "missing-wiring"
    if item.reason.startswith(("precondition_missing", "instrument_error")):
        return "missing-test-tool-skill-context-permission"
    return "orchestration-integration-review"


def _identity_payload(item: FailedEval, project: str) -> dict[str, Any]:
    metadata = item.metadata
    reason_parts = item.reason.split(":")
    reason_class = (
        reason_parts[0] if reason_parts[0] == "pytest_failure" else ":".join(reason_parts[:2])
    )
    return {
        "check_id": item.check_id,
        "capability": metadata.get("capability", "unknown"),
        "project": project,
        "scope": metadata.get("scope", "unknown"),
        "verdict": item.verdict.value,
        "reason_class": reason_class,
    }


def _new_gap(
    item: FailedEval,
    *,
    signature: str,
    run_id: str,
    project: str,
    evidence_root: Path,
    bundle_path: Path,
    dry_run: bool = True,
) -> dict[str, Any]:
    metadata = item.metadata
    binding = metadata.get("binding") if isinstance(metadata.get("binding"), dict) else {}
    target = str(binding.get("target") or "unknown")
    gate = bool(metadata.get("gate"))
    cause_class = _cause_class(item)
    evidence = {
        "run_id": run_id,
        "receipt": str(evidence_root / "records" / "northstar-cert" / f"{run_id}.json"),
        "bundle": str(bundle_path),
    }
    return {
        "schema": _SCHEMA if dry_run else _SCHEMA_LIVE,
        "dry_run": dry_run,
        "gap_id": "nsgap-" + signature.split(":", 1)[1][:24],
        "signature": signature,
        "identity_payload": _identity_payload(item, project),
        "check_id": item.check_id,
        "capability": metadata.get("capability", "unknown"),
        "project": project,
        "scenario": target,
        "expected": "PASS",
        "actual": f"{item.verdict.value}({item.reason})",
        "evidence_refs": [evidence],
        "severity": "critical" if gate else "high",
        "hard_gate": gate,
        "frequency": 1,
        "quality_impact": "certification requirement unsatisfied",
        "speed_impact": "unknown",
        "root_cause_hypothesis": cause_class,
        "confidence": 0.5,
        "cause_class": cause_class,
        "existing_component_pointer": target,
        "reuse_action": "inspect and repair the existing bound component",
        "recommended_next_step": f"review evidence for {item.check_id} before planning a fix",
        "first_seen_run": run_id,
        "latest_run": run_id,
        "observed_runs": [run_id],
    }


def _atomic_replace(path: Path, payload: dict[str, Any]) -> None:
    fd, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def _upsert_gap(path: Path, proposed: dict[str, Any], run_id: str) -> None:
    lock_path = path.with_name(f".{path.name}.lock")
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise GapEmissionError(f"existing gap {path} is unreadable: {exc}") from exc
            if not isinstance(existing, dict) or existing.get("signature") != proposed["signature"]:
                raise GapEmissionError(f"existing gap identity mismatch at {path}")
            observed = existing.get("observed_runs")
            if not isinstance(observed, list):
                raise GapEmissionError(f"existing gap has invalid observed_runs at {path}")
            if run_id not in observed:
                existing["frequency"] = int(existing.get("frequency", 0)) + 1
                existing["latest_run"] = run_id
                existing["actual"] = proposed["actual"]
                existing["evidence_refs"] = [
                    *existing.get("evidence_refs", []),
                    *proposed["evidence_refs"],
                ]
                existing["observed_runs"] = [*observed, run_id]
                _clear_resolution(existing, run_id)
            # The mode label always follows the pipeline that last wrote the
            # artifact: a live emit must never inherit a stale `dry_run: true`
            # from a dry-run file that happens to share the identity.
            existing["schema"] = proposed["schema"]
            existing["dry_run"] = proposed["dry_run"]
            proposed = existing
        _atomic_replace(path, proposed)


def _clear_resolution(existing: dict[str, Any], run_id: str) -> None:
    """A resolved gap seen failing again is a REGRESSION, not a resolved gap.

    Leaving ``resolved_run`` on an artifact whose check just failed again is the
    favourable-absence shape this estate loses to: the loop would read the gap
    as closed while the certification run says it is open. The resolution is not
    deleted, it is demoted into ``regressions`` so the history survives.
    """
    if "resolved_run" not in existing:
        return
    history = existing.get("regressions")
    if not isinstance(history, list):
        history = []
    history.append(
        {
            "resolved_run": existing.pop("resolved_run"),
            "resolved_at": existing.pop("resolved_at", None),
            "regressed_run": run_id,
            "regressed_at": _utcnow(),
        }
    )
    existing["regressions"] = history


def emit_gaps(
    *,
    db_path: Path,
    run_id: str,
    output_dir: Path,
    evidence_root: Path,
    bundle_path: Path,
    project: str = "estate",
    dry_run: bool = True,
) -> EmittedGaps:
    failures, voided, voided_rows, undescribable = _read_failed_evals(db_path, run_id)
    output_dir.mkdir(parents=True, exist_ok=True)
    emitted = EmittedGaps(voided=voided, voided_rows=voided_rows, undescribable=undescribable)
    for item in failures:
        identity = _identity_payload(item, project)
        signature = content_identity(identity)
        path = output_dir / f"{signature.replace(':', '_')}.json"
        proposed = _new_gap(
            item,
            signature=signature,
            run_id=run_id,
            project=project,
            evidence_root=evidence_root,
            bundle_path=bundle_path,
            dry_run=dry_run,
        )
        _upsert_gap(path, proposed, run_id)
        emitted.append(path)
    return emitted


def resolve_gaps(
    *,
    db_path: Path,
    run_id: str,
    output_dir: Path,
    project: str = "estate",
) -> list[Path]:
    """Stamp gap artifacts whose check PASSED in this run as resolved.

    This is the close-the-loop half the pipeline was missing: without it a gap
    file, once written, stays open forever and nothing downstream can tell a
    live breakage from one that was fixed three weeks ago.

    The artifact is KEPT — resolution is a stamp, never a delete, because the
    gap's history is the only record that the certification ever caught this.
    Matching is on ``check_id`` + ``project``, NOT on the gap signature: the
    signature hashes the verdict and reason class, so a passing run can never
    reproduce the failing run's signature.
    """
    if not output_dir.is_dir():
        return []
    decoded = _read_evals(db_path, run_id)
    # A check with ANY row this run could not describe is EXCLUDED from
    # resolution — it can neither be stamped resolved nor contribute to one.
    # The skipped row may itself be the genuine FAIL, and a false `resolved`
    # stamped on a real red is the worst outcome this tool can produce: the
    # gap stops being reported and nobody looks again. Partial evidence closes
    # nothing.
    excluded = {
        reason.split(":", 1)[1]: reason
        for reason in getattr(decoded, "skipped", ())
        if ":" in reason
    }
    passed = {
        item.check_id
        for item in decoded
        if item.verdict is CheckVerdict.PASS and item.check_id not in excluded
    }
    for check_id, reason in sorted(excluded.items()):
        print(
            f"[emit-gaps] run {run_id}: {check_id} excluded from resolution ({reason})",
            file=sys.stderr,
        )
    if not passed:
        return []
    resolved_at = _utcnow()
    resolved: list[Path] = []
    for path in sorted(output_dir.glob("*.json")):
        lock_path = path.with_name(f".{path.name}.lock")
        with lock_path.open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            if not path.exists():
                continue
            try:
                gap = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise GapEmissionError(f"existing gap {path} is unreadable: {exc}") from exc
            if not isinstance(gap, dict):
                raise GapEmissionError(f"existing gap {path} is not an object")
            if gap.get("check_id") not in passed or gap.get("project") != project:
                continue
            if run_id in (gap.get("observed_runs") or []):
                # The same run reported this check both failing and passing.
                # That is an instrument fault, not a resolution; refuse rather
                # than stamp a gap closed on contradictory evidence.
                raise GapEmissionError(
                    f"run {run_id!r} reports {gap.get('check_id')!r} as both failing and passing"
                )
            if "resolved_run" in gap:
                continue  # already closed by an earlier passing run; keep the first
            gap["resolved_run"] = run_id
            gap["resolved_at"] = resolved_at
            _atomic_replace(path, gap)
            resolved.append(path)
    return resolved


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--db", type=Path, default=DEFAULT_RESULTS_DB)
    parser.add_argument("--evidence-root", type=Path, default=DEFAULT_EVIDENCE_ROOT)
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--project", default="estate")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help=(
            f"where gap artifacts are written (default {DEFAULT_OUTPUT_DIR}, "
            f"or {DEFAULT_LIVE_OUTPUT_DIR} under --live)"
        ),
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help=(
            "label emitted gaps as live pipeline input instead of dry-run artifacts. "
            f"Refused unless {LIVE_ENV_FLAG}=1 is also set (two-key arming)."
        ),
    )
    parser.add_argument(
        "--resolve",
        action="store_true",
        help=(
            "after emitting, stamp resolved_run/resolved_at on gap artifacts whose "
            "check PASSED in this run. The artifact is kept, never deleted."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    bundle = args.bundle or Path("var/northstar-cert/runs") / args.run_id
    dry_run = not args.live
    if args.live and os.environ.get(LIVE_ENV_FLAG) != "1":
        # Two-key arming, same rule as file_gap_findings.py: the flag alone must
        # not be able to flip a scheduled job from dry-run to live.
        print(
            json.dumps(
                {
                    "run_id": args.run_id,
                    "dry_run": True,
                    "error": f"--live refused: {LIVE_ENV_FLAG}=1 is not set",
                }
            )
        )
        return 2
    output_dir = args.output_dir or (DEFAULT_OUTPUT_DIR if dry_run else DEFAULT_LIVE_OUTPUT_DIR)
    try:
        paths = emit_gaps(
            db_path=args.db,
            run_id=args.run_id,
            output_dir=output_dir,
            evidence_root=args.evidence_root,
            bundle_path=bundle,
            project=args.project,
            dry_run=dry_run,
        )
        resolved = (
            resolve_gaps(
                db_path=args.db,
                run_id=args.run_id,
                output_dir=output_dir,
                project=args.project,
            )
            if args.resolve
            else []
        )
    except GapEmissionError as exc:
        print(json.dumps({"run_id": args.run_id, "dry_run": dry_run, "error": str(exc)}))
        return 70
    except Exception as exc:  # noqa: BLE001 - CLI boundary maps instrument faults to VOID
        error = f"instrument_error:{type(exc).__name__}:{exc}"
        print(json.dumps({"run_id": args.run_id, "dry_run": dry_run, "error": error}))
        return 70
    print(
        json.dumps(
            {
                "run_id": args.run_id,
                "dry_run": dry_run,
                "output_dir": str(output_dir),
                "gaps": [str(path) for path in paths],
                # Instrument faults, reported as a COUNT and never as gaps: a
                # reader must be able to tell "nothing broke" from "nothing was
                # measurable" on the face of the summary.
                "voided": paths.voided,
                # Each VOID row named, not just counted: a summary field alone
                # is invisible to every downstream consumer but the exit status
                # (see the fail-closed check below).
                "voided_rows": list(paths.voided_rows),
                # Rows whose own suite held no case for them: named, never
                # described from another suite's metadata (their gap identity
                # would be wrong in the one field nobody re-checks).
                "undescribable": list(paths.undescribable),
                "resolved": [str(path) for path in resolved],
            }
        )
    )
    # FAIL CLOSED on EITHER instrument-fault class. The describable gaps were
    # still emitted — good evidence is not held hostage — but a VOID row or an
    # undescribable row may itself be a genuine FAIL, so the run must not look
    # successful. Only the EXIT STATUS is consumed downstream (the cadence
    # propagates stage rc), so the JSON fields alone are invisible; this is the
    # difference between a red run and a silently green one. Both are kept
    # distinct from rc 70 ON PURPOSE: 70 means nothing could be emitted at all
    # (GapEmissionError above), rc 2 means emission otherwise completed but at
    # least one row was an instrument fault this run refuses to launder into a
    # clean summary. The normal cadence never reaches the voided branch (the
    # recorder aborts with rc 70 before emission if ANY check VOIDs); it exists
    # for every OTHER caller that can reach emission without that gate firing.
    # (This reconciles the landed voided-visibility fix with this lane's
    # undescribable/unknown-suite fail-close — both surface here.)
    fail_closed = False
    if paths.voided_rows:
        print(
            f"[emit-gaps] run {args.run_id}: {len(paths.voided_rows)} eval row(s) voided "
            f"outside the recorder's rc-70 path ({', '.join(paths.voided_rows)})",
            file=sys.stderr,
        )
        fail_closed = True
    if paths.undescribable:
        print(
            f"[emit-gaps] run {args.run_id}: {len(paths.undescribable)} eval row(s) could not "
            f"be read against their own suite ({', '.join(paths.undescribable)})",
            file=sys.stderr,
        )
        fail_closed = True
    if fail_closed:
        print(
            f"[emit-gaps] run {args.run_id}: exiting 2 — instrument faults present", file=sys.stderr
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
