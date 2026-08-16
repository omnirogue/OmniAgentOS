#!/usr/bin/env python3
"""bridge/train_assembler.py — group approved candidates into LINEAR trains.

A train is a set of pairwise file-disjoint candidates cherry-picked, in a
deterministic order, onto the CURRENT `main` — or, when there are more eligible
candidates than one train can hold, onto the TIP OF THE TRAIN BEFORE IT (a
CHAIN). Gating a train instead of a candidate is where landing throughput comes
from: one ~12-minute gate clears up to `cap` candidates at once, and
file-disjoint members never conflict when replayed onto the same base.

Five rules do the load-bearing work, each with the wound it came from:

  * DISJOINTNESS IS COMPUTED FROM THE REAL DIFF, never the declared `paths`.
    A candidate that understates its footprint produces a WRONG schedule (two
    members silently editing the same file land together and corrupt each
    other), not a slow one. The footprint is `git diff --name-only base..branch`
    against the branch's actual trees — the same source `merge-gate` grades.

  * SELF-GOVERNING SURFACES RUN AS ONE-MEMBER TRAINS. Pipeline-critical code,
    `scripts/merge-gate.sh`, and schema changes are never mixed with ordinary
    work: the pinned-main judge gates them alone. Secret-bearing surfaces remain
    human-only and are never mechanically assembled.

  * THE TRAIN TIP IS DETERMINISTIC ACROSS TICKS. The committer identity and
    date are pinned (committer date := each commit's own author date), so
    replaying the SAME members onto the SAME base yields the SAME tip sha on
    every tick. Without this the cherry-pick's committer date would default to
    "now" and every 60-second tick would mint a new tip for an unchanged train
    — a fresh `<train>@<tip>` gate key each tick, i.e. the re-gate storm this
    estate has measured at 28 identical refusals on one symbol.

  * TRAINS BEYOND THE FIRST ARE CHAINED, NEVER RACED. Chunking >cap candidates
    into several trains ALL ROOTED ON `main` gates them in parallel and then
    throws almost all of it away: landing is single-writer, so the first train to
    land moves `main` and every other completed train is base-stale, its ~618s
    (p50) gate run discarded. Measured over this daemon's lifetime: 20 landed vs
    57 done-but-superseded vs 38 rejected across 116 gate runs — 3x the compute
    for 1x the landings. Chunk k is therefore rooted at chunk k-1's TIP, so each
    train's gate grades EXACTLY the tree that lands if its whole ancestor prefix
    lands, and the tick can land the longest gate-passed PREFIX of the chain in
    ONE fast-forward push. The depth is bounded by the number of gate slots
    (`gate_loop.MAX_CONCURRENT_GATES`), because a train nobody can grade this
    tick is a train built for nothing. With <=cap candidates this is byte-for-
    byte the pre-chain behaviour: ONE train, rooted on `main`, no new semantics.

  * A CANDIDATE THAT CONFLICTS IS PARKED, NOT RETRIED FOREVER. A cherry-pick
    CONTENT conflict is a property of the candidate's diff and the base — the
    identical input on the next tick produces the identical conflict. Measured
    2026-08-11: 687 consecutive ticks (~11h) of "no trains assembled this tick"
    with the same two candidates logging the same conflict. After
    `ASSEMBLY_STRIKE_LIMIT` consecutive conflicting assembly attempts a candidate
    is PARKED (CONTRACT §9) with the only remedy that changes the input —
    re-anchor onto current main and re-file at the new head. An INSTRUMENT-shaped
    cherry-pick failure (git timed out, the host refused to fork) never counts a
    strike: terminalising an instrument fault is what CONTRACT §1 forbids.

Grouping REUSES `integration.conflict_groups` (union-find over shared paths):
candidates in DIFFERENT connected components share no file transitively, so one
representative from each component is a set of pairwise-disjoint members — a
valid train. The non-representative members of a component serialise behind
their representative and are picked up on a later tick, exactly as the
integration adapter schedules its gate wave.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# conflict_groups is the SOLE grouping authority (do not reimplement the
# union-find); Candidate is the shape it groups over. `_lineage` is REUSED (not
# reimplemented) so this file normalises lineage labels exactly as admission
# (integration.cross_lineage_check) and the daemon (approved_cross_lineage).
from bridge.integration import (
    ROLE,
    Candidate,
    _iso,
    _lineage,
    _now,
    _stem,
    conflict_groups,
    git,
)

# The ONE transport for ledger appends (flock'd, single os.write, fsync'd). A
# park event is hand-appended nowhere: a torn ledger line is a torn ledger for
# every later reader.
from bridge.ledger_read import iter_events
from bridge.ledger_write import LedgerAppendError, append_event

# The reviewer vocabulary `approved_cross_lineage` decides on. Imported rather
# than re-listed so the ranking below counts exactly what the approval gate
# counts (review_policy imports nothing from here, so there is no cycle).
from bridge.review_policy import KNOWN_LINEAGES

# Self-governing code is safe only as a one-member train graded by the pinned
# main judge. A tracked secret surface is never mechanically assembled.
SOLO_EXACT = {
    "omniagentos/scheduler/gate_evidence.py",
    "pipeline/bridge/gate_host.py",
    "pipeline/bridge/gate_loop.py",
    "pipeline/bridge/integration.py",
    "pipeline/bridge/review_policy.py",
    "pipeline/bridge/train_assembler.py",
    "scripts/merge-gate.sh",
}
SOLO_PREFIXES = ("pipeline/schema/", "schema/")
HUMAN_ONLY_EXACT = {".env", "configs/accounts.yaml"}
HUMAN_ONLY_PREFIXES = ("configs/keys/", "keys/", "secrets/")
HUMAN_ONLY_SUFFIXES = (".key", ".p12", ".pem", ".pfx")

MAX_TRAIN_MEMBERS = 10

# How deep a CHAIN may go. Chunk k is rooted at chunk k-1's tip, so the chain is
# only worth building as far as there are gate slots to grade it in parallel —
# gate_loop passes its real slot count (MAX_CONCURRENT_GATES = 1 local + one per
# active twin). Kept as a module default so a direct caller (tests, gate-watch)
# gets a sane bound without importing the daemon; the daemon is the authority.
MAX_CHAIN_TRAINS = 3

# Consecutive cherry-pick CONTENT conflicts before a candidate is parked. Three
# is one minute of ticks: enough that a conflict which clears the moment another
# train lands is never punished, small enough that the 687-tick thrash measured
# on 2026-08-11 stops inside the first four minutes.
ASSEMBLY_STRIKE_LIMIT = 3
# Where the strike counter lives, relative to the loop-queue root. Operational
# per-loop state, NOT a CONTRACT-governed content item, so it sits beside
# budget.json / queue.json in state/ rather than in the queue directories.
STRIKES_REL = ("state", "assembly_strikes.json")
# This module always runs inside the gate-loop daemon; the ledger events it
# writes carry that actor. Duplicated rather than imported because gate_loop
# imports THIS module (importing back would be a cycle).
ASSEMBLY_ACTOR = "gate-loop-daemon"
PARK_REMEDY = "re-anchor onto current main and re-file at the new head"

# Deterministic committer identity for cherry-picked train commits. Pinning
# name/email AND date (to each commit's author date) is what makes the train
# tip reproducible tick-to-tick. Changing the email starts a new tip-hash
# epoch — tips computed before the change stop matching recomputed ones — so
# this value must only move at a clean tick boundary.
_COMMITTER_NAME = "gate-loop"
_COMMITTER_EMAIL = "4580856+omniagentos-bot[bot]@users.noreply.github.com"


@dataclass
class Train:
    branch: str
    base: str                       # 40-hex; the commit this train was built ON
    tip: str                        # 40-hex; the train branch head
    members: list[dict] = field(default_factory=list)   # {id, branch, base, paths}
    paths: list[str] = field(default_factory=list)       # sorted union of member paths
    # --- position in a CHAIN -------------------------------------------------
    # Index 0 is a train rooted directly on `main`: `parent` is None and
    # `chain_root == base`, which is byte-for-byte the pre-chain shape. For index
    # k>0, `base` is the TIP of train k-1 (what this train was cherry-picked
    # onto) while `chain_root` stays the `main` sha the whole chain hangs off —
    # the sha that must still be `main` for any prefix of the chain to land.
    parent: str | None = None       # branch name of the train this is rooted on
    chain_root: str | None = None   # 40-hex `main` sha the chain is rooted at
    chain_index: int = 0

    @property
    def root(self) -> str:
        """The `main` sha this train's landing is anchored to.

        For an unchained train that is its own base; for a chained one it is the
        chain's root, because landing a chain lands a PREFIX in one fast-forward
        from `main` and it is `main` — not this train's own parent tip — that the
        single-writer check must compare against.
        """
        return self.chain_root or self.base

    @property
    def chained(self) -> bool:
        """True only for a train rooted on ANOTHER TRAIN's tip.

        The chain fields are omitted from every serialised form when this is
        False, so an unchained train's manifest and land receipt are byte-
        identical to the pre-chain ones. That is not cosmetic: "<=cap behaves
        exactly as before" has to hold for what lands ON DISK and gets read back
        by other tools, not merely for the in-memory dataclass (round-4 review,
        BLOCKER — the in-memory claim was vacuous for the serialised shape).
        """
        return self.parent is not None or self.chain_index > 0

    def as_manifest(self) -> dict:
        manifest = {"branch": self.branch, "base": self.base, "tip": self.tip,
                    "members": self.members, "paths": self.paths}
        if self.chained:
            manifest.update({"parent": self.parent, "chain_root": self.root,
                             "chain_index": self.chain_index})
        return manifest


def chain_groups(trains: list[Train]) -> list[list[Train]]:
    """Split an assembly result into its CHAINS, order preserved.

    `assemble_trains` emits each chain contiguously, parent before child, so a
    train continues the previous chain exactly when it names the previous train
    as its parent. Everything else (solo trains, the first train of a chain)
    starts a new single-element chain — which is precisely how the caller should
    treat an unchained train: a chain of one.
    """
    groups: list[list[Train]] = []
    for t in trains:
        if groups and t.parent and t.parent == groups[-1][-1].branch:
            groups[-1].append(t)
        else:
            groups.append([t])
    return groups


def real_paths(repo: Path, base: str, branch: str) -> set[str] | None:
    """Files the branch changes relative to `base`, from the REAL diff.

    `git diff --name-only base..branch` — the two-dot form compares the two
    trees directly (base's tree vs branch's tree), which is what actually lands.
    None means the diff could not be read (unresolvable ref / unreadable repo),
    an INSTRUMENT fact the caller must not mistake for "touches nothing".
    """
    rc, out, _ = git(repo, "diff", "--name-only", f"{base}..{branch}")
    if rc != 0:
        return None
    return {ln.strip() for ln in out.splitlines() if ln.strip()}


def touches_solo(paths: set[str]) -> list[str]:
    """Self-governing paths that must be gated in isolation."""
    return [p for p in sorted(paths)
            if p in SOLO_EXACT or p.startswith(SOLO_PREFIXES)]


def touches_human_only(paths: set[str]) -> list[str]:
    """Secret-bearing paths the mechanical lander must never assemble."""
    return [
        p for p in sorted(paths)
        if (p in HUMAN_ONLY_EXACT or p.startswith(HUMAN_ONLY_PREFIXES)
            or p.casefold().endswith(HUMAN_ONLY_SUFFIXES))
    ]


def _train_branch_name(member_idents: list[str]) -> str:
    """Deterministic branch name for a member SET (order-independent).

    The same members always yield the same name, so a train's identity — and
    therefore its `<train>@<tip>` gate-state key — is stable across ticks.

    THE ROOT IS DELIBERATELY *NOT* IN THIS NAME, and that is load-bearing for
    chains in both directions:

      * a chained train is already distinguished by the `@<tip>` half of the gate
        key. A different root is a different PARENT COMMIT, and a commit's parent
        is inside its sha, so the same members on a new root always produce a new
        tip and therefore a new key. There is no stale-key reuse to guard against;
      * and folding the root in would DESTROY the reuse that makes chaining pay.
        When train k-1 lands, train k's members become chunk 0 on the new `main`
        — same members, same base commit, byte-identical tip. With a root-free
        name that is the SAME `<train>@<tip>` key, so a gate already running (or
        already passed) for train k is picked up and finishes the job. With the
        root folded in it would be a new key, and a ~618s gate run of an
        unchanged tree would be thrown away and re-run — the exact re-gate storm
        this file's header exists to prevent.
    """
    digest = hashlib.sha256("\n".join(sorted(member_idents)).encode()).hexdigest()
    return f"train/gl-{digest[:12]}"


def _member_commits(repo: Path, base: str, branch: str) -> list[str] | None:
    """The member's own commits, oldest first. None = unreadable."""
    rc, out, _ = git(repo, "rev-list", "--reverse", "--no-merges", f"{base}..{branch}")
    if rc != 0:
        return None
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


