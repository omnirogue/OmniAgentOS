"""One decoder for one ledger line, shared by every reader of `ledger.jsonl`.

Deliberately a shared module and not a fifth copy of a private helper. The
`detail`-shaped repair that preceded this one closed 10 carriers by pasting
`_event_detail` verbatim into four files, which converted one defect into a
four-member clone family — the next person to harden it has to find all four.
This estate already carries 518 clone families; the mission says the third time
you do something mechanically, mechanise it. So: one function, imported.

Two values, and the second one is now a MEASURED incident, not a latent one.

**1. A `ledger.jsonl` line that is valid JSON but is not an object must never
be handed to a reader that will call `.get` on it.** `null`, `123`,
`"a string"`, `[...]` and `true` all parse cleanly and none of them has `.get`,
so the failure is an AttributeError, not a JSONDecodeError — and every reader
here caught only `json.JSONDecodeError`. That shape stays LATENT: every in-tree
writer does `json.dumps(<dict>)`, so only an ad-hoc or type-buggy append
produces it. It is kept closed because `ledger.jsonl` is append-only and never
rewritten, so one such line would re-break every reader below, forever.

**2. A line that carries MORE THAN ONE complete event must not erase both.**
This one is LIVE and was measured on 2026-08-10, which is why the earlier
"the live ledger is CLEAN … this is LATENT, not a live incident" claim in this
docstring is now false and has been removed:

  * `var/loopqueue/ledger.jsonl` line 3323 holds TWO complete, well-formed JSON
    objects concatenated on one physical line — an ad-hoc appender wrote object
    1 without its trailing newline and the next O_APPEND writer landed object 2
    behind it. `json.loads` refuses the whole line with
    `Extra data: line 1 column 3262`.
  * The two events that decoder erased were a planner `observed` and, worse, a
    TERMINAL `rejected` for `sha256:truepending-83cc73206`. Every reader in
    this repo read that id as non-terminal WIP forever — favourable absence in
    the terminal-state replay, which is the class this estate refuses.
  * `integration.read_ledger` counted the line as `discarded`, and
    `spawn_builders._queue_state` fails closed on `view.discarded`, so ONE line
    refused ALL claim selection: `ledger state incomplete (torn_tail=False,
    discarded=1)`. The Implementer loop's whole build drain was starved by it.

So the decoder is per-LINE but not per-OBJECT: `parse_events` recovers every
complete object on the line. There is deliberately no single-object
`parse_event` left. A decoder that returns the FIRST object of a line and
reports `OK` is exactly the erasure above wearing a healthier status.

**Addendum, 2026-08-10, cross-lineage review (GPT-5.6-Sol, BLOCKER):** a fully
recovered multi-object line used to report plain `OK` — the events came back,
so nothing looked wrong. That is favourable absence one layer up from the
erasure above: the SPLICE that produced the line (a short `os.write` that
dropped a newline) is exactly the writer bug this incident is about, and
folding it into `OK` makes it invisible forever on an append-only file, no
matter how many more short writes happen after this one. `parse_events` now
reports a line of N>1 complete objects as `RECOVERED`, a status distinct from
`OK`, and `EventList.recovered` counts the LINES that needed it — separate
from `discarded`, which still means only "could not be read". Readers must
route `RECOVERED` the same direction as `OK` for admission (the events are
whole; nothing here should re-block builders) while still surfacing it
somewhere a human or the integrity auditor will see it.

`ledger.jsonl` is append-only and is never rewritten, so the repair path for
both shapes is here, in the reader, and nowhere else. (The ad-hoc writer that
dropped the newline is a separate defect; tolerating its output is not the same
as blessing it.)

`status` is returned rather than swallowed so callers can choose direction,
which is NOT the same for all of them:

  * retention and reconciliation SKIP — a non-object line carries no `id` and no
    `event`, so it can never have contributed to WIP or to a terminal state, and
    dropping it cannot under-count anything. Aborting is the harmful direction:
    it stops the daemon.
  * the AUDITOR must skip but COUNT — a health checker that silently discards
    corruption is the exact failure its own zero-byte guard exists to prevent.
  * the queue publisher distinguishes `undecodable` on the FINAL line (a torn
    tail, an expected crash artifact) from `not_an_object` (a producer bug).

A PARTIAL recovery reports both halves of the truth: the objects it recovered
AND a failure status. `{"a":1}{"b":` is a genuine interleave — event 1 is
whole and must not be dropped, and the remainder is unread garbage the caller
must still alert on. Returning `OK` there because something was recovered would
be the favourable direction; returning nothing because something was broken
would be the erasure this module exists to stop.
"""
from __future__ import annotations

import json

