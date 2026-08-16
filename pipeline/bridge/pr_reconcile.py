#!/usr/bin/env python3
"""GitHub PR reconciler for the loop queue. See BRIDGE.md / github_bridge.py.

github_bridge.py mirrors external GitHub PRs into ``findings/`` the moment they
open (``finding_from_pr``, ~line 168). Nothing previously updated the queue
copy when the PR was later merged or closed ON GITHUB, so those findings sat
open forever even after the real PR resolved. This module sweeps ``findings/``
for PR-derived entries, asks GitHub for their current state in as few calls as
possible, and closes the ones GitHub has already resolved — so the queue stays
truthful about what still needs attention.

Design rules this file exists to honour:

* **Idempotent.** A finding whose id already has a ``rejected/<id>.json`` is
  skipped — never double-closed, never double-appended.
* **Claim-correct against every claim-respecting writer, not lock-correct
  against every writer (round 2, risk (b); see "Claims" below for the
  residual, stated once there).** A snapshot recheck alone cannot close the
  window between "another producer's terminal event could land" and "this
  reconciler writes its own" — narrowing that window is not eliminating it,
  and a reviewer proved it with a repro that injects a competing event
  synchronously inside the write call itself, which no claim of any kind can
  exclude. ``_close_finding`` and ``_repair_tombstone`` take an exclusive
  claim on the finding's id before doing anything, hold it across the
  terminal recheck and every write, and release it in a ``finally``.
* **Self-healing across its own two writes, ownership-aware, re-verified
  INSIDE the claim (round 2 finding #1; round 3 hardened further).**
  ``_close_finding`` appends the ledger event BEFORE writing the tombstone
  (reversed from a naive tombstone-then-ledger order): a crash between the
  two now leaves a ledger event with no tombstone, never a tombstone with no
  ledger event — and a finding orphaned that way (this reconciler's OWN
  prior ``rejected`` event exists, but no ``rejected/<id>.json`` followed it)
  gets its tombstone backfilled on the next run via ``_repair_tombstone``,
  counted separately as ``repaired``. A terminal event from ANOTHER producer
  (e.g. the Implementer's ``merged``) always wins and is never backfilled —
  enforced twice, not once: (a) ``_terminal_ledger_state`` computes
  ``foreign_ids`` and ``own_orphans`` as mutually exclusive by construction,
  so an id with a foreign terminal event can never reach the repair branch
  from the sweep's initial scan; and (b) ``_repair_tombstone`` re-reads
  terminal ownership a SECOND time, INSIDE its held claim, immediately
  before writing — round 3 fixed the gap where a foreign claim-respecting
  writer could record its OWN terminal event for this id strictly AFTER the
  sweep's initial snapshot but BEFORE this repair's claim was granted, which
  check (a) alone could not see.
* **Fail-soft per PR, fail-hard on the run.** A `gh` error on one PR must not
  abort the sweep (``RuntimeError`` -> count it, keep going). Auth, quota, or
  suspension is TERMINAL for the whole run (bridge doctrine — see
  ``github_bridge.TerminalError``): stop the sweep immediately and report what
  happened via the returned ``error`` key. The CLI wiring in
  ``github_bridge.py`` treats that key as a hard failure — ``ok: false`` and a
  nonzero exit — never a silent ``ok: true``. Never retry in a loop.
* **The source repo must match.** A finding's ``source_url``/``source_ref``
  names an ``owner/repo``; that is compared against the ``repo`` argument
  before any lookup. A same-numbered PR in a different repository must never
  be used to judge this finding — mismatches are counted and skipped, never
  looked up.
* **A malformed id is refused, never normalized.** Every id this module
  writes anywhere is validated against the schema's own pattern
  (``^sha256:[0-9a-f]{64}$``) before any write; a bad id is counted as an
  error and the finding is left alone.
* **Never delete ``findings/``.** Retention/janitor sweeps terminal artifacts;
  deleting the source file here is not this module's job.
* **``role: "external"``, never ``"integration"``, on everything this writes.**
  The PR resolved on GitHub, outside any loop, so the record must say so
  (CONTRACT.md SS1). ``actor`` is ``"github-reconciler"`` so a human reading the
  ledger can tell this closure apart from a Planner or Reviewer decision.
* **A PR still OPEN is left untouched.** GitHub's reported state is the only
  thing this module consults — no content-based judgment of an open PR. (A
  separately-authored landed-detection feature was carved back out of this
  module pending its own review; see the doctrine note below.)

Schema note (schema/ledger-event.schema.json): ``detail.class`` on a
``rejected`` event is constrained to an enum — ``candidate-defect``,
``instrument-error``, ``blocked-on-human``, ``stale-base``, ``answered`` —
none of which name "resolved by an external event". The closest fit is
``answered``: CONTRACT.md SS4 already uses it for the case where an artifact's
open question was settled by something outside the artifact itself (there, a
proposal answering an inquiry; here, GitHub resolving the PR). ``remedy`` is
``"drop"``: there is nothing to resubmit, the underlying PR is gone from the
work queue's perspective.

Claims — a NARROWED variant of CONTRACT.md SS6, not "exactly what SS6
prescribes"
-----------------------------------------------------------------------

``_close_finding`` and ``_repair_tombstone`` take an ``O_EXCL`` claim marker
under ``claims/<id>.claim`` before writing anything, reusing CONTRACT.md SS6's
own primitive rather than inventing a new CAS/serialization protocol a
reviewer proposed and the operator explicitly declined. It is a NARROWED
variant of SS6, not a faithful implementation of its full protocol — three
concrete differences, each because the write this protects takes
single-digit milliseconds, not the open-ended duration a real work-claim
covers:

* **No ``claimed``/``released`` ledger events.** SS6 says append ``claimed``
  on acquire and ``released`` on release; this module does not, on purpose —
  a run that closes six findings would otherwise add twelve ledger lines to
  arbitrate operations the marker file alone already arbitrates. Ledger
  events are a record, never the arbiter (SS6's own words); the successful
  ``O_EXCL`` create is the ONLY arbiter of ownership here, same as everywhere
  else in the queue.
* **A 300-second fixed TTL, not "your realistic worst case for that item".**
  SS6 sizes ``expires_at`` per-item; here it is fixed at
  ``CLAIM_TTL_SECONDS`` because the write this protects takes milliseconds,
  and a leaked claim must resolve well inside one reconciler sweep (900s),
  never wait on the janitor's 24h retention pass.
* **Atomic RENAME-takeover to reclaim an expired marker, never a blind
  unlink.** SS6's own steal recipe ("Stealing an expired claim") stats the
  marker twice and compares before unlinking, specifically because a blind
  unlink can destroy a marker a THIRD claimant only just created live
  (CONTRACT.md:410 names this exact failure). An earlier version of this
  module's reclaim did exactly that — unlink-then-recreate — and a
  concurrency repro showed two reclaimers both believing they won. The fix
  here does not reproduce SS6's stat/compare dance; it uses a stronger,
  simpler primitive: ``Path.rename()`` is atomic at the filesystem level, so
  a reclaim renames the STALE marker to
  ``claims/<id>.claim.stale-<pid>-<ts>`` before creating a fresh one at the
  original path. At most ONE concurrent reclaimer's rename can ever succeed
  on a given stale marker — every other one's rename raises
  ``FileNotFoundError`` on an already-moved source and backs off having
  touched nothing. The marker at the claim path is never unlinked during a
  reclaim, only renamed away by whichever single caller wins, or left
  completely alone. The renamed-away ``.stale-*`` artifact is deleted in the
  same ``finally`` that releases this reconciler's own claim.

**The accepted residual, stated once:** none of the above makes this module
race-free against a writer that ignores the claim protocol entirely — no
claim, implemented any way, can exclude a writer that never checks for one.
Nothing in this codebase today requires the Implementer's own
``merged``-recording code to take this claim before writing. That is a
queue-level contract gap (either every terminal-event writer adopts this
convention, or no claim protects the queue against all of them), not a defect
in this module's OWN claim logic, which is correct for every writer that DOES
respect it.

Landed-detection doctrine (deferred feature — carved out, round 3)
----------------------------------------------------------------------

A concurrent, separately-authored feature added opt-in content-based
landed-detection to this module (``land_detector``/``_land_finding``/
``_land_pass``, plus ``bridge/land_detect.py``, ``bridge/close_on_land.py``,
and ``github_bridge.py``'s ``--land-detect`` wiring) while this file was
mid-review. Round-3 review (``.fusion/REVIEW-r3.md`` and its ``critic-last-r3.txt``
findings) found it not yet safe to ship: false-positive close classes (a
mode-only change and a rename-with-source-retained are both misclassified as
landed), a self-test gap (an empty-tip main commit skips the positive
canary), and an outward-facing GitHub-close tool the operator has not
authorized. The coordinator's ruling: carve the feature back out of this
module's three files (this one, ``github_bridge.py``'s wiring, and its
tests) and leave it to its authoring lane to harden —
``bridge/land_detect.py``, ``bridge/close_on_land.py`` and
``tests/test_land_detect.py`` are UNTOUCHED and remain in the tree for that
purpose. This module currently closes on GitHub-reported state only, full
stop, exactly as it did before the feature existed.

**Doctrine, retained for whoever re-lands the feature:** GitHub's ``MERGED``
state only catches PRs merged through GitHub; work here also lands by
cherry-pick, rebase, or re-authoring inside a train branch, so a PR can be
fully superseded while GitHub still calls it OPEN. If landed-detection is
reintroduced, it MUST classify by ``git cherry`` / patch-id content
equivalence, never by SHA ancestry — SHA ancestry gets real cases in this
repo wrong (PRs #19, #38, #60, #77 are claimed to land by patch-id while
GitHub still reports them OPEN; PR #89 looks superficially similar but is
claimed to be genuinely unlanded, and a same-shape check has to be able to
tell those apart). Beyond that starting point, read ``.fusion/REVIEW-r3.md``
in full before re-landing this feature — it names concrete false-positive
classes a naive patch-id-only check still gets wrong (a mode-only change, a
rename whose source path main independently retains), a self-test gap, and
an outward-close safety gap, none of which this doctrine note repeats in
detail.

Usage (library only — no CLI here; wired into ``github_bridge.py --reconcile``):
    from pr_reconcile import reconcile_once
    counts = reconcile_once("owner/name", Path("/path/to/var/loopqueue"))
"""

