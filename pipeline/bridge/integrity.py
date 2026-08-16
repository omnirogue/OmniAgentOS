#!/usr/bin/env python3
"""Loop integrity checks — mechanical assertions about the system's own mechanisms.

NOT an agent. An agent asked "is the system healthy?" produces prose that can be
confidently wrong in the same way the thing it judges is wrong. An assertion
cannot rationalise. Anything needing judgement about WHY belongs in an inquiry,
which Planning acts on — this file never proposes process changes.

Why it exists: a mechanism cannot verify itself. The governor could not notice
its own database was empty, because empty read as thrift. Measured instances on
this estate of a guard passing while protecting nothing: a hard-stop gate that
never ran for `doc` targets, a budget cap that read $0.00 forever, an ISSUE-8
fail-closed branch made unreachable by its own writer, 8 accounts reported
expired by a broken symlink farm, "13 live accounts" that were one identity.

Categories and cadence (deliberately not one cadence — see --category):

  liveness      every 15m   is anything happening? GOVERNOR-CONDITIONED, because
                            a stalled loop is usually CORRECT here (backpressure,
                            caps, terminal errors). Unconditioned liveness is an
                            alert-fatigue generator, and ALERTS.md is where the
                            whole fail-safe chain terminates.
  invariants    hourly      cheap ledger truths admission does not check
  reachability  daily       does the guard actually fire? Probes run against a
                            SCRATCH queue — a corrupt budget.json must never
                            touch the live one.
  absence       daily       a guard with zero hits AND a demonstrably non-empty
                            trigger population. Without the population clause
                            this flags every healthy rare-event guard and gets
                            ignored within a week.

Failures route through existing channels, never a new one:
  broken mechanism      -> finding   (Repair)
  possibly unreachable  -> inquiry   (Planning)
  dead/stalled loop     -> ALERTS.md (operator), once per EPISODE

Nothing here halts the loops. This is new code with no track record, and a false
positive that halts a working system is a self-inflicted outage.

Usage:
    integrity.py --loops-root var/loopqueue --category liveness [--apply]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
# THE shared implementation of the load-ceiling default. Imported rather than
# re-derived: a second copy of "what does an absent load_avg_1m_max mean" is
# how this file came to disagree with governor.py and integration.py in the
# first place.
import governor as _governor
from canonical import content_id  # noqa: E402
from ledger_read import BLANK, RECOVERED, EventList, parse_events  # noqa: E402
from ledger_read import OK as LEDGER_OK
from ledger_write import append_event  # noqa: E402


def _event_detail(ev: dict) -> dict:
    """`detail` as a dict, whatever the record actually holds.

    `ev.get("detail") or {}` is NOT enough: a non-empty string is truthy, so it
    survives the `or` and then has no `.get`. That exact shape took the queue
    publisher down estate-wide on 2026-08-09 when a producer appended `detail`
    as a string. `ledger.jsonl` is append-only, so those records are permanent
    and every reader has to tolerate them.

    A malformed detail costs the event its detail-derived fields and NOTHING
    else. The caller still sees the event, so it still counts for status and
    WIP -- dropping it would under-count WIP and invent headroom, which is the
    dangerous direction.
    """
    d = ev.get("detail")
    return d if isinstance(d, dict) else {}


class LedgerTornError(RuntimeError):
    """The ledger is unreadable. A check that cannot read cannot pass."""


CATEGORIES = ("liveness", "invariants", "reachability", "absence")


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime | None = None) -> str:
    return (dt or _now()).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(ts) -> datetime | None:
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def read_ledger(root: Path) -> list[dict]:
    """Every event. Tolerates a torn final line — a crash can leave one, and
    aborting the read would stop integrity checking exactly when it matters."""
    p = root / "ledger.jsonl"
    if not p.exists():
        return []
    # A zero-byte ledger EXISTS but is torn. Returning [] would make every check
    # below pass on nothing — a health checker reporting green on an unreadable
    # system. Sibling of integration.py's B3; that fix did not reach here.
    if p.stat().st_size == 0:
        raise LedgerTornError(f"{p} exists but is zero bytes")
    # Every check below does ev.get(...). This used to append whatever
    # json.loads returned, so a valid-JSON non-object line (null/123/"s"/[])
    # reached the checks and raised AttributeError, taking the auditor down.
    #
    # It COUNTS what it drops rather than silently shortening the list: the
    # argument is identical to the zero-byte guard above. A health checker that
    # quietly discards part of the ledger reports green on a system it did not
    # fully read — favourable absence, the shape this whole module exists to
    # catch. `discarded` is what lets a caller tell the two apart.
    #
    # One line can carry more than one event (2026-08-10, live ledger line
    # 3323), so this keeps EVERY object the decoder recovered and counts the
    # line only if something on it was still unreadable. The auditor must not
    # grade a ledger it read short, and it must not manufacture a shortfall
    # either — a line that fully recovers is not a discard.
    out = EventList()
    for raw_line in re.split(rb"\r\n?|\n", p.read_bytes()):
        line = raw_line.decode("utf-8", errors="replace")
        evs, status = parse_events(line)
        out.extend(evs)
        if status == RECOVERED:
            # All events on the line are whole — this is not a partial read,
            # so it must not count as `discarded` (see EventList.recovered).
            # It is still a genuine corruption signal (a writer dropped a
            # newline); `assert_whole_ledger` reports it separately, below.
            out.recovered += 1
            continue
        if status not in (LEDGER_OK, BLANK):
            out.discarded += 1
    return out


def assert_ledger_line_split_parity(root: Path, r: Result) -> None:
    """Flag decoded text whose universal line boundaries exceed physical lines."""
    p = root / "ledger.jsonl"
    if not p.exists() or p.stat().st_size == 0:
        return
    raw_bytes = p.read_bytes()
    raw = raw_bytes.decode("utf-8", errors="replace")
    r.check()
    if len(raw.splitlines()) != len(raw_bytes.splitlines()):
        r.fail(
            "ledger.decoded_and_physical_line_counts_match",
            "ledger.jsonl contains a Unicode line separator that str.splitlines() "
            "would mistake for a physical event boundary; ledger readers must split "
            "bytes before decoding.",
            [f"decoded_lines={len(raw.splitlines())}",
             f"physical_lines={len(raw_bytes.splitlines())}"],
            scope="ledger",
        )


def assert_whole_ledger(events: list, r: Result) -> None:
    """Refuse to report on a ledger that was only partly read.

    `read_ledger` counts the lines it drops, but a counter nothing consults is
    the "built, tested, never wired" defect this repo's reachability gate
    exists to catch — and it would be that defect inside the auditor whose job
    is to catch it. So every category calls this.

    Every category, not just one: `--category liveness|invariants|absence` are
    three SEPARATE launchd processes, each doing its own read. Wiring this into
    one of them would leave the other two silently grading a short ledger.

    Two separate dedup traps, both measured rather than assumed:

    1. The detail string carries NO measurement. `emit` hashes assertion+detail
       into the finding's content id, so a count in the detail mints a new id
       on every run whose count differed — dedup dies and the queue floods.
       The count goes in the refs, which are not hashed.
    2. It is scoped to "ledger", not to the category. `emit` puts the category
       in source_ref, which IS hashed, so without the override the three
       category processes mint THREE findings for one condition. Verified by
       running all three and counting artifacts: 3 before the override, 1
       after, and still 1 on a second sweep.
    """
    r.check()
    discarded = getattr(events, "discarded", 0)
    if discarded:
        r.fail(
            "ledger.every_line_is_an_object",
            "ledger.jsonl carries lines that are not JSON objects; they were "
            "skipped, so every check in this run graded less than the whole "
            "ledger. An auditor reporting green on a partial read is the "
            "favourable-absence shape it exists to refuse.",
            [f"discarded_lines={discarded}", "see bridge/ledger_read.parse_events"],
            scope="ledger",   # substrate, not category: all three must converge
        )
    # A spliced line (2026-08-10, cross-lineage review, GPT-5.6-Sol, BLOCKER)
    # is NOT a partial read — every event on it was recovered — so it must
    # not be folded into the `discarded` finding above, and it must not read
    # as clean either. `RECOVERED` reports it under its own assertion so the
    # auditor keeps DISTINGUISHING "read short" from "read whole but the file
    # itself is corrupt": the whole point is that the writer bug (a short
    # `os.write` that dropped a newline) that produced the splice must stay
    # observable, or it can recur silently forever on an append-only ledger.
    recovered = getattr(events, "recovered", 0)
    if recovered:
        r.fail(
            "ledger.line_carries_spliced_events",
            "ledger.jsonl carries one or more lines where more than one "
            "complete JSON object was concatenated together (a writer "
            "dropped a newline mid-append). Every event on those lines was "
            "recovered and none was lost, but the framing loss itself must "
            "stay visible or the writer bug that caused it is invisible "
            "forever on an append-only file.",
            [f"recovered_lines={recovered}", "see bridge/ledger_read.parse_events"],
            scope="ledger",   # substrate, not category: all three must converge
        )


class Result:
    def __init__(self) -> None:
        self.failures: list[dict] = []   # -> findings
        self.suspicions: list[dict] = [] # -> inquiries
        self.alerts: list[str] = []      # -> ALERTS.md
        # Alert TEXT stays list[str] (see `alert`) — pipeline/tests/
        # test_falsy_zero_governor_knobs.py does substring membership over
        # r.alerts elements ('silent' in a); a dict/tuple element there
        # would make those assertions vacuously true. The stable key that
        # names the CONDITION (never the measurement) lives in this side
        # map instead, keyed by the alert text.
        self.alert_keys: dict[str, str] = {}
        self.checks_run = 0

    def check(self) -> None:
        self.checks_run += 1

    def fail(self, assertion: str, detail: str, refs: list[str] | None = None,
             scope: str | None = None) -> None:
        # payload carries the ASSERTION NAME and a stable repro — never the
        # measurement. A measurement in the payload changes the content hash
        # every run, so dedup dies and the queue floods hourly, forever.
        #
        # `scope` overrides the category in the artifact's source_ref, and so
        # in its content id. Default None keeps the category, which is right
        # for an assertion ABOUT that category. Some assertions are about a
        # shared SUBSTRATE instead — the ledger file itself — and are made by
        # all three category processes independently; without an override each
        # one mints a separate artifact for a single condition. Measured, not
        # assumed: all three categories emitting produced 3 distinct findings.
        self.failures.append({"assertion": assertion, "detail": detail,
                              "evidence_refs": refs or [], "scope": scope})

    def suspect(self, area: str, observation: str, unknown: str, refs=None) -> None:
        self.suspicions.append({"area": area, "observation": observation,
                                "why_not_a_fix": unknown, "evidence_refs": refs or []})

    def alert(self, msg: str, key: str | None = None) -> None:
        self.alerts.append(msg)
        self.alert_keys[msg] = key or msg


# ---------------------------------------------------------------- liveness


def check_liveness(root: Path, r: Result) -> None:
    """Governor-conditioned. 'Silent' is only suspicious when the governor says
    work SHOULD be happening — a loop correctly stopped on backpressure or a
    spend cap is healthy, and alerting on it teaches the operator to ignore
    ALERTS.md."""
    budget = {}
    try:
        budget = json.loads((root / "state" / "budget.json").read_text())
    except (OSError, json.JSONDecodeError):
        r.check()
        r.fail("governor.budget_readable",
               "state/budget.json is absent or unparseable; every loop's "
               "fail-closed rule keys on this file")
        return

    # Should anything be running right now?
    blocked = []
    # `or 1e9` conflated "no disk reading" with "0 GB free" — a genuinely full
    # disk read as infinite headroom, so this stall was not recognised and the
    # liveness alarm below fired "silent ... while the governor reports no
    # blocking limit" during exactly the outage that legitimately silenced the
    # loops. Absent stays 1e9 (unknown must not manufacture a stall); an
    # explicit 0 is a reading and is honoured.
    #
    # A malformed-but-PRESENT value is neither: it is an instrument error, and
    # it must not silently read as "healthy". The old `or` idiom coerced ""
    # to 1e9 — a favourable value — which is the defect class this file exists
    # to catch, so it fails the check instead.
    try:
        free_gb = budget.get("disk_free_gb")
        free_gb = 1e9 if free_gb is None else _governor.finite_limit(
            free_gb, what="disk_free_gb")
        floor_gb = _governor.finite_limit(
            budget.get("disk_free_gb_min", 20), what="disk_free_gb_min")
        # `load_avg_1m_max` ABSENT must default the way every sibling reader
        # defaults it — governor.check() and integration.read_governor() both
        # fall back to the host's performance-core count. A local 1e9 here
        # meant integrity could not recognise a load stall those two WOULD
        # declare (measured: load 20 vs sibling ceiling 16), and it then fired
        # the very "silent while the governor reports no blocking limit" alarm
        # this function is supposed to suppress. Same incomplete-propagation
        # shape as the disk carrier above, one line down.
        raw_ceiling = budget.get("load_avg_1m_max")
        if raw_ceiling is None:
            raw_ceiling = _governor.perf_core_count() or os.cpu_count() or 8
        load_ceiling = _governor.finite_limit(raw_ceiling, what="load_avg_1m_max")
        load_now = _governor.finite_limit(budget.get("load_avg_1m") or 0,
                                          what="load_avg_1m")
    except (TypeError, ValueError, OverflowError) as exc:
        r.check()
        r.fail("governor.budget_values_usable",
               f"state/budget.json parses but a governor value is unusable "
               f"({type(exc).__name__}); a malformed limit must not read as "
               f"headroom", ["state/budget.json"])
        return

    if free_gb < floor_gb:
        blocked.append("disk")
    if load_now > load_ceiling:
        blocked.append("load")
    pool = budget.get("subscription", {}).get("claude_pool", {})
    if pool.get("distinct_accounts") == 0:
        blocked.append("no-accounts")

    r.check()
    stamp = _parse(budget.get("updated_at"))
    if stamp is None or (_now() - stamp) > timedelta(minutes=20):
        r.fail("governor.writes_budget",
               "budget.json has not been updated within 2 governor ticks; every "
               "loop reads a stale counter and cannot tell that it is stale",
               ["state/budget.json"])

    events = read_ledger(root)
    assert_whole_ledger(events, r)

    # PER-ROLE, not aggregate. Checking only the newest event from ANY role means
    # the GitHub bridge's `external` events keep this green while planner,
    # reviewer and implementer are all dead — a health check that cannot report
    # the most likely failure. Measured: heartbeat read failures=0 alerts=0 with
    # zero reviewer or implementer events in the ledger, ever.
    #
    # EXPECTED is keyed by the actual ledger `role` enum (schema/ledger-event.
    # schema.json: planner|reviewer|implementer|external), not the old
    # planning/repair/integration names — those never appear post-rename, so a
    # watch list keyed by them can never fire OR clear, i.e. a permanent false
    # "silent" alarm. Verified against the live ledger (var/loopqueue/ledger.
    # jsonl): planner writes `proposed`, reviewer writes `found` (findings),
    # implementer writes `admitted`/`gated`/`merged` — each category below is
    # the role that genuinely produces it.
    EXPECTED = {"planner": 6, "reviewer": 6, "implementer": 6}   # hours
    last_by_role: dict[str, datetime] = {}
    for e in events:
        ts = _parse(e.get("ts"))
        if ts and e.get("role"):
            prev = last_by_role.get(e["role"])
            if prev is None or ts > prev:
                last_by_role[e["role"]] = ts

    # `blocked` suppresses ONLY the per-role silence alerts below, because a
    # governor stall is a complete explanation for silence and alerting on it
    # is the alert-fatigue generator this function's docstring warns about.
    # It used to `return` here instead, which also skipped the queue-growth
    # check further down — and that check is not explained by a stall at all.
    # The opposite: producers still filing while nothing drains is MOST worth
    # knowing during an outage. Correcting the disk carrier above widened the
    # set of states that reach this point, so leaving the early return would
    # have traded a false silence alarm for a missed growth alarm.
    for role, hours in ({} if blocked else EXPECTED).items():
        r.check()
        # A role with NO history at all is NOT treated as absent-by-design.
        # It used to be (pre-rename: "no Integration implementation yet") but
        # that read is exactly the never-fires blindspot this rename can
        # reintroduce: right after deploy, "planner"/"reviewer"/"implementer"
        # have little or no ledger history under the NEW names (history before
        # the rename is stamped "planning"/"repair"/"integration" and is
        # deliberately not rewritten — CONTRACT.md's dated epoch). A role that
        # crashes before ever emitting its first event under the new name
        # would silently pass this check forever. Nothing in this file or
        # CONTRACT.md declares any of planner/reviewer/implementer optional,
        # so absence alarms, matching the estate's "absence never renders as
        # the favourable value" doctrine. (external stays out of EXPECTED —
        # the GitHub bridge genuinely may not fire for long stretches and is
        # not a loop this check owns.)
        if role not in last_by_role:
            r.alert(f"{role}: no events EVER recorded under this role name — "
                    f"either the loop has not run since deploy or it is dead "
                    f"on arrival; this is not an absent-by-design role",
                    key=f"role-never-seen:{role}")
            continue
        age = _now() - last_by_role[role]
        if age > timedelta(hours=hours):
            r.alert(f"{role} loop silent {int(age.total_seconds()//3600)}h "
                    f"while the governor reports no blocking limit",
                    key=f"role-silent:{role}")

    # Queue growth: producers outrunning the drain shows up here and nowhere else.
    r.check()
    six_ago = _now() - timedelta(hours=6)
    produced = sum(1 for e in events
                   if e.get("event") in ("proposed", "submitted")
                   and (_parse(e.get("ts")) or _now()) > six_ago)
    drained = sum(1 for e in events
                  if e.get("event") in ("merged", "completed", "rejected", "closed")
                  and (_parse(e.get("ts")) or _now()) > six_ago)
    if produced >= 10 and drained == 0:
        r.alert(f"{produced} items produced in 6h and NONE reached a terminal event "
                f"— the queue is growing with no drain",
                key="queue-growth-no-drain")


# ---------------------------------------------------------------- invariants


def check_invariants(root: Path, r: Result) -> None:
    """Three ledger truths admission does not check. Deliberately not a
    full-artifact schema sweep: Integration already validates at admission, so
    re-validating everything on disk is the most expensive, least informative
    check available."""
    events = read_ledger(root)
    assert_whole_ledger(events, r)

    r.check()
    terminal: dict[str, list[str]] = {}
    for e in events:
        # `completed` is terminal alongside merged/rejected (ruling D13a) and
        # `closed` is the finding-side terminal: the "exactly one terminal
        # event" guarantee (CONTRACT §11) spans all four, so any pair of them
        # on one id is a double-terminal violation this auditor must catch.
        if e.get("event") in ("merged", "completed", "rejected", "closed") and e.get("id"):
            terminal.setdefault(e["id"], []).append(e["event"])
    doubled = {k: v for k, v in terminal.items() if len(v) > 1}
    if doubled:
        r.fail("ledger.exactly_one_terminal_event",
               "ids carry more than one terminal event, breaking the guarantee "
               "queue rebuilds depend on",
               [f"ids: {', '.join(list(doubled)[:3])}"])

    r.check()
    missing_ttl = [e.get("id") for e in events
                   if e.get("event") == "rejected"
                   and not _event_detail(e).get("expires_at")]
    if missing_ttl:
        r.fail("rejection.has_expires_at",
               "rejections without expires_at are permanent bans; nothing here "
               "should be permanent",
               [f"count: {len(missing_ttl)}"])

    r.check()
    # Promoted from the 2026-08-09 outage, not from principle. A producer wrote
    # `detail` as a STRING on 11 events; every reader used `ev.get("detail") or
    # {}`, a non-empty string is truthy, and the queue publisher died with
    # AttributeError -- taking all three loops down. The readers now tolerate it,
    # but tolerance without DETECTION means the auditor reports clean ledger
    # health while a producer is actively writing corrupt records, and the next
    # occurrence is found the same way: by an outage. This asserts the shape
    # directly rather than relying on a malformed `rejected` event incidentally
    # tripping rejection.has_expires_at -- which is all that catches it today,
    # and which says nothing about a malformed `found` or `admitted` event.
    malformed_detail = [e.get("id") for e in events
                        if e.get("detail") is not None
                        and not isinstance(e.get("detail"), dict)]
    if malformed_detail:
        r.fail("ledger.detail_is_an_object",
               "ledger events carry `detail` as something other than an object; "
               "readers tolerate this but the producer is writing records that "
               "no reader can use, and the field is lost forever once appended",
               [f"count: {len(malformed_detail)}",
                f"ids: {', '.join(str(i) for i in malformed_detail[:3] if i)}"])

    r.check()
    unparked = {e["id"] for e in events if e.get("event") == "unparked" and e.get("id")}
    parked = {e["id"] for e in events if e.get("event") == "parked" and e.get("id")}
    relanded = {e["id"] for e in events if e.get("event") == "merged" and e.get("id")}
    bad = (parked & relanded) - unparked
    if bad:
        r.fail("park.unparked_precedes_merge",
               "an item that was parked reached merged with no authenticated "
               "unparked event — the approval boundary was bypassed",
               [f"ids: {', '.join(list(bad)[:3])}"])


# ---------------------------------------------------------------- absence


def check_absence(root: Path, r: Result) -> None:
    """A guard with zero hits is only suspicious when its trigger population is
    demonstrably NON-EMPTY. Without that clause this flags every healthy
    rare-event guard: no claim_expired events means no crashes, which is good
    news. The estate's own rule — 'ask what population your measurement covers'."""
    events = read_ledger(root)
    assert_whole_ledger(events, r)
    recent = [e for e in events
              if (_parse(e.get("ts")) or _now()) > _now() - timedelta(days=30)]
    if len(recent) < 50:
        r.check()
        return  # too young to reason about; silence beats a false positive

    counts: dict[str, int] = {}
    for e in recent:
        counts[e.get("event", "?")] = counts.get(e.get("event", "?"), 0) + 1

    # (guard event, the population that would trigger it)
    pairs = [
        ("rejected", "submitted", "Integration has never refused anything"),
        ("instrument_error", "gated", "no gate run has ever been classified as an instrument error"),
        ("claim_expired", "claimed", "no claim has ever been recovered from a crashed holder"),
        ("answered", "inquired", "no inquiry has ever been closed by a proposal"),
    ]
    for guard, population, phrasing in pairs:
        r.check()
        if counts.get(guard, 0) == 0 and counts.get(population, 0) >= 20:
            r.suspect(
                area="tooling",
                observation=(f"{phrasing}, across {counts[population]} {population} events "
                             f"in 30 days"),
                unknown=("whether this guard is genuinely never triggered or is "
                         "structurally unreachable — that needs a reachability "
                         "probe, which this check cannot perform"),
            )


