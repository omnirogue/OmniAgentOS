#!/usr/bin/env python3
"""The Integration adapter. Non-agentic, and an ADAPTER — not a new gate.

Everything hard is already built and lives in the repo being worked on:

    scripts/merge-gate.sh          the gate (1,494 lines of specific mistakes)
    scripts/mint-merge-candidate.py  signed receipt minting
    var/gate-evidence/               the receipt store

This file is the missing CONSUMER: it reads `candidates/`, decides what is even
worth spending a gate on, schedules the ones that can run in parallel, invokes
the gate that already exists, **classifies what comes back**, and writes the
ledger events and `state/queue.json` that the rest of the system reads.

*******************************************************************************
* THIS VERSION NEVER PUSHES TO `main`. IT NEVER MERGES. IT NEVER FAST-FORWARDS *
* ANY CHECKOUT.  Landing stays MANUAL until this adapter has a track record.   *
*                                                                             *
* It defaults to --dry-run: it reports what it WOULD admit, gate and land, and *
* writes NOTHING — no ledger line, no rejected/ file, no queue.json, and it    *
* does not even invoke the gate (the gate mints receipts and builds worktrees, *
* which is a write).  Only --apply admits, gates, and records.  Even under     *
* --apply, a gate PASS produces a `gated` event and a "READY TO LAND" report   *
* line for a human — never a `merged` event, because writing `merged` without  *
* having merged is exactly the favourable-absence defect this estate keeps     *
* shipping.                                                                    *
*******************************************************************************

House rules it implements, each with the wound it came from:

  fail CLOSED on the governor   an absent/unparseable/STALE budget.json stops the
                                iteration. A stale counter that still parses is
                                the dangerous case: it reads as headroom.
  refuse before gating          admission is seconds, a gate is ~12 minutes. Every
                                envelope rule in CONTRACT §3 is checked first.
  paths must be COMPLETE        checked against `git diff --name-only base...branch`,
                                not taken on trust. The conflict graph is built from
                                `paths`; understating it produces a WRONG schedule,
                                not a slow one.
  classify before believing     64 of 90 refusals in recorded history here were
                                INSTRUMENT ERRORS — dirty workspace, moved base,
                                missing git identity — not candidate defects. A
                                refusal that blames the code for a dirty workspace
                                sends the next agent to debug the wrong thing.
  a defect claim needs proof    a gate that reports a candidate defect but emitted
                                no signed receipt is DOWNGRADED to instrument error.
                                Prose is not evidence.
  never re-gate an unchanged    a gate that could not run on an input (merge-gate
  input                         exit 2 = refuse-before-verdict) will not run on the
                                same unchanged input either. One symbol here drew
                                28 identical refusals (~4.5h) because the fix was
                                being written where the gate does not read.
  exactly one terminal event    the ledger is re-read immediately before every
                                terminal append. `parked` is a SUSPENSION.
  every rejection names a       detail.remedy = replan | drop | blocked, and a
  remedy and a TTL              MANDATORY expires_at. A rejection without a TTL is a
                                permanent ban and nothing here should be permanent.
  queue.json is REBUILT         by replaying ledger.jsonl. `bridge/publish_queue.py`
                                is its sole RUNTIME writer (a 300s timer, plus this
                                adapter's own --apply tick, both delegate to it);
                                `bootstrap.sh` still seeds an initial placeholder at
                                repo setup. `wip` — the only backpressure signal in
                                the system — is fail-closed on read: a missing or
                                stale key must never be treated as headroom.

Usage:
    integration.py --loops-root var/loopqueue --once            # report only (default)
    integration.py --loops-root var/loopqueue --once --apply    # admit, gate, record
    integration.py --loops-root var/loopqueue --apply           # run continuously
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bridge.canonical import content_id
from bridge.gate_host import (
    REMOTE_EVIDENCE_ROOT,
    REMOTE_GATE_WORKSPACE,
    TWIN_HOST,
    choose_gate_host,
    effective_ladder_workers,
    preflight_remote,
    remote_gate_command,
)
from bridge.governor import perf_core_count, sample_load_until_clear
from bridge.ledger_read import BLANK, RECOVERED, UNDECODABLE, EventList, parse_events
from bridge.ledger_read import OK as LEDGER_OK
from bridge.ledger_write import append_event
from bridge.remote_evidence import pin_remote_candidate, sync_back_evidence
from bridge.review_policy import approved_cross_lineage, risky_review_paths


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


# Twin gate-workspace/evidence-root defaults now live in bridge/gate_host.py,
# alongside TWIN_HOST — one place for the twin-host constants, and it keeps this
# file free of a hardcoded absolute path (tests/test_interpreter_remedy.py pins
# that constraint for bridge/integration.py).

ROLE = "implementer"
TICK_SECONDS = 300

# budget.json is written every 60s by the governor. Two ticks of slack, then it
# is STALE — and a stale budget must stop the loop, not be believed.
BUDGET_STALE_SECONDS = 1200

GATE_TIMEOUT_SECONDS = 45 * 60  # p90 gate is ~12 min; a timeout is an instrument error

# How long a refusal stays binding. Never None: a rejection with no TTL is a
# permanent ban on an idea, and nothing here should be permanent.
REJECT_TTL_DAYS = {
    "candidate-defect": 7,
    "stale-base": 1,      # rebase and resubmit is same-day work
    "instrument-error": 1,  # never used for a rejection; here so a lookup cannot KeyError
    "blocked-on-human": 30,
}

# ---------------------------------------------------------------------------
# Gate refusal taxonomy.
#
# merge-gate.sh exits 0 pass / 1 verdict-fail / 2 refuse-before-verdict. Exit 2
# is NOT synonymous with "instrument error": four of its refusal slugs are real
# candidate defects that are hoisted ahead of the twelve-minute suites. So the
# classification is by SLUG, and the slug tables below are transcribed from
# `grep -n 'refuse "' scripts/merge-gate.sh` — verified at 2026-08-07 against
# merge-gate.sh @ 1494 lines.
#
# SCOPE (Ruling #4, 2026-08-09): merge-gate.sh is an EXTERNAL gate in the
# OmniAgentOS repo with its own exit-code scheme; it is out of scope of the
# estate producer-tool convention that ratifies exit 2 = COULD NOT RUN. Its
# "refuse-before-verdict" exit 2 deliberately bundles could-not-run mechanics
# faults WITH hoisted candidate defects, which is why we classify by slug here
# rather than reading exit 2 as "could not run" wholesale. Reconciling
# merge-gate.sh's exit-2 into the ratified split is host-ops follow-up, tracked
# with AccurateGate — do not assume it here.
# ---------------------------------------------------------------------------
GATE_INSTRUMENT_SLUGS = {
    "no-interpreter", "gate-workspace-missing", "gate-workspace-not-a-checkout",
    "unpinned-workspace", "dirty-workspace", "ruff-unavailable",
    "ruff-baseline-unavailable", "unreadable-repo", "unpinned-non-main-head",
    "unknown-branch", "unresolvable-candidate", "unresolvable-merge-base",
    "unreadable-diff", "unreadable-history", "unquotable-path",
    "test-retarget-failed", "no-gate-worktree", "bad-ladder-workers",
    "reachability-probe-unusable",
    # argument-parsing refusals: this adapter built the command line, so a
    # refusal here is OUR bug, never the candidate's.
    "missing-value", "unknown-flag", "extra-argument", "missing-branch",
}
GATE_CANDIDATE_DEFECT_SLUGS = {
    "oracle-path", "secrets", "openapi-drift", "reachability",
}

# A refusal that only says what is wrong makes the producer guess, and a guess is
# usually another refusal. These end it in one round.
REMEDY_HINTS = {
    "oracle-path": (
        "ARCHI.md / ARCHI.json / contracts are MERGE-OWNED: they are regenerated on main "
        "after a merge. Drop them from the candidate; do not hand-edit them in a lane."
    ),
    "reachability": (
        "the exemption must land on main FIRST as its own `chore(gates):` commit — the gate "
        "reads devtasks/REACHABILITY-EXEMPT.txt from the checkout it RUNS IN, not from the "
        "candidate, so a branch-side line reads as absent at the symbol you just exempted."
    ),
    "secrets": (
        "configs/accounts.yaml and vault/sources/*.enc are TRACKED. Remove them from the "
        "diff; a rewrite of history is required if they were ever committed."
    ),
    "openapi-drift": "regenerate the OpenAPI artefact on the branch and recommit.",
    "dirty-workspace": (
        "INSTRUMENT, not your code: the pinned gate workspace has uncommitted or untracked "
        "files. Clean it, then re-gate the SAME input."
    ),
}

REFUSING_RE = re.compile(r"^refusing:\s*([a-z0-9-]+)", re.MULTILINE)


# --------------------------------------------------------------------- time


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(ts) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None


def _short(ident: str) -> str:
    return ident.split(":", 1)[-1][:12]


def _stem(ident: str) -> str:
    return ident.replace(":", "_", 1)


# ---------------------------------------------------------------------- git


def git(repo: Path, *args: str, timeout: int = 60) -> tuple[int, str, str]:
    """Read-only-by-convention git. Nothing in this file passes a writing verb."""
    try:
        p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                           text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, "", f"{type(exc).__name__}: {exc}"
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def is_ancestor_of_main(repo: Path, sha: str) -> bool | None:
    """True/False, or None when it could not be determined.

    None is NOT False. "I could not check" and "it is not an ancestor" are
    different claims, and collapsing them would let an unreadable repo refuse a
    perfectly good candidate as `stale-base`.

    Deliberately implemented with `rev-list --not main` rather than
    `merge-base --is-ancestor`: this estate's git guard hook intercepts commands
    containing merge verbs, and a hook-blocked probe returns a rc that looks
    exactly like "not an ancestor".
    """
    rc, _, _ = git(repo, "cat-file", "-e", f"{sha}^{{commit}}")
    if rc != 0:
        return None
    rc, out, _ = git(repo, "rev-list", "--max-count=1", sha, "--not", "main")
    if rc != 0:
        return None
    return out.strip() == ""


def changed_paths(repo: Path, base_sha: str, branch: str) -> set[str] | None:
    """Files the branch actually changes since `base_sha`. None = unreadable."""
    rc, out, _ = git(repo, "diff", "--name-only", f"{base_sha}...{branch}")
    if rc != 0:
        return None
    return {ln.strip() for ln in out.splitlines() if ln.strip()}


# ----------------------------------------------------------------- governor


@dataclass
class Governor:
    """Two classes of stop, because they have two different blast radii.

    `stops` are HARD: absent/stale/unparseable budget, disk floor, subscription
    window. Nothing in the iteration may proceed — these say the instrument or
    the resource is gone, and they do not clear on their own within an
    iteration.

    `load_stops` are SOFT and scoped to the GATE alone. Load says nothing about
    whether an envelope validates, whether a rejection has expired, or whether
    queue.json matches the ledger; those are file reads costing milliseconds and
    they are the loop's only way to make progress while the box is busy. Load
    bounds exactly one thing — starting a new 12-minute, 8-worker pytest ladder.

    That distinction is what the implementer loop's own prompt has said all along:
    "**Do not start a gate if any is true:** free disk < 20 GB · 1-min load >
    host performance-core count". The adapter was reading it as "do not start an
    ITERATION", and discarded ~9.2 minutes of cheap work (measured mean over 57
    implementer iterations) every time the box got busy.
    """

    ok: bool
    stops: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    wip_cap: int = 4
    raw: dict = field(default_factory=dict)
    load_stops: list[str] = field(default_factory=list)
    load_check: dict = field(default_factory=dict)

    @property
    def may_gate(self) -> bool:
        """Hard limits clear AND the box has room for a gate."""
        return self.ok and not self.load_stops


def read_governor(root: Path, *, live_load: bool = True) -> Governor:
    """Fail CLOSED. Absent, unparseable, or stale stops the iteration.

    The stale branch is the one that matters. An absent file is loud; a budget
    written six hours ago still parses, still reports 380 GB free, and reads to
    every consumer as live headroom.

    LOAD IS NOT READ FROM THE FILE. It is sampled here, live, at the moment the
    decision is made. budget.json is refreshed by launchd on a 300 s interval,
    so its `load_avg_1m` is up to five minutes old when this function runs, and
    over one refresh the value moves by a median of 3.3 and a p90 of 27.3.
    Checked against a 30 s ground-truth trace the file's verdict at the ceiling
    was wrong on 23% of reads: 7% blocked a box that was actually quiet, and 17%
    PASSED a box that was actually over — which is the direction that puts an
    8-worker ladder on a host at load 90 and comes back as a "candidate defect".

    `live_load=False` restores the old file-reading behaviour for tests and for
    any caller that must reason about the published telemetry rather than the
    box it is standing on.
    """
    path = root / "state" / "budget.json"
    try:
        budget = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Governor(False, [f"budget absent: {path} — every loop's fail-closed rule keys on it"])
    except (OSError, json.JSONDecodeError) as exc:
        return Governor(False, [f"budget unreadable: {type(exc).__name__}: {exc}"])
    if not isinstance(budget, dict):
        return Governor(False, ["budget.json is not a JSON object"])

    g = Governor(True, raw=budget)
    # A deliberate `wip_cap: 0` (halt every producer) is a real, distinct
    # value from "the key is absent" — `budget.get("wip_cap") or 4` used to
    # collapse them, silently ignoring an operator's zero. This is the same
    # falsy-zero shape `publish_queue.read_wip_cap()` was fixed for (F6):
    # missing/null falls back to the default; a present-but-zero value is
    # honoured as zero. The two publishers (this tick path and the 300s
    # timer) must agree on wip_cap for the same budget.json, or they stamp
    # disagreeing values into the same queue.json depending on which ran last.
    raw_cap = budget.get("wip_cap")
    g.wip_cap = 4 if raw_cap is None else int(raw_cap)

    stamp = _parse(budget.get("updated_at"))
    if stamp is None:
        g.stops.append("budget has no parseable updated_at — cannot tell fresh from rotten")
    else:
        age = (_now() - stamp).total_seconds()
        if age > BUDGET_STALE_SECONDS:
            g.stops.append(
                f"budget is STALE by {int(age)}s (limit {BUDGET_STALE_SECONDS}s) — "
                "the governor is not running; a stale counter reads as headroom")

    # Disk. An unmeasurable disk is not an empty one: a parallel run on this host
    # died with ENOSPC at 100% and the error read exactly like a code defect.
    free = budget.get("disk_free_gb")
    floor = budget.get("disk_free_gb_min", 20)
    if free is None:
        g.stops.append("disk_free_gb is null — UNKNOWN free space stops, it never passes")
    elif free < floor:
        g.stops.append(f"disk {free:.1f} GB < floor {floor} GB")

    # Load. A SOFT stop: it bounds the gate, never the iteration. See Governor.
    load_max = budget.get("load_avg_1m_max")
    if load_max is None:
        load_max = perf_core_count() or os.cpu_count() or 8
        g.notes.append(f"budget has no load_avg_1m_max — using performance-core count {load_max}")
    if live_load:
        lc = sample_load_until_clear(float(load_max))
        g.load_check = lc.as_dict()
        if lc.clear:
            g.notes.append(lc.reason)
        else:
            g.load_stops.append(lc.reason)
        stale = budget.get("load_avg_1m")
        if stale is not None and lc.load is not None and abs(stale - lc.load) > 5:
            g.notes.append(
                f"budget.json reported load {stale:.2f}; live sample is {lc.load:.2f} "
                f"(file is up to 300s old — the live sample is the one that decided)")
    else:
        load = budget.get("load_avg_1m")
        if load is None:
            g.notes.append("load_avg_1m is null — unmeasured, not idle")
        elif load > load_max:
            g.load_stops.append(f"1-min load {load:.2f} > max {load_max} (from file, not sampled)")

    # Metered dollars. CONTRACT §9 annotates null as "UNKNOWN, and UNKNOWN stops".
    # Integration makes NO metered-provider call: it runs a local bash gate. The
    # ceiling that cannot bind is reported as unknown rather than silently
    # dropped — see var/loopqueue/research/metered-meter-null-may-loops-run.md,
    # and the operator ruling still pending as sha256:d12b0f….
    for name, row in (budget.get("metered_usd") or {}).items():
        if name.startswith("//") or not isinstance(row, dict):
            continue
        spent, daily = row.get("spent_today"), row.get("daily")
        if spent is None:
            g.notes.append(f"metered[{name}].spent_today = UNKNOWN (null), cap {daily} — "
                           "does not bind: Integration makes no metered-provider call")
        elif daily is not None and spent >= daily:
            g.stops.append(f"metered[{name}] spent {spent} >= daily {daily}")

    for name, acct in ((budget.get("subscription") or {}).get("accounts") or {}).items():
        if name.startswith("//") or not isinstance(acct, dict):
            continue
        pct, pct_max = acct.get("window_pct"), acct.get("window_pct_max")
        if pct_max is None:
            continue
        if pct is None:
            g.notes.append(f"subscription[{name}].window_pct UNKNOWN (null)")
        elif pct > pct_max:
            g.stops.append(f"subscription[{name}] window {pct}% > max {pct_max}%")

    g.ok = not g.stops
    return g


# ------------------------------------------------------------------- ledger


def read_ledger(root: Path) -> tuple[list[dict], bool]:
    """(events, torn_tail). Tolerates a torn final line; never aborts the read.

    torn_tail is returned rather than swallowed: a replay built over an
    unreadable tail is a replay with a hole, and a hole in the ledger must not
    silently become a favourable queue.json.
    """
    path = root / "ledger.jsonl"
    if not path.exists():
        return [], False
    events, torn = EventList(), False
    raw_bytes = path.read_bytes()
    physical_breaks = list(re.finditer(rb"\r\n?|\n", raw_bytes))
    raw_lines = re.split(rb"\r\n?|\n", raw_bytes)
    byte_offsets = [0] + [match.end() for match in physical_breaks]
    if physical_breaks and physical_breaks[-1].end() == len(raw_bytes):
        raw_lines.pop()
        byte_offsets.pop()
    lines = [line.decode("utf-8", errors="replace") for line in raw_lines]
    damage_detail = ""
    # Converged onto the shared decoder. This function already had the
    # isinstance(dict) guard and was the ONE correct sibling for the non-object
    # shape -- but it still caught only json.JSONDecodeError, so it died on the
    # ~200k-nested-bracket line that raises RecursionError. Cross-lineage review
    # (Grok, 2026-08-09) caught that the consolidation had not reached here.
    #
    # Semantics are unchanged: blank and non-object lines are skipped and are
    # NOT torn; only an UNDECODABLE final line sets torn. A recursion-blowing
    # final line now reads as torn rather than raising, which is the alerting
    # direction, not the favourable one.
    #
    # 2026-08-10: a line may carry MORE THAN ONE complete event (live ledger
    # line 3323 — an ad-hoc append with no trailing newline, then an O_APPEND
    # behind it). Every recovered event is kept, from every line, INCLUDING a
    # line that also carried garbage — the events are whole and dropping them
    # is the favourable direction. The status is independent and unchanged, so
    # a fully recovered line is now neither torn NOR discarded, while a
    # partially recovered one still counts and still alerts.
    #
    # RECOVERED (2026-08-10, cross-lineage review, GPT-5.6-Sol, BLOCKER) is
    # its OWN branch, not folded into the OK/BLANK "nothing wrong" path and
    # not folded into `discarded` either: the line's events are all whole
    # (route it like OK for admission — do not set torn or discarded, which
    # is exactly what `spawn_builders._queue_state` fails closed on), but the
    # splice itself is a real writer-bug signal that must stay countable
    # somewhere other than `discarded`, or the bug that produced it is
    # invisible forever on an append-only file.
    for i, line in enumerate(lines):
        evs, status = parse_events(line)
        events.extend(evs)
        if status == RECOVERED:
            events.recovered += 1
            continue
        if status in (LEDGER_OK, BLANK):
            continue
        if status == UNDECODABLE and i == len(lines) - 1:
            torn = True
            if not damage_detail:
                byte_offset = byte_offsets[i] if i < len(byte_offsets) else 0
                damage_detail = (f"torn tail at line {i + 1}, byte {byte_offset}: "
                                 f"{line[:80]!r}")
        else:
            # Counted, not swallowed. Cross-lineage review (GPT-5.6-Sol,
            # 2026-08-09) showed an all-malformed ledger returned
            # (0 events, torn=False) -- byte-identical to a genuinely EMPTY
            # one -- so publish_queue wrote a healthy-looking
            # `ledger_events: 0` and nothing anywhere recorded that the read
            # had thrown the whole file away. That is favourable absence in
            # the one writer of queue.json.
            events.discarded += 1
            if not damage_detail:
                byte_offset = byte_offsets[i] if i < len(byte_offsets) else 0
                damage_detail = f"line {i + 1}, byte {byte_offset}: {line[:80]!r}"
    events.damage_detail = damage_detail
    return events, torn


@dataclass
class LedgerView:
    events: list[dict]
    torn_tail: bool
    discarded: int = 0          # ledger lines that were valid JSON but not objects
    recovered: int = 0          # ledger lines that spliced >1 whole event together
    damage_detail: str = ""      # first discarded/torn line locator
    terminal: dict[str, dict] = field(default_factory=dict)     # id -> merged|completed|rejected|closed
    parked: set[str] = field(default_factory=set)               # parked, not since unparked
    admitted: set[str] = field(default_factory=set)
    wip_entered: set[str] = field(default_factory=set)
    gate_runs: dict[str, list[dict]] = field(default_factory=dict)  # id -> gated events
    instrument_runs: dict[str, list[dict]] = field(default_factory=dict)  # id -> instrument_error
    status: dict[str, str] = field(default_factory=dict)
    kinds: dict[str, str] = field(default_factory=dict)
    titles: dict[str, str] = field(default_factory=dict)

    @classmethod
    def build(cls, root: Path) -> LedgerView:
        events, torn = read_ledger(root)
        v = cls(events, torn, getattr(events, "discarded", 0),
                 getattr(events, "recovered", 0),
                 damage_detail=getattr(events, "damage_detail", ""))
        order = {"found": "open", "inquired": "open", "proposed": "open",
                 "claimed": "claimed", "released": "open", "claim_expired": "open",
                 "submitted": "submitted", "admitted": "admitted", "gated": "gated",
                 "merged": "merged", "completed": "completed", "rejected": "rejected",
                 "closed": "closed",
                 "unparked": "admitted",
                 # repair round 4, IN SCOPE 2: an instrument_error event (admission-time,
                 # via reject()'s non-terminal branch, or gate-time, via
                 # instrument_inquiry()) makes the block itself visible in queue
                 # state rather than leaving the id absent (favourable absence) or
                 # stuck reporting whatever status it had before the block. It does
                 # NOT touch ledger.terminal or ledger.parked, so re-admission /
                 # re-gating on repair is unaffected.
                 "instrument_error": "blocked"}
        for ev in events:
            ident, event = ev.get("id"), ev.get("event")
            if not ident or not event:
                continue
            detail = _event_detail(ev)
            if detail.get("title"):
                v.titles.setdefault(ident, detail["title"])
            if event in ("merged", "completed", "rejected", "closed"):
                # `completed` is terminal exactly like merged/rejected: out-of-repo
                # / host-ops work that was verified and applied but has no merge_sha
                # (ruling D13a). `closed` is the finding-side terminal: the gate
                # loop emits it on a FINDING id when a landing candidate resolves
                # it with its bound test proven green — findings never enter WIP,
                # so this cannot move the cap. First terminal wins; the guarantee
                # is exactly one, so a second is a bug elsewhere and must not
                # overwrite the first.
                v.terminal.setdefault(ident, ev)
                v.status[ident] = event
                v.parked.discard(ident)
                v.wip_entered.discard(ident)
                continue
            if event == "unrejected":
                # THE ONE REVERSAL. This must sit ABOVE the terminal guard below,
                # or it is unreachable for exactly the ids it exists to rescue —
                # adding "unrejected" to the `order` map instead is the documented
                # no-op fix for this defect and was tried and recorded as such.
                #
                # Cost of not having it: proposals whose
                # LAST ledger event was `admitted` read back as `rejected` and were
                # ABSENT from state/queue.json, so `wip` under-read and live
                # re-admitted work was unclaimable. Operator-reasoned `unrejected`
                # events had been discarded in silence along with everything that
                # followed them.
                #
                # ONLY `rejected` is reversible, and the check is on the RECORDED
                # terminal event, not on `status` — `status` is last-write-wins
                # across the terminal branch above, so a `merged` id that later
                # collected a bogus `rejected` reads status=rejected while its
                # terminal slot correctly still holds the merge. Keying on the slot
                # is what keeps landed work landed. A stray `unrejected` on an id
                # with a hard terminal, or with no terminal at all, is inert here:
                # it must never invent state, because demoting a live `admitted`
                # id to `open` would silently free a WIP slot.
                prior = v.terminal.get(ident)
                if isinstance(prior, dict) and prior.get("event") == "rejected":
                    del v.terminal[ident]
                    # `open`, never `admitted`: the reversal restores ELIGIBILITY,
                    # not the state the id held before it was refused. Whatever
                    # comes next in the ledger decides, and if nothing does, the
                    # item is claimable rather than counted against the cap.
                    v.status[ident] = "open"
                    # The terminal slot is now re-armed on purpose: a later
                    # `rejected` terminalises again and records ITS OWN event
                    # (a control sequence rejects, is unrejected, then is
                    # rejected a second time — that refusal is the one that sticks).
                continue
            if ident in v.terminal:
                continue
            if event == "parked":
                v.parked.add(ident)
                continue
            if event == "unparked":
                v.parked.discard(ident)
                v.wip_entered.add(ident)
            if event == "admitted":
                v.admitted.add(ident)
                v.wip_entered.add(ident)
            if event == "gated":
                v.gate_runs.setdefault(ident, []).append(ev)
                v.wip_entered.add(ident)
            if event == "instrument_error":
                v.instrument_runs.setdefault(ident, []).append(ev)
                # A blocked-on-human refusal is a live candidate awaiting an
                # operator ruling, so it enters WIP at the refusal itself.
                # Ledger damage is queue-level uncertainty handled by
                # `wip_degraded`/`read_wip`, never invented per-id provenance.
                if detail.get("class") == "blocked-on-human":
                    v.wip_entered.add(ident)
            if event in order:
                v.status[ident] = order[event]
        return v

    def prior_gate(self, ident: str, candidate_sha: str, merge_base: str | None) -> dict | None:
        """A previous gate run on this EXACT input, if there was one.

        This is the whole anti-storm mechanism. A prior run already produced its
        result on this EXACT input — a merge-gate exit 2 (refuse-before-verdict /
        could-not-run) will not change on an unchanged input; re-running it buys
        the same answer twice at full price.
        """
        for ev in self.gate_runs.get(ident, []):
            if not isinstance(ev.get("detail"), dict):
                # A malformed detail describes no prior run. Skip it outright
                # rather than letting an empty dict compare equal to a None
                # argument -- a false "prior run matches" here SKIPS A REAL
                # GATE, which is the one direction that must never happen. A
                # false "no prior run" merely costs one gate run.
                continue
            d = _event_detail(ev)
            if d.get("candidate_sha") != candidate_sha:
                continue
            if merge_base and d.get("merge_base_sha") and d["merge_base_sha"] != merge_base:
                continue
            return ev
        return None

    def prior_instrument_error(self, ident: str, candidate_sha: str,
                               workspace_fingerprint: str) -> dict | None:
        """A previous instrument failure on this input WITH THE INSTRUMENT UNCHANGED.

        An instrument error is meant to be re-gated once the instrument is fixed.
        "Once the instrument is fixed" is the load-bearing clause: without it the
        loop re-runs a ~12-minute gate every tick against a workspace nobody has
        touched, which is the same storm as re-running on an unchanged input,
        only wearing a different label.
        """
        for ev in self.instrument_runs.get(ident, []):
            if not isinstance(ev.get("detail"), dict):
                continue  # see prior_gate: a malformed record is never a match
            d = _event_detail(ev)
            if d.get("candidate_sha") == candidate_sha and \
               d.get("workspace_fingerprint") == workspace_fingerprint:
                return ev
        return None


# ------------------------------------------------------------------ verdict


@dataclass
class Refusal:
    reason: str
    cls: str      # candidate-defect | instrument-error | blocked-on-human | stale-base
    remedy: str   # replan | drop | blocked

    def ttl(self) -> str:
        return _iso(_now() + timedelta(days=REJECT_TTL_DAYS.get(self.cls, 7)))


@dataclass
class Candidate:
    ident: str
    path: Path
    art: dict
    paths: list[str] = field(default_factory=list)
    branch: str = ""
    base_sha: str = ""
    tip_sha: str = ""
    title: str = ""
    refusal: Refusal | None = None
    skip: str = ""            # not a refusal — a suspension or an already-decided id
    warnings: list[str] = field(default_factory=list)
    group: int = -1


# ----------------------------------------------------------------- admission

# Interpreter discovery lives in ONE place (bridge/validate_envelope.py) and
# is imported here rather than duplicated — three copies of this probe is
# exactly this codebase's #1 recurring defect pattern (one bug becomes
# 2-13 across clone families), and it already cost a real bug once: an
# earlier duplicated copy caught only OSError, so a hanging candidate
# interpreter escaped as an uncaught subprocess.TimeoutExpired identically
# in all three copies.
from bridge.validate_envelope import _no_conforming_interpreter_hint  # noqa: E402


def _schema_validate(art: dict, schema_dir: Path,
                     schema_file: str = "envelope.schema.json") -> tuple[str, str | None]:
    """('ok'|'fail'|'unavailable', message).

    'unavailable' is a distinct third value ON PURPOSE. If jsonschema is not
    installed, a two-valued function would return 'ok' and the caller would
    record a validation that never happened — the favourable-absence class,
    exactly. `schema_file` defaults to the envelope schema; pass
    "ledger-event.schema.json" to validate a ledger line instead (INT-11: the
    adapter must not exempt its own writes from the schema it enforces on
    everyone else).
    """
    try:
        import jsonschema  # type: ignore
    except ImportError:
        return "unavailable", ("jsonschema not importable under sys.executable="
                               f"{sys.executable!r} — structural checks only. "
                               f"{_no_conforming_interpreter_hint(Path(__file__).resolve())}")
    try:
        schema = json.loads((schema_dir / schema_file).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return "unavailable", f"{schema_file} unreadable: {exc}"
    try:
        jsonschema.validate(art, schema)
    except jsonschema.ValidationError as exc:  # type: ignore[attr-defined]
        loc = "/".join(str(p) for p in exc.absolute_path) or "<root>"
        return "fail", f"{loc}: {exc.message}"
    except Exception as exc:  # a broken schema is an instrument error, not a defect
        return "unavailable", f"validator error: {type(exc).__name__}: {exc}"
    return "ok", None


def _lineage(value: object) -> str | None:
    """Normalise a lineage label, or None if it carries no information.

    "anthropic" and "Anthropic" are the same lab, so a difference that exists
    only in case or surrounding whitespace is not a second lineage. This closes
    the ACCIDENT, not the lie: a producer willing to misreport its reviewer can
    write any string at all, and catching that needs a lineage vocabulary —
    a decision, tracked as its own finding rather than guessed at here.
    """
    if not isinstance(value, str):
        return None
    return value.strip().casefold() or None


def cross_lineage_check(art: dict, expected_sha: str) -> tuple[Refusal | None, list[str]]:
    """Fail closed on risky review evidence and bind approval to one exact commit."""
    if approved_cross_lineage(art, expected_sha):
        return None, []
    return Refusal(
        "risky real diff lacks a named final approval from a different known lineage "
        f"with reviewed_sha={expected_sha}; absent, same-lineage, anonymous, non-approving, "
        "or different-SHA evidence is not a passing review",
        "candidate-defect", "replan"), []


def validate(cand: Candidate, repo: Path, root: Path, schema_dir: Path,
             ledger: LedgerView, rejected: dict[str, dict], parked: set[str]) -> None:
    """Admission. Cheap, and therefore first.

    Sets `cand.refusal` (a decision that must be recorded) or `cand.skip` (an id
    that is already dead or suspended and simply does not get a gate).
    """
    art = cand.art

    # --- already decided ---------------------------------------------------
    if cand.ident in ledger.terminal:
        ev = ledger.terminal[cand.ident]
        cand.skip = (f"already terminal: {ev.get('event')} at {ev.get('ts')} — "
                     "exactly one terminal event per candidate")
        return
    if cand.ident in parked or cand.ident in ledger.parked:
        cand.skip = "PARKED — a human decision is owed; parking is a suspension, not a refusal"
        return
    rej = rejected.get(cand.ident)
    if rej:
        exp = _parse(rej.get("expires_at"))
        if exp is None or exp > _now():
            cand.skip = (f"in rejected/ until {rej.get('expires_at')} "
                         f"({rej.get('class')}/{_event_detail(rej).get('remedy', '?')}) "
                         "— a dead id does not get a gate")
            return
        cand.warnings.append(f"prior rejection expired {rej.get('expires_at')} — re-argued, re-admitting")

    # --- envelope ----------------------------------------------------------
    status, msg = _schema_validate(art, schema_dir)
    if status == "fail":
        cand.refusal = Refusal(f"envelope fails schema/envelope.schema.json — {msg}",
                               "candidate-defect", "replan")
        return
    if status == "unavailable":
        # FAIL CLOSED (Lane C), but NON-TERMINAL (repair round after
        # cross-lineage review F1). Before this, "unavailable" fell through
        # to structural checks only and the candidate was admitted on a
        # validation that never ran — the favourable-absence class this gate
        # exists to close.
        #
        # It must not become a `cand.refusal`, though: `iterate()` routes
        # every refusal through `reject()`, which writes a TERMINAL
        # `rejected` ledger event, and `validate()` itself checks
        # `ledger.terminal` FIRST, before this schema check ever runs. A
        # terminal rejection here would freeze the UNCHANGED candidate dead
        # forever, even after the host is repaired — an instrument error
        # rendered as a candidate defect, exactly what this whole plan
        # exists to close, one level down, and exactly what Lane A landing
        # before Lane C was meant to prevent. `cand.skip` is the correct
        # verb here: CONTRACT's own vocabulary calls parking/suspension
        # "not a refusal", and re-running validate() unchanged on the next
        # tick re-checks schema availability with no ledger event blocking
        # it either way -- repair is automatic, no human, no resubmission.
        cand.skip = (f"schema validation could not run ({msg}) — instrument error, not a "
                     "candidate defect; re-checked automatically once repaired, no terminal "
                     "event written")
        return

    if art.get("kind") != "candidate":
        cand.refusal = Refusal(f"kind is {art.get('kind')!r}, not 'candidate', in candidates/",
                               "candidate-defect", "replan")
        return

    payload = art.get("payload")
    if not isinstance(payload, dict):
        cand.refusal = Refusal("payload is absent or not an object", "candidate-defect", "replan")
        return
    try:
        derived = content_id(payload)
    except (TypeError, ValueError) as exc:
        cand.refusal = Refusal(f"payload is not canonicalisable: {exc}", "candidate-defect", "replan")
        return
    if derived != cand.ident:
        cand.refusal = Refusal(
            f"id does not match sha256 of the canonical payload (computed {derived}) — "
            "dedup against rejected/ is defeated by a mismatched id",
            "candidate-defect", "replan")
        return

    base = art.get("base_sha") or ""
    if not re.fullmatch(r"[0-9a-f]{40}", base):
        cand.refusal = Refusal(
            f"base_sha must be the FULL 40 hex chars, got {base!r} — abbreviations have collided here",
            "candidate-defect", "replan")
        return
    cand.base_sha = base

    head = art.get("head_sha") or ""
    if not re.fullmatch(r"[0-9a-f]{40}", head):
        cand.refusal = Refusal(
            f"head_sha must be the FULL immutable 40 hex chars, got {head!r}",
            "candidate-defect", "replan")
        return

    branch = art.get("branch") or ""
    if not branch:
        cand.refusal = Refusal("no branch", "candidate-defect", "replan")
        return
    cand.branch = branch

    paths = art.get("paths")
    if not isinstance(paths, list) or not paths or not all(isinstance(p, str) and p for p in paths):
        cand.refusal = Refusal(
            "paths must be a non-empty list of strings — Integration's conflict graph is built "
            "from it, so an empty or partial paths produces a WRONG schedule, not a slow one",
            "candidate-defect", "replan")
        return
    cand.paths = sorted({p.strip().lstrip("./") for p in paths})

    evidence = art.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        cand.refusal = Refusal("no evidence array", "candidate-defect", "replan")
        return
    executed = [e for e in evidence
                if isinstance(e, dict) and e.get("verified_by") == "execution"]
    if not executed:
        cand.refusal = Refusal(
            "no evidence entry with verified_by='execution' — a 'reading' may never carry a "
            "blocker, and this is what makes the admission promise machine-checkable",
            "candidate-defect", "replan")
        return
    incomplete = [e for e in executed if not e.get("command") or e.get("exit_code") is None]
    if incomplete:
        cand.refusal = Refusal(
            f"{len(incomplete)} verified_by='execution' entries lack command and/or exit_code — "
            "an execution claim with no command is a reading claim wearing a costume",
            "candidate-defect", "replan")
        return

    # --- git reality --------------------------------------------------------
    rc, tip, _ = git(repo, "rev-parse", "--verify", "-q", f"{head}^{{commit}}")
    if rc != 0 or not tip:
        cand.refusal = Refusal(
            f"immutable head_sha {head} does not resolve in {repo}",
            "candidate-defect", "replan")
        return
    rc_branch, branch_tip, _ = git(repo, "rev-parse", "--verify", "-q", f"{branch}^{{commit}}")
    if rc_branch == 0 and branch_tip and branch_tip != tip:
        cand.refusal = Refusal(
            f"branch {branch!r} moved away from immutable head_sha {tip} to {branch_tip}",
            "candidate-defect", "replan")
        return
    cand.tip_sha = tip

    anc = is_ancestor_of_main(repo, base)
    if anc is None:
        # Could not check. NOT a stale base — an unreadable repo is an instrument
        # error, and blaming the candidate for it is the single most expensive
        # mistake this loop can make.
        cand.refusal = Refusal(
            f"could not determine whether base_sha {base[:12]} is an ancestor of main "
            f"in {repo} (object missing or git unreadable)",
            "instrument-error", "blocked")
        return
    if not anc:
        cand.refusal = Refusal(
            f"base_sha {base[:12]} is no longer an ancestor of main — main moved. Re-verify on the "
            "new base and resubmit with `supersedes`; work is never silently landed against a base "
            "it was not verified on",
            "stale-base", "replan")
        return

    # --- paths completeness, checked rather than trusted ---------------------
    actual = changed_paths(repo, base, tip)
    if actual is None:
        cand.skip = "real candidate diff could not be read — instrument error; not admitted"
        return
    else:
        declared = set(cand.paths)
        actual_n = {p.strip().lstrip("./") for p in actual}
        risky = risky_review_paths(actual_n)
        if risky:
            refusal, warnings = cross_lineage_check(art, tip)
            cand.warnings.extend(warnings)
            if refusal:
                cand.refusal = refusal
                return
        missing = sorted(actual_n - declared)
        if missing:
            cand.refusal = Refusal(
                f"paths understates the diff by {len(missing)} file(s): {', '.join(missing[:6])}"
                f"{' …' if len(missing) > 6 else ''} — the conflict graph is built from paths, so "
                "an understated paths schedules a conflicting candidate to land in parallel",
                "candidate-defect", "replan")
            return
        extra = sorted(declared - actual_n)
        if extra:
            cand.warnings.append(
                f"paths overstates the diff by {len(extra)} file(s) ({', '.join(extra[:4])}) — "
                "not a refusal; it only over-serialises the schedule")


# ------------------------------------------------------------ conflict graph


def conflict_groups(cands: list[Candidate]) -> list[list[Candidate]]:
    """Union-find over shared paths. This is where throughput comes from.

    File-disjoint candidates may land in parallel; anything sharing a file
    serialises. Deterministic ordering so two runs schedule identically.
    """
    parent: dict[int, int] = {i: i for i in range(len(cands))}

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    owner: dict[str, int] = {}
    for i, c in enumerate(cands):
        for p in c.paths:
            if p in owner:
                union(owner[p], i)
            else:
                owner[p] = i

    buckets: dict[int, list[Candidate]] = {}
    for i, c in enumerate(cands):
        buckets.setdefault(find(i), []).append(c)
    groups = [sorted(v, key=lambda c: (c.art.get("created_at") or "", c.ident))
              for v in buckets.values()]
    for gi, grp in enumerate(sorted(groups, key=lambda g: g[0].ident)):
        for c in grp:
            c.group = gi
    return sorted(groups, key=lambda g: g[0].ident)


# -------------------------------------------------------------------- gating


@dataclass
class GateVerdict:
    result: str          # pass | candidate-defect | instrument-error
    exit_code: int
    slug: str
    reason: str
    receipt: Path | None
    stdout_tail: str
    duration_s: float


def classify_gate(rc: int, out: str, err: str, receipt: Path | None,
                  duration_s: float) -> GateVerdict:
    """Classify BEFORE believing. The highest-value judgement in this file.

    Measured across all recorded history on this estate: 64 of 90 refusals were
    instrument errors — unpinned or dirty workspace, a moved merge base, an
    exemption landed in the wrong checkout — not candidate defects.

    Two rules do the work:
      * classify by SLUG, not by exit code. `refuse()` in merge-gate.sh exits 2
        for both classes; `oracle-path`, `secrets`, `openapi-drift` and
        `reachability` are real defects hoisted ahead of the expensive suites.
      * a defect claim with NO SIGNED RECEIPT is downgraded to instrument error.
        The canonical trap here: a CI job with no git identity made `git merge`
        exit 128, and the gate reported "conflicts against main" while silently
        skipping every suite. There were no conflicts. An unreproducible defect
        claim is not evidence of a defect.
    """
    blob = f"{out}\n{err}"
    tail = "\n".join(blob.strip().splitlines()[-25:])
    slugs = REFUSING_RE.findall(blob)
    slug = slugs[-1] if slugs else ""

    have_receipt = bool(receipt and receipt.exists())

    if rc == 0:
        return GateVerdict("pass", rc, "", "gate passed", receipt, tail, duration_s)

    # PIPELINE-GAP GUARD (2026-08-09). The mandatory candidate-bound signed receipt
    # being ABSENT is a pipeline gap (the minter never ran), NEVER a code defect --
    # even though the gate wrote its own --emit-receipt run-receipt (so have_receipt
    # is True). Without this, "no signed receipt" is misfiled as a candidate-defect
    # below and every train the mechanical lander gates is wrongly rejected with a
    # TTL. Only the ABSENT case is downgraded; a PRESENT-but-invalid receipt (bad
    # signature/binding) stays a real refusal and is deliberately not forgiven here.
    if "no signed receipt" in blob:
        return GateVerdict(
            "instrument-error", rc, "candidate-receipt-absent",
            "gate refused: the mandatory candidate-bound signed receipt was never minted "
            "(pipeline gap -- the lander must mint it before gating; not a code defect); "
            "re-gate after minting, NEVER reject the candidate for it",
            receipt, tail, duration_s)

    if rc == 2 or slug:
        if slug in GATE_CANDIDATE_DEFECT_SLUGS:
            if not have_receipt:
                return GateVerdict(
                    "instrument-error", rc, slug,
                    f"gate refused '{slug}' but emitted no signed receipt — a defect claim "
                    "without a receipt is prose, not evidence; not recorded against the candidate",
                    receipt, tail, duration_s)
            hint = REMEDY_HINTS.get(slug, "")
            return GateVerdict("candidate-defect", rc, slug,
                               f"gate: {slug}{' — ' + hint if hint else ''}",
                               receipt, tail, duration_s)
        if slug in GATE_INSTRUMENT_SLUGS:
            hint = REMEDY_HINTS.get(slug, "")
            return GateVerdict("instrument-error", rc, slug,
                               f"gate instrument refusal: {slug}{' — ' + hint if hint else ''}",
                               receipt, tail, duration_s)
        return GateVerdict(
            "instrument-error", rc, slug or "<none>",
            f"unrecognised gate refusal slug {slug or '<none>'!r} at exit {rc} — classified as an "
            "instrument error because blaming the candidate requires a slug this adapter knows",
            receipt, tail, duration_s)

    if rc == 1:
        if not have_receipt:
            return GateVerdict(
                "instrument-error", rc, "no-receipt",
                "gate exited 1 (verdict fail) but emitted no signed receipt — an unreproducible "
                "defect claim is not evidence; treat as instrument error and fix the instrument",
                receipt, tail, duration_s)
        fails = [ln.strip(" -") for ln in blob.splitlines() if ln.strip().startswith("- ")]
        return GateVerdict("candidate-defect", rc, "verdict",
                           "gate verdict FAIL: " + ("; ".join(fails[:6]) or tail[-300:]),
                           receipt, tail, duration_s)

    # 127 = we could not even run it. 128 = the canonical git-identity trap.
    return GateVerdict("instrument-error", rc, f"exit-{rc}",
                       f"gate exited {rc} with no verdict — host/tooling failure, says nothing "
                       "about the code",
                       receipt, tail, duration_s)


# ---------------------------------------------------------------------- queue


def kinds_from_disk(root: Path) -> dict[str, str]:
    """id -> kind, read from the artifact directories.

    The ledger does not carry `kind` on every event, and an item rendered as
    kind "unknown" in queue.json is a reader's problem, not a writer's.
    """
    out: dict[str, str] = {}
    for dirname, kind in (("inquiries", "inquiry"), ("findings", "finding"),
                          ("proposals", "proposal"), ("candidates", "candidate")):
        d = root / dirname
        if not d.is_dir():
            continue
        for p in d.glob("*.json"):
            out[p.stem.replace("_", ":", 1)] = kind
    return out


DEFAULT_PRIORITY = 2  # CONTRACT.md §3 -- absent `priority` on an envelope is 2 ("normal"),
                       # everywhere, so legacy envelopes with no such field remain valid and
                       # are neither starved nor favoured against artifacts that carry it.
PRIORITY_AGING_PROMOTE_AFTER_SECONDS = 86400  # 24h: one promotion rewards sustained waiting without flattening bands.


def priorities_from_disk(root: Path) -> dict[str, tuple[int, str | None]]:
    """id -> (priority, created_at) read straight from the envelope file on disk.

    Only claimable kinds (`inquiries/`, `findings/`, `proposals/` -- CONTRACT.md
    §6: "Candidates are not claimed") are read here; a candidate has no
    priority-ordered claim step, so it is left out and callers fall back to
    `DEFAULT_PRIORITY` / no aging for it.

    A `priority` that is absent, unparseable, non-integer, or out of the
    documented 0..3 range is read as `DEFAULT_PRIORITY` (2, "normal") rather
    than refused here -- `bridge/validate_envelope.py` is the gate that
    refuses a malformed envelope at write time; a READER that also refused
    would let one bad file on disk take down ordering for every other item,
    which is the exact favourable-absence-vs-DoS tradeoff CONTRACT.md's
    claim-marker reads already settled the other way (see `claim.py`'s
    `count_live_claims`).
    """
    out: dict[str, tuple[int, str | None]] = {}
    for dirname in ("inquiries", "findings", "proposals"):
        d = root / dirname
        if not d.is_dir():
            continue
        for p in d.glob("*.json"):
            ident = p.stem.replace("_", ":", 1)
            priority = DEFAULT_PRIORITY
            created_at = None
            try:
                art = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                art = None
            if isinstance(art, dict):
                raw = art.get("priority")
                if isinstance(raw, int) and not isinstance(raw, bool) and 0 <= raw <= 3:
                    priority = raw
                created_at = art.get("created_at") if isinstance(art.get("created_at"), str) else None
            out[ident] = (priority, created_at)
    return out


def effective_priority(priority: int, created_at: str | None, *, now: datetime | None = None,
                       promote_after: int = PRIORITY_AGING_PROMOTE_AFTER_SECONDS) -> int:
    """Return a capped, threshold-gated anti-starvation promotion.

    An item remains at its nominal priority until it has waited a full
    `promote_after` seconds, then improves by exactly one priority level.  It
    never receives another age-based promotion, preserving the priority bands
    even for very old items.  Missing, unparseable, or timezone-ambiguous
    `created_at` values receive no promotion; the caller sorts those unknown
    ages after known ages within an effective-priority band.
    """
    if created_at is None:
        return priority
    when = _parse(created_at)
    if when is None or when.tzinfo is None:
        return priority
    ref = now or _now()
    age_s = max(0.0, (ref - when).total_seconds())
    if age_s < promote_after:
        return priority
    return max(0, priority - 1)


def rebuild_queue(root: Path, ledger: LedgerView, wip_cap: int) -> dict:
    """state/queue.json, rebuilt by replaying ledger.jsonl. The ledger wins.

    `wip` is THE backpressure signal both producer loops gate on, and nothing has
    ever written it: the file still holds the bootstrap seed, so "the consumer is
    dead" and "the consumer is idle with full capacity" are the same four bytes.

    `wip_definition` ships inside the file on purpose — a bare integer that two
    readers define differently is a backpressure signal only by accident.
    """
    priorities = priorities_from_disk(root)
    now = _now()
    items = []
    for ident, status in sorted(ledger.status.items()):
        if status in ("merged", "completed", "rejected", "closed"):
            continue
        if ident in ledger.parked:
            status = "parked"
        priority, created_at = priorities.get(ident, (DEFAULT_PRIORITY, None))
        items.append({
            "id": ident,
            "kind": ledger.kinds.get(ident, "unknown"),
            "status": status,
            "title": ledger.titles.get(ident, ""),
            "priority": priority,
            "effective_priority": effective_priority(priority, created_at, now=now),
        })
    # CONTRACT.md §6: claimable items are ordered priority-then-age.  Aging
    # supplies only one capped promotion; within a band, oldest known item
    # first supplies the remaining anti-starvation signal.  Unknown or
    # unparseable ages use +infinity (newest) so they sort after known ages,
    # rather than being rewarded for missing data.  Ownership is still decided
    # solely by the O_EXCL claim create (bridge/claim.py); this sort has zero
    # effect on that arbiter.  The full `(effective_priority, created_at, id)`
    # key keeps equal-age ties deterministic.
    def queue_sort_key(item: dict) -> tuple[int, float, str]:
        created_at = priorities.get(item["id"], (DEFAULT_PRIORITY, None))[1]
        when = _parse(created_at)
        created_at_key = (when.timestamp()
                          if when is not None and when.tzinfo is not None
                          else float("inf"))
        return (item["effective_priority"], created_at_key, item["id"])

    items.sort(key=queue_sort_key)
    wip = sum(1 for i in items if i["status"] in ("admitted", "gated")
              or (i["status"] == "blocked" and i["id"] in ledger.wip_entered))
    degraded = bool(ledger.discarded or ledger.torn_tail)
    queue = {
        "items": items,
        "wip_cap": wip_cap,
        "wip_definition": "ids whose status is admitted or gated, plus ids blocked while "
                          "occupying a slot (entered WIP via an admitted/gated/unparked event "
                          "or a blocked-on-human refusal, no terminal event, not parked); an "
                          "instrument error on an id that never entered WIP is visible but not "
                          "WIP; ledger damage (discarded lines or a torn tail) sets wip_degraded "
                          "and omits wip so every reader refuses the guessed count",
        "wip_degraded": degraded,
        "wip_degraded_detail": getattr(ledger, "damage_detail", ""),
        "rebuilt_at": _iso(_now()),
        "rebuilt_by": ROLE,
        "ledger_events": len(ledger.events),
        "ledger_torn_tail": ledger.torn_tail,
        # Distinguishes "the ledger is empty" from "the ledger was thrown away".
        # Without this, both render as ledger_events: 0.
        "ledger_discarded": ledger.discarded,
        # Distinguishes "fully recovered a spliced line" from "read clean" —
        # both admit all their events, but only one of them means a writer
        # dropped a newline mid-append (2026-08-10, cross-lineage review).
        "ledger_recovered": ledger.recovered,
    }
    if not degraded:
        queue["wip"] = wip
    return queue


# ------------------------------------------------------------------ the loop


class DaemonSelfTestFailed(RuntimeError):
    """Raised by Integration.__init__ when this process's own interpreter
    cannot import jsonschema. Downtime over corruption, intentionally (Lane
    C, operator ruling D7-yes 2026-08-08T12:45Z): a daemon that starts
    without a working validator is the exact favourable-absence shape this
    plan closes, one level up — an ABSENT validator must not read as a
    running one."""


class LedgerWriteRefused(RuntimeError):
    """Raised by `Integration._append_ledger` when the write-boundary guard
    (repair round 4) refuses an event, instead of the guard logging, alerting,
    and returning `None` — success-shaped, identical to what a landed write
    also returns (repair round 5, IN SCOPE 1).

    MECHANISM CHOICE: raise, not a sentinel/boolean return. A return value
    that signals refusal still has to be CHECKED by every caller, present and
    future, to mean anything — exactly the same "trust the caller to remember"
    shape this guard exists to delete on the write side (see
    `_append_ledger`'s own docstring: the guard exists because a caller could
    not be trusted to remember not to build a forbidden event). An exception
    cannot be silently discarded by a caller that forgets to check it; it
    unwinds the stack unless something explicitly catches it, so "a
    distinguishable refusal that every caller ignores" cannot recur by
    construction. No caller in this file currently constructs the forbidden
    shape (reject() converts it to a non-terminal `instrument_error` event
    before ever reaching `_append_ledger`), so this raise is not expected to
    fire in the normal admission path — it is the write boundary's own
    defence for a dynamic/future/cross-module caller, and it must fail LOUD
    when it does."""


class Integration:
    def __init__(self, root: Path, repo: Path, gate_ws: Path, schema_dir: Path,
                 apply: bool, *, allow_remote_gate: bool = False,
                 remote_gate_ws: str = REMOTE_GATE_WORKSPACE,
                 remote_evidence_root: str = REMOTE_EVIDENCE_ROOT) -> None:
        # Lane C daemon-start self-test: import jsonschema under THIS
        # process's own sys.executable (not a subprocess, not a probe of
        # some other interpreter) and refuse to start if it fails. This is
        # what makes the "unavailable" admission-time refusal below
        # unreachable in normal operation — reaching it after startup means
        # the environment broke mid-run, not that the daemon started
        # degraded.
        try:
            import jsonschema  # noqa: F401
        except ImportError as exc:
            hint = _no_conforming_interpreter_hint(Path(__file__).resolve())
            raise DaemonSelfTestFailed(
                f"jsonschema is not importable under sys.executable={sys.executable!r} "
                f"({exc}) — refusing to start rather than admitting artifacts on "
                f"structural checks only. {hint}") from exc

        self.root, self.repo, self.gate_ws = root, repo, gate_ws
        self.schema_dir, self.apply = schema_dir, apply
        # Remote gate execution is OFF unless asked for. The routing DECISION
        # is always computed and always recorded; only the dispatch is gated.
        self.allow_remote_gate = allow_remote_gate
        self.remote_gate_ws = remote_gate_ws
        self.remote_evidence_root = remote_evidence_root
        self.lines: list[str] = []
        self.alerts: list[str] = []
        self.summary: dict = {}

    # -- writes (all of them go through here, all of them respect --apply) ----

    def _self_validate(self, obj: dict, schema_file: str, required: tuple[str, ...],
                       role_path: tuple[str, ...]) -> None:
        """INT-11: this adapter refuses OTHER producers' envelopes that fail
        schema/*.schema.json (see `validate()` above) — it must not exempt its
        OWN writes from the same rule. Raises RuntimeError on failure; a
        self-write that fails its own schema is an adapter bug, not a
        candidate defect, and must stop the run rather than land silently.

        FAIL CLOSED (Lane C, repair round after cross-lineage review F2):
        'unavailable' used to run a minimal structural check (required keys +
        role enum) and return success-shaped if that passed -- so a
        schema-invalid `event` enum (e.g. a typo'd event name) still got
        permanently appended to the ledger, which is APPEND-ONLY and cannot
        be repaired after the fact. "the adapter writes it, so it is
        trustworthy" is the exact fail-open assumption Lane C exists to
        delete on the ingest side; the ledger deserves the STRICTEST check
        here, not the most forgiving one, because unlike an ingest refusal
        (re-checkable once the instrument is fixed) a bad ledger append is
        permanent. The daemon-start self-test (Integration.__init__) already
        guarantees jsonschema is importable at process start, so reaching
        'unavailable' here means the SCHEMA FILE itself broke mid-run --
        that must stop the write, not fall back to a check that cannot see
        the schema it is meant to enforce.

        `required` and `role_path` are UNUSED now that this always raises on
        'unavailable' rather than falling back to a structural check; kept
        as parameters (not removed) so callers do not need to change and the
        git history of "what this used to check" stays legible.
        """
        del required, role_path  # kept for call-site compatibility; see docstring
        status, msg = _schema_validate(obj, self.schema_dir, schema_file)
        if status == "fail":
            raise RuntimeError(f"self-write fails {schema_file} — {msg}: {obj!r}")
        if status == "unavailable":
            raise RuntimeError(
                f"self-write could not be validated against {schema_file} ({msg}) — "
                f"refusing to write rather than admitting on structural checks only: {obj!r}")
        # status == "ok"

    def _append_ledger(self, event: dict) -> None:
        # WRITE-BOUNDARY GUARD (repair round 4, replacing the round-3
        # per-caller CHOKEPOINT in reject()). Round 3 believed `reject()`
        # was the only writer of a terminal "rejected" event; it is not —
        # `_append_ledger` accepts a VARIABLE event object, so ANY caller,
        # present or future, literal or built from data, that constructs a
        # `{"event": "rejected", "detail": {"class": "instrument-error" |
        # "blocked-on-human", ...}}` dict and hands it here would sail past
        # a per-caller check. CONTRACT §1: instrument errors are never used
        # for a rejection. This is the one point every writer must pass
        # through, so it is where the invariant is enforced now — refuse
        # the WRITE itself, loudly (self.lines + an operator alert), rather
        # than trusting each call site to remember not to build this shape.
        #
        # REPAIR ROUND 5, IN SCOPE 1: this used to `return` here — `None`,
        # the exact same value a SUCCESSFUL append also returns (see the
        # unconditional fall-through to `return` at the end of a dry-run
        # append, and the implicit `None` at the end of an applied one). A
        # caller could not tell a refused write from a landed one; that is
        # the very favourable-absence defect this guard exists to eliminate,
        # now committed by the guard itself. Raise instead — see
        # `LedgerWriteRefused`'s docstring for why raise was chosen over a
        # sentinel/boolean return.
        if event.get("event") == "rejected":
            cls = _event_detail(event).get("class")
            if cls in ("instrument-error", "blocked-on-human"):
                ident = event.get("id", "")
                self.lines.append(
                    f"REFUSED WRITE: a 'rejected' event with detail.class={cls!r} for "
                    f"{_short(ident)} was NOT appended — instrument errors and "
                    "blocked-on-human conditions are never terminalized (CONTRACT §1), "
                    "enforced at the ledger write boundary regardless of caller")
                self._alert(
                    f"BUG: a caller tried to write a terminal 'rejected' event with "
                    f"class={cls!r} for {ident!r} — refused at the ledger write boundary; "
                    "this should never happen and means a caller built an invalid event "
                    "instead of routing through Integration.reject()/instrument_inquiry()")
                raise LedgerWriteRefused(
                    f"refused to append a terminal 'rejected' event with "
                    f"detail.class={cls!r} for {ident!r} — instrument errors and "
                    "blocked-on-human conditions are never terminalized (CONTRACT §1); "
                    "route through Integration.reject()/instrument_inquiry() instead")
        # Validate BEFORE any dict-indexing of `event` below. A malformed
        # event (e.g. missing "event") must die with _self_validate's
        # structured RuntimeError, not a raw KeyError from the log-line
        # f-string — the whole point of self-validation is a clear diagnosis,
        # and that is lost if indexing runs first and crashes first.
        self._self_validate(event, "ledger-event.schema.json",
                            ("ts", "role", "event", "id"), ("role",))
        self.lines.append(f"ledger += {event['event']} {_short(event.get('id', ''))} "
                          # NOT _event_detail(): this is the log-forensics site,
                          # and json.dumps already tolerates a bare string. Printing
                          # `{}` for a malformed detail would destroy the very
                          # evidence needed to find the producer that wrote it --
                          # favourable absence, in the diagnostic channel.
                          f"{json.dumps(event.get('detail', {}))[:160]}")
        if not self.apply:
            return
        append_event(self.root, event)

    def _write_json(self, path: Path, obj: dict, why: str) -> None:
        self.lines.append(f"write {path.relative_to(self.root)}  ({why})")
        if obj.get("contract") == "v1.1":  # only true envelopes carry this marker
            self._self_validate(obj, "envelope.schema.json",
                                ("id", "kind", "title", "created_at", "producer", "payload"),
                                ("producer", "role"))
        if not self.apply:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")   # same directory: rename is atomic
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        tmp.rename(path)

    def _alert(self, msg: str) -> None:
        self.alerts.append(msg)
        if self.apply:
            with open(self.root / "ALERTS.md", "a", encoding="utf-8") as fh:
                fh.write(f"- {_iso(_now())} integration: {msg}\n")

    # -- terminal events -----------------------------------------------------

    def reject(self, cand: Candidate, refusal: Refusal, receipt: str | None = None) -> None:
        """Exactly one terminal event, and it always names its own remedy.

        CHOKEPOINT, SUPERSEDED (repair round 3, F1 sibling; repair round 4):
        round 3 made this method the sole enforcement point for CONTRACT §1
        ("instrument errors are never used for a rejection") on the theory
        that `"rejected"` has exactly one writer in the whole bridge/
        package. That theory was disproved: `_append_ledger` accepts a
        VARIABLE event object, and nothing stopped a different or future
        caller from building a `{"event": "rejected", "detail": {"class":
        "instrument-error", ...}}` dict directly and handing it to
        `_append_ledger`, bypassing this method entirely. The real
        enforcement point is now `_append_ledger` itself (the write
        boundary) — see its docstring. This method's own early-return below
        is KEPT (defence in depth, and it is where the non-terminal
        `instrument_error` bookkeeping event belongs), but it is no longer
        the thing that makes the invariant hold.

        No terminal event, no rejected/ file for an instrument-error or
        blocked-on-human refusal — the next iteration re-checks admission
        from scratch (ledger.terminal will not contain this id), so
        repairing the instrument re-admits the SAME, unchanged candidate
        automatically, with no resubmission and no human. A NON-TERMINAL
        `instrument_error` ledger event IS written, though (repair round 4,
        IN SCOPE 2): without it, `state/queue.json` reported `items: [],
        wip: 0` while admission was actually blocked — a false "maximum
        headroom" read at the exact moment there was none. The event does
        not touch `ledger.terminal` or `ledger.parked`, so it cannot regress
        the re-admission property above.
        """
        if refusal.cls in ("instrument-error", "blocked-on-human"):
            self.lines.append(
                f"NOT rejecting {_short(cand.ident)}: refusal class {refusal.cls!r} is an "
                f"instrument condition, not a candidate defect ({refusal.reason}) — no "
                "terminal event written; re-checked automatically once the instrument is "
                "repaired")
            self._alert(f"{refusal.cls} blocking admission of {cand.ident}: {refusal.reason}")
            # Non-terminal bookkeeping only: makes the block visible in
            # state/queue.json without freezing the candidate. A blocked-on-human
            # refusal enters `wip_entered` here; a generic instrument error counts
            # only when an earlier WIP-entry transition exists. Never touches
            # ledger.terminal/parked.
            self._append_ledger({
                "ts": _iso(_now()), "role": ROLE, "event": "instrument_error",
                "id": cand.ident, "actor": "integration-adapter",
                "detail": {"reason": refusal.reason, "class": refusal.cls,
                           "candidate_sha": cand.tip_sha, "remedy": refusal.remedy}})
            return
        fresh = LedgerView.build(self.root)          # re-read: exactly ONE terminal
        if cand.ident in fresh.terminal:
            self.lines.append(f"SKIP reject {_short(cand.ident)}: a terminal event landed "
                              f"since this iteration started ({fresh.terminal[cand.ident].get('event')})")
            return
        expires = refusal.ttl()
        detail = {"reason": refusal.reason, "class": refusal.cls,
                  "expires_at": expires, "remedy": refusal.remedy}
        if receipt:
            detail["receipt"] = receipt
        event = {"ts": _iso(_now()), "role": ROLE, "event": "rejected",
                 "id": cand.ident, "actor": "integration-adapter", "detail": detail}
        # base_sha is schema-typed as a required-40-hex STRING with no null
        # variant (schema/ledger-event.schema.json). A refused candidate is
        # exactly the input most likely to carry a missing/short/absent
        # base_sha (that is often WHY it was refused) — writing `None` here
        # crashes _self_validate and kills the adapter on its own refusal
        # path. Only ever emit the key when it is the real 40-char sha; when
        # it is not, OMIT the key (base_sha is optional per schema) rather
        # than write null. The event still names the candidate by `id` and
        # the reason (incl. the malformed base_sha, if that is it) is on
        # `detail.reason` — nothing about the refusal is lost by omitting it.
        if re.fullmatch(r"[0-9a-f]{40}", cand.base_sha or ""):
            event["base_sha"] = cand.base_sha
        self._append_ledger(event)
        self._write_json(self.root / "rejected" / f"{_stem(cand.ident)}.json", {
            "id": cand.ident, "kind": "candidate", "reason": refusal.reason,
            "class": refusal.cls, "at": _iso(_now()), "expires_at": expires,
            "by": ROLE, "receipt": receipt,
            "detail": {"remedy": refusal.remedy, "branch": cand.branch,
                       "base_sha": cand.base_sha, "title": cand.title},
        }, f"rejected/{refusal.cls}/{refusal.remedy}")

    def instrument_inquiry(self, cand: Candidate, verdict: GateVerdict,
                           workspace_fingerprint: str) -> None:
        """An instrument error is an INQUIRY (area: tooling), never a rejection.

        CONTRACT §1: instrument errors say nothing about the code and are not
        landable work. The candidate keeps its admitted status and no terminal
        event is written — it is re-gated once the instrument is fixed.

        No timestamp goes in the payload: the id is the payload hash, so a clock
        in there makes every occurrence a brand-new artifact and floods the queue.
        """
        payload = {
            "area": "tooling",
            "observation": (
                f"merge-gate refused candidate {cand.branch} ({cand.tip_sha[:12]}) with "
                f"'{verdict.slug}' at exit {verdict.exit_code}: {verdict.reason}"),
            "why_not_a_fix": (
                "The failure is in the gate's environment, not in the candidate's code. What is "
                "not known is whether the instrument fault is host-local or reproducible; until "
                "that is answered, refusing the candidate would send the next agent to debug the "
                "wrong thing."),
        }
        ident = content_id(payload)
        evidence_item = {
            "claim": f"merge-gate exited {verdict.exit_code} with slug {verdict.slug}",
            "verified_by": "execution",
            "command": f"scripts/merge-gate.sh --candidate {cand.branch}",
            "exit_code": verdict.exit_code,
        }
        # evidence[].receipt is schema-typed as a STRING with no null variant
        # (schema/envelope.schema.json) -- same class as the base_sha and
        # instrument_detail.receipt fixes above, caught by the mechanical
        # sweep this file's siblings drew. Only ever emit the key with a
        # real value; omit it otherwise (receipt is not in evidence's
        # `required`), never null.
        if verdict.receipt:
            evidence_item["receipt"] = str(verdict.receipt)
        art = {
            "contract": "v1.1", "id": ident, "kind": "inquiry",
            "title": f"gate instrument error: {verdict.slug} on {cand.branch}"[:200],
            "created_at": _iso(_now()),
            "producer": {"role": ROLE, "actor": "integration-adapter"},
            "payload": payload,
            "evidence": [evidence_item],
        }
        target = self.root / "inquiries" / f"{_stem(ident)}.json"
        if target.exists():
            self.lines.append(f"inquiry {_short(ident)} already exists (same payload) — not duplicated")
        else:
            self._write_json(target, art, "instrument error -> inquiry(area=tooling)")
            self._append_ledger({"ts": _iso(_now()), "role": ROLE, "event": "inquired",
                                 "id": ident, "actor": "integration-adapter",
                                 "detail": {"area": "tooling", "about": cand.ident}})
        instrument_detail = {"reason": verdict.reason, "class": "instrument-error",
                             "exit_code": verdict.exit_code,
                             "inquiry": ident,
                             "candidate_sha": cand.tip_sha,
                             "workspace_fingerprint": workspace_fingerprint}
        # detail.receipt is schema-typed as a STRING with no null variant
        # (schema/ledger-event.schema.json), same as base_sha above. An
        # instrument error is exactly the case where there is often no
        # receipt (the gate itself failed to run), so `str(x) if x else
        # None` here was the same class of self-write crash waiting to
        # happen — only ever emit the key when there is a real value;
        # otherwise omit it (receipt is not in `required`), never null.
        if verdict.receipt:
            instrument_detail["receipt"] = str(verdict.receipt)
        self._append_ledger({"ts": _iso(_now()), "role": ROLE, "event": "instrument_error",
                             "id": cand.ident, "actor": "integration-adapter",
                             "detail": instrument_detail})

    # -- the gate ------------------------------------------------------------

    def gate_workspace_state(self) -> dict:
        """Read-only readiness probe. Reported even in dry-run: knowing a gate
        COULD not run is more useful than a plan that assumes it can."""
        state = {"path": str(self.gate_ws), "exists": self.gate_ws.is_dir(),
                 "head": None, "dirty": None, "ready": False, "why": ""}
        if not state["exists"]:
            state["why"] = "gate workspace missing — run scripts/gate-workspace.sh main"
            return state
        rc, head, _ = git(self.gate_ws, "rev-parse", "--verify", "HEAD")
        state["head"] = head if rc == 0 else None
        if rc != 0:
            state["why"] = "not a git checkout"
            return state
        rc, dirt, _ = git(self.gate_ws, "status", "--porcelain=v1", "--untracked-files=all")
        if rc != 0:
            state["why"] = "could not read workspace status"
            return state
        n = len([ln for ln in dirt.splitlines() if ln.strip()])
        state["dirty"] = n
        if n:
            state["why"] = (f"{n} uncommitted/untracked file(s) — the gate refuses "
                            "'dirty-workspace'; this is an INSTRUMENT fault, not a candidate one")
            return state
        rc, main_sha, _ = git(self.repo, "rev-parse", "--verify", "main")
        state["main"] = main_sha if rc == 0 else None
        state["ready"] = True
        return state

    def plan_gate_host(self, cand: Candidate) -> dict:
        """Where this gate should run — decided NOW, never from a cached number.

        Called once per gate invocation, immediately before dispatch. The
        probes live inside `choose_gate_host`; nothing here is memoised, and a
        reading older than 30 s is refused by construction. Routing on a
        minutes-old "the box is quiet" reading is a mistake this estate has
        already made twice in one day.

        The decision is REPORTED even when it comes back "local" — an operator
        reading the summary should be able to see that the twin was considered
        and why it was not used.
        """
        choice = choose_gate_host(sorted(cand.paths) if cand.paths else None)
        record = {"id": cand.ident, **choice.as_dict()}

        if choice.is_remote and not self.allow_remote_gate:
            record["dispatched"] = "local"
            record["why_not_remote"] = (
                "--allow-remote-gate is off. The routing decision is real and recorded; "
                "REMOTE EXECUTION is opt-in because proving it end-to-end costs a full "
                "gate run, and an unproven dispatch path that fails at minute 40 comes "
                "back looking like a candidate defect.")
        elif choice.is_remote:
            # Use the paths the ROUTER resolved for the box it picked, not the
            # instance defaults: the twins do not share a prefix (MW0001 runs
            # as `youruser`, MW0002 only has `cloud`), so a fixed prefix here
            # would gate host A against host B's path and return a workspace
            # error shaped like a candidate defect. Falls back to the instance
            # defaults for a single-twin choice that carries no paths.
            # An explicit --twin-gate-workspace / --twin-evidence-root override
            # applies only to the twin those flags were configured FOR (the
            # default twin). Applying them to any chosen host would push one
            # twin's paths onto another — the mismatch this whole change exists
            # to prevent. Before this guard the overrides were simply dead,
            # because a pool choice always carries registry paths, so configured
            # deployments silently stopped targeting their own workspace
            # (cross-lineage review 2026-08-10, finding 6).
            twin_ws = choice.workspace or self.remote_gate_ws
            twin_evidence = choice.evidence_root or self.remote_evidence_root
            if choice.host == TWIN_HOST:
                if str(self.remote_gate_ws) != REMOTE_GATE_WORKSPACE:
                    twin_ws = self.remote_gate_ws
                if str(self.remote_evidence_root) != REMOTE_EVIDENCE_ROOT:
                    twin_evidence = self.remote_evidence_root
            # Write the RESOLVED paths back into record immediately: `record`
            # was seeded from choice.as_dict() before this resolution ran, so
            # without this write run_gate reads plan["workspace"] straight
            # off the registry entry — the workspace pin/preflight never
            # touched — not the workspace this function actually proved
            # (design review round 2, finding 6 / BLOCKER 4).
            record["workspace"] = str(twin_ws)
            record["evidence_root"] = str(twin_evidence)
            # Measure the base in THIS checkout before touching the twin. The
            # artifact's base_sha is a claim, not an instrument reading: a
            # feature that merged newer main legitimately has a later merge
            # base, and grading it from the declared base would charge main's
            # already-incorporated commits to the candidate.
            #
            # `git()` invokes git directly as a Python subprocess. The estate
            # guard discussed by is_ancestor_of_main intercepts agent Bash-tool
            # probes, not subprocesses spawned by this daemon (or by pytest), so
            # the canonical git merge-base verb is safe and unambiguous here.
            rc, measured_base, merge_base_err = git(
                self.repo, "merge-base", "main", cand.tip_sha)
            if rc != 0 or not measured_base:
                detail = (merge_base_err or "git returned empty output").strip()
                record["dispatched"] = "local"
                record["why_not_remote"] = (
                    "could not measure the merge base locally (instrument, not "
                    f"candidate; git rc={rc}): {detail}")
                self.summary.setdefault("gate_hosts", []).append(record)
                return record

            # BASE PIN FIRST — the same hardening gate_loop got on 2026-08-10,
            # which this path was missing (cross-lineage review, blocker 1). The
            # twin recomputes its merge-base from its OWN HEAD, so a twin left on
            # a stale main charges every not-yet-synced main commit to the
            # candidate and then emits a SIGNED receipt saying so. A signed wrong
            # answer is worse than no answer: classify_gate trusts it and records
            # a candidate defect against code that is fine. Detaching the twin
            # onto the exact base measured here makes its judgement byte-identical
            # to the local gate's. It matters more with two twins, not less — the
            # newer box is the one most likely to be behind.
            base_pin = pin_remote_candidate(
                choice.host, str(twin_ws), "gate-pinned-main", measured_base,
                local_repo=str(self.repo), checkout=True)
            record["base_pin"] = base_pin
            base_pin["measured_base"] = measured_base
            if measured_base != cand.base_sha:
                base_pin["declared_base_mismatch"] = cand.base_sha
            if not base_pin["ok"]:
                record["dispatched"] = "local"
                record["why_not_remote"] = ("twin base pin failed (instrument, not "
                                            "candidate): " + base_pin["why"])
                self.summary.setdefault("gate_hosts", []).append(record)
                return record
            # Pin BEFORE preflight: preflight's "candidate ref reachable" check
            # can only pass once the branch has been fetched onto the twin, and
            # the pin also proves the twin will grade EXACTLY the tip this host
            # queued — a moved branch is a refusal to dispatch, not a shrug.
            pin = pin_remote_candidate(choice.host, str(twin_ws),
                                       cand.branch, cand.tip_sha,
                                       local_repo=str(self.repo))
            record["pin"] = pin
            if not pin["ok"]:
                record["dispatched"] = "local"
                record["why_not_remote"] = ("twin pin failed (instrument, not candidate): "
                                            + pin["why"])
                self.summary.setdefault("gate_hosts", []).append(record)
                return record
            pre = preflight_remote(
                choice.host, workspace=str(twin_ws),
                evidence_root=str(twin_evidence), candidate=cand.branch,
                expected_base=measured_base)
            record["preflight"] = pre
            if not pre["ready"]:
                # An unready twin is an INSTRUMENT fact. Fall back to local and
                # say so; never let it reach the candidate's record.
                record["dispatched"] = "local"
                record["why_not_remote"] = ("twin preflight failed (instrument, not candidate): "
                                            + "; ".join(pre["failed"]))
            else:
                record["dispatched"] = choice.host
        else:
            record["dispatched"] = "local"

        self.summary.setdefault("gate_hosts", []).append(record)
        return record

    def run_gate(self, cand: Candidate) -> GateVerdict:
        script = self.repo / "scripts" / "merge-gate.sh"
        receipts_dir = self.root / "receipts" / _stem(cand.ident)
        receipts_dir.mkdir(parents=True, exist_ok=True)
        receipt = receipts_dir / f"merge-gate-{cand.tip_sha[:12]}.json"
        plan = self.plan_gate_host(cand)
        env = dict(os.environ)
        if plan["dispatched"] != "local":
            # Same rule as plan_gate_host: the prefixes belong to the box, so
            # they are read off the PLAN (which recorded the chosen twin's own
            # paths) rather than from the instance defaults, which name only one
            # of the twins and would send host A to host B's filesystem.
            run_ws = plan.get("workspace") or self.remote_gate_ws
            run_evidence = plan.get("evidence_root") or self.remote_evidence_root
            remote_receipt = f"{run_evidence}/adapter-{cand.tip_sha[:12]}.json"
            cmd = remote_gate_command(
                plan["dispatched"], workspace=str(run_ws),
                candidate=cand.branch, receipt=remote_receipt,
                evidence_root=str(run_evidence),
                env_pins=plan.get("env_pins") or None,
                # Set NOWHERE else on a fresh box: an unset ladder-workers value
                # silently runs the ladder SERIAL at ~2x wall with no error
                # (Gate-Bottleneck-Elimination §1). GATE_LADDER_WORKERS is the
                # single source of truth (gate_host.py) — no more literal "8"
                # duplicated here (design review M4).
                extra_env={"MERGE_GATE_LADDER_WORKERS":
                           str(effective_ladder_workers())},
                # The twin self-bounds via gtimeout (I6); this path gets the
                # same GATE_TIMEOUT_SECONDS bound the local subprocess.run call
                # below already uses, so a hung remote gate terminates on the
                # twin instead of only being noticed when the local wait times
                # out (I6 consumer, design review).
                timeout_s=GATE_TIMEOUT_SECONDS - 300)
            cwd = str(self.repo)
        else:
            cmd = ["bash", str(script), "--candidate", cand.branch,
                   "--emit-receipt", str(receipt)]
            env["MERGE_GATE_PINNED"] = "1"                   # determinism mode
            env["OMNIAGENTOS_GATE_WORKSPACE"] = str(self.gate_ws)
            cwd = str(self.gate_ws)
        started = time.time()
        try:
            p = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True,
                               text=True, timeout=GATE_TIMEOUT_SECONDS, check=False)
            rc, out, err = p.returncode, p.stdout, p.stderr
        except subprocess.TimeoutExpired:
            return GateVerdict("instrument-error", 124, "timeout",
                               f"gate exceeded {GATE_TIMEOUT_SECONDS}s — host/instrument, "
                               "says nothing about the code",
                               receipt if receipt.exists() else None, "", time.time() - started)
        except OSError as exc:
            return GateVerdict("instrument-error", 127, "unrunnable",
                               f"could not execute {script}: {exc}", None, "", time.time() - started)
        if plan["dispatched"] != "local":
            # Bring the evidence home BEFORE classifying. classify_gate
            # downgrades receipt-less defect claims to instrument-error, so a
            # remote verdict without its receipt synced would silently convert
            # every real remote candidate-defect into instrument noise — the
            # exact failure --allow-remote-gate existed to prevent.
            local_ev = str(self.repo / "var" / "gate-evidence")
            sync = sync_back_evidence(
                plan["dispatched"], remote_receipt=remote_receipt,
                local_receipt=str(receipt),
                remote_records_dir=f"{run_evidence}/records/merge-gate",
                local_records_dir=f"{local_ev}/records/merge-gate")
            self.summary.setdefault("gate_sync_back", []).append(
                {"id": cand.ident, **sync})
            if not sync["ok"]:
                return GateVerdict(
                    "instrument-error", rc, "evidence-sync-failed",
                    f"remote gate on {plan['dispatched']} finished rc={rc} but the "
                    f"evidence could not be brought home ({sync['why']}) — a remote "
                    "verdict whose receipt did not come home is prose, not evidence",
                    None, "", time.time() - started)
        return classify_gate(rc, out, err, receipt, time.time() - started)

    # -- one iteration -------------------------------------------------------

    def iterate(self) -> int:
        root = self.root
        gov = read_governor(root)
        ws = self.gate_workspace_state()
        self.summary = {
            "mode": "APPLY" if self.apply else "DRY-RUN (writes nothing; use --apply)",
            "at": _iso(_now()),
            "repo": str(self.repo),
            "governor": {"ok": gov.ok, "stops": gov.stops, "notes": gov.notes,
                         "wip_cap": gov.wip_cap, "load_stops": gov.load_stops,
                         "may_gate": gov.may_gate, "load": gov.load_check},
            "gate_workspace": ws,
        }

        if not gov.ok:
            # Fail CLOSED on a HARD limit only — absent/stale budget, disk floor,
            # subscription window. Nothing is admitted, gated, or written.
            self.summary["decision"] = "STOP — governor"
            return 0

        if not gov.may_gate:
            # A load stop is NOT this. The box being busy does not make an
            # envelope valid or a rejection unexpired, and admission, ledger
            # replay and the queue rebuild are file reads costing milliseconds.
            # Discarding them cost ~9.2 minutes of loop time per block
            # (measured mean iteration, 57 samples) and bought nothing.
            self.summary["decision"] = ("PROCEED (gating deferred) — "
                                        + "; ".join(gov.load_stops))

        ledger = LedgerView.build(root)
        if ledger.torn_tail:
            self._alert("ledger.jsonl has a torn final line — replay skipped it; "
                        "queue.json is rebuilt over a hole")

        rejected = {}
        rej_dir = root / "rejected"
        if rej_dir.is_dir():
            for p in rej_dir.glob("*.json"):
                try:
                    body = json.loads(p.read_text())
                except (OSError, json.JSONDecodeError):
                    continue
                if body.get("id"):
                    rejected[body["id"]] = body
        parked_dir = root / "parked"
        parked = ({p.stem.replace("_", ":", 1) for p in parked_dir.glob("*.json")}
                  if parked_dir.is_dir() else set())

        # --- read candidates -------------------------------------------------
        cands: list[Candidate] = []
        broken: list[str] = []
        cdir = root / "candidates"
        for p in sorted(cdir.glob("*.json")) if cdir.is_dir() else []:
            try:
                art = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                broken.append(f"{p.name}: unparseable ({type(exc).__name__})")
                continue
            if not isinstance(art, dict) or not art.get("id"):
                broken.append(f"{p.name}: no id")
                continue
            c = Candidate(art["id"], p, art, title=str(art.get("title", ""))[:120])
            ledger.kinds.setdefault(c.ident, "candidate")
            ledger.titles.setdefault(c.ident, c.title)
            cands.append(c)

        for c in cands:
            validate(c, self.repo, root, self.schema_dir, ledger, rejected, parked)

        refused = [c for c in cands if c.refusal]
        skipped = [c for c in cands if c.skip]
        admissible = [c for c in cands if not c.refusal and not c.skip]

        # Lane C fail-closed, non-terminal (repair round F1): schema
        # validation being unavailable at admission time is a systemic host
        # problem, not a per-candidate one — every candidate in this
        # iteration would skip the same way, and NONE of them get a ledger
        # event for it (cand.skip, not cand.refusal — see validate()). ONE
        # alert for the whole run is still owed, or the condition is
        # invisible until someone reads `skipped` in the summary. The
        # daemon-start self-test (__init__) is meant to make this
        # unreachable; reaching it here means jsonschema or the schema file
        # broke mid-run, after startup.
        _schema_unavailable = [c for c in skipped
                               if c.skip.startswith("schema validation could not run")]
        if _schema_unavailable:
            self._alert(
                f"schema validation UNAVAILABLE for {len(_schema_unavailable)} candidate(s) this "
                f"iteration ({_schema_unavailable[0].skip}) — admission is suspended, not "
                "refused (Lane C fail-closed, non-terminal); this should be unreachable past "
                "the daemon-start self-test")

        # --- schedule --------------------------------------------------------
        groups = conflict_groups(admissible)
        current_wip = sum(1 for i, s in ledger.status.items()
                          if s in ("admitted", "gated") and i not in ledger.parked)
        # The gate wave cap is a CONCURRENCY bound, deliberately NOT `wip_cap -
        # current_wip`. wip is queue DEPTH — admitted work that has not reached a
        # terminal event — and subtracting it here would deadlock the loop the
        # moment the queue held wip_cap items: everything admitted, nothing ever
        # gated, and the depth that caused it never draining.
        wave_cap = max(1, gov.wip_cap)

        wave: list[Candidate] = []
        deferred: list[tuple[Candidate, str]] = []
        for grp in groups:
            head, rest = grp[0], grp[1:]
            for c in rest:
                deferred.append((c, f"serialises behind {_short(head.ident)} "
                                    f"(shares {', '.join(sorted(set(c.paths) & set(head.paths))[:3])})"))
            if len(wave) < wave_cap:
                wave.append(head)
            else:
                deferred.append((head, f"gate wave cap {wave_cap} reached this iteration"))

        # --- report the plan, then act ---------------------------------------
        self.summary.update({
            "candidates_on_disk": len(cands),
            "unreadable_files": broken,
            "would_refuse": [{"id": c.ident, "branch": c.branch, "class": c.refusal.cls,
                              "remedy": c.refusal.remedy, "reason": c.refusal.reason}
                             for c in refused],
            "skipped": [{"id": c.ident, "why": c.skip} for c in skipped],
            "would_admit": [{"id": c.ident, "branch": c.branch, "group": c.group,
                             "paths": len(c.paths), "warnings": c.warnings}
                            for c in admissible],
            "conflict_groups": [[_short(c.ident) for c in g] for g in groups],
            "wip": current_wip, "wip_cap": gov.wip_cap, "gate_wave_cap": wave_cap,
            "would_gate": [{"id": c.ident, "branch": c.branch,
                            "cmd": f"scripts/merge-gate.sh --candidate {c.branch}"} for c in wave],
            "deferred": [{"id": c.ident, "why": why} for c, why in deferred],
            "proposals_pending": len(list((root / 'proposals').glob('*.json')))
                                 if (root / 'proposals').is_dir() else 0,
            "proposals_note": ("this adapter does NOT admit proposals; proposal admission and the "
                               "daily escalation digest (PROMPT §1b) are not implemented here, so a "
                               "zero here would be an absence, not an all-clear"),
        })

        for c in refused:
            self.reject(c, c.refusal)

        # Admission is a DECISION, and it is recorded whether or not the
        # instrument is ready to gate. This append used to live inside the gate
        # block, which meant that with a dirty or missing gate workspace nothing
        # was ever marked admitted, `wip` stayed 0, and every producer read the
        # backlog as maximum headroom — the favourable-absence class, in the very
        # file written to close it.
        for c in admissible:
            if c.ident in ledger.admitted:
                continue                       # idempotent: admitted exactly once
            self._append_ledger({"ts": _iso(_now()), "role": ROLE, "event": "admitted",
                                 "id": c.ident, "base_sha": c.base_sha,
                                 "actor": "integration-adapter",
                                 "detail": {"branch": c.branch, "title": c.title,
                                            "paths": len(c.paths)}})

        gated: list[dict] = []
        if wave:
            if not gov.may_gate:
                # Deferred, not refused. The candidate is untouched and stays in
                # the wave for the next iteration; nothing is written against it,
                # because "the box was busy" is a fact about the host and must
                # never be recorded as a fact about the code.
                self.summary["gate_note"] = (
                    "gate DEFERRED (not refused): " + "; ".join(gov.load_stops)
                    + f" — {len(wave)} candidate(s) held; admission, ledger and "
                      "queue.json still ran")
            elif not self.apply:
                self.summary["gate_note"] = ("dry run: the gate was NOT invoked. It mints receipts "
                                             "and builds worktrees, which is a write.")
            elif not ws["ready"]:
                self.summary["gate_note"] = f"gate NOT run: workspace not ready — {ws['why']}"
                self._alert(f"gate workspace not ready ({ws['why']}) — {len(wave)} candidate(s) "
                            "waiting; this is an INSTRUMENT fault, not a candidate defect")
            else:
                fingerprint = f"{ws.get('head')}:{ws.get('dirty')}"
                for c in wave:
                    stale_inst = ledger.prior_instrument_error(c.ident, c.tip_sha, fingerprint)
                    if stale_inst:
                        self.lines.append(
                            f"NOT re-gating {_short(c.ident)}: an instrument error "
                            f"({_event_detail(stale_inst).get('exit_code')}) was recorded at "
                            f"{stale_inst.get('ts')} for this candidate AND the gate workspace is "
                            f"unchanged ({fingerprint}). Fix the instrument, then it re-gates by "
                            f"itself. Inquiry: {_short(_event_detail(stale_inst).get('inquiry', ''))}")
                        continue
                    prior = ledger.prior_gate(c.ident, c.tip_sha, None)
                    if prior and _event_detail(prior).get("exit_code") == 2:
                        self.lines.append(
                            f"NOT re-gating {_short(c.ident)}: exit 2 on this exact input at "
                            f"{prior.get('ts')} — the gate could not run on this input, and it will "
                            "not run on the unchanged input either. Change the input or fix the "
                            "mechanics; one symbol here drew 28 identical refusals.")
                        continue
                    if prior:
                        self.lines.append(
                            f"NOT re-gating {_short(c.ident)}: an unchanged input "
                            f"({c.tip_sha[:12]}) was already gated at {prior.get('ts')} with "
                            f"result {_event_detail(prior).get('result')}")
                        continue

                    v = self.run_gate(c)
                    gated.append({"id": c.ident, "branch": c.branch, "result": v.result,
                                  "exit_code": v.exit_code, "slug": v.slug,
                                  "duration_s": round(v.duration_s, 1), "reason": v.reason})
                    rel = (str(v.receipt.relative_to(root))
                           if v.receipt and v.receipt.exists() and root in v.receipt.parents
                           else None)
                    if v.result != "instrument-error":
                        self._append_ledger({
                            "ts": _iso(_now()), "role": ROLE, "event": "gated", "id": c.ident,
                            "base_sha": c.base_sha, "actor": "integration-adapter",
                            "detail": {"result": "pass" if v.result == "pass" else "fail",
                                       "receipt": rel or "", "exit_code": v.exit_code,
                                       "candidate_sha": c.tip_sha, "merge_base_sha": c.base_sha,
                                       "duration_s": round(v.duration_s, 1)}})
                    if v.result == "pass":
                        # NOT a `merged` event. Nothing merged. Writing `merged`
                        # here would be the favourable-absence defect verbatim.
                        self.lines.append(
                            f"READY TO LAND (MANUAL): {c.branch} @ {c.tip_sha[:12]} passed the gate. "
                            f"This version never pushes to main — a human lands it. Receipt: {rel}")
                        self._alert(f"{c.branch} @ {c.tip_sha[:12]} PASSED the gate and is ready to "
                                    "land manually (this adapter never pushes to main)")
                    elif v.result == "candidate-defect":
                        self.reject(c, Refusal(v.reason, "candidate-defect", "replan"), rel)
                    else:
                        self.instrument_inquiry(c, v, fingerprint)

        self.summary["gated"] = gated

        # --- publish the queue ------------------------------------------------
        # `bridge/publish_queue.py:write_atomic()` is the ONE runtime call site
        # that ever writes state/queue.json — reached either from its own 300s
        # timer (com.threeloops.publish-queue.plist) or, as here, from this
        # adapter. This adapter used to carry its own near-identical
        # rebuild-and-write block; two independent writers racing the same
        # file is exactly the ambiguity this proposal closes.
        #
        # Rebuild a FRESH ledger view here (the ledger can have advanced since
        # `ledger` was read at the top of this method — admissions/gates/
        # rejections happened in between) and enrich it exactly as the old
        # inline block did: `kinds_from_disk` sees only what is currently on
        # disk, so kinds/titles learned about in-flight candidates this very
        # iteration are merged in on top, or a candidate processed and then
        # moved out of `candidates/` in this same tick would read back as
        # "unknown".
        fresh = LedgerView.build(root)
        fresh.kinds.update(kinds_from_disk(root))
        fresh.kinds.update(ledger.kinds)
        fresh.titles.update(ledger.titles)

        # `write=self.apply` preserves the dry-run contract this run banner
        # promises ("dry-run writes nothing"): `_write_json` used to no-op
        # when `apply` was false, and `publish_from` must be told the same
        # thing explicitly now that it is not `_write_json` doing the writing.
        # A local import avoids making module import order matter
        # (publish_queue also imports this module, at call time only, so the
        # cycle never resolves an unset name either direction).
        from bridge import publish_queue as _publish_queue
        q, msg = _publish_queue.publish_from(root, fresh, gov.wip_cap, write=self.apply)
        if q is None:
            self._alert(msg)
        else:
            self.summary["queue"] = {"wip": q.get("wip"),
                                     "wip_degraded": q.get("wip_degraded", False),
                                     "items": len(q["items"]),
                                     "rebuilt_at": q["rebuilt_at"]}
            # REPAIR ROUND 5, IN SCOPE 2: the top-level `summary["wip"]` set
            # above (see `current_wip`) was computed from the ledger read at
            # the TOP of this iteration, before this tick's own refusal/
            # admission events were appended, and its status filter excludes
            # "blocked" entirely — so one iteration could report
            # `summary["wip"] == 0` while the published `state/queue.json`
            # (rebuilt from `fresh`, i.e. AFTER those events, via admitted/gated
            # statuses unconditionally and blocked iff `fresh.wip_entered`)
            # truthfully reported `wip: 1, status: blocked`. Two
            # operator-facing surfaces disagreeing about wip is worse than
            # one being wrong. `q.get("wip")` is the clean-replay source of
            # truth; degraded replay deliberately omits the key and therefore
            # reconciles to None instead of a guessed integer. (When `q is None`
            # — publish itself failed, already alerted above — there is no
            # published queue to agree with, so the pre-tick `current_wip`
            # is left as the best available estimate.)
            self.summary["wip"] = q.get("wip")
            self.summary["wip_degraded"] = q.get("wip_degraded", False)
            # The label must match what actually happened: `write=self.apply`
            # means a DRY-RUN tick computes but does not write, and a report
            # line reading "write state/queue.json" on a run that wrote
            # nothing is exactly the kind of favourable lie this whole
            # candidate exists to stop shipping.
            label = "write state/queue.json" if self.apply else "state/queue.json (NOT written, dry-run)"
            self.lines.append(f"{label}  ({msg})")

        # The prompt says check the governor AFTER an iteration too — that is
        # what catches a spike that arrived mid-gate.
        after = read_governor(root)
        if not after.ok:
            self.summary["governor_after"] = after.stops
            self._alert("governor limit bound DURING the iteration: " + "; ".join(after.stops))
        return 0


# ---------------------------------------------------------------------- lock


class Lock:
    """PID lockfile. Exactly one Integration instance may run at a time — it holds
    the only write lock on `main`, and two instances produce double admission and
    mid-gate landings that invalidate each other's receipts.

    Only taken under --apply: a dry run writes nothing, so it neither needs
    exclusion nor should it block a real one.
    """

    def __init__(self, path: Path, enabled: bool) -> None:
        self.path, self.enabled, self.held = path, enabled, False

    def __enter__(self) -> Lock:
        if not self.enabled:
            return self
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
                except (OSError, ValueError, json.JSONDecodeError, TypeError) as exc:
                    if isinstance(exc, PermissionError):
                        break            # alive, owned by another user
                    self.path.unlink(missing_ok=True)   # holder is gone: stale lock
                    continue
                break
        raise SystemExit("another Integration instance holds "
                         f"{self.path} — exiting rather than proceeding 'just to check'")

    def __exit__(self, *exc) -> None:
        if self.held:
            self.path.unlink(missing_ok=True)


# ---------------------------------------------------------------------- main


def render(summary: dict, lines: list[str], alerts: list[str]) -> None:
    print(json.dumps(summary, indent=2, default=str))
    for ln in lines:
        print(f"  {ln}")
    for a in alerts:
        print(f"  ALERT: {a}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Integration adapter (dry-run by default; NEVER pushes to main).")
    ap.add_argument("--loops-root", required=True, type=Path)
    ap.add_argument("--repo", type=Path, default=None,
                    help="the repo being worked on (default: <loops-root>/../..)")
    ap.add_argument("--gate-workspace", type=Path, default=None,
                    help="pinned one-writer gate workspace (default: <repo>-gate)")
    ap.add_argument("--once", action="store_true", help="one iteration, then exit")
    ap.add_argument("--apply", action="store_true",
                    help="actually admit, gate and record. Default is a dry run that writes "
                         "nothing and does not invoke the gate.")
    ap.add_argument("--dry-run", action="store_true",
                    help="explicit form of the default. If both are given, THIS WINS — a "
                         "command line that asks for both must resolve to the safe one.")
    ap.add_argument("--allow-remote-gate", action="store_true",
                    default=os.environ.get("THREELOOPS_ALLOW_REMOTE_GATE", "") == "1",
                    help="permit dispatching a gate to the twin when it is materially "
                         "quieter AND passes preflight (pin + evidence sync-back are "
                         "mandatory parts of that path). Also enabled by "
                         "THREELOOPS_ALLOW_REMOTE_GATE=1 in the environment, so a "
                         "proven path can be switched on without editing every caller.")
    ap.add_argument("--twin-gate-workspace", default=REMOTE_GATE_WORKSPACE)
    ap.add_argument("--twin-evidence-root", default=REMOTE_EVIDENCE_ROOT)
    args = ap.parse_args(argv)
    if args.dry_run:
        args.apply = False

    root: Path = args.loops_root.resolve()
    if not root.is_dir():
        print(f"no such queue: {root}", file=sys.stderr)
        return 2

    repo = (args.repo or root.parent.parent).resolve()
    if not (repo / ".git").exists():
        print(f"not a git repo: {repo} (pass --repo)", file=sys.stderr)
        return 2
    gate_ws = (args.gate_workspace or Path(str(repo) + "-gate")).resolve()
    schema_dir = Path(__file__).resolve().parent.parent / "schema"

    lock_path = root / "state" / "integration.lock"
    with Lock(lock_path, enabled=args.apply):
        while True:
            it = Integration(root, repo, gate_ws, schema_dir, args.apply,
                             allow_remote_gate=args.allow_remote_gate,
                             remote_gate_ws=args.twin_gate_workspace,
                             remote_evidence_root=args.twin_evidence_root)
            try:
                it.iterate()
            except Exception as exc:  # an adapter crash is an instrument error, never a verdict
                it.summary.setdefault("error", f"{type(exc).__name__}: {exc}")
                render(it.summary, it.lines, it.alerts)
                return 1
            render(it.summary, it.lines, it.alerts)
            if args.once:
                return 0
            time.sleep(TICK_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
