#!/usr/bin/env python3
"""Record ONE provider call through the real cost DAL, from outside Python.

Why this exists
---------------
Estate shell wrappers (the Kimi spend-cap shim being the live one) were
appending money rows by building a 20-column ``INSERT INTO provider_call_usage``
as a *shell string* and piping it to ``/usr/bin/sqlite3``. That bypass has four
defects this entry point removes:

1. **Unquoted interpolation.** A provider/outcome string containing a quote
   either broke the statement or corrupted the row. Here every value is bound
   by the DAL through SQLite parameters, so a quote is just a character.
2. **Placeholder attribution.** ``call_id``/``request_id``/``execution_id`` were
   all set to the SAME synthetic value, so the row *looked* attributed while
   joining to nothing. Attribution is now explicit: an identity the caller
   actually has is used as-is, and an identity it does NOT have is written as a
   distinct, namespaced :data:`UNATTRIBUTED_PREFIX` marker carrying the reason.
   A marked row is quarantinable (``WHERE request_id LIKE 'unattributed:%'``);
   a placeholder-filled one was not. Submitting the old shape
   (``call_id == request_id == execution_id``) is refused outright.
3. **No settlement.** Rows written behind the DAL never pass through
   :meth:`SqliteStore.record_provider_call`, so replay-idempotency, the
   cost-quality invariants and any later settlement never saw them.
4. **Swallowed failure.** The shell wrapper discarded the sqlite3 exit status,
   so a lost cost row and a recorded cost row looked identical downstream.
   This process makes the outcome observable in two carriers: a NON-ZERO exit
   status, and a single-line JSON result record on stdout. Callers are expected
   to persist that record; the exit status alone is not evidence of a write,
   because a wrapper that preserves its child's status says nothing about its
   own telemetry.

Contract
--------
Input is a CostObservation payload: JSON object on stdin (``--stdin-json``),
or individual ``--flags``, or both (an explicitly-passed flag overrides the
matching stdin key). Output is exactly one JSON line on stdout.

Exit status
-----------
``0``  row recorded (or an identical replay matched an existing row)
``1``  the write did not land (missing/unmigrated/corrupt DB, SQLite error)
``2``  the request was refused before any write (bad or dishonest payload, or a
       spend ledger that cannot be identified safely)

Nothing here invents money: cost quality/columns are validated by
``CostObservation`` and the DAL, never by this file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import traceback
from collections.abc import Sequence
from pathlib import Path
from typing import Any

if __package__ in (None, ""):  # pragma: no cover - direct `python3 scripts/spend/...`
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from omniagentos import contracts  # noqa: E402
from omniagentos.adapters.spend_db import (  # noqa: E402
    SpendDbResolutionError,
    resolve_spend_db_path,
)
from omniagentos.contracts import CostObservation  # noqa: E402
from omniagentos.db.store import SqliteStore  # noqa: E402

# --- attribution marking ---------------------------------------------------

#: Every synthetic identity this entry point mints starts with this prefix, so
#: unattributed spend is one ``LIKE 'unattributed:%'`` away instead of hiding
#: behind a plausible-looking id.
UNATTRIBUTED_PREFIX = "unattributed:"

#: Namespaces keep the minted request/execution ids DISTINCT values. They share
#: a token (so both halves of one call are recognisably the same call) but they
#: are never equal to each other or to the call_id -- that equality was exactly
#: the disguise being removed.
REQUEST_NAMESPACE = "request"
EXECUTION_NAMESPACE = "execution"

_SLUG_ALLOWED = re.compile(r"[^a-z0-9._-]+")
_MAX_REASON_SLUG = 64

#: CostObservation fields a caller must state outright. Money-shaped fields get
#: no silent defaults: a wrapper that cannot say which provider/model/quality it
#: is reporting is refused rather than guessed at.
REQUIRED_FIELDS = (
    "provider",
    "transport",
    "requested_model",
    "effective_model",
    "model_lineage",
    "billing_provider",
    "adapter_key",
    "stage",
    "request_state",
    "cost_quality",
    "cost_source",
)

#: Payload keys accepted from stdin/flags. Exactly the CostObservation surface.
_STR_FIELDS = (
    "call_id",
    "request_id",
    "execution_id",
    "run_id",
    "campaign_id",
    "reservation_id",
    "task_id",
    "attempt_id",
    "session_id",
    "work_id",
    "root_trace_id",
    "stage",
    "provider",
    "transport",
    "requested_model",
    "effective_model",
    "model_lineage",
    "billing_provider",
    "adapter_key",
    "provider_request_id",
    "request_state",
    "provider_outcome",
    "cost_usd_decimal",
    "cost_quality",
    "cost_source",
    "pricing_revision",
    "created_at",
    "settled_at",
)
_INT_FIELDS = (
    "attempt_index",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cost_usd_nanos",
    "cost_upper_bound_usd_nanos",
)
PAYLOAD_FIELDS = _STR_FIELDS + _INT_FIELDS


class RecorderRefusal(Exception):
    """The payload was refused before any write was attempted (exit 2)."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


