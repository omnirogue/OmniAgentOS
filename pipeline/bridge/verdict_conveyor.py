#!/usr/bin/env python3
"""The standing cross-lineage VERDICT CONVEYOR: one bounded, one-shot pass.

WHY THIS EXISTS (measured 2026-08-13). No standing role produces cross-lineage
build verdicts. The reviewer loop's own prompt forbids it, so verdict supply
collapsed from ~15/day to ~0-2/day while the gate logged 5,340
"risky diff requires a genuine cross-lineage build verdict" skips across 94
candidates, and 13 fully-built candidates died stale WITHOUT EVER BEING READ.
The gate is not wrong to refuse them -- `review_policy.approved_cross_lineage`
is the estate's real approval predicate and it is correct. What was missing was
anything that MANUFACTURES the input it asks for. This does exactly that, and
nothing else.

WHAT IT IS NOT. It is not a reviewer. It never forms an opinion about a diff,
never summarises one, and has no path that can emit a verdict string it
authored. It is a conveyor: it finds candidates the gate is refusing for want
of a verdict, pays for one read-only seat of a DIFFERENT lineage to actually
read the diff, and copies that seat's own final verdict line into the envelope
verbatim. If the seat says nothing usable, nothing is written. An absent
verdict is not an approval, and it is not a rejection either.

THIS IS A CLASS A SURFACE: it writes into `verdicts[]`, which is what decides
whether risky code lands. Every branch below fails CLOSED, and every skip is
named in the ledger, because the failure mode that matters here is not "the
conveyor did nothing" -- it is "the conveyor wrote an approval nobody gave".
The five rules that keep that impossible:

  1. **NEVER FABRICATE.** The stored `verdict` is the seat's own final line,
     copied byte-for-byte, and it must fully match the pinned grammar. A seat
     that timed out, crashed, or rambled produces NO entry. There is deliberately
     no default, no "assume approve on rc=0", and no summarisation step.
  2. **RECORD THE MODEL THAT ACTUALLY ANSWERED.** A dispatched seat is not
     proof of the lineage that replied -- this estate has already measured the
     substitution (a Gemini seat answering as Sonnet). The seat must self-declare
     `REVIEWER-MODEL:`, and that declaration may only ever DOWNGRADE trust: it
     can move the recorded lineage away from the one we dispatched, never
     toward it. If the resulting lineage equals the producer's, the verdict is
     still recorded (it is honest, and it is information) but it is NOT counted
     as a success and it cannot satisfy `approved_cross_lineage`.
  3. **BIND TO THE SHA.** The branch is re-resolved immediately before the
     write. If the tip moved while the seat was reading, the review is of code
     that is no longer there: the transcript is archived, the ledger says so,
     and NOTHING is written to the envelope.
  4. **NEVER REPAIR IDENTITY.** `verdicts[]` sits OUTSIDE `payload`, so
     appending to it cannot change `content_id(payload)` and therefore cannot
     change the envelope id. That invariant is verified BEFORE and AFTER every
     write. An envelope that is already unbound is skipped entirely -- a
     conveyor that "fixed" an id would be forging provenance.
  5. **A REJECTION IS A RESULT.** REQUEST-CHANGES is written, ledgered and
     alerted once. Discarding rejections would make the conveyor an
     approval-only pump, which is the same defect as fabricating.

LOOPING IS LAUNCHD'S JOB. This is a one-shot pass with a singleton lock; the
plist's StartInterval provides the cadence. There is no internal daemon loop,
so a hung seat cannot wedge a long-lived process, and the 20-minute per-seat
timeout is enforced with a process-GROUP kill (a provider CLI that spawns
children survives a bare terminate).
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

# `pipeline/` at the FRONT of sys.path, spelled exactly as every sibling daemon
# in this directory spells it: the policy that decides what "risky" and
# "approved" mean must come from the checkout being operated on, never from
# whatever tree happened to be on $PYTHONPATH.
PIPELINE_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = PIPELINE_DIR.parent
sys.path.insert(0, str(PIPELINE_DIR))

from bridge import gate_loop as GL  # type: ignore[import-not-found]  # noqa: E402, I001
from bridge.canonical import content_id  # type: ignore[import-not-found]  # noqa: E402
from bridge.integration import git  # type: ignore[import-not-found]  # noqa: E402
from bridge.ledger_read import iter_events  # type: ignore[import-not-found]  # noqa: E402
from bridge.ledger_write import append_event  # type: ignore[import-not-found]  # noqa: E402
from bridge.review_policy import (  # type: ignore[import-not-found]  # noqa: E402
    KNOWN_LINEAGES,
    approved_cross_lineage,
    risky_review_paths,
)
from bridge.review_policy import lineage as normalise_lineage  # type: ignore[import-not-found]  # noqa: E402

ACTOR = "verdict-conveyor"

#: Terminal ledger events. `GL.TERMINAL_EVENTS` is the shared authority and is
#: imported rather than retyped; `superseded` is added because the brief names
#: it and because a terminal set can only ever be made LARGER safely -- an
#: over-broad terminal set skips a candidate that did not need reviewing, while
#: a too-narrow one pays a seat to review something already dead.
#: (`superseded` is NOT in the ledger event enum today: supersession is carried
#: by a `supersedes` field on the newer artifact and terminalises the old one
#: with `rejected`/`completed`. It is honoured here defensively, at zero cost.)
TERMINAL_EVENTS = frozenset(GL.TERMINAL_EVENTS) | {"superseded"}

#: The one verdict grammar this conveyor will store. Anchored and whole-line on
#: purpose: "APPROVE the refactor once X is fixed" must never read as APPROVE.
#: The separator accepts em dash, en dash or hyphen because provider CLIs
#: normalise punctuation unpredictably and all three are already accepted by
#: `review_policy._APPROVAL_VERDICT_RE`, which is the predicate that ultimately
#: judges what is written here. Case-insensitive for DETECTION only -- the
#: stored string is always the seat's own bytes.
VERDICT_LINE_RE = re.compile(
    r"^(?:APPROVE(?:\s*[—–-]\s*(?:with[- ]nits|zero\s+blockers))?"
    r"|REQUEST-CHANGES:\s*\S.*)$",
    re.IGNORECASE,
)
#: The marker a decision MUST carry. Whole-line, anchored, never stripped of
#: decoration: a decorated line is not a declaration, and treating it as one is
#: what let a bulleted quotation inside a rejection be read as an approval.
_FINAL_VERDICT_RE = re.compile(r"^FINAL-VERDICT:\s*(?P<verdict>\S.*?)\s*$")
_REQUEST_CHANGES_RE = re.compile(r"^REQUEST-CHANGES:\s*(?P<reason>.+)$", re.IGNORECASE)
_REVIEWER_MODEL_RE = re.compile(r"^\s*REVIEWER-MODEL:\s*(?P<model>\S.*?)\s*$", re.IGNORECASE)

#: Model-name token -> lineage. Used ONLY to interpret a seat's self-declared
#: model, and only ever to move the recorded lineage AWAY from the dispatched
#: one. An unrecognised self-report is recorded as such and changes nothing.
_MODEL_LINEAGE_TOKENS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(?:gpt|codex|o[34]|sol|terra|luna)\b", re.IGNORECASE), "openai"),
    (re.compile(r"\b(?:claude|sonnet|opus|haiku|fable)\b", re.IGNORECASE), "anthropic"),
    (re.compile(r"\bgemini\b", re.IGNORECASE), "google"),
    (re.compile(r"\bgrok\b", re.IGNORECASE), "xai"),
    (re.compile(r"\b(?:kimi|moonshot)\b", re.IGNORECASE), "moonshot"),
)

#: Seat command resolution. Module-level so tests can point them at fakes.
#:
#: TILDE TRAP (measured 2026-08-13): a home path only expands in a SHELL. A
#: value like "~/.codex-third" handed to a child through `env` or written into
#: a plist is passed through literally, and the provider CLI then looks for a
#: directory called "~" in the cwd -- so the seat silently runs against the
#: wrong (or an empty) account home. Every home-rooted path here therefore goes
#: through `os.path.expanduser`, and this conveyor deliberately does NOT set
#: CODEX_HOME itself: `codexpick` owns account routing and sets it correctly.
CODEXPICK = Path(os.path.expanduser("~/.local/bin/codexpick"))
CODEX_MODEL = os.environ.get("OMNI_VERDICT_CODEX_MODEL", "gpt-5.6-sol")
GROK_MODEL = os.environ.get("OMNI_VERDICT_GROK_MODEL", "")

#: Prefix that makes a recorded lineage MECHANICALLY unusable as an approval:
#: `unverified:openai` is not in `KNOWN_LINEAGES`, so `approved_cross_lineage`
#: rejects it without needing to know this constant exists. The quarantine is
#: enforced by the shared predicate, not by a second copy of the policy.
QUARANTINE_PREFIX = "unverified:"

#: The ref every risk diff is taken from. Resolved out of the REPOSITORY, so no
#: envelope field -- tampered or merely dishonest -- can narrow the diff a
#: reviewer is shown while the approval still binds to the full head.
DEFAULT_INTEGRATION_REF = "main"

DEFAULT_SEAT_TIMEOUT_S = 20 * 60
DEFAULT_LOCK_TTL_S = 30 * 60
DEFAULT_MAX_CANDIDATES = 2
_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_IDENT = re.compile(r"^sha256:[0-9a-f]{64}$")


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _stamp(dt: datetime) -> str:
    return dt.strftime("%Y%m%dT%H%M%SZ")


def _short(ident: str) -> str:
    """The 12 hex chars this estate names artifacts by in logs and filenames."""
    return ident.split(":", 1)[-1][:12]


# --------------------------------------------------------------- singleton lock


@dataclass
class LockHandle:
    """A held singleton lock. `fd` stays open for the life of the pass."""

    fd: int
    path: Path
    took_over_from: dict[str, Any] | None = None


class LockUnsupported(RuntimeError):
    """The lock file lives somewhere `flock` cannot serialise. Refuse to run."""


#: errnos that mean "this filesystem does not do flock", as opposed to
#: "someone else holds it". Mixing the two is how a broken lock becomes an
#: invisible one.
_FLOCK_UNSUPPORTED = frozenset({errno.ENOTSUP, errno.EOPNOTSUPP, errno.EINVAL, errno.ENOLCK})


def acquire_lock(
    loops_root: Path,
    *,
    ttl_s: int = DEFAULT_LOCK_TTL_S,
    now: datetime | None = None,
) -> LockHandle | None:
    """Take the pass lock, or return None when a LIVE pass already holds it.

    `flock(LOCK_EX|LOCK_NB)` is the ONLY authority, and it is held for the whole
    pass. It is the right primitive precisely because the KERNEL releases it
    when the holder dies, so a crashed pass recovers with nobody reaping a
    stale file, and a live pass cannot be stolen from.

    THERE IS DELIBERATELY NO PID FALLBACK (cross-lineage review round 2,
    2026-08-13). A previous version consulted the marker's pid with
    `os.kill(pid, 0)` when flock said the file was free. On a filesystem where
    flock does not serialise -- the exact case a fallback is supposed to
    cover -- a pid from ANOTHER machine reads as "not running here", so the
    fallback did not protect the lock, it AUTHORISED THE STEAL, and two hosts
    would convey verdicts over the same candidates simultaneously. A liveness
    test that is only valid within one machine cannot adjudicate a lock that is
    only contended across machines.

    So the fallback is gone, and the filesystem must actually support flock: if
    it does not, this raises `LockUnsupported` rather than running unlocked.
    This daemon only ever runs on the local box, so that refusal costs nothing
    real -- and "the lock is not enforceable here" must never be reported in
    the same colour as "the lock is free".

    The marker JSON is now purely a RECORD (who holds it, since when, until
    when) for an operator reading the directory. Nothing branches on it.
    """
    stamp = now or _now()
    locks = loops_root / "locks"
    locks.mkdir(parents=True, exist_ok=True)
    path = locks / "verdict-conveyor.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        if exc.errno in _FLOCK_UNSUPPORTED:
            raise LockUnsupported(
                f"{path} is on a filesystem that does not support flock "
                f"({errno.errorcode.get(exc.errno or 0, exc.errno)}). The verdict "
                "conveyor writes approvals and refuses to run without enforceable "
                "mutual exclusion. Point --loops-root at local storage."
            ) from exc
        return None                                   # a live pass holds it
    try:
        raw = os.pread(fd, 65536, 0).decode("utf-8", errors="replace")
        prior: dict[str, Any] | None
        try:
            parsed = json.loads(raw) if raw.strip() else None
            prior = parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            prior = None                              # unreadable marker is stale
        marker = {
            "pid": os.getpid(),
            "acquired_at": _iso(stamp),
            "deadline": _iso(stamp + timedelta(seconds=ttl_s)),
            "actor": ACTOR,
        }
        payload = (json.dumps(marker, separators=(",", ":")) + "\n").encode("utf-8")
        os.ftruncate(fd, 0)
        os.pwrite(fd, payload, 0)
        return LockHandle(fd=fd, path=path, took_over_from=prior)
    except OSError:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
        raise


def release_lock(handle: LockHandle) -> None:
    """Drop the lock. The marker file is left in place as a last-run record."""
    try:
        fcntl.flock(handle.fd, fcntl.LOCK_UN)
    finally:
        os.close(handle.fd)


# ------------------------------------------------------------------- discovery


@dataclass(frozen=True)
class Starved:
    """A candidate the gate is refusing solely for want of a cross-lineage verdict."""

    ident: str
    path: Path
    art: dict[str, Any]
    branch: str
    #: The repo-derived ref the diff was taken from — never a producer value.
    base_sha: str
    base_source: str
    #: The exact three-dot revision range that was graded and is quoted to the seat.
    diff_spec: str
    #: What the producer claimed, recorded but never acted on.
    declared_base: str | None
    head_sha: str
    producer_lineage: str
    changed: tuple[str, ...]
    risky: tuple[str, ...]
    title: str


@dataclass
class Scan:
    """What one discovery pass saw. `skips` is (ident, reason) and is never lossy."""

    scanned: int = 0
    starved: list[Starved] = field(default_factory=list)
    skips: list[tuple[str, str]] = field(default_factory=list)

    def skip(self, ident: str, reason: str) -> None:
        self.skips.append((ident, reason))

    def reasons(self) -> dict[str, int]:
        return dict(Counter(reason for _, reason in self.skips))


#: Skip reasons that name a REPAIRABLE upstream condition, so their ids go into
#: the ledger note rather than only a count. Everything else is ordinary
#: traffic (terminal artifacts, low-risk diffs, already-approved work) and
#: listing those ids every 10 minutes would be the spam that makes a note
#: worthless.
NOTED_SKIP_REASONS = (
    "missing-producer-lineage",
    "unknown-producer-lineage",
    "branch-moved",
    "branch-unresolvable",
    "head-sha-unresolvable",
    "parked",
    "identity-unbound",
    "diff-unreadable",
)


def terminal_ids(loops_root: Path) -> set[str]:
    """Every artifact id with a standing terminal event, from ONE ledger read.

    Mirrors `gate_loop.terminal_event_for` exactly -- first terminal wins, and
    an `unrejected` clears a `rejected` slot and only that one -- but resolves
    the WHOLE queue in a single pass over the file. The shared helper re-reads
    all 8.8 MB per id, which is right inside a retirement lock (one id, and it
    must be as fresh as possible) and wrong here (300+ ids, a read-only scan).
    Same rules, same terminal set, one read.

    An unreadable ledger returns the EMPTY set, which is the fail-closed
    direction for this caller: nothing is treated as terminal, so nothing is
    hidden from the operator -- and every candidate still has to pass the
    identity, lineage, sha and risk gates below before a seat is paid for.
    """
    path = loops_root / "ledger.jsonl"
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return set()
    slots: dict[str, str] = {}
    for line in raw.splitlines():
        if "sha256:" not in line:
            continue
        for ev in iter_events(line):
            ident = ev.get("id")
            if not isinstance(ident, str):
                continue
            event = ev.get("event")
            if event in TERMINAL_EVENTS:
                slots.setdefault(ident, str(event))
            elif event == "unrejected" and slots.get(ident) == "rejected":
                del slots[ident]
    return set(slots)


def _parked_ids(loops_root: Path) -> set[str]:
    """Ids with a `parked/` content marker, read from the filenames."""
    parked = loops_root / "parked"
    out: set[str] = set()
    try:
        entries = list(parked.iterdir())
    except OSError:
        return out
    for entry in entries:
        stem = entry.name[:-5] if entry.name.endswith(".json") else entry.name
        if stem.startswith("sha256_") and len(stem) == 71:
            out.add("sha256:" + stem[7:])
    return out


def _full_sha(value: object) -> str | None:
    return value.strip().lower() if isinstance(value, str) and _SHA40.fullmatch(
        value.strip().lower()) else None


@dataclass(frozen=True)
class BoundRefs:
    """The refs a review may be conducted against, and where each came from."""

    head_sha: str
    branch: str
    #: The ref the risk diff is computed against. NEVER a producer-authored value.
    diff_base: str
    diff_base_source: str
    #: The EXACT git revision range, three-dot. One string is used for the
    #: classification diff AND for the command quoted in the seat brief, so the
    #: reviewer cannot be shown a different diff than the one that was graded.
    diff_spec: str
    #: What the producer CLAIMED its base was, for the audit trail only.
    declared_base: str | None


def bound_refs(art: dict[str, Any], ident: str, integration_sha: str,
               integration_ref: str) -> tuple[BoundRefs | None, str | None]:
    """(refs, skip_reason). The refs a verdict may bind to — id-bound sources only.

    THE BLOCKER THIS CLOSES (cross-lineage review round 2, 2026-08-13). Head,
    base and branch used to be read from the TOP-LEVEL envelope fields. Only
    `payload` is covered by the content id, so those fields are editable in
    place after filing: setting `base_sha` to `head~1` makes the conveyor
    classify risk over a one-commit diff, quote that trivial diff in the seat
    brief, and then bind the resulting approval to the FULL head. A reviewer
    approves three lines and the gate lands three hundred.

    So head and branch now come from the PAYLOAD ONLY, and a top-level mirror
    that disagrees with the payload is a refusal rather than a tie-break — the
    unhashed carrier never wins, and never gets to be the fallback either.

    THE BASE IS DIFFERENT, AND THE OBVIOUS FIX IS NOT ENOUGH. Moving base_sha
    into the payload would only close TAMPERING, not LYING: the payload is
    authored by the producer, and the content id proves the value is unchanged
    since filing, never that it was honest to begin with. A producer that files
    `payload.base_sha = head~1` from the start defeats a payload-only rule
    completely, and that is the same attack.

    The risk diff is therefore computed against the INTEGRATION REF resolved
    from the repository (`main`), which no envelope can influence at all, using
    the THREE-DOT form `main...head` — the merge base of the two, which the
    commit graph decides and no field can move.

    Three-dot and not two-dot, measured on the live queue: `git diff A..B` is a
    TREE diff, so `main..head` also reports every file MAIN has moved since the
    lane branched. On the one live starved candidate that inflated a 1-file
    change into 102 files, which is not a safe over-approximation — it is a
    brief that hands the reviewer a hundred files of somebody else's work and
    asks them to judge it, and it would classify nearly every candidate as
    risky, collapsing the distinction the tiering exists to draw. The three-dot
    form returns exactly the 1 file, matching the honestly-declared base
    exactly, while remaining immune to a dishonest one.

    Nothing is lost to a spoof this way: the only commits three-dot excludes
    are those already reachable from `main`, i.e. already landed, and an
    attacker cannot move the merge base without putting the code INTO main
    first. `payload.base_sha` is still recorded as `declared_base` for the
    audit trail, and is never allowed to decide anything.
    """
    payload = art.get("payload")
    if not isinstance(payload, dict):
        return None, "payload-not-an-object"
    try:
        if content_id(payload) != ident:
            return None, "payload-id-mismatch"
    except (TypeError, ValueError):
        return None, "payload-id-mismatch"

    head = _full_sha(payload.get("head_sha"))
    if head is None:
        return None, "head-sha-not-id-bound"
    branch_value = payload.get("branch") or payload.get("lane")
    branch = branch_value.strip() if isinstance(branch_value, str) else ""
    if not branch:
        return None, "branch-not-id-bound"
    declared_base = _full_sha(payload.get("base_sha"))

    # A top-level mirror may agree or be absent. It may never disagree, and it
    # may never stand in for a missing payload value.
    top_head = _full_sha(art.get("head_sha"))
    if top_head is not None and top_head != head:
        return None, "head-sha-mismatch"
    top_base = _full_sha(art.get("base_sha"))
    if declared_base is not None and top_base is not None and top_base != declared_base:
        return None, "base-sha-mismatch"
    top_branch = art.get("branch")
    if isinstance(top_branch, str) and top_branch.strip() and top_branch.strip() != branch:
        return None, "branch-mismatch"

    return BoundRefs(head_sha=head, branch=branch, diff_base=integration_sha,
                     diff_base_source=integration_ref,
                     diff_spec=f"{integration_sha}...{head}",
                     declared_base=declared_base), None


def _already_conveyed(art: dict[str, Any], head_sha: str) -> bool:
    """True once THIS conveyor has recorded a SETTLED verdict for exactly this tip.

    Without it a REQUEST-CHANGES candidate would be re-dispatched every single
    pass forever: it stays unapproved by construction, so "unapproved" alone is
    not a work signal. This is also what makes the operator alert fire ONCE.

    A QUARANTINED entry is explicitly NOT settled (Class A review, 2026-08-13).
    Quarantining an approval whose reviewer identity could not be verified is
    only half a fix if the quarantined entry then reads as "already reviewed":
    ONE silent seat would permanently freeze the candidate in a state where it
    can never be approved and will never be re-offered -- a worse outcome than
    the defect, because it is invisible and terminal. The dedupe therefore keys
    on entries that could actually settle the question: a KNOWN lineage, and no
    quarantine flag. A quarantined entry stays on the record as evidence and
    the next pass tries a fresh seat.
    """
    verdicts = art.get("verdicts")
    if not isinstance(verdicts, list):
        return False
    for entry in verdicts:
        if not isinstance(entry, dict) or entry.get("by") != ACTOR:
            continue
        reviewed = entry.get("reviewed_sha")
        if not (isinstance(reviewed, str) and reviewed.casefold() == head_sha.casefold()):
            continue
        if entry.get("identity_verified") is False or is_quarantined(entry.get("lineage")):
            continue
        if normalise_lineage(entry.get("lineage")) not in KNOWN_LINEAGES:
            continue
        return True
    return False


def scan(repo: Path, loops_root: Path, *, integration_ref: str = DEFAULT_INTEGRATION_REF) -> Scan:
    """Find candidates starved of a cross-lineage verdict. Read-only throughout.

    The predicate, in the order it is applied (each step fails CLOSED -- an
    unanswerable question is a skip, never a dispatch):

      1. the file parses, and its body IS the artifact its filename names
         (`gate_loop.envelope_identity_problem`, which also proves
         `content_id(payload) == id`);
      2. no standing terminal event (merged / completed / rejected / closed /
         superseded, first-terminal-wins with the `unrejected` reversal);
      3. no `parked/` marker;
      4. `producer.lineage` is present and is a KNOWN lineage -- without it
         `approved_cross_lineage` can never return True no matter who reviews,
         so paying for a seat would burn a real 8.7 minutes for nothing;
      5. head and branch from the ID-BOUND PAYLOAD ONLY, no top-level mirror
         disagreeing with them, and a branch that still resolves to that head
         (`bound_refs`);
      6. the REAL `git diff --name-only <integration_ref>...<head>` touches a
         path `risky_review_paths` flags. The base is resolved from the REPO,
         never from the envelope: declared `paths` are not consulted (an
         understated claim must not downgrade its own tier) and neither is a
         declared base (a narrowed base would hide the rest of the diff from
         the reviewer while the approval still bound to the full head);
      7. `approved_cross_lineage(art, head) is False`;
      8. this conveyor has not already SETTLED a verdict at this tip.
    """
    result = Scan()
    rc_i, integration_sha, _ = git(repo, "rev-parse", "--verify",
                                   f"{integration_ref}^{{commit}}")
    integration_sha = integration_sha.strip().lower() if rc_i == 0 else ""
    if not integration_sha:
        # Without the integration ref there is no trustworthy diff base, and the
        # only other candidates are producer-authored. Review nothing rather
        # than review against a value the producer chose.
        result.skip("*", "integration-ref-unresolvable")
        return result
    candidates = loops_root / "candidates"
    try:
        files = sorted(p for p in candidates.iterdir() if p.suffix == ".json")
    except OSError:
        return result
    terminal = terminal_ids(loops_root)
    parked = _parked_ids(loops_root)
    for path in files:
        stem = path.stem
        if not stem.startswith("sha256_"):
            continue
        ident = "sha256:" + stem[7:]
        if not _IDENT.fullmatch(ident):
            continue
        result.scanned += 1
        try:
            art = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            result.skip(ident, "unreadable-envelope")
            continue
        if not isinstance(art, dict):
            result.skip(ident, "unreadable-envelope")
            continue
        # Identity FIRST, and never repaired: an envelope that cannot prove it
        # is what it is read as gets no seat and no write.
        if GL.envelope_identity_problem(art, ident) is not None:
            result.skip(ident, "identity-unbound")
            continue
        if ident in terminal:
            result.skip(ident, "terminal")
            continue
        if ident in parked:
            result.skip(ident, "parked")
            continue
        producer = art.get("producer")
        producer = producer if isinstance(producer, dict) else {}
        producer_lineage = normalise_lineage(producer.get("lineage"))
        if producer_lineage is None:
            result.skip(ident, "missing-producer-lineage")
            continue
        if producer_lineage not in KNOWN_LINEAGES:
            result.skip(ident, "unknown-producer-lineage")
            continue
        refs, why = bound_refs(art, ident, integration_sha, integration_ref)
        if refs is None:
            result.skip(ident, why or "refs-not-id-bound")
            continue
        head, branch, base = refs.head_sha, refs.branch, refs.diff_base
        rc, tip, _ = git(repo, "rev-parse", "--verify", f"{branch}^{{commit}}")
        if rc != 0 or not tip.strip():
            result.skip(ident, "branch-unresolvable")
            continue
        if tip.strip().lower() != head:
            result.skip(ident, "branch-moved")
            continue
        rc_h, _, _ = git(repo, "cat-file", "-e", f"{head}^{{commit}}")
        if rc_h != 0:
            result.skip(ident, "head-sha-unresolvable")
            continue
        rc_d, out, _ = git(repo, "diff", "--name-only", refs.diff_spec)
        if rc_d != 0:
            result.skip(ident, "diff-unreadable")
            continue
        changed = tuple(sorted({ln.strip() for ln in out.splitlines() if ln.strip()}))
        risky = tuple(risky_review_paths(set(changed)))
        if not risky:
            result.skip(ident, "not-risky")
            continue
        if approved_cross_lineage(art, head):
            result.skip(ident, "already-approved")
            continue
        if _already_conveyed(art, head):
            result.skip(ident, "already-conveyed")
            continue
        title = art.get("title")
        result.starved.append(Starved(
            ident=ident, path=path, art=art, branch=branch, base_sha=base,
            base_source=refs.diff_base_source, declared_base=refs.declared_base,
            diff_spec=refs.diff_spec,
            head_sha=head, producer_lineage=producer_lineage, changed=changed,
            risky=risky, title=str(title)[:160] if isinstance(title, str) else "",
        ))
    return result


# ----------------------------------------------------------------- seat dispatch


@dataclass(frozen=True)
class Seat:
    """A read-only reviewer seat: the command, and the lineage we dispatched it as."""

    name: str
    lineage: str
    model: str
    argv: tuple[str, ...]


def _codex_binary() -> str | None:
    """`codexpick` if installed (it routes to the account with window left), else codex."""
    if CODEXPICK.exists():
        return str(CODEXPICK)
    return shutil.which("codex")


def make_seat(want: str, repo: Path, last_message: Path) -> Seat | None:
    """Build the read-only command for one lineage, or None if it is not installed."""
    if want == "openai":
        binary = _codex_binary()
        if binary is None:
            return None
        argv = [binary, "exec", "--cd", str(repo), "--sandbox", "read-only",
                "--color", "never", "-o", str(last_message)]
        if CODEX_MODEL:
            argv += ["-m", CODEX_MODEL]
        argv.append("-")
        return Seat("codex", "openai", CODEX_MODEL or "codex", tuple(argv))
    if want == "xai":
        binary = shutil.which("grok")
        if binary is None:
            return None
        argv = [binary, "--cwd", str(repo), "--sandbox", "read-only",
                "--output-format", "plain", "--prompt-file", "-"]
        if GROK_MODEL:
            argv += ["-m", GROK_MODEL]
        return Seat("grok", "xai", GROK_MODEL or "grok", tuple(argv))
    return None


def seat_chain(
    producer_lineage: str,
    repo: Path,
    last_message_for: Callable[[str], Path],
) -> list[Seat]:
    """Up to two read-only seats, each of a DIFFERENT lineage from the producer
    and from each other -- the dispatch order, including the one retry.

    Preference is codex/openai, then grok/xai. Excluding the producer's own
    lineage is not cosmetic: a same-lineage verdict cannot satisfy
    `approved_cross_lineage`, so dispatching one spends a real seat on an
    answer the gate must ignore.

    WHY THERE IS A SECOND SEAT AT ALL (measured 2026-08-13, finding
    sha256:c748786e7405): codex and gemini seats both returned **exit 0 with
    output that was exactly the prompt echoed back** -- 4,940 bytes, zero model
    output -- on the same brief, while a probe proved transport and quota were
    fine. The failure was brief-content-dependent, which means an rc of 0 is
    not evidence that a seat answered, and re-running the SAME seat on the SAME
    brief buys the same silence at full price (gate-retry doctrine: change the
    input or change the action). So the retry changes the ACTION by changing
    the lineage, and it happens at most once. Two silent seats end the
    candidate for this pass with both attempts named in the ledger.
    """
    order = [lin for lin in ("openai", "xai") if lin != producer_lineage]
    seats: list[Seat] = []
    for want in order:
        seat = make_seat(want, repo, last_message_for(want))
        if seat is not None:
            seats.append(seat)
    return seats[:2]


def build_prompt(repo: Path, cand: Starved) -> str:
    """The seat brief. Deliberately names NO model, lab or lineage.

    Two reasons, both learned the hard way. A prompt that names lineages is
    echoed back in the transcript, and the substitution check below would then
    read our own words as the seat's self-report. And a reviewer told which lab
    wrote the code is a reviewer given a prior it has no use for -- the diff is
    the whole evidence.

    The four grammar forms are shown with a "form N:" label for the same
    reason, and it is not cosmetic: `codex exec` echoes the prompt into its own
    stdout, so a BARE `REQUEST-CHANGES: <one sentence ...>` example line would
    sit in the transcript of a seat that produced no verdict at all -- and a
    parser could read our own placeholder as the seat's rejection. The label
    makes every example fail the grammar match, the `FINAL-VERDICT:` marker
    means an undeclared line is not a decision at all, and `_seat_answer`
    subtracts the echoed prompt on top of both.

    The base quoted here is the INTEGRATION REF, not anything the envelope
    claimed, so the diff the reviewer is asked to read is the same diff the
    risk classification was computed over and the same change that landing
    would introduce. A brief that quoted a producer-supplied base could show a
    reviewer three lines while the approval bound to three hundred.
    """
    changed = "\n".join(f"  - {p}" for p in cand.changed) or "  (none)"
    risky = "\n".join(f"  - {p}" for p in cand.risky)
    return f"""You are a READ-ONLY build reviewer. Do not modify, create or delete any file.