from __future__ import annotations

import calendar
import json
import os
import re
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from github_bridge import TerminalError, _append_ledger, _atomic_write, _gh  # noqa: E402
from ledger_read import parse_events  # noqa: E402


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


TTL_SECONDS = 7 * 24 * 3600  # ~7 days, matching other rejected tombstones' TTLs
PR_REF_RE = re.compile(r"github\.com/(?P<repo>[^/\s]+/[^/\s]+?)/pull/(?P<number>\d+)(?:$|[/?#])")
ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
RECONCILER_ACTOR = "github-reconciler"
# `completed` is terminal alongside rejected/merged (ruling D13a). It must be
# recognised here so the reconciler treats an already-`completed` id as terminal
# and never stamps a second (`rejected`) terminal event over it — a double
# terminal is exactly what integrity.exactly_one_terminal_event flags.
TERMINAL_LEDGER_EVENTS = ("rejected", "merged", "completed", "closed")

# A close takes milliseconds; a leaked claim must not park a finding anywhere
# near the 900s poll interval. See the "Claims" section of the module
# docstring for the full design rationale.
CLAIM_TTL_SECONDS = 5 * 60
# Matches janitor.py's own CLAIM_GRACE_SECONDS: a marker whose body has not
# landed yet (O_EXCL creates it empty, the body follows a moment later) is a
# claim mid-creation, never an orphan, until this much time has passed.
CLAIM_EMPTY_GRACE_SECONDS = 600


