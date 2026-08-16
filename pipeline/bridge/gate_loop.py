#!/usr/bin/env python3
"""bridge/gate_loop.py — the MECHANICAL GATE LOOP daemon (the lander).

THE landing bottleneck cure. An LLM should never watch a gate log: with a single
LLM lander serially assembling trains, dispatching a gate, watching it, and
ff-merging, landing caps at ~0-2/hr while a full verdict-approved queue sits and
the twin idles. This daemon removes the LLM from the gate/land loop entirely.
The LLM loops PRODUCE candidates (admit + claim + build + verdict); this
deterministic daemon LANDS them.

It is deterministic, which is the whole reason it may safely hold the
single-writer role the LLM held: given the same queue and the same `main`, every
tick schedules identically and the `main`-merge is serialised behind one O_EXCL
lockfile. No model, no prompt, no judgement that drifts run to run.

One tick does exactly this, all of it under the lock:

  1. Read non-terminal `candidates/`. Routine diffs flow directly to the
     mandatory mechanical gate. Diffs touching auth, money, migrations,
     permissions, gate/schema/policy, or other self-governing surfaces must
     additionally carry an approved cross-lineage verdict from build time.
  2. FORWARD-PORT them onto current `main` (a candidate whose base fell behind is
     replayed onto `main` as part of train assembly — the cherry-pick onto the
     `main`-rooted train branch IS the rebase-forward).
  3. Assemble file-disjoint candidates into linear trains (train_assembler.py,
     grouping via integration.conflict_groups, footprint from the REAL diff).
  4. Dispatch each train's gate DETACHED (a new session, so it survives this
     process), writing
     `state/gates/<train>@<tip>.json = {state:"running", deadline, pid}`. The
     first train runs from the local pinned workspace; a second, disjoint train
     is pinned to the twin, receives its candidate-bound receipt, and runs from
     the twin's pinned workspace so both boxes gate in parallel without ever
     scheduling two full gates back onto one Mac.
  5. Read a finished gate's result and classify it with the EXISTING
     `integration.classify_gate` — the SOLE verdict author. A missing or expired
     status file is an INSTRUMENT ERROR, never "no refusal" and never a pass
     (the favourable-absence guard); an unknown refusal slug fails closed to
     instrument-error too (classify_gate already does this).
  6. On PASS: ff-only merge to `main`, push, re-pin the gate workspace, append a
     `merged` event (with `merge_sha`) per member, write a receipt. On
     candidate-defect: `rejected` per member with class + reason + a mandatory
     TTL, plus `rejected/<id>.json`. On instrument-error: an `instrument_error`
     event + ONE inquiry (area: tooling), and re-gate ONCE.
  7. NEVER land two trains concurrently. Gates run concurrently; the ff-merge to
     `main` is serialised behind the single lockfile. THIS is the single-writer
     invariant, now held by deterministic code instead of an LLM.

It REUSES, and never reimplements: `integration.classify_gate` (the 64/90
instrument-vs-defect doctrine), `integration.conflict_groups` (the schedule),
the shared gate-host/remote-evidence helpers (pin + preflight + twin dispatch +
receipt sync), and `merge-gate.sh` (the gate itself).
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bridge import risk_tier  # noqa: E402
from bridge.canonical import content_id  # noqa: E402
from bridge.gate_host import (  # noqa: E402
    TWIN_HOST,
    TWIN_SPECS,
    busy_physical_hosts,
    effective_ladder_workers,
    pick_twin,
    preflight_remote,
    probe_remote_load,
    remote_gate_command,
    twin_spec,
)
from bridge.integration import (  # noqa: E402
    REJECT_TTL_DAYS,
    ROLE,
    Candidate,
    GateVerdict,
    _iso,
    _now,
    _parse,
    _short,
    _stem,
    classify_gate,
    git,
    is_ancestor_of_main,
)
from bridge.ledger_read import UNDECODABLE, iter_events, parse_events  # noqa: E402
from bridge.ledger_write import append_event  # noqa: E402
from bridge.remote_evidence import (  # noqa: E402
    pin_remote_candidate,
    sync_back_evidence,
    sync_forward_candidate_receipt,
)
from bridge.review_policy import approved_cross_lineage, risky_review_paths  # noqa: E402
from bridge.train_assembler import Train, assemble_trains, chain_groups  # noqa: E402

ACTOR = "gate-loop-daemon"
#: The durable member-isolation backstop event. Kept as a SINGLE constant so that
#: if it is ever promoted from an OFF-ENUM extension event to a first-class
#: ledger-schema enum value, only this one string changes — no scattered literals.
ISOLATED_EVENT = "isolated"
GATE_DEADLINE_S = 7200            # a running gate past this is an instrument timeout
# Floors for deadline-derived child bounds.  The effective values are calculated
# from the lease state file immediately before the gate command is run.
GATE_CHILD_TIMEOUT_S = 360
GATE_REMOTE_TIMEOUT_S = 300
# Orphan sweep grace past the recorded deadline before train-independent reap
# (design M3): trains that no longer assemble leave state files ~daily.
ORPHAN_SWEEP_GRACE_S = 300
TICK_SECONDS = 60
#: An unreadable NAMESPACED `iso-` carrier is provably a CLOSED audit record, not
#: a possibly-live 2h gate: quarantine it after ONE tick interval (the durable
#: ledger backstop already holds its membership, so nothing is lost). A GENERIC
#: gate-state file might be a running gate and keeps GATE_DEADLINE_S untouched.
ISO_UNREADABLE_QUARANTINE_S = TICK_SECONDS
#: Bound on the disposable build worktree's `git worktree add`. A normal add of
#: this ~5k-file checkout is ~2s; the old 60s default tripped on Mac load spikes,
#: and a timed-out add leaves git's own transient `initializing` lock behind that
#: wedged EVERY later tick until a human cleared it. 180s absorbs the spikes while
#: staying bounded so a genuinely stuck add never blocks the tick pathologically.
#: It doubles as the STALENESS window for reclaiming an orphaned `initializing`
#: lock: a marker older than the longest possible add cannot belong to a live add.
WORKTREE_ADD_TIMEOUT_S = 180
DEFAULT_OFFLOAD = "/Users/youruser/Work/Ops/bin/offload"
# One slot per BOX: this host plus every twin in the pool. Assembly may emit
# more disjoint trains than there are boxes; the extras dispatch on later ticks
# as slots free, rather than bursting N ~12-minute gates onto the fleet.
#
# This was hard-coded 2 while the pool had one member. It is derived now because
# the constant and the pool drifting apart fails in the expensive direction: a
# cap of 2 with two twins silently wastes a paid box (measured 2026-08-10: 728
# "gate slots full" deferrals across 12 trains while MW0002 sat at 0 agents),
# and a cap of 3 with one twin double-books the same box.
#
# `TWIN_SPECS` is now DECLARED in configs/gate-hosts.yaml, so adding a box is a
# config edit — which is exactly why the constant counts DISTINCT PHYSICAL
# MACHINES rather than config entries. gate_host collapses two names for one Mac
# before this line runs, and `scripts/gate-watch` reads THIS constant when it
# kills excess gates, so the two ceilings cannot disagree.
MAX_CONCURRENT_GATES = 1 + len(TWIN_SPECS)
# How many times ONE train tip may fail to push for the same instrument reason
# before its state is parked. A push that never succeeds is not a schedule miss:
# retrying it every tick until morning is the never-re-run-an-unchanged-input
# violation wearing a network costume, and it hides the outage instead of
# raising it (finding sha256:b1edeafa).
PUSH_RETRY_LIMIT = 5
class DaemonPoisoned(RuntimeError):
    """Local `main` may have diverged from origin and the daemon MUST NOT land
    anything more until a human reconciles. Raised after a push failure whose
    rollback ALSO failed (the double-failure path); a durable poison marker is
    written first, and every subsequent tick refuses at the top of run_once. The
    single writer never advances a possibly-diverged main on its own."""


def _full_sha(val: object) -> str | None:
    """The stripped full 40-hex commit sha, or None if ``val`` is not one."""
    if not isinstance(val, str):
        return None
    s = val.strip()
    return s if re.fullmatch(r"[0-9a-fA-F]{40}", s) else None


# PUSH-REFUSAL CLASSIFICATION (finding sha256:b1edeafa). A refused push is one
# of two completely different things, and treating them alike is what blocked
# every landing on 2026-08-11:
#
#   * REMOTE AHEAD (non-fast-forward) — expected the moment a PR merges on
#     GitHub. The push will be refused identically forever until OUR INPUT
#     changes; the fix is fetch + fast-forward + re-assemble, never a retry.
#   * NETWORK / AUTH — the instrument failed. The identical push is legitimate
#     once the path clears, so it retries, but under the bounded budget above.
#
# Git says which one it is in its own words, on stderr.
_PUSH_NON_FF_RE = re.compile(
    r"non-fast-forward|\(fetch first\)|updates were rejected because",
    re.IGNORECASE)
# `! [remote rejected] main -> main (pre-receive hook declined)` is NOT
# remote-ahead: the server refused the CONTENT. A fetch would find nothing to
# fast-forward, so re-anchoring cannot help and the bounded budget is the right
# instrument. Checked first because the same line also contains "rejected".
_PUSH_REMOTE_DECLINED_RE = re.compile(r"remote rejected", re.IGNORECASE)


def push_refusal_is_non_ff(text: str) -> bool:
    """True when git's refusal means THE REMOTE IS AHEAD OF US.

    False for everything else, including an unreadable/empty message: the
    conservative direction here is "not remote-ahead", because that answer costs
    one bounded retry, while a false "remote-ahead" would spend a re-anchor on a
    remote that never moved and could hide a real instrument outage.
    """
    if not text:
        return False
    if _PUSH_REMOTE_DECLINED_RE.search(text):
        return False
    return bool(_PUSH_NON_FF_RE.search(text))


def _receipt_verified(receipt: Path | None) -> bool:
    """A receipt is only evidence if it is present AND has usable content.

    Existence alone is not enough (cross-lineage review, Grok): a zero-byte or
    garbage file passes an `.exists()` check while proving nothing. The gate mints
    a JSON receipt object, so the minimum bar a pass must clear is: the file
    exists, is non-empty, and parses as a JSON object. This estate has no receipt
    SIGNATURE verifier inside ThreeLoops (receipts are minted in the target repo),
    so signature validation is deliberately out of scope here — but an empty or
    unparseable receipt is caught, which is the favourable-absence hole."""
    if receipt is None or not receipt.exists():
        return False
    try:
        raw = receipt.read_text(encoding="utf-8")
    except OSError:
        return False
    if not raw.strip():
        return False                     # zero-byte / whitespace-only == absence
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return False                     # garbage == absence
    return isinstance(data, dict) and bool(data)


# ------------------------------------------------------- proposal retirement
#
# ONE definition of what "this proposal shipped" looks like on the ledger,
# shared by the land-time half (`_terminalize_resolved_proposals`, below) and
# the one-shot backfill (`close_on_land.retire_proposals`). Two writers minting
# two shapes for the same fact is how a queue ends up unable to tell its own
# history apart, so the backfill imports THIS function rather than reproducing
# the dict.


#: The terminal event a retired proposal reaches. CONTRACT §8: "Artifacts are
#: immutable, so status is derived from the ledger, never stored" — there is no
#: tombstone file for a proposal that SHIPPED (`rejected/` is for dead ideas and
#: carries a mandatory TTL, which is exactly wrong for landed work), so the
#: ledger event IS the marker, and §10 sweeps the artifact 7 days after it.
#:
#: `completed` rather than `merged`, deliberately. The PROPOSAL never merges —
#: it has no branch and no head_sha; its CANDIDATE merged, and that candidate
#: already carries the `merged` event with `detail.merge_sha`. Stamping a second
#: `merged` on the proposal id would double-count the estate's merge-rate metric
#: (`known_traps` counts `merged` events) and would trip the integrity check
#: `park.unparked_precedes_merge` for any proposal that was ever parked.
#: `completed` is terminal in exactly the same way for every reader that
#: matters: `integration.LedgerView.terminal`, `spawn_builders`' selection,
#: `janitor`'s 7-day sweep, `pr_reconcile`, and `reconcile_queue`.
PROPOSAL_RETIRED_EVENT = "completed"

#: The ids a retirement decision is allowed to consult, at most, per artifact.
TERMINAL_EVENTS = ("merged", "completed", "rejected", "closed")

#: The ledger event schema, as the estate ships it. Loaded once, lazily, and
#: never fatal: the daemon must not stop landing because a schema file moved.
LEDGER_EVENT_SCHEMA = Path(__file__).resolve().parent.parent / "schema" / "ledger-event.schema.json"

_ROLES = frozenset({"planner", "reviewer", "implementer", "external"})
_LEDGER_EVENT_NAMES = frozenset({
    "found", "inquired", "answered", "proposed", "claimed", "released",
    "claim_expired", "submitted", "admitted", "gated", "merged", "completed",
    "rejected", "parked", "unparked", "isolated", "instrument_error", "closed"})
_RFC3339_Z = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z")


def ledger_event_problems(event: object) -> list[str]:
    """Every way `event` violates the ledger-event schema, in plain words.

    Two lenses, deliberately. `jsonschema` is authoritative when it imports,
    but this runs inside the LANDER: an environment without `jsonschema` must
    not silently stop validating (which is how the invalid event this lane
    exists to fix got written in the first place) and must not stop the daemon
    landing either. So the structural check below is complete on its own for
    the constraints that apply to a terminal event, and the library check is
    layered on top when it is available.
    """
    problems: list[str] = []
    if not isinstance(event, dict):
        return ["the event is not a JSON object"]
    ts, role, name = event.get("ts"), event.get("role"), event.get("event")
    if not isinstance(ts, str) or not _RFC3339_Z.fullmatch(ts):
        problems.append(f"ts {ts!r} is not RFC3339 UTC ending in Z")
    if role not in _ROLES:
        problems.append(f"role {role!r} is not one of {sorted(_ROLES)}")
    if name not in _LEDGER_EVENT_NAMES:
        problems.append(f"event {name!r} is not a known ledger event")
    ident = event.get("id")
    if name != "instrument_error" and (
            not isinstance(ident, str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", ident)):
        problems.append(f"id {ident!r} is not sha256:<64 lowercase hex>")
    base = event.get("base_sha")
    if base is not None and (not isinstance(base, str)
                             or not re.fullmatch(r"[0-9a-f]{40}", base)):
        problems.append(f"base_sha {base!r} is not 40 lowercase hex")
    detail = event.get("detail")
    if detail is not None and not isinstance(detail, dict):
        problems.append("detail is present but is not an object")
        detail = None
    det = detail or {}
    if name in ("merged", "completed", "rejected", "gated", "parked", "closed") \
            and not isinstance(detail, dict):
        problems.append(f"{name} requires a detail object")
    required = {"merged": ("merge_sha",), "completed": ("reason",),
                "rejected": ("reason", "class", "expires_at"),
                "gated": ("result", "receipt"), "parked": ("reason",),
                "closed": ("closed_by", "merge_sha", "bound_test")}.get(name, ())
    for field_name in required:
        if det.get(field_name) in (None, ""):
            problems.append(f"detail.{field_name} is required on {name}")
    if "result" in det and det["result"] not in ("pass", "fail"):
        problems.append(f"detail.result {det['result']!r} is not 'pass' or 'fail'")
    for sha_field in ("merge_sha", "closed_by"):
        value = det.get(sha_field)
        pattern = (r"[0-9a-f]{40}" if sha_field == "merge_sha"
                   else r"sha256:[0-9a-f]{64}")
        if value is not None and (not isinstance(value, str)
                                  or not re.fullmatch(pattern, value)):
            problems.append(f"detail.{sha_field} {value!r} is malformed")
    if "class" in det and det["class"] not in (
            "candidate-defect", "instrument-error", "blocked-on-human",
            "stale-base", "answered"):
        problems.append(f"detail.class {det['class']!r} is not a known class")
    if "remedy" in det and det["remedy"] not in ("replan", "drop", "blocked"):
        problems.append(f"detail.remedy {det['remedy']!r} is not a known remedy")
    for date_field in ("expires_at",):
        value = det.get(date_field)
        if value is not None and (not isinstance(value, str)
                                  or not _RFC3339_Z.fullmatch(value)):
            problems.append(f"detail.{date_field} {value!r} is not RFC3339 UTC")
    for int_field in ("exit_code", "attempts"):
        if int_field in det and not isinstance(det[int_field], int):
            problems.append(f"detail.{int_field} must be an integer")
    if "duration_s" in det and not isinstance(det["duration_s"], (int, float)):
        problems.append("detail.duration_s must be a number")
    if "alerted" in det and not isinstance(det["alerted"], bool):
        problems.append("detail.alerted must be a boolean")
    if problems:
        return problems
    try:                                    # the authoritative second lens
        import jsonschema  # noqa: PLC0415, I001 - optional, resolved at call time

        schema = json.loads(LEDGER_EVENT_SCHEMA.read_text(encoding="utf-8"))
    except Exception:                       # noqa: BLE001 - absence is not a pass
        return problems                     # ...but it is not a refusal either
    try:
        jsonschema.validate(event, schema)
    except jsonschema.ValidationError as exc:
        problems.append(f"schema: {exc.message}")
    except Exception as exc:                # noqa: BLE001
        problems.append(f"schema check could not run: {type(exc).__name__}: {exc}")
    return problems


def envelope_id_is_bound(art: object) -> bool:
    """True only if this envelope PROVES its own id: `content_id(payload) == id`.

    CONTRACT §7 makes the id the sha256 of the canonical payload BY
    CONSTRUCTION, which is the only reason an id can be trusted at all — the
    body is the content address of itself. An artifact whose id does not hash
    its payload is either hand-assembled, edited in place after filing, or
    forged, and there is no way to tell those three apart from the file.

    Why this matters HERE specifically: a retirement is the one operation that
    takes a claim made by artifact A (a candidate) and writes a TERMINAL,
    unrepairable event onto artifact B (a proposal) that nobody else vouched
    for. Trusting an unverified `id` field means a file dropped into
    `candidates/` under ANY name, claiming an already-merged candidate's id,
    picks an arbitrary live proposal out of the queue and kills it. So this is
    fail-closed on purpose: an unbound envelope retires NOTHING.

    Measured on the live queue 2026-08-12: 25 of 249 candidate envelopes are
    unbound (they were edited after filing), so this refusal has a real cost —
    those proposals keep being offered, which is exactly today's behaviour and
    the safe direction. Both call sites COUNT and NAME them so the envelopes
    can be re-filed rather than the refusal being invisible.
    """
    if not isinstance(art, dict) or "payload" not in art:
        return False
    ident = art.get("id")
    if not isinstance(ident, str):
        return False
    try:
        return content_id(art["payload"]) == ident.strip()
    except (TypeError, ValueError):
        return False               # an unhashable payload proves nothing


def envelope_identity_problem(art: object, expected_id: str) -> str | None:
    """None if this envelope IS the artifact `expected_id` names — else why not.

    TWO independent facts, and a self-consistency check alone silently skips
    the first (cross-lineage review, round 2, 2026-08-12):

      1. **THE BODY MUST BE THE ARTIFACT WE LOOKED IT UP AS.** Every reader
         here resolves an artifact by KEY — the daemon builds the path from the
         landed `member_id`, the backfill reads the id out of the filename — and
         then acts on the body's `resolves`. If the body's own `id` is never
         compared back to that key, the key is decoration: a file written at an
         already-merged candidate's path can name any id it likes.
      2. **THE BODY MUST HASH TO ITS OWN ID** (`envelope_id_is_bound`), so a
         body sitting at the right path cannot be edited into a different claim.

    Neither implies the other, and check 2 without check 1 is WORSE THAN IT
    LOOKS: a self-consistent forgery needs no preimage at all, because the
    attacker picks the payload and the id together — `{"id": content_id(P),
    "payload": P}` is trivially well-formed. Dropped at the path of a candidate
    that really merged, it passed, and the retirement it caused cited the real
    member id and the real merge sha as cover. The preimage barrier only exists
    when the id is pinned to a key the attacker does not choose.
    """
    if not isinstance(art, dict):
        return "the artifact is not a JSON object"
    ident = art.get("id")
    expected = (expected_id or "").strip()
    if not isinstance(ident, str) or not ident.strip():
        return "the artifact names no id"
    if ident.strip() != expected:
        return (f"body id {_short(ident.strip())} is not the "
                f"{_short(expected)} it was read as")
    if not envelope_id_is_bound(art):
        return "content_id(payload) != id"
    return None


def terminal_event_for(loops_root: Path, ident: str) -> dict | None:
    """The single terminal event recorded for `ident`, or None. Authoritative.

    A targeted re-read rather than a whole-queue replay, because it is called
    INSIDE the retirement lock immediately before the append and must be cheap
    enough to belong there: lines that cannot contain the id are skipped
    without being parsed (a JSON line naming this id must contain the literal
    id string), and the survivors go through the SHARED `iter_events`, which
    recovers two whole events spliced onto one physical line by two O_APPEND
    writers.

    It reproduces `integration.LedgerView`'s rules exactly, including the one
    reversal: first terminal wins, and an `unrejected` clears a `rejected`
    slot (only that one) so a legitimately re-opened proposal can still be
    retired later.
    """
    path = loops_root / "ledger.jsonl"
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    slot: dict | None = None
    for line in raw.splitlines():
        if ident not in line:
            continue
        for ev in iter_events(line):
            if ev.get("id") != ident:
                continue
            event = ev.get("event")
            if event in TERMINAL_EVENTS:
                if slot is None:
                    slot = ev
            elif event == "unrejected" and isinstance(slot, dict) \
                    and slot.get("event") == "rejected":
                slot = None
    return slot


@contextlib.contextmanager
def retirement_lock(loops_root: Path):
    """Serialize the whole check-and-append of a retirement, across processes.

    THE RACE THIS CLOSES (cross-lineage review, 2026-08-12): the
    exactly-one-terminal-event guard used to be a pre-read taken before the
    ledger lock, while `ledger_write` holds `locks/ledger.lock` only around the
    byte append. Two retirement writers -- the daemon and a backfill, or two
    backfills -- could therefore both observe "not terminal" and both append,
    and an append-only ledger cannot take the second one back.

    It is a SEPARATE lock file rather than `locks/ledger.lock` because the
    transport's lock is not re-entrant (it takes a process-local
    `threading.Lock` before the `flock`), so holding it across a decision and
    then calling `append_event` inside would deadlock -- and reimplementing the
    append under our own lock would make a SECOND WRITER of the ledger, which
    is the thing this whole subsystem exists to avoid. So the transport is
    unchanged and still the only writer; this is an outer mutex over the
    DECISION.

    Lock ordering is fixed and one-way: `proposal-retire.lock` is only ever
    taken OUTSIDE `ledger.lock`, never the reverse, so the two cannot cycle.

    Honest bound: this serializes the retirement writers, which are the ones
    that make this decision. A different producer appending its own terminal
    for the same proposal at the same instant is still possible; nothing short
    of the transport itself offering check-and-append can exclude that, and
    that file is not in this lane's scope.
    """
    locks_dir = loops_root / "locks"
    locks_dir.mkdir(parents=True, exist_ok=True)
    lock_path = locks_dir / "proposal-retire.lock"
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0), 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def retire_proposal_once(loops_root: Path, proposal_id: str, *, resolved_by: str,
                         carrier_sha: str, receipt: str | None = None,
                         train: str | None = None, actor: str = ACTOR,
                         append: Callable[[dict], object] | None = None) -> dict | None:
    """Mint and append ONE proposal retirement, at most once, ever.

    The terminal re-check happens INSIDE `retirement_lock`, immediately before
    the append, so a pre-read taken by the caller can only ever save work --
    it can never authorize the write. Returns the event that was appended, or
    `None` if the proposal was already terminal when the lock was held (the
    idempotent no-op; the caller decides what to log).

    `append` lets the daemon keep writing through its own `_append_ledger`,
    which carries the write-boundary guards. It defaults to the one sanctioned
    transport. Either way there is exactly one writer of ledger bytes.
    """
    if append is None:
        def append(event: dict) -> object:
            return append_event(loops_root, event)
    event = proposal_retirement_event(
        proposal_id, resolved_by=resolved_by, carrier_sha=carrier_sha,
        receipt=receipt, train=train, actor=actor)
    with retirement_lock(loops_root):
        if terminal_event_for(loops_root, event["id"]) is not None:
            return None
        append(event)
    return event


def proposal_retirement_event(proposal_id: str, *, resolved_by: str,
                              carrier_sha: str, receipt: str | None = None,
                              train: str | None = None,
                              actor: str = ACTOR) -> dict:
    """The ledger event that retires one proposal, or `ValueError`.

    Refuses rather than minting a degraded record, because an append-only log
    cannot be corrected later:

      * `carrier_sha` must be a full 40-hex commit. **A landing nobody can NAME
        is not evidence** (`close_on_land`'s sharpest refusal, reused): retiring
        a proposal against an unnameable carrier hides shipped work behind a
        commit no reader can go and check.
      * `proposal_id` / `resolved_by` must be canonical artifact ids.

    `detail.reason` is schema-MANDATORY on `completed` and is written to be
    read by a human who is asking why their plan vanished, so it names the
    candidate and the commit in prose. `detail.result` is deliberately ABSENT:
    the ledger schema's `result` is an enum of exactly `pass`/`fail`, so the
    free-text value this once carried made every retirement line fail
    validation while looking fine to the naked eye.
    """
    sha = _full_sha(carrier_sha)
    if sha is None:
        raise ValueError(
            f"refusing to retire {proposal_id!r}: {carrier_sha!r} is not a nameable "
            "landing commit (a full 40-hex sha) — a landing nobody can NAME is not "
            "evidence that the work shipped")
    for label, ident in (("proposal", proposal_id), ("resolver", resolved_by)):
        if not isinstance(ident, str) or not _ARTIFACT_ID_RE.fullmatch(ident.strip()):
            raise ValueError(
                f"refusing to retire on a malformed {label} id {ident!r}")
    detail: dict = {
        "reason": (f"resolved by candidate {_short(resolved_by)}, which landed on "
                   f"main at {sha[:12]}; the plan shipped, so it is no longer "
                   "offered to builders"),
        "resolved_by": resolved_by.strip(),
        "carrier_sha": sha,
    }
    if receipt:
        detail["receipt"] = receipt
    if train:
        detail["train"] = train
    return {"ts": _iso(_now()), "role": ROLE, "event": PROPOSAL_RETIRED_EVENT,
            "id": proposal_id.strip(), "actor": actor, "detail": detail}


# ------------------------------------------------------------ closure binding
#
# THE SELF-CLOSING LOOP, emit half (CLOSURE-CONTRACT §5.5). A finding is CLOSED
# only when the candidate that resolves it lands AND the test that finding was
# bound to ran GREEN on the merged tree in that same gate run. Everything below
# exists to carry one chain — candidate -> proposal -> (finding, failing test) —
# from the queue, through the gate's argv, into the `closed` event, without ever
# letting a missing link read as a favourable one.


#: The canonical content-address shape of every queue artifact id. Used to
#: resolve `resolves` into a FILENAME, so a malformed/hostile value can never
#: walk out of `proposals/` (`_stem` alone would happily carry `../`).
_ARTIFACT_ID_RE = re.compile(r"sha256:[0-9a-f]{64}")

#: Gate failure names that mean "the binding was not satisfied". `fail
#: "bound-test"` exits 1, so these arrive inside the VERDICT reason rather than
#: as a refusal slug; `bad-bound-test` comes through `refuse()` (exit 2) and is
#: listed for completeness. See `_bound_test_refusal`.
BOUND_TEST_FAILURES = frozenset({
    "bound-test",                 # the bound node ran and did not pass (or did not execute)
    "bound-test-untouched",       # the candidate edited its own bound test / its conftest chain
    "bound-test-unclassifiable",  # the run produced no judgeable status for the node
    "bad-bound-test",             # the binding itself was not a <file>::<test> in the merge base
})


class Binding(NamedTuple):
    """One resolved closure chain: which finding this member closes, and the
    test that proves it.

    `member_id` is the CANDIDATE (what lands), `finding_id` is what `closed` is
    emitted ON, `node_id` is the pytest node the gate re-runs on the merged tree.
    A Binding only ever exists when all three parsed; a partial chain is no
    binding at all, never a guess.
    """

    member_id: str
    finding_id: str
    node_id: str


def _valid_node_id(value: object) -> str | None:
    """The node id as merge-gate.sh will accept it, or None.

    Refusing a malformed binding HERE is not pedantry, it is the difference
    between "this candidate closes nothing" and "this candidate can never land":
    merge-gate.sh `refuse()`s a value it cannot use (`bad-bound-test`, exit 2),
    which classifies to instrument-error and re-gates. Three specific shapes are
    load-bearing:

      * a leading `-` is read as a missing value by the shell's own `''|-*` guard;
      * BOUND_TESTS is NEWLINE-delimited in the script, so an embedded newline
        smuggles in a second, unaudited binding;
      * `<file>::<test>` is the whole contract — a bare file or directory names
        a suite, not the one test a finding is bound to.
    """
    if not isinstance(value, str):
        return None
    node = value.strip()
    if not node or node.startswith("-"):
        return None
    if any(ch in node for ch in ("\n", "\r", "\t")):
        return None
    head, sep, tail = node.partition("::")
    if not sep or not head.strip() or not tail.strip():
        return None
    return node


def gate_supports_bound_test(gate_workspace: Path, *, ref: str | None = None) -> bool:
    """Does the merge-gate.sh that will ACTUALLY RUN accept `--bound-test`?

    THE SAFETY GATE of this whole change. The shell half of the closure contract
    (§5.1-5.3) is a SEPARATE candidate, so the pinned workspace may still carry a
    script whose argument loop ends in
    `-*) echo "refusing: unknown-flag — $1" >&2; exit 2`. That slug is in neither
    classification table, so every gated train would classify instrument-error,
    re-gate once, and park: passing an unsupported flag does not degrade closure,
    it stops LANDING. Merge rate is the scarce resource here, so this probe fails
    CLOSED in every direction — unreadable script, missing file, missing blob —
    and the loop simply gates without bindings, exactly as it does today.

    `ref` reads the blob at a commit instead of the working file. The twin is
    checked out to the minted MERGE-BASE, which can be older than the local
    pinned workspace's HEAD, so the remote path must ask about the tree the twin
    will run, not the one the daemon can see.
    """
    try:
        if ref:
            rc, out, _ = git(gate_workspace, "show", f"{ref}:scripts/merge-gate.sh")
            return rc == 0 and "--bound-test" in out
        script = Path(gate_workspace) / "scripts" / "merge-gate.sh"
        return "--bound-test" in script.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def _receipt_fields(receipt: Path | None) -> dict | None:
    """The run receipt's field map, or None when there is nothing to read.

    merge-gate.sh mints the signed run receipt FLAT (`bound_test` and
    `bound_test_result` are top-level keys — verified against the live evidence
    store and against `mint_run_receipt`), while the plan's own acceptance
    snippet reads a nested `payload`. Accept both shapes so a future wrapper
    cannot silently turn every green into a withheld closure — and return None,
    never `{}`, when the file is absent/garbage: absence is not green.
    """
    if receipt is None or not Path(receipt).exists():
        return None
    try:
        data = json.loads(Path(receipt).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    inner = data.get("payload")
    return inner if isinstance(inner, dict) else data


def _bound_test_refusal(verdict: GateVerdict) -> str | None:
    """The bound-test failure name behind a candidate-defect verdict, or None.

    `fail "bound-test" …` puts the name in FAILURES and the gate exits 1, so
    classify_gate labels the verdict `verdict` and the NAME survives only inside
    the reason it builds from the gate's `  - <name>: <detail>` lines. Matching
    on the slug alone would therefore never fire on a real bound-test refusal —
    an enrichment that never fires is indistinguishable from one that had
    nothing to enrich, which is the favourable-absence shape this file guards
    against everywhere else. Parsed field-wise (`<name>: <detail>` clauses)
    rather than by substring, so the words "bound-test" inside someone's prose
    cannot mislabel an unrelated refusal.
    """
    if verdict.slug in BOUND_TEST_FAILURES:
        return verdict.slug
    reason = (verdict.reason or "").split("gate verdict FAIL:", 1)[-1]
    for clause in reason.split(";"):
        name = clause.strip().split(":", 1)[0].strip()
        if name in BOUND_TEST_FAILURES:
            return name
    return None


# ---------------------------------------------------------------------- lock


class Lock:
    """The single-writer lock. O_EXCL at `locks/gate-loop.lock`.

    Exactly one gate-loop tick runs at a time. Because the ff-merge to `main`
    happens only inside a tick that holds this lock, two trains can never
    ff-merge concurrently — the merge is serialised behind this one file. A
    stale lock (holder pid gone) is stolen; a live holder makes us EXIT rather
    than proceed "just to check", because proceeding would mean two writers on
    `main`. Modelled on integration.Lock.
    """

    def __init__(self, path: Path) -> None:
        self.path, self.held = path, False

    def __enter__(self) -> Lock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for attempt in (1, 2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
                os.write(fd, json.dumps({"pid": os.getpid(), "at": _iso(_now())}).encode())
                os.close(fd)
                self.held = True
                return self
            except FileExistsError:
                if attempt == 2:
                    break
                try:
                    holder = json.loads(self.path.read_text()).get("pid")
                    os.kill(int(holder), 0)
                except PermissionError:
                    break                                  # alive, another user
                except (OSError, ValueError, json.JSONDecodeError, TypeError):
                    self.path.unlink(missing_ok=True)      # holder gone: stale
                    continue
                break
        raise SystemExit(
            f"another gate-loop instance holds {self.path} — exiting rather than "
            "landing a second train concurrently (single-writer invariant)")

    def __exit__(self, *exc) -> None:
        if self.held:
            self.path.unlink(missing_ok=True)


# ------------------------------------------------------------------ gate I/O


def gate_state_path(root: Path, train: Train) -> Path:
    """`state/gates/<train>@<tip>.json`. `/` in the branch name is flattened so
    the key is one file, and the tip is IN the key so a moved tip is a new gate,
    never a stale result read against fresh code."""
    safe = train.branch.replace("/", "__")
    return root / "state" / "gates" / f"{safe}@{train.tip}.json"


def iso_state_path(root: Path, train: Train) -> Path:
    """`state/gates/iso-<train>@<tip>.json` — the isolation carrier, DELIBERATELY
    namespaced with an ``iso-`` prefix so it is categorically distinguishable
    from a running-gate lease file (`<train>@<tip>.json`). The occupancy parser
    excludes ``iso-*`` from lease counting, so an unreadable isolation record can
    never masquerade as a live 2h gate and wedge dispatch. It is a `state:closed`
    audit record, not a lease; the durable membership truth lives in the ledger's
    ``isolated`` events, of which this file is only the readable mirror."""
    safe = train.branch.replace("/", "__")
    return root / "state" / "gates" / f"iso-{safe}@{train.tip}.json"


def read_gate_verdict(state_file: Path, *, now: float | None = None) -> GateVerdict | None:
    """Turn a gate-state file into a verdict, or None if the gate is still running.

    THE favourable-absence guard lives here:

      * file MISSING -> instrument-error. A status that should exist and does not
        says nothing about the code; it must never read as pass or "no refusal".
      * state == running AND past its deadline -> instrument-error (timeout). A
        gate that never reported is a host/tooling fact.
      * state == running, within deadline -> None (genuinely still in flight).
      * state == done -> integration.classify_gate is the SOLE author of the
        verdict (pass / candidate-defect / instrument-error; unknown slug already
        fails closed to instrument-error inside it).
    """
    now = time.time() if now is None else now
    if not state_file.exists():
        return GateVerdict("instrument-error", 0, "status-missing",
                           f"gate status file {state_file.name} is absent — a result that "
                           "should exist and does not says nothing about the code; it is "
                           "NEVER a pass (favourable-absence guard)", None, "", 0.0)
    try:
        st = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return GateVerdict("instrument-error", 0, "status-unreadable",
                           f"gate status file {state_file.name} is unreadable ({exc}) — "
                           "instrument fact, never a pass", None, "", 0.0)
    state = st.get("state")
    if state == "running":
        deadline = st.get("deadline")
        if isinstance(deadline, (int, float)) and now > deadline:
            return GateVerdict("instrument-error", 124, "gate-deadline-expired",
                               f"gate still 'running' past its deadline ({int(now - deadline)}s "
                               "over) — an instrument timeout, says nothing about the code; "
                               "re-gate, never treat as pass", None, "", 0.0)
        return None
    if state != "done":
        return GateVerdict("instrument-error", 0, "status-bad-state",
                           f"gate status state={state!r} is neither 'running' nor 'done' — "
                           "instrument fact, never a pass", None, "", 0.0)
    rc = st.get("rc")
    if not isinstance(rc, int):
        return GateVerdict("instrument-error", 0, "status-no-rc",
                           "gate reported done with no integer rc — the receipt did not come "
                           "home; a verdict without its exit code is prose, never a pass",
                           None, "", 0.0)
    receipt_str = st.get("receipt")
    receipt = Path(receipt_str) if receipt_str else None
    # PASS-WITHOUT-RECEIPT GUARD (cross-lineage review, Gemini + Grok).
    # classify_gate returns `pass` on rc==0 UNCONDITIONALLY — it does not check
    # the receipt on the pass path (integration.py: `if rc == 0: return pass`).
    # So an `offload gate` that exits 0 but left no USABLE signed receipt (a
    # crash after the suites, a disk-full, a truncated write, a script bug) would
    # otherwise land UNGATED code on main. A pass is only believable with a
    # VERIFIABLE receipt home: rc==0 with a receipt that is missing, empty, or
    # not a parseable receipt object is an INSTRUMENT ERROR, never a pass — an
    # empty or garbage receipt is ABSENCE wearing a costume, not evidence.
    if rc == 0 and not _receipt_verified(receipt):
        return GateVerdict(
            "instrument-error", 0, "pass-no-receipt",
            "gate exited 0 but its receipt is missing/empty/unparseable — a pass without a "
            "verifiable signed receipt is prose, not evidence; NEVER land on it "
            "(favourable-absence guard)",
            None, "", float(st.get("duration_s") or 0.0))
    return classify_gate(rc, st.get("stdout", ""), st.get("stderr", ""),
                         receipt, float(st.get("duration_s") or 0.0))


def local_gate_command(gate_workspace: Path, candidate: str, receipt: str,
                       bound_tests: list[str] | None = None) -> list[str]:
    """The argv for a LOCAL gate run, executed FROM THE PINNED WORKSPACE.

    The script is `$GATE_WS/scripts/merge-gate.sh` — the workspace's OWN copy —
    NOT a repo-relative or hardcoded path. merge-gate.sh has a self-identity
    guard (scripts/merge-gate.sh): it hashes the executing script ($0) and, in
    the workspace-pin step, compares that against the blob the pinned workspace
    carries at HEAD (`$PIN_SHA:scripts/merge-gate.sh`). A caller that runs some
    OTHER copy — e.g. the serving repo's `{repo}/scripts/merge-gate.sh`, which is
    exactly what `offload gate` hardcodes on its LOCAL branch — grades a
    correctly-pinned workspace with a foreign/stale judge, and the gate refuses
    `stale-gate-script` (rc 2) BY DESIGN. That is the loop the daemon was stuck
    in: every local tick self-refused rc 2 and re-gated forever.

    Running the workspace's own copy makes $0 == the pinned blob, so the identity
    check passes and the gate actually grades (PASS / candidate-defect). This is
    verbatim the remedy merge-gate.sh names: "invoke the gate FROM the workspace
    it grades — bash $GATE_WS/scripts/merge-gate.sh <candidate>". The twin path
    (bridge/gate_host.remote_gate_command) already runs `bash {workspace}/scripts/
    merge-gate.sh` from the twin's own pinned workspace, so it was never affected;
    only offload's local branch was, and this replaces it for local gates.

    `bound_tests` is REPEATED, one flag per binding: a train grades one merged
    tree but carries up to N members, and collapsing N bindings into one flag
    would grade N-1 members as if they had no binding at all — a false GREEN of
    exactly the class the closure contract exists to remove.
    """
    argv = ["bash", str(Path(gate_workspace) / "scripts" / "merge-gate.sh"),
            "--candidate", candidate, "--emit-receipt", receipt]
    for node in bound_tests or ():
        argv += ["--bound-test", node]
    return argv


# --------------------------------------------------------------- the daemon


class IsolationVeto(Exception):
    """A fail-closed isolation-scan veto: dispatch nothing, degrade the tick, and
    alert per-offending-file (a NEW offending file always re-alerts; a repeat of
    the same one does not storm). `.path` names the file that triggered the veto
    so the alert dedup is keyed per record, never global — a second, independently
    broken record can never be starved of its alert.

    Only a readable-but-MALFORMED carrier vetoes (below). An UNREADABLE carrier no
    longer vetoes the tick: the durable ledger backstop holds its membership and
    the file is short-quarantined, so one corrupt file cannot wedge all landings."""

    def __init__(self, message: str, *, path: str) -> None:
        super().__init__(message)
        self.path = path


class MalformedIsolation(IsolationVeto):
    """A readable ``iso-`` carrier whose isolation shape is unusable (no canonical
    member IDs, or a non-hex/absent source base/tip). Never an empty set: an empty
    set would let the very members the isolation exists to protect re-batch into
    another train-wide verdict. A clear, actionable corruption — hence still a
    fail-closed tick veto."""


class LedgerUnreadable(Exception):
    """The ledger file itself could not be READ AT ALL (OSError) — distinct from a
    single torn line inside a readable ledger. This is NOT an `IsolationVeto`: the
    caller decides whether it is fatal, VETOING only when there is ALSO no readable
    carrier to serve as the backstop (no usable isolation source at all). A single
    torn/undecodable line or one malformed event is SKIPPED, never raised — the
    ledger is append-only and never rewritten, so raising on one bad line would
    halt every landing forever (round-5 availability blocker)."""


@dataclass
class Outcome:
    train: Train
    action: str                    # dispatched | waiting | landed | rejected | instrument | skipped | isolated
    detail: str = ""


@dataclass
class GateLeases:
    """Occupancy facts from gate-state files only (I4').

    Produced by the ONE parser that both `_running_gate_count` and
    `_twins_in_flight` consult. A corrupt gate-state is busy both directions
    (running ≥ 1 and every active twin) until its recorded veto clock plus
    ``GATE_DEADLINE_S``, then
    quarantined. Expired-but-present running state still counts — a passed
    deadline does not prove the child stopped; the slot frees only after reap.
    """
    running: int = 0
    twins: set = field(default_factory=set)
    corrupt: bool = False


def _pid_lstart(pid: int) -> str | None:
    """The kernel's start-time stamp for ``pid``.

    Returns the lstart string when the process is live and readable; ``None``
    when the pid is absent. Raises ``OSError`` when the probe itself fails
    (permission/sandbox/timeout) so the caller can distinguish ``pid-absent``
    (may free) from ``unverifiable`` (must NOT kill, must NOT pretend free).

    Used as the identity check before any killpg backstop (I8): pid recycling
    over a multi-hour deadline is real on a Mac that spawns many session-leader
    children, and killpg against a recycled pid hits an innocent victim.
    """
    try:
        proc = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            capture_output=True, text=True, check=False, timeout=5)
    except subprocess.TimeoutExpired as exc:
        raise OSError(f"ps lstart timed out for pid {pid}") from exc
    # PermissionError is an OSError subclass — re-raise so reap can mark
    # unverifiable rather than treating a blocked probe as "pid gone".
    if proc.returncode != 0:
        return None
    stamp = (proc.stdout or "").strip()
    return stamp or None


class GateLoop:
    def __init__(self, root: Path, repo: Path, *, gate_ws: Path | None = None,
                 offload_bin: str = DEFAULT_OFFLOAD, repo_key: str = "omniagentos",
                 remote: str | None = "origin", allow_remote_gate: bool = False,
                 push: bool = True, python: str | None = None) -> None:
        self.root = root
        self.repo = repo
        self.gate_ws = gate_ws
        self.offload_bin = offload_bin
        self.repo_key = repo_key
        self.remote = remote
        self.allow_remote_gate = allow_remote_gate
        self.push = push
        self.python = python or sys.executable
        self.lines: list[str] = []
        self.alerts: list[str] = []
        # candidate id -> its resolved closure chains, filled by load_candidates.
        # Held per-TICK on purpose: run_once re-loads candidates before it reads
        # any gate result, so every member of every train judged in a tick has
        # its bindings in hand, and a stale binding from an earlier tick can
        # never be spent on a later merge.
        self.bindings: dict[str, list[Binding]] = {}

    # -- primitives ----------------------------------------------------------

    def _log(self, msg: str) -> None:
        self.lines.append(msg)

    def _alert(self, msg: str) -> None:
        self.alerts.append(msg)
        try:
            with open(self.root / "ALERTS.md", "a", encoding="utf-8") as fh:
                fh.write(f"- {_iso(_now())} gate-loop: {msg}\n")
        except OSError:
            pass

    def _edge_alert(self, key: str, msg: str) -> None:
        """Alert ONCE per distinct `key`, then stay quiet — a persistent condition
        (e.g. a torn line in an append-only ledger, re-read every tick) must not
        spam an alert every tick. Same edge-trigger discipline as the per-file
        isolation-veto alert."""
        marker = self._alert_marker_path(key)
        if marker.exists():
            return
        try:
            self._write_json_atomic(marker, {"key": key, "at": time.time()})
        except OSError:
            pass
        self._alert(msg)

    def _append_ledger(self, event: dict) -> None:
        """One event, written with a single os.write(2) to an O_APPEND fd.

        Buffered library I/O may split the line, and a split line is a torn
        ledger for every later reader; fsync before returning so the event is
        durable. A terminal 'rejected' event is refused at THIS boundary if its
        class is instrument-error/blocked-on-human — instrument conditions are
        never terminalised (CONTRACT §1), enforced at the write, not per caller.
        """
        if event.get("event") == "closed":
            # THE WORST OUTCOME OF THE WHOLE CLOSURE PLAN is a `closed` minted on
            # a test nobody measured, so the write boundary refuses one that
            # cannot name its own provenance — the same shape as the `rejected`
            # guard below, and the same argument: enforce at the write, not per
            # caller. This is the second line; `_emit_closures` is the first.
            det = event.get("detail") if isinstance(event.get("detail"), dict) else {}
            missing = [k for k in ("closed_by", "merge_sha", "bound_test")
                       if not det.get(k)]
            if missing or not event.get("id"):
                self._alert(
                    f"REFUSED WRITE: a 'closed' event for {_short(event.get('id') or '')} "
                    f"without {', '.join(missing) or 'an id'} — closure is only ever "
                    "recorded with the finding, the candidate that closed it, the merge "
                    "it landed in, and the test that proved it")
                raise ValueError(
                    f"refused to write 'closed' missing {missing or ['id']}")
        if event.get("event") == "completed":
            # A `completed` is TERMINAL and the ledger is append-only, so an
            # invalid one is invisible to every validating reader forever. A
            # presence check on `detail.reason` was not enough (cross-lineage
            # review, 2026-08-12): the event that started this whole lane was
            # invalid on a DIFFERENT field (`detail.result` outside the pass|fail
            # enum) and would still have been written. So the whole schema is
            # the bar here, and the refusal names the field that failed.
            problems = ledger_event_problems(event)
            if problems:
                detail = "; ".join(problems)
                self._alert(
                    f"REFUSED WRITE: a 'completed' event for "
                    f"{_short(event.get('id') or '')} that the ledger schema "
                    f"rejects — {detail}. A terminal event no reader can parse is "
                    "worse than none, and an append-only log cannot take it back.")
                raise ValueError(f"refused to write invalid 'completed': {detail}")
        if event.get("event") == "rejected":
            det = event.get("detail")
            cls = det.get("class") if isinstance(det, dict) else None
            if cls in ("instrument-error", "blocked-on-human"):
                self._alert(
                    f"REFUSED WRITE: a terminal 'rejected' with class={cls!r} for "
                    f"{_short(event.get('id', ''))} — instrument/blocked conditions are "
                    "never terminalised (CONTRACT §1)")
                raise ValueError(
                    f"refused to write terminal 'rejected' with class={cls!r}")
        self.root.mkdir(parents=True, exist_ok=True)
        append_event(self.root, event)

    def _write_json_atomic(self, path: Path, obj: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)

    def _alert_marker_path(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self.root / "state" / "alert-markers" / digest

    def _read_marker(self, path: Path) -> dict:
        try:
            marker = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return {}
        return marker if isinstance(marker, dict) else {}

    def _clear_veto_marker(self, path: Path) -> None:
        self._alert_marker_path(f"gate-veto:{path}").unlink(missing_ok=True)

    def _gc_veto_markers(self) -> None:
        """Drop bounded-veto markers whose state-file episode is gone."""
        marker_dir = self.root / "state" / "alert-markers"
        if not marker_dir.is_dir():
            return
        for marker_path in marker_dir.iterdir():
            marker = self._read_marker(marker_path)
            state_path = marker.get("path")
            if (isinstance(marker.get("veto_started_at"), (int, float))
                    and isinstance(state_path, str)
                    and not Path(state_path).exists()):
                marker_path.unlink(missing_ok=True)

    def _veto_started_at(self, path: Path, st: dict | None, *, kind: str,
                         now: float, raw: str | None = None) -> float:
        """Return the immutable start of this bounded veto.

        Valid state JSON carries the clock itself.  Corrupt JSON cannot safely
        be edited, so its equivalent durable clock lives in the alert-marker
        record.  Neither record is touched when a later tick merely observes
        the same condition.
        """
        field = "parked_at" if isinstance(st, dict) and \
            st.get("disposition") == "reap-unverifiable-parked" else "veto_started_at"
        if isinstance(st, dict) and isinstance(st.get(field), (int, float)):
            return float(st[field])
        marker_path = self._alert_marker_path(f"gate-veto:{path}")
        marker = self._read_marker(marker_path)
        digest = hashlib.sha256((raw or "").encode("utf-8")).hexdigest()
        try:
            stat = path.stat()
            inode, ctime_ns = stat.st_ino, stat.st_ctime_ns
        except OSError:
            marker_path.unlink(missing_ok=True)
            inode, ctime_ns = None, None
        if (marker.get("kind") == kind and marker.get("digest") == digest
                and marker.get("inode") == inode
                and marker.get("ctime_ns") == ctime_ns
                and isinstance(marker.get("veto_started_at"), (int, float))):
            return float(marker["veto_started_at"])
        if isinstance(st, dict):
            # Upgrade legacy state exactly once.  Its old mtime is sampled into
            # immutable JSON before any subsequent observation could alter it.
            try:
                started_at = path.stat().st_mtime
            except OSError:
                started_at = now
            st[field] = started_at
            self._write_json_atomic(path, st)
            return started_at
        self._write_json_atomic(marker_path, {
            "kind": kind, "digest": digest, "veto_started_at": now,
            "path": str(path), "inode": inode, "ctime_ns": ctime_ns,
            "alert_emitted": False,
        })
        return now

    def _record_corrupt_veto(self, path: Path, started_at: float, *, kind: str) -> None:
        """Durably announce the first tick a bounded state-file veto is active."""
        marker_path = self._alert_marker_path(f"gate-veto:{path}")
        marker = self._read_marker(marker_path)
        if marker.get("alert_emitted"):
            return
        # A valid state keeps its clock in the state file; this marker is solely
        # the cross-process edge trigger for the alert and ledger emission.
        marker.update({"kind": kind, "veto_started_at": started_at,
                       "path": str(path), "alert_emitted": True})
        self._write_json_atomic(marker_path, marker)
        until = started_at + GATE_DEADLINE_S
        msg = (f"{kind} gate-state {path.name} — fail-closed occupancy until "
               f"{datetime.fromtimestamp(until, UTC).isoformat()}")
        self._alert(msg)
        self._append_ledger({
            "ts": _iso(_now()), "role": ROLE, "event": "instrument_error",
            "id": None, "actor": ACTOR,
            "detail": {
                "class": "instrument-error", "area": "gate-instrument",
                "kind": "gate-state-veto-start", "path": str(path),
                "veto_kind": kind, "veto_until": until, "reason": msg,
            },
        })

    def _quarantine_gate_state(self, path: Path, *, kind: str) -> None:
        """Record an expired veto quarantine, then rename its state artifact."""
        ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        dest = path.with_name(f"{path.name}.corrupt-{ts}")
        msg = (f"quarantining {kind} gate-state {path.name} → {dest.name}: "
               "fail-closed occupancy has reached its bounded exit")
        self._append_ledger({
            "ts": _iso(_now()), "role": ROLE, "event": "instrument_error",
            "id": None, "actor": ACTOR,
            "detail": {
                "class": "instrument-error", "area": "gate-instrument",
                "kind": "gate-state-quarantined", "path": str(path),
                "quarantine": str(dest), "veto_kind": kind, "reason": msg,
            },
        })
        try:
            path.rename(dest)
        except OSError as exc:
            failed = f"quarantine rename failed for {path.name}: {exc}"
            self._alert(failed)
            self._log(failed)
            self._append_ledger({
                "ts": _iso(_now()), "role": ROLE, "event": "instrument_error",
                "id": None, "actor": ACTOR,
                "detail": {"class": "instrument-error", "area": "gate-instrument",
                           "kind": "gate-state-quarantine-failed", "path": str(path),
                           "quarantine": str(dest), "veto_kind": kind,
                           "reason": failed},
            })
            return
        self._clear_veto_marker(path)
        self._alert(msg)
        self._log(msg)

    def _terminal_ids(self) -> set[str]:
        """Ids that already reached a terminal event (merged/completed/rejected/closed).

        Read fresh from the ledger; a terminal id never gets landed again. A torn
        or malformed line is skipped, never allowed to abort the read (that would
        make a full queue look empty).

        Decoded through the SHARED `iter_events`, not a private `json.loads`,
        because two O_APPEND writers can land two complete events on one physical
        line (measured: `var/loopqueue/ledger.jsonl` line 3323, whose second
        object was a terminal `rejected`). `json.loads` refuses that whole line
        and returns neither event. A terminal id this set cannot see is an id the
        gate is willing to land a SECOND time, and `ledger.jsonl` is append-only,
        so the duplicate terminal history it writes can never be repaired."""
        terminal: set[str] = set()
        path = self.root / "ledger.jsonl"
        if not path.exists():
            return terminal
        for ev in iter_events(path.read_text(encoding="utf-8", errors="replace")):
            if ev.get("event") in ("merged", "completed", "rejected", "closed") \
                    and ev.get("id"):
                terminal.add(ev["id"])
        return terminal

    def _instrument_regate_count(self, train_key: str) -> int:
        """How many times this exact `<train>@<tip>` already recorded an
        instrument_error. Bounds re-gating to ONCE — an unbounded re-gate on an
        unchanged instrument is the same storm as re-running an unchanged input,
        only wearing a different label.

        Shared `iter_events` for the same reason as `_terminal_ids`: an
        `instrument_error` sharing a physical line with another event is invisible
        to `json.loads`, and an under-count here is a re-gate bound that does not
        bind — the storm this bound exists to stop."""
        n = 0
        path = self.root / "ledger.jsonl"
        if not path.exists():
            return 0
        for ev in iter_events(path.read_text(encoding="utf-8", errors="replace")):
            if ev.get("event") != "instrument_error":
                continue
            det = ev.get("detail")
            if isinstance(det, dict) and det.get("train_key") == train_key:
                n += 1
        return n

    # -- candidate loading ---------------------------------------------------

    def _read_queue_artifact(self, kind_dir: str, ident: str) -> dict | None:
        """One artifact by id, or None. Never raises, never leaves `kind_dir`."""
        if not isinstance(ident, str) or not _ARTIFACT_ID_RE.fullmatch(ident.strip()):
            return None
        path = self.root / kind_dir / f"{_stem(ident.strip())}.json"
        try:
            art = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return art if isinstance(art, dict) else None

    def _resolve_bindings(self, art: dict, member_id: str) -> list[Binding]:
        """Resolve candidate -> proposal(s) -> (finding, bound test), or NOTHING.

        The chain is `candidate.payload.resolves` (a proposal id, or a LIST of
        them — 38 of the 168 live candidates carry a list) -> that proposal's
        `payload.answers_finding` (the finding this closes) and
        `payload.failing_test.node_id` (the test that proves it).

        CLOSURE IS OPT-IN AND ABSENT-SAFE. Neither field exists on today's
        artifacts, so the overwhelmingly common answer is `[]` and the candidate
        is loaded, gated and landed exactly as before — zero behaviour change for
        every routine candidate. A malformed link is treated as an ABSENT one and
        NEVER makes a candidate ineligible: refusing to land a real fix because
        someone wrote a bad `failing_test` block would charge this plan's own
        bookkeeping to the merge rate, which is the scarce thing here.
        """
        payload = art.get("payload")
        if not isinstance(payload, dict):
            return []
        raw = payload.get("resolves")
        refs = raw if isinstance(raw, list) else [raw]
        out: list[Binding] = []
        seen: set[tuple[str, str]] = set()
        for ref in refs[:32]:                    # a bounded fan-out, never a queue walk
            proposal = self._read_queue_artifact("proposals", ref)
            if proposal is None:
                continue
            ppayload = proposal.get("payload")
            if not isinstance(ppayload, dict):
                continue
            finding = ppayload.get("answers_finding")
            failing = ppayload.get("failing_test")
            node = _valid_node_id(failing.get("node_id") if isinstance(failing, dict) else None)
            if not isinstance(finding, str) or not _ARTIFACT_ID_RE.fullmatch(finding.strip()):
                continue
            if node is None:
                continue
            key = (finding.strip(), node)
            if key in seen:
                continue
            seen.add(key)
            out.append(Binding(member_id, finding.strip(), node))
        return out

    def load_candidates(self, terminal: set[str]) -> list[Candidate]:
        """Non-terminal candidates eligible for the mandatory mechanical gate.

        Routine work does not wait for a separate verdict conveyor. Risky work
        requires a genuine cross-lineage build-time verdict, with risk derived
        from ``base..tip`` rather than the envelope's declared paths. The
        forward-port itself is done by train assembly, so the base only needs to
        be known to this repository.
        """
        out: list[Candidate] = []
        self.bindings = {}
        cdir = self.root / "candidates"
        if not cdir.is_dir():
            return out
        for p in sorted(cdir.glob("*.json")):
            try:
                art = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(art, dict) or art.get("kind") != "candidate":
                continue
            ident = art.get("id")
            branch = art.get("branch")
            base = art.get("base_sha")
            if not ident or not branch or not isinstance(base, str) or len(base) != 40:
                continue
            if ident in terminal:
                continue

            # The immutable, approved head SHA is the candidate identity. A
            # branch is only a convenience ref and builders routinely delete it
            # after publishing. If both exist they MUST agree; a moved branch is
            # not silently substituted for reviewed code.
            top_tip = _full_sha(art.get("head_sha"))
            payload = art.get("payload")
            payload_tip = (_full_sha(payload.get("head_sha"))
                           if isinstance(payload, dict) else None)
            if (top_tip is not None and payload_tip is not None
                    and top_tip.lower() != payload_tip.lower()):
                # The top-level field is OUTSIDE the content id; the payload is
                # inside it. When both name a head they must agree — an unhashed
                # carrier is never allowed to override the hashed one.
                self._log(f"skip {_short(ident)}: top-level head_sha "
                          f"{top_tip[:12]} conflicts with payload head_sha "
                          f"{payload_tip[:12]}")
                self._note_ineligible(
                    ident, "head-sha-conflict", branch=branch,
                    symptom=(f"top-level head_sha {top_tip} disagrees with payload "
                             f"head_sha {payload_tip}; the identity is ambiguous and "
                             "the unhashed top-level carrier may not override the "
                             "payload the envelope id was hashed over"),
                    remedy=("re-file the envelope with ONE head — if the payload value "
                            "is right, drop or correct the top-level field; if the "
                            "top-level value is right, re-file with a payload that "
                            "names it (which changes the content id, as it must)"))
                continue
            declared_tip = top_tip
            if declared_tip is None and payload_tip is not None:
                # The ThreeLoops-side schema never required head_sha, so a whole
                # producer class nests it under `payload` instead (52 of 154
                # archived envelopes; every one silently skipped). The payload is
                # hashed into the envelope id, so a payload head_sha is
                # producer-authored and immutable — stronger provenance than the
                # mutable branch ref. But that argument only holds if the id
                # actually binds this payload: verify it before trusting the
                # value, else an in-place payload edit smuggles in any tip.
                try:
                    bound = content_id(payload) == ident
                except (TypeError, ValueError):
                    bound = False           # unhashable payload proves nothing
                if not bound:
                    self._log(f"skip {_short(ident)}: payload head_sha present but "
                              "the payload does not hash to the envelope id")
                    self._note_ineligible(
                        ident, "payload-id-mismatch", branch=branch,
                        symptom=("payload names a head_sha but content_id(payload) "
                                 "does not equal the envelope id, so the payload "
                                 "cannot be trusted as the immutable identity "
                                 "carrier (tampered or hand-assembled envelope)"),
                        remedy=("re-file the envelope so its id is the content id "
                                "of its payload (compute it with "
                                "bridge.canonical.content_id), or add the correct "
                                "top-level head_sha instead"))
                    continue
                declared_tip = payload_tip
                self._log(f"note {_short(ident)}: head_sha hoisted from payload")
            if declared_tip is None:
                self._log(f"skip {_short(ident)}: missing full immutable head_sha")
                self._note_ineligible(
                    ident, "missing-head-sha", branch=branch,
                    symptom=("no top-level or payload head_sha; the loader refuses to "
                             "substitute the mutable branch ref for reviewed code"),
                    remedy=("re-file (or repair) the envelope with a top-level head_sha: "
                            "the full 40-hex commit the evidence was produced at. If the "
                            "branch still resolves, `git rev-parse <branch>` names the "
                            "tip — verify the evidence was produced AT that tip before "
                            "pinning it. payload.head_sha is also accepted."))
                continue
            rc_b, branch_tip, _ = git(
                self.repo, "rev-parse", "--verify", f"{branch}^{{commit}}")
            branch_tip = branch_tip.strip() if rc_b == 0 else ""
            rc_t, exact_tip, _ = git(
                self.repo, "rev-parse", "--verify", f"{declared_tip}^{{commit}}")
            exact_tip = exact_tip.strip() if rc_t == 0 else ""
            if not exact_tip:
                self._log(f"skip {_short(ident)}: approved head_sha "
                          f"{declared_tip[:12]} is unresolvable in {self.repo_key}")
                self._note_ineligible(
                    ident, "head-sha-unresolvable", branch=branch,
                    symptom=(f"approved head_sha {declared_tip} does not resolve in "
                             f"{self.repo_key}"),
                    remedy=("push or fetch the reviewed commit into the serving "
                            "repository (worktrees share its object store), or re-file "
                            "the envelope at a tip that exists there"))
                continue
            if branch_tip and branch_tip != exact_tip:
                self._log(f"skip {_short(ident)}: branch {branch} moved away from "
                          f"approved head_sha {exact_tip[:12]} (now {branch_tip[:12]})")
                self._note_ineligible(
                    ident, "branch-moved", branch=branch,
                    symptom=(f"branch {branch} no longer points at approved head_sha "
                             f"{exact_tip}; a moved branch is never silently "
                             "substituted for reviewed code"),
                    remedy=("re-verify and re-file at the branch's current tip, or "
                            "reset the branch to the approved head_sha"))
                continue
            tip = exact_tip
            rc_a, _, _ = git(self.repo, "cat-file", "-e", f"{base}^{{commit}}")
            if not tip or rc_a != 0:
                self._log(f"skip {_short(ident)}: candidate head/base unresolvable in "
                          f"{self.repo_key}")
                continue

            # Tiered verification: EVERY candidate still goes through the full
            # mechanical gate. Only risky/self-governing diffs also need a
            # separate human/model verdict. Read the real tree diff here so an
            # understated `paths` claim cannot downgrade its own review tier.
            rc_p, diff_out, diff_err = git(
                self.repo, "diff", "--name-only", f"{base}..{tip}")
            if rc_p != 0:
                self._log(f"skip {_short(ident)}: real diff unreadable (instrument): "
                          f"{diff_err[:160]}")
                continue
            actual_paths = {line.strip() for line in diff_out.splitlines() if line.strip()}
            # TIERED VERIFICATION (kill switch: OMNIAGENTOS_TIERED_VERIFY=1).
            # Risk is derived from the REAL diff, never the declared paths.
            #   HIGH => a genuine cross-lineage build verdict, exactly as before.
            #   LOW  => a signed, receipt-verified merge-gate PASS on the
            #           candidate's OWN tip stands in for that verdict.
            # The gate stays the floor for BOTH tiers: a LOW candidate with no
            # verified PASS receipt on its tip does NOT land. With the switch
            # OFF, classify() forces HIGH for everything, so this reduces to the
            # exact pre-tiering approval check below.
            tier = self._risk_tier(art, actual_paths)
            if tier == risk_tier.LOW:
                ok, receipt_sha, why = self._low_tier_gate_pass(exact_tip)
                if not ok:
                    self._log(f"skip {_short(ident)}: LOW tier but {why}")
                    self._note_ineligible(
                        ident, "low-tier-no-gate-pass", branch=branch,
                        symptom=(f"classified LOW-risk from its real diff but has no signed, "
                                 f"receipt-verified merge-gate PASS on its own tip "
                                 f"{exact_tip}: {why}. The mechanical gate is the floor for "
                                 "EVERY tier — LOW only waives the extra cross-lineage LLM "
                                 "verdict, it never waives the gate"),
                        remedy=("run merge-gate on the candidate tip so a signed PASS receipt "
                                "is recorded in the evidence store, or obtain a genuine "
                                "cross-lineage verdict so it can land on the HIGH path"))
                    continue
                # Synthetic verdict: the audit trail records WHY the LLM verdict
                # was waived (a mechanical-gate PASS, not a lineage reviewer). Its
                # 'mechanical-gate' lineage is deliberately outside KNOWN_LINEAGES,
                # so it can never satisfy approved_cross_lineage on a HIGH surface.
                verdicts = art.setdefault("verdicts", [])
                if isinstance(verdicts, list):
                    verdicts.append({
                        "lineage": "mechanical-gate", "by": "merge-gate",
                        "receipt": receipt_sha, "reviewed_sha": exact_tip,
                    })
                self._log(f"note {_short(ident)}: LOW tier landed on mechanical-gate PASS "
                          f"{receipt_sha[:12]} — cross-lineage LLM verdict waived")
            else:
                risky = risky_review_paths(actual_paths)
                if risky and not approved_cross_lineage(art, exact_tip):
                    self._log(f"skip {_short(ident)}: risky diff requires a genuine "
                              f"cross-lineage build verdict: {', '.join(risky[:5])}")
                    continue
            # Freeze the ref used by diff/cherry-pick to the resolved commit.
            # Even a branch that matched one line above may move between queue
            # loading and assembly; using its name would re-open that TOCTOU.
            landing_ref = tip
            c = Candidate(ident, p, art, branch=landing_ref, base_sha=base,
                          tip_sha=tip,
                          title=str(art.get("title", ""))[:120])
            # Resolved AFTER eligibility is settled, so a closure chain can only
            # ever ADD information to a candidate that was already landing.
            bindings = self._resolve_bindings(art, ident)
            if bindings:
                self.bindings[ident] = bindings
                self._log(f"{_short(ident)} closes {len(bindings)} finding(s) via "
                          + ", ".join(f"{_short(b.finding_id)}:{b.node_id}"
                                      for b in bindings))
            out.append(c)
        return out

    def _reconcile_already_merged(self, cands: list[Candidate],
                                  main_sha: str) -> list[Candidate]:
        """Terminalise an approved exact head that is already on ``main``.

        Candidate artifacts can arrive after a human or another lander has
        advanced main. Without reconciliation they occupy the eligible set
        forever and gate-surface candidates in particular log the same exclusion
        every minute. Only exact commit ancestry qualifies; a similar diff or a
        moved branch never does.
        """
        active: list[Candidate] = []
        for c in cands:
            if not c.tip_sha:
                active.append(c)
                continue
            rc, _, err = git(self.repo, "merge-base", "--is-ancestor",
                             c.tip_sha, main_sha)
            if rc == 0:
                original_branch = c.art.get("branch") or c.branch
                self._append_ledger({
                    "ts": _iso(_now()), "role": ROLE, "event": "merged",
                    "id": c.ident, "base_sha": c.base_sha, "actor": ACTOR,
                    # `result` is a pass|fail ENUM in the ledger schema; the
                    # provenance of this landing rides in `disposition` and
                    # `reconciled`, which are free-form. It used to say
                    # "already-on-main" here, which made every reconciliation
                    # line fail validation (cross-lineage review, 2026-08-12).
                    "detail": {"result": "pass", "disposition": "already-on-main",
                               "merge_sha": main_sha,
                               "candidate_sha": c.tip_sha, "branch": original_branch,
                               "reconciled": True},
                })
                self._log(f"reconciled {_short(c.ident)}: approved head "
                          f"{c.tip_sha[:12]} already on main {main_sha[:12]}")
                # THIS IS A LANDING, so it owes everything a landing owes. It is
                # a second bookkeeping path (no train, no gate of ours, no
                # receipt), and it used to terminalise the candidate and stop --
                # leaving the proposal offered forever for exactly the work that
                # is provably on main. Retirement is the one obligation that
                # transfers, and it is routed through the SAME seam so the two
                # paths cannot drift apart.
                self._terminalize_resolved_proposals(
                    c.ident, main_sha, "", original_branch)
                continue
            if rc not in (0, 1):
                self._log(f"{_short(c.ident)} head ancestry undeterminable "
                          f"(instrument): {err[:160]}")
            active.append(c)
        return active

    # -- scratch worktree ----------------------------------------------------

    def _scratch(self) -> Path:
        return self.root / "state" / "gate-loop-build"

    def _builder_active_path(self) -> Path:
        return self.root / "state" / "gate-loop-build.active.json"

    def _builder_lock_reason(self, scratch: Path) -> str | None:
        active = self._builder_active_path()
        if active.exists() or active.is_symlink():
            try:
                detail = active.read_text(encoding="utf-8")[:300]
            except OSError as exc:
                detail = f"unreadable: {type(exc).__name__}: {exc}"
            return f"active marker {active}: {detail}"

        dotgit = scratch / ".git"
        if not dotgit.exists() and not dotgit.is_symlink():
            return None
        try:
            if dotgit.is_file():
                text = dotgit.read_text(encoding="utf-8").strip()
                if not text.startswith("gitdir: "):
                    return f"unrecognized worktree metadata at {dotgit}"
                admin = Path(text.removeprefix("gitdir: "))
                if not admin.is_absolute():
                    admin = (scratch / admin).resolve()
            else:
                admin = dotgit
        except OSError as exc:
            return f"unreadable worktree metadata at {dotgit}: {type(exc).__name__}: {exc}"
        for indicator in (admin / "locked", admin / "index.lock"):
            if indicator.exists() or indicator.is_symlink():
                return f"git worktree lock indicator {indicator}"
        return None

    def _registered_worktree_paths(self) -> set[Path] | None:
        proc = subprocess.run(
            ["git", "-C", str(self.repo), "worktree", "list", "--porcelain"],
            capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            self._alert(
                "could not read worktree registry (instrument); refusing builder cleanup: "
                f"{(proc.stderr or proc.stdout).strip()}")
            return None
        paths: set[Path] = set()
        for line in proc.stdout.splitlines():
            if line.startswith("worktree "):
                paths.add(Path(line.removeprefix("worktree ")).resolve())
        return paths

    def _orphaned_initializing_lock(self, scratch: Path, now: float | None = None) -> bool:
        """True IFF the build worktree is wedged ONLY by git's own transient
        ``initializing`` marker from an interrupted ``git worktree add``, AND that
        marker is stale enough that no live add could still hold it.

        This reads the CONTENT of git's ``locked`` file, not merely its presence:
        ``git worktree add`` writes exactly ``initializing`` there while it checks
        out and unlinks it on success (verified: git 2.43 writes ``initializing\\n``).
        A time-out or a killed daemon leaves that marker behind, and on the next
        tick :meth:`_builder_lock_reason` sees a lock indicator and the daemon
        refuses forever. gate-loop-build is a regenerable full checkout of ``main``
        holding NO real work, so that specific marker is safe to reclaim.

        It is deliberately narrow. Any OTHER lock content — a human ``git worktree
        lock --reason ...``, an empty manual lock, an ``index.lock`` with no
        ``locked`` file — is a real lock we must never disturb, so this returns
        False and the caller keeps refusing. The daemon itself NEVER runs ``git
        worktree lock`` with a custom reason (grep-confirmed), so ``initializing``
        is unambiguously git's own add marker and never a deliberate hold.
        """
        now = time.time() if now is None else now
        # An active marker means an assembly WE own is (or was) in flight; that is
        # never reclaimable orphaned debris, and _builder_lock_reason already
        # reports it ahead of any git indicator. Re-check here so this predicate is
        # safe to call on its own.
        active = self._builder_active_path()
        if active.exists() or active.is_symlink():
            return False
        dotgit = scratch / ".git"
        try:
            if dotgit.is_file():
                text = dotgit.read_text(encoding="utf-8").strip()
                if not text.startswith("gitdir: "):
                    return False
                admin = Path(text.removeprefix("gitdir: "))
                if not admin.is_absolute():
                    admin = (scratch / admin).resolve()
            elif dotgit.is_dir():
                admin = dotgit
            else:
                return False
        except OSError:
            return False
        locked = admin / "locked"
        try:
            reason = locked.read_text(encoding="utf-8").strip()
        except OSError:
            return False
        if reason != "initializing":
            return False
        # Staleness guard (belt-and-suspenders). The single-writer tick lock
        # already precludes a second daemon invocation racing a live add on this
        # path, but a daemon SIGKILLed moments after git wrote the marker could
        # leave a young one; requiring it to be older than the longest possible
        # add guarantees we never reclaim a lock a live add still holds. A young
        # marker is simply left for a later tick (a bounded wait, not a halt).
        try:
            age = now - locked.stat().st_mtime
        except OSError:
            return False
        return age >= WORKTREE_ADD_TIMEOUT_S

    def _worktree_admin_dir(self, scratch: Path) -> Path | None:
        """This worktree's own git admin dir (``<repo>/.git/worktrees/<name>``).

        Read AUTHORITATIVELY from the worktree's ``.git`` pointer file when
        present — git suffixes the admin name on a basename collision, so the
        conventional path is not always right. Fall back to the conventional
        ``<repo>/.git/worktrees/<basename>`` only when the pointer is missing or
        partial (e.g. an add that failed before writing it). Returns None when
        neither resolves — there is then no admin entry to deregister.
        """
        dotgit = scratch / ".git"
        try:
            if dotgit.is_file():
                text = dotgit.read_text(encoding="utf-8").strip()
                if text.startswith("gitdir: "):
                    admin = Path(text.removeprefix("gitdir: "))
                    if not admin.is_absolute():
                        admin = (scratch / admin).resolve()
                    return admin
            elif dotgit.is_dir():
                return dotgit
        except OSError:
            pass
        conventional = self.repo / ".git" / "worktrees" / scratch.name
        return conventional if conventional.exists() else None

    def _reclaim_orphaned_builder(self, scratch: Path) -> bool:
        """Clear an orphaned build worktree left LOCKED by an interrupted
        ``git worktree add``. Returns True once the path is fully gone and
        unregistered (caller may recreate), False on any snag (caller refuses).

        PATH-SCOPED by construction: it deregisters ONLY gate-loop-build and
        NEVER runs a repo-global ``git worktree prune``. prune would sweep EVERY
        worktree whose working dir is merely missing-and-unlocked, silently
        deregistering an unrelated human/lane worktree that is mid-teardown or on
        an unmounted volume (finding F2). Instead the sequence is: unlock (drop
        the ``locked`` file) -> ``rm -rf`` the working dir -> ``rm -rf`` THIS
        worktree's OWN admin entry (resolved BEFORE the working dir — and its
        ``.git`` pointer — are removed). A later ``git worktree add`` at the same
        path re-registers cleanly (verified on git 2.43). ``git worktree remove
        --force`` is avoided too: it is git-guard-banned here and cannot remove a
        locked worktree with a single force anyway, so the direct admin-dir
        removal sidesteps both questions. Every filesystem removal is scoped to a
        specific path; no repo-global git state is touched.
        """
        # Resolve the admin entry FIRST — removing the working dir destroys the
        # `.git` pointer this reads.
        admin = self._worktree_admin_dir(scratch)
        subprocess.run(
            ["git", "-C", str(self.repo), "worktree", "unlock", str(scratch)],
            capture_output=True, text=True, check=False)
        shutil.rmtree(scratch, ignore_errors=True)
        if admin is not None:
            shutil.rmtree(admin, ignore_errors=True)
        registered = self._registered_worktree_paths()
        if registered is None or scratch.resolve() in registered:
            self._alert(
                f"reclaimed build worktree {scratch} still registered after admin-dir "
                "removal; refusing to recreate")
            return False
        if scratch.exists() or scratch.is_symlink():
            self._alert(
                f"reclaimed build worktree dir {scratch} still present after removal; "
                "refusing to recreate")
            return False
        return True

    def _open_builder(self) -> Path | None:
        """A disposable worktree where trains are assembled and cherry-picked,
        so the SERVING checkout's HEAD is never touched. Shares the repo's object
        store, so branch refs it creates are visible in the repo."""
        scratch = self._scratch()
        lock_reason = self._builder_lock_reason(scratch)
        if lock_reason is not None:
            # SELF-HEAL: an interrupted `git worktree add` leaves git's own
            # transient `initializing` lock, which otherwise wedges every future
            # tick (recurring production halt). Reclaim ONLY that specific,
            # provably-orphaned marker; leave any other lock (a real hold, a young
            # in-flight add) untouched and keep refusing.
            if self._orphaned_initializing_lock(scratch):
                self._alert(
                    f"reclaiming orphaned build worktree {scratch}: interrupted "
                    f"`git worktree add` left git's transient 'initializing' lock "
                    f"({lock_reason})")
                if not self._reclaim_orphaned_builder(scratch):
                    return None
                # fall through to the normal (re)create path below.
            else:
                self._alert(
                    f"refusing to disturb active/orphaned build worktree {scratch}: "
                    f"{lock_reason}")
                return None

        removed = subprocess.run(
            ["git", "-C", str(self.repo), "worktree", "remove", "--force", str(scratch)],
            capture_output=True, text=True, check=False)
        registered = self._registered_worktree_paths()
        if registered is None:
            return None
        scratch_resolved = scratch.resolve()
        if removed.returncode != 0 and scratch_resolved in registered:
            self._alert(
                f"could not remove registered build worktree {scratch} (instrument): "
                f"{(removed.stderr or removed.stdout).strip()}")
            return None

        if scratch.exists() or scratch.is_symlink():
            if scratch_resolved in registered:
                self._alert(
                    f"build worktree {scratch} remains registered after removal; refusing "
                    "to rename or reuse it")
                return None
            quarantine = scratch.with_name(f"{scratch.name}.stale-{time.time_ns()}")
            try:
                scratch.rename(quarantine)
            except OSError as exc:
                self._alert(
                    f"could not quarantine proven-unregistered build debris {scratch}: {exc}")
                return None
            self._alert(f"quarantined stale build debris {scratch} at {quarantine}")

        rc, _, err = git(self.repo, "worktree", "add", "--detach", "--force",
                         str(scratch), "main", timeout=WORKTREE_ADD_TIMEOUT_S)
        if rc != 0:
            # A timed-out or otherwise interrupted add can leave a PARTIAL worktree
            # locked by git's own `initializing` marker, which would block every
            # future tick. We hold the single-writer lock and just created this
            # ourselves, so any debris here is unambiguously ours to clear now —
            # leave the NEXT tick a clean slate rather than a stale lock to reclaim.
            self._reclaim_orphaned_builder(scratch)
            self._alert(f"could not open build worktree (instrument): {err}")
            return None
        try:
            self._write_json_atomic(self._builder_active_path(), {
                "path": str(scratch), "pid": os.getpid(), "opened_at": _iso(_now()),
            })
        except OSError as exc:
            subprocess.run(
                ["git", "-C", str(self.repo), "worktree", "remove", "--force", str(scratch)],
                capture_output=True, text=True, check=False)
            self._alert(f"could not publish builder active marker (instrument): {exc}")
            return None
        return scratch

    def _close_builder(self) -> None:
        scratch = self._scratch()
        removed = subprocess.run(
            ["git", "-C", str(self.repo), "worktree", "remove", "--force", str(scratch)],
            capture_output=True, text=True, check=False)
        if removed.returncode != 0:
            registered = self._registered_worktree_paths()
            if (registered is None or scratch.resolve() in registered
                    or scratch.exists() or scratch.is_symlink()):
                self._alert(
                    f"could not close build worktree {scratch}; active marker retained: "
                    f"{(removed.stderr or removed.stdout).strip()}")
                return
        try:
            self._builder_active_path().unlink(missing_ok=True)
        except OSError as exc:
            self._alert(f"could not clear builder active marker (instrument): {exc}")

    # -- gate dispatch -------------------------------------------------------

    def _effective_gate_ws(self) -> Path:
        """The pinned gate workspace to grade FROM. Defaults to `<repo>-gate`,
        matching offload's own local default, when not set explicitly."""
        return self.gate_ws or Path(str(self.repo) + "-gate")

    # -- tiered verification -------------------------------------------------

    def _risk_tier(self, art: dict, actual_paths: set[str]) -> str:
        """HIGH/LOW for a candidate from its REAL changed paths (never declared).

        Respects the ``OMNIAGENTOS_TIERED_VERIFY`` kill switch via
        :func:`risk_tier.classify`. The two narrow LOW carve-outs are attested by
        the candidate envelope, but each attestation is INTERSECTED with the real
        diff first, so a self-reported claim can only ever apply to a path the
        candidate genuinely touched — the classifier still refuses to trust the
        declared ``paths`` for the risk decision itself.
        """
        def _attested(field: str) -> set[str]:
            val = art.get(field)
            if not isinstance(val, list):
                return set()
            return {p for p in val if isinstance(p, str) and p in actual_paths}

        return risk_tier.classify(
            actual_paths,
            attested_additive_schema=_attested("additive_schema_paths"),
            attested_scripts=_attested("mechanical_script_paths"))

    def _low_tier_gate_pass(self, tip: str) -> tuple[bool, str, str]:
        """A signed, receipt-verified merge-gate PASS on the candidate's OWN tip.

        Returns ``(ok, receipt_sha, reason)``. The receipt is the §0
        candidate-bound record the gate mints into the durable evidence store at
        ``<evidence_root>/records/merge-gate/<tip>.json`` — exactly the path
        :func:`_mint_candidate_receipt` reads. The bar is this file's own receipt
        bar (:func:`_receipt_verified`: present, non-empty, a parseable JSON
        object) PLUS a guard that the record does not itself report a FAILED run.
        Signature/schema verification is the target repo's job at gate time; this
        is the ThreeLoops-side presence+integrity bar, the same one
        :func:`read_gate_verdict` applies before believing a pass.
        """
        gate_ws = str(self._effective_gate_ws())
        shared_root = gate_ws[:-len("-gate")] if gate_ws.endswith("-gate") else gate_ws
        receipt = (Path(shared_root) / "var" / "gate-evidence"
                   / "records" / "merge-gate" / f"{tip}.json")
        if not _receipt_verified(receipt):
            return False, "", (f"no signed, receipt-verified merge-gate PASS on the tip "
                               f"(expected at {receipt})")
        try:
            data = json.loads(receipt.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return False, "", f"tip receipt unreadable ({exc})"
        # FAIL CLOSED on the receipt's own PASS/FAIL fields. Every negative check
        # below refuses on an UNKNOWN or malformed shape, never lets it through:
        # a receipt that authorizes waiving cross-lineage review must prove a
        # green run, so anything we cannot positively read as a pass is treated as
        # a fail. (Cross-lineage review, Kimi K3: the earlier checks were
        # type-fragile — `isinstance(rc, int)` let a string `"rc":"1"` slip, and a
        # blocklist of four failure strings let every other `result` value slip.)
        # NOTE: this is the cheap presence+integrity PRE-check only. Authentic
        # SIGNATURE/binding verification of the gate receipt is enforced by the
        # EXISTING verifier `omniagentos.scheduler.gate_evidence verify-candidate`
        # at the mandatory gate FLOOR (see `_verify_candidate_receipt` /
        # merge-gate.sh's signed-receipt step, which re-mints & verifies the
        # assembled train's tip). A forged receipt that slips this pre-check still
        # cannot land: the floor re-verifies independently before merge.
        if not isinstance(data, dict):
            return False, "", "tip receipt is not a JSON object"
        # candidate binding: the record must not name a DIFFERENT tip than the one
        # it is filed under (the path is tip-addressed; a mismatched body is a
        # tampered or misfiled receipt).
        cand = data.get("candidate_sha")
        if isinstance(cand, str) and cand.strip() and cand.strip().casefold() != tip.casefold():
            return False, "", (f"tip receipt records candidate_sha={cand!r} != tip {tip}")
        # rc: PASS is exactly integer 0. Coerce int-or-digit-string; any non-zero,
        # non-integer, or otherwise uncoercible rc fails closed.
        if "rc" in data:
            rc_raw = data.get("rc")
            rc_val: int | None
            if isinstance(rc_raw, bool):
                rc_val = None            # bool is not a valid rc
            elif isinstance(rc_raw, int):
                rc_val = rc_raw
            elif isinstance(rc_raw, str) and rc_raw.strip().lstrip("+-").isdigit():
                rc_val = int(rc_raw.strip())
            else:
                rc_val = None
            if rc_val != 0:
                return False, "", f"tip receipt records a non-pass gate rc={rc_raw!r}"
        # boolean pass fields: present-and-not-truthy is an explicit fail.
        for key in ("passed", "pass", "ok"):
            if key in data and not data.get(key):
                return False, "", f"tip receipt records {key}={data.get(key)!r}"
        # result: ALLOWLIST the pass vocabulary. If a `result` is present at all,
        # it must be a recognized PASS value; every other value (including a blank
        # or an unexpected word) fails closed.
        if "result" in data:
            result = str(data.get("result", "")).strip().casefold()
            _PASS_RESULTS = {"pass", "passed", "ok", "green", "success"}
            if result not in _PASS_RESULTS:
                return False, "", f"tip receipt records non-pass result={data.get('result')!r}"
        try:
            receipt_sha = hashlib.sha256(receipt.read_bytes()).hexdigest()
        except OSError as exc:
            return False, "", f"tip receipt unreadable ({exc})"
        return True, receipt_sha, ""

    def _train_bindings(self, train: Train) -> list[Binding]:
        """Every member's resolved bindings, in member order."""
        return [b for m in train.members for b in self.bindings.get(m["id"], ())]

    def _train_bound_tests(self, train: Train, gate_ws: Path) -> list[str]:
        """The `--bound-test` node ids this train may hand the gate.

        Deduplicated (two members bound to the same node is one step, not two)
        and EMPTY unless the pinned script actually accepts the flag — see
        `gate_supports_bound_test`. Withholding is logged in one line because the
        operator's question when a `closed` does not appear is always "was the
        gate even told?", and silence is the wrong answer to it.
        """
        nodes: list[str] = []
        for b in self._train_bindings(train):
            if b.node_id not in nodes:
                nodes.append(b.node_id)
        if not nodes:
            return []
        if not gate_supports_bound_test(gate_ws):
            self._log(
                f"bound-test flags WITHHELD for {train.branch}: "
                f"{gate_ws}/scripts/merge-gate.sh does not accept --bound-test "
                "(the shell half of the closure contract has not landed in this pinned "
                "workspace) — gating without bindings; nothing will be closed on this run")
            return []
        return nodes

    def _read_gate_leases(self, now: float | None = None) -> GateLeases:
        """THE occupancy parser (I4'). Only place gate-state JSON is parsed for
        slot/twin arithmetic. Both `_running_gate_count` and `_twins_in_flight`
        are thin delegates of this return value.

        Receipt filter (design B1): `receipt-*.json` shares `state/gates/` and
        is written non-atomically by merge-gate.sh — a truncated receipt is NOT
        an occupancy fact and must never block dispatch.

        Corrupt gate-STATE (bounded veto): busy both directions until its
        durably recorded veto-start plus ``GATE_DEADLINE_S``, then quarantined.
        The daemon writes states atomically, so a corrupt one was never
        daemon-written.

        Expired-present running state: still counted (running + twin busy). A
        passed deadline does not prove the child stopped; the slot frees only
        after the verified reap unlinks/stamps the file (I8).
        """
        now = time.time() if now is None else now
        leases = GateLeases()
        self._gc_veto_markers()
        gdir = self.root / "state" / "gates"
        if not gdir.is_dir():
            return leases
        pool = {s.host for s in TWIN_SPECS}
        for p in sorted(gdir.glob("*.json")):
            # Occupancy facts come ONLY from running-gate lease files. A
            # `receipt-*` is a merge-gate artifact; an `iso-*` is a CLOSED
            # isolation carrier (never a live lease) — counting an unreadable
            # one as a running gate is exactly what wedged dispatch for 2h.
            if p.name.startswith(("receipt-", "iso-")):
                continue
            raw = ""
            try:
                raw = p.read_text(encoding="utf-8")
                st = json.loads(raw)
            except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                started_at = self._veto_started_at(
                    p, None, kind="corrupt", now=now, raw=raw)
                if started_at + GATE_DEADLINE_S > now:
                    # Fail-closed BOTH directions for the bound window.
                    leases.corrupt = True
                    leases.running += 1
                    leases.twins |= pool
                    self._record_corrupt_veto(p, started_at, kind="corrupt")
                    self._log(f"corrupt gate-state {p.name} — fail-closed occupancy")
                else:
                    self._quarantine_gate_state(p, kind="corrupt")
                continue
            if not isinstance(st, dict) or st.get("state") != "running":
                self._clear_veto_marker(p)
                continue
            deadline = st.get("deadline")
            parked = st.get("disposition") == "reap-unverifiable-parked"
            if not isinstance(deadline, (int, float)) or parked:
                # A parked lease holds exactly the resource it had claimed;
                # direct parks hold only the local slot, never the whole pool.
                kind = "reap-unverifiable-parked" if parked else "deadlineless"
                started_at = self._veto_started_at(p, st, kind=kind, now=now)
                if started_at + GATE_DEADLINE_S > now:
                    leases.running += 1
                    if parked and st.get("mode") == "remote":
                        leases.twins.add(st.get("twin") or TWIN_HOST)
                    elif not parked:
                        leases.corrupt = True
                        leases.twins |= pool
                    self._record_corrupt_veto(p, started_at, kind=kind)
                else:
                    self._quarantine_gate_state(p, kind=kind)
                continue
            self._clear_veto_marker(p)
            # Expired-present still holds the slot AND the twin (I4' / F-B1).
            leases.running += 1
            if st.get("mode") == "remote":
                # LEGACY: pre-pool remotes carry no "twin"; they always meant
                # TWIN_HOST. Absence is not freedom.
                leases.twins.add(st.get("twin") or TWIN_HOST)
        return leases

    def _twins_in_flight(self, *, now: float | None = None) -> set:
        """Which twins are already gating — thin delegate of `_read_gate_leases`.

        Derived from the same parser as `_running_gate_count`, so the slot count
        and the twin assignment can never disagree — two trains landing on one
        box would put two full gates on a machine this scheduler exists to keep
        to one, and the second would be graded under contention it did not
        cause.
        """
        return set(self._read_gate_leases(now=now).twins)

    def _busy_boxes(self, *, now: float | None = None, extra=(), resolve=None) -> set:
        """Pool hosts whose MACHINE is already gating (`_twins_in_flight` + `extra`).

        `_twins_in_flight` returns the NAMES gates were dispatched under, which
        need not be the pool's current names for those machines: the pool is
        config-declared now, so a rename or an added alias can leave a running
        gate recorded as `mw0001` while the pool offers `mw0001-owner` — the same
        Mac. Excluding by name would then dispatch a second concurrent gate
        there, which is the outage this scheduler exists to prevent, so the
        exclusion set is resolved to physical identity before it is used.

        Fail-closed shapes upstream are preserved: a corrupt gate-state already
        claims every active twin, and an in-flight name whose identity cannot be
        resolved marks the whole pool busy (see `busy_physical_hosts`).
        """
        in_flight = self._twins_in_flight(now=now) | {h for h in extra if h}
        if not in_flight:
            return set()
        return busy_physical_hosts(in_flight, specs=TWIN_SPECS, resolve=resolve)

    def dispatch_gate(self, train: Train, *, allow_remote: bool,
                      twin: str | None = None) -> None:
        """Write the running-state file, then spawn the gate DETACHED.

        The state file is written FIRST, so the moment a train is dispatched it
        has a status; if the detached child dies without finishing, the file
        stays 'running' and expires into an instrument-error rather than
        vanishing. `start_new_session=True` (setsid) detaches the child so it
        outlives this tick — an LLM never watches the log.

        HOST CHOICE (Grok last-mile fix): a LOCAL gate runs the pinned
        workspace's OWN merge-gate.sh directly (mode=direct), so $0 == the pinned
        blob and the self-identity guard passes instead of self-refusing
        `stale-gate-script`. The second train (mode=remote) is pinned and gated
        on the twin deterministically. It is not merely "twin eligible": allowing
        an adaptive router to send it back local would put two full gates on one
        Mac and recreate the CPU overload this two-host scheduler exists to stop."""
        sf = gate_state_path(self.root, train)
        sf.parent.mkdir(parents=True, exist_ok=True)
        receipt = sf.parent / f"receipt-{train.branch.replace('/', '__')}@{train.tip[:12]}.json"
        now = time.time()
        run_remote = bool(allow_remote and self.allow_remote_gate)
        gate_ws = self._effective_gate_ws()
        bound_tests = self._train_bound_tests(train, gate_ws)
        # Which BOX this remote gate belongs to. Recorded in the state file
        # before the child spawns, so the next tick's slot arithmetic sees the
        # box as taken even if the child is still starting up.
        host = (twin or TWIN_HOST) if run_remote else None
        self._write_json_atomic(sf, {
            "state": "running", "train": train.branch, "tip": train.tip,
            "base": train.base, "members": [m["id"] for m in train.members],
            "deadline": now + GATE_DEADLINE_S, "started_at": _iso(_now()),
            "receipt": str(receipt), "pid": None, "pid_started": None,
            # Both dispatch paths invoke run_gate_child with a deadline-derived
            # child bound.  This is the premise on which a later refused reap
            # may release a new-format lease; legacy state files lack it.
            "self_bounded": True,
            "mode": "remote" if run_remote else "direct",
            "gate_workspace": str(gate_ws),
            "bound_tests": bound_tests,
            "twin": host,
        })
        head = [self.python, str(Path(__file__).resolve()), "run-gate",
                "--state-file", str(sf), "--candidate", train.branch,
                "--tip", train.tip, "--receipt", str(receipt)]
        for node in bound_tests:
            head += ["--bound-test", node]
        if run_remote:
            # The second slot is the twin slot, not another adaptive local slot.
            child = head + ["--mode", "remote", "--gate-workspace", str(gate_ws),
                            "--twin", host,
                            "--local-repo", str(self.repo), "--repo-key", self.repo_key]
            for pth in train.paths:
                child += ["--target", pth]
        else:
            # Local: run the pinned workspace's OWN gate script directly so the
            # self-identity guard sees a matching judge.
            child = head + ["--mode", "direct", "--gate-workspace", str(gate_ws)]
        env = dict(os.environ)
        env["THREELOOPS_ALLOW_REMOTE_GATE"] = "1" if run_remote else "0"
        try:
            proc = subprocess.Popen(
                child, env=env, start_new_session=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL)
        except OSError as exc:
            self._write_json_atomic(sf, {
                "state": "done", "rc": 127, "stdout": "", "stderr": f"could not spawn gate child: {exc}",
                "receipt": None, "duration_s": 0.0, "train": train.branch, "tip": train.tip})
            self._alert(f"could not spawn gate child for {train.branch} (instrument): {exc}")
            return
        # Record the child's pid AND its start-time identity so the I8 reap
        # backstop can refuse to killpg a recycled pid.  This publication is a
        # transaction: a live child without both facts cannot safely be reaped,
        # so failure terminates the held Popen before publishing a terminal
        # instrument outcome.
        try:
            st = json.loads(sf.read_text())
            st["pid"] = proc.pid
            try:
                st["pid_started"] = _pid_lstart(proc.pid)
            except OSError:
                # The identity probe may be unavailable in a hardened sandbox;
                # pid publication itself still succeeds and later follows the
                # explicit unverifiable/park path rather than disappearing.
                st["pid_started"] = None
            self._write_json_atomic(sf, st)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError, TypeError) as exc:
            reaped = False
            try:
                proc.terminate()
                proc.wait(timeout=2)
                reaped = True
            except (OSError, subprocess.TimeoutExpired):
                try:
                    proc.kill()
                    proc.wait(timeout=2)
                    reaped = True
                except (OSError, subprocess.TimeoutExpired):
                    pass
            terminal = {
                "state": "closed" if reaped else "running",
                "disposition": "pid-publication-failed" if reaped
                else "reap-unverifiable-parked",
                "reap": "reaped" if reaped else "unverifiable",
                "train": train.branch, "tip": train.tip, "pid": proc.pid,
                "reason": f"pid publication failed after spawn: {type(exc).__name__}: {exc}",
            }
            if reaped:
                terminal["closed_at"] = _iso(_now())
            else:
                # A failed terminate + kill is not proof of death.  Keep the
                # claimed slot and, for a remote dispatch, its exact twin busy
                # under the same durable clock as all other unverified reaps.
                terminal.update({
                    "mode": "remote" if run_remote else "direct",
                    "twin": host,
                    "parked_at": time.time(),
                })
            try:
                self._write_json_atomic(sf, terminal)
            except OSError:
                # The critical safety operation above already completed; retain
                # a loud durable path even if the state filesystem is failing.
                pass
            msg = (f"pid publication failed for {train.branch} pid={proc.pid}; "
                   + ("terminated untracked child" if reaped
                      else "child remains unverified and local slot is parked"))
            self._alert(msg)
            self._append_ledger({
                "ts": _iso(_now()), "role": ROLE, "event": "instrument_error",
                "id": train.members[0]["id"] if train.members else None,
                "actor": ACTOR,
                "detail": {"class": "instrument-error", "area": "gate-instrument",
                           "kind": "pid-publication-failed", "train": train.branch,
                           "tip": train.tip, "pid": proc.pid, "reason": msg},
            })
            self._log(msg)
            return
        mode = f"remote({host})" if run_remote else f"direct({gate_ws}/scripts/merge-gate.sh)"
        bound = f", bound tests: {len(bound_tests)}" if bound_tests else ""
        self._log(f"dispatched gate for {train.branch} @ {train.tip[:12]} "
                  f"(pid {proc.pid}, mode={mode}{bound})")

    # -- main movement: ONE fast-forward, two callers ------------------------

    def _head_ref(self) -> str | None:
        """The ref the serving checkout has checked out, or None when it is
        detached/unreadable. The serving checkout is PINNED to `main`; anything
        else means someone left it somewhere it should not be, and the only
        correct response is to move nothing."""
        rc, head_ref, _ = git(self.repo, "symbolic-ref", "--quiet", "HEAD")
        head_ref = head_ref.strip()
        return head_ref if rc == 0 and head_ref else None

    def _ff_only_advance(self, target: str) -> tuple[int, str]:
        """THE fast-forward of the serving checkout's `main`. The ONLY way this
        daemon ever moves main, and the ONLY implementation of it — the landing
        path (`land_train`) and the origin-sync path (`_sync_main_from_origin`)
        both go through here on purpose, so there can never be two notions of
        "advance main", one of which is lax.

        `git merge --ff-only` refuses anything that is not a clean fast-forward
        and neither caller is allowed to escalate that refusal into a reset, a
        force, or a checkout. Callers MUST have verified `_head_ref()` first;
        the merge advances CURRENT HEAD, so on the wrong branch it would advance
        the wrong ref.
        """
        rc, out, err = git(self.repo, "merge", "--ff-only", target)
        return rc, (err or out)

    #: git's phrasing when a fast-forward would clobber an untracked working-tree
    #: file. Matched ONLY on ``_ff_only_advance``'s own error text; divergence and
    #: local-ahead are classified and returned BEFORE the ff is ever attempted, so
    #: this never fires on them.
    _UNTRACKED_FF_COLLISION = "untracked working tree files would be overwritten"

    #: Turns OFF git's pathspec MAGIC (``:(glob)``, ``:(exclude)``, ``:/``…) for
    #: the whole invocation. EVERY command that hands one of git's own PARSED
    #: collision filenames to git AS A PATHSPEC must carry it: ``--`` stops OPTION
    #: parsing but leaves magic ACTIVE, so an untracked file literally named
    #: ``:(glob)**`` expands to match every path in the tree and the salvage sweeps
    #: up unrelated operator work (BLOCKER, pathspec-scope-escape, 2026-08-13).
    #: Deliberately NOT passed to ``hash-object``/``check-attr``, whose operands are
    #: FILE NAMES rather than pathspecs (verified on git 2.43: both treat a file
    #: named ``:(glob)**`` literally) — and it must never be combined with a
    #: ``:(literal)`` prefix, which under this flag would be read as part of the
    #: filename itself.
    _LITERAL = "--literal-pathspecs"

    #: Attributes whose presence means git does NOT store the file's bytes as they
    #: sit on disk. A ``filter`` driver is an arbitrary external program — a LOSSY
    #: clean filter maps DIFFERENT working-tree bytes onto the SAME blob sha, and
    #: ``git stash`` runs that same filter, so the operator's unique bytes would
    #: exist nowhere afterwards (BLOCKER, lossy-clean-filter-data-loss,
    #: 2026-08-13). ``working-tree-encoding`` re-encodes on the way in. Under
    #: either, neither the identity comparison nor the salvage preserves raw bytes,
    #: so a colliding file carrying one is NEVER salvaged. (``text``/``eol`` are
    #: deliberately not listed: eol normalisation is caught by the raw-vs-filtered
    #: hash agreement below, which proves the conversion is a no-op on these exact
    #: bytes — refusing on ``text`` would disable the fix in any repo that sets
    #: ``* text=auto``.)
    _CONTENT_TRANSFORM_ATTRS = ("filter", "working-tree-encoding")

    def _parse_untracked_ff_collisions(self, ff_err: str) -> list[str]:
        """The repo-relative paths git listed as untracked ff collisions.

        git prints them indented between the ``_UNTRACKED_FF_COLLISION`` header and
        the ``Please move or remove them`` footer; the first non-indented (or
        blank) line ends the list. Anything not so framed is ignored, so unrelated
        stderr can never be read as a path to delete.

        This is only a CANDIDATE GENERATOR: every name it returns must still
        survive :meth:`_prove_redundant_untracked` in full, and that proof makes
        git itself echo the path it matched (``ls-tree``) and requires an exact
        string match. Only the leading indentation is stripped (never trailing
        whitespace, which can be part of a real filename), and a name git quoted
        (``core.quotePath`` for non-ASCII) simply fails the proof and is refused.
        """
        paths: list[str] = []
        collecting = False
        for line in ff_err.splitlines():
            if self._UNTRACKED_FF_COLLISION in line:
                collecting = True
                continue
            if not collecting:
                continue
            if not line.strip():
                break
            if line[:1].isspace():          # indented -> a listed path
                paths.append(line.lstrip().rstrip("\r"))
            else:                            # footer / any non-indented line -> end
                break
        return paths

    def _tree_blob_at(self, treeish: str, rel: str) -> str | None:
        """The blob sha ``treeish`` carries at exactly ``rel``, or None.

        ``ls-tree -z`` rather than ``rev-parse <rev>:<path>`` on purpose: it also
        reports the MODE and TYPE (so a symlink/gitlink/subtree entry is refused
        rather than compared as if it were a regular file), it echoes the path it
        actually matched so the caller can require an exact string match, and ``-z``
        emits that path UNQUOTED regardless of ``core.quotePath``. Anything other
        than exactly one regular-file blob record returns None -> the caller
        refuses.
        """
        rc, out, _ = git(self.repo, self._LITERAL, "ls-tree", "-z", "--full-tree",
                         treeish, "--", rel)
        if rc != 0 or not out:
            return None
        records = [r for r in out.split("\0") if r]
        if len(records) != 1:
            return None
        info, tab, path = records[0].partition("\t")
        if not tab or path != rel:
            return None
        fields = info.split(" ")
        if len(fields) != 3:
            return None
        mode, kind, sha = fields
        if kind != "blob" or mode not in ("100644", "100755"):
            return None
        return _full_sha(sha)

    def _worktree_file_key(self, abs_path: Path) -> tuple | None:
        """An identity key for the on-disk file — device, inode, size, mtime_ns,
        ctime_ns, mode — or None when it is not a plain regular file (a symlink, a
        directory, a missing or unstattable path all fail CLOSED here).

        Taken before and re-taken after the content proof, and again immediately
        before the destructive step: an operator write between check and use
        changes mtime/ctime (and usually size), so a stale proof can never be
        spent (HIGH, compare/action TOCTOU, 2026-08-13). ``lstat`` is deliberate —
        a symlink must never be followed into a file it points at.
        """
        try:
            st = os.lstat(abs_path)
        except OSError:
            return None
        if not stat.S_ISREG(st.st_mode):
            return None
        return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns,
                st.st_ctime_ns, st.st_mode)

    def _has_content_transform(self, rel: str) -> bool | None:
        """Does a content-rewriting attribute apply to ``rel``? None == unreadable.

        None is NOT False: an unreadable attribute stack means we cannot prove git
        stores this file's bytes verbatim, and the caller must refuse.
        """
        rc, out, _ = git(self.repo, "check-attr", "-z", *self._CONTENT_TRANSFORM_ATTRS,
                         "--", rel)
        if rc != 0 or not out:
            return None
        fields = out.split("\0")
        # `<path>\0<attr>\0<value>\0` per attribute, in the order asked.
        values = fields[2::3]
        if len(values) != len(self._CONTENT_TRANSFORM_ATTRS):
            return None
        return any(v not in ("unspecified", "unset") for v in values)

    def _prove_redundant_untracked(self, target: str, rel: str) -> tuple[str, tuple] | None:
        """PROOF that ``rel`` is an untracked working-tree file whose bytes the
        fast-forward would restore EXACTLY. Returns ``(blob_sha, file_key)``, or
        None when any step cannot be positively established.

        Every check fails CLOSED, because the favourable answer here authorises a
        destructive step. In order: the path is repo-relative and non-traversing;
        ``target`` carries a regular-file BLOB at exactly that path; the working
        copy is a plain regular file; no content-rewriting attribute applies; git
        itself reports it as ``??`` (untracked — never a tracked-and-modified
        file) under a LITERAL pathspec; and its content hashes to the incoming
        blob BOTH with filters applied AND on its RAW on-disk bytes.

        THE RAW HASH IS THE LOAD-BEARING ONE (BLOCKER, lossy-clean-filter-data-
        loss). ``git hash-object`` without ``--no-filters`` runs the configured
        clean filter first, so under a lossy filter genuinely DIFFERENT operator
        bytes hash to the same blob sha and the file reads as redundant — and
        ``git stash`` applies that same filter, so the stash entry would hold only
        the filtered content and the operator's unique bytes would exist nowhere.
        ``--no-filters`` hashes the bytes as they sit on disk, which is the only
        hash that can license removing them. The filtered hash is kept as a
        SECOND, narrower gate (both must equal the incoming blob), so an
        eol/encoding conversion that is not a no-op on these exact bytes also
        refuses rather than salvages.
        """
        if not rel or os.path.isabs(rel) or any(
                p in ("", ".", "..") for p in rel.split("/")):
            return None                                   # never escape the repo
        want = self._tree_blob_at(target, rel)
        if not want:
            return None
        abs_path = self.repo / rel
        key = self._worktree_file_key(abs_path)
        if key is None or abs_path.is_symlink():
            return None
        transformed = self._has_content_transform(rel)
        if transformed is not False:                      # True or unreadable
            return None
        # `--untracked-files=all` is EXPLICIT rather than relied upon: git already
        # reports the individual file (not a collapsed `?? dir/`) when a pathspec
        # names it, but the exact-string match below must not depend on that
        # subtlety, and this makes the record shape identical to the whole-tree
        # snapshot `_porcelain_status` diffs the salvage against.
        rc_s, st_out, _ = git(self.repo, self._LITERAL, "status", "--porcelain", "-z",
                              "--untracked-files=all", "--", rel)
        records = [r for r in st_out.split("\0") if r]
        if rc_s != 0 or len(records) != 1 or records[0] != f"?? {rel}":
            return None
        # FILTERED first, then RAW. Both must equal the incoming blob.
        rc_f, filtered, _ = git(self.repo, "hash-object", "--", rel)
        rc_r, raw, _ = git(self.repo, "hash-object", "--no-filters", "--", rel)
        if rc_f != 0 or rc_r != 0:
            return None
        if _full_sha(filtered) != want or _full_sha(raw) != want:
            return None
        if self._worktree_file_key(abs_path) != key:      # moved under the probe
            return None
        return want, key

    def _redundant_untracked_ff_blockers(self, target: str,
                                         ff_err: str) -> dict[str, tuple[str, tuple]]:
        """Colliding untracked files PROVEN safe to move out of the ff's way, as
        ``{path: (blob_sha, file_key)}`` — the evidence the salvage re-checks
        under exclusion before it touches anything.

        A colliding file qualifies ONLY when its RAW on-disk bytes are identical
        to the version ``target`` would install: such a file carries no unique
        work — the fast-forward restores an identical tracked copy — so salvaging
        it loses nothing and clears the deadlock. Every other colliding file
        (distinct content, filtered content, unreadable, a symlink, actually
        tracked, or absent from ``target``) is EXCLUDED, so the caller stays
        refused and the operator's distinct copy is never destroyed. This narrows
        the estate's "never auto-touch untracked work" doctrine to the one case
        where "untracked" and "byte-identical to what we are about to write"
        coincide.
        """
        if self._UNTRACKED_FF_COLLISION not in ff_err:
            return {}
        proven: dict[str, tuple[str, tuple]] = {}
        for rel in self._parse_untracked_ff_collisions(ff_err):
            evidence = self._prove_redundant_untracked(target, rel)
            if evidence is not None:
                proven[rel] = evidence
        return proven

    def _porcelain_status(self) -> frozenset[str] | None:
        """The WHOLE working tree's porcelain status as a set of records, or None
        when it is unreadable. ``-z`` (unquoted paths) and ``--untracked-files=all``
        so an individual file inside an untracked directory is its own record: this
        is the before/after evidence that a salvage removed EXACTLY the paths it
        proved and nothing else.
        """
        rc, out, _ = git(self.repo, self._LITERAL, "status", "--porcelain", "-z",
                         "--untracked-files=all", timeout=120)
        if rc != 0:
            return None
        return frozenset(r for r in out.split("\0") if r)

    def _concurrent_writer_reason(self) -> str | None:
        """Why another writer may be touching the serving checkout right now, or
        None when none is detectable. Unreadable probes return a REASON (refuse),
        never None: "I could not check" is not "nobody is there"."""
        rc, out, _ = git(self.repo, "rev-parse", "--git-path", "index.lock")
        if rc != 0 or not out.strip():
            return "git's index.lock path is unreadable (instrument)"
        lock = Path(out.strip())
        if not lock.is_absolute():
            lock = self.repo / lock
        if lock.exists():
            return f"another git process holds {lock}"
        holder_path = self.root / "locks" / "gate-loop.lock"
        try:
            holder = json.loads(holder_path.read_text(encoding="utf-8")).get("pid")
        except (OSError, ValueError, json.JSONDecodeError, AttributeError):
            return None                       # no readable holder record -> not a writer
        if not isinstance(holder, int) or holder == os.getpid():
            return None
        try:
            os.kill(holder, 0)
        except PermissionError:
            pass                              # alive, another user
        except OSError:
            return None                       # holder gone: a stale lock file
        return f"another gate-loop process (pid {holder}) holds the single-writer lock"

    @contextlib.contextmanager
    def _salvage_exclusion(self):
        """Hold writer exclusion across the FINAL proof and the destructive step,
        yielding None when it is held and a REASON STRING when it is not.

        A stale proof is what makes the compare/action window dangerous (HIGH,
        hash/stash TOCTOU): the operator can change the file after it hashes
        identical and before it is stashed. Nothing can lock a human out of a
        working tree, so this does the two things that ARE provable — take an
        exclusive `flock` no other gate-loop process can hold at the same time,
        and refuse outright when another writer is detectably active (git's
        `index.lock`, a second gate-loop tick) — and the caller re-proves every
        path INSIDE the window and re-checks the stash entry afterwards. When
        exclusion cannot be established the salvage is REFUSED: a lost landing
        tick costs minutes, a lost operator file is unrecoverable.
        """
        lock_path = self.root / "locks" / "origin-sync-salvage.lock"
        reason: str | None = None
        fd: int | None = None
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0),
                         0o644)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            reason = (f"the salvage lock {lock_path} is unavailable "
                      f"({type(exc).__name__}: {exc})")
            if fd is not None:
                os.close(fd)
                fd = None
        if reason is None:
            reason = self._concurrent_writer_reason()
        try:
            yield reason
        finally:
            if fd is not None:
                with contextlib.suppress(OSError):
                    fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

    def _salvage_untracked_ff_blockers(self, target: str,
                                       proven: dict[str, tuple[str, tuple]],
                                       why: str) -> bool:
        """Stash the PROVEN-redundant collisions so the ff can install the
        identical tracked copy. True == salvaged (the caller retries the advance);
        False == refused with the working tree untouched, or refused LOUDLY after
        a stash whose effect could not be proven (in which case the content is in
        the stash — never destroyed — and main still does not move).

        The proof is re-run INSIDE the exclusion window and the result is verified
        AFTERWARDS: exactly the proven paths left the working tree, our entry
        exists, and it carries the exact blob we proved. Any gap refuses.
        """
        with self._salvage_exclusion() as blocked:
            if blocked:
                self._log(f"origin sync ({why}): {len(proven)} redundant untracked "
                          f"ff-blocker(s) look salvageable but writer exclusion could not "
                          f"be established ({blocked}) — refusing the advance rather than "
                          "acting on evidence another writer can invalidate")
                return False
            for rel, evidence in proven.items():
                if self._prove_redundant_untracked(target, rel) != evidence:
                    self._log(f"origin sync ({why}): {rel} changed between the identity "
                              "check and the salvage — refusing the advance and leaving "
                              "the working tree exactly as it is")
                    return False
            before = self._porcelain_status()
            if before is None:
                self._log(f"origin sync ({why}): the working tree's status is unreadable "
                          "(instrument) — refusing the salvage rather than acting blind")
                return False
            label = f"origin-sync-salvage {_iso(_now())} redundant-untracked"
            # `_LITERAL`: these paths came out of git's OWN error text and are
            # pathspecs here — without it a name that is itself pathspec magic
            # would sweep in unrelated untracked work.
            srsc, srout, srerr = git(self.repo, self._LITERAL, "stash", "push",
                                     "--include-untracked", "-m", label,
                                     "--", *proven, timeout=120)
            if srsc != 0:
                self._log(f"origin sync ({why}): could not salvage the {len(proven)} "
                          f"redundant untracked ff-blocker(s) into a stash "
                          f"({(srerr or srout)[:160]}) — leaving them and refusing the advance")
                return False
            sha = self._find_own_stash(label)
            after = self._porcelain_status()
            expected = before - {f"?? {rel}" for rel in proven}
            if sha is None or after is None or after != expected:
                self._alert(
                    f"origin sync ({why}): the salvage stash did not do exactly what it "
                    f"proved — entry {sha or 'MISSING'}, status delta "
                    f"{sorted((after or frozenset()) ^ expected)[:5]} — NOTHING is destroyed "
                    f"(recover with `git -C {self.repo} stash list` / `git stash pop`), main "
                    "was NOT advanced, and the advance is refused pending a human")
                return False
            for rel, (want, _key) in proven.items():
                if self._tree_blob_at(f"{sha}^3", rel) != want:
                    self._alert(
                        f"origin sync ({why}): stash entry {sha} does not carry the proven "
                        f"bytes of {rel} — it changed under the salvage. The content is in "
                        f"the stash (`git -C {self.repo} stash show -p stash@{{0}}`), main "
                        "was NOT advanced, and the advance is refused pending a human")
                    return False
            self._log(
                f"origin sync ({why}): salvaged {len(proven)} redundant untracked file(s) "
                f"byte-identical to {self.remote}/main into stash {sha[:12]} to clear the "
                f"fast-forward ({', '.join(list(proven)[:5])}"
                f"{'…' if len(proven) > 5 else ''}) — retrying the advance")
            return True

    def _divergence_marker(self) -> Path:
        return self._alert_marker_path("main-diverged-from-origin")

    def _push_deferred_marker(self) -> Path:
        """Edge marker for `_on_push_refused`'s hold_base deferral (Gemini/Grok
        polish). Its CONTENT is the SET of push_keys already announced this
        episode (``{"push_keys": [...]}``), NOT a bare existence flag. Each
        deferred train's `push_deferred` ledger row fires ONCE — keyed on ITS OWN
        push_key — so a burst holds without storming AND a second, concurrently
        deferred train (MAX_CONCURRENT_GATES > 1) is never suppressed by the
        first's marker (Gemini favourable-absence finding). `_sync_main_from_origin`
        deletes the whole file the moment the base is current or advances: an
        advance resolves EVERY outstanding deferral at once, re-arming all keys."""
        return self._alert_marker_path("push-deferred-gate-in-flight")

    def _record_divergence(self, local_sha: str, origin_sha: str) -> None:
        """Refuse a diverged origin LOUDLY, once per distinct divergence.

        Fail-CLOSED: no fast-forward is possible, and the two repairs a machine
        could reach for — `reset --hard origin/main` (destroys local commits) and
        `push --force` (destroys origin's) — are both destructive and both
        forbidden. So the daemon moves nothing and says so. Assembly continues on
        the local view; the landing path's own push guard is what stops a train
        from being recorded as merged while origin disagrees.

        Edge-triggered on the (local, origin) sha pair: a divergence nobody has
        fixed yet is an UNCHANGED INPUT, and alerting on it every 60s trains the
        operator to ignore the alert. A new divergence re-alerts.
        """
        msg = (f"main has DIVERGED from {self.remote}/main: local {local_sha[:12]} carries "
               f"commits that origin {origin_sha[:12]} does not, and origin carries commits "
               "local does not. NOT fast-forwarding, NOT resetting, NOT forcing — a human "
               "must reconcile. Assembly continues on the local view; no train can land "
               "until origin and local agree.")
        self._log(msg)
        self._mark_tick_degraded("main diverged from origin")
        marker = self._divergence_marker()
        prior = self._read_marker(marker)
        if prior.get("local_sha") == local_sha and prior.get("origin_sha") == origin_sha:
            return
        self._write_json_atomic(marker, {
            "kind": "main-diverged-from-origin", "started_at": time.time(),
            "local_sha": local_sha, "origin_sha": origin_sha, "remote": self.remote,
        })
        self._alert(msg)
        self._append_ledger({
            "ts": _iso(_now()), "role": ROLE, "event": "instrument_error",
            "id": None, "actor": ACTOR,
            "detail": {"class": "instrument-error", "area": "tooling",
                       "kind": "main-diverged-from-origin", "reason": msg,
                       "remote": self.remote, "local_sha": local_sha,
                       "origin_sha": origin_sha,
                       "remedy": ("reconcile local main with the remote by hand (rebase the "
                                  "local-only commits onto origin/main, or land them through "
                                  "the normal path); the daemon will never force either side")},
        })

    def _content_free_heal_count_24h(self) -> int:
        """Count `main-reconciled-content-free` heals recorded in the last 24h.

        Grok follow-up on #410: a heal is a safety net, not a fix -- if it
        keeps firing, something upstream is repeatedly re-merging content
        that already landed, and that producer needs to be found and
        stopped. Used by `_content_free_reconcile` to escalate its own
        success alert once the rate crosses 3/24h; the heal's behavior is
        otherwise unchanged.

        Same tolerant `iter_events` reader `_ledger_has_event` uses: a torn/
        garbage line must not hide a heal record and undercount how often
        this is actually firing (favourable absence is the direction this
        repo refuses)."""
        path = self.root / "ledger.jsonl"
        if not path.exists():
            return 0
        cutoff = _now() - timedelta(hours=24)
        count = 0
        for ev in iter_events(path.read_text(encoding="utf-8", errors="replace")):
            detail = ev.get("detail")
            if not isinstance(detail, dict):
                continue
            if detail.get("kind") != "main-reconciled-content-free":
                continue
            ts = _parse(ev.get("ts"))
            if ts is not None and ts >= cutoff:
                count += 1
        return count

    def _content_free_reconcile(self, local_sha: str, origin_sha: str, *,
                                why: str) -> bool:
        """AUTO-HEAL for the content-free divergence class (2026-08-14 stall).

        A divergence whose LOCAL-only commits change no tree relative to the
        merge base is not a real fork: every byte of local-only work is already
        contained in origin's history (the recurring shape: a lane re-merged
        locally after its content had already landed via a GitHub PR — commit
        OBJECTS differ, content does not). Refusing it parks the entire
        pipeline behind a human to protect nothing; measured 2026-08-13/14 it
        held 233 candidates and 3 gate-PASSED trains for 5+ hours (finding
        sha256:26be6580…, remedy executed by the operator's overnight session).

        This is deliberately NARROWER than the banned repairs. `reset --hard`
        and `push --force` destroy work by construction; this path first
        PROVES no COMMITTED work would be destroyed (empty `git diff` against
        the merge base — the narrow untracked-file residual is documented at
        the collision re-probe below), preserves the old tip on a salvage
        branch anyway, and then
        moves the ref by ATOMIC compare-and-swap (`update-ref <new> <old>`,
        which refuses if a peer committed after the proof — the Grok
        FINDING-1 TOCTOU) followed by `read-tree -m -u`, which refuses rather
        than clobber any tracked file a peer modified in the window; a
        read-tree refusal compensates the ref swap back. Every precondition
        failure falls through to the existing fail-closed
        `_record_divergence` path unchanged, so this can only ever heal MORE
        narrowly than a human would. Kill switch:
        GATE_LOOP_AUTOHEAL_CONTENT_FREE=0 restores the old always-refuse
        behavior (for the launchd-run daemon: set it in the plist
        EnvironmentVariables and reload; a shell export does not reach it).

        BASE-MOTION NOTE: a heal moves main exactly like an out-of-band
        advance; a gate in flight then hits the documented honest path
        (`stale-base` at land, one re-gate). A heal cannot repeat-churn: after
        it, main == origin/main, and this daemon never creates the local-only
        commits that would re-arm it.
        """
        if os.environ.get("GATE_LOOP_AUTOHEAL_CONTENT_FREE", "1") == "0":
            return False
        rc, mb, _ = git(self.repo, "merge-base", local_sha, origin_sha)
        mb = mb.strip()
        if rc != 0 or not _full_sha(mb):
            return False
        rc, _, _ = git(self.repo, "diff", "--quiet", mb, local_sha)
        if rc != 0:
            return False   # real local content (rc 1) or diff error: fail closed
        rc, dirty, _ = git(self.repo, "status", "--porcelain")
        if rc != 0 or any(ln and not ln.startswith("??")
                          for ln in dirty.splitlines()):
            return False   # a peer session's tracked edits sit here: hands off
        # IGNORED/UNTRACKED COLLISION GUARD (Sol F2, PR #410 review). Porcelain
        # never lists ignored files, so "zero tracked modifications" cannot see
        # an ignored file with real content sitting at a path that origin's
        # tree begins to TRACK — materializing origin would overwrite it with
        # no salvage ref and no git object to recover from. Refuse the heal if
        # ANY path added between the two trees already exists on disk.
        # NUL-delimited on purpose (Sol F24, round 4): `--name-only` without
        # `-z` C-quotes non-ASCII names (café.env → "caf\303\251.env"), and
        # probing that literal string finds nothing — the guard would pass
        # while the REAL path gets overwritten. `-z` emits raw bytes.
        rc, added, _ = git(self.repo, "diff", "-z", "--name-only",
                           "--diff-filter=A", local_sha, origin_sha)
        if rc != 0:
            return False
        added_paths = [p for p in added.split("\0") if p]
        # lexists, not exists: a BROKEN symlink at a collided path is real
        # local state too, and Path.exists() follows the link and lies about
        # its presence (Sol F5, round 2).
        collisions = [p for p in added_paths
                      if os.path.lexists(Path(self.repo) / p)]
        if collisions:
            self._log(f"origin sync ({why}): content-free reconcile refused — origin "
                      f"newly tracks {len(collisions)} path(s) that already exist "
                      f"locally (first: {collisions[0]!r}); an ignored/untracked file "
                      "with real content would be overwritten; falling through to the "
                      "fail-closed divergence path")
            return False
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        salvage = f"salvage/gl-contentfree-{stamp}-{local_sha[:7]}"
        # WRITE-AHEAD EVIDENCE (Sol F4). The heal's evidence must survive a
        # crash or a ledger-append failure AFTER the ref has moved: once
        # main == origin, nothing else in the repository proves a heal
        # happened. The marker is written durably BEFORE the swap and deleted
        # only after the ledger append succeeds.
        # Marker path is PER-HEAL-ATTEMPT, keyed on BOTH shas (Sol F8 rounds
        # 2-3): a fixed path — or one keyed on local_sha alone — would let a
        # later attempt silently overwrite an earlier attempt's un-replayed
        # evidence.
        intent_marker = self._alert_marker_path(
            f"content-free-heal-intent-{local_sha}-{origin_sha}")
        self._write_json_atomic(intent_marker, {
            "kind": "content-free-heal-intent", "started_at": time.time(),
            "local_sha": local_sha, "origin_sha": origin_sha,
            "merge_base": mb, "salvage_ref": salvage, "remote": self.remote,
            "why": why})
        rc, _, err = git(self.repo, "branch", salvage, local_sha)
        if rc != 0:
            intent_marker.unlink(missing_ok=True)
            self._log(f"origin sync ({why}): content-free reconcile refused — salvage "
                      f"branch {salvage} not created ({err[:160]}); falling through to "
                      "the fail-closed divergence path")
            return False
        # COMPARE-AND-SWAP on the ref (Grok FINDING-1, PR #410 review). The
        # checks above ran against a SNAPSHOT of main; a peer session could
        # commit real work to the serving checkout in the gap, and a blind
        # reset would strand that commit reflog-only — the exact TOCTOU class
        # this file's rollback path already treats as a BLOCKER. `update-ref
        # <new> <old>` is atomic at the ref store: it refuses unless main
        # still IS the snapshot we proved content-free.
        rc, _, err = git(self.repo, "update-ref", "refs/heads/main",
                         origin_sha, local_sha)
        if rc != 0:
            # Un-mint the salvage: main still reaches local_sha (a peer commit
            # can only have built ON it), so `-d` agrees it is safe.
            git(self.repo, "branch", "-d", salvage)
            intent_marker.unlink(missing_ok=True)
            self._log(f"origin sync ({why}): content-free reconcile refused — main "
                      f"moved between proof and swap ({err[:120]}); a concurrent "
                      "writer owns the tip now; falling through to the fail-closed "
                      "divergence path")
            return False
        # LAST-INSTANT COLLISION RE-PROBE (Sol F6, rounds 2-4): a file can
        # appear at a collided path in the window between the first probe and
        # the materialization below. Re-checking here, after the CAS, shrinks
        # that window to one subprocess call. ACCEPTED RESIDUAL, stated
        # plainly (round-4 evidence corrected an earlier claim): an ignored/
        # untracked file that a peer writes at exactly an origin-newly-tracked
        # path INSIDE the read-tree call is overwritten SILENTLY and is NOT
        # recoverable from the salvage branch (salvage holds commits, not
        # untracked files). Closing this fully requires either refusing every
        # heal where origin adds any path (which is nearly all real
        # divergences — the feature would never fire) or a peer-cooperative
        # write lock that does not exist. Exposure: heals are rare (measured
        # ~1/night) and the racing write must hit one of the added paths
        # within milliseconds. The kill switch disables the whole carve-out
        # for operators who reject this tradeoff.
        late = [p for p in added_paths if os.path.lexists(Path(self.repo) / p)]
        if late:
            rc2, _, err2 = git(self.repo, "update-ref", "refs/heads/main",
                               local_sha, origin_sha)
            if rc2 != 0:
                # Same double-failure shape as the read-tree branch below
                # (Grok delta finding D3-1): CAS moved the ref, compensation
                # refused. Poison with evidence; salvage and marker survive
                # as the operator's remedy anchors.
                self._write_json_atomic(self._poison_path(), {
                    "poisoned": True, "at": _iso(_now()), "by": ACTOR,
                    "reason": ("content-free heal double failure: late-arriving "
                               "collision refused the heal AND the compensating "
                               "ref swap failed — main ref is at origin with the "
                               "OLD worktree"),
                    "remote": self.remote,
                    "origin_expected_sha": origin_sha,
                    "local_diverged_sha": local_sha,
                    "collision_path": late[0],
                    "rollback_error": err2[:500],
                    "salvage_ref": salvage,
                    "remedy": ("verify main's ref vs worktree, reconcile by "
                               "hand (the old tip is on the salvage ref), then "
                               "delete this file to resume the daemon")})
                self._alert(f"content-free reconcile: late collision at {late[0]!r} "
                            f"AND the compensating ref swap failed ({err2[:120]}) — "
                            f"main ref is at {origin_sha[:12]} with the OLD worktree; "
                            "daemon POISONED until an operator reconciles (instrument)")
                # Raise, do not return (Sol F7 round 3): a return would let
                # THIS tick continue into candidate loading and landing on top
                # of the inconsistent state; the land path's double failure
                # raises for exactly this reason.
                raise DaemonPoisoned(
                    "content-free heal double failure (late collision)")
            git(self.repo, "branch", "-d", salvage)
            intent_marker.unlink(missing_ok=True)
            self._log(f"origin sync ({why}): content-free reconcile refused — path "
                      f"{late[0]!r} appeared at a collided location mid-heal; "
                      "ref swap compensated; falling through to the "
                      "fail-closed divergence path")
            return False
        # Materialize index+worktree old-tree -> origin-tree. `read-tree -m -u`
        # REFUSES (rc!=0) rather than clobber any tracked file a peer modified
        # after the porcelain check above — the uncommitted half of the same
        # TOCTOU. On refusal, compensate the ref swap and stand down.
        rc, _, err = git(self.repo, "read-tree", "-m", "-u", local_sha, origin_sha)
        if rc != 0:
            rc2, _, err2 = git(self.repo, "update-ref", "refs/heads/main",
                               local_sha, origin_sha)
            if rc2 == 0:
                # Only a SUCCESSFUL compensation un-mints the salvage; in the
                # double-failure branch below the salvage ref is part of the
                # operator's remedy and must survive. (`-d` would refuse there
                # anyway — the old tip is no longer reachable from main — but
                # the intent should not lean on that accident.)
                git(self.repo, "branch", "-d", salvage)
            if rc2 != 0:
                # DOUBLE FAILURE (Sol F7, round 2): ref at origin, worktree
                # old, and the swap-back ALSO refused. Continuing to land on
                # this inconsistent state compounds it — freeze the daemon
                # with a durable poison marker, exactly like the land-path's
                # push-and-rollback double failure.
                self._write_json_atomic(self._poison_path(), {
                    "poisoned": True, "at": _iso(_now()), "by": ACTOR,
                    "reason": ("content-free heal double failure: read-tree "
                               "refused AND the compensating ref swap failed — "
                               "main ref is at origin with the OLD worktree"),
                    "remote": self.remote,
                    "origin_expected_sha": origin_sha,
                    "local_diverged_sha": local_sha,
                    "read_tree_error": err[:500],
                    "rollback_error": err2[:500],
                    "salvage_ref": salvage,
                    "remedy": ("verify main's ref vs worktree, reconcile by "
                               "hand (the old tip is on the salvage ref), then "
                               "delete this file to resume the daemon")})
                self._alert("content-free reconcile: read-tree refused AND the "
                            f"compensating ref swap failed ({err2[:120]}) — main ref "
                            f"is at {origin_sha[:12]} with the OLD worktree; daemon "
                            "POISONED until an operator reconciles (instrument)")
                # Raise, do not return (Sol F7 round 3): see the sibling
                # late-collision branch.
                raise DaemonPoisoned(
                    "content-free heal double failure (read-tree)")
            else:
                intent_marker.unlink(missing_ok=True)
                self._log(f"origin sync ({why}): content-free reconcile refused — a "
                          f"tracked file changed under the heal ({err[:120]}); ref "
                          "swap compensated, nothing moved; falling through to the "
                          "fail-closed divergence path")
            return False
        rc, now_sha, _ = git(self.repo, "rev-parse", "--verify", "main")
        rc_c, dirty_after, _ = git(self.repo, "status", "--porcelain")
        tracked_dirty_after = rc_c != 0 or any(
            ln and not ln.startswith("??") for ln in dirty_after.splitlines())
        if rc != 0 or now_sha.strip() != origin_sha or tracked_dirty_after:
            self._alert(f"content-free reconcile: post-swap verify FAILED — main at "
                        f"{now_sha.strip()[:12]!r} (expected {origin_sha[:12]}), "
                        f"tracked_dirty={tracked_dirty_after} (instrument); treating "
                        "as an unreconciled divergence")
            self._append_ledger({
                "ts": _iso(_now()), "role": ROLE, "event": "instrument_error",
                "id": None, "actor": ACTOR,
                "detail": {"kind": "main-reconcile-verify-failed", "area": "tooling",
                           "class": "instrument-error", "remote": self.remote,
                           "local_sha": local_sha, "origin_sha": origin_sha,
                           "salvage_ref": salvage}})
            # The ledger record above IS the durable evidence for this branch;
            # the write-ahead marker would otherwise sit orphaned (Grok R2-1).
            intent_marker.unlink(missing_ok=True)
            return False
        # Prior heals only -- this heal's own ledger record has not been
        # written yet, so the count below never includes it.
        prior_heals_24h = self._content_free_heal_count_24h()
        msg = (f"main auto-reconciled with {self.remote}/main: the divergence ({why}) was "
               f"CONTENT-FREE (git diff {mb[:12]}..{local_sha[:12]} empty — every "
               f"local-only commit's content already reached origin); old tip "
               f"{local_sha[:12]} preserved on {salvage}, main now {origin_sha[:12]} via "
               "CAS ref swap + guarded read-tree. No COMMITTED work discarded (the "
               "narrow untracked-file residual documented in _content_free_reconcile "
               "applies); assembly continues on the fresh base. Operator kill switch: set "
               "GATE_LOOP_AUTOHEAL_CONTENT_FREE=0 in the gate-loop launchd plist "
               "EnvironmentVariables (or loop env) and reload — a shell export does "
               "not reach the launchd job.")
        self._log(msg)
        alert_msg = msg
        if prior_heals_24h >= 3:
            alert_msg = (
                f"{msg} 3+ content-free heals in 24h — a writer is repeatedly "
                "re-merging landed content; find and stop the producer (heals "
                "are a safety net, not the fix)."
            )
        self._alert(alert_msg)
        try:
            # `instrument_error` is the schema-valid shape for a self-healed
            # instrument condition (id may be null); the non-ff re-anchor path
            # uses it for exactly this kind of benign automatic repair.
            # "observed" is NOT in _LEDGER_EVENT_NAMES (Sol F3).
            heal_id = "sha256:" + hashlib.sha256(
                f"contentfree:{local_sha}:{origin_sha}".encode()).hexdigest()
            self._append_ledger({
                "ts": _iso(_now()), "role": ROLE, "event": "instrument_error",
                "id": heal_id, "actor": ACTOR,
                "detail": {"kind": "main-reconciled-content-free", "area": "tooling",
                           "class": "instrument-error",
                           "reason": msg, "remote": self.remote,
                           "local_sha": local_sha, "origin_sha": origin_sha,
                           "merge_base": mb, "salvage_ref": salvage,
                           "falsifier": f"git diff --quiet {mb} {local_sha} (rc 0)"}})
        except Exception as exc:   # Sol F4: the heal already happened; its
            # evidence must survive the append failure. The write-ahead
            # intent marker stays in place and the alert (durable in
            # ALERTS.md) names it; a later iteration can re-emit the record.
            self._alert(f"content-free reconcile SUCCEEDED but the ledger append "
                        f"failed ({exc}); durable evidence retained in "
                        f"{intent_marker} — re-emit the ledger record from it "
                        "(instrument)")
            self._divergence_marker().unlink(missing_ok=True)
            return True
        intent_marker.unlink(missing_ok=True)
        self._divergence_marker().unlink(missing_ok=True)
        return True

    def _local_ahead_marker(self) -> Path:
        return self._alert_marker_path("main-local-ahead-unreviewed")

    def _record_local_ahead(self, local_sha: str, origin_sha: str) -> None:
        """Refuse an UNEXPLAINED local-ahead-of-origin state LOUDLY, once per
        distinct (local, origin) pair. FAIL-CLOSED.

        BLOCKER (local-ahead-pushes-unreviewed). The daemon pushes every landing
        ATOMICALLY (ff-merge then push, rolled back together on failure), so it
        never legitimately leaves local `main` ahead of `origin/main`. A commit
        that IS local-ahead therefore has no gate or push provenance — it is a
        manual commit on the serving checkout, or a stranded landing — and the
        next train's fast-forward push would carry it onto `origin/main` for
        free. So the daemon moves NOTHING toward origin over it and says so; a
        human reconciles. Assembly still runs (the same fail-open-on-assembly the
        divergence path takes); the LAND path is what actually refuses the push.

        Edge-triggered on the (local, origin) sha pair, exactly like
        `_record_divergence`: an anomaly nobody has fixed yet is an UNCHANGED
        INPUT, and re-alerting on it every tick trains the operator to ignore it.
        """
        msg = (f"main is AHEAD of {self.remote}/main: local {local_sha[:12]} carries "
               f"commit(s) that origin {origin_sha[:12]} does not, with NO gate or push "
               "provenance. The daemon pushes every landing atomically, so this is not a "
               "healthy state — REFUSING to land or push any train that would carry these "
               "ungated commits to origin/main. A human must reconcile: land the "
               f"local-only commits through the normal gate path, or reset local main to "
               f"{self.remote}/main.")
        self._log(msg)
        self._mark_tick_degraded("main ahead of origin with no provenance")
        marker = self._local_ahead_marker()
        prior = self._read_marker(marker)
        if prior.get("local_sha") == local_sha and prior.get("origin_sha") == origin_sha:
            return
        self._write_json_atomic(marker, {
            "kind": "main-local-ahead-unreviewed", "started_at": time.time(),
            "local_sha": local_sha, "origin_sha": origin_sha, "remote": self.remote,
        })
        self._alert(msg)
        self._append_ledger({
            "ts": _iso(_now()), "role": ROLE, "event": "instrument_error",
            "id": None, "actor": ACTOR,
            "detail": {"class": "instrument-error", "area": "tooling",
                       "kind": "main-local-ahead-unreviewed", "reason": msg,
                       "remote": self.remote, "local_sha": local_sha,
                       "origin_sha": origin_sha,
                       "remedy": ("land the local-only commits through the normal gate "
                                  f"path, or reset local main to {self.remote}/main; the "
                                  "daemon will never push ungated commits to origin")},
        })

    def _local_ahead_provenance(self, cur_main: str) -> tuple[str, str | None]:
        """Can ``cur_main`` be fast-forward-pushed to ``<remote>/main`` WITHOUT
        carrying commits that have no gate/push provenance? Returns
        ``(tag, origin_sha)`` where tag is one of:

          ``clear``     — origin/main resolves and cur_main carries nothing it
                          lacks (or origin is ahead/equal): safe to land.
          ``no-origin`` — there is genuinely NO ``<remote>/main`` remote-tracking
                          ref to protect (brand-new repo, never fetched). Nothing
                          on origin can be over-written, so land. AFFIRMATIVE
                          absence only — never inferred from a failed read.
          ``ahead``     — cur_main carries commit(s) origin/main does not; an
                          ungated commit would ride to origin under this train's
                          ff-push. FAIL CLOSED.
          ``unknown``   — ``<remote>/main`` OR the ancestry between it and
                          cur_main could not be READ. FAIL CLOSED (BLOCKER,
                          local-ahead-unreadable-probe-fails-open).

        The whole point of the ``unknown`` tag: a probe that cannot answer is NOT
        the answer "no unexplained commits". The round-1 guard folded an
        unreadable origin/main into the favourable path (``not _full_sha`` skipped
        the check entirely), so a blocked or transient probe silently pushed
        possibly-ungated work to origin. Absence of evidence is never evidence of
        safety here; the two are kept strictly distinct."""
        rc_o, origin_main, _ = git(self.repo, "rev-parse", "--verify",
                                   f"{self.remote}/main")
        origin_main = origin_main.strip()
        if not _full_sha(origin_main):
            # rev-parse yielded no sha. Two very different worlds share that
            # outcome: the ref genuinely does not exist (nothing to protect) and
            # the ref exists but could not be read (a blocked hook, a transient
            # fault). Fail OPEN only when the ref is AFFIRMATIVELY absent; an
            # unreadable probe is UNKNOWN, and unknown fails closed.
            if self._remote_main_ref_absent():
                return "no-origin", None
            return "unknown", None
        if origin_main == cur_main:
            return "clear", origin_main
        rc_a, ahead, _ = git(self.repo, "rev-list", "--max-count=1", cur_main,
                             "--not", origin_main)
        if rc_a != 0:
            # Ancestry undeterminable — we cannot prove cur_main carries nothing
            # ungated, so we must not carry it to origin.
            return "unknown", origin_main
        if ahead.strip():
            return "ahead", origin_main
        return "clear", origin_main

    def _remote_main_ref_absent(self) -> bool:
        """True ONLY when ``<remote>/main`` AFFIRMATIVELY has no remote-tracking
        ref. ``git show-ref --verify --quiet`` exits 0 when the ref exists and 1
        when it does not; ANY other exit (an error, a blocked probe) is NOT proof
        of absence and returns False, so an unreadable ref is treated as PRESENT
        — the safe assumption, because guessing "absent" is the guess that pushes
        ungated commits to origin."""
        rc, _, _ = git(self.repo, "show-ref", "--verify", "--quiet",
                       f"refs/remotes/{self.remote}/main")
        return rc == 1

    def _record_provenance_unreadable(self, local_sha: str,
                                      origin_sha: str | None) -> None:
        """Refuse a landing LOUDLY when local-ahead provenance could NOT be read
        (BLOCKER, local-ahead-unreadable-probe-fails-open). FAIL-CLOSED, and kept
        DISTINCT from `_record_local_ahead`: that recorder KNOWS local is ahead;
        this one could not determine it and refuses rather than guess safe.
        Edge-triggered on the (local, origin) pair like the other anomaly
        recorders, so a persistent unreadable state does not alert every tick."""
        origin_key = origin_sha or ""
        origin_disp = origin_sha[:12] if origin_sha else "unreadable"
        msg = (f"REFUSING to land: could not read {self.remote}/main or its ancestry to "
               f"prove local main {local_sha[:12]} carries no ungated commits (origin "
               f"{origin_disp}). An unreadable provenance probe is NOT evidence that the "
               "base is safe to push — failing CLOSED rather than carry possibly-ungated "
               f"commits to origin. A human must restore read access to {self.remote}/main "
               "(or reconcile main with origin).")
        self._log(msg)
        self._mark_tick_degraded(f"{self.remote}/main provenance unreadable")
        marker = self._alert_marker_path("main-provenance-unreadable")
        prior = self._read_marker(marker)
        if prior.get("local_sha") == local_sha and prior.get("origin_sha") == origin_key:
            return
        self._write_json_atomic(marker, {
            "kind": "main-provenance-unreadable", "started_at": time.time(),
            "local_sha": local_sha, "origin_sha": origin_key, "remote": self.remote,
        })
        self._alert(msg)
        self._append_ledger({
            "ts": _iso(_now()), "role": ROLE, "event": "instrument_error",
            "id": None, "actor": ACTOR,
            "detail": {"class": "instrument-error", "area": "tooling",
                       "kind": "main-provenance-unreadable", "reason": msg,
                       "remote": self.remote, "local_sha": local_sha,
                       "origin_sha": origin_key,
                       "remedy": (f"restore read access to {self.remote}/main (unblock the "
                                  "probe or re-fetch), then re-run; the daemon fails closed "
                                  "rather than push commits it cannot prove are gated")},
        })

    def _sync_main_from_origin(self, *, why: str, hold_base: bool = False) -> str:
        """Bring the serving checkout's `main` up to `<remote>/main` BEFORE
        anything is assembled on it. Returns one of:

          ``no-remote``    — nothing configured to sync from
          ``wrong-head``   — the checkout is not on main; refused, nothing moved
          ``fetch-failed`` — FAIL-OPEN: proceed on the stale view, tick degraded
          ``unreadable``   — refs/ancestry undeterminable; proceed on the local view
          ``current``      — local main already carries every origin commit
          ``local-ahead``  — FAIL-CLOSED (push on): local carries ungated commits
                             origin lacks; refused loudly, nothing pushed
          ``diverged``     — FAIL-CLOSED: refused loudly, nothing moved
          ``reconciled-content-free`` — a divergence whose LOCAL-only commits
                             are provably content-free (empty diff against the
                             merge base) was auto-healed onto <remote>/main via
                             an atomic `update-ref` compare-and-swap plus a
                             guarded `read-tree -m -u` materialization, old tip
                             preserved on a salvage branch; treat like
                             ``synced`` (fresh base)
          ``ff-refused``   — git refused the fast-forward; NOT escalated to force
          ``synced``       — main was fast-forwarded onto <remote>/main
          ``deferred-gate-in-flight`` — CHURN GUARD (``hold_base=True``): origin
                             is ahead by a clean fast-forward but a gate is in
                             flight, so the advance is DEFERRED rather than
                             orphan that gate into a from-scratch re-gate.
                             Nothing moved; the base advances on a later tick
                             with a free slot.

        WHY THIS EXISTS (finding sha256:b1edeafa). The daemon assembles trains on
        LOCAL main and pushes them to origin, but nothing ever moved local main
        FROM origin. A pull request merged through GitHub advances origin only;
        from that moment every train push is non-fast-forward, the daemon rebuilds
        the IDENTICAL train on the IDENTICAL stale base every tick, and the whole
        land pipeline is dead until a human runs `git pull --ff-only`. Measured
        2026-08-11: two routine PRs, three identical refusals in eight minutes,
        and it would have run until morning. One fetch at the top of the tick
        turns that outage back into a non-event.

        FAIL-OPEN ON FETCH, FAIL-CLOSED ON DIVERGENCE. A network blip must not
        halt assembly — the stale view is exactly what the daemon had before this
        method existed, and it is safe (the push guard refuses to record a merge
        that did not reach origin). A divergence is the opposite: it cannot be
        repaired without destroying commits on one side, so the machine stops and
        tells a human.

        WHAT IT DELIBERATELY DOES NOT DO. It never checks out a branch, never
        forces, and never touches the pinned GATE workspace. Its effects are
        `_ff_only_advance` (the same verified fast-forward every landing
        already performs) plus exactly one provably-lossless carve-out,
        `_content_free_reconcile` — see that method's contract and its
        GATE_LOOP_AUTOHEAL_CONTENT_FREE kill switch.

        RUNNING-GATE INTERACTION (the auto-rebase lane's MAJOR-1). That review
        refused a re-anchor that could REPIN THE SHARED GATE WORKSPACE under a
        running local gate. This path cannot: `_repin` is reached only from a
        successful landing, and moving the serving checkout's main does not touch
        `gate_ws`, so a gate in flight keeps grading exactly the tree it was
        pinned to. What a sync CAN do is move main out from under an in-flight
        train, and the consequence is the existing, honest one: at land time
        `main != train.base`, the train is refused with `stale-base` and
        re-assembles onto the new base next tick — the identical path a train
        already takes when another train lands ahead of it in the same tick. The
        cost is one re-gate; the alternative is a receipt bound to a merge base
        that is no longer main, which is a false GREEN.

        CHURN GUARD (``hold_base``). The "one re-gate" above is fine for a SINGLE
        out-of-band advance. Under a BURST — several PRs merging on origin while
        one ~25-min gate runs — the unconditional tick-top advance pays that cost
        EVERY tick: each advance moves the base, the same members re-assemble to a
        new tip, the `<train>@<tip>` gate key changes, and the running gate is
        orphaned and re-dispatched from scratch before it can finish. The gate
        never completes and nothing lands (the livelock). `hold_base=True` (set by
        the caller when `_running_gate_count() > 0`) DEFERS only the benign
        fast-forward while a gate is in flight, so the gate finishes against the
        base it started on and the base advances at the next free slot. It is
        placed AFTER the fail-open fetch and BOTH fail-closed guards (divergence,
        local-ahead), so no safety check is skipped — only the throughput-oriented
        advance is debounced — and it never touches the land / tip-stability path.
        """
        if not self.remote:
            return "no-remote"
        head_ref = self._head_ref()
        if head_ref != "refs/heads/main":
            self._alert(f"serving checkout HEAD is {head_ref or 'detached'!r}, not "
                        "refs/heads/main — refusing to sync any ref from "
                        f"{self.remote} (instrument)")
            return "wrong-head"
        rc, _, err = git(self.repo, "fetch", self.remote, timeout=120)
        if rc != 0:
            self._log(f"origin sync ({why}): fetch from {self.remote} failed "
                      f"({err[:160]}) — assembling on the stale local view of main; "
                      "a fetch outage never halts landing")
            self._mark_tick_degraded(f"{self.remote} fetch failed; stale view of main")
            return "fetch-failed"
        rc_o, origin_sha, _ = git(self.repo, "rev-parse", "--verify",
                                  f"{self.remote}/main")
        rc_l, local_sha, _ = git(self.repo, "rev-parse", "--verify", "main")
        origin_sha, local_sha = origin_sha.strip(), local_sha.strip()
        if rc_o != 0 or rc_l != 0 or not (_full_sha(origin_sha) and _full_sha(local_sha)):
            self._log(f"origin sync ({why}): could not resolve {self.remote}/main or main "
                      "(instrument) — assembling on the local view")
            return "unreadable"
        if origin_sha == local_sha:
            self._divergence_marker().unlink(missing_ok=True)
            self._push_deferred_marker().unlink(missing_ok=True)
            return "current"
        # Ahead/behind by ANCESTRY, and deliberately via `rev-list --not` rather
        # than `merge-base --is-ancestor`: this estate's git guard hook
        # intercepts commands containing merge verbs, and a hook-blocked probe
        # returns an rc that looks exactly like "not an ancestor" (the same
        # argument integration.is_ancestor_of_main is written on). Guessing
        # "diverged" from a blocked probe would park a healthy pipeline.
        rc_a, behind, _ = git(self.repo, "rev-list", "--max-count=1",
                              origin_sha, "--not", local_sha)
        rc_b, unpushed, _ = git(self.repo, "rev-list", "--max-count=1",
                                local_sha, "--not", origin_sha)
        if rc_a != 0 or rc_b != 0:
            self._log(f"origin sync ({why}): ancestry between main and {self.remote}/main "
                      "undeterminable (instrument) — assembling on the local view")
            return "unreadable"
        if not behind.strip():
            # Origin has nothing local lacks. Exact equality already returned
            # "current" above, so reaching here means local is STRICTLY AHEAD of
            # origin. That is only healthy when we are NOT pushing: push=False
            # leaves local ahead by design (unpushed landings). With push ON the
            # daemon pushes every landing atomically and so never legitimately
            # leaves local ahead — an unexplained local-ahead commit would ride
            # to origin under the next train's fast-forward push (BLOCKER,
            # local-ahead-pushes-unreviewed). Classify it DISTINCTLY and fail
            # closed; the land path is what actually refuses to push over it.
            self._divergence_marker().unlink(missing_ok=True)
            if self.push and unpushed.strip():
                self._record_local_ahead(local_sha, origin_sha)
                return "local-ahead"
            self._local_ahead_marker().unlink(missing_ok=True)
            return "current"
        if unpushed.strip():
            # CONTENT-FREE SHORT-CIRCUIT (2026-08-14 stall class). Before the
            # fail-closed refusal, prove-or-refuse the one shape a machine CAN
            # repair losslessly: every local-only commit changes NOTHING vs the
            # merge base (the recurring cause: a lane re-merged locally after
            # its content already landed on origin via a GitHub PR). Any
            # precondition miss falls through to the unchanged fail-closed path.
            if self._content_free_reconcile(local_sha, origin_sha, why=why):
                self._push_deferred_marker().unlink(missing_ok=True)
                return "reconciled-content-free"
            self._record_divergence(local_sha, origin_sha)
            return "diverged"
        if hold_base:
            # CHURN GUARD (out-of-band-origin-advance re-gate storm). Origin is
            # strictly ahead by a CLEAN fast-forward (equality, local-ahead and
            # divergence are all ruled out above), but a gate is IN FLIGHT and its
            # train was assembled on the CURRENT base. Advancing main now moves the
            # base out from under that gate: next tick the same members re-assemble
            # onto the new base, mint a DIFFERENT tip, and the `<train>@<tip>` gate
            # key changes — so the running gate is orphaned, reaped, and
            # re-dispatched from scratch. Under a burst of merged PRs that repeats
            # every tick and the ~25-min gate never finishes. Hold the base: the
            # gate completes against the base it started on, and the advance lands
            # on a later tick with no gate running — or, once a finished train
            # cannot fast-forward onto the moved origin, via the push-refusal
            # re-anchor, which carries this SAME hold_base and therefore defers too
            # while a sibling is still gating (Grok MAJOR-1). Only the benign ff is
            # deferred — the fail-open fetch and both fail-closed guards already ran
            # this tick, and nothing in the land / tip-stability path is touched.
            self._log(f"origin sync ({why}): main {local_sha[:12]} is behind "
                      f"{self.remote}/main {origin_sha[:12]} by a clean fast-forward, but "
                      "a gate is in flight — DEFERRING the advance so the running gate is "
                      "not orphaned into a from-scratch re-gate; the base advances once the "
                      "gate slot frees")
            return "deferred-gate-in-flight"
        rc, ff_err = self._ff_only_advance(origin_sha)
        if rc != 0:
            # HARDENING (untracked-ff-collision deadlock, 2026-08-13). An otherwise
            # clean fast-forward (equality, local-ahead and divergence are all ruled
            # out above) is still refused when an UNTRACKED working-tree file would
            # be overwritten by a file the incoming commit ADDS. Left alone this
            # pins the whole land pipeline at ZERO: the ff refuses every tick until
            # a human clears the file (measured — a stray identical HANDOFF doc
            # stalled the daemon at 0/3 util for 26 sweeps). For the colliding
            # untracked files whose RAW on-disk bytes are identical to what the ff
            # would install (they carry no unique work), SALVAGE them into a stash
            # — the same never-destroy doctrine as `_salvage_before_rollback`, not
            # a delete — so the ff can install the identical tracked copy, then
            # retry once. EVERY step of that is fail-closed: the identity is proven
            # on RAW bytes (a lossy clean filter must never make distinct bytes
            # read as identical), every parsed filename reaches git only under
            # `--literal-pathspecs` (a name that is itself pathspec magic must
            # never widen the salvage), and the proof is re-run under writer
            # exclusion immediately before the stash and verified after it. A file
            # with distinct content — or any input that cannot be positively
            # proven redundant — is never touched, so the sync still refuses below
            # and the operator's work is preserved either way.
            proven = self._redundant_untracked_ff_blockers(origin_sha, ff_err)
            if proven and self._salvage_untracked_ff_blockers(origin_sha, proven, why):
                rc, ff_err = self._ff_only_advance(origin_sha)
        if rc != 0:
            self._alert(f"origin sync ({why}): main {local_sha[:12]} is an ancestor of "
                        f"{self.remote}/main {origin_sha[:12]} but the fast-forward was "
                        f"REFUSED ({ff_err[:200]}) — not escalating to a reset or a force; "
                        "assembling on the local view")
            self._mark_tick_degraded("origin fast-forward refused")
            return "ff-refused"
        self._divergence_marker().unlink(missing_ok=True)
        # The deferred push (if any) can now proceed: the base advanced, so the
        # edge marker is cleared and the NEXT deferral episode re-arms its event.
        self._push_deferred_marker().unlink(missing_ok=True)
        self._log(f"origin sync ({why}): fast-forwarded main {local_sha[:12]} -> "
                  f"{origin_sha[:12]} from {self.remote} (out-of-band commits, e.g. a "
                  "merged PR); this tick assembles on the fresh base")
        return "synced"

    # -- rollback salvage (never destroy the operator's uncommitted work) ----

    def _stash_head(self) -> str | None:
        """The current top of the stash stack, or None when there is none."""
        rc, out, _ = git(self.repo, "rev-parse", "--verify", "--quiet", "refs/stash")
        return _full_sha(out) if rc == 0 else None

    def _tracked_dirty(self) -> bool | None:
        """Does the serving checkout hold uncommitted TRACKED changes?

        That — staged or unstaged modifications to tracked files — is exactly
        what `reset --hard` destroys. Untracked files are outside its blast
        radius, so they are neither counted here nor swept into the salvage
        stash: sweeping them would be a second surprise the rollback never
        needed to cause. None means the probe was unreadable, and None is not
        False: an unreadable status is treated as possibly-dirty, because
        guessing "clean" is the guess that destroys work.
        """
        rc, out, _ = git(self.repo, "status", "--porcelain", "--untracked-files=no")
        if rc != 0:
            return None
        return bool(out.strip())

    def _salvage_before_rollback(self, train: Train) -> tuple[str, str | None]:
        """Put any uncommitted tracked work somewhere durable before the rollback
        touches it. Returns ``("clean"|"stashed"|"failed", stash_sha|None)``.

        This is the daemon-side instance of the estate's salvage-commit doctrine:
        a dirty worktree is salvaged before anything destructive happens to it.
        The stash is deliberately NOT popped afterwards — re-applying onto the
        rolled-back tree can conflict, and a conflicted serving checkout would
        wedge the next tick's ff-merge. The work is safe, the operator is told
        where it is, and restoring it is their (reversible) call.

        RUNNING-GATE INTERACTION (the auto-rebase lane's MAJOR-1): none. Gates
        run from the pinned GATE workspace, a separate checkout; a stash on the
        serving checkout writes objects, `refs/stash`, and the serving
        checkout's own index — no file any in-flight gate reads. The only lock
        it can contend for is the serving checkout's `index.lock`, and losing
        that race is an rc failure that refuses the rollback rather than
        corrupting anything.
        """
        # ATOMICITY (BLOCKER, rollback-dirty-toctou). We deliberately do NOT gate
        # the stash on a prior cleanliness verdict. ANY status probe is a
        # time-of-check/time-of-use window: a tracked edit that lands AFTER the
        # probe but BEFORE `reset --hard` reads as "clean" and is destroyed with
        # no durable copy — the exact 00:35-00:48 data-loss shape. So we probe
        # only for the log, then ALWAYS run `git stash push`, which captures
        # whatever tracked dirt is present at the instant it runs (atomic w.r.t.
        # the worktree) and is a harmless no-op ("No local changes to save") on a
        # genuinely clean tree.
        dirty = self._tracked_dirty()
        label = f"gate-rollback-salvage {_iso(_now())} {train.branch}"
        rc, out, err = git(self.repo, "stash", "push", "--no-include-untracked",
                           "-m", label, timeout=120)
        if rc != 0:
            self._log(f"salvage stash of {self.repo} FAILED ({(err or out)[:200]}) — the "
                      "rollback will be refused rather than destroy the working tree "
                      f"(pre-stash tracked probe: {'dirty' if dirty else 'clean/unreadable'})")
            return "failed", None
        # PROVE the entry is OURS by its unique label, located ANYWHERE in the
        # stack (MAJOR, shared-stash-head-race). `refs/stash` is SHARED across
        # every worktree of this repo, so a peer's concurrent `git stash push`
        # can move `stash@{0}`/`refs/stash` off our entry the instant after ours
        # lands. Reading the top of the stack would then name the PEER's stash
        # (or miss ours) and skip `_record_salvage`, losing the recovery pointer.
        # `git stash push` also exits 0 with "No local changes to save" when the
        # tree turned out clean, in which case no entry carries our label at all.
        sha = self._find_own_stash(label)
        if sha is None:
            # No entry carries our label: the tree was clean at stash time, so
            # `git stash push` created nothing. Nothing to record, nothing lost.
            return "clean", None
        return "stashed", sha

    def _find_own_stash(self, label: str) -> str | None:
        """The sha of the stash entry created for THIS salvage, located by its
        unique label ANYWHERE in the stack — never by `stash@{0}`, which
        `refs/stash` sharing lets a peer worktree move under us the instant after
        our push (MAJOR, shared-stash-head-race). Returns None when no live entry
        carries the label (the tree was clean, so no stash was created).

        EXACT-label match (MAJOR, shared-stash-head-race v2). `git stash push -m
        LABEL` records the entry subject as ``On <branch>: LABEL``. A SUBSTRING
        test (`label in subject`) also matches a peer whose label merely EXTENDS
        ours — ``LABEL-peer`` contains ``LABEL`` — and refs/stash sharing puts
        that peer entry ABOVE ours in the shared stack, so a substring scan reads
        the PEER's stash as our salvage and records the wrong sha. We therefore
        match the WHOLE label: the subject must equal the label, or end with
        ``": " + label`` (the ``On <branch>: `` form). A git ref name cannot
        contain ``": "`` and the label we mint carries none, so that boundary is
        unambiguous and an EXTENDED peer label (…-peer, or one that prepends
        text) can never satisfy it."""
        rc, out, _ = git(self.repo, "stash", "list", "--format=%H%x1f%gs")
        if rc != 0:
            return None
        suffix = f": {label}"
        for line in out.splitlines():
            sha, sep, subject = line.partition("\x1f")
            if sep and (subject == label or subject.endswith(suffix)):
                full = _full_sha(sha)
                if full:
                    return full
        return None

    def _record_salvage(self, train: Train, stash_sha: str, pre_merge: str) -> None:
        """Tell the operator where their work went — in the alert AND the ledger.

        The ledger copy matters because the alert file rotates and a storm of
        push failures can bury it, and because the recovery command has to be
        reconstructible hours later. Deliberately carries NO ``push_key``: the
        push-retry budget counts push refusals, and a salvage is not one.
        """
        msg = (f"SALVAGED the serving checkout's uncommitted tracked changes into stash "
               f"{stash_sha[:12]} before rolling main back to {pre_merge[:12]} after the "
               f"failed push of {train.branch}. Recover with: git -C {self.repo} stash "
               f"apply {stash_sha}  (untracked files were left in place, never stashed).")
        self._alert(msg)
        self._log(msg)
        self._append_ledger({
            "ts": _iso(_now()), "role": ROLE, "event": "instrument_error",
            "id": None, "actor": ACTOR,
            "detail": {"class": "instrument-error", "area": "tooling",
                       "kind": "rollback_salvage", "reason": msg,
                       "train": train.branch, "candidate_sha": train.tip,
                       "stash_sha": stash_sha, "repo": str(self.repo),
                       "rolled_back_to": pre_merge,
                       "remedy": f"git -C {self.repo} stash apply {stash_sha}"},
        })

    def _pin_concurrent_commits(self, train: Train, merged_tip: str) -> str | None:
        """If `main` moved PAST our just-merged tip — a commit landed on the
        SHARED serving checkout between our ff-merge and the push-failure rollback
        — pin the live tip into a durable ``refs/gate-loop/rollback-salvage/…``
        ref BEFORE ``reset --hard`` orphans it (BLOCKER, rollback-dirty-toctou /
        concurrent-commit half). Returns the ref name, or None when `main` is
        exactly `merged_tip` (nothing extra is at risk — the train tip is already
        retained by the train branch). Best-effort and non-fatal: a failure here
        alerts and points at the reflog rather than aborting the rollback."""
        rc, live, _ = git(self.repo, "rev-parse", "--verify", "main")
        live = (live or "").strip()
        if not _full_sha(live) or live == merged_tip:
            return None
        ts = _iso(_now()).replace(":", "")            # ':' is illegal in ref names
        ref = f"refs/gate-loop/rollback-salvage/{train.branch.replace('/', '_')}-{ts}"
        rc, _, err = git(self.repo, "update-ref", ref, live)
        if rc != 0:
            self._alert(
                f"a commit landed on {self.repo} main ({live[:12]}) between the gate "
                f"merge of {train.branch} and the push-failure rollback, and it could NOT "
                f"be pinned into a durable ref ({(err or '')[:160]}) before reset --hard "
                f"— if it is lost, recover it from the reflog (git -C {self.repo} reflog "
                "show main).")
            return None
        msg = (f"a commit ({live[:12]}) landed on the serving checkout's main in the "
               f"window between the gate merge of {train.branch} and the push-failure "
               f"rollback; it is retained by no branch, so it was PINNED into {ref} before "
               f"reset --hard. Recover with: git -C {self.repo} branch recovered-{live[:12]} "
               f"{ref}")
        self._alert(msg)
        self._log(msg)
        self._append_ledger({
            "ts": _iso(_now()), "role": ROLE, "event": "instrument_error",
            "id": None, "actor": ACTOR,
            "detail": {"class": "instrument-error", "area": "tooling",
                       "kind": "rollback_concurrent_commit_pinned", "reason": msg,
                       "train": train.branch, "pinned_sha": live, "ref": ref,
                       "repo": str(self.repo),
                       "remedy": f"git -C {self.repo} branch recovered-{live[:12]} {ref}"},
        })
        return ref

    def _pin_reset_instant_orphan(self, train: Train, pre_merge: str,
                                  merged_tip: str) -> str | None:
        """Pin the commit main pointed at the INSTANT before the rollback reset,
        when no durable ref already retains it (BLOCKER, rollback-dirty-toctou v2,
        commit half). `git reset` records the tip it moved away from in main's
        reflog ATOMICALLY, so ``main@{1}`` is exactly that tip — the ONLY place a
        commit that landed between the pre-reset `_pin_concurrent_commits` read
        and the reset itself can be recovered from. No-op when that tip is already
        back at pre_merge, is the gated train tip (retained by the train branch),
        is reachable from the rolled-back main, or was already pinned pre-reset (so
        we never create a duplicate salvage ref). Best-effort and non-fatal: a
        failure alerts and points at the reflog rather than aborting anything."""
        rc, prior, _ = git(self.repo, "rev-parse", "--verify", "--quiet", "main@{1}")
        prior = (prior or "").strip()
        if not _full_sha(prior) or prior == pre_merge or prior == merged_tip:
            return None
        rc_r, out, _ = git(self.repo, "rev-list", "--max-count=1", prior,
                           "--not", pre_merge)
        if rc_r != 0 or not out.strip():
            # Reachable from the rolled-back main (or ancestry unreadable): either
            # it is not orphaned, or we cannot prove it is — do not fabricate a ref.
            return None
        rc_c, contained, _ = git(self.repo, "for-each-ref", "--contains", prior,
                                 "--format=%(refname)",
                                 "refs/gate-loop/rollback-salvage/")
        if rc_c == 0 and contained.strip():
            return None                               # already pinned pre-reset
        ts = _iso(_now()).replace(":", "")            # ':' is illegal in ref names
        ref = f"refs/gate-loop/rollback-salvage/{train.branch.replace('/', '_')}-{ts}"
        rc, _, err = git(self.repo, "update-ref", ref, prior)
        if rc != 0:
            self._alert(
                f"a commit ({prior[:12]}) was on the serving checkout's main at the instant "
                f"the push-failure rollback of {train.branch} reset it, and it could NOT be "
                f"pinned into a durable ref ({(err or '')[:160]}) — if it is lost, recover it "
                f"from the reflog (git -C {self.repo} reflog show main).")
            return None
        msg = (f"a commit ({prior[:12]}) was on the serving checkout's main at the instant "
               f"the push-failure rollback of {train.branch} moved it back to "
               f"{pre_merge[:12]}; it is retained by no branch, so it was PINNED into {ref}. "
               f"Recover with: git -C {self.repo} branch recovered-{prior[:12]} {ref}")
        self._alert(msg)
        self._log(msg)
        self._append_ledger({
            "ts": _iso(_now()), "role": ROLE, "event": "instrument_error",
            "id": None, "actor": ACTOR,
            "detail": {"class": "instrument-error", "area": "tooling",
                       "kind": "rollback_concurrent_commit_pinned", "reason": msg,
                       "train": train.branch, "pinned_sha": prior, "ref": ref,
                       "repo": str(self.repo),
                       "remedy": f"git -C {self.repo} branch recovered-{prior[:12]} {ref}"},
        })
        return ref

    def _sweep_reset_instant_dirt(self, train: Train, pre_merge: str) -> str | None:
        """After a `reset --keep` rollback, capture any uncommitted tracked edit
        that `--keep` PRESERVED because it appeared in the reset-instant window
        (BLOCKER, rollback-dirty-toctou v2, edit half). `--keep` never discards
        such an edit, so it is already safe in the worktree; stashing it here
        makes it DURABLE (labelled, recorded, recoverable) and leaves a clean
        serving tree for the next tick — exactly what the pre-reset salvage does
        for pre-existing dirt. A clean tree is a harmless no-op. On stash failure
        the edit simply stays in the worktree (still not lost), so this never
        poisons. Returns the stash sha, or None when nothing was captured."""
        if self._tracked_dirty() is False:
            return None                               # provably clean: nothing kept
        label = f"gate-rollback-salvage {_iso(_now())} {train.branch}"
        rc, out, err = git(self.repo, "stash", "push", "--no-include-untracked",
                           "-m", label, timeout=120)
        if rc != 0:
            self._log(
                f"post-reset sweep stash of {self.repo} failed ({(err or out)[:160]}) — a "
                "tracked edit preserved by `reset --keep` was left in the worktree (NOT "
                "lost); the operator must commit or clear it before the next landing")
            return None
        return self._find_own_stash(label)

    # -- landing (the serialised single-writer step) -------------------------

    def land_train(self, train: Train) -> tuple[str | None, str]:
        """ff-only merge the train onto `main`, push, re-pin. Returns
        (new_main_sha, "landed") on success, or (None, <reason-tag>) when it did
        not land — the tag distinguishes a routine schedule miss ("stale-base",
        "not-ff", "wrong-head") from a genuine push instrument fault
        ("push-failed") and from a REMOTE-AHEAD push refusal ("push-non-ff",
        which is neither: it is fixed by changing our base, never by retrying),
        so the caller can record the right event and take the right action. On
        the push-then-rollback DOUBLE failure it does not return at all: it
        writes a poison marker and raises DaemonPoisoned (see below).

        Called ONLY inside the held lock. It re-checks that `main` is exactly the
        ROOT the train's chain was assembled on: if `main` moved since assembly (a
        train landed earlier THIS tick), this train is stale — it is NOT
        force-landed; it re-assembles onto the new `main` next tick. That
        refusal-to-force is what keeps the merge honest under the single-writer
        serialisation.

        For a CHAINED train the root is the chain's root, not the train's own
        base: the train's base is its predecessor's tip, which is by construction
        NOT on `main` yet. Fast-forwarding `main` to this train's tip therefore
        lands its whole ancestor prefix with it — which is exactly what the
        caller asks for, and only ever after every one of those ancestors PASSED
        its own gate (`_land_prefix` is the sole caller that passes a chained
        train, and it passes only the deepest member of a fully-passed prefix)."""
        # The ff-merge advances CURRENT HEAD; if the serving checkout is not on
        # `main` (detached, or another branch), refuse rather than advance the
        # wrong ref. This is an INSTRUMENT condition, never a candidate fault.
        head_ref = self._head_ref()
        if head_ref != "refs/heads/main":
            self._alert(f"serving checkout HEAD is {head_ref or 'detached'!r}, not "
                        "refs/heads/main — refusing to ff-merge onto the wrong ref (instrument)")
            return None, "wrong-head"
        rc, cur_main, _ = git(self.repo, "rev-parse", "--verify", "main")
        if rc != 0:
            self._alert("could not resolve main to land onto (instrument)")
            return None, "no-main"
        cur_main = cur_main.strip()
        if cur_main != train.root:
            self._log(f"NOT landing {train.branch}: main moved {train.root[:12]} -> "
                      f"{cur_main[:12]} since assembly; re-assembles next tick (never forced)")
            return None, "stale-base"
        # UNREVIEWED LOCAL-AHEAD GUARD (BLOCKER, local-ahead-pushes-unreviewed).
        # The gate reviewed the train TIP on top of `train.base`; it never
        # reviewed `train.base` itself — the daemon trusts that base to already be
        # on origin, because it pushes every landing atomically and so never
        # legitimately leaves local main ahead of origin. If `main` (== base)
        # carries commit(s) that `origin/main` does NOT, those are ungated (a
        # manual commit on the serving checkout, or a stranded landing), and a
        # fast-forward push of this train would ride them onto origin/main for
        # free. Refuse to land: nothing without gate provenance reaches origin.
        # We do this BEFORE the ff-merge, so main never even advances and there is
        # nothing to roll back. Provenance is decided by `_local_ahead_provenance`,
        # which distinguishes four states — and, crucially, keeps "genuinely no
        # unexplained commits" separate from "could not determine". A genuinely
        # ABSENT origin/main (brand-new repo, mocked-git tests) fails OPEN because
        # there is no origin state to protect; an UNREADABLE origin/main or
        # ancestry fails CLOSED (BLOCKER, local-ahead-unreadable-probe-fails-open)
        # — an unreadable probe must never be read as proof the base is safe.
        if self.push and self.remote:
            prov, origin_main = self._local_ahead_provenance(cur_main)
            if prov == "ahead":
                self._record_local_ahead(cur_main, origin_main)
                self._log(
                    f"NOT landing {train.branch}: local main {cur_main[:12]} is AHEAD "
                    f"of {self.remote}/main {(origin_main or '')[:12]} by ungated commit(s); "
                    "refusing to push them to origin under a gated train "
                    "(re-attempts once a human reconciles main with origin)")
                return None, "local-ahead-unreviewed"
            if prov == "unknown":
                self._record_provenance_unreadable(cur_main, origin_main)
                self._log(
                    f"NOT landing {train.branch}: could not read {self.remote}/main or its "
                    f"ancestry to prove local main {cur_main[:12]} carries no ungated "
                    "commits; failing closed rather than push work of unknown provenance "
                    "(re-attempts once read access to origin is restored)")
                return None, "provenance-unknown"
        pre_merge = cur_main                          # roll-back target on push failure
        # ff-only merge on the serving checkout (HEAD is `main`, the pinned
        # single writer). If it is not a clean fast-forward, do NOT force it.
        rc, ff_err = self._ff_only_advance(train.tip)
        if rc != 0:
            self._log(f"NOT landing {train.branch}: not a clean fast-forward "
                      f"({ff_err.splitlines()[:1]}); left for re-assembly")
            return None, "not-ff"
        rc, new_main, _ = git(self.repo, "rev-parse", "--verify", "main")
        new_main = new_main.strip()
        # PUSH-DIVERGENCE GUARD (cross-lineage review, Gemini + Grok). The local
        # fast-forward and the push must be ATOMIC from main's point of view: if
        # the push fails, a local main that advanced while origin did not is a
        # split-brain that fails every later push and diverges the served
        # checkout. So on a push failure we ROLL BACK the local ff to the
        # pre-merge sha and treat the tick as an instrument error — the train is
        # untouched and re-lands next tick once the push path clears. We NEVER
        # record `merged` for a merge that did not reach origin.
        if self.push and self.remote:
            # Push the GATED SHA by explicit refspec, never the mutable ref name.
            # `push <remote> main` resolves refs/heads/main AT PUSH TIME, so an
            # operator commit landing on the shared serving checkout in the
            # window between the ff-merge above and this push would ride to
            # origin ungated — the exact invariant the local-ahead guard exists
            # to hold. Pushing `train.tip` publishes what the gate graded and
            # only that; a late concurrent commit merely leaves local main
            # ahead, which the tick-top classifier refuses loudly next tick.
            prc, pout, perr = git(self.repo, "push", self.remote,
                                  f"{train.tip}:refs/heads/main", timeout=180)
            if prc != 0:
                # SALVAGE BEFORE ROLLBACK (DATA_LOSS 2026-08-11T00:48:44Z,
                # severity critical). The rollback below is `reset --hard` on the
                # SERVING checkout — a checkout a human also edits. It is not
                # guaranteed clean, and during the 00:35-00:48 push-failure storm
                # it ran every ~60s and destroyed four uncommitted tracked
                # operator edits (ci.yml, .gitignore, .mcp.json,
                # COMBINED-QUEUE.md). The guard's INTENT is right and stays; its
                # INSTRUMENT was a data-loss machine and does not.
                salvage, stash_sha = self._salvage_before_rollback(train)
                if salvage == "failed":
                    # A STUCK TRAIN IS RECOVERABLE; DESTROYED WORK IS NOT. The
                    # reset does not happen. Local main is left ahead of origin,
                    # which is exactly the split-brain the guard exists to
                    # prevent — so take the same halt the double-failure path
                    # takes, for the same reason: a resumed daemon would
                    # otherwise push an un-recorded merge to origin on a later
                    # tick. Note the tick-top origin sync CANNOT catch this
                    # state: local-ahead-of-origin is not a divergence, so the
                    # poison marker is the only thing standing between this and
                    # a silent split brain.
                    self._write_poison(
                        train, pre_merge, new_main, perr,
                        "rollback NOT attempted: the working tree could not be salvaged",
                        reason=("push failed and the serving checkout held uncommitted "
                                "tracked changes that could not be salvaged — the rollback "
                                "was REFUSED rather than destroy them"))
                    self._alert(
                        f"CRITICAL: push of {train.branch} to {self.remote} failed and the "
                        f"serving checkout {self.repo} holds UNCOMMITTED tracked changes that "
                        "could not be stashed — REFUSING to roll main back with reset --hard "
                        "rather than destroy them (data loss 2026-08-11). Local main is left "
                        f"at {new_main[:12]} (ahead of origin). Daemon POISONED; commit or "
                        "stash the working tree by hand, reconcile main with origin, then "
                        "clear the marker.")
                    raise DaemonPoisoned(
                        f"refused to reset --hard over unsalvageable uncommitted work in "
                        f"{self.repo}; local main left at {new_main} — halting all landing")
                # DURABLE-REF CAPTURE BEFORE THE DESTRUCTIVE RESET (BLOCKER,
                # rollback-dirty-toctou / concurrent-commit half). The stash above
                # saves uncommitted work, but `git stash` cannot save a COMMIT. Our
                # ff-merge advanced main to `train.tip` (retained by the train
                # branch). The serving checkout is a SHARED tree, so an operator
                # commit can land ON TOP of main in the window between our merge
                # and this rollback; it is retained by NO branch, and
                # `reset --hard pre_merge` would erase it from every durable ref.
                # Pin the live tip into a namespaced ref first so it stays
                # reachable — this must happen BEFORE the reset, never after.
                # Anchor the comparison on `train.tip`, NOT `new_main`: new_main
                # is re-read from the live ref after the merge, so a commit
                # racing into the merge-to-rev-parse window is absorbed INTO
                # new_main, compares equal, and would never be pinned. train.tip
                # is the sha the merge deterministically set main to, so anything
                # past it is at risk by definition.
                self._pin_concurrent_commits(train, train.tip)
                # STRUCTURAL ROLLBACK THAT CANNOT DISCARD UNCOMMITTED WORK
                # (BLOCKER, rollback-dirty-toctou v2). `reset --hard` is a
                # check-then-act on a SHARED worktree: the salvage/stash and the
                # pin above are SEPARATE subprocesses, so a tracked edit or a
                # commit that appears in the window right up to the reset is
                # outside their capture and `--hard` destroys it with no durable
                # copy. Two mechanism-level changes close that class for good:
                #   1. `reset --keep` NEVER discards an uncommitted tracked edit.
                #      It keeps local changes to files that do not differ between
                #      pre_merge and HEAD, and ABORTS (touching nothing) if a file
                #      that DOES differ has local changes. So an edit that lands at
                #      the reset instant is preserved in the worktree, or forces an
                #      abort we handle as a rollback failure below — never silently
                #      lost.
                #   2. `reset` records the exact tip it moved away from in main's
                #      reflog, atomically. That is the ONE place a commit that
                #      landed between the pre-reset pin and the reset survives, so
                #      after the reset we pin it durably (`_pin_reset_instant_orphan`)
                #      and sweep any edit `--keep` preserved into a labelled stash
                #      (`_sweep_reset_instant_dirt`).
                # Together: nothing that exists at the instant of reset is
                # unrecoverable.
                rrc, _, rerr = git(self.repo, "reset", "--keep", pre_merge)
                if rrc != 0:
                    # DOUBLE FAILURE (Grok MAJOR-2): the ff-merge advanced local
                    # main, the push did NOT reach origin, and the rollback ALSO
                    # failed (e.g. a held index.lock). Local main is now DIVERGED
                    # from origin, and the single writer must not advance it one
                    # step further on its own. Poison the daemon: write a durable
                    # halt marker with the divergence details and raise — every
                    # future tick refuses at the top of run_once until a human
                    # reconciles origin and clears the marker. Alerting alone is
                    # not enough; the process kept landing on the diverged main.
                    self._write_poison(train, pre_merge, new_main, perr, rerr,
                                       stash_sha=stash_sha)
                    self._alert(
                        f"CRITICAL: push of {train.branch} to {self.remote} failed AND the "
                        f"rollback of local main to {pre_merge[:12]} ALSO failed ({rerr[:160]}) "
                        f"— local main is DIVERGED at {new_main[:12]}, origin is behind. "
                        "Daemon POISONED (halt marker written); NO further landing until a "
                        "human reconciles origin and clears the marker."
                        + (f" The serving checkout's uncommitted tracked changes were "
                           f"salvaged first and are in stash {stash_sha[:12]} "
                           f"(git stash apply {stash_sha})." if stash_sha else ""))
                    raise DaemonPoisoned(
                        f"local main diverged from {self.remote} at {new_main} (origin behind "
                        f"{pre_merge}); rollback failed — halting all landing")
                # POST-RESET CAPTURE (BLOCKER, rollback-dirty-toctou v2, the
                # reset-instant window). The reset SUCCEEDED; recover anything that
                # existed at the instant it ran but the pre-reset salvage/pin could
                # not have seen: a commit that advanced main after the pin read
                # (recovered from main's reflog and pinned durably), and a tracked
                # edit `--keep` preserved in the worktree (swept into a labelled,
                # recorded stash, leaving a clean serving tree for the next tick).
                # Anchor on `train.tip` (the gated sha the merge set main to), for
                # the same reason the pin above does: `new_main` is re-read from
                # the shared live ref and may already carry a raced commit.
                self._pin_reset_instant_orphan(train, pre_merge, train.tip)
                late_sha = self._sweep_reset_instant_dirt(train, pre_merge)
                if stash_sha:
                    self._record_salvage(train, stash_sha, pre_merge)
                if late_sha:
                    self._record_salvage(train, late_sha, pre_merge)
                # CLASSIFY THE REFUSAL. "The remote is ahead" and "the instrument
                # failed" are different facts with different remedies, and the
                # rolled-back state is identical either way — only the tag the
                # caller acts on differs.
                if push_refusal_is_non_ff(f"{perr}\n{pout}"):
                    self._log(
                        f"push of {train.branch} to {self.remote} was refused as "
                        f"NON-FAST-FORWARD (the remote is ahead) — rolled local main back "
                        f"to {pre_merge[:12]}; re-anchoring instead of retrying")
                    return None, "push-non-ff"
                self._alert(
                    f"push of {train.branch} to {self.remote} failed (instrument): "
                    f"{perr[:200]} — rolled local main back to {pre_merge[:12]}; NOT "
                    "recorded as merged; re-lands next tick once the push path clears")
                return None, "push-failed"
        # Record and repin the GATED tip, not the live ref. `new_main` was
        # re-read from a SHARED checkout after the merge: a concurrent operator
        # commit may already sit on top of it, and it carries no gate
        # provenance. What landed — what the gate graded and (when pushing) what
        # the refspec push published — is exactly `train.tip`; the receipt and
        # the gate workspace must bind that sha. A raced local-only commit is
        # the tick-top classifier's job to refuse loudly next tick.
        self._repin(train.tip)
        return train.tip, "landed"

    # -- poison / halt -------------------------------------------------------

    def _poison_path(self) -> Path:
        return self.root / "state" / "gate-loop-poisoned.json"

    def poisoned(self) -> bool:
        return self._poison_path().exists()

    def _write_poison(self, train: Train, pre_merge: str, diverged: str,
                      push_err: str, reset_err: str, *, reason: str | None = None,
                      stash_sha: str | None = None) -> None:
        body = {
            "poisoned": True, "at": _iso(_now()), "by": ACTOR,
            "reason": reason or ("push failed AND rollback failed — local main may be "
                                 "diverged from origin"),
            "train": train.branch, "remote": self.remote,
            "origin_expected_sha": pre_merge, "local_diverged_sha": diverged,
            "push_error": push_err[:500], "rollback_error": reset_err[:500],
            "remedy": ("reconcile origin with local main (push once origin is reachable, or "
                       "reset local main to origin), then delete this file to resume the daemon")}
        if stash_sha:
            # The operator's uncommitted work is not lost, and the marker is the
            # durable place that says so — an alert file rotates, this does not.
            body["salvage_stash_sha"] = stash_sha
            body["salvage_remedy"] = f"git -C {self.repo} stash apply {stash_sha}"
        self._write_json_atomic(self._poison_path(), body)

    def _repin(self, new_main: str) -> None:
        """Fast-forward the pinned gate workspace to the freshly landed `main` so
        the next gate grades against current `main` (a moved merge base
        invalidates every in-flight receipt). Best-effort and INSTRUMENT-only: a
        failure here never touches a candidate's record."""
        if not self.gate_ws:
            return
        gw = self.gate_ws
        if not (gw / ".git").exists() and not gw.is_dir():
            return
        rc, _, _ = git(gw, "rev-parse", "--verify", "HEAD")
        if rc != 0:
            return
        # Bring the ref home, then hard-pin. The gate workspace is a
        # single-purpose pinned checkout, so reset --hard is correct here.
        git(gw, "fetch", self.remote or "origin", "main", timeout=120)
        rc, _, err = git(gw, "reset", "--hard", new_main)
        if rc != 0:
            rc2, _, err2 = git(gw, "reset", "--hard", "main")
            if rc2 != 0:
                self._alert(f"re-pin of gate workspace to {new_main[:12]} failed "
                            f"(instrument): {err or err2}")
                return
        self._log(f"re-pinned gate workspace {gw} to {new_main[:12]}")

    def _retire_train_branch(self, train: Train) -> None:
        """Delete a temporary train ref only after its tip is on ``main``.

        ``-d`` is deliberate: Git independently proves the branch is merged.
        Never escalate to ``-D``. A cleanup failure is instrument noise after a
        successful landing, not a reason to falsify or roll back the merge.
        """
        rc, _, _ = git(self.repo, "show-ref", "--verify", "--quiet",
                       f"refs/heads/{train.branch}")
        if rc != 0:
            self._log(f"train branch {train.branch} already retired")
            return
        rc, out, err = git(self.repo, "branch", "-d", train.branch)
        if rc != 0:
            self._alert(f"landed {train.branch} but could not retire its temporary "
                        f"branch with git branch -d (instrument): {(err or out)[:200]}")
            return
        self._log(f"retired merged temporary train branch {train.branch} with git branch -d")

    # -- outcome handlers ----------------------------------------------------

    def _push_failure_count(self, push_key: str) -> int:
        """How many times THIS EXACT train tip has already failed to push for an
        instrument reason.

        Keyed on ``detail.push_key`` and deliberately NOT on the ``train_key``
        the re-gate bound uses: the two budgets are independent, and sharing a
        key would let a push outage silently spend the gate's one re-gate (or a
        gate instrument error spend the push budget). Read from the ledger rather
        than held in memory because every tick is a fresh process.

        ONLY ``kind == "push_failed"`` counts. The budget exists for INSTRUMENT
        push failures (network/auth) that a bounded retry then park addresses. The
        other push-keyed rows are NOT failures of this input and must never spend
        it: ``push_non_ff`` is a successful re-anchor onto a new base, and
        ``push_deferred`` is a green train deliberately held for a sibling gate —
        counting either could park a train that never had anything wrong with it.
        """
        n = 0
        path = self.root / "ledger.jsonl"
        if not path.exists():
            return 0
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(ev, dict) or ev.get("event") != "instrument_error":
                continue
            det = ev.get("detail")
            if (isinstance(det, dict) and det.get("push_key") == push_key
                    and det.get("kind") == "push_failed"):
                n += 1
        return n

    def _on_push_refused(self, train: Train, reason: str) -> Outcome:
        """The gate PASSED, the ff-merge was clean, and origin refused the push.

        Two refusals, two remedies (finding sha256:b1edeafa):

        REMOTE AHEAD (``push-non-ff``) — a PR landed on origin. The push is not
        broken and there is nothing to wait for: the INPUT is wrong. Re-anchor
        now (fetch + fast-forward) so the next tick assembles this train on the
        new base and pushes something the remote can accept. This consumes no
        retry budget because it is not a retry — the next attempt is a different
        push. If the re-anchor changes NOTHING, though (origin already current,
        unreachable, or diverged), then the next push WOULD be identical, so it
        falls through to the bounded budget rather than looping forever. The
        re-anchor carries ``hold_base`` (Grok MAJOR-1): if a SIBLING gate is in
        flight it is DEFERRED, not advanced, and — crucially — a deferral is
        benign, not an "unchanged input", so it spends NO budget and never parks
        this passed train (it re-attempts the push once the base advances).

        INSTRUMENT (``push-failed``) — network or auth. The identical push is
        legitimate once the path clears, so it retries; but it retries a bounded
        number of times and then parks the train state LOUDLY. A daemon that
        retries all night while the queue backs up reports nothing and fixes
        nothing.
        """
        push_key = f"{train.branch}@{train.tip}"
        ident = train.members[0]["id"] if train.members else None
        note = "push failed (network/auth instrument)"
        if reason == "push-non-ff":
            # CHURN-GUARD PROPAGATION (Grok MAJOR-1). This re-anchor moves the
            # base exactly as the tick-top sync does, so it must obey the SAME
            # hold. This train has already PASSED and left the running set (so it
            # never counts itself), but under MAX_CONCURRENT_GATES > 1 a SIBLING
            # gate can still be in flight — and advancing here would move the base
            # out from under it, orphaning it into a from-scratch re-gate, the
            # exact churn the tick-top guard exists to stop.
            sync = self._sync_main_from_origin(
                why=f"non-ff push of {train.branch}",
                hold_base=self._running_gate_count() > 0)
            if sync in ("synced", "reconciled-content-free"):
                self._append_ledger({
                    "ts": _iso(_now()), "role": ROLE, "event": "instrument_error",
                    "id": ident, "actor": ACTOR,
                    "detail": {"reason": f"gate PASSED but the push of {train.branch} to "
                                         f"{self.remote} was refused as non-fast-forward "
                                         "(the remote is ahead); local main rolled back and "
                                         "re-anchored onto the new base, re-assembles next "
                                         "tick",
                               "class": "instrument-error", "area": "tooling",
                               "kind": "push_non_ff", "slug": "push-non-ff",
                               "train": train.branch, "candidate_sha": train.tip,
                               "push_key": push_key, "sync": sync}})
                self._log(f"non-ff push of {train.branch}: re-anchored main onto "
                          f"{self.remote}/main — the identical push is NOT retried; the "
                          "train re-assembles on the new base next tick")
                return Outcome(train, "instrument",
                               f"push refused as non-fast-forward; re-anchored onto "
                               f"{self.remote}/main, re-assembles next tick")
            if sync == "deferred-gate-in-flight":
                # THE TRAP (Grok): a deferral is NOT "the re-anchor changed
                # nothing". This train is GREEN; the base was deliberately HELD to
                # protect a running sibling, and origin is legitimately ahead — so
                # the next push is not an identical broken input, it is a push we
                # have chosen to postpone. Falling through to the bounded budget
                # below would spend a push-retry and, after PUSH_RETRY_LIMIT, PARK
                # a passed train that never had anything wrong with it. Return
                # benign and non-terminal: consume NO budget, never park; the
                # train re-attempts the push next tick and lands once the sibling's
                # slot frees and the base advances.
                #
                # OBSERVABILITY (Gemini finding / Grok polish-1). A persistent
                # sibling-gate stall must be visible to the ledger/monitor, not only
                # the log. Emit a `push_deferred` row EDGE-TRIGGERED — once per
                # DEFERRED PUSH_KEY, never per tick over the ~25-min hold. The
                # marker CONTENT is the set of push_keys already announced this
                # episode, keyed per train so a SECOND concurrently-deferred train
                # (MAX_CONCURRENT_GATES > 1) is not suppressed by the first's marker
                # (Gemini favourable-absence finding; a bare existence flag hid the
                # 2nd+ train's deferral). The row carries `push_key` for correlation
                # but is deliberately kind=`push_deferred`, which `_push_failure_count`
                # (counting only `push_failed`) never scans, so it can NEVER count
                # toward PUSH_RETRY_LIMIT or park this green train.
                marker = self._push_deferred_marker()
                announced = self._read_marker(marker).get("push_keys")
                announced = announced if isinstance(announced, list) else []
                if push_key not in announced:
                    self._append_ledger({
                        "ts": _iso(_now()), "role": ROLE, "event": "instrument_error",
                        "id": ident, "actor": ACTOR,
                        "detail": {"reason": f"gate PASSED for {train.branch} but its push to "
                                             f"{self.remote} was refused non-fast-forward and "
                                             "the re-anchor was DEFERRED: a sibling gate is in "
                                             "flight, so the base is HELD rather than advanced "
                                             "(which would orphan the sibling). The train "
                                             "re-attempts the push once the sibling's slot "
                                             "frees; no push-retry budget is spent.",
                                   "class": "instrument-error", "area": "tooling",
                                   "kind": "push_deferred", "slug": "push-deferred",
                                   "train": train.branch, "candidate_sha": train.tip,
                                   "push_key": push_key, "sync": sync}})
                    self._write_json_atomic(marker, {
                        "push_keys": [*announced, push_key],
                        "updated": _iso(_now())})
                self._log(f"non-ff push of {train.branch}: re-anchor DEFERRED — a sibling "
                          "gate is in flight, so the base is HELD rather than moved out from "
                          "under it; this passed train re-attempts the push next tick (no "
                          "push-retry budget spent, never parked)")
                return Outcome(train, "instrument",
                               "push refused as non-fast-forward; re-anchor deferred while a "
                               "sibling gate is in flight — re-attempts next tick")
            note = (f"push refused as non-fast-forward but the re-anchor changed nothing "
                    f"({sync}) — the next push would be the identical input")
        attempt = self._push_failure_count(push_key) + 1
        self._append_ledger({
            "ts": _iso(_now()), "role": ROLE, "event": "instrument_error",
            "id": ident, "actor": ACTOR,
            "detail": {"reason": f"gate PASSED but push of {train.branch} to "
                                 f"{self.remote} failed; local main rolled back "
                                 + ("and the train state was PARKED"
                                    if attempt >= PUSH_RETRY_LIMIT else
                                    "re-lands next tick"),
                       "class": "instrument-error", "area": "tooling",
                       "kind": "push_failed", "slug": "push-failed",
                       "train": train.branch, "candidate_sha": train.tip,
                       "push_key": push_key, "attempt": attempt,
                       "limit": PUSH_RETRY_LIMIT, "note": note}})
        if attempt >= PUSH_RETRY_LIMIT:
            # PARK, do not keep retrying. The state file carries the terminal
            # disposition so the next tick skips this train instead of pushing
            # the identical thing a sixth time; an operator clears it after
            # fixing the push path.
            self._gate_state_done(train, "push-parked",
                                  f"{note}; {attempt} identical push failures")
            self._alert(
                f"PARKED {train.branch} after {attempt} identical push failures to "
                f"{self.remote} ({note}). The gate PASSED every time — this is the push "
                "path, not the code. Nothing further is retried for this train until the "
                f"push path is fixed and {gate_state_path(self.root, train).name} is "
                "cleared.")
            self._log(f"push failed for {train.branch} {attempt} times — PARKED "
                      "(never retried again on the identical input)")
            return Outcome(train, "instrument",
                           f"push to {self.remote} failed {attempt}x; train state parked")
        self._log(f"push failed for {train.branch}: rolled back, recorded "
                  f"instrument_error(push_failed) attempt {attempt}/{PUSH_RETRY_LIMIT}; "
                  "re-lands next tick")
        return Outcome(train, "instrument",
                       f"push to {self.remote} failed; rolled back, re-lands next tick "
                       f"(attempt {attempt}/{PUSH_RETRY_LIMIT})")

    # -- write-ahead landing intent -----------------------------------------
    # THE PUSH IS NOT THE LAST STEP OF A LANDING, and everything after it is a
    # separate write that can fail (round-4 review, BLOCKER). `main` advances and
    # the push publishes; only THEN come the receipt, the `merged` events, the
    # closures and the terminal gate state. A crash, an ENOSPC or a killed daemon
    # in that window leaves code ON MAIN with no terminal event for its members —
    # and `_reconcile_already_merged` cannot see it, because it checks whether the
    # candidate's ORIGINAL sha is an ancestor of main and a train's members were
    # CHERRY-PICKED (new shas, by design). The members therefore come back
    # eligible and can be assembled, gated and landed a SECOND time.
    #
    # This window is not new — a single train has always had it — but a chain
    # multiplies it by the prefix length, so it is this change's job to close.
    #
    # DESIGN CHOICE, and why this one. The alternative the review offered was to
    # teach `_reconcile_already_merged` to recognise cherry-picked-EQUIVALENT
    # trees (patch-id / `git cherry`). That would terminalise a candidate on
    # "something with your patch is on main" rather than "your reviewed commit is
    # on main", which is exactly the equivalence `_reconcile_already_merged`
    # documents itself as refusing ("Only exact commit ancestry qualifies; a
    # similar diff or a moved branch never does") — and the park remedy this same
    # change introduces ("re-anchor and re-file") deliberately MANUFACTURES
    # candidates that share a patch with another. Weakening that check would make
    # a terminal `merged` event derivable from a guess.
    #
    # So instead the landing records its INTENT durably BEFORE it touches main,
    # and the next tick completes any bookkeeping the crash interrupted. The
    # intent is written first or the landing does not happen at all, which makes
    # the ordering fail-closed: no push is ever issued that a restart cannot
    # account for.

    def _landing_intent_path(self) -> Path:
        return self.root / "state" / "landing-intent.json"

    def _write_landing_intent(self, prefix: list[tuple[Train, GateVerdict]]) -> bool:
        """Record what this landing is about to do. False = do not land."""
        deepest = prefix[-1][0]
        try:
            self._write_json_atomic(self._landing_intent_path(), {
                "kind": "landing-intent", "at": _iso(_now()), "by": ACTOR,
                "root": deepest.root, "merge_sha": deepest.tip,
                "trains": [{
                    "branch": t.branch, "base": t.base, "tip": t.tip,
                    "members": t.members, "paths": t.paths,
                    "parent": t.parent, "chain_root": t.chain_root,
                    "chain_index": t.chain_index,
                    "verdict": {
                        "result": v.result, "exit_code": v.exit_code,
                        "slug": v.slug, "reason": v.reason,
                        "receipt": str(v.receipt) if v.receipt else None,
                        "stdout_tail": v.stdout_tail, "duration_s": v.duration_s,
                    },
                } for t, v in prefix],
            })
        except OSError as exc:
            self._alert(
                f"REFUSING to land {deepest.branch}: the landing intent could not be "
                f"written ({type(exc).__name__}: {exc}). Advancing main without it "
                "would leave a crash in the post-push window unrecoverable, and the "
                "members re-landable. Nothing was pushed.")
            return False
        return True

    def _clear_landing_intent(self) -> None:
        self._landing_intent_path().unlink(missing_ok=True)

    def _train_from_intent(self, rec: dict) -> tuple[Train, GateVerdict] | None:
        try:
            v = rec["verdict"]
            receipt = v.get("receipt")
            return (
                Train(branch=rec["branch"], base=rec["base"], tip=rec["tip"],
                      members=list(rec.get("members") or []),
                      paths=list(rec.get("paths") or []),
                      parent=rec.get("parent"), chain_root=rec.get("chain_root"),
                      chain_index=int(rec.get("chain_index") or 0)),
                GateVerdict(str(v.get("result", "pass")), int(v.get("exit_code") or 0),
                            str(v.get("slug") or ""), str(v.get("reason") or ""),
                            Path(receipt) if receipt else None,
                            str(v.get("stdout_tail") or ""),
                            float(v.get("duration_s") or 0.0)),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def _recover_landing_intent(self, main_sha: str) -> None:
        """Finish the bookkeeping of a landing that was interrupted after the push.

        Runs at the top of the tick, BEFORE the terminal set and the candidate
        load, so anything it repairs is already terminal by the time this tick
        decides what is eligible.
        """
        path = self._landing_intent_path()
        if not path.exists():
            return
        try:
            intent = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            quarantine = path.with_name(f"landing-intent.corrupt-{int(time.time())}.json")
            try:
                path.rename(quarantine)
            except OSError:
                pass
            self._alert(
                f"UNREADABLE landing intent ({type(exc).__name__}: {exc}) — quarantined at "
                f"{quarantine.name}. If a landing was interrupted, its members may have no "
                "terminal event; reconcile the ledger against main by hand before trusting "
                "the next landing.")
            self._mark_tick_degraded("unreadable landing intent")
            return
        merge_sha = _full_sha((intent or {}).get("merge_sha"))
        if not merge_sha:
            self._alert("landing intent carries no usable merge_sha — discarding it; "
                        "it cannot describe a landing to complete")
            self._clear_landing_intent()
            return
        # An object that is not in this repository AT ALL cannot be on main — the
        # rolled-back tip of a train whose branch was retired can be garbage
        # collected. Checked first so that case ends in a clean discard instead
        # of an unreadable-ancestry alert repeating every tick forever.
        present, _, _ = git(self.repo, "cat-file", "-e", f"{merge_sha}^{{commit}}")
        rc, _, err = ((1, "", "") if present != 0 else
                      git(self.repo, "merge-base", "--is-ancestor", merge_sha, main_sha))
        if rc == 1:
            # The push never took effect (rolled back, or it never happened).
            # There is nothing on main to account for.
            self._log(f"landing intent for {merge_sha[:12]} is not on main — the landing "
                      "did not take effect; discarding the intent")
            self._clear_landing_intent()
            return
        if rc != 0:
            self._alert(f"could not determine whether the interrupted landing {merge_sha[:12]} "
                        f"reached main ({err[:160]}) — intent RETAINED; refusing to guess "
                        "in either direction")
            self._mark_tick_degraded("landing intent ancestry unreadable")
            return
        terminal = self._terminal_ids()
        recovered = 0
        for rec in (intent.get("trains") or []):
            pair = self._train_from_intent(rec if isinstance(rec, dict) else {})
            if pair is None:
                self._alert("a train inside the landing intent is unreadable — its members "
                            "may have no terminal event; skipping it, intent retained")
                return
            train, verdict = pair
            # NO SHORTCUT ON CANDIDATE-TERMINAL STATE (cross-lineage review,
            # 2026-08-12). This used to `continue` once every member had a
            # terminal event, on the assumption that the members ARE the
            # bookkeeping. They are not: `_record_landing` writes the member
            # `merged` events, THEN retires the resolved proposals, THEN emits
            # closures. A crash in that window leaves every member terminal and
            # the proposal un-retired -- and skipping here cleared the intent,
            # so the retirement was lost permanently and the proposal went back
            # to being handed out. `_record_landing` is idempotent (it skips a
            # member already in `terminal`, the retirement re-checks under its
            # own lock, and closures skip terminal findings), so replaying the
            # whole thing is both cheap and the only shape that cannot lose a
            # half-written landing.
            done = all(m.get("id") in terminal for m in train.members)
            self._log(
                (f"REPLAYING the tail of the landing of {train.branch} @ "
                 f"{merge_sha[:12]}: every member is already terminal, so this "
                 "completes only what comes after the `merged` events "
                 "(proposal retirement, closures)")
                if done else
                (f"RECOVERING interrupted landing of {train.branch} @ "
                 f"{merge_sha[:12]}: main already carries it but its members have no "
                 "terminal event"))
            try:
                self._record_landing(train, verdict, merge_sha, terminal=terminal)
            except (OSError, ValueError) as exc:
                self._alert(f"could not complete the interrupted landing of {train.branch} "
                            f"({type(exc).__name__}: {exc}) — intent RETAINED for the next "
                            "tick; its members stay non-terminal until then")
                self._mark_tick_degraded("landing recovery incomplete")
                return
            recovered += 1
        if recovered:
            self._alert(f"completed the bookkeeping of {recovered} train(s) whose landing "
                        f"was interrupted after the push to {merge_sha[:12]} (no candidate "
                        "was re-landed)")
        self._clear_landing_intent()

    def on_pass(self, train: Train, verdict: GateVerdict) -> Outcome:
        """One passed train, landed alone — a chain prefix of length one."""
        return self._land_prefix([(train, verdict)])[0]

    def _land_prefix(self, prefix: list[tuple[Train, GateVerdict]]) -> list[Outcome]:
        """Land the longest gate-PASSED PREFIX of one chain in ONE push.

        SINGLE-WRITER IS PRESERVED, and this is the whole reason the prefix is
        landed as a unit rather than train by train: `main` advances exactly once
        (one fast-forward, one push) to the DEEPEST passed train's tip, which by
        construction carries every ancestor's commits. Landing them one at a time
        would be N pushes, N windows for the remote to move, and N chances to
        leave main half-advanced.

        Every train in the prefix then gets its OWN receipt, its OWN `merged`
        events and its OWN closure pass, because each was graded by its own gate
        with its own bound tests — one train's green says nothing about another's
        bindings. That is `_record_landing`, shared verbatim with the
        single-train path so the two can never drift.
        """
        # FAIL-CLOSED STRUCTURAL CHECK. Landing the deepest tip fast-forwards
        # main over EVERY commit beneath it, so if the list handed here is not
        # the chain's own prefix starting at index 0, the merge would carry
        # ancestor trains whose verdicts nobody in this call ever looked at —
        # ungated code on main, by construction. The caller already guarantees
        # this; the guard is here because the cost of it being wrong is the one
        # thing this daemon exists to make impossible.
        ordered = [t for t, _ in prefix]
        broken = (ordered[0].chain_index != 0
                  or ordered[0].parent is not None
                  or any(later.parent != earlier.branch or later.base != earlier.tip
                         for earlier, later in zip(ordered, ordered[1:], strict=False)))
        if broken:
            names = ", ".join(f"{t.branch}#{t.chain_index}" for t in ordered)
            self._alert(
                f"REFUSING to land [{names}]: the trains handed to _land_prefix are not "
                "a contiguous chain prefix rooted on main, so a fast-forward to the "
                "deepest tip would carry ancestor commits this call never graded "
                "(instrument/bug, never a candidate fault)")
            return [Outcome(ordered[-1], "skipped",
                            "prefix is not a contiguous chain from main; refused")]
        deepest = ordered[-1]
        # WRITE-AHEAD: no push is issued that a restart could not account for.
        if not self._write_landing_intent(prefix):
            return [Outcome(deepest, "skipped",
                            "landing intent unwritable; refused to advance main")]
        merge_sha, reason = self.land_train(deepest)
        if not merge_sha:
            self._clear_landing_intent()
        if not merge_sha:
            if reason in ("push-failed", "push-non-ff"):
                # A PUSH REFUSAL IS NOT A SCHEDULE MISS (Grok MAJOR-1). It must be
                # DISTINGUISHABLE in the ledger from a routine base-move, so an
                # operator reading the stream sees "the gate PASSED but the push
                # did not reach origin", not silence. Non-terminal either way:
                # the candidate stays landable.
                head = self._on_push_refused(deepest, reason)
            else:
                head = Outcome(deepest, "skipped",
                               f"gate PASS but did not land ({reason}); "
                               "re-assembles next tick")
            # EVERY TRAIN IN THE ATTEMPTED PREFIX IS ACCOUNTED FOR (round-2
            # review, MINOR). The push carries the whole prefix, so when it fails
            # the ancestors did not land either — and returning only the deepest
            # train's outcome made 1..n-1 disappear from the tick's report
            # entirely. A train that was attempted and did not land must say so:
            # a silent absence in this list is indistinguishable from "never
            # considered", which is the favourable-absence shape this daemon
            # refuses everywhere else.
            for train in ordered[:-1]:
                self._log(f"{train.branch} PASSED and did not land: the prefix push at "
                          f"{deepest.branch} failed ({reason}); re-assembles next tick")
            return [*(Outcome(t, "skipped",
                              f"prefix landing failed at {deepest.branch} ({reason}); "
                              "re-assembles next tick")
                      for t in ordered[:-1]), head]
        if len(prefix) > 1:
            self._log(
                f"landing a chain PREFIX of {len(prefix)} trains in ONE push to "
                f"{merge_sha[:12]}: " + " -> ".join(t.branch for t, _ in prefix))
        # ONE FAILED WRITE MUST NOT ABANDON THE REST OF THE PREFIX (round-4
        # review, BLOCKER). main has already advanced over every train here, so
        # a receipt or ledger failure in train k says nothing about train k+1 —
        # and letting it propagate would leave k+1 landed on main with no
        # terminal event at all. Each train's bookkeeping is therefore contained,
        # and any failure KEEPS the intent so the next tick completes it.
        terminal = self._terminal_ids()
        outcomes: list[Outcome] = []
        incomplete: list[str] = []
        for train, verdict in prefix:
            try:
                self._record_landing(train, verdict, merge_sha, terminal=terminal)
            except (OSError, ValueError) as exc:
                incomplete.append(train.branch)
                self._alert(
                    f"{train.branch} LANDED at {merge_sha[:12]} but its records are "
                    f"INCOMPLETE ({type(exc).__name__}: {exc}) — the landing intent is "
                    "retained and the next tick completes them; the code is on main")
                outcomes.append(Outcome(train, "landed",
                                        f"merge_sha={merge_sha[:12]} (records incomplete)"))
                continue
            outcomes.append(Outcome(train, "landed", f"merge_sha={merge_sha[:12]}"))
        if incomplete:
            self._mark_tick_degraded(f"incomplete landing records: {', '.join(incomplete)}")
        else:
            self._clear_landing_intent()
        return outcomes

    def _record_landing(self, train: Train, verdict: GateVerdict,
                        merge_sha: str, *, terminal: set[str] | None = None) -> None:
        """Everything a landed train owes once `main` has actually moved.

        `merge_sha` is the sha main became — for a chained train that is the
        prefix's deepest tip, which is why the train's OWN graded sha rides
        separately as `candidate_sha`.

        `terminal` is the set of ids that already reached a terminal event. A
        member inside it is SKIPPED rather than given a second `merged`: this
        method is re-entered by the interrupted-landing recovery, where some
        members of a train may already be recorded, and `exactly_one_terminal_event`
        is a ledger invariant that cannot be repaired after the fact."""
        already = self._terminal_ids() if terminal is None else terminal
        receipt_rel = self._write_land_receipt(train, verdict, merge_sha)
        for m in train.members:
            if m["id"] in already:
                self._log(f"{_short(m['id'])} already has a terminal event — not "
                          "recording a second `merged` for it")
                continue
            self._append_ledger({
                "ts": _iso(_now()), "role": ROLE, "event": "merged", "id": m["id"],
                "base_sha": train.base, "actor": ACTOR,
                "detail": {"result": "pass", "merge_sha": merge_sha,
                           "receipt": receipt_rel, "train": train.branch,
                           "candidate_sha": train.tip,
                           "duration_s": round(verdict.duration_s, 1)}})
        for m in train.members:
            self._terminalize_resolved_proposals(m["id"], merge_sha, receipt_rel,
                                                  train.branch)
        self._emit_closures(train, verdict, merge_sha, receipt_rel)
        self._log(f"LANDED {train.branch} @ {merge_sha[:12]} — "
                  f"{len(train.members)} candidate(s) merged ff-only")
        self._gate_state_done(train, "landed", merge_sha)
        self._retire_train_branch(train)

    def _terminalize_resolved_proposals(self, member_id: str, merge_sha: str,
                                        receipt_rel: str, train_branch: str) -> None:
        """Retire every proposal this MERGED candidate resolves.

        A landed candidate's ``payload.resolves`` (a proposal id, or a LIST of
        them) is the queue's own claim that the candidate answers that
        proposal. Once the candidate is actually on `main`, the proposal it
        resolves must stop being offered to builders -- without this, the
        governor keeps handing out already-landed work forever, and builders
        rebuild it (the measured root cause of the proposal-queue bloat this
        method exists to close).

        Fail-closed: called only from `_record_landing`, i.e. only once a
        candidate is CONFIRMED merged -- never speculatively, never on a
        candidate that merely passed the gate. Idempotent: a proposal that
        already carries a terminal event (from this method, from a human, or
        from any other writer) is skipped, never given a second terminal
        event -- `exactly_one_terminal_event` is a ledger invariant.

        `resolves` absent or None is the OTHER half of the bloat (about half
        of live candidates never stamped the link at all): logged so the gap
        is visible, but never guessed at -- inventing a link here would be
        worse than the bloat it is meant to fix.

        Re-run safe: this iterates ALL of a train's members every time
        `_record_landing` runs (including the interrupted-landing replay for
        a member whose own `merged` was already recorded on an earlier tick),
        which also means a candidate that landed BEFORE this method existed
        gets its unretired proposals swept up the next time its train's
        landing bookkeeping is replayed -- a backfill, not just a forward
        fix, at zero extra cost because the read is idempotent either way.
        (For everything whose train will never be replayed, the one-shot
        `close_on_land.py retire-proposals` sweep is the other half.)

        Scope is EXACT candidate-id membership of what landed: the caller
        passes one landed member at a time, and only the proposals THAT
        member names are touched. A partial train -- a chain prefix that
        landed while later trains did not -- therefore retires only its own
        members' proposals; nothing is inferred from a landing "nearby".
        """
        if _full_sha(merge_sha) is None:
            # Fail closed BEFORE reading anything: with no nameable landing
            # commit there is no evidence to retire against, and a retirement
            # whose carrier cannot be checked is worse than none.
            self._log(f"proposal-retirement-withheld: {_short(member_id)} has no "
                      f"nameable landing commit ({merge_sha!r}) -- nothing retired")
            return
        art = self._read_queue_artifact("candidates", member_id)
        if art is None:
            # UNKNOWN is not ABSENT. A candidate file that is missing or
            # unreadable tells us nothing about what it resolved, and unknown
            # never reads as a licence to retire (or as proof there is
            # nothing to retire) -- say so, and leave the proposals offered.
            self._log(f"proposal-retirement-withheld: {_short(member_id)} landed but "
                      "its candidate artifact is missing/unreadable -- what it "
                      "resolves is UNKNOWN, not absent; nothing retired")
            return
        problem = envelope_identity_problem(art, member_id)
        if problem is not None:
            # The `resolves` edge is only as trustworthy as the envelope that
            # asserts it, and `_read_queue_artifact` returns whatever body sits
            # at the path it built from `member_id` -- it does not check that
            # the body agrees it IS that member. Both halves are enforced here
            # (`envelope_identity_problem`): the body must be the artifact we
            # looked up, AND it must hash to its own id. What is being bought
            # with that trust is an unrepairable terminal event on somebody
            # else's plan, so it is never bought on an unproved id.
            self._log(f"proposal-retirement-withheld: {_short(member_id)} landed but "
                      f"its candidate envelope does not prove its identity -- "
                      f"{problem}; edited in place, misfiled, or forged. Re-file "
                      "the envelope; nothing retired")
            return
        payload = art.get("payload")
        raw = payload.get("resolves") if isinstance(payload, dict) else None
        if raw is None:
            self._log(f"{_short(member_id)} landed with no payload.resolves -- "
                      "the proposal link was never stamped on this candidate; "
                      "nothing to retire (sub-case 2 of the bloat: the link "
                      "was lost, not merely unterminalized)")
            return
        refs = raw if isinstance(raw, list) else [raw]
        # A cheap pre-filter only. The AUTHORITATIVE terminal check happens
        # inside `retire_proposal_once`, under the retirement lock, immediately
        # before the append -- this set can save work, never authorize a write.
        already = self._terminal_ids()
        for ref in refs[:32]:                 # bounded fan-out, matches _resolve_bindings
            if not isinstance(ref, str) or not _ARTIFACT_ID_RE.fullmatch(ref.strip()):
                self._log(f"{_short(member_id)} payload.resolves names an "
                          f"unrecognised id {ref!r} -- skipped, not guessed at")
                continue
            pid = ref.strip()
            if pid in already:
                continue
            try:
                written = retire_proposal_once(
                    self.root, pid, resolved_by=member_id, carrier_sha=merge_sha,
                    receipt=receipt_rel, train=train_branch,
                    append=self._append_ledger)
            except (ValueError, OSError) as exc:
                # Same posture as `_emit_closures`: the landing already
                # happened and must not be abandoned over a retirement that
                # is safe to withhold -- the proposal simply stays offered
                # and gets swept up next time this replays.
                self._log(f"proposal-retirement-withheld: {_short(pid)} -- "
                          f"{type(exc).__name__}: {exc}")
                continue
            already.add(pid)
            if written is None:
                self._log(f"{_short(pid)} reached a terminal event before this "
                          "retirement could be written -- not stamping a second one")
                continue
            self._log(f"RETIRED proposal {_short(pid)} -- resolved by "
                      f"{_short(member_id)} landing at {merge_sha[:12]}")

    def _emit_closures(self, train: Train, verdict: GateVerdict, merge_sha: str,
                       receipt_rel: str) -> None:
        """Emit `closed` on every finding this landing actually PROVED closed.

        Beside the `merged` events (which terminalise the CANDIDATE), this is the
        FINDING-side terminal event: the one thing in the pipeline that can say
        "this defect is gone" without an agent asserting it. It is emitted only
        when all four of these hold, and each one has cost something to learn:

          1. the chain resolved at load time (candidate -> proposal -> finding +
             node), so the finding and the test are the queue's claim, not ours;
          2. the run receipt NAMES this node in `bound_test` — proof the gate was
             actually handed this binding, not merely that some other member's
             binding went green;
          3. `bound_test_result == "green"`. The field is WORST-WINS across the
             whole run in merge-gate.sh, so green means every binding EXECUTED
             and passed; `red`, `weakened` (defeated rather than satisfied — a
             skip, a deselect, an edited test) and a MISSING field all withhold.
             The missing case is the older-gate case and is the plan's own named
             worst outcome: a `closed` minted on an unmeasured test;
          4. the finding is not already terminal — a second terminal event on one
             id is exactly what `ledger.exactly_one_terminal_event` refuses.

        Withholding is never silent. A landing that closed nothing must be
        readable as such, or "the loop is not closing findings" and "the gate
        never ran the test" look identical in the log.
        """
        bindings = self._train_bindings(train)
        if not bindings:
            return
        fields = _receipt_fields(verdict.receipt)
        result = fields.get("bound_test_result") if fields else None
        if result not in ("green", "red", "weakened"):
            self._log(
                "closed-withheld: gate did not report bound_test_result — "
                f"{train.branch} landed at {merge_sha[:12]} but no finding is closed on it "
                f"({len(bindings)} binding(s) unmeasured)")
            return
        if result != "green":
            self._log(
                f"closed-withheld: bound_test_result={result} on {train.branch} — the "
                "bound test did not pass on the merged tree; the candidate landed, the "
                "finding stays OPEN")
            return
        ran = fields.get("bound_test") if fields else None
        ran_nodes = {n for n in ran if isinstance(n, str)} if isinstance(ran, list) else set()
        already = self._terminal_ids()
        closed: set[str] = set()
        for b in bindings:
            if b.node_id not in ran_nodes:
                self._log(
                    f"closed-withheld: {b.node_id} is not in the run receipt's bound_test "
                    f"list — the gate was never told about {_short(b.finding_id)}, so its "
                    "green says nothing about it")
                continue
            if b.finding_id in already or b.finding_id in closed:
                self._log(f"closed-withheld: {_short(b.finding_id)} already has a terminal "
                          "event — never stamping a second one")
                continue
            try:
                self._append_ledger({
                    "ts": _iso(_now()), "role": ROLE, "event": "closed",
                    "id": b.finding_id, "actor": ACTOR,
                    "detail": {"closed_by": b.member_id, "merge_sha": merge_sha,
                               "bound_test": b.node_id, "receipt": receipt_rel,
                               "train": train.branch}})
            except (ValueError, OSError) as exc:
                # The write boundary refused it (ValueError), or the ledger
                # transport itself failed (LedgerAppendError, an OSError). Either
                # way it is not a reason to abandon the landing that already
                # happened — nor the records of the OTHER trains in this prefix,
                # which is what an escaping LedgerAppendError used to cost
                # (round-4 review, BLOCKER). A closure is the one thing here that
                # is safe to withhold: the finding simply stays open.
                self._log(f"closed-withheld: {_short(b.finding_id)} — "
                          f"{type(exc).__name__}: {exc}")
                continue
            closed.add(b.finding_id)
            self._log(f"CLOSED {_short(b.finding_id)} by {_short(b.member_id)} — "
                      f"{b.node_id} green on {merge_sha[:12]}")

    def on_candidate_defect(self, train: Train, verdict: GateVerdict,
                            ancestors: list[Train] | None = None) -> Outcome:
        """A real defect in the code. Reject every member with a mandatory TTL
        and a `rejected/<id>.json` — the rejection file is what stops every other
        loop rediscovering a dead idea. On a MULTI-member train the failure is
        attributed to the train (no bisection in the mechanical loop); the reason
        says so, and the TTL means an innocent member can be re-argued rather
        than banned. This is recorded as a KNOWN coarseness, not a silent one.

        `ancestors` are the CHAIN trains whose commits were in the graded tree
        beneath this one. Every one of them PASSED its own gate on this same
        base, which is what makes the defect attributable to THIS train's members
        — the only ungreen difference between the two trees. It is still recorded
        in the rejection, because the one thing that argument does not cover is
        an INTERACTION defect (each train green alone, red together), and an
        agent sent to debug this rejection must be able to see the tree it was
        actually graded on rather than assume `main` + its own diff.

        MEMBER-AWARE ISOLATION (the whole point of this handler now): a composite
        verdict is NOT member-specific evidence, so a MULTI-member train is never
        terminalised here. It is stamped ``isolation-pending`` and its members
        re-gate SOLO, so a terminal rejection can only ever be written against a
        one-member train — exactly the tree that failed. The single-member path
        below is unchanged: a one-member train IS its own evidence."""
        if len(train.members) > 1:
            return self._isolate_train(train, verdict, ancestors)
        expires = _iso(_now() + timedelta(days=REJECT_TTL_DAYS.get("candidate-defect", 7)))
        multi = len(train.members) > 1
        chain_ancestors = [a.branch for a in ancestors or ()]
        receipt_rel = self._receipt_rel(verdict.receipt)
        # A BINDING refusal names its own remedy. The two ways a bound test
        # refuses are "the fix does not work" and "the fix edited the test it was
        # bound to", and both have the SAME wrong answer available (weaken the
        # test until it passes). Saying so in the event is what lets Phase 6 count
        # attempts on the finding axis instead of re-litigating the refusal.
        bound_slug = _bound_test_refusal(verdict)
        train_nodes = [b.node_id for b in self._train_bindings(train)]
        for m in train.members:
            reason = verdict.reason
            if multi:
                reason = (f"train {train.branch} ({len(train.members)} members) failed the "
                          f"gate: {verdict.reason} — attributed to the train (no bisection in "
                          "the mechanical loop); re-argue after TTL if this member is innocent")
            if chain_ancestors:
                reason += (f" [graded on top of {len(chain_ancestors)} chain ancestor train(s) "
                           f"that each PASSED their own gate on this base "
                           f"({', '.join(chain_ancestors)}), so this train's members are the "
                           "only ungreen difference — but read the receipt before assuming "
                           "the defect is independent of them]")
            detail = {"reason": reason, "class": "candidate-defect",
                      "expires_at": expires, "remedy": "replan",
                      "receipt": receipt_rel or "", "train": train.branch}
            if chain_ancestors:
                detail["chain_ancestors"] = chain_ancestors
            if bound_slug:
                # The member's OWN bindings when it has them, else the train's:
                # the mechanical loop does not bisect, so this is the list the
                # gate was GIVEN, never a guess at which one failed.
                own = [b.node_id for b in self.bindings.get(m["id"], ())]
                detail["bound_test"] = own or train_nodes
                detail["bound_test_refusal"] = bound_slug
                detail["remedy"] = "fix the named cause; do not edit the bound test"
            self._append_ledger({
                "ts": _iso(_now()), "role": ROLE, "event": "rejected", "id": m["id"],
                "base_sha": train.base, "actor": ACTOR, "detail": detail})
            self._write_json_atomic(
                self.root / "rejected" / f"{_stem(m['id'])}.json",
                {"id": m["id"], "kind": "candidate", "reason": reason,
                 "class": "candidate-defect", "at": _iso(_now()), "expires_at": expires,
                 "by": ACTOR, "receipt": receipt_rel,
                 "detail": {"remedy": detail["remedy"], "branch": m["branch"],
                            "base_sha": train.base, "train": train.branch,
                            **({"chain_ancestors": chain_ancestors} if chain_ancestors else {}),
                            **({"bound_test": detail["bound_test"]} if bound_slug else {})}})
        self._log(f"REJECTED {len(train.members)} member(s) of {train.branch}: {verdict.slug}")
        self._gate_state_done(train, "rejected", verdict.slug)
        return Outcome(train, "rejected", verdict.reason)

    def _isolate_train(self, train: Train, verdict: GateVerdict,
                       ancestors: list[Train] | None = None) -> Outcome:
        """A multi-member candidate-defect verdict ISOLATES its members instead of
        terminalising them. No `rejected` event, no `rejected/<id>.json` tombstone
        is written on the composite verdict — that is the measured
        15-bans-from-5-verdicts blast radius this exists to stop.

        DURABILITY HAS TWO CARRIERS, one authoritative:

          1. The DURABLE ledger backstop — one append-only ``isolated`` event per
             member (id = that member's canonical sha256, so the ledger's id
             field is satisfied). This is an OFF-ENUM extension event: NON-terminal
             (never counted by `_terminal_ids`), read ONLY by `_pending_isolation_ids`.
             It is the SOLE source of truth that survives an unreadable OR
             quarantined carrier file — the doom-loop only dies if isolation
             membership cannot evaporate with a fragile file.
          2. The readable ``iso-<train>@<tip>`` carrier — a `state:closed` audit
             mirror, namespaced so it can never be counted as a running lease, and
             used for the operator-visible disposition and dormancy stamping.

        DURABILITY IS DECIDED BY THE LEDGER, NOT THE CARRIER. "isolated" is
        returned ONLY when at least ONE ledger event actually persisted (each
        carries the complete sorted group, so one suffices to reconstruct the
        whole membership). If EVERY append failed, the carrier is NOT written and
        the composite's gate lease is left OPEN so it reassembles and re-attempts
        the durable append next tick — a successful carrier write must never be
        mistaken for durability (round-4 BLOCKER B). Never a terminal ban."""
        isolation_members = sorted(m["id"] for m in train.members)
        chain_ancestors = [a.branch for a in ancestors or ()]
        source = {"train": train.branch, "base": train.base, "tip": train.tip}
        if chain_ancestors:
            source["chain_ancestors"] = chain_ancestors
        receipt_rel = self._receipt_rel(verdict.receipt) or ""
        now = _iso(_now())

        # 1) DURABLE backstop first — one non-terminal `isolated` event per member.
        #    Each event carries the COMPLETE SORTED member group, so a crash
        #    mid-append can never SHRINK the isolated set: any ONE surviving event
        #    reconstructs the whole group (favourable-absence guard on durability).
        appended = 0
        for mid in isolation_members:
            try:
                self._append_ledger({
                    "ts": now, "role": ROLE, "event": ISOLATED_EVENT, "id": mid,
                    "base_sha": train.base, "actor": ACTOR,
                    # NB: no `detail.class` — that field is a REFUSAL-class enum in
                    # the ledger schema, and `isolated` is not a refusal.
                    "detail": {"kind": "isolation", "train": train.branch,
                               "base": train.base, "tip": train.tip,
                               "classifier": verdict.slug, "receipt": receipt_rel,
                               "isolation_members": isolation_members,
                               **({"chain_ancestors": chain_ancestors}
                                  if chain_ancestors else {})}})
                appended += 1
            except Exception as exc:  # noqa: BLE001 — transport fault must not abort the tick
                self._alert(f"could not durably record isolation of {_short(mid)} "
                            f"({type(exc).__name__}: {exc})")

        if appended == 0:
            # ZERO durable events: the backstop was NOT written, so isolation would
            # rest on the fragile carrier alone (the round-2 re-shred hole). Do NOT
            # claim durable isolation and do NOT write the carrier; leave the gate
            # LEASE open so the composite reassembles and RE-attempts the append.
            self._mark_tick_degraded(f"isolation not durably recorded for {train.branch}")
            self._alert(f"isolation of {train.branch} NOT durably recorded "
                        f"(0/{len(isolation_members)} ledger events) — retrying next "
                        "tick, no member terminalised, no fragile carrier written")
            return Outcome(train, "degraded", "isolation ledger append failed")

        # >=1 event durably recorded. Write the readable audit mirror (namespaced —
        # never a lease). Its failure is non-fatal: the ledger holds the truth.
        sf = iso_state_path(self.root, train)
        st = {
            "state": "closed",
            "disposition": "isolation-pending",
            "isolation_members": isolation_members,
            "isolation_source": source,
            "classifier": verdict.slug,
            "receipt": receipt_rel,
            "isolated_at": now,
            "closed_at": now,
        }
        try:
            self._write_json_atomic(sf, st)
        except OSError as exc:
            self._mark_tick_degraded(f"isolation carrier write failed for {train.branch}")
            self._alert(f"could not write isolation carrier for {train.branch} "
                        f"({type(exc).__name__}: {exc}) — ledger backstop is durable, "
                        "members remain isolated")
        else:
            self._log(f"ISOLATED {len(isolation_members)} member(s) of {train.branch}: "
                      f"{verdict.slug} — each re-gates SOLO (no train-wide rejection)")
        # Close the composite's own gate LEASE file so it is skipped, not re-read,
        # if it ever reassembles (its members are also forced solo — belt & braces).
        self._gate_state_done(train, "isolated", verdict.slug)
        return Outcome(train, "isolated", verdict.slug)

    def _ledger_isolated_active(self, active_ids: set[str]) -> set[str]:
        """The DURABLE isolation backstop: still-active member IDs reconstructed
        from non-terminal ``isolated`` ledger events. This is the source of truth
        that survives an unreadable OR quarantined `iso-` carrier — the ledger is
        append-only, so isolation membership cannot evaporate with a fragile file.

        Each event carries the COMPLETE SORTED group, so the reconstruction unions
        every event's group: a crash that left only a subset of a group's events
        still yields the WHOLE group.

        SKIP A SINGLE BAD RECORD, NEVER HALT THE TICK (round-5 availability). The
        ledger is append-only and never rewritten, so raising on one torn line or
        one malformed event would re-veto EVERY tick forever — a permanent
        total-dispatch-halt from a benign crash artifact, strictly worse than the
        TTL-recoverable re-shred it guards against. So:
          * a torn/UNDECODABLE NON-final line -> SKIP it, build membership from the
            decodable events, and edge-alert ONCE (keyed on the line's content);
          * an `isolated` event whose ``isolation_members`` is not a NON-EMPTY list
            of canonical sha256 ids -> SKIP that ONE event (the complete-group
            invariant means its well-formed siblings still carry the full group),
            edge-alert ONCE. A group ALL of whose events are bad degrades only to
            pre-failiso batching for those members — the gate still runs, so no bad
            code merges.
        The ONLY hard failure here is `LedgerUnreadable` on an OSError reading the
        whole file; the caller VETOES only if there is ALSO no readable carrier —
        i.e. no usable isolation source at all."""
        path = self.root / "ledger.jsonl"
        if not path.exists():
            return set()                       # missing -> caller decides via carriers
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise LedgerUnreadable(f"{type(exc).__name__}: {exc}") from exc
        group_members: set[str] = set()
        lines = text.splitlines()
        last = len(lines) - 1
        for i, line in enumerate(lines):
            events, status = parse_events(line)
            for ev in events:
                if ev.get("event") != ISOLATED_EVENT:
                    continue
                detail = ev.get("detail") if isinstance(ev.get("detail"), dict) else {}
                group = detail.get("isolation_members")
                if not (isinstance(group, list) and group
                        and all(isinstance(m, str)
                                and _ARTIFACT_ID_RE.fullmatch(m.strip()) for m in group)):
                    self._edge_alert(
                        "iso-ledger-malformed-event:"
                        + hashlib.sha256(line.encode("utf-8", "replace")).hexdigest(),
                        f"skipped a malformed isolated ledger event "
                        f"(id={_short(ev.get('id') or '')}, members={group!r}) — building "
                        "isolation from its well-formed sibling events; the gate still "
                        "runs for these members, no bad code can merge")
                    continue
                group_members.update(m.strip() for m in group)
            if status == UNDECODABLE and i != last:
                # SKIP a torn NON-tail line rather than halt every landing forever.
                self._edge_alert(
                    "iso-ledger-torn-line:"
                    + hashlib.sha256(line.encode("utf-8", "replace")).hexdigest(),
                    f"skipped an undecodable ledger line (#{i + 1} of {len(lines)}) while "
                    "reading the isolation backstop — membership built from the decodable "
                    "events; its members, if any, degrade to normal batching (gate still runs)")
        return {m for m in group_members if m in active_ids}

    def _pending_isolation_ids(self, active_ids: set[str]) -> set[str]:
        """The still-active member IDs that must re-gate SOLO. Called BEFORE
        assembly; the returned IDs are forced into one-member trains so an
        isolated member re-gates alone instead of re-batching.

        Two carriers, UNIONED so membership never depends on a fragile file:
          * the DURABLE ledger backstop (`_ledger_isolated_active`) — authoritative,
            survives an unreadable or quarantined carrier;
          * the readable ``iso-*`` carriers — used for dormancy stamping and the
            hex/shape validation veto.

        `active_ids` is the current non-terminal candidate set. Once EVERY member
        of a carrier is terminal-or-absent it is stamped ``isolation-resolved``
        (dormant); audit content is KEPT, never erased (step 5).

        FAIL-CLOSED handling — VETO ONLY WHEN THERE IS NO USABLE ISOLATION SOURCE:
          * a readable carrier with a malformed shape (bad members, or a non-hex
            base/tip) -> `MalformedIsolation` (a tick veto — clear, actionable
            corruption of a daemon-written, operator-removable file);
          * an UNREADABLE carrier does NOT abort the tick when the ledger backstop
            is usable: it is quarantined on a SHORT bound (`ISO_UNREADABLE_
            QUARANTINE_S`, never the 2h gate deadline);
          * the ledger being unreadable AT ALL (OSError) is fatal ONLY if there is
            also no readable carrier to serve as the source — no usable isolation
            source at all is the one case worth a hard stop."""
        try:
            ids: set[str] = set(self._ledger_isolated_active(active_ids))
            ledger_unreadable = False
        except LedgerUnreadable:
            ids = set()
            ledger_unreadable = True
        gdir = self.root / "state" / "gates"
        if not gdir.is_dir():
            if ledger_unreadable:
                raise IsolationVeto(
                    "no usable isolation source: the ledger is unreadable (OSError) and "
                    "there is no carrier backstop", path="ledger.jsonl")
            return ids
        now = time.time()
        readable_carrier = False
        for p in sorted(gdir.glob("iso-*.json")):
            try:
                st = json.loads(p.read_text(encoding="utf-8"))
                if not isinstance(st, dict):
                    st = {}
            except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                # An unreadable carrier does not abort the tick when the ledger is
                # a usable backstop — quarantine it on a SHORT bound (a closed
                # record is never a 2h running gate). When the ledger is ALSO
                # unreadable this carrier gives nothing, and the no-source veto
                # below decides the tick.
                if not ledger_unreadable:
                    raw = ""
                    try:
                        raw = p.read_text(encoding="utf-8", errors="replace")
                    except OSError:
                        raw = ""
                    self._quarantine_unreadable_iso(p, raw, now=now)
                continue
            if st.get("disposition") != "isolation-pending":
                continue
            members = st.get("isolation_members")
            source = st.get("isolation_source")
            base = source.get("base") if isinstance(source, dict) else None
            tip = source.get("tip") if isinstance(source, dict) else None
            # base/tip must be full 40-HEX shas, not merely 40 characters — a
            # non-hex value like "z"*40 is a corrupt source, not a valid one, and
            # must veto (a length-only check is exactly what let that through).
            well_formed = (
                isinstance(members, list) and members
                and all(isinstance(m, str) and _ARTIFACT_ID_RE.fullmatch(m.strip())
                        for m in members)
                and _full_sha(base) is not None
                and _full_sha(tip) is not None)
            if not well_formed:
                raise MalformedIsolation(
                    f"isolation record {p.name} is malformed "
                    f"(members={members!r}, base={base!r}, tip={tip!r}) — refusing to "
                    "assemble this tick: an isolation with no usable member set is a "
                    "fail-closed veto, never an empty set that re-batches the members",
                    path=p.name)
            # A readable, well-formed carrier IS a usable isolation source, so it
            # justifies proceeding even if the ledger was unreadable.
            readable_carrier = True
            canonical = [m.strip() for m in members]
            still_active = [m for m in canonical if m in active_ids]
            if not still_active:
                # Step 5 dormancy: every member is terminal or gone. Retire the
                # record (audit content kept) so it is neither re-parsed nor a
                # source of phantom forced-solos, and never a second queue.
                self._retire_isolation_record(p, st)
                continue
            ids.update(still_active)
        if ledger_unreadable and not readable_carrier:
            # No usable isolation source at all: the ledger cannot be read and no
            # readable carrier could stand in. THIS is the only case worth a hard
            # stop — a genuine total failure, not one bad line among good ones.
            raise IsolationVeto(
                "no usable isolation source: the ledger is unreadable (OSError) and no "
                "readable iso- carrier can serve as the backstop", path="ledger.jsonl")
        return ids

    def _quarantine_unreadable_iso(self, p: Path, raw: str, *, now: float) -> None:
        """A SHORT-bounded quarantine for an unreadable ``iso-`` carrier. It is a
        `state:closed` audit mirror, not a running lease, so it must clear fast
        (`ISO_UNREADABLE_QUARANTINE_S`) — the durable ledger holds its membership,
        so nothing is lost. Within the bound the file is left in place (a true
        transient blip repairs itself); past it the file is renamed out of the
        scan and the operator is alerted once."""
        started_at = self._veto_started_at(
            p, None, kind="iso-unreadable", now=now, raw=raw)
        if started_at + ISO_UNREADABLE_QUARANTINE_S > now:
            marker_path = self._alert_marker_path(f"gate-veto:{p}")
            marker = self._read_marker(marker_path)
            if not marker.get("iso_alert_emitted"):
                marker.update({"kind": "iso-unreadable", "veto_started_at": started_at,
                               "path": str(p), "iso_alert_emitted": True})
                self._write_json_atomic(marker_path, marker)
                self._alert(f"unreadable isolation carrier {p.name} — ledger backstop "
                            f"holds its members; short-bound quarantine at "
                            f"{ISO_UNREADABLE_QUARANTINE_S}s (NOT the 2h gate deadline)")
            return
        self._quarantine_gate_state(p, kind="iso-unreadable")
        self._clear_veto_marker(p)

    def _retire_isolation_record(self, p: Path, st: dict) -> None:
        """Stamp a fully-resolved isolation CARRIER ``isolation-resolved``. The
        source train/base/tip and member list are preserved for audit — the
        carrier is made DORMANT, never deleted, matching how the rest of the file
        retires state.

        This retires only the readable CARRIER, not the durable ledger truth:
        `_ledger_isolated_active` rescans the append-only ledger every tick and
        filters by the LIVE active-candidate set, so membership is governed by
        liveness, not by carrier disposition. A member that goes terminal drops
        out of both; a member that is re-argued after TTL and re-enters the active
        set would be re-forced solo by the ledger backstop — which errs toward
        solo (safe: a re-argued idea gates alone), never toward re-batching."""
        st = dict(st)
        st.update({"state": "closed", "disposition": "isolation-resolved",
                   "resolved_at": _iso(_now())})
        try:
            self._write_json_atomic(p, st)
            self._log(f"isolation record {p.name} is dormant: every member terminal "
                      "or absent — retired for audit, no longer forcing solos")
        except OSError as exc:
            # A failed retirement only means one more harmless re-parse next tick;
            # it never drops an active isolation. Degrade so it is visible.
            self._mark_tick_degraded(f"could not retire isolation record {p.name}")
            self._log(f"could not retire isolation record {p.name} ({exc})")

    def on_instrument_error(self, train: Train, verdict: GateVerdict) -> Outcome:
        """The gate's ENVIRONMENT failed, not the code. Never a rejection: an
        instrument_error event (non-terminal) plus ONE inquiry (area: tooling),
        and re-gate ONCE. A second instrument failure on the same key does NOT
        re-gate again — that would storm — it leaves the inquiry standing for a
        human to fix the instrument.

        I8: when the state is still ``running`` with a recorded pid (the
        deadline-expiry case — the only instrument verdict where the child may
        still be alive), identity-verified reap runs BEFORE any unlink so a
        re-dispatch cannot race the prior gate.
        """
        train_key = f"{train.branch}@{train.tip}"
        prior = self._instrument_regate_count(train_key)
        self._raise_inquiry(train, verdict)
        sf = gate_state_path(self.root, train)
        reap_outcome: str | None = None
        unreadable_state = False
        try:
            st = json.loads(sf.read_text(encoding="utf-8")) if sf.exists() else {}
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            st = {}
            unreadable_state = sf.exists()
        if unreadable_state:
            # A malformed state is a bounded veto, not a re-gate candidate.
            # Deleting it here would cancel the veto in the very tick that
            # observed it and double-book an unverified child.
            self._append_ledger({
                "ts": _iso(_now()), "role": ROLE, "event": "instrument_error",
                "id": train.members[0]["id"] if train.members else None,
                "actor": ACTOR,
                "detail": {"reason": verdict.reason, "class": "instrument-error",
                           "area": "gate-instrument", "slug": verdict.slug,
                           "train": train.branch, "train_key": train_key,
                           "kind": "unreadable-gate-state-held"},
            })
            self._log(f"instrument error on {train.branch} ({verdict.slug}) — "
                      "unreadable gate state held by bounded veto (no re-gate)")
            return Outcome(train, "instrument", f"{verdict.slug}: corrupt state held")
        if isinstance(st, dict) and st.get("state") == "running":
            # Every instrument attempt reaps a still-running child before either
            # re-gating or terminally parking it, so a second failure cannot
            # free a live twin through the terminal state stamp.
            reap_outcome = self._reap_gate_child(st, sf, reason="instrument-expiry")
            if reap_outcome in ("identity-mismatch", "unverifiable"):
                self_bounded = bool(st.get("self_bounded"))
                missing_pid = not isinstance(st.get("pid"), int) or st.get("pid", 0) <= 0
                if not self_bounded or missing_pid:
                    self._park_unverifiable_reap(sf, st, reap_outcome)
                    disposition = "reap-unverifiable-parked"
                else:
                    # A new-format child has an independently enforced timeout
                    # inside this lease.  Its confirmed signal may be unknown,
                    # but its remote gate cannot outlive the release premise.
                    self._gate_state_done(train, "reap-unverifiable", reap_outcome)
                    disposition = "reap-unverifiable"
                self._append_ledger({
                    "ts": _iso(_now()), "role": ROLE, "event": "instrument_error",
                    "id": train.members[0]["id"] if train.members else None,
                    "actor": ACTOR,
                    "detail": {
                        "reason": verdict.reason, "class": "instrument-error",
                        "exit_code": verdict.exit_code, "slug": verdict.slug,
                        "train": train.branch, "train_key": train_key,
                        "candidate_sha": train.tip, "regate_attempt": prior + 1,
                        "reap": reap_outcome, "disposition": disposition,
                    }})
                self._log(
                    f"instrument error on {train.branch} ({verdict.slug}) — "
                    f"reap={reap_outcome}; state {disposition} (no re-gate)")
                return Outcome(
                    train, "instrument",
                    f"{verdict.slug}: {disposition} (reap {reap_outcome})")
        self._append_ledger({
            "ts": _iso(_now()), "role": ROLE, "event": "instrument_error",
            "id": train.members[0]["id"] if train.members else None, "actor": ACTOR,
            "detail": {
                "reason": verdict.reason, "class": "instrument-error",
                "exit_code": verdict.exit_code, "slug": verdict.slug,
                "train": train.branch, "train_key": train_key,
                "candidate_sha": train.tip, "regate_attempt": prior + 1,
                **({"reap": reap_outcome} if reap_outcome is not None else {}),
            }})
        if prior < 1:
            # Re-gate ONCE: clear the state so the next tick re-dispatches this
            # exact train (a fresh gate on the same input is legitimate ONLY
            # because the failure was the instrument, not the code). Reap
            # (above) ran first when the child may still have been alive.
            sf.unlink(missing_ok=True)
            self._log(
                f"instrument error on {train.branch} ({verdict.slug}) — "
                f"re-gating once"
                + (f" (reap={reap_outcome})" if reap_outcome else ""))
            return Outcome(train, "instrument", f"{verdict.slug}: re-gate scheduled")
        self._gate_state_done(train, "instrument-parked", verdict.slug)
        self._log(f"instrument error on {train.branch} ({verdict.slug}) AGAIN — NOT "
                  "re-gating (would storm); inquiry stands for the instrument to be fixed")
        return Outcome(train, "instrument", f"{verdict.slug}: parked after re-gate")

    def _park_unverifiable_reap(self, path: Path, st: dict, reap: str) -> None:
        """Keep an unverifiable lease occupied until its recorded veto exits."""
        try:
            current = json.loads(path.read_text(encoding="utf-8")) if path.exists() else st
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            current = st
        if not isinstance(current, dict) or current.get("state") != "running":
            return
        if current.get("disposition") == "reap-unverifiable-parked":
            # Observation is not a transition.  In particular, do not reset the
            # bounded-exit clock by re-writing this file every launchd tick.
            return
        parked_at = time.time()
        current.update({"disposition": "reap-unverifiable-parked", "reap": reap,
                        "parked_at": parked_at})
        self._write_json_atomic(path, current)
        msg = (f"reap unverifiable for {path.name}; retaining legacy/no-pid lease "
               "until parked_at + GATE_DEADLINE_S")
        self._alert(msg)

    def _reap_gate_child(self, st: dict, sf: Path, *, reason: str) -> str:
        """Identity-verified killpg backstop (I8). Never raises.

        Outcomes recorded in ledger detail ``"reap"``:
          * ``reaped`` — identity matched; SIGTERM → 2s grace → SIGKILL
          * ``pid-absent`` — no live process at the recorded pid (may free)
          * ``identity-mismatch`` / ``unverifiable`` — NEVER kill.  A state
            carrying ``self_bounded: true`` may be terminalised because its
            child timeout is inside the lease; a legacy/no-pid state is parked
            until the bounded state-file veto expires.
          * ``done-skip`` — state already terminal (child exited)
        """
        state = st.get("state")
        if state == "done" or state == "closed":
            self._log(f"reap skip {sf.name}: state={state} (child already exited)")
            return "done-skip"
        raw_pid = st.get("pid")
        if not isinstance(raw_pid, int) or raw_pid <= 0:
            self._alert(f"reap unverifiable for {sf.name}: no numeric pid ({reason})")
            self._log(f"reap unverifiable {sf.name}: no-pid ({reason})")
            return "unverifiable"
        pid = raw_pid
        recorded = st.get("pid_started")
        try:
            current = _pid_lstart(pid)
        except OSError as exc:
            self._alert(
                f"reap unverifiable for {sf.name} pid={pid}: lstart probe failed "
                f"({exc}) — refusing killpg ({reason})")
            self._log(f"reap unverifiable {sf.name} pid={pid}: {exc}")
            return "unverifiable"
        if current is None:
            # Process gone — slot may free; do not kill.
            self._log(f"reap pid-absent for {sf.name} pid={pid} ({reason})")
            return "pid-absent"
        if not recorded:
            # Without a recorded start stamp we cannot prove identity; never kill.
            self._alert(
                f"reap unverifiable for {sf.name} pid={pid}: no pid_started recorded "
                f"— refusing killpg ({reason})")
            self._log(f"reap unverifiable {sf.name} pid={pid} ({reason})")
            return "unverifiable"
        if str(recorded).strip() != str(current).strip():
            self._alert(
                f"reap identity-mismatch for {sf.name} pid={pid}: recorded "
                f"pid_started={recorded!r} now={current!r} — refusing killpg "
                f"({reason}); twin safe via remote self-bound")
            self._log(f"reap identity-mismatch {sf.name} pid={pid} ({reason})")
            return "identity-mismatch"
        # Identity matched — SIGTERM the process group (children are spawned with
        # start_new_session=True so pgid == pid), brief grace, then SIGKILL.
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            self._log(f"reap pid-absent (gone at SIGTERM) {sf.name} pid={pid}")
            return "pid-absent"
        except PermissionError as exc:
            self._alert(
                f"reap unverifiable for {sf.name} pid={pid}: PermissionError "
                f"at SIGTERM ({exc}) — refusing terminal reap claim ({reason})")
            self._log(f"reap unverifiable {sf.name} pid={pid}: PermissionError: {exc}")
            return "unverifiable"
        time.sleep(2.0)
        # A SIGTERM we successfully SENT (identity already verified) has
        # already committed us to "reaped": what follows is only escalation
        # to SIGKILL if the process is slow to die, and a liveness re-check
        # via `killpg(pid, 0)`/`killpg(pid, SIGKILL)` that is UNRELIABLE for
        # an already-dead zombie process group on macOS (EPERM instead of
        # ProcessLookupError once the group holds only a zombie) — that
        # ambiguity must not retroactively downgrade a signal we already
        # delivered to "unverifiable" (that value is reserved for a kill we
        # never sent at all, i.e. the SIGTERM PermissionError branch above).
        try:
            os.killpg(pid, 0)                 # still alive?
        except ProcessLookupError:
            self._record_reap_confidence(sf, confirmed=True)
            self._log(f"reaped {sf.name} pid={pid} after SIGTERM ({reason})")
            return "reaped"
        except PermissionError as exc:
            self._record_reap_confidence(sf, confirmed=False)
            self._log(f"reaped {sf.name} pid={pid} (alive-check unsignalable "
                      f"post-SIGTERM, treated as reaped: {exc}; {reason})")
            return "reaped"
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            self._record_reap_confidence(sf, confirmed=True)
            self._log(f"reaped {sf.name} pid={pid}: gone at SIGKILL ({reason})")
            return "reaped"
        except PermissionError as exc:
            self._record_reap_confidence(sf, confirmed=False)
            self._log(f"reaped {sf.name} pid={pid}: SIGKILL unsignalable "
                      f"post-SIGTERM, treated as reaped: {exc} ({reason})")
            return "reaped"
        self._record_reap_confidence(sf, confirmed=True)
        self._log(f"reaped {sf.name} pid={pid} via SIGKILL ({reason})")
        return "reaped"

    def _record_reap_confidence(self, path: Path, *, confirmed: bool) -> None:
        """Merge a reap confidence flag without erasing a verdict."""
        try:
            current = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return
        if not isinstance(current, dict):
            return
        current["reap_confirmed"] = confirmed
        try:
            self._write_json_atomic(path, current)
        except OSError:
            self._log(f"could not record reap confidence for {path.name}")
        self._append_ledger({
            "ts": _iso(_now()), "role": ROLE, "event": "instrument_error",
            "id": None, "actor": ACTOR,
            "detail": {"class": "instrument-error", "area": "gate-instrument",
                       "kind": ("reap-confidence-confirmed" if confirmed else
                                "reap-confidence-narrowed"), "path": str(path),
                       "reap_confirmed": confirmed},
        })

    # -- receipts / inquiries / state ---------------------------------------

    def _receipt_rel(self, receipt: Path | None) -> str | None:
        if receipt and receipt.exists() and self.root in receipt.parents:
            return str(receipt.relative_to(self.root))
        return str(receipt) if receipt else None

    def _write_land_receipt(self, train: Train, verdict: GateVerdict, merge_sha: str) -> str:
        path = self.root / "receipts" / train.branch.replace("/", "__") / f"land-{merge_sha[:12]}.json"
        body = {
            "kind": "land-receipt", "train": train.branch, "base": train.base,
            "tip": train.tip, "merge_sha": merge_sha, "at": _iso(_now()),
            "by": ACTOR, "members": train.members, "paths": train.paths,
            "gate": {"result": verdict.result, "exit_code": verdict.exit_code,
                     "slug": verdict.slug, "duration_s": round(verdict.duration_s, 1),
                     "receipt": str(verdict.receipt) if verdict.receipt else None,
                     "stdout_tail": verdict.stdout_tail}}
        if train.chained:
            # Chain provenance, present ONLY on a chained train. For one of those
            # `base` is its PARENT's tip and `merge_sha` is the whole prefix's
            # tip, so without this an auditor cannot tell from the receipt alone
            # which tree the gate graded or what else rode in on the same
            # fast-forward. An UNCHAINED train's receipt keeps the pre-chain key
            # set exactly, because "<=cap is unchanged" has to be true of the
            # bytes a later reader parses (round-4 review, BLOCKER).
            body["chain"] = {"root": train.root, "parent": train.parent,
                             "index": train.chain_index}
        self._write_json_atomic(path, body)
        return str(path.relative_to(self.root))

    def _raise_inquiry(self, train: Train, verdict: GateVerdict) -> None:
        payload = {
            "area": "tooling",
            "observation": (f"merge-gate refused train {train.branch} ({train.tip[:12]}) "
                            f"with '{verdict.slug}' at exit {verdict.exit_code}: {verdict.reason}"),
            "why_not_a_fix": ("The failure is in the gate's environment, not the candidate's "
                              "code; refusing the members would send the next agent to debug "
                              "the wrong thing."),
        }
        # id is the payload hash so a repeat of the SAME instrument failure does
        # not mint a brand-new inquiry every tick and flood the queue.
        from bridge.canonical import content_id
        ident = content_id(payload)
        target = self.root / "inquiries" / f"{_stem(ident)}.json"
        if target.exists():
            self._log(f"inquiry {_short(ident)} already exists (same payload) — not duplicated")
            return
        ev = {"claim": f"merge-gate exited {verdict.exit_code} with slug {verdict.slug}",
              "verified_by": "execution",
              "command": f"offload gate --candidate {train.branch} --tip {train.tip}",
              "exit_code": verdict.exit_code}
        if verdict.receipt:
            ev["receipt"] = str(verdict.receipt)
        self._write_json_atomic(target, {
            "contract": "v1.1", "id": ident, "kind": "inquiry",
            "title": f"gate instrument error: {verdict.slug} on {train.branch}"[:200],
            "created_at": _iso(_now()),
            "producer": {"role": ROLE, "actor": ACTOR}, "payload": payload,
            "evidence": [ev]})
        self._append_ledger({"ts": _iso(_now()), "role": ROLE, "event": "inquired",
                             "id": ident, "actor": ACTOR,
                             "detail": {"area": "tooling", "about": train.branch}})

    def _ledger_has_event(self, ident: str, event: str) -> bool:
        """Whether the ledger already carries `event` for `ident`. Tolerant of
        torn/garbage lines, same as every other ledger reader here.

        Shared `iter_events`, not `json.loads`: this guards a HEALING append, so
        an event hidden behind another on the same physical line reads as absent
        and the heal is written a second time. False here is the favourable
        direction, which is the direction this repo refuses."""
        path = self.root / "ledger.jsonl"
        if not path.exists():
            return False
        for ev in iter_events(path.read_text(encoding="utf-8", errors="replace")):
            if ev.get("event") == event and ev.get("id") == ident:
                return True
        return False

    def _note_ineligible(self, ident: str, reason_class: str, *, branch: str | None,
                         symptom: str, remedy: str) -> None:
        """File ONE deduped finding for a candidate the loader must skip every tick.

        A silent 60s skip loop starved the daemon to zero for ~12h on 2026-08-10
        (0 of 155 candidates eligible; the one live candidate was skipped 700+
        times for a missing top-level head_sha, visible only in a log nobody
        drains). An ineligible candidate must announce itself once, into the
        queue the loops actually read.

        The payload is deterministic per (candidate, reason) so the content id
        dedups repeats across ticks; volatile specifics (current branch tip)
        ride in evidence, which is outside the id. Presence in findings/,
        rejected/, or parked/ suppresses re-filing (the integrity-check idiom).
        """
        payload = {
            "symptom": (f"gate-ineligible candidate {ident}: {symptom}. The gate loop "
                        "skips it every tick; until repaired it can never land."),
            "candidate": ident,
            "reason_class": reason_class,
            "remedy": remedy,
        }
        fid = content_id(payload)
        stem = _stem(fid)
        for d in ("rejected", "parked"):
            if (self.root / d / f"{stem}.json").exists():
                return                       # a disposition is never overridden
        target = self.root / "findings" / f"{stem}.json"
        if target.exists():
            # The artifact write and the ledger append are two steps; a crash
            # between them leaves the finding on disk but invisible to every
            # ledger reader (queue.json rebuilds from events, not files — the
            # favourable-absence shape). Presence therefore heals the missing
            # event, not just suppresses the re-file.
            if not self._ledger_has_event(fid, "found"):
                self._append_ledger({"ts": _iso(_now()), "role": ROLE,
                                     "event": "found", "id": fid, "actor": ACTOR,
                                     "detail": {"candidate": ident,
                                                "reason_class": reason_class,
                                                "healed": True}})
                self._log(f"healed missing found event for {_short(fid)}")
            return
        evidence: list[dict] = [{
            "claim": f"load_candidates skipped {ident} with reason {reason_class}",
            "verified_by": "execution",
            "command": "gate_loop.py --once (candidate loading)",
            "exit_code": 0,
        }]
        if branch:
            rc_b, branch_tip, _ = git(self.repo, "rev-parse", "--verify",
                                      f"{branch}^{{commit}}")
            if rc_b == 0:
                evidence.append({
                    "claim": f"branch {branch} currently resolves to {branch_tip.strip()}",
                    "verified_by": "execution",
                    "command": f"git rev-parse --verify {branch}^{{commit}}",
                    "exit_code": 0,
                })
        self._write_json_atomic(self.root / "findings" / f"{stem}.json", {
            "contract": "v1.1", "id": fid, "kind": "finding",
            "title": f"gate-ineligible candidate ({reason_class}): {_short(ident)}"[:200],
            "created_at": _iso(_now()),
            "producer": {"role": ROLE, "actor": ACTOR},
            "payload": payload,
            "evidence": evidence})
        self._append_ledger({"ts": _iso(_now()), "role": ROLE, "event": "found",
                             "id": fid, "actor": ACTOR,
                             "detail": {"candidate": ident,
                                        "reason_class": reason_class}})
        self._log(f"filed ineligibility finding {_short(fid)} for {_short(ident)} "
                  f"({reason_class})")

    def _running_gate_count(self, *, now: float | None = None) -> int:
        """How many gates are in flight — thin delegate of `_read_gate_leases`.

        Expired-present still counts (I4'): a passed deadline does not free the
        slot; only the verified reap does.
        """
        return self._read_gate_leases(now=now).running

    def _gate_state_done(self, train: Train, disposition: str, note: str) -> None:
        """Stamp the terminal disposition onto the gate-state file so the same
        train is not re-processed next tick before its members leave `candidates/`.
        (Kept, not deleted, so an operator can see what happened.)"""
        sf = gate_state_path(self.root, train)
        try:
            st = json.loads(sf.read_text()) if sf.exists() else {}
        except (OSError, json.JSONDecodeError):
            st = {}
        st.update({"state": "closed", "disposition": disposition,
                   "closed_at": _iso(_now())})
        if disposition.startswith("reap-"):
            # State-file carriers use one field name for the reap outcome; the
            # orphan sweep below writes this same key.
            st["reap"] = note
            st.pop("note", None)
        else:
            st["note"] = note
        self._write_json_atomic(sf, st)

    # -- one tick ------------------------------------------------------------

    def _mark_tick_degraded(self, reason: str) -> None:
        if not getattr(self, "_tick_degraded_reason", None):
            self._tick_degraded_reason = reason

    def _publish_lander_heartbeat(self, status: str, reason: str | None = None) -> None:
        body = {
            "repo": self.repo_key,
            "last_tick_ts": _iso(_now()),
            "status": status,
            "pid": os.getpid(),
        }
        if reason:
            body["reason"] = reason
        self._write_json_atomic(self.root / "state" / "landers.json", body)

    def run_once(self) -> list[Outcome]:
        self._tick_degraded_reason: str | None = None
        try:
            outcomes = self._run_once()
        except Exception as exc:
            self._publish_lander_heartbeat(
                "degraded", f"{type(exc).__name__}: {exc}")
            raise
        status = "degraded" if self._tick_degraded_reason else "ok"
        self._publish_lander_heartbeat(status, self._tick_degraded_reason)
        return outcomes

    def _run_once(self) -> list[Outcome]:
        outcomes: list[Outcome] = []
        # POISON CHECK (Grok MAJOR-2). If a prior tick left local main possibly
        # diverged from origin, the daemon does ZERO further landing until a human
        # reconciles and clears the marker. Refuse at the very top — before
        # reading candidates, assembling, or dispatching anything.
        if self.poisoned():
            self._alert(
                f"POISONED: {self._poison_path()} present — local main may be diverged from "
                "origin; refusing to run any tick until a human reconciles and removes the "
                "marker. NO candidates read, NO trains landed.")
            raise DaemonPoisoned(f"halt marker present at {self._poison_path()}")
        # OPERATOR PAUSE (2026-08-09): the dashboard drops state/gate-loop-PAUSED;
        # honour it as a soft halt (no landing) a human clears via the resume button.
        if (self.root / "state" / "gate-loop-PAUSED").exists():
            self._log("gate-loop PAUSED (state/gate-loop-PAUSED present) -- no landing this tick")
            return outcomes
        # PRE-ASSEMBLY ORIGIN SYNC (finding sha256:b1edeafa). This has to run
        # HERE — after the halt checks, before ANYTHING reads main's position.
        # Everything downstream binds the base it sees at this instant:
        # `_reconcile_already_merged`, train assembly, `MintOutcome.merge_base`
        # and the twin base-pin. Syncing after any of them would hand a receipt a
        # merge base that is no longer main, which is the false GREEN the whole
        # gate exists to prevent.
        #
        # It is also the last moment where moving main races nothing: the builder
        # lock is not held yet, no train has been claimed for assembly, and the
        # whole tick already runs behind the single gate-loop lockfile. Every
        # outcome (stale view, divergence, fast-forward) is handled inside;
        # nothing here can stop the tick.
        #
        # CHURN GUARD (hold_base): if a gate is already IN FLIGHT, DEFER the
        # fast-forward instead of moving the base out from under it. Advancing
        # here would change the running train's `<train>@<tip>` gate key next
        # tick and orphan the ~25-min gate into a from-scratch re-gate — every
        # tick under a burst of merged PRs, so nothing ever lands (the livelock).
        # The fetch and BOTH fail-closed guards (divergence, local-ahead) still
        # run this tick; only the benign advance waits for a free gate slot.
        self._sync_main_from_origin(
            why="tick", hold_base=self._running_gate_count() > 0)
        rc, main_sha, _ = git(self.repo, "rev-parse", "--verify", "main")
        if rc != 0:
            self._alert("could not resolve main — nothing to do this tick (instrument)")
            self._mark_tick_degraded("could not resolve main")
            return outcomes
        main_sha = main_sha.strip()

        # INTERRUPTED-LANDING RECOVERY, before anything reads the terminal set or
        # the candidate queue: a landing that pushed but died before recording
        # its `merged` events must be completed HERE, or this tick will load its
        # members as eligible and land them a second time.
        self._recover_landing_intent(main_sha)

        # Empty active pool is edge-triggered across fresh --once processes.
        # The marker is removed when the pool recovers, re-arming the next real
        # empty-pool transition without relying on a Python instance attribute.
        empty_pool_marker = self._alert_marker_path("empty-active-twin-pool")
        if self.allow_remote_gate and len(TWIN_SPECS) == 0:
            msg = ("allow_remote_gate is on but the ACTIVE twin pool is empty "
                   "(THREELOOPS_ACTIVE_TWINS failed closed or filtered all hosts) "
                   "— remote gating disabled this tick; local-only")
            if not empty_pool_marker.exists():
                self._write_json_atomic(empty_pool_marker, {
                    "kind": "empty-active-twin-pool", "started_at": time.time(),
                })
                self._alert(msg)
                self._append_ledger({
                    "ts": _iso(_now()), "role": ROLE, "event": "instrument_error",
                    "id": None, "actor": ACTOR,
                    "detail": {
                        "class": "instrument-error", "area": "gate-instrument",
                        "kind": "empty-active-twin-pool", "reason": msg,
                        "suppressed_ticks": 0,
                    },
                })
            self._log(msg)
        else:
            empty_pool_marker.unlink(missing_ok=True)

        terminal = self._terminal_ids()
        cands = self.load_candidates(terminal)
        cands = self._reconcile_already_merged(cands, main_sha)

        # Member-aware failure isolation: a prior tick's multi-member candidate-
        # defect verdict recorded its members as ``isolated`` (durable ledger
        # backstop) + an ``iso-`` carrier instead of terminalising them. Read the
        # still-active set BEFORE assembly and force each member into a one-member
        # train (below), so an isolated member re-gates SOLO instead of
        # re-batching. An UNREADABLE carrier no longer stalls the tick (the ledger
        # holds its members and it is short-quarantined); only a readable-but-
        # MALFORMED carrier is a fail-closed veto — a clear, actionable
        # corruption. The alert is keyed per offending file, so a second,
        # independently broken record is never starved of its own alert.
        active_ids = {c.ident for c in cands}
        try:
            force_solo_ids = self._pending_isolation_ids(active_ids)
        except IsolationVeto as exc:
            marker = self._alert_marker_path("isolation-veto")
            alerted = self._read_marker(marker).get("alerted", [])
            if exc.path not in alerted:
                alerted.append(exc.path)
                self._write_json_atomic(marker, {
                    "kind": "isolation-veto", "alerted": alerted,
                    "updated_at": time.time()})
                self._alert(str(exc))
            self._mark_tick_degraded("malformed isolation carrier (fail-closed veto)")
            self._sweep_orphan_gates(())
            return outcomes
        # A fully-clean scan re-arms the per-file alert dedup so a NEW broken
        # record later re-alerts from scratch.
        self._alert_marker_path("isolation-veto").unlink(missing_ok=True)
        # Base-ancestry is INFORMATIONAL: a candidate whose base is not yet an
        # ancestor of main is forward-ported by assembly. None (unreadable) is
        # not False and is logged, never used to silently drop.
        for c in cands:
            anc = is_ancestor_of_main(self.repo, c.base_sha)
            if anc is False:
                self._log(f"{_short(c.ident)} base not an ancestor of main — forward-porting")
            elif anc is None:
                self._log(f"{_short(c.ident)} base ancestry undeterminable (instrument)")

        trains: list[Train] = []
        if cands:
            builder = self._open_builder()
            if builder is None:
                # BOTH sides of the 2026-08-10 merge kept deliberately: main's
                # degraded-tick marker AND the train-independent orphan sweep —
                # the reap must not depend on a successful assemble.
                self._mark_tick_degraded("could not open builder")
                self._sweep_orphan_gates(())
                return outcomes
            try:
                trains, excluded = assemble_trains(
                    self.repo, builder, cands, main_sha,
                    # A chain is only worth building as deep as there are boxes
                    # this daemon will actually gate on. Deliberately the BOX
                    # count and not "boxes free right now": chunk composition
                    # decides train branch names, so making it depend on live
                    # occupancy would reshuffle members between ticks and mint a
                    # fresh gate key for an unchanged tree every time a slot
                    # freed — the re-gate storm, wearing a scheduler costume.
                    chain_depth=(MAX_CONCURRENT_GATES if self.allow_remote_gate else 1),
                    force_solo_ids=force_solo_ids,
                    root=self.root)
            finally:
                self._close_builder()
            for ex in excluded:
                self._log(f"excluded {ex.get('id', '?')[:19]}: {ex.get('why')}")
                if ex.get("instrument"):
                    # An assembly refusal caused by the HOST (an unreadable
                    # parked/ index) must reach the heartbeat as degraded, not
                    # look like a quiet tick with nothing to land.
                    self._alert(f"assembly refused this tick: {ex.get('why')}")
                    self._mark_tick_degraded(str(ex.get("why"))[:120])
        else:
            self._log("no gate-eligible candidates to land this tick")

        # I8 / design M3: train-independent orphan sweep BEFORE dispatch. A
        # running state past deadline+grace with no matching assembled train
        # this tick is reaped so slots cannot leak ~daily.
        self._sweep_orphan_gates(trains)

        if not trains:
            if cands:
                self._log("no trains assembled this tick")
            return outcomes

        # 4) READ every assembled train's gate state, RESOLVE each chain, then
        #    DISPATCH whatever is left. The three phases are separate because a
        #    CHAIN's verdicts only mean anything TOGETHER: whether train k lands,
        #    is rejected, or has its verdict discarded depends on what its
        #    ancestors did, and that cannot be decided while walking the list one
        #    train at a time. Resolving before dispatching also means the slot
        #    arithmetic sees the slots this tick's landings and rejections just
        #    freed, instead of deferring a train to a box that is already idle.
        claimed_now: set = set()
        tick_readings: dict = {}
        tick_considered: list[dict] | None = None
        twin_deferrals: list[dict] = []

        def probe_once_this_tick(host: str):
            if host not in tick_readings:
                tick_readings[host] = probe_remote_load(host)
            return tick_readings[host]

        def claim_twin_once_per_tick(busy: set) -> tuple[object | None, list[dict]]:
            """Reuse the first full probe round for every later train claim."""
            nonlocal tick_considered
            if tick_considered is None:
                spec, tick_considered = pick_twin(
                    exclude=busy, probe=probe_once_this_tick,
                    readings=tick_readings)
                return spec, tick_considered
            considered = [entry for entry in tick_considered
                          if entry.get("host") not in busy]
            admitted = {entry.get("host") for entry in considered
                        if entry.get("admitted") is True}
            spec = next((candidate for candidate in TWIN_SPECS
                         if candidate.host not in busy and candidate.host in admitted), None)
            return spec, considered

        max_concurrent = MAX_CONCURRENT_GATES if self.allow_remote_gate else 1

        # -- phase 1: READ. Nothing acts yet; a train either has a verdict to
        #    resolve, has no gate state (a dispatch candidate), or is already
        #    accounted for (closed / still running).
        verdicts: dict[str, GateVerdict] = {}
        pending: list[Train] = []
        for train in trains:
            sf = gate_state_path(self.root, train)
            if not sf.exists():
                pending.append(train)
                continue
            # If a prior tick already closed this train's gate, skip it — its
            # members will have left candidates/ once they landed/rejected.
            try:
                st = json.loads(sf.read_text())
            except (OSError, json.JSONDecodeError):
                st = {}
            if st.get("state") == "closed":
                self._log(f"{train.branch} already {st.get('disposition')} — skipping")
                outcomes.append(Outcome(train, "skipped", st.get("disposition", "closed")))
                continue
            verdict = read_gate_verdict(sf)
            if verdict is None:
                self._log(f"{train.branch} @ {train.tip[:12]} still gating (waiting)")
                outcomes.append(Outcome(train, "waiting"))
                continue
            verdicts[train.branch] = verdict

        # -- phase 2: RESOLVE, chain by chain. At most ONE chain may advance
        #    `main` (single-writer), and it advances it exactly once.
        chains = chain_groups(trains)
        landed_chain: int | None = None
        dead_from: dict[int, int] = {}
        for idx, chain in enumerate(chains):
            chain_outcomes, landed, dead = self._resolve_chain(
                chain, verdicts, may_land=(landed_chain is None))
            outcomes.extend(chain_outcomes)
            if landed:
                landed_chain = idx
            if dead is not None:
                dead_from[idx] = dead

        # -- phase 3: DISPATCH the trains that still have no gate at all.
        #    The FIRST gate in flight stays local; each later one is allowed onto
        #    a twin, so every box gates in parallel. Dispatch is bounded to the
        #    boxes; extra trains wait for a slot.
        position = {t.branch: (idx, j)
                    for idx, chain in enumerate(chains)
                    for j, t in enumerate(chain)}
        in_flight = self._running_gate_count()
        for train in pending:
            idx, j = position.get(train.branch, (None, 0))
            if landed_chain is not None and idx != landed_chain:
                # `main` moved to another chain's tip, so this train's root is
                # gone: its gate would grade a tree that can no longer land.
                self._log(f"{train.branch} not dispatched: another chain landed this "
                          "tick, so it re-assembles onto the new main next tick "
                          "(single-writer serialisation)")
                outcomes.append(Outcome(train, "deferred", "main advanced this tick"))
                continue
            if idx is not None and j >= dead_from.get(idx, len(chains[idx]) + 1):
                self._log(f"{train.branch} not dispatched: an ancestor train in its "
                          "chain was rejected, so this train's root will not exist "
                          "next tick")
                outcomes.append(Outcome(train, "deferred", "chain ancestor rejected"))
                continue
            if in_flight >= max_concurrent:
                self._log(f"gate slots full ({in_flight}/{max_concurrent}) — "
                          f"{train.branch} deferred to next tick")
                outcomes.append(Outcome(train, "deferred", "gate slots full"))
                continue
            # First in-flight gate -> local; each later one claims a twin
            # via pick_twin (I7/M1): real probes + headroom floor, not a
            # bare preference walk that probes nothing.
            claim = None
            if in_flight >= 1:
                # On-disk state covers gates from EARLIER ticks; `claimed_now`
                # covers this tick. The in-tick set is tracked explicitly
                # rather than re-read from disk because that read-back
                # depends on the state file being flushed and re-globbed
                # between two iterations of this loop — and when it is not,
                # the failure is silent and doubles a box.
                #
                # Compared by MACHINE, not by name: with the pool declared in
                # configs/gate-hosts.yaml, the name a running gate was
                # dispatched under and the name the pool offers today can be
                # two aliases for one Mac.
                busy = self._busy_boxes(extra=claimed_now)
                spec, considered = claim_twin_once_per_tick(busy)
                if spec is None:
                    why = ("; ".join(
                        f"{c.get('host')}: "
                        f"{c.get('why') or c.get('reason') or 'not admitted'}"
                        for c in considered) or "no active twins")
                    self._log(
                        f"no free twin for {train.branch} — deferred "
                        f"(considered: {why})")
                    outcomes.append(Outcome(train, "deferred", "no free twin"))
                    twin_deferrals.append({"train": train.branch,
                                           "considered": considered})
                    continue
                claim = spec.host
            self.dispatch_gate(train, allow_remote=(in_flight >= 1), twin=claim)
            if claim:
                claimed_now.add(claim)
            in_flight += 1
            outcomes.append(Outcome(train, "dispatched", train.tip[:12]))
        if twin_deferrals:
            self._append_ledger({
                "ts": _iso(_now()), "role": ROLE, "event": "instrument_error",
                "id": None, "actor": ACTOR,
                "detail": {
                    "class": "instrument-error", "area": "gate-instrument",
                    "kind": "twin-pool-inadmissible", "deferrals": twin_deferrals,
                    "probed_hosts": sorted(tick_readings),
                    "reason": "no admissible free twin in this scheduling tick",
                },
            })
        return outcomes

    def _resolve_chain(self, chain: list[Train], verdicts: dict[str, GateVerdict],
                       *, may_land: bool) -> tuple[list[Outcome], bool, int | None]:
        """Turn ONE chain's gate verdicts into actions.

        Returns (outcomes, landed, dead_from) — `dead_from` being the chain index
        from which no train may be dispatched, because an ancestor was rejected
        and its root will not exist next tick.

        ATTRIBUTION IS THE WHOLE POINT, and it has exactly three cases:

          * LEADING PASSES LAND, as one fast-forward push, longest prefix first.
            Train k's gate graded `chain_root..k.tip` — its own changes on top of
            every ancestor's — so a prefix of passes is a single proven tree.

          * THE FIRST TRAIN AFTER THE PASSED PREFIX MAY BE REJECTED, and only it.
            Every one of its ancestors is GREEN, so the tree the gate refused is
            "green ancestors + this train's own changes", and the defect is
            attributable to ITS members. This is the same coarseness a
            multi-member train already carries (no bisection in the mechanical
            loop), no more.

          * EVERYTHING BEHIND A FAILED OR UNRESOLVED ANCESTOR IS DISCARDED, pass
            or fail. Its tree contains an ancestor that is not green (or not yet
            known to be), so its verdict says nothing attributable about its own
            members: rejecting them would blame them for someone else's defect,
            and landing them is impossible because their root is not `main`. They
            are logged as superseded and re-assemble next tick.

            The gate STATE FILE IS DELIBERATELY LEFT ALONE. Train tips are
            deterministic, so if the chain re-forms on the same root next tick
            the same `<train>@<tip>` key reads this same verdict and the run is
            reused rather than repeated — the discarded compute this whole
            feature exists to stop. Stamping it closed here would instead retire
            a train that never got a disposition.

        An INSTRUMENT verdict keeps its existing handling wherever it appears:
        it is a fact about the gate's environment, never about a member, and
        `on_instrument_error` is non-terminal (one bounded re-gate, one inquiry).
        """
        outcomes: list[Outcome] = []
        prefix: list[tuple[Train, GateVerdict]] = []
        boundary = 0
        while boundary < len(chain):
            verdict = verdicts.get(chain[boundary].branch)
            if verdict is None or verdict.result != "pass":
                break
            prefix.append((chain[boundary], verdict))
            boundary += 1

        landed = False
        if prefix and may_land:
            landing = self._land_prefix(prefix)
            outcomes.extend(landing)
            landed = any(o.action == "landed" for o in landing)
        elif prefix:
            # Single-writer: another chain already advanced `main` this tick.
            for train, _ in prefix:
                self._log(f"{train.branch} PASSED but another chain already landed this "
                          "tick — only ONE train may advance main; it re-assembles onto "
                          "the new main next tick (single-writer serialisation)")
                outcomes.append(Outcome(train, "skipped",
                                        "another chain landed this tick"))

        dead_from: int | None = None
        for j in range(boundary, len(chain)):
            train = chain[j]
            verdict = verdicts.get(train.branch)
            if verdict is None:
                continue                     # never gated, still gating, or closed
            if verdict.result not in ("pass", "candidate-defect"):
                outcomes.append(self.on_instrument_error(train, verdict))
                continue
            if j == boundary:
                # Every ancestor PASSED (or there are none): attributable.
                outcomes.append(self.on_candidate_defect(
                    train, verdict, [t for t, _ in prefix]))
                dead_from = j + 1
                continue
            ancestor = chain[boundary].branch
            self._log(
                f"{train.branch} @ {train.tip[:12]} gate result '{verdict.result}' "
                f"DISCARDED as superseded-by-chain-ancestor ({ancestor} did not pass "
                "this tick): its tree contains an ancestor that is not green, so the "
                "verdict is not attributable to its own members — no rejection, no "
                "landing; the members re-assemble next tick. The gate state is kept: "
                "if the chain re-forms on the same root, this exact result is reused.")
            outcomes.append(Outcome(train, "superseded",
                                    f"superseded-by-chain-ancestor {ancestor}"))
        return outcomes, landed, dead_from

    def _sweep_orphan_gates(self, trains: list | tuple) -> None:
        """Train-independent reap of expired running states with no assembled train.

        Design M3: today ~9 hand-cleared orphans prove trainless states happen
        ~daily. Any ``running`` state past ``deadline + ORPHAN_SWEEP_GRACE_S``
        whose (train, tip) is not among this tick's assembled trains gets the
        same identity-verified reap + terminal stamp + alert as the instrument
        path — so the slot AND twin free for the next tick.
        """
        gdir = self.root / "state" / "gates"
        if not gdir.is_dir():
            return
        assembled = {(t.branch, t.tip) for t in trains}
        now = time.time()
        for p in gdir.glob("*.json"):
            # `iso-*` carriers are closed records, not running leases — the
            # isolation reader owns their (short-bound) quarantine, not this sweep.
            if p.name.startswith(("receipt-", "iso-")):
                continue
            try:
                st = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                continue
            if not isinstance(st, dict) or st.get("state") != "running":
                continue
            deadline = st.get("deadline")
            if not isinstance(deadline, (int, float)):
                started_at = self._veto_started_at(
                    p, st, kind="deadlineless", now=now)
                if now <= started_at + GATE_DEADLINE_S:
                    continue
                # Do not free/quarantine a record with a child identity until
                # a verified reap has had a chance to clear it cleanly.
                if isinstance(st.get("pid"), int) and st["pid"] > 0:
                    reap = self._reap_gate_child(st, p, reason="deadlineless-sweep")
                    try:
                        st2 = json.loads(p.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                        st2 = st
                    if isinstance(st2, dict) and st2.get("state") != "running":
                        continue
                    if reap in ("reaped", "pid-absent") and isinstance(st2, dict):
                        st2.update({"state": "closed", "disposition": "deadlineless-reaped",
                                    "closed_at": _iso(_now()), "reap": reap})
                        self._write_json_atomic(p, st2)
                        self._clear_veto_marker(p)
                        continue
                self._quarantine_gate_state(p, kind="deadlineless")
                continue
            if now <= float(deadline) + ORPHAN_SWEEP_GRACE_S:
                continue
            key = (st.get("train"), st.get("tip"))
            if key in assembled:
                continue
            if (st.get("disposition") == "reap-unverifiable-parked"
                    and not st.get("self_bounded")):
                # Still drive the transition helper so its transition-only
                # contract stays pinned, but do not repeat the reap probe that
                # emitted the original refusal alert.
                self._park_unverifiable_reap(
                    p, st, str(st.get("reap") or "unverifiable"))
                continue
            reap = self._reap_gate_child(st, p, reason="orphan-sweep")
            try:
                st2 = json.loads(p.read_text(encoding="utf-8")) if p.exists() else dict(st)
            except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                st2 = dict(st)
            if not isinstance(st2, dict):
                st2 = dict(st)
            # A child can write a genuine terminal verdict during the reap
            # grace.  Preserve that re-read content rather than converting a
            # landable result into an orphan-swept closed state.
            if st2.get("state") != "running":
                self._log(f"orphan sweep preserved terminal state for {p.name}")
                continue
            if reap in ("identity-mismatch", "unverifiable") and not st.get("self_bounded"):
                self._park_unverifiable_reap(p, st2, reap)
                msg = (f"orphan gate-state retained {p.name}: reap={reap}; "
                       "legacy child remains occupied until bounded veto exit")
                marker_path = self._alert_marker_path(f"gate-veto:{p}")
                marker = self._read_marker(marker_path)
                try:
                    parked = json.loads(p.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                    parked = {}
                transitioned_at = parked.get("parked_at")
                if (isinstance(transitioned_at, (int, float))
                        and not marker.get("park_emitted")):
                    marker.update({
                        "kind": "reap-unverifiable-parked",
                        "path": str(p),
                        "veto_started_at": transitioned_at,
                        "park_emitted": True,
                    })
                    self._write_json_atomic(marker_path, marker)
                    self._alert(msg)
                    self._append_ledger({
                        "ts": _iso(_now()), "role": ROLE,
                        "event": "instrument_error", "id": None, "actor": ACTOR,
                        "detail": {
                            "class": "instrument-error", "area": "gate-instrument",
                            "kind": "orphan-gate-parked", "path": str(p),
                            "train": st.get("train"), "tip": st.get("tip"),
                            "reap": reap, "reason": msg,
                        },
                    })
                continue
            # A self-bounded child has an independently enforced deadline, so
            # an unverifiable reap can be terminalised only under that premise.
            st2.update({
                "state": "closed",
                "disposition": "orphan-swept",
                "closed_at": _iso(_now()),
                "reap": reap,
            })
            st2.pop("note", None)
            self._write_json_atomic(p, st2)
            msg = (f"orphan gate-state swept {p.name}: past deadline+"
                   f"{ORPHAN_SWEEP_GRACE_S}s with no assembled train; reap={reap}")
            self._alert(msg)
            self._append_ledger({
                "ts": _iso(_now()), "role": ROLE, "event": "instrument_error",
                "id": None, "actor": ACTOR,
                "detail": {
                    "class": "instrument-error",
                    "area": "gate-instrument",
                    "kind": "orphan-gate-swept",
                    "path": str(p),
                    "train": st.get("train"),
                    "tip": st.get("tip"),
                    "reap": reap,
                    "reason": msg,
                },
            })
            self._log(msg)


# ------------------------------------------------------- detached gate child


#: The evidence store is APPEND-ONCE per run id: `GateEvidenceStore.record` publishes
#: with `os.link` and raises `GateEvidenceExists` carrying exactly this sentence when a
#: record for that run already exists. For a merge candidate the run id IS the train tip
#: SHA, so the SECOND mint of a tip \u2014 which is precisely what the daemon's "re-gate ONCE
#: on an instrument error" path asks for \u2014 refuses, and every re-gated train parked
#: permanently at exit 127. Re-minting is NOT the remedy (that store must stay
#: write-once; it is the thing that makes a receipt history); re-VERIFYING the receipt
#: that is already on disk is.
_ALREADY_RECORDED_RE = re.compile(
    r"gate evidence already recorded for run ([0-9a-fA-F]{7,64})")

#: Re-verifying one signed file reads a few KB; it must never inherit the mint budget
#: (which covers a full allowlisted verifier run).
VERIFY_TIMEOUT_S = 120


class MintOutcome(NamedTuple):
    """How the \u00a70 candidate-bound receipt for a train tip was obtained.

    `provenance` is recorded in the gate state file so that:
      * a REUSED receipt is never silently indistinguishable from a freshly minted
        one (the operator can see which trains re-gated on existing evidence), and
      * a reuse that was REFUSED \u2014 an unverifiable receipt, or one bound to a
        different candidate \u2014 is never indistinguishable from an ordinary mint
        failure, because those two faults have different remedies.
    """

    ok: bool
    detail: str            # the receipt path when ok, else the refusal reason
    evidence_root: str
    provenance: str        # minted | reused | mint-failed | reuse-refused
    # The merge-base SHA the receipt is BOUND to (None on failure). The remote
    # path pins the twin's HEAD to exactly this value: a twin grading from its
    # own (stale) HEAD recomputes a different merge-base, refuses the signed
    # receipt as "bound to a different merge-base SHA", and charges every
    # main commit it is missing to the candidate (the false oracle-path class,
    # measured 2026-08-10: a 1-file candidate refused for "touching" ARCHI).
    merge_base: str | None = None


def _verify_candidate_receipt(py: str, gate_ws: Path, *, receipt: Path,
                              evidence_root: str, tip: str,
                              merge_base: str) -> tuple[bool, str]:
    """Re-verify an EXISTING signed receipt with the gate's OWN verifier.

    Deliberately shells out to `omniagentos.scheduler.gate_evidence verify-candidate`
    \u2014 the exact check `merge-gate.sh` runs at its mandatory `signed-receipt` step
    (signature, schema, routine/gate identity, candidate + merge-base binding,
    workspace tip, clean tree, non-zero collected checks, freshness). No second
    receipt reader is written here on purpose: a receipt this accepts is one the gate
    accepts, and a receipt this refuses is one the gate would refuse anyway.
    """
    if not receipt.exists():
        return False, f"no signed receipt at {receipt}"
    env = dict(os.environ)
    inherited = env.get("PYTHONPATH")
    # Same import wiring merge-gate.sh uses: PYTHONPATH=<gate workspace>.
    env["PYTHONPATH"] = f"{gate_ws}{os.pathsep}{inherited}" if inherited else str(gate_ws)
    try:
        p = subprocess.run(
            [py, "-m", "omniagentos.scheduler.gate_evidence", "verify-candidate",
             "--receipt", str(receipt), "--evidence-root", evidence_root,
             "--candidate-sha", tip, "--merge-base-sha", merge_base],
            cwd=str(gate_ws), capture_output=True, text=True, env=env,
            timeout=VERIFY_TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if p.returncode != 0:
        why = (p.stderr or p.stdout).strip()[:300]
        return False, why or f"verifier rc={p.returncode}"
    return True, (p.stdout or "").strip()[:300]


def _mint_candidate_receipt(gate_ws: Path, tip: str, *, timeout: int = 900) -> MintOutcome:
    """Mint the \u00a70 candidate-bound signed receipt merge-gate.sh mandates, for the
    train TIP, into the SAME durable evidence store the gate reads. THE missing step
    of the mechanical lander: without it merge-gate refuses \'signed-receipt\' at its
    first mandatory check and every assembled train is wrongly rejected.

    Returns a :class:`MintOutcome`. Safety: minting runs a real allowlisted
    verifier (PytestGateRunner) tip-bound to `tip` and signs the TRUE result -- it
    cannot fabricate a pass -- and it writes ONLY the binding record (no per-step
    receipts), so merge-gate still runs every real suite afterwards. It never merges,
    never lands, and never disturbs the pinned gate workspace HEAD.

    IDEMPOTENCE: a tip whose evidence was already recorded is not a fault. The mint
    refusal is converted into a REUSE of the receipt already on disk, and only after
    that receipt has been re-verified against this exact candidate SHA and merge base.
    An unverifiable or differently-bound receipt stays an instrument failure.
    """
    ws = str(gate_ws)
    # merge-gate.sh pinned mode: EVIDENCE_ROOT = <SHARED_ROOT>/var/gate-evidence,
    # SHARED_ROOT = gate workspace path with the trailing "-gate" removed.
    shared_root = ws[:-len("-gate")] if ws.endswith("-gate") else ws
    evidence_root = str(Path(shared_root) / "var" / "gate-evidence")
    rc, mb, err = git(gate_ws, "merge-base", "HEAD", tip)
    mb = mb.strip()
    if rc != 0 or not mb:
        return MintOutcome(False, f"merge-base(HEAD,{tip[:12]}) unreadable: {err}",
                           evidence_root, "mint-failed")
    # A workspace tip-bound to the candidate: a detached worktree AT the tip, sharing
    # the object store, so the pinned gate workspace HEAD is never disturbed.
    mint_ws = Path(shared_root) / "var" / "gate-mint" / tip[:12]
    # The one path merge-gate.sh reads its §0 receipt from; also exactly the
    # append-once record path the evidence store refuses to rewrite.
    receipt = Path(evidence_root) / "records" / "merge-gate" / f"{tip}.json"

    def _drop():
        subprocess.run(["rm", "-rf", str(mint_ws)], capture_output=True, text=True, check=False)
        subprocess.run(["git", "-C", ws, "worktree", "prune"], capture_output=True, text=True, check=False)

    _drop()
    rc, _, err = git(gate_ws, "worktree", "add", "--detach", str(mint_ws), tip)
    if rc != 0:
        _drop()
        return MintOutcome(False, f"could not open tip-bound mint worktree: {err}",
                           evidence_root, "mint-failed")
    try:
        venv_py = gate_ws / ".venv" / "bin" / "python"
        py = str(venv_py) if venv_py.exists() else sys.executable
        minter = str(gate_ws / "scripts" / "mint-merge-candidate.py")
        env = dict(os.environ)
        env["MERGE_GATE_EVIDENCE_ROOT"] = evidence_root
        env["OMNIAGENTOS_GATE_WORKSPACE"] = ws
        try:
            p = subprocess.run(
                [py, minter, "--candidate-sha", tip, "--merge-base-sha", mb,
                 "--evidence-root", evidence_root, "--workspace", str(mint_ws)],
                capture_output=True, text=True, env=env, timeout=timeout, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            return MintOutcome(False, f"{type(exc).__name__}: {exc}",
                               evidence_root, "mint-failed")
        if p.returncode != 0:
            combined = (p.stderr or p.stdout).strip()
            already = _ALREADY_RECORDED_RE.search(combined)
            if already is None or already.group(1).lower() != tip.lower():
                # Any OTHER refusal — including evidence recorded under some other
                # run id — is a genuine mint failure and stays one.
                return MintOutcome(False, f"minter rc={p.returncode}: {combined[:300]}",
                                   evidence_root, "mint-failed")
            # Write-once evidence for THIS EXACT tip already exists, so the receipt
            # the gate is about to read is already on disk. The only open question is
            # whether it is the real thing bound to this candidate — and the gate's
            # own verifier, not this daemon, answers that.
            ok, why = _verify_candidate_receipt(
                py, gate_ws, receipt=receipt, evidence_root=evidence_root,
                tip=tip, merge_base=mb)
            if not ok:
                return MintOutcome(
                    False,
                    f"signed receipt already recorded for {tip[:12]} but it cannot be "
                    f"reused: {why}",
                    evidence_root, "reuse-refused")
            return MintOutcome(True, str(receipt), evidence_root, "reused", mb)
        if not receipt.exists():
            return MintOutcome(False, "minter reported ok but the signed receipt file is absent",
                               evidence_root, "mint-failed")
        return MintOutcome(True, str(receipt), evidence_root, "minted", mb)
    finally:
        _drop()


def run_gate_child(argv: list[str]) -> int:
    """The DETACHED child: run the gate, capture the outcome, and atomically
    overwrite the state file with the result. Spawned with a new session so the
    gate outlives the tick. It NEVER classifies — it only records
    rc/stdout/stderr/receipt for the daemon's `read_gate_verdict` (which delegates
    to classify_gate) to judge on a later tick.

    Three modes:
      * direct  — run the PINNED workspace's own merge-gate.sh
        (`$GATE_WS/scripts/merge-gate.sh`), cwd=$GATE_WS, so the gate's
        self-identity guard sees a matching judge (no `stale-gate-script`).
      * remote  — pin the exact tip on the twin, forward the pre-gate signed
        receipt, gate from the twin's pinned workspace, and sync evidence home.
      * offload — compatibility path for older callers; the daemon itself no
        longer uses adaptive offload because its second slot must stay remote.
    """
    ap = argparse.ArgumentParser(prog="gate_loop run-gate")
    ap.add_argument("--mode", choices=["direct", "remote", "offload"],
                    default="offload")
    ap.add_argument("--state-file", required=True, type=Path)
    ap.add_argument("--offload", default=DEFAULT_OFFLOAD)
    ap.add_argument("--gate-workspace", default=None)
    ap.add_argument("--twin", default=TWIN_HOST,
                    help="which twin grades this run; its workspace and evidence "
                         "root travel with it (they differ per box)")
    ap.add_argument("--local-repo", default=None)
    ap.add_argument("--candidate", required=True)
    ap.add_argument("--tip", required=True)
    ap.add_argument("--repo-key", default="omniagentos")
    ap.add_argument("--receipt", required=True)
    ap.add_argument("--target", action="append", default=[])
    ap.add_argument("--bound-test", action="append", default=[], dest="bound_test",
                    help="a pytest node id this train's landing would close (repeatable)")
    args = ap.parse_args(argv)
    bound_tests = [n for n in (_valid_node_id(v) for v in args.bound_test) if n]

    # Read the lease once, but calculate each timeout at its point of use below:
    # minting/pinning/preflight can consume most of a lease before a command is
    # actually handed to the local or remote executor.
    try:
        dispatch_state = json.loads(args.state_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        dispatch_state = {}
    deadline = dispatch_state.get("deadline") if isinstance(dispatch_state, dict) else None
    def remaining_timeout(*, margin: int, minimum: int) -> int | None:
        if not isinstance(deadline, (int, float)):
            return minimum
        remaining = float(deadline) - time.time() - margin
        return int(remaining) if remaining >= minimum else None

    env = dict(os.environ)
    cwd = None
    # (remote run receipt, local evidence root, twin host, twin evidence root).
    # The host and its evidence root travel WITH the pending sync rather than
    # being re-read from a module constant down here: the sync-back runs after
    # the mode branch has closed, so a constant would fetch the evidence from
    # whichever box the default names — not the box that actually graded.
    remote_sync: tuple | None = None
    # How this gate's §0 receipt was obtained (None for the offload path, which does
    # not mint). Recorded in the state file so a REUSED receipt and a REFUSED reuse
    # are both visible to the operator instead of collapsing into "rc 127".
    mint_provenance: str | None = None
    if args.mode == "direct":
        if not args.gate_workspace:
            rc, out, err = 127, "", "direct gate requires --gate-workspace"
            cmd = None
        else:
            gate_ws = Path(args.gate_workspace)
            # SECTION-0 MINT (2026-08-09): merge-gate mandates a candidate-bound
            # signed receipt; the mechanical lander must mint it for the TRAIN TIP
            # or the gate refuses "signed-receipt" and every train is wrongly rejected.
            mint = _mint_candidate_receipt(gate_ws, args.tip)
            mint_detail, evidence_root = mint.detail, mint.evidence_root
            mint_provenance = mint.provenance
            if not mint.ok:
                # A mint failure is an INSTRUMENT fault, never a code defect. rc 127
                # -> classify_gate instrument-error -> the daemon inquires and re-gates
                # once, and NEVER rejects the candidate for it.
                rc, out, err = 127, "", f"could not mint signed candidate receipt (instrument): {mint_detail}"
                cmd = None
            else:
                cmd = local_gate_command(gate_ws, args.candidate, args.receipt,
                                         bound_tests)
                env["MERGE_GATE_PINNED"] = "1"                # determinism mode
                env["OMNIAGENTOS_GATE_WORKSPACE"] = str(gate_ws)
                env["MERGE_GATE_EVIDENCE_ROOT"] = evidence_root  # same store the mint wrote
                # I1: admission bar and actual burst share GATE_LADDER_WORKERS.
                env["MERGE_GATE_LADDER_WORKERS"] = str(effective_ladder_workers())
                cwd = str(gate_ws)
    elif args.mode == "remote":
        # Resolve the ASSIGNED twin once, here, and use its own paths for the
        # rest of this run. MW0001 runs as `youruser` and MW0002 only has a
        # `cloud` account, so the prefixes genuinely differ; pairing a host
        # with the other box's prefix gates against a path that does not exist
        # and returns a workspace error shaped exactly like a code defect.
        # An unregistered --twin is an INSTRUMENT fault (someone pointed the
        # daemon at a box the pool does not describe), so it is classified as
        # one rather than allowed to raise: an uncaught KeyError here kills the
        # child, leaves the state file 'running' until the deadline, and costs a
        # whole gate window to report what is really a one-line config error.
        try:
            _spec = twin_spec(args.twin)
        except KeyError as exc:
            _spec = None
            rc, out, err = 75, "", f"unknown twin (instrument): {exc}"
            cmd = None
        if _spec is None:
            TWIN = TWIN_WS = TWIN_EVIDENCE = None
        else:
            TWIN, TWIN_WS, TWIN_EVIDENCE = _spec.host, _spec.workspace, _spec.evidence_root

        if _spec is None:
            pass                                    # rc/err already set above
        elif not args.gate_workspace or not args.local_repo:
            rc, out, err = 127, "", (
                "remote gate requires --gate-workspace and --local-repo")
            cmd = None
        else:
            gate_ws = Path(args.gate_workspace)
            mint = _mint_candidate_receipt(gate_ws, args.tip)
            mint_detail, evidence_root = mint.detail, mint.evidence_root
            mint_provenance = mint.provenance
            if not mint.ok or not mint.merge_base:
                rc, out, err = 127, "", (
                    "could not mint signed candidate receipt for twin (instrument): "
                    + mint_detail)
                cmd = None
            else:
                # BASE PIN FIRST (2026-08-10): the signed receipt is bound to the
                # merge-base the mint measured LOCALLY. The twin recomputes its
                # merge-base and its candidate diff from its own HEAD, so a twin
                # left on a stale main refuses the receipt ("bound to a different
                # merge-base SHA") AND charges every not-yet-synced main commit
                # to the candidate (false oracle-path on ARCHI regenerations —
                # both observed live 2026-08-10T13:40Z). Detaching the twin's
                # HEAD onto the exact minted merge-base makes its judgement
                # byte-identical to the local gate's.
                #
                # Which is also why the bound-test flags are re-probed HERE
                # against the merge-base BLOB: the twin runs the script at that
                # commit, which can be older than the local pinned workspace the
                # daemon probed at dispatch. Handing an old script an unknown
                # flag refuses `unknown-flag` at exit 2 — an instrument error
                # that costs the landing, not just the closure.
                if bound_tests and not gate_supports_bound_test(
                        gate_ws, ref=mint.merge_base):
                    err_note = (
                        "bound-test flags withheld from the twin: scripts/merge-gate.sh at "
                        f"the merge-base {mint.merge_base[:12]} does not accept --bound-test")
                    print(err_note, file=sys.stderr)
                    bound_tests = []
                base_pin = pin_remote_candidate(
                    TWIN, TWIN_WS, "gate-pinned-main",
                    mint.merge_base, local_repo=args.local_repo, checkout=True)
                if not base_pin.get("ok"):
                    rc, out, err = 75, "", (
                        "twin base pin failed (instrument): " + str(base_pin.get("why")))
                    cmd = None
                    pin = None
                else:
                    pin = pin_remote_candidate(
                        TWIN, TWIN_WS, args.candidate, args.tip,
                        local_repo=args.local_repo)
                if pin is not None and not pin.get("ok"):
                    rc, out, err = 75, "", "twin pin failed (instrument): " + str(pin.get("why"))
                    cmd = None
                elif pin is not None:
                    pre = preflight_remote(
                        TWIN, workspace=TWIN_WS,
                        evidence_root=TWIN_EVIDENCE, candidate=args.candidate,
                        expected_base=mint.merge_base)
                    if not pre.get("ready"):
                        rc, out, err = 75, "", (
                            "twin preflight failed (instrument): "
                            + "; ".join(pre.get("failed") or ["unknown failure"]))
                        cmd = None
                    else:
                        remote_candidate_receipt = str(
                            Path(TWIN_EVIDENCE) / "records" / "merge-gate"
                            / f"{args.tip}.json")
                        forward = sync_forward_candidate_receipt(
                            TWIN, local_receipt=mint_detail,
                            remote_receipt=remote_candidate_receipt)
                        if not forward.get("ok"):
                            rc, out, err = 75, "", (
                                "candidate receipt sync to twin failed (instrument): "
                                + str(forward.get("why")))
                            cmd = None
                        else:
                            remote_run_receipt = str(
                                Path(TWIN_EVIDENCE) / f"gate-loop-{args.tip[:12]}.json")
                            remote_timeout_s = remaining_timeout(margin=120, minimum=300)
                            if remote_timeout_s is None:
                                rc, out, err = 75, "", (
                                    "insufficient-lease-remaining before remote dispatch "
                                    "(instrument): refusing to outlive lease")
                                cmd = None
                            else:
                                cmd = remote_gate_command(
                                    TWIN, workspace=TWIN_WS,
                                    candidate=args.candidate, receipt=remote_run_receipt,
                                    evidence_root=TWIN_EVIDENCE,
                                    extra_env={
                                        "MERGE_GATE_LADDER_WORKERS":
                                            str(effective_ladder_workers()),
                                    },
                                    bound_tests=bound_tests,
                                    timeout_s=remote_timeout_s)
                                remote_sync = (remote_run_receipt, evidence_root,
                                               TWIN, TWIN_EVIDENCE)
    else:
        cmd = [args.offload, "gate", "--candidate", args.candidate, "--tip", args.tip,
               "--repo", args.repo_key, "--receipt", args.receipt]
        for t in args.target:
            cmd += ["--target", t]

    t0 = time.monotonic()
    if cmd is None:
        pass                                             # rc/out/err already set
    else:
        # Re-evaluate at the exact local exec boundary as setup may have spent
        # most of the lease since the command was assembled.
        child_timeout_s = remaining_timeout(margin=60, minimum=GATE_CHILD_TIMEOUT_S)
        if child_timeout_s is None:
            rc, out, err = 75, "", (
                "insufficient-lease-remaining before local dispatch "
                "(instrument): refusing to outlive lease")
        else:
            try:
                # I8: local child self-bounds inside its remaining lease. On timeout,
                # write the normal terminal state (rc 124) — never raise through.
                # The deadline+reap path remains the outer mechanism; this is the
                # belt-and-braces so "expired-present" is rare rather than primary.
                p = subprocess.run(
                    cmd, cwd=cwd, env=env, capture_output=True,
                    text=True, check=False, timeout=child_timeout_s)
                rc, out, err = p.returncode, p.stdout, p.stderr
            except subprocess.TimeoutExpired as exc:
                out_raw = exc.stdout or ""
                err_raw = exc.stderr or ""
                if isinstance(out_raw, bytes):
                    out_raw = out_raw.decode("utf-8", errors="replace")
                if isinstance(err_raw, bytes):
                    err_raw = err_raw.decode("utf-8", errors="replace")
                rc, out = 124, out_raw
                err = (
                    (err_raw + "\n") if err_raw else ""
                ) + (
                    f"gate subprocess exceeded local timeout {child_timeout_s}s "
                    f"(lease-derived); child self-bounded — instrument timeout, "
                    f"says nothing about the code"
                )
            except (OSError, subprocess.SubprocessError) as exc:
                rc, out, err = 127, "", f"could not run gate ({args.mode}): {type(exc).__name__}: {exc}"
    if remote_sync is not None:
        remote_run_receipt, local_evidence_root, sync_twin, sync_evidence = remote_sync
        sync = sync_back_evidence(
            sync_twin, remote_receipt=remote_run_receipt,
            local_receipt=args.receipt,
            remote_records_dir=f"{sync_evidence}/records/merge-gate",
            local_records_dir=f"{local_evidence_root}/records/merge-gate")
        if not sync.get("ok"):
            err = ((err + "\n") if err else "") + (
                "twin evidence sync-back failed (instrument): " + str(sync.get("why")))
            rc = 75
    dur = round(time.monotonic() - t0, 1)
    receipt = args.receipt if Path(args.receipt).exists() else None
    state = {"state": "done", "rc": rc, "stdout": out, "stderr": err,
             "receipt": receipt, "duration_s": dur, "candidate": args.candidate,
             "tip": args.tip, "mode": args.mode, "mint": mint_provenance,
             "bound_tests": bound_tests, "finished_at": _iso(_now())}
    if "insufficient-lease-remaining" in err:
        state["instrument_state"] = "insufficient-lease-remaining"
    sf = Path(args.state_file)
    sf.parent.mkdir(parents=True, exist_ok=True)
    tmp = sf.with_suffix(sf.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, sf)
    return 0


# --------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "run-gate":
        return run_gate_child(argv[1:])

    ap = argparse.ArgumentParser(
        description="Mechanical gate loop daemon — the deterministic lander.")
    ap.add_argument("--loops-root", required=True, type=Path)
    ap.add_argument("--repo", type=Path, default=None,
                    help="target repo being landed (default: <loops-root>/../..)")
    ap.add_argument("--gate-workspace", type=Path, default=None,
                    help="pinned one-writer gate workspace to re-pin after a land")
    ap.add_argument("--offload", default=DEFAULT_OFFLOAD)
    ap.add_argument("--repo-key", default="omniagentos")
    ap.add_argument("--remote", default="origin")
    ap.add_argument("--no-push", action="store_true")
    ap.add_argument("--allow-remote-gate", action="store_true",
                    default=os.environ.get("THREELOOPS_ALLOW_REMOTE_GATE", "") == "1")
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args(argv)

    root: Path = args.loops_root.resolve()
    if not root.is_dir():
        print(f"no such queue: {root}", file=sys.stderr)
        return 2
    repo = (args.repo or root.parent.parent).resolve()
    if not (repo / ".git").exists():
        print(f"not a git repo: {repo} (pass --repo)", file=sys.stderr)
        return 2
    gate_ws = args.gate_workspace.resolve() if args.gate_workspace else None

    lock_path = root / "locks" / "gate-loop.lock"
    with Lock(lock_path):
        while True:
            loop = GateLoop(root, repo, gate_ws=gate_ws, offload_bin=args.offload,
                            repo_key=args.repo_key, remote=args.remote,
                            allow_remote_gate=args.allow_remote_gate,
                            push=not args.no_push)
            try:
                outcomes = loop.run_once()
            except DaemonPoisoned as exc:
                # Local main may be diverged from origin. Exit NON-ZERO and stay
                # out until a human clears the marker; each launchd relaunch will
                # re-refuse at run_once's top and land nothing.
                print(json.dumps({"poisoned": True, "error": str(exc)}), file=sys.stderr)
                for a in loop.alerts:
                    print(f"  ALERT: {a}", file=sys.stderr)
                return 3
            except Exception as exc:  # a lander crash is an instrument fact, never a verdict
                print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}), file=sys.stderr)
                for a in loop.alerts:
                    print(f"  ALERT: {a}", file=sys.stderr)
                return 1
            print(json.dumps({"at": _iso(_now()),
                              "outcomes": [{"train": o.train.branch, "action": o.action,
                                            "detail": o.detail} for o in outcomes]},
                             indent=2))
            for ln in loop.lines:
                print(f"  {ln}")
            for a in loop.alerts:
                print(f"  ALERT: {a}")
            if args.once:
                return 0
            time.sleep(TICK_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