OK = "ok"
BLANK = "blank"
UNDECODABLE = "undecodable"      # not valid JSON at all — e.g. a torn tail
NOT_AN_OBJECT = "not_an_object"  # valid JSON, but null/number/string/list/bool
RECOVERED = "recovered"          # >1 whole object on one line — a splice, not a tear

# One decoder instance, reused. `json.loads` IS `JSONDecoder().decode`, i.e.
# `raw_decode` plus a refusal of anything trailing — and that refusal is what
# erased two live events on line 3323 of the loopqueue ledger. Calling
# `raw_decode` directly is the same C scanner at the same cost; what changes is
# that this module, not the decoder, decides what a remainder means.
_DECODER = json.JSONDecoder()

# JSON's own whitespace grammar (RFC 8259 §2), and nothing wider. Python's
# `str.isspace()` also accepts U+00A0 (NBSP) and a dozen other Unicode
# separators that are NOT valid JSON whitespace. Using it to decide where one
# object ends and the next begins let `'{"a":1}\xa0{"b":2}'` — genuinely
# corrupt JSON — read as two clean adjacent objects (cross-lineage review,
# GPT-5.6-Sol, 2026-08-10). Restricting the separator to exactly this set
# means a non-JSON-whitespace byte between two objects is left for
# `raw_decode` to trip over, which reports it the same way `X` already does
# between two objects: recovered prefix, UNDECODABLE.
_JSON_WS = " \t\r\n"


def _walk(line: str):
    """Scan one ledger line and yield every raw decoded JSON VALUE, in order.

    The single scanner both `parse_events` and `iter_events` funnel through —
    a generator, not a materializing loop, so the caller decides how much of
    the walk it turns into memory. `iter_events` consumes it directly, one
    value at a time, so a single line holding millions of objects never costs
    more than one object's worth of extra memory; `parse_events` drains it
    into a list because its callers (`read_ledger` in integration.py and
    integrity.py) need the whole line's events together with its status.

    Only JSON's own whitespace (`_JSON_WS`) is skipped between values. Raises
    ValueError/RecursionError exactly where `json.JSONDecoder.raw_decode`
    does — mid-`next()`, so whatever was already yielded before the raise is
    still whole and still real; the caller decides what the exception means.
    """
    index, end = 0, len(line)
    while True:
        while index < end and line[index] in _JSON_WS:
            index += 1
        if index >= end:
            return
        value, index = _DECODER.raw_decode(line, index)
        yield value


def parse_events(line: str) -> tuple[list[dict], str]:
    """Decode one ledger line into EVERY complete event on it.

    Returns (events, status):

      * whitespace-only line                  -> ([], BLANK)
      * one object                            -> ([obj], OK)
      * N>1 complete objects on one line      -> ([o1..oN], RECOVERED)
      * complete prefix + torn remainder      -> ([recovered...], UNDECODABLE)
      * any decoded value that is not a dict  -> ([recovered dicts], NOT_AN_OBJECT)

    Every returned element is a dict, always projected through
    `normalize_event`. Callers may therefore use the events unconditionally and
    read `status` purely as an alerting signal — the two are independent, and
    that independence is the point: a partial recovery must neither lose the
    whole events nor report clean.

    RECOVERED is deliberately NOT OK (2026-08-10, cross-lineage review,
    GPT-5.6-Sol, BLOCKER): a line carrying more than one complete object is
    proof a writer dropped a newline mid-append — the exact incident that
    produced live ledger line 3323. The events are still whole and are still
    returned, unconditionally; folding that report into a plain OK is
    favourable absence wearing a healthy status, because it makes the writer
    bug that caused the splice invisible forever on an append-only file.
    RECOVERED must never set `discarded` or `torn_tail` — the recovery
    unblocks builders (`spawn_builders._queue_state` fails closed on those
    two only), it must not re-block them.

    Status precedence, when a line manages to be wrong in two ways at once
    (`null{"a":1}{"b":`): UNDECODABLE wins over NOT_AN_OBJECT wins over
    RECOVERED. Scanning stops at the undecodable byte, so there may be
    further objects nobody can reach; "there is unread garbage here" is the
    stronger and more honest report. NOT_AN_OBJECT wins over RECOVERED
    because a non-dict value is itself a producer bug worth its own report,
    independent of how many objects were also on the line.

    The failure statuses stay distinct on purpose: a torn tail is an expected
    crash artifact, a non-object value is a producer bug, and a spliced line
    is a dropped-newline writer bug — a reader that cannot tell them apart
    reports the wrong one.

    Catches ValueError (json.JSONDecodeError is a subclass) and RecursionError,
    NOT bare Exception — swallowing everything here would hide real bugs in the
    one function six readers now funnel through.

    RecursionError is deliberate and was measured, not guessed: a line of
    ~200k nested brackets blows the C recursion limit inside the scanner. Every
    caller caught only json.JSONDecodeError, so that line crashed them at
    base_sha too — this is NOT a regression this module introduced.

    It does NOT catch MemoryError: that is a fact about the host, not about the
    line, and pretending a machine out of memory merely read a bad record is
    the favourable-absence direction this repo refuses.
    """
    events: list[dict] = []
    saw_non_object = False
    object_count = 0
    walker = _walk(line)
    while True:
        try:
            value = next(walker)
        except StopIteration:
            break
        except (ValueError, RecursionError):
            # Whatever was already recovered is still whole and still real.
            return events, UNDECODABLE
        if isinstance(value, dict):
            events.append(normalize_event(value))
            object_count += 1
        else:
            # Keep scanning: a bare `null` ahead of a real event must not cost
            # us the event, and the status below still makes the caller alert.
            saw_non_object = True
    if saw_non_object:
        return events, NOT_AN_OBJECT
    if not events:
        return events, BLANK
    if object_count > 1:
        return events, RECOVERED
    return events, OK