# ---------------------------------------------------------------- primitives


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _expires_at_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + TTL_SECONDS))


def _pr_ref(finding: dict) -> tuple[str, int] | None:
    """Extract (owner/repo, PR number) from source_url or payload.source_ref.

    Returns None if neither field carries a parseable GitHub PR URL. Deliberately
    does NOT fall back to a bare number anywhere else in the payload — the URL
    is the only field that also carries the repo, and the repo is exactly what
    finding #3 says must never be assumed.
    """
    payload = finding.get("payload") or {}
    for candidate in (finding.get("source_url"), payload.get("source_ref")):
        if not candidate:
            continue
        m = PR_REF_RE.search(candidate)
        if m:
            return m.group("repo"), int(m.group("number"))
    return None


def _is_pr_derived(finding: dict) -> bool:
    payload = finding.get("payload") or {}
    if payload.get("source") == "external-pr":
        return True
    source_url = finding.get("source_url") or ""
    return "/pull/" in source_url


def _already_closed(root: Path, finding_id: str) -> bool:
    """True if a rejected/<id>.json tombstone already exists for this id."""
    stem = finding_id.replace(":", "_")
    return (root / "rejected" / f"{stem}.json").exists()


def _ids_with_terminal_ledger_event(root: Path) -> set[str]:
    """One pass over ledger.jsonl: every id carrying a terminal event
    (rejected, merged, completed, or closed — see TERMINAL_LEDGER_EVENTS).

    Tolerates a torn final line after a crash (schema SS5) by skipping lines
    that fail to parse rather than raising. Called both for the sweep's
    initial scan AND, again, as the per-id race recheck immediately before
    ``_close_finding`` writes anything — ledger.jsonl only grows, so a second
    full read is correct, if not the cheapest possible check; "cheap enough"
    matters more here than "as cheap as possible".
    """
    ids: set[str] = set()
    ledger_path = root / "ledger.jsonl"
    if not ledger_path.exists():
        return ids
    with open(ledger_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            # A valid-JSON NON-object (null/123/"s"/[]/true) parses fine and
            # has no .get — it raised AttributeError here, not JSONDecodeError.
            # And one physical line can carry MORE than one event, so a terminal
            # event can sit BEHIND another one on the same line: missing it here
            # makes an already-closed id look open and invites a second closure.
            for event in parse_events(line)[0]:
                if event.get("event") in TERMINAL_LEDGER_EVENTS and event.get("id"):
                    ids.add(event["id"])
    return ids


def _terminal_ledger_state(root: Path) -> tuple[set[str], dict[str, dict]]:
    """One pass over ledger.jsonl, split by who owns the terminal event.

    Returns (foreign_ids, own_orphans):

      foreign_ids  - ids with a terminal event (rejected or merged) authored
        by anyone OTHER than this reconciler. A foreign terminal event always
        wins over everything this module might otherwise do with that id —
        repair included. (Round 2, new finding #1: the repair path used to
        run BEFORE this check, so an id closed by BOTH this reconciler's own
        orphaned ``rejected`` AND another producer's ``merged`` got a
        reconciler tombstone written over the foreign closure. Computing
        ``foreign_ids`` and ``own_orphans`` as mutually exclusive here, and
        having ``reconcile_once`` check ``foreign_ids`` first, makes that
        ordering bug structurally impossible rather than merely reordered.)
      own_orphans  - id -> this reconciler's own ``rejected`` event, for ids
        that have NO tombstone yet AND (by construction) no foreign terminal
        event either. Only these are eligible for the repair path.
    """
    foreign: set[str] = set()
    own_rejected: dict[str, dict] = {}
    ledger_path = root / "ledger.jsonl"
    if not ledger_path.exists():
        return foreign, own_rejected
    with open(ledger_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            # A valid-JSON NON-object (null/123/"s"/[]/true) parses fine and
            # has no .get — it raised AttributeError here, not JSONDecodeError.
            # Every event on the line: a FOREIGN terminal event hidden behind
            # another on one physical line is the worst thing this function can
            # miss — foreign_ids is what stops the repair path writing a
            # reconciler tombstone over somebody else's closure.
            for event in parse_events(line)[0]:
                if event.get("event") not in TERMINAL_LEDGER_EVENTS or not event.get("id"):
                    continue
                fid = event["id"]
                if event.get("event") == "rejected" and event.get("actor") == RECONCILER_ACTOR:
                    own_rejected[fid] = event
                else:
                    foreign.add(fid)
    # Only ids with no tombstone AND no foreign terminal event are orphans.
    own_orphans = {
        fid: ev for fid, ev in own_rejected.items()
        if fid not in foreign and not _already_closed(root, fid)
    }
    return foreign, own_orphans


# -------------------------------------------------------------------- claims


def _parse_iso_epoch(ts: str) -> float | None:
    """RFC3339 UTC 'Z' timestamp -> epoch seconds, or None if unparseable.

    calendar.timegm (not time.mktime) because these timestamps are always
    UTC and mktime applies the LOCAL timezone offset — silently wrong on any
    host not set to UTC.
    """
    try:
        return calendar.timegm(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ"))
    except (ValueError, TypeError):
        return None


def _claim_path(root: Path, finding_id: str) -> Path:
    stem = finding_id.replace(":", "_")
    return root / "claims" / f"{stem}.claim"


def _write_claim_marker(path: Path) -> None:
    """O_EXCL-create the marker with its body written immediately (CONTRACT.md
    SS6's empty-marker rule: O_EXCL creates the file empty, so the body must
    land in the same breath, not "eventually")."""
    body = json.dumps({
        "actor": RECONCILER_ACTOR,
        "at": _now_iso(),
        "expires_at": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + CLAIM_TTL_SECONDS)
        ),
    }).encode()
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    try:
        os.write(fd, body)
        os.fsync(fd)
    finally:
        os.close(fd)


def _acquire_claim(root: Path, finding_id: str) -> tuple[Path, Path | None] | None:
    """O_EXCL-create a micro-claim marker for finding_id. See the module
    docstring's "Claims" section for the full design rationale.

    Returns ``(marker_path, stale_path)`` if acquired — ``stale_path`` is the
    renamed-away former marker, present only when this call RECLAIMED an
    expired one, and must be deleted once the caller releases its own claim
    (``_claim`` does this). Returns ``None`` if a LIVE claim is already held —
    the caller must not write anything and should simply try again next sweep.
    """
    (root / "claims").mkdir(parents=True, exist_ok=True)
    path = _claim_path(root, finding_id)

    try:
        _write_claim_marker(path)
        return path, None
    except FileExistsError:
        pass

    # A marker already exists. stat() FIRST and keep that result regardless
    # of what happens next — a JSONDecodeError on the read below must NOT
    # reset the age basis to "now" (that bug let an old empty marker never
    # age past its grace period, since every attempt restarted the clock).
    try:
        st = path.stat()
    except OSError:
        return None  # vanished between our O_EXCL failure and this stat(); try again next sweep

    existing: dict | None
    expires: float | None
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(body, dict):
            # Valid JSON, wrong shape (e.g. `[]`) — evidence-wise identical to
            # unparseable: neither names an actor or an expiry, so both take
            # the same empty-marker grace path, not a crash at .get().
            raise ValueError("claim marker body is valid JSON but not an object")
        existing, expires = body, _parse_iso_epoch(body.get("expires_at", ""))
    except (OSError, json.JSONDecodeError, ValueError):
        existing, expires = None, None

    if existing is None or expires is None:
        # Empty or unparseable: CONTRACT.md SS6's empty-marker rule — treat
        # as held until CLAIM_EMPTY_GRACE_SECONDS past the ORIGINAL mtime,
        # never before, and never reset by a failed read.
        age = time.time() - st.st_mtime
        if age <= CLAIM_EMPTY_GRACE_SECONDS:
            return None
        # Unparseable/wrong-shape AND past grace: fall through and reclaim.
    elif time.time() < expires:
        return None  # live claim, not ours

    # Genuinely expired (or unparseable-and-past-grace): reclaim. rename()'s
    # own exclusivity is NOT sufficient by itself here — measured
    # empirically, not just reasoned about: even with a stat-compare
    # immediately before the rename (CONTRACT.md SS6's own steps 1-3), a
    # SLOWER reclaimer's `rename()` call can still execute — due to ordinary
    # GIL/OS scheduling delay between "decided to reclaim" and "the syscall
    # actually runs" — strictly AFTER a FASTER reclaimer has already renamed
    # AND recreated. `rename()` does not know or care what currently occupies
    # its source path; it just moves whatever is there. So the slow
    # reclaimer's "takeover" ends up moving the FAST reclaimer's brand-new
    # LIVE marker away, not the stale one it originally identified — the
    # exact CONTRACT.md:410 failure this design exists to avoid, just
    # relocated from unlink to rename. The fix: serialize the ENTIRE reclaim
    # critical section (re-verify, rename, recreate) behind a small,
    # short-lived, PER-ID lock acquired with O_EXCL — the one primitive
    # proven reliable, under every scenario tested here, for "exactly one
    # winner". At most one reclaimer may be mid-reclaim for a given id at a
    # time; every other one backs off immediately (same as `claim_held`) and
    # tries again next sweep.
    reclaim_lock = path.with_name(path.name + ".reclaiming")
    try:
        fd = os.open(reclaim_lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        os.close(fd)
    except FileExistsError:
        return None  # another reclaimer is already mid-reclaim for this id

    try:
        # Re-verify nothing changed between our read above and acquiring the
        # reclaim lock — a FRESH O_EXCL acquirer (not going through this
        # reclaim path at all, e.g. the marker's original crashed holder
        # cleanly recovering) could have recreated a live marker meanwhile.
        try:
            st2 = path.stat()
        except OSError:
            return None
        if (st2.st_ino, st2.st_mtime_ns) != (st.st_ino, st.st_mtime_ns):
            return None  # changed since our first read: someone else already acted

        # RENAME-takeover, never a blind unlink (CONTRACT.md:410 forbids it
        # by name — a blind unlink can destroy a THIRD claimant's freshly
        # created LIVE marker, not just the stale one this reclaimer read).
        # Now genuinely exclusive: we are the only reclaimer for this id.
        # PID alone is not enough for the stale filename either: two
        # THREADS in the same process share a PID, so include thread ident
        # too — unique among currently-alive threads, so pid+ident+ts
        # together are unique across both processes and threads.
        stale = path.with_name(
            path.name + f".stale-{os.getpid()}-{threading.get_ident()}-{int(time.time() * 1000)}"
        )
        try:
            path.rename(stale)
        except FileNotFoundError:
            return None  # someone else already reclaimed or released it; back off

        try:
            _write_claim_marker(path)
            return path, stale
        except FileExistsError:
            # Should not happen while we hold the reclaim lock — nothing
            # else should be creating a fresh marker at this path right
            # now — but never leave the renamed-away file orphaned under
            # our name if it somehow does.
            try:
                stale.unlink()
            except FileNotFoundError:
                pass
            return None
    finally:
        try:
            reclaim_lock.unlink()
        except FileNotFoundError:
            pass


@contextmanager
def _claim(root: Path, finding_id: str):
    """Hold this reconciler's micro-claim on finding_id for the with-block;
    release (unlink both the live marker and any renamed-away stale
    artifact) in a finally regardless of how the block exits.

    Yields True if acquired, False if a live claim is already held — callers
    must not enter their write logic when this yields False.
    """
    result = _acquire_claim(root, finding_id)
    marker, stale = result if result is not None else (None, None)
    try:
        yield marker is not None
    finally:
        for p in (marker, stale):
            if p is not None:
                try:
                    p.unlink()
                except FileNotFoundError:
                    pass


# ---------------------------------------------------------------- GitHub


def _fetch_pr_states(repo: str, numbers: set[int]) -> dict[int, dict]:
    """Resolve every PR number to its current {number, state, mergedAt,
    closedAt} in as few `gh` calls as possible.

    One `gh pr list --state all` covers the whole repo up to --limit; any
    numbers it misses (repo has more PRs than the limit, or a PR aged out of
    the default listing order) fall back to one `gh pr view` call each. A
    RuntimeError on the list call degrades to per-PR lookups rather than
    failing the whole sweep; a TerminalError always propagates (auth/quota is
    terminal for the run, never fail-soft).
    """
    states: dict[int, dict] = {}
    if not numbers:
        return states

    limit = max(200, len(numbers) * 4)
    try:
        prs = _gh(
            ["pr", "list", "--repo", repo, "--state", "all",
             "--json", "number,state,mergedAt,closedAt,url,title,headRefName,headRefOid",
             "--limit", str(limit)]
        )
    except TerminalError:
        raise
    except RuntimeError:
        prs = []

    for pr in prs or []:
        states[pr["number"]] = pr

    for num in numbers - states.keys():
        try:
            pr = _gh(
                ["pr", "view", str(num), "--repo", repo,
                 "--json", "number,state,mergedAt,closedAt,url,title"]
            )
        except TerminalError:
            raise
        except RuntimeError:
            continue  # counted by the caller against the finding it belongs to
        if pr:
            states[num] = pr

    return states


# ---------------------------------------------------------------- closing


def _build_tombstone(finding_id: str, pr_number: int, state: str, pr: dict) -> dict:
    now = _now_iso()
    expires = _expires_at_iso()
    if state == "MERGED":
        when = pr.get("mergedAt") or now
        reason = f"PR #{pr_number} merged on GitHub {when}"
    else:
        when = pr.get("closedAt") or now
        reason = f"PR #{pr_number} closed without merge on GitHub {when}"
    return {
        "id": finding_id,
        "rejected_at": now,
        "by": RECONCILER_ACTOR,
        "class": "answered",
        "remedy": "drop",
        "expires_at": expires,
        "reason": reason,
        "note": (
            "closed by the PR reconciler: GitHub already resolved this PR, so "
            "the queue copy was stale, not wrong. Re-raising is dropped at "
            "source until the TTL expires (CONTRACT.md SS7)."
        ),
    }


def _close_finding(root: Path, finding: dict, pr_number: int, state: str, pr: dict,
                    *, dry_run: bool) -> tuple[dict | None, str]:
    """Build (and, unless dry_run, write) the ledger event + rejected/ tombstone.

    Returns ``(tombstone_or_None, outcome)`` where ``outcome`` is one of:
      "closed"      - written (or, under dry_run, would be)
      "race"        - another producer's terminal event landed for this id
                       since the sweep's scan; nothing written
      "claim_held"  - another writer holds this id's micro-claim right now;
                       nothing attempted here — try again next sweep

    Write order is ledger event FIRST, tombstone SECOND (finding #2): a crash
    between the two leaves a repairable orphan (ledger event, no tombstone),
    never an unrecoverable one (tombstone, no ledger event) that
    ``_already_closed`` would then skip forever.

    A dry run acquires NO claim — creating a marker is a real filesystem
    write, which a preview must never perform — and does only the read-only
    race recheck. That means a dry run's report can go stale between its
    recheck and a REAL run's claim-protected one in a way a real run cannot;
    that asymmetry is inherent to "preview vs. commit", not new here.
    """
    finding_id = finding["id"]

    if dry_run:
        if finding_id in _ids_with_terminal_ledger_event(root):
            return None, "race"
        return _build_tombstone(finding_id, pr_number, state, pr), "closed"

    with _claim(root, finding_id) as acquired:
        if not acquired:
            return None, "claim_held"

        # Race recheck (finding #1), now performed WHILE HOLDING the claim —
        # this closes the round-1 window where a terminal event could land
        # between a bare recheck and the writes below. It does not, and
        # cannot by itself, close the window against a writer that never
        # takes this claim at all (round 2, risk (b) — see the module
        # docstring's "Claims" section): only against every OTHER
        # claim-respecting writer, including a second instance of this same
        # reconciler.
        if finding_id in _ids_with_terminal_ledger_event(root):
            return None, "race"

        tombstone = _build_tombstone(finding_id, pr_number, state, pr)
        _append_ledger(
            root,
            {
                "ts": tombstone["rejected_at"],
                "role": "external",
                "event": "rejected",
                "id": finding_id,
                "actor": RECONCILER_ACTOR,
                "detail": {
                    "reason": tombstone["reason"],
                    "class": "answered",
                    "expires_at": tombstone["expires_at"],
                },
            },
        )
        stem = finding_id.replace(":", "_")
        _atomic_write(root / "rejected" / f"{stem}.json", tombstone)
        return tombstone, "closed"


def _build_repair_tombstone(finding_id: str, event: dict) -> dict:
    detail = _event_detail(event)
    return {
        "id": finding_id,
        "rejected_at": event.get("ts") or _now_iso(),
        "by": event.get("actor", RECONCILER_ACTOR),
        "class": detail.get("class", "answered"),
        "remedy": "drop",
        "expires_at": detail.get("expires_at") or _expires_at_iso(),
        "reason": detail.get(
            "reason",
            "repaired: a prior reconciler run's ledger event existed with no tombstone",
        ),
        "note": (
            "tombstone repaired by a later reconciler run: the ledger event "
            "from a previous run existed but the tombstone write never "
            "completed (crash between the two writes). No new ledger event "
            "was written; this only backfills the missing file."
        ),
    }


def _repair_tombstone(root: Path, finding_id: str, event: dict,
                       *, dry_run: bool) -> tuple[dict | None, str]:
    """Backfill a tombstone for this reconciler's own orphaned ledger event.

    No new ledger event is written here — the ledger already carries the
    terminal event; only the missing tombstone half is completed. Claim-
    protected the same way as ``_close_finding`` and for the same reason
    (design decision #1): without it, two reconciler runs racing the same
    orphan could both pass ``_already_closed`` (False) and both call
    ``_atomic_write`` — harmless in outcome since both would compute
    byte-identical content from the same source ledger event, but the claim
    keeps this write on the same footing as every other one in this module
    rather than being the one silent exception.

    Returns ``(tombstone_or_None, outcome)``; outcome is one of:
      "repaired"       - the tombstone was written (or, under dry_run, would be)
      "claim_held"      - another writer holds this id's claim right now
      "already_closed"  - another racer's repair or close won the claim first
      "foreign_won"     - a FOREIGN claim-respecting writer recorded its OWN
                           terminal event for this id strictly AFTER the
                           sweep's initial snapshot (the caller's `event`
                           argument) but BEFORE this claim was granted — that
                           foreign event wins; nothing is written here
    """
    if dry_run:
        return _build_repair_tombstone(finding_id, event), "repaired"

    with _claim(root, finding_id) as acquired:
        if not acquired:
            return None, "claim_held"
        if _already_closed(root, finding_id):
            return None, "already_closed"  # another racer's write landed first
        # Re-check terminal ownership INSIDE the held claim, not just at the
        # sweep's initial snapshot (round 3 fix): a foreign claim-respecting
        # writer may have recorded ITS OWN terminal event for this id after
        # the snapshot the caller is repairing from, but before this claim
        # was granted. That event wins outright; this repair must not paper
        # over it with a `rejected` tombstone.
        foreign_ids, _own_orphans = _terminal_ledger_state(root)
        if finding_id in foreign_ids:
            return None, "foreign_won"
        tombstone = _build_repair_tombstone(finding_id, event)
        stem = finding_id.replace(":", "_")
        _atomic_write(root / "rejected" / f"{stem}.json", tombstone)
        return tombstone, "repaired"


# ---------------------------------------------------------------- the sweep


def reconcile_once(
    repo: str,
    root: Path,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Close every PR-derived finding whose PR is MERGED or CLOSED on GitHub.
    A PR GitHub still reports OPEN is left completely untouched — no
    content-based judgment of it (see the module docstring's "Landed-detection
    doctrine" section for the carved-out feature that would have added one).

    Returns counts:
      closed        - newly closed this run (or, under dry_run, would be)
      skipped       - already terminal (has a rejected/ tombstone, or a
                       foreign terminal ledger event)
      open          - PR still open on GitHub — untouched
      errors        - malformed finding, invalid id, unparseable PR reference,
                       or a `gh` failure resolving one PR (fail-soft; the
                       sweep keeps going)
      repaired      - this reconciler's own orphaned ledger event got its
                       missing tombstone backfilled (finding #2), and no
                       foreign terminal event existed for the same id at
                       either check (round 2 finding #1 at the sweep's
                       initial scan; round 3 fix inside the held claim too)
      race_skipped  - another producer's terminal event landed for this id
                       between the sweep's scan and the write (finding #1)
      claim_held    - another writer holds this id's micro-claim right now
                       (round 2, risk (b)); nothing attempted, try again next
                       sweep
      repo_mismatch - the finding's source repo does not match `repo`
                       (finding #3)
      details       - one entry per non-trivial outcome above, always
                       carrying "id"; error-shaped entries also carry "error"

    A terminal gh error (auth/quota/suspension) stops the sweep immediately;
    the returned dict carries an "error" key instead of raising. The CLI
    wiring in github_bridge.py treats that key as a hard failure.
    """
    counts: dict[str, Any] = {
        "closed": 0,
        "skipped": 0,
        "open": 0,
        "errors": 0,
        "repaired": 0,
        "race_skipped": 0,
        "claim_held": 0,
        "repo_mismatch": 0,
        "details": [],
    }

    findings_dir = root / "findings"
    if not findings_dir.is_dir():
        return counts

    foreign_terminal_ids, own_orphans = _terminal_ledger_state(root)

    candidates: list[tuple[dict, int]] = []
    for path in sorted(findings_dir.glob("*.json")):
        try:
            finding = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            counts["errors"] += 1
            counts["details"].append({"id": path.stem, "error": "unreadable finding file"})
            continue

        if not _is_pr_derived(finding):
            continue

        finding_id = finding.get("id")
        if finding_id is None or finding_id == "":
            counts["errors"] += 1
            counts["details"].append({"id": path.stem, "error": "finding has no id"})
            continue

        # New finding #3 (round 2): validate TYPE before regex-matching — a
        # non-string id used to raise TypeError out of re.Pattern.match and
        # abort the whole sweep on one corrupt finding. Every
        # schema-nonmatching id, string or not, is now a per-finding error.
        if not isinstance(finding_id, str) or not ID_RE.match(finding_id):
            counts["errors"] += 1
            counts["details"].append({
                "id": finding_id,
                "error": "id is missing, non-string, or does not match "
                         "^sha256:[0-9a-f]{64}$; refused, not normalized",
            })
            continue

        if _already_closed(root, finding_id):
            counts["skipped"] += 1
            continue

        if finding_id in foreign_terminal_ids:
            # New finding #1 (round 2) fix: a foreign terminal event ALWAYS
            # wins, even if this reconciler ALSO has its own orphaned
            # `rejected` for the same id (mixed ownership). This check runs
            # BEFORE the repair branch on purpose — `_terminal_ledger_state`
            # guarantees `own_orphans` never contains an id also present
            # here, but the ordering is kept explicit rather than relied on
            # implicitly, so a future edit cannot silently invert it again.
            counts["skipped"] += 1
            continue

        # Finding #2 repair path: our own previous half-write, with no
        # foreign terminal event for this id (guaranteed above).
        if finding_id in own_orphans:
            _tombstone, outcome = _repair_tombstone(
                root, finding_id, own_orphans[finding_id], dry_run=dry_run
            )
            if outcome == "claim_held":
                counts["claim_held"] += 1
                counts["details"].append({
                    "id": finding_id,
                    "note": "repair deferred: another writer holds this id's claim",
                })
            elif outcome in ("already_closed", "foreign_won"):
                # "foreign_won" (round 3): a foreign claim-respecting writer's
                # own terminal event landed strictly between the sweep's
                # snapshot and this claim being granted — it wins, same
                # bucket as any other foreign-terminal skip.
                counts["skipped"] += 1
            else:
                counts["repaired"] += 1
                counts["details"].append({
                    "id": finding_id,
                    "note": "repaired: this reconciler's ledger event existed with no tombstone",
                })
            continue

        ref = _pr_ref(finding)
        if ref is None:
            # Finding #6: distinguishable from a healthy already-closed skip.
            counts["errors"] += 1
            counts["details"].append({
                "id": finding_id,
                "error": "no parseable PR reference in source_url or payload.source_ref",
                "reason": "unparseable-pr-ref",
            })
            continue

        pr_repo, num = ref
        if pr_repo.lower() != repo.lower():
            # Finding #3: never judge a finding from another repo's same-numbered PR.
            counts["repo_mismatch"] += 1
            counts["details"].append({
                "id": finding_id,
                "pr": num,
                "source_repo": pr_repo,
                "expected_repo": repo,
                "error": "finding's source repo does not match --repo; skipped, not looked up",
            })
            continue

        candidates.append((finding, num))

    if not candidates:
        return counts

    numbers = {num for _, num in candidates}
    try:
        states = _fetch_pr_states(repo, numbers)
    except TerminalError as exc:
        counts["error"] = str(exc)
        return counts

    for finding, num in candidates:
        finding_id = finding["id"]
        pr = states.get(num)
        if pr is None:
            counts["errors"] += 1
            counts["details"].append({
                "id": finding_id, "pr": num,
                "error": "gh could not resolve a state for this PR",
                "reason": "gh-lookup-failed",
            })
            continue

        state = (pr.get("state") or "").upper()
        if state == "OPEN":
            counts["open"] += 1
            continue
        if state not in ("MERGED", "CLOSED"):
            counts["errors"] += 1
            counts["details"].append({
                "id": finding_id, "pr": num,
                "error": f"unrecognised PR state {state!r}",
                "reason": "unrecognised-state",
            })
            continue

        _tombstone, outcome = _close_finding(root, finding, num, state, pr, dry_run=dry_run)
        if outcome == "claim_held":
            counts["claim_held"] += 1
            counts["details"].append({
                "id": finding_id, "pr": num, "state": state,
                "note": "close deferred: another writer holds this id's claim right now",
            })
            continue
        if outcome == "race":
            counts["race_skipped"] += 1
            counts["details"].append({
                "id": finding_id, "pr": num, "state": state,
                "note": "another producer's terminal event landed before this "
                        "reconciler could close it; left untouched",
            })
            continue

        counts["closed"] += 1
        counts["details"].append(
            {"id": finding_id, "pr": num, "state": state, "title": finding.get("title", "")}
        )

    return counts

