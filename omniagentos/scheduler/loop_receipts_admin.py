"""Operator reconcile path for a receipt wedged on UNKNOWN. Manual, on purpose.

THE DEBT THIS PAYS
------------------

``loop_effects`` classifies every failure it cannot resolve as UNKNOWN and fails
CLOSED: the worker's idempotency receipt stays CLAIMED and never completes, and
``receipts._attempt`` re-raises ``EffectStateUnknown`` on that business key for
every subsequent tick. That is the correct trade — a read timeout on a paid call
may have been received and billed, and the alternative (call it absence, release
the claim) is the double-billing defect this whole module exists to prevent.

But the trade creates a debt. ``store.idem_release`` is reached from exactly one
place — ``receipts._attempt`` on ``EffectUnavailable`` — so a claim parked on
UNKNOWN has no way back. There is no TTL, no janitor and, until this module, no
operator tool: recovery was manual SQL against the control-plane database. The
wedge is LOUD (a failed tick, every tick, with the receipt key in the event), so
it is an operability gap and not a correctness hole, but "loud forever" is not a
recovery path.

WHY THIS IS NOT A TTL
---------------------

The obvious fix is to expire a claim after some age and release it
automatically. It must not be built, and the reason is the whole argument of
this lane restated:

    an UNKNOWN receipt is precisely the case where NOTHING OBSERVABLE says
    whether the provider ran and charged.

A timer observes nothing new. Releasing on a timer therefore converts every
UNKNOWN into an ABSENCE after a delay, which is the exact defect
``_after_billable_work`` and ``_billable_scope`` exist to prevent, reintroduced
with a wait in front of it. The next tick re-issues the paid call, and because
the claim is gone it does not even consume an attempt. A crash-looping instance
would re-buy on every expiry.

The missing evidence cannot be manufactured inside this process. It exists —
Replicate's dashboard says whether the prediction was created, the provider's
usage page says whether the completion was billed — but only OUTSIDE it. So the
release is gated on a human having gone and looked, and this tool's whole job is
to make that cheap, auditable, and hard to do by accident.

WHAT IS AND IS NOT GUARANTEED
-----------------------------

``store.idem_release`` carries ``result_json IS NULL`` in its SQL, so a
COMPLETED receipt — the record of an effect that really ran — can never be
deleted through this tool no matter what an operator types. What this tool CAN
do is release a claim whose payment really did land, if the operator says so
wrongly. That is not a hole to be closed in code; it is the decision being
delegated, and it is why a release demands an explicit reason, refuses to act
without a stated provider check, and writes an event naming the operator before
it touches the row.

WHERE THE INTERLOCK LIVES, AND WHY IT IS NOT THE FLAG
-----------------------------------------------------

The confirmation used to be enforced only in ``_cmd_release``, the argparse
wrapper. That put the entire safety property in the CLI, so anything that
IMPORTED this module could call the release function directly and drop a
possibly-billed claim with no human in the loop — the double-billing defect,
reachable in one line, from inside the tool built to prevent it.

So the requirement now lives in the destructive function itself. ``_release_claim``
takes a required keyword-only ``checked: _ProviderChecked``: a caller that does
not supply one gets a ``TypeError`` before any row is examined, and there is no
default, no ``bool`` and no truthy stand-in that satisfies it. ``_ProviderChecked``
carries the operator and the reason with the assertion, because the three are one
decision — an assertion nobody signed, or a signature with nothing asserted, is
not a decision anyone can be held to.

That is a guarantee about CALLERS, not about humans: no in-process check can
prove someone really opened the dashboard. What it does buy is that the claim is
explicit, attributable, recorded in the audit event, and greppable — every place
in this repo that asserts a human checked the provider is a place that names
``_ProviderChecked``.

The ``--i-have-checked-the-provider`` flag survives as the CLI's own affordance:
without it the command is a dry run that prints the hazard and changes nothing.
It is now the outer of two interlocks rather than the only one.

THE PUBLIC SURFACE IS ``main`` AND NOTHING ELSE
-----------------------------------------------

A human at a terminal is the only intended caller of this module, so ``main`` is
the only public name in it. The listing helper, the release function and the
parser are private: they are the CLI's own parts, and a module whose destructive
function is importable by name invites exactly the caller this tool must not
have.

USAGE
-----

    python -m omniagentos.scheduler.loop_receipts_admin list
    python -m omniagentos.scheduler.loop_receipts_admin list --instance render_probe
    python -m omniagentos.scheduler.loop_receipts_admin show --key loop:...
    python -m omniagentos.scheduler.loop_receipts_admin release --key loop:... \\
        --reason "replicate dashboard shows no prediction for this key" \\
        --operator owner --i-have-checked-the-provider

``list`` and ``show`` are reads. ``release`` without the confirmation flag is a
dry run that prints what it would do and changes nothing.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

#: Every receipt minted by the loop runtime is namespaced this way
#: (``receipts.receipt_key``: ``loop:<template>:<instance>:<node>:<tool>:<bk>``).
#: The tool refuses to touch anything else, so a runner receipt that happens to
#: be mid-flight can never be released through the loops operator path.
LOOP_KEY_PREFIX = "loop:"

EVENT_TYPE = "loop.effect"
ACTION_RELEASED = "loop.receipt.operator_released"


@dataclass(frozen=True, slots=True)
class _ProviderChecked:
    """A named human's assertion that they looked OUTSIDE this process.

    This exists to be un-omittable. ``_release_claim`` requires one, so the
    question "did anybody check whether the provider already charged?" cannot be
    skipped by a caller that never thought to ask it — the call does not type-check
    at runtime without an answer.

    It is deliberately a type and not a ``bool``. A flag can be defaulted, read
    out of config, or arrive as ``or True`` from a refactor, and none of those
    reads as a claim about the world. A value that must be constructed by name
    does: every assertion in this repo that a human checked the provider is a
    literal ``_ProviderChecked(...)`` somebody wrote on purpose, and a reviewer
    can find all of them with one grep.

    ``operator`` and ``reason`` ride along because the assertion, the person and
    the evidence are one decision. Splitting them would allow a release recorded
    with no name against it, or a name against nothing observed, and the audit
    event this produces is the only lasting trace of a choice about money.

    Note what this does NOT claim: nothing in this process can verify the human
    actually opened the dashboard. The guarantee is that the claim was made
    explicitly, by someone, and is written down.
    """

    operator: str
    reason: str


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _parse_ts(raw: Any) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _age_minutes(created_at: Any, *, now: datetime | None = None) -> float | None:
    created = _parse_ts(created_at)
    if created is None:
        return None
    return ((now or _utc_now()) - created).total_seconds() / 60.0


def _open_store(db_path: str) -> Any:
    from omniagentos.db.store import SqliteStore

    return SqliteStore(db_path)


def _claimed_receipts(
    store: Any,
    *,
    instance: str = "",
    older_than_minutes: float = 0.0,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Every loop receipt that is CLAIMED and never completed.

    ``result_json IS NULL`` is the definition of CLAIMED (``receipts.CLAIMED``
    is "the absence of a result, not a stored value"), and it is applied in SQL
    rather than in Python so a completed row cannot reach a caller that might
    then offer it for release.
    """
    rows = store._connection.execute(  # noqa: SLF001 — admin read, no public list API
        "SELECT key, run_id, step_name, created_at FROM idempotency "
        "WHERE result_json IS NULL AND key LIKE ? ORDER BY created_at",
        (LOOP_KEY_PREFIX + "%",),
    ).fetchall()

    out: list[dict[str, Any]] = []
    for row in rows:
        record = {
            "key": row["key"],
            "instance": row["run_id"],
            "node": row["step_name"],
            "created_at": row["created_at"],
            "age_minutes": _age_minutes(row["created_at"], now=now),
        }
        if instance and record["instance"] != instance:
            continue
        if older_than_minutes:
            age = record["age_minutes"]
            if age is None or age < older_than_minutes:
                continue
        out.append(record)
    return out