# A failed cherry-pick is TWO different facts wearing one non-zero exit, and only
# ONE of them is about the candidate. Measured directly (git 2.43.0):
#
#   CONTENT CONFLICT   rc 1, `CHERRY_PICK_HEAD` set, stdout "CONFLICT (content):
#                      Merge conflict in <file>", stderr "error: could not apply
#                      <sha>" + "hint: After resolving the conflicts".
#   HOST FATAL         rc 128, NO `CHERRY_PICK_HEAD` — a held index.lock, a bad
#                      object, ENOSPC, OOM. Its stderr ends "fatal: cherry-pick
#                      failed", which is exactly why a substring match on
#                      "cherry-pick failed" is a trap and not a signature.
#
# So a conflict is recognised only on POSITIVE evidence, and everything else is
# an instrument fault. The failure direction is deliberate: an unrecognised
# conflict costs one missed strike (the candidate is retried, as it is today),
# while an unrecognised host fault would spend a strike on an innocent candidate
# and three flaky ticks would PARK it — terminalising an instrument condition,
# which CONTRACT §1 forbids.
_CONFLICT_RE = re.compile(
    r"CONFLICT \(|Merge conflict in|could not apply|after resolving the conflicts",
    re.IGNORECASE)


def _cherry_pick_conflicted(builder: Path, rc: int, blob: str) -> bool:
    """Positive evidence that git stopped on a CONTENT conflict, not a host fault.

    Probed BEFORE `cherry-pick --abort`, because the abort is what clears
    `CHERRY_PICK_HEAD`. `rc == 1` is required in conjunction with the evidence so
    that a stale `CHERRY_PICK_HEAD` left by an earlier crashed operation cannot
    make the next fatal error read as a conflict.
    """
    if rc != 1:
        return False
    if _CONFLICT_RE.search(blob or ""):
        return True
    probe, _, _ = git(builder, "rev-parse", "--verify", "--quiet", "CHERRY_PICK_HEAD")
    return probe == 0