# ---------------------------------------------------------------- reachability


def check_reachability(root: Path, r: Result) -> None:
    """Does the guard actually fire? Probes run against a SCRATCH queue, never
    the live one — testing 'does the governor stop' requires a corrupt
    budget.json, which must never touch production state."""
    with tempfile.TemporaryDirectory(prefix="integrity-") as tmp:
        scratch = Path(tmp) / "var" / "loopqueue"
        for d in ("state", "claims", "rejected", "parked", "findings"):
            (scratch / d).mkdir(parents=True, exist_ok=True)
        (scratch / "ledger.jsonl").touch()

        # 1. O_EXCL claiming actually excludes
        r.check()
        marker = scratch / "claims" / "probe.claim"
        first = second = None
        try:
            first = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            pass
        try:
            second = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            second = None
        for fd in (first, second):
            if fd is not None:
                os.close(fd)
        if first is None or second is not None:
            r.fail("claim.o_excl_excludes",
                   "two concurrent O_EXCL creates on one id did not resolve to "
                   "exactly one winner; mutual exclusion is the only "
                   "kernel-arbitrated mechanism in the contract")

        # 2. torn-tail tolerance
        r.check()
        led = scratch / "ledger.jsonl"
        led.write_text('{"ts":"2026-01-01T00:00:00Z","role":"repair","event":"submitted",'
                       '"id":"sha256:' + "a"*64 + '"}\n{"ts":"2026-01-01T00:0')
        try:
            got = read_ledger(scratch)
            if len(got) != 1:
                r.fail("ledger.tolerates_torn_tail",
                       f"a truncated final line yielded {len(got)} events, expected 1; "
                       "a crash mid-append would stop every reader")
        except Exception as exc:
            r.fail("ledger.tolerates_torn_tail",
                   f"reading a truncated ledger raised {type(exc).__name__}")

        # 3. id determinism — same payload, same id, regardless of key order
        r.check()
        a = content_id({"area": "x", "observation": "y", "why_not_a_fix": "z"})
        b = content_id({"why_not_a_fix": "z", "observation": "y", "area": "x"})
        if a != b:
            r.fail("id.canonicalisation_is_order_independent",
                   "two key orderings of one payload produced different ids; "
                   "rejected-ledger dedup silently stops working")

        # 4. the approval-tier boundary — the NEWEST guard, and guards die youngest
        r.check()
        tiers = Path(__file__).resolve().parents[1] / "DESIGN-approval-tiers.md"
        if tiers.exists():
            text = tiers.read_text()
            for needle, why in (
                ("schema/**", "schema/ is the highest-value self-governing surface"),
                ("tier_from_resolves", "provenance is the strongest mechanical tier signal"),
                ("authenticated", "the un-park boundary must not be a file deletion"),
            ):
                if needle not in text:
                    r.fail("tiers.surface_list_intact",
                           f"the approval-tier design no longer mentions '{needle}' — {why}",
                           [str(tiers)])
        else:
            r.fail("tiers.design_present",
                   "DESIGN-approval-tiers.md is missing; the approval boundary has no "
                   "written definition to check against")


