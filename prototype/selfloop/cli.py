"""``python -m selfloop`` — the plug-and-play surface a scheduler drives.

One tick per process, one JSON line on stdout, and an exit status a cron job can
branch on::

    python -m selfloop tick --tools mypkg.loops --instance daily --template \\
        propose_evaluate_promote --params '{"brief": "..."}'

Everything this CLI needs from you arrives through **one function in one module
you name**: ``build_context(*, instance_id, template, params) -> LoopContext``.
That is the whole integration contract. The CLI never guesses a database path,
never picks a lease backend, never invents a gate and never registers a tool —
those are the decisions that make a loop safe or unsafe on your machine, and a
default for any of them would be this package choosing on your behalf and not
telling you.

Two properties of this file are safety properties rather than ergonomics.

**The module allowlist is an argument, never a hardcoded prefix.** An import
path is arbitrary code execution, so ``--tools`` cannot be permitted to come
from an unconstrained source. But a hardcoded ``selfloop.*`` prefix would also
mean a third party can never register tools from their own package, which makes
the library unusable for its actual purpose. The resolution: :func:`main` takes
``allow_prefixes``. An embedder that feeds this CLI from a config row passes an
explicit tuple; a human at a terminal passes nothing, because a person typing
``--tools X`` already has arbitrary execution and there is nothing left to
protect them from. ``--allow-prefix`` narrows what is in force and can never
widen it.

**A lease that cannot exclude another OS process is refused.** Not warned
about — refused, unless ``--accept-inprocess-lease`` says the caller has
established there is only one process. The failure this closes is silent: on a
host without POSIX ``flock`` the obvious fallback is a ``threading.Lock``, two
scheduled invocations then both acquire it, both read the same checkpoint and
both run the tick, and every symptom of that is a duplicate effect nobody
attributes to the lease. Degrading quietly to a lock that protects nothing is
worse than not running.

Exit status
-----------

``0`` the command did its job (for ``tick``: the tick reported, whatever it
reported). ``1`` the tick reported ``FAILED``. ``2`` the command could not run
at all — bad arguments, a refused module, a refused lease, a store that said no.

**Alert on the JSON line's ``outcome`` field, not on the exit status.** A tick
that stood aside for a peer, parked for a human or was blocked by a dead
credential all exit 0, because the process did exactly what it was asked to do;
what those ticks mean is in ``outcome`` (``favourable`` / ``neutral`` /
``adverse``) and in ``detail``. An exit status has three values and this package
has three-valued outcomes for a reason.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

from selfloop.approvals import AUTOMATION_IDENTITY_PREFIXES
from selfloop.context import LoopContext
from selfloop.contracts import ApprovalState, LoopError, LoopStatus, RecordKind
from selfloop.lease import FlockLease, InProcessLease, SqliteLease, flock_available
from selfloop.receipts import reconcile
from selfloop.runtime import run_once

#: The one function the ``--tools`` module must define. Named rather than
#: guessed at, and singular rather than a family of optional hooks, because an
#: integration contract a reader has to reconstruct from a search is a contract
#: nobody implements correctly the first time.
CONTEXT_FACTORY = "build_context"

#: Exit statuses. See the module docstring for why ``BLOCKED`` is not among the
#: non-zero ones.
EXIT_OK = 0
EXIT_TICK_FAILED = 1
EXIT_REFUSED = 2

#: Every record kind ``gc`` sizes. Enumerated from the shared vocabulary so a
#: kind added to :class:`~selfloop.contracts.RecordKind` is reported without
#: anyone having to remember this file exists.
GC_KINDS: tuple[str, ...] = tuple(kind.value for kind in RecordKind)


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def _emit_line(payload: Mapping[str, Any], *, stream: Any = None) -> None:
    """Write exactly one JSON line. ``default=str`` so nothing is ever unprintable.

    A command whose output depends on whether every value happened to be
    JSON-serialisable is a command that fails at 03:00 on the one field that was
    a ``Path``. The line is flushed because a scheduler that reads stdout through
    a pipe and then kills the process must not lose it.
    """
    out = sys.stdout if stream is None else stream
    out.write(json.dumps(payload, default=str, sort_keys=True) + "\n")
    out.flush()


def _refuse(detail: str, **extra: Any) -> int:
    """Report a refusal on stderr as one JSON line and return :data:`EXIT_REFUSED`.

    On stderr rather than stdout, so a caller piping stdout into a log of tick
    reports never has to distinguish a report from an apology.
    """
    _emit_line({"ok": False, "error": detail, **extra}, stream=sys.stderr)
    return EXIT_REFUSED


# ---------------------------------------------------------------------------
# Loading the caller's module
# ---------------------------------------------------------------------------


def _permitted(module_path: str, allowed: Sequence[str] | None) -> bool:
    """Is *module_path* inside one of *allowed*? ``None`` means unconstrained.

    Prefix matching is on package boundaries, never on characters: ``mypkg``
    permits ``mypkg`` and ``mypkg.loops`` and does **not** permit
    ``mypkg_evil``. A ``str.startswith`` check without the dot is the classic
    way an allowlist stops being one.
    """
    if allowed is None:
        return True
    return any(module_path == prefix or module_path.startswith(f"{prefix}.") for prefix in allowed)


def _effective_prefixes(
    constructor: Sequence[str] | None, flags: Sequence[str]
) -> tuple[tuple[str, ...] | None, str]:
    """Combine the constructor allowlist with ``--allow-prefix`` flags.

    Flags NARROW. Each one must itself be permitted by whatever the constructor
    supplied, so a command line can restrict an embedder's allowlist and can
    never escape it — the direction that makes the flag useful to an operator
    without making it a hole in the embedder's decision.
    """
    if not flags:
        return (None if constructor is None else tuple(constructor)), ""
    outside = [prefix for prefix in flags if not _permitted(prefix, constructor)]
    if outside:
        return None, (
            f"--allow-prefix {outside} is outside the allowlist this CLI was constructed "
            f"with ({list(constructor or ())}); the flag narrows what may be imported and "
            "cannot widen it"
        )
    return tuple(flags), ""


def _load_factory(module_path: str, allowed: Sequence[str] | None) -> Any:
    """Import *module_path* and return its :data:`CONTEXT_FACTORY`.

    Raises :class:`LoopError` with a message aimed at whoever typed the flag —
    the three things that go wrong are a path outside the allowlist, a module
    that does not import, and a module that imports but defines no factory, and
    each needs a different fix.
    """
    if not _permitted(module_path, allowed):
        raise LoopError(
            f"module {module_path!r} is not inside the allowed prefixes {list(allowed or ())}. "
            "An import path is arbitrary code execution, so this CLI imports only what its "
            "caller declared it may."
        )
    try:
        module = importlib.import_module(module_path)
    except Exception as exc:  # noqa: BLE001 - the import error IS the answer for the operator
        raise LoopError(f"could not import {module_path!r}: {type(exc).__name__}: {exc}") from exc
    factory = getattr(module, CONTEXT_FACTORY, None)
    if not callable(factory):
        raise LoopError(
            f"module {module_path!r} defines no callable {CONTEXT_FACTORY}(). This CLI builds "
            "no context of its own: define "
            f"`def {CONTEXT_FACTORY}(*, instance_id, template, params) -> LoopContext` there, "
            "register your tools on its ToolRegistry, and choose its store, lease and gate."
        )
    return factory


def _build_context(
    args: argparse.Namespace, allowed: Sequence[str] | None, params: Mapping[str, Any]
) -> LoopContext:
    """Call the caller's factory and check that it handed back a context.

    The tick's params are handed to the factory as well as to
    :func:`~selfloop.runtime.run_once`, because a context may legitimately
    depend on them — an artifact gate whose path carries today's date is the
    obvious case — and a factory that could not see them would have to guess.
    """
    factory = _load_factory(args.tools, allowed)
    ctx = factory(instance_id=args.instance, template=args.template, params=dict(params))
    if not isinstance(ctx, LoopContext):
        raise LoopError(
            f"{args.tools}.{CONTEXT_FACTORY}() returned {type(ctx).__name__}, not a LoopContext"
        )
    return ctx


def _parse_params(raw: str) -> dict[str, Any]:
    """Parse ``--params``. It must be a JSON **object** or nothing at all."""
    if not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LoopError(f"--params is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise LoopError(
            f"--params must be a JSON object, got {type(value).__name__}; it becomes "
            "state['params'], which templates read by key"
        )
    return value


# ---------------------------------------------------------------------------
# The lease refusal (FIX-18)
# ---------------------------------------------------------------------------


def _lease_excludes_processes(lease: Any) -> bool | None:
    """``True`` / ``False`` / ``None`` (this CLI cannot tell).

    ``None`` is a real answer and not a shrug: a lease this package did not
    write may well be correct, and refusing every third-party lease would make
    the CLI unusable for anyone who wrote one. It is treated as a refusal only
    where the platform makes the silent degradation likely — see
    :func:`_lease_refusal`.
    """
    if isinstance(lease, InProcessLease):
        return False
    if isinstance(lease, (FlockLease, SqliteLease)):
        return True
    return None


def _lease_refusal(ctx: LoopContext, accepted: bool) -> str:
    """Why this lease must not drive a scheduled tick, or ``""``.

    Two refusals, both lifted by ``--accept-inprocess-lease``:

    * the lease is an :class:`~selfloop.lease.InProcessLease`, which excludes
      two threads of one interpreter and nothing else. Two ``python -m
      selfloop`` processes a second apart both acquire it and both run the tick;
    * POSIX ``flock`` is unavailable and this CLI cannot recognise the lease as
      one that excludes processes. That is the exact shape of the Windows
      degradation: the obvious fallback on a host without ``fcntl`` is a lock
      that protects nothing, and the loop keeps running while having quietly
      stopped being safe.
    """
    if accepted:
        return ""
    verdict = _lease_excludes_processes(ctx.lease)
    if verdict is True:
        return ""
    if verdict is False:
        return (
            "this context's lease is an InProcessLease, which gives zero protection between "
            "OS processes: two scheduled invocations both acquire it, both read the same "
            "checkpoint and both run the tick. Use FlockLease (POSIX) or SqliteLease "
            "(portable), or pass --accept-inprocess-lease to state that you have established "
            "there is only one process."
        )
    if not flock_available():
        return (
            f"POSIX flock is unavailable on this interpreter and this CLI cannot verify that "
            f"{type(ctx.lease).__name__} excludes another OS process. Refusing rather than "
            "degrading silently — SqliteLease is the portable multi-process answer. Pass "
            "--accept-inprocess-lease if you have established that this lease is safe here."
        )
    return ""


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def _cmd_tick(ctx: LoopContext, args: argparse.Namespace) -> int:
    """Run one tick and print its :class:`~selfloop.contracts.RunReport`.

    The report is printed whatever it says, because a tick reports and does not
    crash: a scheduler handed a traceback learns nothing it can act on, while a
    line carrying ``status``, ``outcome``, ``stage`` and ``detail`` is
    actionable even when the news is bad.
    """
    refusal = _lease_refusal(ctx, args.accept_inprocess_lease)
    if refusal:
        return _refuse(refusal, instance_id=args.instance, template=args.template)

    report = run_once(ctx, args.template, params=args.params_obj)
    _emit_line(report.as_dict())
    return EXIT_TICK_FAILED if report.status == LoopStatus.FAILED else EXIT_OK


def _cmd_lessons(ctx: LoopContext, args: argparse.Namespace) -> int:
    """List this store's lessons, with a count per status.

    The counts are the point and they are printed first. "207 staged, 0
    promoted" is the entire disease this package was built to prevent, and it is
    invisible in a list of rows that an operator scrolls past. A store whose
    staged count climbs while its promoted count stays at zero has a promotion
    gate that is wired and closed, which is exactly the failure that looks like
    health.

    Deliberately not :func:`selfloop.learn.recall`: recall returns promoted rows
    only, and the rows an operator most needs to see here are the ones that were
    never promoted.
    """
    rows = list(ctx.records.query(RecordKind.LESSON.value))
    if args.scope:
        rows = [row for row in rows if str(row.get("scope", "")) == args.scope]
    rows.sort(key=lambda row: (str(row.get("created_at", "")), str(row.get("id", ""))))

    counts: dict[str, int] = {}
    for row in rows:
        status = str(row.get("status", "unknown"))
        counts[status] = counts.get(status, 0) + 1

    _emit_line(
        {
            "instance_id": ctx.instance_id,
            "scope": args.scope or None,
            "counts": counts,
            "total": len(rows),
            "lessons": rows,
        }
    )
    return EXIT_OK


def _cmd_approve(ctx: LoopContext, args: argparse.Namespace) -> int:
    """Record a human's decision on one parked approval.

    Two refusals before the store is touched, because they are different
    mistakes and the message should name the one that was made.

    An automation identity is refused outright. The loop can only ever write
    ``loop:<instance>`` as its actor, so it cannot approve itself — but a human
    running this with ``--by cron`` would hand the same authority to a scheduler,
    and :func:`selfloop.approvals.read_outcome` would then refuse the row at the
    seam anyway, leaving an effect that is approved on paper and can never run.
    Refusing here says so while it can still be fixed.

    Losing the compare-and-set is reported as a refusal rather than as success.
    ``decide`` is a CAS on ``pending``, so ``False`` means the row was already
    decided or has expired, and telling the operator "approved" when their
    decision did not take is the one outcome that would matter later.
    """
    who = args.by.strip()
    if not who or any(who.lower().startswith(p) for p in AUTOMATION_IDENTITY_PREFIXES):
        return _refuse(
            f"--by {args.by!r} is not a person. An approval decided by an automation "
            "identity is refused at the execution seam, so recording one here would "
            "produce an effect that is approved and can never run.",
            approval_id=args.approval_id,
        )

    before = ctx.approvals.get(args.approval_id)
    if before is None:
        return _refuse(
            f"no approval row {args.approval_id!r} in this store. A decision must not mint "
            "the row it decides.",
            approval_id=args.approval_id,
        )

    decided = ctx.approvals.decide(
        args.approval_id,
        state=args.decision,
        by=who,
        note=args.note,
        at=ctx.clock.now_iso(),
    )
    after = ctx.approvals.get(args.approval_id) or {}
    payload = {
        "approval_id": args.approval_id,
        "decision": args.decision,
        "by": who,
        "recorded": bool(decided),
        "state": str(after.get("state", "")),
        "decided_by": str(after.get("decided_by", "") or after.get("by", "")),
    }
    if not decided:
        return _refuse(
            f"approval {args.approval_id!r} was not pending (it is "
            f"{after.get('state', 'unknown')!r}); the decision was NOT recorded. A "
            "compare-and-set that loses must not report success — that is how an "
            "approval overwrites a rejection.",
            **payload,
        )
    _emit_line(payload)
    return EXIT_OK


def _cmd_reconcile(ctx: LoopContext, args: argparse.Namespace) -> int:
    """Declare what actually happened to an effect whose state is unknown.

    The only way out of a fail-closed :class:`~selfloop.contracts.
    EffectStateUnknown`, and it is a person's statement rather than a timer's:
    the runtime refuses to re-run an effect that may already have happened, and
    no amount of waiting establishes anything, because a timer observes nothing.
    Everything that makes this safe — history first, then the completion; a
    refusal to overwrite a settled row; a refusal of an automation identity —
    lives in :func:`selfloop.receipts.reconcile`, which this only calls.
    """
    record = reconcile(ctx, args.key, outcome=args.outcome, by=args.by, note=args.note)
    _emit_line(record.as_dict())
    return EXIT_OK


def _cmd_gc(ctx: LoopContext, args: argparse.Namespace) -> int:
    """Size the store's retention problem. **Deletes nothing, on purpose.**

    Every port in this package is ``put``/``get``/``query``/``transition``.
    There is no ``delete``, and its absence is a design commitment rather than a
    gap: these records are the evidence the loop was graded on, and a loop that
    can erase its own ledger can erase the evidence of what it did. Pruning is
    therefore your backend's operation, taken deliberately, and this command
    exists so that you take it with a number in front of you instead of a guess.

    ``--keep-days`` splits each kind into rows stamped inside the window, rows
    stamped outside it, and rows this command could not date. **Undateable rows
    are reported separately and are never counted as old.** A row whose stamp
    cannot be read is a row nothing is known about, and "I cannot tell how old
    it is" must not be the reason it appears on a deletion list.
    """
    cutoff = _cutoff_iso(ctx, args.keep_days)
    kinds: dict[str, dict[str, int]] = {}
    for kind in GC_KINDS:
        rows = list(ctx.records.query(kind))
        if not rows:
            continue
        older = newer = undateable = 0
        for row in rows:
            stamp = str(row.get("at") or row.get("created_at") or "")
            if not stamp:
                undateable += 1
            elif stamp < cutoff:
                older += 1
            else:
                newer += 1
        kinds[kind] = {
            "total": len(rows),
            "older_than_keep_days": older,
            "within_keep_days": newer,
            "undateable": undateable,
        }
    _emit_line(
        {
            "instance_id": ctx.instance_id,
            "keep_days": args.keep_days,
            "cutoff": cutoff,
            "kinds": kinds,
            "deleted": 0,
            "note": (
                "nothing was deleted: selfloop's RecordStore port exposes no delete, because "
                "the records are the evidence the loop was graded on. Prune with your own "
                "backend's tools, and keep the outcome and evidence rows longest — they are "
                "the only durable account of what this loop actually produced."
            ),
        }
    )
    return EXIT_OK


def _cutoff_iso(ctx: LoopContext, keep_days: int) -> str:
    """The ISO stamp *keep_days* before the context's own clock reading.

    Derived from ``ctx.clock`` rather than from ``datetime.now`` so that a
    pinned clock — a replay, a backfill — produces a window consistent with the
    stamps on the rows it is comparing.
    """
    now = datetime.fromisoformat(ctx.clock.now_iso())
    return (now - timedelta(days=max(0, int(keep_days)))).isoformat()


def _cmd_counterfeit(args: argparse.Namespace) -> int:
    """Re-anchor one counterfeit entry. **Requires the test suite; not shipped here.**

    A counterfeit is a mutation of this package's source that MUST make a named
    test go red; re-anchoring is what you do when a refactor moved the line the
    mutation targets. It is a real operation and it is deliberately not
    implemented in this file, because doing it honestly means applying the
    mutation and running the named tests — which needs the corpus, the harness
    and pytest, none of which exist in an installed wheel.

    The refusal is the whole point. A ``--reanchor`` that quietly said "moved"
    without re-running the tests would let an anchor drift onto a line where the
    mutation no longer fails anything, and the corpus would keep reporting green
    while guarding nothing. That is the same shape as a vacuous gate, applied to
    the thing that checks the gates.
    """
    try:
        available = importlib.util.find_spec("tests.counterfeits.harness") is not None
    except (ImportError, ValueError):
        # ``find_spec`` imports the PARENT package to find a submodule, so it
        # raises rather than answering False when ``tests`` is not importable at
        # all — which is the ordinary case from an installed wheel, and the
        # answer we wanted was "no".
        available = False
    _emit_line(
        {
            "ok": False,
            "counterfeit": args.reanchor,
            "harness_importable": available,
            "error": (
                "re-anchoring runs the mutation and the tests it must break, so it lives with "
                "the corpus in the source tree, not in the installed package. Run it from a "
                f"checkout: `pytest tests/test_counterfeits.py --reanchor {args.reanchor}`. "
                "An anchor accepted without re-running the named tests is an anchor that may "
                "no longer make anything go red."
            ),
        },
        stream=sys.stderr,
    )
    return EXIT_REFUSED


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """The full command line. Exposed so a test can read it without running it."""
    parser = argparse.ArgumentParser(
        prog="selfloop",
        description="Run one durable, self-improving loop tick per process invocation.",
    )
    subs = parser.add_subparsers(dest="command", required=True)

    context_args = argparse.ArgumentParser(add_help=False)
    context_args.add_argument(
        "--tools",
        required=True,
        metavar="MODULE",
        help=(
            f"module defining {CONTEXT_FACTORY}(*, instance_id, template, params) -> "
            "LoopContext. This is the whole integration contract."
        ),
    )
    context_args.add_argument("--instance", required=True, help="loop instance id")
    context_args.add_argument("--template", required=True, help="template name")
    context_args.add_argument(
        "--allow-prefix",
        action="append",
        default=[],
        metavar="PREFIX",
        dest="allow_prefix",
        help="narrow which module prefixes --tools may name; repeatable",
    )

    tick = subs.add_parser("tick", parents=[context_args], help="run one tick")
    tick.add_argument("--params", default="", metavar="JSON", help="JSON object for this tick")
    tick.add_argument(
        "--accept-inprocess-lease",
        action="store_true",
        help=(
            "run even though the lease cannot exclude another OS process. Only pass this "
            "when you have established that no second process exists."
        ),
    )

    lessons = subs.add_parser("lessons", parents=[context_args], help="list stored lessons")
    lessons.add_argument("--scope", default="", help="restrict to one learning scope")

    approve = subs.add_parser("approve", parents=[context_args], help="decide one approval")
    approve.add_argument("approval_id", help="the approval row id from the parked tick")
    approve.add_argument("--by", required=True, help="the PERSON deciding; never an automation")
    approve.add_argument(
        "--decision",
        default=ApprovalState.APPROVED.value,
        choices=(ApprovalState.APPROVED.value, ApprovalState.REJECTED.value),
    )
    approve.add_argument("--note", default="", help="why, for the audit trail")

    rec = subs.add_parser("reconcile", parents=[context_args], help="settle an unknown effect")
    rec.add_argument("key", help="the ATTEMPT key, exactly as the error printed it")
    rec.add_argument("--outcome", required=True, choices=("succeeded", "failed"))
    rec.add_argument("--by", required=True, help="the PERSON who went and looked")
    rec.add_argument("--note", default="", help="what they found")

    gc = subs.add_parser("gc", parents=[context_args], help="size the store's retention problem")
    gc.add_argument("--keep-days", type=int, required=True, dest="keep_days")

    counterfeit = subs.add_parser("counterfeit", help="counterfeit-corpus maintenance")
    counterfeit.add_argument("--reanchor", required=True, metavar="ID")

    return parser


_CONTEXT_COMMANDS = {
    "tick": _cmd_tick,
    "lessons": _cmd_lessons,
    "approve": _cmd_approve,
    "reconcile": _cmd_reconcile,
    "gc": _cmd_gc,
}


def main(argv: Sequence[str] | None = None, *, allow_prefixes: Sequence[str] | None = None) -> int:
    """Parse *argv*, run one command, return the exit status.

    *allow_prefixes* is the module allowlist, and passing it is how an embedder
    that feeds this CLI from configuration keeps ``--tools`` from becoming an
    arbitrary-import hole. ``None`` — the default, and the right value for a
    person at a terminal — means unconstrained, because somebody typing
    ``--tools X`` into a shell already has whatever access that module would
    give them.

    Returns rather than exits, so that a test can call it and an embedder can
    wrap it. ``python -m selfloop`` turns the return value into a process exit
    status.
    """
    args = build_parser().parse_args(argv)

    if args.command == "counterfeit":
        return _cmd_counterfeit(args)

    allowed, complaint = _effective_prefixes(allow_prefixes, args.allow_prefix)
    if complaint:
        return _refuse(complaint)

    try:
        args.params_obj = _parse_params(getattr(args, "params", "") or "")
        ctx = _build_context(args, allowed, args.params_obj)
    except LoopError as exc:
        return _refuse(str(exc), command=args.command)

    handler = _CONTEXT_COMMANDS[args.command]
    try:
        return handler(ctx, args)
    except LoopError as exc:
        # A refusal the package raised deliberately — a reconciliation of a
        # settled row, a params object that is not an object. It is the
        # operator's answer, so it is printed as one and not as a traceback.
        return _refuse(f"{type(exc).__name__}: {exc}", command=args.command)


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())


__all__ = [
    "CONTEXT_FACTORY",
    "EXIT_OK",
    "EXIT_REFUSED",
    "EXIT_TICK_FAILED",
    "GC_KINDS",
    "build_parser",
    "main",
]