def _cherry_pick_onto(builder: Path, commit: str) -> tuple[bool, str, bool]:
    """Cherry-pick ONE commit with a deterministic committer. (ok, why, instrument).

    `instrument` is True when the HOST failed (git could not run, timed out, hit
    a held index.lock or a full disk, or the commit's author date was unreadable)
    and False ONLY when git is proven to have stopped on a content CONFLICT.
    Only the conflict is evidence about the candidate, and only the conflict may
    ever count a park strike — parking a candidate for a git timeout would
    terminalise an instrument fault and send the next agent to debug the wrong
    thing.
    """
    rc, adate, err = git(builder, "show", "-s", "--format=%aI", commit)
    if rc != 0:
        return False, f"could not read author date of {commit[:12]}: {err}", True
    env = dict(os.environ)
    env["GIT_COMMITTER_NAME"] = _COMMITTER_NAME
    env["GIT_COMMITTER_EMAIL"] = _COMMITTER_EMAIL
    env["GIT_COMMITTER_DATE"] = adate.strip()
    try:
        # --no-gpg-sign is MANDATORY for a deterministic tip (cross-lineage
        # review, Gemini). If the host has commit.gpgsign=true, a signed
        # cherry-pick embeds a fresh per-commit nonce, so replaying the SAME
        # members onto the SAME base mints a DIFFERENT tip every tick — a new
        # `<train>@<tip>` gate key each tick, i.e. the 28x re-gate storm. (It
        # also avoids a hard failure when gpgsign=true but no signing key is
        # configured.) Pinned committer identity/date is necessary but NOT
        # sufficient without this.
        p = subprocess.run(
            ["git", "-C", str(builder), "cherry-pick", "--no-gpg-sign",
             "--allow-empty", "--keep-redundant-commits", commit],
            capture_output=True, text=True, env=env, timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        # Leave no half-applied pick behind.
        subprocess.run(["git", "-C", str(builder), "cherry-pick", "--abort"],
                       capture_output=True, text=True, check=False)
        return False, f"{type(exc).__name__}: {exc}", True
    if p.returncode != 0:
        # A conflict here means the member was NOT actually disjoint from what
        # is already on the train — abort cleanly and let the caller drop it.
        # CLASSIFY BEFORE ABORTING: `--abort` is what clears CHERRY_PICK_HEAD,
        # so the evidence has to be read while it still exists.
        conflicted = _cherry_pick_conflicted(
            builder, p.returncode, f"{p.stdout}\n{p.stderr}")
        subprocess.run(["git", "-C", str(builder), "cherry-pick", "--abort"],
                       capture_output=True, text=True, check=False)
        why = (p.stderr.strip() or p.stdout.strip()
               or f"cherry-pick exited {p.returncode}")
        if not conflicted:
            why = f"rc={p.returncode} (no conflict evidence): {why}"
        return False, why, not conflicted
    return True, "", False


# ----------------------------------------------------------------- ranking
# The order of `reps` decides WHICH candidates fill train #1 — the chunk gated
# locally first (reps[start:start + cap] below) — so it is a throughput lever,
# not cosmetics. Every key is a pure function of the candidate set, so the train
# tip stays reproducible tick-to-tick; `ident` is the final total-order tiebreak.

# Absent an explicit priority, a candidate inherits the operator's precedence
# rule ("work that speeds up the gate / eliminates a bottleneck lands first")
# from a keyword scan. Two tiers keep the signal sharp: P0 is bottleneck-
# eliminating vocabulary; P1 is the broader gate/latency/perf vocabulary ("gate"
# alone is too common in this estate to mean P0). Everything else is normal —
# the same DEFAULT (2) integration.py uses (CONTRACT §3) — so nothing is starved.
_PRIORITY_DEFAULT = 2
_PRIORITY_P0_RE = re.compile(
    r"throughput|bottleneck|unblock|deadlock|\bstall|back[- ]?pressure|"
    r"pipeline|merges?\s*/?\s*hr|merge[- ]rate|speed[- ]?up",
    re.IGNORECASE)
_PRIORITY_P1_RE = re.compile(
    r"\bgates?\b|latenc|parallel|concurren|throttl|\bslow|performance|daemon",
    re.IGNORECASE)


def _priority(c: Candidate) -> int:
    """Landing priority for a representative; LOWER lands first.

    1. An explicit integer `priority` on the candidate art (top level, then
       `payload`) wins outright. No candidate carries one today — proposals do
       (0 = P0 bottleneck, 1 = P1 fix), and this reads it the moment the
       implementer propagates it onto the candidate. An out-of-range or non-int
       value is ignored exactly as integration.priorities_from_disk treats it:
       one malformed file must not break ordering for every other candidate.
    2. Otherwise a keyword scan of title + payload hoists throughput/bottleneck
       work (0), then gate/latency/perf work (1); everything else is normal (2).
    """
    art = c.art if isinstance(c.art, dict) else {}
    payload = art.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    for explicit in (art.get("priority"), payload.get("priority")):
        if isinstance(explicit, int) and not isinstance(explicit, bool) and 0 <= explicit <= 3:
            return explicit
    hay = _priority_text(art, payload)
    if _PRIORITY_P0_RE.search(hay):
        return 0
    if _PRIORITY_P1_RE.search(hay):
        return 1
    return _PRIORITY_DEFAULT


def _priority_text(art: dict, payload: dict) -> str:
    """Deterministic, never-raising haystack: title + serialised payload,
    casefolded. A payload that will not serialise falls back to str(); the title
    alone still carries the operator's precedence words in practice."""
    title = art.get("title")
    parts = [title if isinstance(title, str) else ""]
    try:
        parts.append(json.dumps(payload, sort_keys=True, default=str))
    except (TypeError, ValueError):
        parts.append(str(payload))
    return "\n".join(parts).casefold()


def _cross_lineage_count(c: Candidate) -> int:
    """How many review verdicts come from a lineage OTHER than the producer's.

    The SAME rule the daemon lands on (gate_loop.approved_cross_lineage) and
    admission enforces (integration.cross_lineage_check), reusing the SAME
    `_lineage` normalisation AND the same `KNOWN_LINEAGES` vocabulary — only here
    we COUNT instead of short-circuiting, because more independent approvals mean
    higher landing confidence. An entry with no usable `lineage`, one equal to
    the producer's, or one outside the reviewer vocabulary does not count.

    THE VOCABULARY CHECK IS LOAD-BEARING, and it is not decoration. Since the
    tiered-verify change (1bb016c3f), `load_candidates` appends a SYNTHETIC
    verdict to a LOW-risk candidate — `{"lineage": "mechanical-gate", "by":
    "merge-gate", ...}` — to record that a signed gate PASS stood in for the LLM
    verdict. That label is deliberately outside `KNOWN_LINEAGES` precisely so it
    can never read as an independent review. Counting it here would let every
    LOW-tier candidate outrank genuinely twice-reviewed work for the front of
    train #1: not a landing-correctness bug, but a ranking that silently means
    "a machine ran the gate" while claiming to mean "another lab looked at it".
    """
    art = c.art if isinstance(c.art, dict) else {}
    producer = art.get("producer")
    producer = producer if isinstance(producer, dict) else {}
    pnorm = _lineage(producer.get("lineage"))
    verdicts = art.get("verdicts")
    if not isinstance(verdicts, list):
        return 0
    return sum(1 for v in verdicts
               if isinstance(v, dict)
               and (norm := _lineage(v.get("lineage"))) in KNOWN_LINEAGES
               and norm != pnorm)


def assemble_trains(repo: Path, builder: Path, cands: list[Candidate],
                    main_sha: str, *, cap: int = MAX_TRAIN_MEMBERS,
                    chain_depth: int = MAX_CHAIN_TRAINS,
                    force_solo_ids: set[str] | frozenset[str] = frozenset(),
                    root: Path | None = None,
                    ) -> tuple[list[Train], list[dict]]:
    """Build trains off `main_sha`. Returns (trains, excluded).

    `repo` is read for diffs; `builder` is a scratch checkout (a git worktree
    sharing `repo`'s object store) where the cherry-picks happen so the SERVING
    checkout's HEAD is never disturbed. Branch refs created in `builder` are
    visible in `repo`.

    `chain_depth` bounds how many trains one CHAIN may contain: train #1 is built
    on `main_sha`, train #2 on train #1's tip, and so on. The caller passes the
    number of gate slots it can actually fill this tick.

    `root` is the loop-queue root. Given one, assembly ALSO (a) refuses any
    candidate carrying a `parked/` marker — the drop-at-source check CONTRACT §9
    requires of every producer, which the daemon's own selector does not yet do —
    and (b) keeps the persistent cherry-pick-conflict strike counter that parks a
    candidate whose diff no longer applies. Without it (a direct caller, a test
    that only wants the schedule) neither runs and nothing is written.

    `force_solo_ids` is the set of candidate IDs a caller has decided must gate
    ONE AT A TIME — the failure-isolation carrier: a member of a previously-red
    multi-member train re-gates alone so its verdict is member-specific. A matching
    candidate is routed into the SAME solo list human-only/gate surfaces already
    use, even when its paths are otherwise batchable; the grouping logic and
    MAX_TRAIN_MEMBERS are untouched.

    `excluded` records every candidate that was dropped and WHY — an unreadable
    diff, a human-only surface, an empty diff, a cherry-pick conflict, a park, or
    a chunk past the chain depth — so a silent disappearance can never read as
    "nothing to land".
    """
    excluded: list[dict] = []
    # ident -> (outcome, why) for every candidate assembly actually ATTEMPTED to
    # cherry-pick this tick: "picked" | "conflict" | "instrument". This is the
    # sole input to the strike counter, so an untried candidate (serialised
    # behind its representative, or past the chain depth) neither strikes nor
    # resets — absence of evidence is not evidence.
    picks: dict[str, tuple[str, str]] = {}
    strikes = _load_strikes(root) if root is not None else {}
    # Snapshot BEFORE the park filter, which itself clears released records: the
    # file is rewritten only when something actually changed, and a release is a
    # change worth persisting.
    before = json.dumps(strikes, sort_keys=True)
    if root is not None:
        try:
            cands = _drop_parked(root, cands, strikes, excluded)
        except ParkedIndexUnreadable as exc:
            # FAIL CLOSED (round-4 review, BLOCKER). Which candidates a human has
            # parked is unknown, so NOTHING is assembled this tick. One deferred
            # tick costs 60 seconds; assembling under an unknown park set is how a
            # money-at-risk candidate that was parked precisely to keep the
            # deterministic daemon away from it gets landed by the deterministic
            # daemon. `instrument` marks this as a HOST fault so the caller can
            # report a degraded tick rather than a quiet "nothing to land".
            excluded.append({
                "id": "<assembly>", "branch": "", "instrument": True,
                "why": f"parked/ is UNREADABLE ({exc}) — refusing to assemble ANY "
                       "train this tick; an unknown park set is never an empty one"})
            return [], excluded
    trains = _assemble(repo, builder, cands, main_sha, cap=cap,
                       chain_depth=chain_depth, force_solo_ids=force_solo_ids,
                       excluded=excluded, picks=picks)
    if root is not None:
        _settle_strikes(root, cands, picks, main_sha, strikes, excluded, before)
    return trains, excluded


def _assemble(repo: Path, builder: Path, cands: list[Candidate], main_sha: str,
              *, cap: int, chain_depth: int,
              force_solo_ids: set[str] | frozenset[str] = frozenset(),
              excluded: list[dict],
              picks: dict[str, tuple[str, str]]) -> list[Train]:
    """The schedule itself: footprints, grouping, ranking, chained building."""
    # 1) Real footprint per candidate; drop the untrainable ones. The footprint
    #    is the candidate's OWN change (`base..branch` from ITS base, not main):
    #    a two-dot diff against main would show every file main moved ahead on as
    #    a spurious deletion for a candidate whose base has fallen behind.
    trainable: list[Candidate] = []
    solo: list[Candidate] = []
    for c in cands:
        paths = real_paths(repo, c.base_sha, c.branch)
        if paths is None:
            excluded.append({"id": c.ident, "branch": c.branch,
                             "why": "diff unreadable (instrument) — not trained this tick"})
            continue
        if not paths:
            excluded.append({"id": c.ident, "branch": c.branch,
                             "why": "empty diff against main — nothing to land"})
            continue
        human_only = touches_human_only(paths)
        if human_only:
            excluded.append({"id": c.ident, "branch": c.branch,
                             "why": "touches human-only secret surface; mechanical "
                                    "landing refused: " + ", ".join(human_only[:5])})
            continue
        c.paths = sorted(paths)
        # A forced-solo candidate re-gates ALONE even when its paths would batch:
        # it is a member of a previously-red multi-member train whose verdict was
        # not member-specific, so its terminal claim must be re-proven on a one-
        # member tree. Routed through the SAME solo list as a gate/secret surface.
        if touches_solo(paths) or c.ident in force_solo_ids:
            solo.append(c)
        else:
            trainable.append(c)

    if not trainable and not solo:
        return []

    # Pipeline/gate/schema changes are judged by the pinned-main gate one at a time. The
    # candidate's modified judge is code under test, never the authority running
    # the gate, so this cannot self-approve a weakened instrument.
    #
    # SOLO TRAINS ARE NEVER CHAINED. Each one is rooted on `main` on purpose: the
    # whole point of a one-member train is that the gate grades THIS self-
    # governing change against a green main and nothing else. Rooting one on
    # another's tip would put a second unreviewed change to the same class of
    # surface into the tree being graded, which is the isolation this rule
    # exists to hold. They therefore still race each other; the chain lands the
    # first one that passes and the rest re-assemble, exactly as before.
    trains: list[Train] = []
    solo.sort(key=lambda c: (
        _priority(c), len(c.paths), -_cross_lineage_count(c),
        c.art.get("created_at") or "", c.ident,
    ))
    for c in solo:
        train = _build_train(repo, builder, [c], main_sha, excluded, picks=picks)
        if train is not None:
            trains.append(train)

    if not trainable:
        return trains

    # 2) Group. conflict_groups returns connected components of the shared-file
    #    graph; the representative (index 0, deterministically ordered) of each
    #    component is pairwise-disjoint from every other representative, so the
    #    representatives are exactly a valid, conflict-free train.
    groups = conflict_groups(trainable)
    reps: list[Candidate] = []
    for grp in groups:
        head, tail = grp[0], grp[1:]
        reps.append(head)
        for c in tail:
            shared = sorted(set(c.paths) & set(head.paths))[:3]
            excluded.append({"id": c.ident, "branch": c.branch,
                             "why": f"serialises behind {head.ident[:19]} "
                                    f"(shares {', '.join(shared)}) — next tick"})

    # Quality + priority order (was pure FIFO on created_at). Determinism is
    # preserved — every key is a pure function of the candidate set and `ident`
    # is the final tiebreak — but the leading keys now steer which candidates
    # land in train #1 (chunked below as reps[start:start + cap], gated first):
    #   1. _priority()             gate/bottleneck-eliminating work first (operator rule)
    #   2. len(c.paths) asc        smallest REAL diff first — the biggest gate-pass lever:
    #                              a train fails as a UNIT (no bisection yet), so train #1
    #                              must minimise per-member defect probability, which grows
    #                              with the number of files a member touches
    #   3. cross-lineage desc      more independent approvals = higher landing confidence
    #   4. created_at asc, ident   FIFO within a tier, then a stable total order
    reps.sort(key=lambda c: (
        _priority(c),
        len(c.paths),
        -_cross_lineage_count(c),
        c.art.get("created_at") or "",
        c.ident,
    ))

    # 3) Build a CHAIN of linear trains, each up to `cap` members. Every rep is
    #    pairwise-disjoint from every other rep (they are representatives of
    #    DISTINCT conflict components, which by construction share no file), so
    #    any chunk of them is a valid conflict-free train.
    #
    #    Chunk 0 is rooted on `main`; chunk k is rooted on chunk k-1's TIP. The
    #    chunks used to be rooted on `main` TOO, and that is the waste this
    #    replaces: landing is single-writer, so the first train to land moved
    #    `main` and every other completed train became base-stale and had its
    #    whole gate run discarded (20 landed vs 57 done-but-superseded, measured).
    #    Rooted on its predecessor, train k's gate grades exactly the tree that
    #    lands if trains 0..k land together — the same invariant a single train
    #    already gives its own members — so the tick can land the longest
    #    gate-PASSED PREFIX of the chain in ONE fast-forward push.
    #
    #    With <=cap reps this loop runs once and produces exactly ONE train on
    #    `main`, with `parent=None` and `chain_index=0`: the pre-chain behaviour,
    #    unchanged down to the branch name.
    depth = max(1, int(chain_depth))
    chain_base = main_sha            # what the NEXT train is cherry-picked onto
    parent: str | None = None
    built = 0
    for start in range(0, len(reps), cap):
        if built >= depth:
            # A train nobody can grade this tick is a train built for nothing:
            # it would be dispatched-deferred, and its root moves the moment any
            # prefix lands. Recorded, never silently dropped.
            for c in reps[start:]:
                excluded.append({"id": c.ident, "branch": c.branch,
                                 "why": f"beyond the chain depth bound ({depth} gate "
                                        "slot(s)) — next tick"})
            break
        chunk = reps[start:start + cap]
        train = _build_train(repo, builder, chunk, chain_base, excluded,
                             parent=parent, chain_root=main_sha,
                             chain_index=built, picks=picks)
        if train is None:
            # Nothing in this chunk survived, so it adds no commits: the NEXT
            # chunk is still validly rooted where this one would have been.
            continue
        trains.append(train)
        built += 1
        chain_base, parent = train.tip, train.branch
    return trains


def _build_train(repo: Path, builder: Path, members_in: list[Candidate],
                 base_sha: str, excluded: list[dict], *,
                 parent: str | None = None, chain_root: str | None = None,
                 chain_index: int = 0,
                 picks: dict[str, tuple[str, str]] | None = None) -> Train | None:
    """Cherry-pick one chunk of pairwise-disjoint candidates onto a fresh branch
    rooted at `base_sha`, deterministically. Returns the Train, or None if
    nothing in the chunk survived (each drop is recorded in `excluded`).

    `base_sha` is `main` for an unchained train (`chain_index == 0`) and the
    PREVIOUS train's tip for a chained one; `chain_root` stays the `main` sha the
    whole chain hangs off. `picks` collects the per-candidate cherry-pick outcome
    for the strike counter."""
    branch = _train_branch_name([c.ident for c in members_in])
    rc, _, err = git(builder, "checkout", "--force", "-B", branch, base_sha)
    if rc != 0:
        excluded.append({"id": "<train>", "branch": branch,
                         "why": f"could not start train branch at {base_sha[:12]} "
                                f"(instrument): {err}"})
        return None

    members: list[dict] = []
    union: set[str] = set()
    for c in members_in:
        # The member's OWN commits (from its own base). Cherry-picking these onto
        # the train branch IS the forward-port onto current main (plus, for a
        # chained train, onto the ancestor prefix that lands before it).
        commits = _member_commits(repo, c.base_sha, c.branch)
        if not commits:
            # No commits over its base means either nothing to land or an
            # unreadable range; either way it is not a defect in the code.
            why = "no commits over its base (instrument/empty) — skipped"
            excluded.append({"id": c.ident, "branch": c.branch, "why": why})
            _note_pick(picks, c.ident, "skipped", why)
            continue
        ok = True
        for commit in commits:
            picked, why, instrument = _cherry_pick_onto(builder, commit)
            if not picked:
                ok = False
                if instrument:
                    detail = (f"cherry-pick could not RUN onto train (instrument, no "
                              f"strike): {why[:200]}")
                else:
                    detail = (f"cherry-pick conflict onto train "
                              f"(not disjoint after all): {why[:200]}")
                excluded.append({"id": c.ident, "branch": c.branch, "why": detail})
                _note_pick(picks, c.ident,
                           "instrument" if instrument else "conflict", why[:200])
                break
        if not ok:
            continue
        source_branch = c.art.get("branch") if isinstance(c.art, dict) else None
        members.append({"id": c.ident, "branch": source_branch or c.branch,
                        "base": c.base_sha,
                        "paths": sorted(c.paths)})
        union |= set(c.paths)
        _note_pick(picks, c.ident, "picked", "")

    if not members:
        return None

    rc, tip, _ = git(builder, "rev-parse", "HEAD")
    if rc != 0 or not tip:
        excluded.append({"id": "<train>", "branch": branch,
                         "why": "could not resolve train tip after assembly (instrument)"})
        return None

    return Train(branch=branch, base=base_sha, tip=tip.strip(),
                 members=members, paths=sorted(union),
                 parent=parent, chain_root=chain_root or base_sha,
                 chain_index=chain_index)


def _note_pick(picks: dict[str, tuple[str, str]] | None, ident: str,
               outcome: str, why: str) -> None:
    """Record ONE cherry-pick outcome per candidate per assembly.

    A candidate rides in exactly one chunk, so the first recorded outcome is the
    whole story; a later write would only ever be a bug in the caller, and
    silently overwriting a conflict with a success is the direction that hides
    the thrash this counter exists to stop.
    """
    if picks is None or ident in picks:
        return
    picks[ident] = (outcome, why)


# ------------------------------------------------- conflict strikes / parking
# A cherry-pick CONTENT conflict is deterministic: the same member commits onto
# the same base conflict the same way every time. Retrying it every 60s is the
# never-re-run-an-unchanged-input violation this estate has already paid for —
# measured 2026-08-11, 687 consecutive ticks (~11h) of "no trains assembled this
# tick" logging the same conflict for the same two candidates. Only ONE thing
# changes that input: re-anchoring the candidate onto current main and re-filing
# at the new head, and that is a producer's decision, not this loop's. So the
# candidate is PARKED (CONTRACT §9: marker + `parked` event + one alert, released
# only by an authenticated `unparked`), never rejected — a stale base is not a
# defect in the code.


def _write_json_atomic(path: Path, obj: dict) -> None:
    """tmp + fsync + os.replace. A half-written strike file read by the next tick
    is a counter that silently restarts at zero."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _strikes_path(root: Path) -> Path:
    return root.joinpath(*STRIKES_REL)


# EVERY FIELD OF A LOADED STRIKE RECORD IS UNTRUSTED INPUT, and these three
# readers are the only way any of it is allowed into the code (round-3 review,
# MAJOR). The wound: `[b for b in (rec.get("bases") or [])]` looks defensive but
# `or []` only replaces FALSY values, so `"bases": true` sails past it and the
# comprehension raises `TypeError: 'bool' object is not iterable` — one malformed
# field in one on-disk JSON file taking down assembly for the whole queue.
#
# The same shape appeared on six further lines (the strike count and the last
# conflict text, read into the reason string, the ledger event and the marker),
# which is why this is a TYPE-CHECKED READER used everywhere rather than a fix at
# the crash site: incomplete propagation is this estate's measured house defect,
# and a guard that only covers the line someone happened to test is how it
# recurs. Nothing below can raise, whatever the file says.


def _as_str_list(value: object) -> list[str]:
    """Untrusted JSON -> a list of strings. A non-sequence is not iterated."""
    if isinstance(value, (list, tuple)):
        return [v for v in value if isinstance(v, str)]
    return []


def _as_count(value: object) -> int:
    """Untrusted JSON -> a positive strike count, or 0.

    `bool` is excluded explicitly because it IS an `int` in Python, so a JSON
    `true` would otherwise count as a strike nobody observed.
    """
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return 0


def _as_text(value: object, limit: int = 300) -> str:
    """Untrusted JSON -> a bounded string. A non-string carries no message."""
    return value[:limit] if isinstance(value, str) else ""


def _load_strikes(root: Path | None) -> dict[str, dict]:
    """The per-candidate conflict record, or an empty one.

    ABSENT, unreadable, or corrupt all start EMPTY on purpose: this counter can
    only ever park a candidate, so failing toward "no strikes" merely re-earns
    them over the next few ticks, while failing toward "keep whatever survived
    the corruption" could park on a number nobody can account for.
    """
    if root is None:
        return {}
    try:
        body = json.loads(_strikes_path(root).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    if not isinstance(body, dict):
        return {}
    entries = body.get("candidates")
    if not isinstance(entries, dict):
        return {}
    return {k: dict(v) for k, v in entries.items()
            if isinstance(k, str) and isinstance(v, dict)}


def _save_strikes(root: Path, strikes: dict[str, dict]) -> None:
    try:
        _write_json_atomic(_strikes_path(root), {
            "version": 1, "updated_at": _iso(_now()),
            "limit": ASSEMBLY_STRIKE_LIMIT, "candidates": strikes})
    except OSError:
        # A counter that cannot be persisted must not take the tick down with
        # it: the worst case is that the streak restarts, which costs ticks, not
        # correctness.
        pass


class ParkedIndexUnreadable(OSError):
    """`parked/` exists but could not be enumerated.

    Raised INSTEAD of returning an empty set (round-4 review, BLOCKER). An
    unreadable park directory and an empty one are opposite facts: the second
    means "nothing is parked", the first means "which items are parked is
    UNKNOWN". Returning the empty set for both is favourable absence in its
    purest form — it makes every human-parked candidate, including the
    money-at-risk one this estate parked on 2026-08-11 specifically so the
    deterministic daemon could not land it, eligible to assemble and land.
    """


def _parked_index(root: Path) -> tuple[set[str], dict[str, dict]]:
    """(every id `parked/` names, the markers THIS module wrote, by id).

    A park marker is read for exactly one purpose — keep a parked item away from
    a producer — so this over-excludes deliberately (the same discipline
    spawn_builders settled on after one malformed sidecar took every builder to
    zero): the id derived from the filename AND the id inside the body both
    count, and an unreadable SIDECAR excludes its filename-derived id rather than
    aborting the whole assembly. Over-excluding costs one deferred landing;
    under-excluding hands the mechanical lander something a human parked.

    An unreadable DIRECTORY is the opposite case and raises: one sidecar we
    cannot parse still tells us which id to exclude, but an enumeration failure
    tells us nothing about any of them.
    """
    out: set[str] = set()
    ours: dict[str, dict] = {}
    pdir = root / "parked"
    try:
        if not pdir.is_dir():
            # `is_dir()` swallows every OSError, so False is ambiguous: genuinely
            # absent (fine — nothing is parked yet) or unstattable (NOT fine).
            # stat() is what tells the two apart.
            os.stat(pdir)
            raise ParkedIndexUnreadable(f"{pdir} exists but is not a directory")
        entries = sorted(pdir.glob("*.json"))
    except FileNotFoundError:
        return out, ours
    except ParkedIndexUnreadable:
        raise
    except OSError as exc:
        raise ParkedIndexUnreadable(
            f"could not enumerate {pdir} ({type(exc).__name__}: {exc}); which "
            "candidates are parked is UNKNOWN, which is never the same as none"
        ) from exc
    for p in entries:
        stem = p.name[:-len(".json")]
        if stem.endswith(".parkinfo"):
            stem = stem[:-len(".parkinfo")]
        if stem:
            out.add(stem.replace("_", ":", 1))
        try:
            body = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(body, dict):
            continue
        ident = body.get("id")
        if isinstance(ident, str) and ident.strip():
            ident = ident.strip()
            out.add(ident)
            if body.get("by") == ASSEMBLY_ACTOR:
                ours[ident] = body
    return out, ours


def parked_ids(root: Path) -> set[str]:
    """Every id the `parked/` directory names. Raises ParkedIndexUnreadable."""
    return _parked_index(root)[0]


def _drop_parked(root: Path, cands: list[Candidate], strikes: dict[str, dict],
                 excluded: list[dict]) -> list[Candidate]:
    """Drop-at-source for parked candidates (CONTRACT §9).

    "Producers skip any item with a file in `parked/`" — the same check every
    other producer in this pipeline already makes at selection. The daemon's own
    loader does not yet make it (a known finding), which is why a human-parked
    candidate kept reaching assembly, and conflicting, every 60 seconds for
    hours after a human had explicitly parked it.

    THE MARKER IS THE ONLY AUTHORITY HERE, and that is deliberate. The strike
    counter is a counter, not a second lock: if it also gated selection, an
    authenticated un-park (which appends `unparked` and removes the marker) would
    be silently overruled by a JSON file this module wrote, and no human could
    release the candidate at all. So a strike record that still says `parked`
    while the marker is GONE is treated as a park EPISODE THAT ENDED: the record
    is cleared and the candidate earns its strikes again from zero. Released
    legitimately, it gets a clean run; released by a stray `rm`, it re-parks
    after `ASSEMBLY_STRIKE_LIMIT` more conflicts and alerts again. This loop
    never deletes a marker and never mints an `unparked` — it only reads.
    """
    parked, ours = _parked_index(root)
    recorded: set[str] | None = None          # ledger `parked` ids, read at most once
    kept: list[Candidate] = []
    for c in cands:
        rec = strikes.get(c.ident)
        if c.ident in parked:
            # HEAL A PARK WHOSE EVENT NEVER LANDED. The marker is written before
            # the ledger append, so a crash or a full disk between the two leaves
            # an item that every producer correctly skips and that queue replay
            # still reports as pending — invisible in the one place an operator
            # looks.
            #
            # THE MARKER IS THE EVIDENCE, NOT THE STRIKE RECORD (round-4 review,
            # MAJOR). The first version asked the strike record whether an event
            # was owed, which fails exactly when it matters: if the ledger append
            # AND the replacement strike write both fail (one full disk fails
            # both), the reloaded record carries no `event_written: false` and
            # the park is frozen forever with no ledger evidence. A marker WE
            # wrote is itself the proof that an event is owed, so the healer asks
            # the ledger directly and repairs from the marker's own contents.
            unfinished = bool(isinstance(rec, dict) and rec.get("parked")
                              and not rec.get("event_written"))
            if c.ident in ours or unfinished:
                if recorded is None:
                    recorded = _park_events_in_ledger(root)
                if recorded is not None and c.ident not in recorded:
                    marker = ours.get(c.ident) or {}
                    healed = _append_park_event(
                        root, c, _rec_from_marker(marker) if marker else (rec or {}),
                        excluded, reason=_as_text(marker.get("reason"), 4000) or None)
                    if healed:
                        recorded.add(c.ident)
                        if isinstance(rec, dict):
                            rec["event_written"] = True
            excluded.append({
                "id": c.ident, "branch": c.branch,
                "why": ("PARKED (parked/ marker present) — a park is released only "
                        "by an authenticated `unparked` event, never by this loop")})
            continue
        if isinstance(rec, dict) and rec.get("parked"):
            strikes.pop(c.ident, None)
        kept.append(c)
    return kept


def _park_events_in_ledger(root: Path) -> set[str] | None:
    """Ids that already carry a `parked` event. None = the ledger is unreadable.

    None is NOT an empty set: on an unreadable ledger the healer must not append,
    because it cannot tell an owed event from one already written, and a torn
    ledger is a far louder problem than a late heal. Decoded through the SHARED
    `iter_events` for the same reason every other reader here does: two writers
    can land two whole events on one physical line, and `json.loads` returns
    neither — an event hidden that way would read as owed and be written twice.
    """
    path = root / "ledger.jsonl"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return set()
    except OSError:
        return None
    return {ev.get("id") for ev in iter_events(text)
            if ev.get("event") == "parked" and ev.get("id")}


def _rec_from_marker(marker: dict) -> dict:
    """A strike-record-shaped view of a park marker, for healing.

    Every field still goes through the typed readers downstream, so a marker
    edited by hand cannot crash the healer either.
    """
    detail = marker.get("detail")
    detail = detail if isinstance(detail, dict) else {}
    return {"strikes": detail.get("strikes"), "bases": detail.get("bases"),
            "last_reason": detail.get("last_conflict")}


def _settle_strikes(root: Path, cands: list[Candidate],
                    picks: dict[str, tuple[str, str]], main_sha: str,
                    strikes: dict[str, dict], excluded: list[dict],
                    before: str) -> None:
    """Fold this assembly's cherry-pick outcomes into the persistent counter.

      * a CONTENT conflict adds a strike, and the `ASSEMBLY_STRIKE_LIMIT`-th
        consecutive one parks the candidate;
      * a SUCCESSFUL pick clears the record outright — the diff applies, so
        whatever the earlier conflicts were about is gone;
      * an INSTRUMENT failure changes nothing in either direction: it is not
        evidence about the candidate, and letting a git timeout either strike or
        absolve one would make the counter a measure of host health;
      * a candidate assembly never TRIED (serialised behind its representative,
        past the chain depth) keeps whatever it had — untried is not "cleared";
      * a candidate that has left the eligible set entirely drops its record, so
        the file stays bounded. A park record is kept: it is the evidence for a
        marker that only a human can release.

    `before` is the record as it was LOADED, so the file is rewritten only when
    this tick actually changed something.
    """
    live = {c.ident for c in cands}
    by_id = {c.ident: c for c in cands}
    now = _iso(_now())
    for ident in [i for i in strikes if i not in live and not strikes[i].get("parked")]:
        strikes.pop(ident, None)
    for ident, (outcome, why) in sorted(picks.items()):
        if outcome == "picked":
            strikes.pop(ident, None)
            continue
        if outcome != "conflict":
            continue                     # instrument / skipped: no evidence either way
        rec = strikes.get(ident)
        rec = dict(rec) if isinstance(rec, dict) else {}
        # Every field is read through the typed readers: a non-int, a bool, or a
        # negative restarts the count at this tick's ONE observed conflict rather
        # than inheriting a number nobody can account for.
        rec["strikes"] = _as_count(rec.get("strikes")) + 1
        rec.setdefault("first_at", now)
        rec["last_at"] = now
        rec["last_reason"] = _as_text(why)
        bases = _as_str_list(rec.get("bases"))
        if main_sha not in bases:
            bases.append(main_sha)
        # Bounded: the evidence an operator needs is "did main move while this
        # kept conflicting", which the last handful of bases answers.
        rec["bases"] = bases[-(2 * ASSEMBLY_STRIKE_LIMIT):]
        strikes[ident] = rec
        if rec["strikes"] >= ASSEMBLY_STRIKE_LIMIT and not rec.get("parked"):
            cand = by_id.get(ident)
            if cand is not None:
                _park_candidate(root, cand, rec, excluded, now)
    if json.dumps(strikes, sort_keys=True) != before:
        _save_strikes(root, strikes)


def _park_reason(rec: dict) -> str:
    """The one park reason string, built from the record so the marker, the
    ledger event and any later heal cannot drift apart.

    Reads the record through the typed readers: this runs on a park path AND on
    the next-tick heal path, so a malformed field here would take assembly down
    on every tick after the one that wrote it.
    """
    count = _as_count(rec.get("strikes")) or ASSEMBLY_STRIKE_LIMIT
    bases = _as_str_list(rec.get("bases"))
    return (
        f"cherry-pick CONFLICT onto the assembly train on {count} consecutive "
        f"attempts (base(s): {', '.join(b[:12] for b in bases[-3:]) or 'unknown'}). "
        "A content conflict is deterministic — the identical diff onto the "
        "identical base conflicts identically every tick — so retrying it is "
        "re-running an unchanged input, not making progress (measured 2026-08-11: "
        "687 consecutive ticks of the same conflict). The candidate's diff no "
        f"longer applies to current main. Remedy: {PARK_REMEDY}."
    )


def _append_park_event(root: Path, cand: Candidate, rec: dict,
                       excluded: list[dict], reason: str | None = None) -> bool:
    """Append the `parked` event through the flock'd writer. False = not written.

    `reason` lets a HEAL reuse the marker's own recorded reason verbatim, so the
    repaired event says what the park said rather than a text re-derived from a
    strike record that may no longer exist.
    """
    count = _as_count(rec.get("strikes")) or ASSEMBLY_STRIKE_LIMIT
    try:
        root.mkdir(parents=True, exist_ok=True)
        append_event(root, {
            "ts": _iso(_now()), "role": ROLE, "event": "parked", "id": cand.ident,
            "base_sha": cand.base_sha, "actor": ASSEMBLY_ACTOR,
            "detail": {
                "reason": reason or _park_reason(rec), "alerted": True,
                "class": "blocked-on-human", "area": "assembly",
                "kind": "assembly-cherry-pick-conflict",
                "strikes": count, "limit": ASSEMBLY_STRIKE_LIMIT,
                "remedy": PARK_REMEDY, "bases": _as_str_list(rec.get("bases")),
                "branch": cand.branch,
                "last_conflict": _as_text(rec.get("last_reason")),
            },
        })
    except (LedgerAppendError, OSError, ValueError) as exc:
        excluded.append({"id": cand.ident, "branch": cand.branch,
                         "why": f"park event NOT recorded ({type(exc).__name__}: "
                                f"{exc}) — the marker holds the candidate out of "
                                "selection; the event is healed next tick"})
        return False
    return True


def _alert_once(root: Path, line: str) -> None:
    """One line on the alert target. Never raises — an unwritable ALERTS.md is
    not a reason to abandon a park that already happened."""
    try:
        root.mkdir(parents=True, exist_ok=True)
        with open(root / "ALERTS.md", "a", encoding="utf-8") as fh:
            fh.write(f"- {_iso(_now())} gate-loop: {line}\n")
    except OSError:
        pass


def _park_candidate(root: Path, cand: Candidate, rec: dict,
                    excluded: list[dict], now: str) -> bool:
    """Park one candidate: MARKER first, then the ledger event, then ONE alert.

    THE ORDER IS LOAD-BEARING (round-2 review, MAJOR). It used to be event-first,
    and that had a thrash: if the marker write then failed (ENOSPC), the record
    still said `parked` while `parked/` held nothing — and the next tick's
    drop-at-source, which correctly treats a missing marker as "the episode
    ended", wiped the record and let the candidate earn three more strikes and
    another `parked` event, forever.

    Marker-first makes every partial outcome safe:
      * marker fails      -> NOTHING is claimed. No `parked` flag, no event, the
                             strikes stand, one instrument alert, and the next
                             conflicting tick tries the whole park again.
      * marker ok, event fails -> the candidate IS parked (every producer skips
                             it on the marker) and the record says
                             `event_written: false`, so the next tick heals the
                             missing event from the marker's presence.
      * both ok           -> `parked` + one alert, the CONTRACT §9 shape.
    """
    ident = cand.ident
    count = _as_count(rec.get("strikes")) or ASSEMBLY_STRIKE_LIMIT
    # The artifact is producer-controlled, so its `branch` is untrusted the same
    # way the strike record is; a non-string must not reach the marker.
    source_branch = cand.art.get("branch") if isinstance(cand.art, dict) else None
    source_branch = source_branch if isinstance(source_branch, str) else None
    try:
        _write_json_atomic(root / "parked" / f"{_stem(ident)}.json", {
            "contract": "v1.1", "id": ident, "kind": "candidate",
            "class": "parked", "at": _iso(_now()), "by": ASSEMBLY_ACTOR,
            "reason": _park_reason(rec), "needs": "human",
            "remedy": PARK_REMEDY,
            "unpark_condition": (
                "a re-anchored candidate re-filed at the new head (a NEW id, which "
                "supersedes this one) — or an authenticated `unparked` event if the "
                "conflict is proven gone; no loop may un-park itself"),
            "branch": source_branch or cand.branch,
            "base_sha": cand.base_sha,
            "detail": {"strikes": count, "limit": ASSEMBLY_STRIKE_LIMIT,
                       "bases": _as_str_list(rec.get("bases")),
                       "last_conflict": _as_text(rec.get("last_reason"))},
        })
    except OSError as exc:
        # NOT a park: nothing is flagged and nothing is appended, so the next
        # conflicting tick re-attempts the whole thing. Alerted once per episode
        # so a full disk is visible without becoming its own storm.
        if not rec.get("park_write_failed"):
            _alert_once(root, f"could NOT write the park marker for {ident} after "
                              f"{count} cherry-pick conflicts ({type(exc).__name__}: "
                              f"{exc}) — the candidate is NOT parked and will keep "
                              "being retried until this is fixed (instrument)")
        rec["park_write_failed"] = f"{type(exc).__name__}: {exc}"
        excluded.append({"id": ident, "branch": cand.branch,
                         "why": f"park marker unwritable ({type(exc).__name__}: {exc}) "
                                "— NOT parked, strikes kept, retried next tick"})
        return False
    rec.pop("park_write_failed", None)
    rec["parked"] = True
    rec["parked_at"] = now
    rec["event_written"] = _append_park_event(root, cand, rec, excluded)
    _alert_once(root, f"PARKED {ident} after {count} consecutive cherry-pick "
                      f"conflicts during train assembly. {PARK_REMEDY}.")
    excluded.append({"id": ident, "branch": cand.branch,
                     "why": f"PARKED after {count} consecutive cherry-pick conflicts "
                            f"— remedy: {PARK_REMEDY}"})
    return True