# ---------------------------------------------------------------- emit


def emit(root: Path, r: Result, category: str, apply: bool) -> None:
    """Route failures through the EXISTING channels. Never a new one."""
    now = _iso()

    for f in r.failures:
        payload = {"symptom": f"integrity check failed: {f['assertion']} — {f['detail']}",
                   "source": "integrity-check",
                   "source_ref": f"integrity/{f.get('scope') or category}/{f['assertion']}"}
        art_id = content_id(payload)
        stem = art_id.replace(":", "_")
        # never re-do work whose artifact already exists
        if any((root / d / f"{stem}.json").exists() for d in ("findings", "rejected", "parked")):
            continue
        art = {"contract": "v1.1", "id": art_id, "kind": "finding",
               "title": f"integrity: {f['assertion']}"[:200], "created_at": now,
               "producer": {"role": "external", "actor": "integrity-check"},
               "evidence": [{"claim": f["detail"], "verified_by": "execution",
                             "command": f"integrity.py --category {category}", "exit_code": 1}],
               "payload": payload}
        if apply:
            _write(root / "findings" / f"{stem}.json", art)
            _append(root, {"ts": now, "role": "external", "event": "found",
                           "id": art_id, "actor": "integrity-check"})

    for s in r.suspicions:
        payload = {"area": s["area"], "observation": s["observation"],
                   "why_not_a_fix": s["why_not_a_fix"], "urgency": "normal"}
        art_id = content_id(payload)
        stem = art_id.replace(":", "_")
        if any((root / d / f"{stem}.json").exists() for d in ("inquiries", "rejected", "parked")):
            continue
        art = {"contract": "v1.1", "id": art_id, "kind": "inquiry",
               "title": f"integrity: {s['observation']}"[:200], "created_at": now,
               "producer": {"role": "external", "actor": "integrity-check"},
               "payload": payload}
        if apply:
            _write(root / "inquiries" / f"{stem}.json", art)
            _append(root, {"ts": now, "role": "external", "event": "inquired",
                           "id": art_id, "actor": "integrity-check"})

    # Alerts fire once per EPISODE (transition), not once per run. Scoped to
    # CATEGORY: four launchd jobs (liveness/invariants/reachability/absence)
    # run this one script at four different cadences against ONE
    # --loops-root, and three of them raise no alerts of their own. A
    # root-scoped file meant each quiet category's run reaped every episode
    # key it did not itself raise — i.e. every liveness episode, on every
    # invariants/reachability/absence run — which wiped liveness's alert
    # memory and re-alerted still-true conditions on the next liveness pass.
    state_path = root / "state" / f"integrity-episodes-{category}.json"
    try:
        episodes = json.loads(state_path.read_text())
    except (OSError, json.JSONDecodeError):
        episodes = {}
    # The stable key names the CONDITION, never a measurement (see
    # Result.alert / alert_keys). `.get(a, a)` falls back to the full alert
    # text — never a[:60] prefix — for any callsite that forgot a key, so an
    # un-keyed alert still dedupes on itself rather than on a truncated,
    # possibly-colliding prefix.
    live_keys = {r.alert_keys.get(a, a) for a in r.alerts}
    for a in r.alerts:
        key = r.alert_keys.get(a, a)
        if not episodes.get(key):
            if apply:
                with open(root / "ALERTS.md", "a", encoding="utf-8") as fh:
                    fh.write(f"- {now} integrity: {a}\n")
            episodes[key] = now
    for key in list(episodes):
        if key not in live_keys:
            del episodes[key]   # condition cleared — next occurrence alerts again
    if apply:
        _write(state_path, episodes)

    # Heartbeat. checks_run matters as much as the timestamp: "the checker ran
    # but checked nothing" is the empty-database pattern applied to the checker.
    # Scoped to CATEGORY (own filename, category also kept in the content so a
    # reader can cross-check path against content) — a root-scoped file meant
    # the four categories overwrote one another and liveness's 900s cadence
    # won the last-write race ~96% of the time, making the other three
    # categories' silence indistinguishable from a fresh heartbeat.
    if apply:
        _write(root / "state" / f"integrity-heartbeat-{category}.json", {
            "at": now, "category": category, "checks_run": r.checks_run,
            "failures": len(r.failures), "suspicions": len(r.suspicions),
            "alerts": len(r.alerts)})
        # Retire the legacy shared artifacts so neither can be mistaken for a
        # live instrument by a future reader. Their contents are unattributed
        # across categories, so they are deleted rather than migrated — the
        # one-time cost is exactly one re-alert per still-true condition on
        # the first post-land run of each category.
        (root / "state" / "integrity-episodes.json").unlink(missing_ok=True)
        (root / "state" / "integrity-heartbeat.json").unlink(missing_ok=True)