def _find(store: Any, key: str) -> dict[str, Any] | None:
    return store.idem_get(key)


def _describe(row: dict[str, Any]) -> str:
    completed = row.get("result_json") is not None
    state = "COMPLETED" if completed else "CLAIMED (never completed)"
    age = _age_minutes(row.get("created_at"))
    age_text = f"{age:.1f} min" if age is not None else "unknown"
    lines = [
        f"  key        : {row.get('key')}",
        f"  instance   : {row.get('run_id')}",
        f"  node       : {row.get('step_name')}",
        f"  created_at : {row.get('created_at')}  (age {age_text})",
        f"  state      : {state}",
    ]
    if completed:
        lines.append(f"  completed  : {row.get('completed_at')}")
        lines.append(f"  result     : {str(row.get('result_json'))[:400]}")
    return "\n".join(lines)


def _release_claim(
    store: Any,
    *,
    key: str,
    checked: _ProviderChecked,
) -> tuple[bool, str]:
    """Release ONE claimed loop receipt and record who decided to.

    ``checked`` is required and has no default. That is the safety interlock,
    and it is here rather than in the CLI on purpose: this function is the thing
    that deletes the row, so this is the only place a check cannot be routed
    around by calling something else.

    Two kinds of "no", deliberately different:

    * An OPERATOR mistake — a key that is not a loop receipt, a blank reason, a
      receipt that already completed — returns ``(False, message)``. This is an
      operator tool and the message is the product; a traceback is not an answer
      to someone holding a wedged receipt.
    * A CALLER that does not state the provider check raises ``TypeError``. That
      is not a mistake to be reported back, it is code that must not exist, and
      it should fail at the call site loudly enough that nobody ships it.

    The audit event is written BEFORE the delete. If the process dies between
    the two, the record says a release was authorised and the row is still
    there — recoverable, and the wrong way round would leave an unexplained
    release in a money-adjacent table.
    """
    if not isinstance(checked, _ProviderChecked):
        raise TypeError(
            "releasing a loop claim requires a _ProviderChecked stating that a named "
            f"human looked at the provider; got {type(checked).__name__}. There is no "
            "truthy stand-in on purpose: an UNKNOWN receipt is exactly the case where "
            "nothing inside this process can tell you whether the call was billed, so "
            "no value computed in here is evidence."
        )
    reason = checked.reason
    operator = checked.operator

    if not key.startswith(LOOP_KEY_PREFIX):
        return False, (
            f"refusing: {key!r} is not a loop receipt "
            f"(loop receipts start with {LOOP_KEY_PREFIX!r})"
        )
    if not reason.strip():
        return False, "refusing: --reason is required and must not be blank"
    if not operator.strip():
        return False, "refusing: --operator is required and must not be blank"

    row = _find(store, key)
    if row is None:
        return False, f"refusing: no receipt with key {key!r} exists"
    if row.get("result_json") is not None:
        # idem_release would refuse this anyway (its SQL carries
        # ``result_json IS NULL``); saying so here makes the refusal legible
        # instead of an unexplained "0 rows".
        return False, (
            f"refusing: receipt {key!r} is COMPLETED — it is the record of an "
            "effect that really ran and must never be deleted"
        )

    try:
        store.insert_event(
            type=EVENT_TYPE,
            actor=f"operator:{operator}",
            action=ACTION_RELEASED,
            target_type="loop_receipt",
            target_id=key,
            payload={
                "key": key,
                "instance": row.get("run_id"),
                "node": row.get("step_name"),
                "created_at": row.get("created_at"),
                "reason": reason.strip()[:1000],
                "operator": operator.strip()[:200],
                "note": (
                    "manual reconcile of a receipt wedged on UNKNOWN; the operator "
                    "asserts they checked the provider and no billable effect landed"
                ),
            },
        )
    except Exception as exc:  # noqa: BLE001
        return False, (
            f"refusing: could not write the audit event ({type(exc).__name__}: {exc}); "
            "a release that cannot be recorded does not happen"
        )

    released = bool(store.idem_release(key))
    if not released:
        return False, (
            f"receipt {key!r} was not released (it completed or was removed between "
            "the check and the delete); the audit event records the attempt"
        )
    return True, f"released {key!r} — the next tick may attempt this business key again"


