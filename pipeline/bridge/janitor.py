#!/usr/bin/env python3
"""Queue janitor. Retention for var/loopqueue, per CONTRACT.md §10.

NOT the disk reaper and not the process reaper — those free gigabytes and kill
idle agents. This one enforces artifact retention so the queue does not grow
without bound, which is the failure mode that shows up after a weekend rather
than after an hour.

It deletes files, so it **reports by default and only acts with --apply.**

Duties, each with the reason it exists:

  terminal artifacts    delete after 7d — the ledger keeps the history
  PARKED artifacts      EXEMPT while the marker exists. A human taking over a
                        week is the normal case for a park; deleting the work
                        underneath them is the worst thing in this file.
  expired rejections    inert at expires_at, deleted 30d later
  receipts              30d, then only those a `merged` event references
  claim markers         expired ones on sight — but NEVER an unparseable marker
                        younger than 10 min, which is a claim mid-creation
                        (O_EXCL creates the file empty, the body lands next).
                        Every reap appends a `claim_expired` ledger event naming
                        the id, the dead actor, and the age past expiry — a
                        silent unlink is how "no claim has ever been reaped"
                        became unfalsifiable (see --claims-only below).
  ledger.jsonl          roll at 100 MB. Never delete, never rewrite.
  vanished park marker  ALERT, never release. Every process on the host can
                        delete a file, so "marker gone" is not "human approved".
  stale inquiries       no terminal event after 30d is a BUG in planning, not a
                        backlog — one line to the alert target
  salvage refs          `salvage/gl-contentfree-*` branches (bridge/gate_loop.py's
                        content-free-heal carve-out mints one per heal to preserve
                        the old tip) older than 14d AND reachable from main are
                        deleted with `git branch -d` — never `-D`, never forced;
                        a refusal is kept and logged, not retried harder. OPT-IN:
                        only runs when this Janitor is given a `repo` (the queue
                        and the git working tree are independent concerns here,
                        same split as GateLoop's own `root`/`repo` split).

Usage:
    janitor.py --loops-root var/loopqueue            # report only
    janitor.py --loops-root var/loopqueue --apply    # actually delete

    janitor.py --loops-root var/loopqueue --claims-only --apply
        Skip every sweep above except claim markers. This is the mode the
        300s cadence plist (com.threeloops.claim-reaper.plist) runs: the
        7-day retention sweeps are cheap to run daily, but a claim TTL of
        minutes needs its own reaper on its own cadence, and running the
        FULL sweep every 300s would mean rescanning receipts/, rejected/
        and every artifact directory 288 times a day for no reason — this
        flag makes the fast cadence cheap enough to actually run.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

try:
    # Preferred: reuse whatever "bridge.claim" identity is ALREADY loaded in
    # this process (tests importing `from bridge import claim`, or any other
    # caller with the package context established). This is the only way to
    # guarantee `except claim.MarkerUnreadable` here catches the SAME
    # exception class that bridge/claim.py itself raises.
    #
    # R2F2 (2026-08-08 review, BLOCKER): the previous version unconditionally
    # did `sys.path.insert(0, .../bridge); import claim`. Under a bare
    # `import claim`, Python registers a SECOND, DISTINCT module object under
    # the module name "claim" — separate from "bridge.claim" if anything else
    # in the same process had already imported it that way (which every test
    # in this suite now does). Two module objects means two separate
    # `MarkerUnreadable` *classes*; `except claim.MarkerUnreadable` in one
    # module never catches an instance raised under the other identity's
    # class object, silently defeating the exact fail-closed handling B1 and
    # B3 depend on. This was invisible until it mattered, which is exactly
    # why it is fixed as a BLOCKER rather than noted and left.
    from bridge import claim
except ImportError:
    # Standalone execution (`python bridge/janitor.py` — how the launchd
    # plist actually invokes this). Python puts only THIS script's own
    # directory on sys.path, never its parent, so "bridge" is not resolvable
    # as a package at all here. But in a standalone process, janitor.py is
    # the ONLY entry point — nothing else could already hold a competing
    # "bridge.claim" identity to alias against — so a bare sibling import is
    # safe in this branch specifically, and it is the only branch that ever
    # touches sys.path.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import claim  # noqa: E402

# Same two-mode dance as `claim` above, for the same reason: the launchd plist
# runs this file as a SCRIPT, so `bridge` is not resolvable as a package and a
# `from bridge.ledger_read import ...` here would kill the daemon at import.
# Unlike `claim`, a split module identity is harmless for this one — but the
# reason is narrower than "it only exports functions and constants", which is
# what this comment used to say and which cross-lineage review (GPT-5.6-Sol,
# 2026-08-09) showed was false on two counts: ledger_read also exports the
# EventList CLASS, and parse_events deliberately propagates MemoryError.
#
# The accurate reason: janitor imports neither. It takes only parse_events —
# and since that returns a plain list of dicts, and janitor skips silently
# either way (retention has no use for WHY a line was bad, only for the events
# it can read), it no longer even needs the OK constant. It lets MemoryError
# propagate rather than catching it — so no isinstance and no `except` crosses
# the boundary from here. integrity.py does import EventList, but it resolves
# ledger_read through its own unconditional sys.path shim, so it only ever sees
# one identity. If a future caller starts doing `isinstance(x, EventList)` or
# `except` on something ledger_read raises, this fallback needs revisiting.
try:
    from bridge.ledger_read import parse_events
    from bridge.ledger_write import append_event
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from ledger_read import parse_events  # noqa: E402
    from ledger_write import append_event  # noqa: E402


class LedgerTornError(RuntimeError):
    """The ledger is unreadable. Refuse to act rather than act on emptiness."""


# `completed` is terminal alongside `merged`/`rejected` (ruling D13a): out-of-repo
# / host-ops work verified and applied but with no merge_sha. Its artifacts follow
# the SAME 7-day terminal retention as `merged` — it never references a receipt via
# a `merged` event, so the receipt sweep (merged-only) is deliberately unchanged.
TERMINAL_EVENTS = {"merged", "completed", "rejected", "closed"}
ARTIFACT_DIRS = ("inquiries", "findings", "proposals", "candidates")
TERMINAL_RETENTION_DAYS = 7
REJECTED_RETENTION_DAYS = 30
RECEIPT_RETENTION_DAYS = 30
PARKED_MAX_DAYS = 90
STALE_INQUIRY_DAYS = 30
CLAIM_GRACE_SECONDS = 600  # 10 min — see module docstring
LEDGER_ROLL_BYTES = 100 * 1024 * 1024

# Salvage-ref retention (grok follow-up on #410). Must match the naming
# bridge/gate_loop.py's `_content_free_reconcile` mints:
# f"salvage/gl-contentfree-{stamp}-{local_sha[:7]}" with
# stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()).
SALVAGE_REF_PREFIX = "salvage/gl-contentfree-"
SALVAGE_REF_MAX_AGE_DAYS = 14
_SALVAGE_REF_RE = re.compile(
    r"^salvage/gl-contentfree-(?P<stamp>\d{8}T\d{6}Z)-[0-9a-f]+$")


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None


def _parse_salvage_stamp(stamp: str) -> datetime | None:
    try:
        return datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None


def _git(repo: Path, *args: str, timeout: int = 60) -> tuple[int, str, str]:
    """Read-only-by-convention git, same shape as bridge.integration.git.

    Duplicated rather than imported, for the same reason `_now`/`_iso`/
    `_parse` above are duplicated instead of imported from
    bridge.integration: this module runs standalone under launchd, where
    `bridge` is not resolvable as a package (see the two-mode import dance
    at the top of this file), and pulling in integration.py's own much
    heavier import chain for two small functions is not worth it.
    """
    try:
        p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                           text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, "", f"{type(exc).__name__}: {exc}"
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def _is_ancestor_of_main(repo: Path, sha: str) -> bool | None:
    """True/False, or None when it could not be determined.

    Same `rev-list --not main` implementation as
    bridge.integration.is_ancestor_of_main, duplicated for the reason `_git`
    above is: deliberately NOT `merge-base --is-ancestor` — this estate's git
    guard hook intercepts commands containing merge verbs, and a hook-blocked
    probe returns an rc indistinguishable from "not an ancestor", which here
    would silently keep (never wrongly delete) a branch this sweep should
    have been able to reap. None is not False; a caller must never delete on
    "could not determine".
    """
    rc, _, _ = _git(repo, "cat-file", "-e", f"{sha}^{{commit}}")
    if rc != 0:
        return None
    rc, out, _ = _git(repo, "rev-list", "--max-count=1", sha, "--not", "main")
    if rc != 0:
        return None
    return out.strip() == ""


def read_ledger(root: Path) -> tuple[dict, set]:
    """(id -> latest terminal event, ids referenced by a `merged` event).

    Tolerates a torn final line: a crash mid-append can leave one, and aborting
    the whole read because of it would stop retention entirely.
    """
    terminal: dict[str, dict] = {}
    merged: set[str] = set()
    path = root / "ledger.jsonl"
    if not path.exists():
        return terminal, merged
    # A zero-byte ledger EXISTS but is torn — treat it as unreadable, never as
    # "no events". The difference is destructive here: no merged events means
    # every receipt looks unreferenced. Same defect fixed in integration.py (B3);
    # this is the sibling that fix did not reach, and this one runs on a timer.
    if path.stat().st_size == 0:
        raise LedgerTornError(
            f"{path} exists but is zero bytes — refusing to act on an empty read"
        )

    for raw_line in re.split(rb"\r\n?|\n", path.read_bytes()):
        line = raw_line.decode("utf-8", errors="replace")
        line = line.strip()
        if not line:
            continue
        # torn tail, a line another writer is mid-way through, or a valid-JSON
        # NON-OBJECT (null/123/"s"/[]/true) — the last has no `.get` and used to
        # raise AttributeError here, killing retention outright. See ledger_read.
        #
        # Every event on the line, not just the first: a newline-less ad-hoc
        # append puts a real terminal event BEHIND another one on the same
        # physical line (measured 2026-08-10). Missing it here is destructive in
        # this direction specifically — an unseen `merged` makes retention treat
        # a finished item as live work and keep its artifacts forever, and an
        # unseen terminal event makes its receipt look unreferenced.
        for ev in parse_events(line)[0]:
            ident, event = ev.get("id"), ev.get("event")
            if not ident:
                continue
            if event in TERMINAL_EVENTS:
                terminal[ident] = ev
            if event == "merged":
                merged.add(ident)
    return terminal, merged


class Janitor:
    def __init__(self, root: Path, apply: bool, repo: Path | None = None) -> None:
        self.root, self.apply = root, apply
        # The salvage-ref sweep is the only duty here that touches a git
        # working tree rather than the queue; OPT-IN via `repo` so every
        # existing queue-only caller (tests included) is unaffected. Same
        # root/repo split GateLoop already uses for the same reason.
        self.repo = repo
        self.actions: list[str] = []
        self.alerts: list[str] = []

    def _rm(self, path: Path, why: str) -> None:
        self.actions.append(f"delete {path.relative_to(self.root)}  ({why})")
        if self.apply:
            shutil.rmtree(path) if path.is_dir() else path.unlink(missing_ok=True)

    def _alert(self, msg: str) -> None:
        self.alerts.append(msg)
        if self.apply:
            with open(self.root / "ALERTS.md", "a", encoding="utf-8") as fh:
                fh.write(f"- {_now().strftime('%Y-%m-%dT%H:%M:%SZ')} janitor: {msg}\n")

    def _alert_dedup(self, key: str, msg: str) -> None:
        """Like ``_alert``, but for anything that can recur on THIS sweep's
        own cadence — currently only the unreadable-claim-marker case (R2F5)
        — via ``claim.alert_once``'s persistent, cross-process, exactly-keyed
        dedup (R3F1/R3F2, 2026-08-08 review).

        **Why not just ``self._alert``.** This sweep runs via
        ``com.threeloops.claim-reaper.plist`` at ``StartInterval=300``:
        launchd execs a FRESH Python process every 5 minutes, so anything
        held only on ``self`` (an instance attribute) is empty on every
        single invocation and can never remember "already alerted" —
        exactly the class of bug ``claim.py``'s own ``alert_once`` was built
        to close for its OWN callers. Using a second, ad-hoc dedup here
        instead of that same function would be the fourth instance of this
        codebase's recurring shape (R2F3 was ``release()`` left off the
        shared marker reader; this would have been the fifth if it had
        shipped as its own thing) — so this calls the SAME function
        ``claim.py`` uses, not a sibling copy of it.
        """
        self.alerts.append(msg)
        if self.apply:
            claim.alert_once(self.root, key, msg, source="janitor")

    def _append_ledger(self, event: dict) -> None:
        """Append through the shared, locked transport, per CONTRACT.md §5.

        Recorded in ``self.actions`` even in report mode so ``--claims-only``
        (without ``--apply``) shows what WOULD be logged, matching every other
        sweep in this file. Only ``--apply`` actually writes bytes.
        """
        self.actions.append(
            f"ledger += {event['event']} {event.get('id', '')[:19]} "
            f"{json.dumps(event.get('detail', {}))[:160]}"
        )
        if not self.apply:
            return
        append_event(self.root, event)

    def _parked_ids(self) -> set[str]:
        d = self.root / "parked"
        return {p.stem.replace("_", ":", 1) for p in d.glob("*.json")} if d.is_dir() else set()

    def _sweep_claims(self, now: datetime) -> None:
        """Reap expired / stale-unparseable claim markers.

        Every reap appends a `claim_expired` ledger event naming the id, the
        dead actor (when the body parsed enough to have one), and the age
        past expiry — a silent unlink is how "no claim has ever been reaped"
        became unfalsifiable (the whole reason this proposal exists: the
        janitor had `runs = 0` and TTLs were decorative).

        NEVER treat the claims dir's absence as "no orphans" if it exists but
        cannot be enumerated: `d.is_dir()` false because there is genuinely no
        `claims/` directory yet is fine (nothing has ever claimed anything);
        an `OSError` while iterating an EXISTING directory (permissions, a
        vanished mount) is an instrument error and must not be swallowed into
        silence — it propagates so the run is visibly refused rather than
        quietly doing nothing.
        """
        d = self.root / "claims"
        if not d.is_dir():
            return
        for marker in d.glob("*.claim"):
            self._reap_one_claim(marker, now)

    def _reap_one_claim(self, marker: Path, now: datetime) -> None:
        """Reap ``marker`` if — and only if — it is STILL dead at the moment
        we act, not just when we first looked.

        **B1 (2026-08-08 review, TOC/TOU).** The original version read
        ``age_s`` from a single ``stat()`` taken before the body was even
        read, then used that possibly-stale age to decide whether to unlink.
        A worker re-creating this exact marker (``O_CREAT|O_EXCL`` leaves the
        file briefly EMPTY before the body lands — the same window the
        10-minute grace exists to protect) can land in the gap between that
        early stat and the eventual unlink: the reaper judges the NEW,
        live, just-created claim against the OLD marker's age and deletes it.

        The fix generalises CONTRACT.md §6's own steal-safety pattern
        ("stat, decide, stat again, abort if it changed") to every reap, not
        only an expired-claim steal: take a fresh stat immediately before
        the unlink and compare it to the one the decision was based on. If
        anything changed — inode, mtime, or size — someone touched this
        marker between our read and our act, and we do not delete it.

        **R2F4 (2026-08-08 review, major) — say this precisely, not more
        than it is true.** The double-stat NARROWS the race; it does not
        ELIMINATE it. A gap remains between the second stat (``st2``) and
        the actual ``unlink()`` inside ``self._rm()`` — on POSIX there is no
        atomic "unlink only if unchanged since I last looked" primitive, so
        this gap cannot be fully closed with the tools available here. A
        worker whose recreate lands in THAT narrower window can still lose
        its marker. Calling this "eliminated" would be worse than leaving it
        undocumented, because the next reader would trust a guarantee that
        does not hold. Treat this as a narrowed, disclosed, still-open gap.
        """
        try:
            st1 = marker.stat()
        except OSError:
            return  # vanished already — nothing to reap

        try:
            body, expires = claim.read_claim_marker(marker)
        except claim.MarkerUnreadable as exc:
            # R2F5 (2026-08-08 review, major): the original silently
            # `return`ed here. Combined with the R2F1 remedy in
            # count_live_claims (an unreadable marker now counts as LIVE
            # rather than raising), a marker that stays permission-denied
            # would consume a WIP slot FOREVER, completely unobserved — the
            # DoS fix and this leak fix have to land together, or the DoS
            # fix just trades a loud failure for a silent one. `CONTRACT.md`
            # already has a vocabulary entry for exactly this: `instrument_error`
            # — "tooling/host failed; says nothing about the code."
            ident = marker.stem.replace("_", ":", 1)
            self._append_ledger({
                "ts": _iso(now), "role": "implementer", "event": "instrument_error",
                "id": ident, "actor": "janitor.py",
                "detail": {"marker": marker.name, "reason": f"unreadable, not reaped: {exc}"},
            })
            self._alert_dedup(
                marker.name,
                f"claim marker {ident[:19]} is unreadable — NOT reaped (it counts as "
                "LIVE, not free headroom, per R2F1) and will hold its WIP slot "
                "indefinitely until a human fixes its permissions"
            )
            return

        age_s = time.time() - st1.st_mtime
        ident = marker.stem.replace("_", ":", 1)
        reason: str | None = None
        detail: dict = {}
        if body is None or expires is None:
            if age_s > CLAIM_GRACE_SECONDS:
                reason = f"unparseable and {int(age_s)}s old (past grace)"
                detail = {"dead_actor": None, "age_s": int(age_s),
                           "reason": "unparseable past 10-minute grace"}
        elif now > expires:
            reason = "claim expired"
            detail = {"dead_actor": body.get("actor"),
                       "age_past_expiry_s": int((now - expires).total_seconds()),
                       "reason": "claim expired"}

        if reason is None:
            return

        # Re-verify against a FRESH stat, immediately before acting. If the
        # file changed since st1, abort — do not reap a marker mid-flux.
        try:
            st2 = marker.stat()
        except OSError:
            return  # vanished between our decision and the re-check
        if (st1.st_ino, st1.st_mtime, st1.st_size) != (st2.st_ino, st2.st_mtime, st2.st_size):
            return

        self._rm(marker, reason)
        self._append_ledger({
            "ts": _iso(now), "role": "implementer", "event": "claim_expired",
            "id": ident, "actor": "janitor.py", "detail": detail,
        })

    def _sweep_salvage_refs(self, now: datetime) -> None:
        """Delete `salvage/gl-contentfree-*` branches older than
        SALVAGE_REF_MAX_AGE_DAYS whose tip is reachable from main.

        Grok follow-up on #410: `_content_free_reconcile` mints one of these
        on every content-free heal to preserve the old tip before moving
        main, and nothing else in the repository ever deletes them — left
        alone they accumulate forever.

        No-op if this Janitor has no `repo` (opt-in, see __init__). For each
        candidate branch: parse its embedded UTC stamp (never git ref/commit
        metadata — the name IS the mint time, by construction, and this
        avoids a `log`/`show` call per branch); anything that does not match
        the exact naming shape or fails to parse is left alone, never
        guessed at. Reachability is checked EXPLICITLY via
        `_is_ancestor_of_main` before ever calling `git branch -d` — `-d`'s
        own built-in merge check runs against current HEAD, not
        specifically `main`, and this repo's janitor is not guaranteed to
        run with `main` checked out. Deletion is `git branch -d` ONLY: a
        refusal (from `-d` itself, e.g. a concurrent write raced the
        reachability check) is kept and logged, never retried with `-D`,
        never forced.
        """
        if self.repo is None:
            return
        rc, out, _ = _git(self.repo, "for-each-ref", "--format=%(refname:short)",
                          f"refs/heads/{SALVAGE_REF_PREFIX}*")
        if rc != 0:
            return  # unreadable repo: an instrument condition, not a queue one;
                     # nothing here to delete or alert on for a repo that
                     # cannot even be listed
        for branch in out.splitlines():
            branch = branch.strip()
            if not branch:
                continue
            match = _SALVAGE_REF_RE.match(branch)
            if not match:
                continue  # not this sweep's naming shape; never guess
            minted = _parse_salvage_stamp(match.group("stamp"))
            if minted is None:
                continue
            age = now - minted
            if age <= timedelta(days=SALVAGE_REF_MAX_AGE_DAYS):
                continue  # too young, keep regardless of reachability
            rc, sha, _ = _git(self.repo, "rev-parse", "--verify", branch)
            if rc != 0:
                continue  # vanished or unreadable between listing and here
            if _is_ancestor_of_main(self.repo, sha) is not True:
                # False (a real divergence) and None (could not determine)
                # both keep the branch — never delete on anything but a
                # proven "yes, reachable from main".
                continue
            self._delete_salvage_branch(branch, age)

    def _delete_salvage_branch(self, branch: str, age: timedelta) -> None:
        self.actions.append(
            f"delete branch {branch}  ({age.days}d old, tip reachable from main)")
        if not self.apply:
            return
        rc, _, err = _git(self.repo, "branch", "-d", branch)
        if rc != 0:
            self._alert(
                f"salvage ref sweep: `git branch -d {branch}` refused "
                f"({err[:160] or 'no error text'}) — kept, never forcing with -D")

    def sweep(self, claims_only: bool = False) -> None:
        if claims_only:
            self._sweep_claims(_now())
            return
        now = _now()
        terminal, merged = read_ledger(self.root)
        parked = self._parked_ids()

        # --- artifacts with a terminal event ---------------------------------
        for dirname in ARTIFACT_DIRS:
            d = self.root / dirname
            if not d.is_dir():
                continue
            for art in d.glob("*.json"):
                ident = art.stem.replace("_", ":", 1)
                if ident in parked:
                    # THE most important branch here. Exempt while parked.
                    age = now - datetime.fromtimestamp(art.stat().st_mtime, UTC)
                    if age > timedelta(days=PARKED_MAX_DAYS):
                        self._alert(f"parked {ident[:19]} is {age.days}d old — still awaiting a human")
                    continue
                ev = terminal.get(ident)
                if not ev:
                    continue
                when = _parse(ev.get("ts"))
                if when and (now - when) > timedelta(days=TERMINAL_RETENTION_DAYS):
                    self._rm(art, f"{ev['event']} {(now - when).days}d ago")

        # --- expired rejections ---------------------------------------------
        d = self.root / "rejected"
        if d.is_dir():
            for rej in d.glob("*.json"):
                try:
                    body = json.loads(rej.read_text())
                except (OSError, json.JSONDecodeError):
                    continue
                exp = _parse(body.get("expires_at"))
                if exp and (now - exp) > timedelta(days=REJECTED_RETENTION_DAYS):
                    self._rm(rej, f"expired {(now - exp).days}d ago")

        # --- claim markers ---------------------------------------------------
        self._sweep_claims(now)

        # --- receipts ---------------------------------------------------------
        d = self.root / "receipts"
        if d.is_dir():
            for rec in d.iterdir():
                if not rec.is_dir():
                    continue
                ident = rec.name.replace("_", ":", 1)
                age = now - datetime.fromtimestamp(rec.stat().st_mtime, UTC)
                if age > timedelta(days=RECEIPT_RETENTION_DAYS) and ident not in merged:
                    self._rm(rec, f"{age.days}d old, no merged event")

        # --- a park marker that vanished without an approval -------------------
        # Absence of a marker must NEVER read as "a human approved this".
        parked_seen = {p.stem for p in (self.root / "parked").glob("*.json")} if (self.root / "parked").is_dir() else set()
        unparked = _unparked_ids(self.root)
        for line in _parked_events(self.root):
            ident = line.get("id")
            stem = (ident or "").replace(":", "_", 1)
            if ident and stem not in parked_seen and ident not in unparked:
                self._alert(
                    f"park marker for {ident[:19]} vanished with no authenticated unparked event "
                    "— NOT released; investigate"
                )

        # --- stale inquiries ----------------------------------------------------
        d = self.root / "inquiries"
        if d.is_dir():
            for art in d.glob("*.json"):
                ident = art.stem.replace("_", ":", 1)
                if ident in terminal:
                    continue
                age = now - datetime.fromtimestamp(art.stat().st_mtime, UTC)
                if age > timedelta(days=STALE_INQUIRY_DAYS):
                    self._alert(f"inquiry {ident[:19]} unterminated after {age.days}d — a bug in planning, not a backlog")

        # --- salvage refs (opt-in: only when constructed with a repo) --------
        self._sweep_salvage_refs(now)

        # --- ledger roll ---------------------------------------------------------
        led = self.root / "ledger.jsonl"
        if led.exists() and led.stat().st_size > LEDGER_ROLL_BYTES:
            self.actions.append(
                "rollover REFUSED: archive replay and collision-safe naming are not "
                "implemented; ledger.jsonl remains in place"
            )
            if self.apply:
                self._alert(
                    "ledger.jsonl exceeds 100 MB, but unsafe rollover is disabled until "
                    "archive replay and collision-safe naming share the ledger lock"
                )


def _unparked_ids(root: Path) -> set[str]:
    """Ids released by an `unparked` event. Sibling of `_parked_events`.

    Was an inline set-comprehension in `sweep` guarded by a `_safe_id(line)`
    predicate, which decoded the line to prove it was safe and then threw that
    decode away so a bare `json.loads(line)["id"]` could redo it. That structure
    could not survive the multi-object line: the guard would say "safe" (there
    IS an object with an id) and `json.loads` would then raise `Extra data` and
    take the whole sweep down. One decode, one place.

    It checks `event == "unparked"` rather than trusting the substring
    prefilter, which is BOTH the shape `_parked_events` already uses two
    functions down AND the safe direction: a false "unparked" SILENCES the
    "park marker vanished with no authenticated unparked event" alert, and this
    section's own rule is that absence of a marker must never read as approval.
    """
    led = root / "ledger.jsonl"
    if not led.exists():
        return set()
    out: set[str] = set()
    for raw_line in re.split(rb"\r\n?|\n", led.read_bytes()):
        line = raw_line.decode(errors="replace")
        if '"unparked"' not in line:
            continue
        for ev in parse_events(line)[0]:
            if ev.get("event") == "unparked" and ev.get("id"):
                out.add(ev["id"])
    return out


def _parked_events(root: Path) -> list[dict]:
    led = root / "ledger.jsonl"
    if not led.exists():
        return []
    out = []
    for raw_line in re.split(rb"\r\n?|\n", led.read_bytes()):
        line = raw_line.decode(errors="replace")
        if '"parked"' not in line:
            continue
        # The substring prefilter above does NOT imply an object: the bare JSON
        # string `"parked"` and the array `["parked"]` both match it and both
        # lack `.get`. A generic non-object line is filtered out before it ever
        # gets here, which is precisely why this carrier looked safe.
        for ev in parse_events(line)[0]:
            if ev.get("event") == "parked":
                out.append(ev)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--loops-root", required=True, type=Path)
    ap.add_argument("--apply", action="store_true", help="actually delete (default: report only)")
    ap.add_argument("--claims-only", action="store_true",
                     help="skip every sweep except claim markers (the 300s-cadence mode)")
    ap.add_argument("--repo", type=Path, default=None,
                     help="git working tree to sweep salvage/gl-contentfree-* refs in "
                          "(opt-in; the sweep is a no-op without this)")
    args = ap.parse_args()

    if not args.loops_root.is_dir():
        print(f"no such queue: {args.loops_root}")
        return 2

    j = Janitor(args.loops_root, args.apply, repo=args.repo)
    try:
        j.sweep(claims_only=args.claims_only)
    except LedgerTornError as exc:
        # Refuse the whole sweep, alert once, exit 2 (could-not-run: a torn
        # ledger means the janitor cannot safely evaluate anything — fix the
        # ledger, then re-run; Ruling #4, 2026-08-09).
        print(f"REFUSED: {exc}", file=sys.stderr)
        if args.apply:
            with open(args.loops_root / "ALERTS.md", "a", encoding="utf-8") as fh:
                fh.write(f"- janitor REFUSED to sweep: {exc}\n")
        return 2

    mode = "APPLIED" if args.apply else "would do (use --apply)"
    print(json.dumps({"mode": mode, "actions": len(j.actions), "alerts": len(j.alerts)}))
    for a in j.actions:
        print(f"  {a}")
    for a in j.alerts:
        print(f"  ALERT: {a}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