def normalize_event(event: dict) -> dict:
    """Project the agent-written envelope shape onto the schema shape, in place.

    The LLM loop agents hand-write ledger lines as
    `{"contract":"v1.1","at":…,"event":…,"producer":{"role":…,"actor":…}}` —
    timestamp under `at`, identity nested under `producer`. The schema shape is
    top-level `ts`/`role`/`actor`, and every keyed reader (integrity liveness
    windows, janitor retention aging, pr_reconcile ownership) reads ONLY the
    top level — measured 2026-08-10: 30 of the last 199 events were invisible
    to all of them, including every agent-written `observed`/`completed`.

    Projection only, never authority: an existing top-level key always wins,
    `producer` stays in place untouched, and nothing is invented — an event
    with no actor in either position still has none. Terminal/WIP derivation
    is unaffected either way (it keys on top-level `event`/`id`, which the
    agent shape already places correctly).
    """
    if "ts" not in event and isinstance(event.get("at"), str):
        event["ts"] = event["at"]
    producer = event.get("producer")
    if isinstance(producer, dict):
        if "role" not in event and isinstance(producer.get("role"), str):
            event["role"] = producer["role"]
        if "actor" not in event and isinstance(producer.get("actor"), str):
            event["actor"] = producer["actor"]
    return event


def iter_events(text: str):
    """Yield every well-formed event object in raw ledger text.

    For the readers that only ever want the good events and have no use for the
    distinction — retention, reconciliation, filing read-back.

    "Every" is literal and includes the ones recovered from a line that ALSO
    carried garbage. Gating the yield on `status == OK` (what this did before
    2026-08-10) throws away a whole event because something else on its line
    was broken.

    Streams through `_walk` directly rather than draining `parse_events(line)`
    into a list first (MINOR, cross-lineage review, GPT-5.6-Sol, 2026-08-10):
    a single line of ``"{}" * 10_000_000`` used to cost memory proportional to
    the object count before this generator yielded anything at all, because
    `parse_events` had to finish the whole line to hand back its list. Walking
    a line remains linear either way (measured: 100k objects in 0.031s); this
    changes only how much of that walk is resident at once, not its cost.
    """
    for line in text.splitlines():
        try:
            for value in _walk(line):
                if isinstance(value, dict):
                    yield normalize_event(value)
                # A non-dict value costs only its own status elsewhere;
                # iter_events has no status channel to report it on, so, as
                # before 2026-08-10, it is skipped here and scanning continues.
        except (ValueError, RecursionError):
            # Whatever this line already yielded is still whole and real;
            # the remainder is unread garbage this generator has no status
            # channel to report — exactly the prior parse_events()[0] shape.
            continue


class EventList(list):
    """A list of events that remembers what it refused to include, and what it
    had to splice back together.

    `discarded` exists so the auditor cannot report green on a ledger it only
    partly read. A plain list would make a quietly-shortened read
    indistinguishable from a clean one — favourable absence, the defect class
    this repo's reachability gate exists to catch.

    `recovered` is a SEPARATE counter, not folded into `discarded`
    (2026-08-10, cross-lineage review, GPT-5.6-Sol, BLOCKER): `discarded`
    means data that could NOT be read, and a spliced line's data is fully
    read — every event on it comes back. But the line was still corrupt (a
    writer dropped a newline mid-append), and that framing loss must stay
    observable or the writer bug that caused it is invisible forever on an
    append-only ledger. Counts LINES that needed splicing, not events —
    matching how `discarded` counts lines. Readers must not fold `recovered`
    into `discarded` or into `torn_tail`: `spawn_builders._queue_state` fails
    closed on exactly those two, and recovery exists to UNBLOCK builders, not
    re-block them.
    """

    discarded: int = 0
    recovered: int = 0