def _write(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    tmp.rename(path)


def _append(root: Path, event: dict) -> None:
    append_event(root, event)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--loops-root", required=True, type=Path)
    ap.add_argument("--category", required=True, choices=CATEGORIES)
    ap.add_argument("--apply", action="store_true", help="write artifacts (default: report only)")
    args = ap.parse_args()

    if not args.loops_root.is_dir():
        print(f"no such queue: {args.loops_root}", file=sys.stderr)
        return 2

    r = Result()
    could_not_run = False
    try:
        assert_ledger_line_split_parity(args.loops_root, r)
        {"liveness": check_liveness, "invariants": check_invariants,
         "absence": check_absence, "reachability": check_reachability}[args.category](args.loops_root, r)
    except LedgerTornError as exc:
        # Never report green on an unreadable ledger — that is the exact
        # favourable absence these checks exist to catch.
        r.fail("ledger.readable", str(exc))
        could_not_run = True
    emit(args.loops_root, r, args.category, args.apply)

    print(json.dumps({"category": args.category, "checks_run": r.checks_run,
                      "failures": len(r.failures), "suspicions": len(r.suspicions),
                      "alerts": len(r.alerts),
                      "mode": "applied" if args.apply else "report-only"}))
    for f in r.failures:
        print(f"  FAIL      {f['assertion']}: {f['detail'][:100]}")
    for s in r.suspicions:
        print(f"  SUSPECT   {s['observation'][:100]}")
    for a in r.alerts:
        print(f"  ALERT     {a[:100]}")
    if could_not_run:
        return 2
    return 1 if r.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