Do not commit, push, or run any test that writes. Read, then judge.

Repository:   {repo}
Branch:       {cand.branch}
Head commit:  {cand.head_sha}
Diff range:   {cand.diff_spec}
              (three-dot against this repository's own integration ref
               {cand.base_source} @ {cand.base_sha})

Review EXACTLY the head commit above. It is immutable and it is what will land.
The range is measured from the merge base with the integration ref, so the diff
below is precisely the set of changes landing this candidate would introduce --
no more (the integration ref's own movement is excluded) and no less.

Files this diff actually changes:
{changed}

Paths on this diff that the estate's policy treats as a self-governing or
high-blast-radius surface (this is why a review is required at all):
{risky}

WHAT TO DO
1. READ THE REAL DIFF FIRST. Run:
     git -C {repo} diff {cand.diff_spec}
   and read every hunk. Read the surrounding file where a hunk is not
   self-explanatory. A verdict issued without reading the diff is worthless.
2. Decide whether this is safe to land on main. Weigh, in this order:
   - does it weaken, bypass or widen any gate, guard, approval or permission
     path (including by adding an exemption)?
   - does it fail OPEN anywhere -- an absent input, an unreadable file or a
     failed probe that ends up meaning "allowed"?
   - does it do what its title claims, and does it do it completely (are the
     sibling call sites updated, or only the one that was noticed)?
   - are the tests real? A claimed fix with no test that fails when the fix is
     reverted is not proven.
3. If you cannot read the diff at all, say so in a REQUEST-CHANGES line rather
   than guessing. An honest refusal is useful; a guess is not.

WHAT TO OUTPUT
End your final message with EXACTLY two lines, in this order and with nothing
after them:

  REVIEWER-MODEL: <the exact model id you are actually running as>
  FINAL-VERDICT: <one of the four forms below>

Both markers are literal and both are required. A decision is only counted
when it is on a line that STARTS with FINAL-VERDICT: -- nothing else in your
message is read as a verdict, so you can quote, discuss and argue about
verdicts freely in your reasoning without any risk of being misread. Do not
decorate that line: no bullet, no dash, no bold, no heading. Write exactly ONE
FINAL-VERDICT line; if more than one appears, the most negative is taken.

The verdict must be EXACTLY one of these four forms (the "form N:" labels
below are NOT part of the verdict -- write only the part after the label):

  form 1:  APPROVE
  form 2:  APPROVE — with nits
  form 3:  APPROVE — zero blockers
  form 4:  REQUEST-CHANGES: <one sentence naming the specific blocking problem>

Any other wording is discarded and the review is recorded as having produced no
verdict, so do not hedge, do not add a fifth form, and do not put a summary
after the verdict line. Put your reasoning ABOVE those two lines.

BOTH LINES ARE MANDATORY, AND THE MODEL LINE IS NOT A FORMALITY. This review
only counts if it came from a lab other than the one that WROTE the code, and
your declared model id is how that is established. An approval that arrives
without a usable model id is recorded but QUARANTINED -- it cannot approve
anything, and the work is re-dispatched to another reviewer. So state the model
you are actually running as, plainly and exactly. If you are running as a
different model than whatever dispatched you, say the one you ACTUALLY are.
"""


@dataclass
class SeatResult:
    """What a seat actually produced. `transcript` is archived verbatim, always."""

    argv: tuple[str, ...]
    rc: int
    transcript: str
    last_message: str
    timed_out: bool
    duration_s: float
    error: str | None = None


def run_seat(seat: Seat, prompt: str, timeout_s: int = DEFAULT_SEAT_TIMEOUT_S) -> SeatResult:
    """Run one seat under a hard wall-clock cap, killing its whole process GROUP.

    `start_new_session=True` puts the child in its own process group so the
    timeout path can kill the provider CLI AND every helper it spawned. A bare
    `Popen.kill()` reaps the wrapper and leaves the model call running, which is
    how a 20-minute cap turns into an hour of orphaned seats.
    """
    last_message_path: Path | None = None
    for index, token in enumerate(seat.argv):
        if token == "-o" and index + 1 < len(seat.argv):
            last_message_path = Path(seat.argv[index + 1])
    # REMOVE THE -o FILE BEFORE LAUNCHING (round-3 review, BLOCKER). The
    # last-message file is the PREFERRED answer channel, and it was previously
    # read on whatever it happened to contain. A file left by ANY earlier
    # attempt -- the first seat of this candidate's own retry chain, a crashed
    # pass, a previous run of the daemon -- was therefore read as the live
    # seat's answer, and because that branch never reaches the prompt-echo
    # subtraction, a stale `FINAL-VERDICT: APPROVE` became an approval the
    # running seat never authored. Deleting it first makes the absence of an
    # answer indistinguishable from an empty one, which is exactly right: both
    # mean the seat did not answer.
    if last_message_path is not None:
        try:
            last_message_path.unlink(missing_ok=True)
        except OSError as exc:
            # If we cannot guarantee the channel is empty we must not read it.
            return SeatResult(seat.argv, 127, "", "", False, 0.0,
                              error=f"could not clear the seat's last-message file "
                                    f"{last_message_path}: {type(exc).__name__}: {exc}")
    launched_at = time.time()
    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            list(seat.argv), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, start_new_session=True,
        )
    except OSError as exc:
        return SeatResult(seat.argv, 127, "", "", False, 0.0,
                          error=f"{type(exc).__name__}: {exc}")
    timed_out = False
    try:
        out, _ = proc.communicate(prompt, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_group(proc)
        try:
            out, _ = proc.communicate(timeout=30)
        except (subprocess.TimeoutExpired, ValueError, OSError):
            out = ""
    duration = time.monotonic() - started
    last = _read_last_message(last_message_path, launched_at)
    return SeatResult(seat.argv, proc.returncode if proc.returncode is not None else -1,
                      out or "", last, timed_out, duration)


def _read_last_message(path: Path | None, launched_at: float) -> str:
    """The seat's last-message file, but ONLY if this run could have written it.

    Two independent barriers, because the file is the preferred answer channel
    and a wrong answer here is a fabricated verdict:

      * it was unlinked before the child started, so anything present now was
        created during the run;
      * its mtime must not predate the launch, which catches the case the
        unlink silently did not take effect (a replaced directory, a stale NFS
        handle) and any file that arrived from outside this run.

    Absent, unreadable, or older than the launch all return "" -- the same
    value as "the seat wrote nothing", which routes to the seat-failure path.
    There is deliberately no branch where a doubtful file is read anyway.
    """
    if path is None:
        return ""
    try:
        stat = path.stat()
    except OSError:
        return ""
    if stat.st_mtime < launched_at:
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def seat_run_is_clean(result: SeatResult) -> bool:
    """True only when the seat process finished normally and of its own accord.

    A VERDICT FROM A DEAD SEAT IS NOT A VERDICT (round-3 review, BLOCKER).
    Parsing used to be the only gate: whatever the transcript contained decided
    the outcome, and `timed_out` / `rc` / `error` were consulted only when
    parsing found nothing. So a seat KILLED at the 20-minute timeout that had
    already flushed a provisional `FINAL-VERDICT: APPROVE` mid-reasoning got
    that approval recorded, and it satisfied the gate's predicate. The same
    held for a seat that crashed after printing one.

    A partial review is not a lenient review, it is an ABSENT one: the reviewer
    never reached the end of the diff, and the text it happened to have emitted
    describes an opinion it had not finished forming.
    """
    return result.rc == 0 and not result.timed_out and result.error is None


def _kill_group(proc: subprocess.Popen[str]) -> None:
    """SIGTERM the child's process GROUP, then ALWAYS SIGKILL the group.

    THE LEAK THIS CLOSES (cross-lineage review round 2, 2026-08-13). The
    previous version returned as soon as the LEADER exited during the grace
    period, on the assumption that a dead leader means a dead group. It does
    not: a provider CLI that traps SIGTERM and exits cleanly leaves any
    grandchild that ignores SIGTERM running forever, and the wait-for-leader
    fast path made that the LIKELY outcome rather than an edge case. The whole
    reason this function exists is that killing the wrapper is not enough.

    So the SIGKILL to the group is unconditional. The grace period is only ever
    a courtesy to let a well-behaved group exit on its own terms; it is never a
    reason to skip the kill. `ProcessLookupError` at that point is the good
    case -- it means the group really is gone -- and is not an error.
    """
    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        return                      # already reaped; there is no group to signal
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except OSError:
        pass                        # could not TERM it; still try to KILL it
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            break                   # the LEADER is gone -- the group may not be
        time.sleep(0.2)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass                        # the whole group exited; nothing to kill
    except OSError:
        pass
    try:
        proc.wait(timeout=10)
    except (subprocess.TimeoutExpired, OSError):
        pass


# ---------------------------------------------------------------- verdict parsing


def _seat_answer(result: SeatResult, prompt: str) -> str:
    """The seat's OWN words, with our prompt subtracted.

    `codex exec -o FILE` writes the model's last message in isolation, which is
    the clean channel and is preferred. Providers without that flag give us one
    stream containing the prompt echo AND the answer, so every line that is
    literally a line of the prompt is removed before parsing. Both barriers
    exist because either alone leaves a path where the conveyor reads its own
    instructions back as a verdict.
    """
    if result.last_message.strip():
        return result.last_message
    echoed = {line.strip() for line in prompt.splitlines() if line.strip()}
    return "\n".join(
        line for line in result.transcript.splitlines() if line.strip() not in echoed)


def verdict_lines(text: str) -> list[str]:
    """Every well-formed `FINAL-VERDICT:` decision in the seat's answer, in order."""
    found: list[str] = []
    for raw in text.splitlines():
        match = _FINAL_VERDICT_RE.match(raw)
        if match is None:
            continue
        decision = match.group("verdict").strip()
        if VERDICT_LINE_RE.fullmatch(decision):
            found.append(decision)
    return found


def final_verdict_line(text: str) -> str | None:
    """The seat's decision, verbatim, or None. Fabrication-resistant by shape.

    THE FABRICATION THIS CLOSES (cross-lineage review round 2, 2026-08-13). The
    previous version scanned bottom-up and stripped list markers before
    matching, so a REJECTION whose prose happened to end with a bulleted
    quotation --

        I am issuing a REQUEST-CHANGES.
        Do not:
        - APPROVE

    -- was read as `APPROVE`. Prose ABOUT a verdict became the verdict, and the
    stripping is what made an ordinary rejection able to trip it. Reviewers
    write bulleted lists constantly, so this was not an exotic input.

    Three properties now hold together, and each is load-bearing:

      * a decision must carry the `FINAL-VERDICT:` marker the brief demands, so
        a line the seat merely WROTE cannot be one it DECLARED;
      * whole lines only, with NO marker stripping -- `- FINAL-VERDICT: APPROVE`
        is not a decision, because the thing that made stripping necessary
        (providers decorating output) is exactly the thing that made it unsafe;
      * when several well-formed decisions appear, ANY REQUEST-CHANGES WINS.
        A seat that emitted both said something contradictory, and the only
        safe reading of a contradiction on an approval boundary is the one that
        approves nothing.
    """
    decisions = verdict_lines(text)
    if not decisions:
        return None
    for decision in decisions:
        if request_changes_reason(decision) is not None:
            return decision
    return decisions[-1]


def seat_failure_reason(result: SeatResult, answer: str) -> str:
    """Why this seat produced no verdict. NEVER a verdict, always a failure.

    "The seat did not answer" and "the seat approved" must never be reachable
    from the same state, so this function exists to name the failure precisely
    instead of letting it fall through as silence.

    THE MEASURED CASE THIS EXISTS FOR (2026-08-13, finding sha256:c748786e7405):
    codex and gemini both exited **0** having emitted exactly the prompt back --
    4,940 bytes, zero model output -- on the same brief, with transport and
    quota independently proven healthy. So `rc == 0` is not evidence of an
    answer, and an empty answer is a SEAT failure, not a candidate defect and
    certainly not a verdict. It is classified distinctly here because it is the
    one failure whose remedy is "change the brief or the lineage", not "retry".
    """
    if result.timed_out:
        return f"seat timed out after {result.duration_s:.0f}s and was killed by process group"
    if result.error:
        return f"seat could not be started: {result.error}"
    if not answer.strip():
        return (f"seat exited rc={result.rc} but produced NO model output at all -- its "
                f"stdout was the prompt echoed back ({len(result.transcript)} bytes). "
                "Exit 0 is not evidence that a seat answered")
    return (f"seat exited rc={result.rc} and answered, but no line matched the pinned "
            "verdict grammar; an unparseable answer is not a verdict")


def request_changes_reason(verdict: str) -> str | None:
    match = _REQUEST_CHANGES_RE.match(verdict.strip())
    return match.group("reason").strip() if match else None


def _lineage_of_model(name: str) -> str | None:
    """The lab a model id names, or None when that is not unambiguous.

    NOT first-match (round-3 review). First-match resolved a hybrid or
    adversarial id like "gpt-claude-bridge" or "grok-gpt-eval" to whichever
    lineage happened to sit earliest in the table, which is arbitrary and
    biased toward openai purely by declaration order. A name carrying tokens
    from two labs does not identify one lab, so it identifies none: returning
    None routes it to the quarantine path, which is the direction that cannot
    manufacture an approval.
    """
    hits = {lin for pattern, lin in _MODEL_LINEAGE_TOKENS if pattern.search(name)}
    return hits.pop() if len(hits) == 1 else None


def is_quarantined(lineage_value: object) -> bool:
    """True for a lineage this conveyor deliberately made unusable as approval."""
    return isinstance(lineage_value, str) and lineage_value.startswith(QUARANTINE_PREFIX)


def reviewer_identity(seat: Seat, last_message: str, *,
                      is_approval: bool) -> tuple[str, str, str | None]:
    """(model, lineage, note) actually recorded for this review.

    THE ASYMMETRY. A self-report may move the recorded lineage AWAY from the
    seat we dispatched, never toward it: the measured failure is a seat of one
    lineage answering as another model, and the only safe reading of "I am
    actually X" is to believe the part that REDUCES what we may claim.

    THE ABSENCE (Class A review, 2026-08-13 -- this was a live defect here).
    That asymmetry alone is INCOMPLETE, and the gap sat exactly on the approval
    boundary. Returning the DISPATCHED lineage when the seat declared nothing
    means the substitution is caught only when the substituted seat VOLUNTEERS
    it. A silent substituted seat -- the likelier case, since a model answering
    as something else has no reason to announce it -- was recorded as the
    lineage we dispatched and satisfied `approved_cross_lineage`. That is
    favourable absence on an approval: "we could not verify who reviewed this"
    was being reported with the same colour as "a different lineage reviewed
    this".

    So absence fails CLOSED, but only where it can grant something:

      * APPROVAL + no usable declaration -> the recorded lineage becomes
        `unverified:<dispatched>`, which is outside `KNOWN_LINEAGES` and is
        therefore MECHANICALLY rejected by `approved_cross_lineage`. The
        verdict STRING is never touched -- the seat's words stand verbatim and
        the quarantine is carried entirely by the lineage field, so an auditor
        reads exactly what was said and exactly why it does not count.
      * REQUEST-CHANGES + no declaration -> recorded with the dispatched
        lineage and the note, unchanged. A rejection grants nothing, so
        quarantining it would only discard information; and a rejection from an
        unverifiable reviewer is still a reason to go look.
    """
    declared: list[str] = []
    for raw in last_message.splitlines():
        match = _REVIEWER_MODEL_RE.match(raw)
        if match:
            value = match.group("model").strip()
            if not any(value.casefold() == seen.casefold() for seen in declared):
                declared.append(value)
    if not declared:
        detail = "reviewer-model-not-declared"
    else:
        # EVERY declaration counts, not just the last one (round-3 review).
        # Last-wins let a seat that confessed "claude-opus-5" and then wrote
        # "gpt-5.6-sol" be recorded as openai with NO note -- the favourable
        # reading of a contradiction, which is precisely what the
        # monotone-pessimistic invariant exists to forbid. A witness that gave
        # two different answers has not identified itself.
        mapped = [_lineage_of_model(value) for value in declared]
        unmappable = [value for value, lin in zip(declared, mapped, strict=True)
                      if lin is None]
        distinct = {lin for lin in mapped if lin is not None}
        if unmappable:
            detail = f"reviewer-model-unrecognised ({', '.join(unmappable)})"
        elif len(distinct) > 1:
            detail = ("reviewer-model-contradictory (" + ", ".join(declared) + " map to "
                      + ", ".join(sorted(distinct)) + ")")
        else:
            resolved = distinct.pop()
            reported = declared[-1]
            if resolved != seat.lineage:
                return reported, resolved, (
                    f"seat dispatched as {seat.lineage} ({seat.model}) self-reported as "
                    f"{reported} ({resolved}); the self-reported lineage is recorded")
            return reported, resolved, None
    if not is_approval:
        return seat.model, seat.lineage, detail
    return seat.model, QUARANTINE_PREFIX + seat.lineage, (
        f"{detail}: identity unverifiable, approval quarantined")


# ------------------------------------------------------------------- write-back


def archive_transcript(evidence_dir: Path, ident: str, stamp: str, text: str) -> Path | None:
    """Store the seat's full transcript. The verdict entry is worthless without it."""
    try:
        evidence_dir.mkdir(parents=True, exist_ok=True)
        path = evidence_dir / f"{_short(ident)}-{stamp}.txt"
        path.write_text(text, encoding="utf-8")
    except OSError:
        return None
    return path


def write_verdict(path: Path, ident: str, entry: dict[str, Any]) -> str | None:
    """Append one verdict entry to the TOP-LEVEL `verdicts[]`. Returns why not, or None.

    `verdicts[]` lives OUTSIDE `payload` precisely so that recording a review
    cannot change `content_id(payload)` and therefore cannot change the
    envelope id. That is asserted BEFORE and AFTER the write rather than
    assumed: if a future envelope shape ever moved the list inside the payload,
    the "after" check is what would catch it instead of the gate silently
    dropping every conveyed verdict as unbound.

    The envelope is re-read from disk here, not carried from discovery: minutes
    of seat time passed, and another writer may have touched it.
    """
    try:
        art = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return f"envelope unreadable at write-back: {type(exc).__name__}"
    if not isinstance(art, dict):
        return "envelope is not a JSON object at write-back"
    problem = GL.envelope_identity_problem(art, ident)
    if problem is not None:
        return f"refusing to write into an envelope that is not id-bound: {problem}"
    verdicts = art.get("verdicts")
    if verdicts is None:
        verdicts = []
        art["verdicts"] = verdicts
    if not isinstance(verdicts, list):
        return "verdicts is present but is not a list; refusing to overwrite it"
    verdicts.append(entry)
    if not GL.envelope_id_is_bound(art):
        return "writing the verdict would unbind the envelope id; refused"
    tmp = path.with_suffix(path.suffix + f".conveyor-{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(art, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        try:
            tmp.unlink()
        except OSError:
            pass
        return f"envelope write failed: {type(exc).__name__}: {exc}"
    return None


# ----------------------------------------------------------------------- ledger


def note(loops_root: Path, event: dict[str, Any]) -> None:
    """Append one ledger event through the shared transport. Never fatal."""
    try:
        append_event(loops_root, event)
    except Exception as exc:                                        # noqa: BLE001
        print(f"{ACTOR}: ledger append failed: {type(exc).__name__}: {exc}",
              file=sys.stderr)


def alert(loops_root: Path, line: str) -> None:
    """One durable ALERTS.md line. Best-effort; the ledger event is the record."""
    try:
        with open(loops_root / "ALERTS.md", "a", encoding="utf-8") as fh:
            fh.write(line.rstrip("\n") + "\n")
    except OSError as exc:
        print(f"{ACTOR}: ALERTS.md append failed: {type(exc).__name__}: {exc}",
              file=sys.stderr)


# -------------------------------------------------------------------- the pass


@dataclass
class PassReport:
    """Everything one pass did, in the shape the ledger summary is built from."""

    scanned: int = 0
    starved: int = 0
    dispatched: int = 0
    written: int = 0
    approvals: int = 0
    rejections: int = 0
    discarded: int = 0
    no_verdict: int = 0
    seat_failures: int = 0
    dead_seat_verdicts: int = 0
    quarantined: int = 0
    same_lineage: int = 0
    skips: dict[str, int] = field(default_factory=dict)
    noted: dict[str, list[str]] = field(default_factory=dict)
    outcomes: list[dict[str, Any]] = field(default_factory=list)


SeatRunner = Callable[[Seat, str, int], SeatResult]


def run_pass(
    repo: Path,
    loops_root: Path,
    *,
    evidence_dir: Path,
    max_candidates: int = DEFAULT_MAX_CANDIDATES,
    seat_timeout_s: int = DEFAULT_SEAT_TIMEOUT_S,
    seat_runner: SeatRunner = run_seat,
    integration_ref: str = DEFAULT_INTEGRATION_REF,
    dry_run: bool = False,
    log: Callable[[str], None] = print,
) -> PassReport:
    """One bounded conveyor pass. The lock is the caller's responsibility."""
    found = scan(repo, loops_root, integration_ref=integration_ref)
    report = PassReport(scanned=found.scanned, starved=len(found.starved),
                        skips=found.reasons())
    noted: dict[str, list[str]] = defaultdict(list)
    for ident, reason in found.skips:
        if reason in NOTED_SKIP_REASONS and len(noted[reason]) < 20:
            noted[reason].append(_short(ident))
    report.noted = dict(noted)

    for cand in found.starved[:max_candidates]:
        short = _short(cand.ident)
        chain = seat_chain(
            cand.producer_lineage, repo,
            lambda lin, s=short: evidence_dir / f".{s}-{lin}.last",  # type: ignore[misc]
        )
        if not chain:
            report.outcomes.append({"id": cand.ident, "result": "no-seat-available"})
            log(f"skip {short}: no read-only seat of a lineage other than "
                f"{cand.producer_lineage} is installed on this host")
            note(loops_root, {
                "ts": _iso(_now()), "role": "external", "event": "observed",
                "id": cand.ident, "actor": ACTOR,
                "detail": {"kind": "verdict-conveyor-no-seat",
                           "producer_lineage": cand.producer_lineage,
                           "reason": ("no read-only reviewer CLI of a different lineage "
                                      "is installed on this host")},
            })
            continue
        if dry_run:
            report.outcomes.append({
                "id": cand.ident, "result": "dry-run",
                "chain": [{"seat": s.name, "lineage": s.lineage, "argv": list(s.argv)}
                          for s in chain]})
            log(f"dry-run {short}: would dispatch "
                f"{' then '.join(f'{s.name}({s.lineage})' for s in chain)} on "
                f"{cand.head_sha[:12]}")
            continue
        try:
            evidence_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        prompt = build_prompt(repo, cand)
        # AT MOST ONE RETRY, AND ONLY WITH A DIFFERENT LINEAGE. Re-running the
        # same seat on the same brief is the gate-retry defect: a seat that
        # went silent on this brief will go silent again, at full price. The
        # chain changes the ACTION (a different provider), never just repeats.
        attempts: list[dict[str, Any]] = []
        settled: dict[str, Any] | None = None
        for seat in chain:
            stamp = _stamp(_now())
            log(f"dispatch {short}: {seat.name} ({seat.lineage}) reviewing "
                f"{cand.head_sha[:12]} on {cand.branch}")
            report.dispatched += 1
            result = seat_runner(seat, prompt, seat_timeout_s)
            answer = _seat_answer(result, prompt)
            archived = archive_transcript(
                evidence_dir, cand.ident, f"{stamp}-{seat.lineage}",
                _compose_transcript(cand, seat, result))
            for token_index, token in enumerate(seat.argv):
                if token == "-o" and token_index + 1 < len(seat.argv):
                    try:
                        Path(seat.argv[token_index + 1]).unlink()
                    except OSError:
                        pass
            verdict = final_verdict_line(answer)
            if verdict is not None and not seat_run_is_clean(result):
                # THE RUN, NOT JUST THE TEXT. A seat that was killed at the
                # timeout or that crashed may already have flushed a
                # provisional verdict; recording it would approve a review that
                # never reached the end of the diff.
                why = (f"the seat emitted a well-formed verdict but its run did not "
                       f"complete cleanly (rc={result.rc}, timed_out={result.timed_out}, "
                       f"error={result.error or 'none'}). A verdict flushed by a seat "
                       "that was killed or crashed describes an unfinished review, so "
                       "it is archived and discarded, never recorded")
                report.dead_seat_verdicts += 1
                report.seat_failures += 1
                log(f"seat failure {short}: {seat.name} ({seat.lineage}) -- {why}")
                attempts.append({
                    "seat": seat.name, "lineage": seat.lineage, "model": seat.model,
                    "rc": result.rc, "timed_out": result.timed_out,
                    "empty_output": False, "discarded_verdict": verdict,
                    "reason": why,
                    "transcript": str(archived) if archived is not None else None,
                    "duration_s": round(result.duration_s, 1),
                })
                continue
            if verdict is None:
                why = seat_failure_reason(result, answer)
                report.seat_failures += 1
                log(f"seat failure {short}: {seat.name} ({seat.lineage}) -- {why}")
                attempts.append({
                    "seat": seat.name, "lineage": seat.lineage, "model": seat.model,
                    "rc": result.rc, "timed_out": result.timed_out,
                    "empty_output": not answer.strip(), "reason": why,
                    "transcript": str(archived) if archived is not None else None,
                    "duration_s": round(result.duration_s, 1),
                })
                continue
            outcome = _settle(
                repo=repo, loops_root=loops_root, cand=cand, seat=seat, result=result,
                answer=answer, verdict=verdict, archived=archived,
                report=report, log=log,
            )
            if outcome.get("retry_in_pass"):
                # The seat answered, and its answer is on the record -- but the
                # approval satisfies nothing: either it would not say what it
                # was (quarantined), or it turned out to be the producer's own
                # lineage. Both are SEAT failures like the silent echo, and get
                # the same remedy: change the action (a different lineage), once.
                quarantined = outcome.get("result") == "recorded-quarantined"
                why = ("the seat approved but declared no usable REVIEWER-MODEL, so its "
                       "identity could not be verified; the verdict is recorded with a "
                       "quarantined lineage and cannot approve"
                       if quarantined else
                       "the seat approved but its verified lineage is the PRODUCER's "
                       "own, so the verdict is recorded honestly and cannot approve")
                report.seat_failures += 1
                log(f"seat failure {short}: {seat.name} ({seat.lineage}) -- {why}")
                attempts.append({
                    "seat": seat.name, "lineage": seat.lineage, "model": seat.model,
                    "rc": result.rc, "timed_out": False, "empty_output": False,
                    "quarantined": quarantined,
                    "recorded_lineage": outcome.get("reviewer_lineage"),
                    "reason": why,
                    "transcript": str(archived) if archived is not None else None,
                    "duration_s": round(result.duration_s, 1),
                })
                continue
            settled = outcome
            break
        if settled is None:
            report.no_verdict += 1
            log(f"no verdict {short}: {len(attempts)} seat(s) produced nothing usable")
            note(loops_root, {
                "ts": _iso(_now()), "role": "external", "event": "observed",
                "id": cand.ident, "actor": ACTOR,
                "detail": {
                    "kind": "verdict-conveyor-no-verdict",
                    "reviewed_sha": cand.head_sha,
                    "attempts": attempts,
                    "quarantined": sum(1 for a in attempts if a.get("quarantined")),
                    "reason": ("every dispatched seat failed to produce a verdict that "
                               "can satisfy the cross-lineage predicate -- no grammar "
                               "line, or an approval whose reviewer identity could not "
                               "be verified; the candidate stays unapproved and will be "
                               "re-offered to a fresh seat next pass"),
                },
            })
            report.outcomes.append({"id": cand.ident, "result": "no-verdict",
                                    "attempts": attempts})
        else:
            if attempts:
                settled["failed_attempts"] = attempts
            report.outcomes.append(settled)
    return report


def _compose_transcript(cand: Starved, seat: Seat, result: SeatResult) -> str:
    """Transcript header + raw seat output. The header is what makes it auditable."""
    return (
        f"candidate: {cand.ident}\n"
        f"branch: {cand.branch}\n"
        f"base_sha: {cand.base_sha}\n"
        f"head_sha: {cand.head_sha}\n"
        f"producer_lineage: {cand.producer_lineage}\n"
        f"seat: {seat.name} ({seat.lineage}) model={seat.model}\n"
        f"argv: {' '.join(seat.argv)}\n"
        f"rc: {result.rc} timed_out: {result.timed_out} "
        f"duration_s: {result.duration_s:.1f}\n"
        f"error: {result.error or ''}\n"
        "--- final message ---\n"
        f"{result.last_message}\n"
        "--- full seat output ---\n"
        f"{result.transcript}\n"
    )


def _settle(
    *,
    repo: Path,
    loops_root: Path,
    cand: Starved,
    seat: Seat,
    result: SeatResult,
    answer: str,
    verdict: str,
    archived: Path | None,
    report: PassReport,
    log: Callable[[str], None],
) -> dict[str, Any]:
    """Record ONE seat's verdict: sha rebind, identity, envelope write, ledger.

    Reached only when the seat produced a line that fully matches the pinned
    grammar. Every no-verdict path is handled by the caller's attempt loop, so
    there is no branch in here that can invent one.
    """
    ident = cand.ident
    transcript_ref = str(archived) if archived is not None else None

    # SHA BINDING, re-checked here and not earlier: the branch may have moved
    # during the minutes the seat spent reading. A verdict about code that is
    # no longer at the tip is not a verdict about what would land.
    rc, tip, _ = git(repo, "rev-parse", "--verify", f"{cand.branch}^{{commit}}")
    current = tip.strip().lower() if rc == 0 else ""
    if current != cand.head_sha:
        report.discarded += 1
        log(f"discard {_short(ident)}: branch {cand.branch} moved from "
            f"{cand.head_sha[:12]} to {current[:12] or 'unresolvable'} during review")
        note(loops_root, {
            "ts": _iso(_now()), "role": "external", "event": "observed", "id": ident,
            "actor": ACTOR,
            "detail": {"kind": "verdict-conveyor-discarded", "seat": seat.name,
                       "reviewed_sha": cand.head_sha,
                       "current_tip": current or "unresolvable",
                       "reason": ("the branch moved while the seat was reviewing, so "
                                  "the verdict describes code that would not land"),
                       "transcript": transcript_ref},
        })
        return {"id": ident, "result": "discarded-tip-moved"}

    rejection_reason = request_changes_reason(verdict)
    model, lineage_actual, identity_note = reviewer_identity(
        seat, answer, is_approval=rejection_reason is None)
    quarantined = is_quarantined(lineage_actual)
    entry: dict[str, Any] = {
        "lineage": lineage_actual,
        "model": model,
        "by": ACTOR,
        "verdict": verdict,
        "reviewed_sha": cand.head_sha,
        "transcript": transcript_ref,
    }
    if identity_note:
        entry["identity_note"] = identity_note
    same_lineage = lineage_actual == cand.producer_lineage
    satisfies = not same_lineage and not quarantined
    if not satisfies:
        # Recorded, because it happened and hiding it would be the dishonest
        # direction -- but never counted as supply: it cannot satisfy the
        # predicate, so the candidate is still starved and the operator needs
        # to know the seat routing produced nothing usable.
        entry["satisfies_cross_lineage"] = False
    if quarantined:
        # An explicit flag as well as the unusable lineage string. The lineage
        # prefix is what `approved_cross_lineage` mechanically trips over; this
        # flag is what a HUMAN or a future reader keys on without having to
        # know the prefix convention.
        entry["identity_verified"] = False
        report.quarantined += 1
    if same_lineage:
        report.same_lineage += 1

    failure = write_verdict(cand.path, ident, entry)
    if failure is not None:
        log(f"write refused {_short(ident)}: {failure}")
        note(loops_root, {
            "ts": _iso(_now()), "role": "external", "event": "observed", "id": ident,
            "actor": ACTOR,
            "detail": {"kind": "verdict-conveyor-write-refused", "reason": failure,
                       "reviewed_sha": cand.head_sha, "verdict": verdict,
                       "transcript": transcript_ref},
        })
        return {"id": ident, "result": "write-refused", "reason": failure}

    report.written += 1
    reason = rejection_reason
    if reason is None:
        if satisfies:
            report.approvals += 1
    else:
        report.rejections += 1
    log(f"recorded {_short(ident)}: {verdict} ({lineage_actual}/{model})"
        f"{' [QUARANTINED: reviewer identity unverifiable]' if quarantined else ''}")
    note(loops_root, {
        "ts": _iso(_now()), "role": "external", "event": "observed", "id": ident,
        "actor": ACTOR,
        "detail": {
            "kind": "verdict-conveyor-recorded",
            "verdict": verdict,
            "reviewer_lineage": lineage_actual,
            "reviewer_model": model,
            "seat": seat.name,
            "producer_lineage": cand.producer_lineage,
            "satisfies_cross_lineage": satisfies,
            "identity_verified": not quarantined,
            "reviewed_sha": cand.head_sha,
            "transcript": transcript_ref,
            "duration_s": round(result.duration_s, 1),
            **({"reason": reason} if reason else {}),
            **({"identity_note": identity_note} if identity_note else {}),
        },
    })
    if reason is not None:
        alert(loops_root,
              f"- {_iso(_now())} REQUEST-CHANGES on candidate {_short(ident)} "
              f"({cand.branch} @ {cand.head_sha[:12]}) from {lineage_actual}/{model}: "
              f"{reason} -- transcript: {transcript_ref}")
    # WHAT STOPS THE CHAIN. A recorded verdict only ends this candidate's turn
    # if it SETTLED something: an approval that satisfies the predicate, or any
    # rejection (a rejection settles the candidate regardless of who wrote it —
    # the work needs changing either way, and re-asking would just buy a second
    # opinion nobody requested after the operator was already alerted).
    # An approval that satisfies nothing — quarantined identity, or a verified
    # reviewer that turned out to share the producer's lineage — is on the
    # record but has moved nothing, so the chain's one retry applies.
    retry_in_pass = reason is None and not satisfies
    return {"id": ident,
            "result": "recorded-quarantined" if quarantined else "recorded",
            "verdict": verdict, "reviewer_lineage": lineage_actual,
            "identity_verified": not quarantined,
            "retry_in_pass": retry_in_pass,
            "satisfies_cross_lineage": satisfies}


def summary_event(report: PassReport, duration_s: float,
                  took_over: dict[str, Any] | None) -> dict[str, Any]:
    """The one ledger event every pass appends, whether or not it did anything.

    A pass that found nothing still says so: silence and "the daemon is dead"
    look identical in a log, and this conveyor exists because an absence went
    unnoticed for weeks.
    """
    detail: dict[str, Any] = {
        "kind": "verdict-conveyor-pass",
        "scanned": report.scanned,
        "starved": report.starved,
        "dispatched": report.dispatched,
        "written": report.written,
        "approvals": report.approvals,
        "rejections": report.rejections,
        "discarded": report.discarded,
        "no_verdict": report.no_verdict,
        "seat_failures": report.seat_failures,
        "dead_seat_verdicts_discarded": report.dead_seat_verdicts,
        "quarantined_identity": report.quarantined,
        "same_lineage_recorded": report.same_lineage,
        "skipped": report.skips,
        "skipped_ids": report.noted,
        "duration_s": round(duration_s, 1),
    }
    if took_over is not None:
        detail["took_over_stale_lock"] = took_over
    return {"ts": _iso(_now()), "role": "external", "event": "observed",
            "actor": ACTOR, "detail": detail}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="One bounded pass of the standing cross-lineage verdict conveyor.")
    parser.add_argument("--repo", type=Path, default=REPO_ROOT,
                        help="the serving checkout whose branches are reviewed")
    parser.add_argument("--loops-root", type=Path, default=None,
                        help="var/loopqueue (default: <repo>/var/loopqueue)")
    parser.add_argument("--evidence-dir", type=Path, default=None,
                        help="transcript archive (default: "
                             "<repo>/var/gate-evidence/records/verdicts)")
    parser.add_argument("--max-candidates", type=int, default=DEFAULT_MAX_CANDIDATES)
    parser.add_argument("--seat-timeout-s", type=int, default=DEFAULT_SEAT_TIMEOUT_S)
    parser.add_argument("--lock-ttl-s", type=int, default=DEFAULT_LOCK_TTL_S)
    parser.add_argument("--integration-ref", default=DEFAULT_INTEGRATION_REF,
                        help="the ref every risk diff is taken from; resolved from "
                             "the repo so no envelope field can narrow it "
                             f"(default: {DEFAULT_INTEGRATION_REF})")
    parser.add_argument("--dry-run", action="store_true",
                        help="discover and print the dispatch plan; pay for no seat")
    args = parser.parse_args(argv)

    repo = args.repo.resolve()
    loops_root = (args.loops_root or repo / "var" / "loopqueue").resolve()
    evidence_dir = (args.evidence_dir
                    or repo / "var" / "gate-evidence" / "records" / "verdicts").resolve()
    if not loops_root.is_dir():
        print(f"{ACTOR}: loops root {loops_root} does not exist", file=sys.stderr)
        return 2

    try:
        handle = acquire_lock(loops_root, ttl_s=args.lock_ttl_s)
    except LockUnsupported as exc:
        # Not "no work to do" -- the conveyor cannot guarantee it is the only
        # writer, and it writes approvals. Refusing loudly is the whole point.
        print(f"{ACTOR}: REFUSING TO RUN: {exc}", file=sys.stderr)
        return 2
    if handle is None:
        print(f"{ACTOR}: another pass holds the lock; exiting without work")
        return 0
    started = time.monotonic()
    try:
        report = run_pass(
            repo, loops_root, evidence_dir=evidence_dir,
            max_candidates=max(0, args.max_candidates),
            seat_timeout_s=args.seat_timeout_s,
            integration_ref=args.integration_ref, dry_run=args.dry_run,
        )
        note(loops_root, summary_event(report, time.monotonic() - started,
                                       handle.took_over_from))
        print(f"{ACTOR}: scanned={report.scanned} starved={report.starved} "
              f"dispatched={report.dispatched} written={report.written} "
              f"approvals={report.approvals} rejections={report.rejections} "
              f"discarded={report.discarded} no_verdict={report.no_verdict} "
              f"seat_failures={report.seat_failures}")
    finally:
        release_lock(handle)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