class RecorderWriteError(Exception):
    """The write itself did not land (exit 1)."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


def is_unattributed(value: str | None) -> bool:
    """True when ``value`` is a marker minted for a missing identity."""

    return bool(value) and str(value).startswith(UNATTRIBUTED_PREFIX)


def reason_slug(reason: str) -> str:
    """A stable, id-safe rendering of an operator-supplied reason."""

    slug = _SLUG_ALLOWED.sub("-", reason.strip().lower()).strip("-.")
    slug = re.sub(r"-{2,}", "-", slug)[:_MAX_REASON_SLUG].strip("-.")
    if not slug:
        raise RecorderRefusal(
            "bad_unattributed_reason",
            f"--unattributed-reason {reason!r} has no id-safe characters",
        )
    return slug


def unattributed_id(namespace: str, reason: str, token: str) -> str:
    """A marker id: prefix, namespace, reason, and a call-derived token."""

    return f"{UNATTRIBUTED_PREFIX}{namespace}:{reason_slug(reason)}:{token}"


def _attribution_token(call_id: str) -> str:
    """Derive the marker token from the call id.

    Deterministic on purpose: the DAL treats an identical payload under a known
    ``call_id`` as a replay, so a re-send of the same call must produce the same
    marker ids rather than a fresh conflicting pair.
    """

    return hashlib.sha256(call_id.encode("utf-8")).hexdigest()[:20]


def resolve_attribution(
    payload: dict[str, Any], *, unattributed_reason: str | None
) -> tuple[dict[str, Any], list[str]]:
    """Fill missing request/execution identities with explicit markers.

    Returns the payload (mutated copy) and the list of fields that had to be
    marked. Refuses the two dishonest shapes: an all-identical placeholder
    triple, and a missing identity with no stated reason.
    """

    resolved = dict(payload)
    call_id = str(resolved.get("call_id") or "").strip()
    if not call_id:
        call_id = contracts.new_id("call")
    resolved["call_id"] = call_id

    request_id = str(resolved.get("request_id") or "").strip()
    execution_id = str(resolved.get("execution_id") or "").strip()

    if request_id and execution_id and request_id == execution_id == call_id:
        raise RecorderRefusal(
            "placeholder_attribution",
            "call_id, request_id and execution_id are all the same value "
            f"({call_id!r}); that is an unattributed row wearing a join key. "
            "Pass the real ids, or omit them and state --unattributed-reason.",
        )

    marked: list[str] = []
    token = _attribution_token(call_id)
    for field, namespace, value in (
        ("request_id", REQUEST_NAMESPACE, request_id),
        ("execution_id", EXECUTION_NAMESPACE, execution_id),
    ):
        if value:
            resolved[field] = value
            continue
        if not unattributed_reason or not unattributed_reason.strip():
            raise RecorderRefusal(
                "missing_attribution",
                f"{field} was not supplied and no --unattributed-reason was "
                "given; a row with no identity must say WHY it has none.",
            )
        resolved[field] = unattributed_id(namespace, unattributed_reason, token)
        marked.append(field)

    return resolved, marked


# --- payload assembly ------------------------------------------------------


def build_observation(
    payload: dict[str, Any], *, unattributed_reason: str | None = None
) -> tuple[CostObservation, list[str]]:
    """Validate one payload into a CostObservation plus its marked fields."""

    unknown = sorted(set(payload) - set(PAYLOAD_FIELDS))
    if unknown:
        raise RecorderRefusal(
            "unknown_fields",
            f"payload has fields the cost contract does not define: {', '.join(unknown)}",
        )

    cleaned = {key: value for key, value in payload.items() if value is not None}
    missing = [field for field in REQUIRED_FIELDS if not str(cleaned.get(field) or "").strip()]
    if missing:
        raise RecorderRefusal(
            "missing_fields",
            f"payload is missing required field(s): {', '.join(missing)}",
        )

    resolved, marked = resolve_attribution(cleaned, unattributed_reason=unattributed_reason)
    try:
        observation = CostObservation.model_validate(resolved)
    except Exception as exc:  # pydantic ValidationError and friends
        raise RecorderRefusal("invalid_observation", str(exc)) from exc
    return observation, marked


# --- database resolution ---------------------------------------------------


def resolve_db_path(explicit: str | None) -> str:
    """Resolve the ONE paid-spend ledger through the canonical identity.

    Delegated to :func:`omniagentos.adapters.spend_db.resolve_spend_db_path`
    rather than re-implemented, because that function is the estate's single
    definition of "the spend ledger" (explicit ``--db``, then
    ``OMNIAGENTOS_SPEND_DB``, then the fixed production file) and
    ``SpendGuard`` -- the other writer of this table -- already resolves through
    it. A second precedence chain here is not a cosmetic duplicate: the earlier
    version of this function fell through to ``contracts.default_db_path()``,
    which honours ``OMNIAGENTOS_DB`` and, under an active campaign, resolves to
    the CAMPAIGN database. A paid row written there is invisible to the cap
    preflight that reads the production ledger, i.e. spend that silently reads
    as absent.

    The shared resolver also refuses outright while a simulation context is
    active. That refusal is deliberate and fails closed: a lost row that is
    loudly reported as lost (the caller gets exit 2 and a record) is recoverable
    evidence, whereas a paid row filed into a simulation ledger is not.
    """

    try:
        return str(resolve_spend_db_path(explicit or None))
    except SpendDbResolutionError as exc:
        raise RecorderRefusal("spend_db_unresolvable", str(exc)) from exc


#: Both must already exist for a path to count as "the ledger". ``schema_migrations``
#: is checked as well as the target table because a hand-rolled probe DB that
#: happens to contain ``provider_call_usage`` would otherwise be re-migrated from
#: scratch by the store constructor and fail obscurely mid-DDL.
_REQUIRED_TABLES = ("schema_migrations", "provider_call_usage")


def _assert_ledger_present(db_path: str) -> None:
    """Refuse a path that is not already a migrated ledger.

    Opening ``SqliteStore`` on a missing/blank file would MIGRATE a brand new
    schema into it, turning a typo into a plausible-looking phantom ledger that
    silently absorbs money rows. A cost writer must attach to the real ledger or
    fail loudly.
    """

    import sqlite3

    if not os.path.isfile(db_path):
        raise RecorderWriteError("db_missing", f"ledger DB does not exist: {db_path}")
    try:
        uri = f"file:{db_path}?mode=ro"
        with sqlite3.connect(uri, uri=True) as connection:
            present = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN (?, ?)",
                    _REQUIRED_TABLES,
                ).fetchall()
            }
    except sqlite3.Error as exc:
        raise RecorderWriteError("db_unreadable", f"{db_path}: {exc}") from exc
    missing = [table for table in _REQUIRED_TABLES if table not in present]
    if missing:
        raise RecorderWriteError(
            "schema_missing",
            f"{db_path} is not a migrated ledger (missing table(s): {', '.join(missing)})",
        )


def record(
    payload: dict[str, Any],
    *,
    db_path: str | None = None,
    unattributed_reason: str | None = None,
) -> dict[str, Any]:
    """Record one observation through the DAL and return the result record."""

    observation, marked = build_observation(payload, unattributed_reason=unattributed_reason)
    resolved_db = resolve_db_path(db_path)
    _assert_ledger_present(resolved_db)

    # Store construction is inside the guard on purpose: it runs the migrator,
    # and a failure THERE is just as much a lost cost row as a failed INSERT.
    store: SqliteStore | None = None
    try:
        store = SqliteStore(resolved_db)
        row = store.record_provider_call(observation)
    except Exception as exc:
        raise RecorderWriteError("write_failed", f"{type(exc).__name__}: {exc}") from exc
    finally:
        if store is not None:
            store.close()

    return {
        "ok": True,
        "call_id": row["call_id"],
        "request_id": row["request_id"],
        "execution_id": row["execution_id"],
        "attributed": not marked,
        "unattributed_fields": marked,
        "cost_quality": row["cost_quality"],
        "cost_upper_bound_usd_nanos": row["cost_upper_bound_usd_nanos"],
        "created_at": row["created_at"],
        "db_path": resolved_db,
    }


# --- CLI -------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="record_provider_call.py",
        description="Record one provider call through the cost DAL.",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="ledger path (default: the canonical spend ledger -- OMNIAGENTOS_SPEND_DB, "
        "else the fixed production file)",
    )
    parser.add_argument(
        "--stdin-json",
        action="store_true",
        help="read a CostObservation JSON object from stdin (flags override its keys)",
    )
    parser.add_argument(
        "--unattributed-reason",
        default=None,
        help="why this call has no request/execution identity (required when either is absent)",
    )
    for field in _STR_FIELDS:
        parser.add_argument(f"--{field.replace('_', '-')}", dest=field, default=None)
    for field in _INT_FIELDS:
        parser.add_argument(f"--{field.replace('_', '-')}", dest=field, default=None, type=int)
    return parser


def _payload_from_args(args: argparse.Namespace) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if args.stdin_json:
        raw = sys.stdin.read().strip()
        if not raw:
            raise RecorderRefusal("empty_stdin", "--stdin-json was given but stdin was empty")
        try:
            loaded = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RecorderRefusal("bad_json", f"stdin is not valid JSON: {exc}") from exc
        if not isinstance(loaded, dict):
            raise RecorderRefusal("bad_json", "stdin JSON must be an object")
        payload.update(loaded)
    for field in PAYLOAD_FIELDS:
        value = getattr(args, field, None)
        if value is not None:
            payload[field] = value
    return payload


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        payload = _payload_from_args(args)
        result = record(
            payload,
            db_path=args.db,
            unattributed_reason=args.unattributed_reason,
        )
    except RecorderRefusal as exc:
        _emit({"ok": False, "error_kind": exc.kind, "error": str(exc)})
        return 2
    except RecorderWriteError as exc:
        _emit({"ok": False, "error_kind": exc.kind, "error": str(exc)})
        return 1
    except Exception as exc:  # noqa: BLE001 -- an unexpected fault is still a lost row
        traceback.print_exc(file=sys.stderr)
        _emit({"ok": False, "error_kind": "unexpected", "error": f"{type(exc).__name__}: {exc}"})
        return 1
    _emit(result)
    return 0


def _emit(record_line: dict[str, Any]) -> None:
    """One JSON line on stdout; failures are ALSO echoed to stderr.

    Two carriers on purpose: stdout is what a wrapper persists, stderr is what
    an operator sees in a terminal. Neither replaces the exit status.
    """

    line = json.dumps(record_line, sort_keys=True, separators=(",", ":"))
    print(line)
    if not record_line.get("ok"):
        print(f"record_provider_call: FAILED {line}", file=sys.stderr)


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