def _cmd_list(args: argparse.Namespace, store: Any) -> int:
    rows = _claimed_receipts(
        store,
        instance=args.instance,
        older_than_minutes=args.older_than_minutes,
    )
    if args.json:
        print(json.dumps(rows, indent=2, sort_keys=True))
        return 0
    if not rows:
        print("no claimed-and-uncompleted loop receipts")
        return 0
    print(f"{len(rows)} claimed loop receipt(s) with no recorded outcome:\n")
    for row in rows:
        age = row["age_minutes"]
        age_text = f"{age:.1f} min" if age is not None else "unknown age"
        print(f"  {row['key']}")
        print(f"      instance={row['instance']} node={row['node']} age={age_text}")
    print(
        "\nEach of these fails its tick CLOSED every tick. Before releasing one, "
        "check the provider\n(Replicate dashboard / model usage) and confirm no "
        "billable effect landed for that key."
    )
    return 0


def _cmd_show(args: argparse.Namespace, store: Any) -> int:
    row = _find(store, args.key)
    if row is None:
        print(f"no receipt with key {args.key!r}")
        return 1
    print(_describe(row))
    return 0


def _cmd_release(args: argparse.Namespace, store: Any) -> int:
    row = _find(store, args.key)
    if row is None:
        print(f"no receipt with key {args.key!r}")
        return 1

    if not args.i_have_checked_the_provider:
        # The flag's absence stops here rather than reaching _release_claim, so
        # the dry run can show the operator what they are about to do. The
        # function would refuse anyway — it cannot be called without a
        # _ProviderChecked — but a traceback is not a dry run.
        print("DRY RUN — nothing has been changed.\n")
        print(_describe(row))
        print(
            "\nThis receipt is wedged because the seam could not establish whether the\n"
            "provider ran and charged. Releasing it lets the next tick re-issue the\n"
            "call. If the call DID land, releasing pays for it twice.\n\n"
            "Check the provider first, then re-run with:\n"
            "    --operator <you> --reason <what you saw> --i-have-checked-the-provider"
        )
        return 2

    released, message = _release_claim(
        store,
        key=args.key,
        checked=_ProviderChecked(operator=args.operator, reason=args.reason),
    )
    print(message)
    return 0 if released else 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m omniagentos.scheduler.loop_receipts_admin",
        description=(
            "Inspect and manually reconcile loop idempotency receipts wedged on UNKNOWN. "
            "Release is a human decision about money and is never automatic."
        ),
    )
    parser.add_argument(
        "--db",
        default=None,
        help="control-plane sqlite path (default: contracts.default_db_path(), honors OMNIAGENTOS_DB)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    listing = sub.add_parser("list", help="claimed loop receipts with no recorded outcome")
    listing.add_argument("--instance", default="", help="only this loop instance")
    listing.add_argument(
        "--older-than-minutes",
        type=float,
        default=0.0,
        help="only claims older than this (a fresh claim is probably just in flight)",
    )
    listing.add_argument("--json", action="store_true", help="machine-readable output")
    listing.set_defaults(func=_cmd_list)

    show = sub.add_parser("show", help="everything recorded about one receipt")
    show.add_argument("--key", required=True)
    show.set_defaults(func=_cmd_show)

    release = sub.add_parser(
        "release",
        help="release ONE claimed receipt after checking the provider (dry run by default)",
    )
    release.add_argument("--key", required=True)
    release.add_argument("--reason", default="", help="what you checked and what you saw")
    release.add_argument("--operator", default="", help="who is making this decision")
    release.add_argument(
        "--i-have-checked-the-provider",
        action="store_true",
        help=(
            "assert that you have confirmed OUTSIDE this process that no billable "
            "effect landed for this key; without it this is a dry run"
        ),
    )
    release.set_defaults(func=_cmd_release)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.db is None:
        from omniagentos.contracts import default_db_path

        args.db = default_db_path()

    store = _open_store(args.db)
    try:
        return int(args.func(args, store))
    finally:
        try:
            store.close()
        except Exception:  # noqa: BLE001 — closing is best effort
            pass


if __name__ == "__main__":  # pragma: no cover - CLI entry
    sys.exit(main())
