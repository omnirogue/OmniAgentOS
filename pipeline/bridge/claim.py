#!/usr/bin/env python3
"""The sanctioned writer for ``acquire`` / ``release`` / ``wip`` on
``var/loopqueue/claims/*.claim``.

**Precise claim (2026-08-08, round-3 ruling — narrowed from an earlier,
broader "the ONLY sanctioned writer" phrasing that overstated coverage):**
this module is the sanctioned path for CREATING a claim, RELEASING one you
hold, and READING the live count. It is **not** a complete implementation
of CONTRACT.md §6 — see NOT PROVIDED below — and does not claim to be.

CONTRACT.md §6: "Implementers must claim a proposal before building it,"
and ownership is arbitrated by a successful ``O_EXCL`` create — never by a
ledger line, never by hand-writing a marker. Before this file, claim creation
was prompt-prose (``PROMPT-implementer-loop.md:208-214``): no script wrapped
it, so any session could hand-write a marker in any shape, and 7 live claims
were observed all under one actor with zero TTL enforcement and one marker
that outlived its own ``released`` ledger event.

This is that missing writer. It owns:

  acquire   O_CREAT|O_EXCL create, cap-checked against ``state/budget.json``'s
            ``wip_cap``, TTL'd body, `claimed` ledger event.
  release   verify you still hold it, delete the marker, `released` event.
  wip       report live-claim count / cap, for a caller that wants to know
            before trying (governor-style read, no side effects).

NOT PROVIDED — no sanctioned path exists today: CONTRACT.md §6 also
describes a **renew** protocol (re-verify `actor` is you, write
`<id>.claim.tmp`, `rename()` over the live marker) and a **steal** protocol
(stat/compare/steal an expired marker with the concurrency-safe double-stat
dance). Neither verb is implemented here. A long enumeration that
legitimately needs to extend its own TTL, or a loop that needs to recover a
crashed peer's abandoned claim, has no sanctioned path through this module
and must fall back to the hand-rolled protocol CONTRACT.md warns against.

This is a real gap, ruled explicitly out of THIS build's scope by the
coordinator (2026-08-08, round 3): "change the claim, not the scope" —
i.e. narrow what this docstring asserts rather than expand the
implementation unreviewed. The follow-on (renew/steal) is filed separately
by the coordinator, not silently absorbed here.

FAIL-CLOSED, the theme running through this whole file: an unreadable
``claims/`` directory (permissions, a vanished mount) is never counted as zero
live claims. Zero claims reads as free headroom — exactly the wrong direction
for a cap that exists to apply backpressure. It is reported as an
``instrument_error`` and the caller is refused, never waved through.

Usage:
    claim.py acquire <id> --actor <actor> --ttl-seconds 3600 \\
        --loops-root var/loopqueue
    claim.py release <id> --actor <actor> --loops-root var/loopqueue
    claim.py wip --loops-root var/loopqueue

Exit codes (mirrors the estate convention in bridge/file_proposal.py):
    0   done (acquire: claimed; release: released; wip: printed)
    1   REFUSED, fixable — e.g. at cap: release something, wait for an
        expiry, or run the reaper, then retry
    2   marker already held by someone else (O_EXCL lost the race) or you no
        longer own the marker you tried to release — do not retry blindly
    3   instrument error — could not tell whether the cap is clear. Never
        read as "0 live claims, go ahead."
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
# THE shared numeric-limit guard (governor.py imports nothing from this
# package, so this cannot be circular). A local copy is the clone-family
# defect this whole change is about.
import governor as _governor
import notify as _notify  # push transport for alerts (fail-soft; see notify.py)
from ledger_write import append_event

try:
    from bridge.gate_loop import TICK_SECONDS as _LANDER_TICK_SECONDS
except ModuleNotFoundError:  # standalone ``python bridge/claim.py`` invocation
    from gate_loop import TICK_SECONDS as _LANDER_TICK_SECONDS

EXIT_OK = 0
EXIT_AT_CAP = 1
EXIT_LOST_RACE = 2
EXIT_INSTRUMENT_ERROR = 3

DEFAULT_WIP_CAP = 4
CLAIM_GRACE_SECONDS = 600  # 10 min — CONTRACT.md §6, the empty-marker rule.
DEFAULT_TTL_SECONDS = 3600
_LANDER_HEARTBEAT_STALE_S = 2 * _LANDER_TICK_SECONDS
_LANDER_OVERRIDE_ENV = "THREELOOPS_CLAIM_LANDER_OVERRIDE"
_LANDER_OVERRIDE_REASON_ENV = "THREELOOPS_CLAIM_LANDER_OVERRIDE_REASON"


class ClaimError(RuntimeError):
    """Carries the exit code a CLI caller should use — see module docstring."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


class MarkerUnreadable(RuntimeError):
    """A claim marker file EXISTS but its CONTENT could not be read for an
    OS-level reason (EACCES, EIO, a vanished mount under an open fd, ...) —
    as opposed to being genuinely empty or corrupt JSON, which is the
    mid-creation case CONTRACT.md §6 protects.

    Conflating the two — a bare ``except OSError`` around ``read_text()``
    that folds "permission denied" into "unparseable" — is the exact defect
    a cross-lineage review caught here (2026-08-08, B3): an unreadable but
    LIVE claim silently ages out of the live count once its mtime is more
    than 10 minutes old, handing a caller a WIP slot that nothing actually
    vacated. Every caller of ``read_claim_marker`` must treat this exception
    as "cannot tell, so this counts as held" or "refuse", never as "empty"."""


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


def _marker_path(loops_root: Path, ident: str) -> Path:
    return loops_root / "claims" / f"{ident.replace(':', '_', 1)}.claim"


def _stem_to_id(stem: str) -> str:
    return stem.replace("_", ":", 1)


def _kind_from_disk(loops_root: Path, ident: str) -> str | None:
    filename = f"{ident.replace(':', '_', 1)}.json"
    for dirname, kind in (("proposals", "proposal"),
                          ("findings", "finding"),
                          ("inquiries", "inquiry")):
        if (loops_root / dirname / filename).is_file():
            return kind
    return None


def read_claim_marker(marker: Path) -> tuple[dict | None, datetime | None]:
    """Parse one claim marker.

    Returns ``(body, expires_at)``. Both are ``None`` for a genuinely
    empty/corrupt marker — the mid-creation case (O_EXCL creates the file
    empty; the body lands a moment later). A marker that vanished between
    being listed and being read (``FileNotFoundError``) is ALSO reported as
    ``(None, None)``: there is nothing left on disk to be wrong about.

    Raises ``MarkerUnreadable`` — never folded into the ``(None, None)``
    case — if the file exists but ``read_text()`` failed for any OTHER
    OS-level reason (permissions, I/O). This is the single place both
    ``bridge/claim.py`` and ``bridge/janitor.py`` classify a marker, so the
    unreadable-vs-unparseable distinction only has to be gotten right once.
    """
    try:
        text = marker.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        raise MarkerUnreadable(f"{marker}: {type(exc).__name__}: {exc}") from exc
    try:
        body = json.loads(text)
    except json.JSONDecodeError:
        return None, None
    if not isinstance(body, dict):
        return None, None
    return body, _parse(body.get("expires_at"))


# --------------------------------------------------------------------------
# reads — both fail closed, never a favourable absence
# --------------------------------------------------------------------------


def read_wip_cap(loops_root: Path) -> int:
    """``wip_cap`` from ``state/budget.json``. Absence defaults (the governor's
    own convention, ``bridge/governor.py``: ``budget.setdefault("wip_cap", 4)``);
    a budget.json that EXISTS but will not parse is an instrument error — a
    corrupt config file must never be silently treated as "use the default"."""
    path = loops_root / "state" / "budget.json"
    if not path.exists():
        return DEFAULT_WIP_CAP
    try:
        budget = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ClaimError(
            EXIT_INSTRUMENT_ERROR,
            f"state/budget.json exists but is unreadable/corrupt ({type(exc).__name__}: {exc}) "
            "— refusing to guess wip_cap. REMEDY: repair the JSON, or DELETE the file — "
            "the governor refuses to rewrite a budget it could not read (it will not "
            "overwrite operator policy it cannot see), but it rebuilds a default file "
            "cleanly when the path is ABSENT.",
        ) from exc
    if not isinstance(budget, dict):
        raise ClaimError(
            EXIT_INSTRUMENT_ERROR,
            "state/budget.json is not a JSON object — refusing to guess wip_cap.",
        )
    # `budget.get("wip_cap") or DEFAULT` cannot tell an ABSENT key from an
    # operator's explicit 0, and 0 is the documented full stop. This module is
    # the one that MINTS claims, so collapsing it here did not merely report a
    # wrong number — it granted DEFAULT_WIP_CAP slots against a halt that
    # publish_queue.read_wip_cap (F6) and integration.read_governor (F7) were
    # both already honouring. Third carrier of that family; pinned by
    # tests/test_falsy_zero_governor_knobs.py.
    raw_cap = budget.get("wip_cap")
    if raw_cap is None:
        return DEFAULT_WIP_CAP
    # Present but unusable (`""`, `"abc"`, a list, NaN, inf) is an INSTRUMENT
    # ERROR, the same class this function already raises for a corrupt
    # budget.json — not a crash, and emphatically not the default. The old `or`
    # idiom coerced every one of these to DEFAULT_WIP_CAP, i.e. a malformed cap
    # read as four free slots: an abnormal condition rendering as a favourable
    # value.
    #
    # finite_limit rather than a bare int(): `json.loads` turns `1e400` into
    # float('inf') with no error, and `int(inf)` raises OverflowError, which is
    # NOT a ValueError subclass and escaped an earlier draft's except clause as
    # an unhandled traceback out of the claim minter. Caught by the xai and
    # google seats between them.
    try:
        return int(_governor.finite_limit(raw_cap, what="wip_cap"))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ClaimError(
            EXIT_INSTRUMENT_ERROR,
            f"state/budget.json has wip_cap={raw_cap!r}, which is not an integer "
            f"({type(exc).__name__}) — refusing to guess. REMEDY: fix the value; "
            "the governor (bridge/governor.py) will rewrite a good one within one tick.",
        ) from exc


def _require_fresh_lander_heartbeat(loops_root: Path) -> None:
    path = loops_root / "state" / "landers.json"
    try:
        heartbeat = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ClaimError(
            EXIT_INSTRUMENT_ERROR,
            f"state/landers.json is missing, unreadable, or corrupt "
            f"({type(exc).__name__}: {exc}) — refusing to acquire a proposal claim "
            "that may never drain. REMEDY: restore the gate-loop daemon and wait "
            "for a successful tick.",
        ) from exc
    if not isinstance(heartbeat, dict):
        raise ClaimError(
            EXIT_INSTRUMENT_ERROR,
            "state/landers.json is not a JSON object — refusing to acquire a proposal claim.",
        )
    if heartbeat.get("status") != "ok":
        raise ClaimError(
            EXIT_INSTRUMENT_ERROR,
            f"state/landers.json status is {heartbeat.get('status')!r}, not 'ok' — "
            "refusing to acquire a proposal claim until a successful tick.",
        )
    last_tick = _parse(heartbeat.get("last_tick_ts"))
    if last_tick is None or last_tick.tzinfo is None:
        raise ClaimError(
            EXIT_INSTRUMENT_ERROR,
            "state/landers.json has no parseable last_tick_ts — refusing to acquire "
            "a proposal claim.",
        )
    age_s = (_now() - last_tick).total_seconds()
    if not 0 <= age_s <= _LANDER_HEARTBEAT_STALE_S:
        raise ClaimError(
            EXIT_INSTRUMENT_ERROR,
            f"state/landers.json is stale or future-dated ({age_s:.1f}s age; accepted "
            f"range 0..{_LANDER_HEARTBEAT_STALE_S}s) — refusing to acquire a proposal "
            "claim. REMEDY: restore the gate-loop daemon and wait for a successful tick.",
        )


def count_live_claims(loops_root: Path) -> int:
    """Live = holds the cap: unexpired well-formed markers, plus unparseable
    ones still inside the 10-minute creation grace (they may yet become a
    real claim; treating them as free headroom would let a second acquirer
    in on top of a claim that is mid-write) — **plus any marker whose
    content could not even be read (R2F1, see below).**

    An EXISTING ``claims/`` directory that cannot be enumerated at all
    (permissions, a vanished mount) is still an ``instrument_error`` —
    nothing here can even be listed, so there is genuinely nothing to
    count. A directory that does not exist yet genuinely has zero claims;
    that is a real answer, not an unknown one, so it is not an error.

    **A single unreadable MARKER is different (R2F1, 2026-08-08 review,
    BLOCKER).** The first repair round raised ``ClaimError`` here too,
    which meant one permission-denied file ANYWHERE in ``claims/`` refused
    every subsequent ``acquire()`` for every UNRELATED id — a global denial
    of service traded for a per-file bug. ``wip_cap`` is documented (M4) as
    ADVISORY backpressure, not a hard admission-control primitive, so the
    proportionate response is: count the unreadable marker as LIVE (never
    grant capacity nothing proves is free — the property that actually
    matters) and alert once, rather than refuse the whole directory over
    one file. Fail-closed means "don't grant what you can't verify," not
    "stop the world for everyone."
    """
    d = loops_root / "claims"
    if not d.exists():
        return 0
    try:
        entries = list(d.iterdir())
    except OSError as exc:
        raise ClaimError(
            EXIT_INSTRUMENT_ERROR,
            f"claims/ exists but is unreadable ({type(exc).__name__}: {exc}) — "
            "refusing to compute this as 0 live claims / free headroom. "
            "REMEDY: fix the permissions on claims/ (or the mount backing it) and retry.",
        ) from exc

    now = _now()
    live = 0
    for marker in entries:
        if not marker.is_file() or marker.suffix != ".claim":
            continue
        try:
            st = marker.stat()
        except OSError:
            continue  # vanished mid-scan (e.g. reaped concurrently) — not live
        age_s = time.time() - st.st_mtime
        try:
            body, expires = read_claim_marker(marker)
        except MarkerUnreadable as exc:
            # R2F1: count as LIVE, alert once, keep going — do NOT raise and
            # take down every unrelated acquire() over one bad file.
            alert_once(
                loops_root, marker.name,
                f"{marker.name} exists but its content is unreadable ({exc}) — "
                "counted as LIVE (occupies a WIP slot) rather than refused, so "
                "this does not block acquires for other ids. It will stay "
                "counted until a human fixes its permissions; janitor.py's "
                "reaper deliberately will not touch it either (see R2F5).",
            )
            live += 1
            continue
        if body is None or expires is None:
            if age_s <= CLAIM_GRACE_SECONDS:
                live += 1  # claim mid-creation — counts against the cap
            continue
        if now <= expires:
            live += 1
    return live


# --------------------------------------------------------------------------
# the ledger — one os.write(2) to an O_APPEND fd, per CONTRACT.md §5
# --------------------------------------------------------------------------


def _append_ledger(loops_root: Path, event: dict) -> None:
    append_event(loops_root, event)


def _alert_state_path(loops_root: Path) -> Path:
    return loops_root / "state" / "alerted.json"


def alert_once(loops_root: Path, key: str, msg: str, *, source: str = "claim.py") -> None:
    """Append one line to ``ALERTS.md`` — but only the FIRST time ever, for
    a given ``key`` — matching CONTRACT.md §9's "one alert per item, ever"
    doctrine ("a loop that alerts repeatedly trains its operator to ignore
    it"). This is the ONE shared implementation both ``bridge/claim.py`` and
    ``bridge/janitor.py`` call; there is no sibling copy left to miss.

    **Persistent, not in-process (R3F1, 2026-08-08 review, BLOCKER).** An
    earlier version tracked "already alerted" only via an in-memory check
    against ``ALERTS.md``'s CURRENT content within one call. That is not
    enough for ``bridge/janitor.py``: the plist re-execs a FRESH Python
    process every 300 seconds, so anything held only in the interpreter (an
    instance attribute, a module-level set) is empty on every single
    invocation and can never remember "already alerted" across the process
    boundary — a permanently unreadable marker would write a new
    ``ALERTS.md`` line every 5 minutes, forever (288/day). Dedup state is
    therefore written to disk, in ``state/alerted.json`` — small, separate
    from ``ALERTS.md`` itself, so a lookup never has to re-read the
    (potentially large, human-curated) alerts log ("don't read the whole
    alerts file per call").

    **Exact key match, not substring (R3F2, 2026-08-08 review, major).** The
    prior check was ``key in existing_text`` against ``ALERTS.md``'s raw
    content — a naive substring match that is wrong in BOTH directions: a
    marker named ``sha256_test`` is silently swallowed if
    ``sha256_test_1`` was already alerted (false negative — content that
    should generate a NEW alert doesn't), and a marker whose own name
    happens to appear as a substring of unrelated prose elsewhere in the
    file could equally be suppressed by accident. ``key`` is now checked
    for exact membership in a JSON set, never containment.
    """
    state_path = _alert_state_path(loops_root)
    state_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        alerted = set(json.loads(state_path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        alerted = set()
    except (OSError, json.JSONDecodeError):
        # A corrupt dedup file must fail toward "alert again", never toward
        # "alert silently swallowed forever" — the opposite of the bug this
        # function exists to fix. Losing dedup for one cycle is recoverable;
        # a permanently muted alert is not.
        alerted = set()

    if key in alerted:
        return

    # Write the human-readable line FIRST, then persist the dedup state. If
    # the process dies between the two, the next invocation alerts again —
    # one extra line is recoverable; a dropped, unrecorded alert is not.
    with open(loops_root / "ALERTS.md", "a", encoding="utf-8") as fh:
        fh.write(f"- {_now().strftime('%Y-%m-%dT%H:%M:%SZ')} {source}: {msg}\n")

    alerted.add(key)
    tmp = state_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(sorted(alerted)), encoding="utf-8")
    tmp.replace(state_path)  # atomic rename — CONTRACT.md's "write .tmp, rename()" pattern

    # Push the SAME alert to the operator's phone if OMNI_NTFY_URL is set. This
    # sits AFTER the dedup gate above, so an alert that is written once is
    # pushed at most once — one alert = one ALERTS.md line + one push. The push
    # is fail-soft (unset URL or any network error is swallowed and, on
    # failure, noted back in ALERTS.md), so it can never raise into a loop that
    # is re-exec'd every cycle.
    _notify.push_alert(
        loops_root,
        title=f"ThreeLoops alert ({source})",
        body=msg,
        source=source,
        priority="high",
        tags="warning",
    )


# Backward-compatible alias: this function was named `_alert_once` (private,
# claim.py-only) before round 4 made it the ONE shared implementation both
# claim.py and janitor.py call — renamed to public because it is no longer
# private to this module. Kept as an alias so nothing that already spells
# the old name breaks.
_alert_once = alert_once


# --------------------------------------------------------------------------
# acquire / release
# --------------------------------------------------------------------------


def acquire(
    loops_root: Path,
    ident: str,
    actor: str,
    *,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    role: str = "implementer",
) -> Path:
    """Claim ``ident`` for ``actor``. Raises ``ClaimError`` on any refusal.

    Cap check happens BEFORE the O_EXCL create — CONTRACT.md's "successful
    O_EXCL is the only arbiter of ownership" governs which of two racing
    claimants wins a single slot, not whether a slot exists at all. wip_cap
    is backpressure, applied before the kernel is even asked.

    **wip_cap is ADVISORY under concurrency, not a kernel-enforced ceiling
    (M4, 2026-08-08 review).** ``count_live_claims`` then ``O_CREAT|O_EXCL``
    is check-then-act: two concurrent ``acquire()`` calls on two DISTINCT
    ids can both observe headroom and both create, overshooting ``wip_cap``.
    This is a deliberate, disclosed trade-off, not an oversight — a genuine
    ceiling needs a single serialising lock around the whole
    count-then-create window, which this module does not add, matching
    CONTRACT.md's own framing of ``wip_cap`` as "backpressure" rather than a
    hard admission-control primitive. See ``tests/test_claim_repair_round2.py::
    test_wip_cap_is_advisory_under_concurrency`` for a reproduction that pins
    this as EXPECTED behaviour rather than silently assumed away.
    """
    kind = _kind_from_disk(loops_root, ident)
    lander_override: dict[str, str] | None = None
    if kind in (None, "proposal"):
        if os.environ.get(_LANDER_OVERRIDE_ENV, "") == "1":
            reason = os.environ.get(_LANDER_OVERRIDE_REASON_ENV, "").strip()
            if not reason:
                raise ClaimError(
                    EXIT_INSTRUMENT_ERROR,
                    f"{_LANDER_OVERRIDE_ENV}=1 requires a non-empty "
                    f"{_LANDER_OVERRIDE_REASON_ENV} — refusing an unexplained "
                    "lander-heartbeat override.",
                )
            lander_override = {"env": _LANDER_OVERRIDE_ENV, "reason": reason}
        else:
            _require_fresh_lander_heartbeat(loops_root)

    cap = read_wip_cap(loops_root)
    live = count_live_claims(loops_root)
    if live >= cap:
        raise ClaimError(
            EXIT_AT_CAP,
            f"wip cap reached: {live}/{cap} live claims — refusing to acquire {ident}. "
            "REMEDY: release a claim you hold (`claim.py release <id> --actor <you>`), "
            "wait for one to expire, or run `janitor.py --claims-only --apply` to reap "
            "dead ones, then retry.",
        )

    claims_dir = loops_root / "claims"
    claims_dir.mkdir(parents=True, exist_ok=True)
    path = _marker_path(loops_root, ident)

    now = _now()
    expires = now + timedelta(seconds=ttl_seconds)
    body: dict[str, object] = {
        "actor": actor, "at": _iso(now), "expires_at": _iso(expires),
    }
    if lander_override is not None:
        # B2: the break-glass evidence is part of the same fsynced ownership
        # marker. A crash before the ledger append must not erase why admission
        # bypassed the heartbeat guard.
        body["lander_heartbeat_override"] = lander_override

    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError as exc:
        raise ClaimError(
            EXIT_LOST_RACE,
            f"{ident} is already claimed — O_EXCL lost the race (or a stale marker needs "
            "stealing per CONTRACT.md §6). Do not retry blindly; check the marker's owner.",
        ) from exc
    try:
        os.write(fd, json.dumps(body).encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)

    detail: dict[str, object] = {
        "expires_at": body["expires_at"], "ttl_seconds": ttl_seconds,
    }
    if lander_override is not None:
        detail["reason"] = lander_override["reason"]
        detail["lander_heartbeat_override"] = lander_override
    _append_ledger(loops_root, {
        "ts": _iso(now), "role": role, "event": "claimed", "id": ident, "actor": actor,
        "detail": detail,
    })
    return path


def release(loops_root: Path, ident: str, actor: str, *, role: str = "implementer") -> None:
    """Release ``ident``. Verifies ``actor`` still owns the marker before
    deleting it — the same safety CONTRACT.md §6 requires for renewal: acting
    on a stale read of a marker you no longer hold can delete a NEW owner's
    claim. Missing markers release cleanly (idempotent: a crash between
    unlink and the ledger append must not wedge the release path).

    **An empty/corrupt/unreadable marker is NEVER releasable — by anyone,
    including the actor who thinks they hold it.** B2 (2026-08-08 review):
    ``if body is not None and body.get("actor") != actor`` evaluates False
    when ``body is None``, so an empty marker skipped the ownership check
    entirely and ``unlink()`` proceeded — any actor could delete another
    worker's marker during ITS mid-creation window, exactly the window the
    10-minute grace exists to protect. Positive proof of ownership is
    required to delete; the absence of proof is refused, never treated as
    "no objection".

    **Uses the shared ``read_claim_marker`` parser (R2F3, 2026-08-08
    review, major).** The prior version kept its own inline
    ``except (OSError, json.JSONDecodeError): body = None`` instead of
    migrating to the helper B3 introduced — the "one shared classifier"
    claim was true for ``count_live_claims`` and ``janitor._reap_one_claim``
    but not for this, the THIRD caller. An unreadable (not merely
    empty/corrupt) marker now raises ``MarkerUnreadable`` here exactly as
    it does everywhere else, and is refused for the same reason: no
    positive proof of ownership, no deletion."""
    path = _marker_path(loops_root, ident)
    if not path.exists():
        # Already gone (reaped, or a prior release's unlink landed but the
        # ledger append did not). Append the event anyway so the history is
        # complete, but do not treat a missing marker as an error.
        _append_ledger(loops_root, {
            "ts": _iso(_now()), "role": role, "event": "released", "id": ident, "actor": actor,
            "detail": {"note": "marker already absent"},
        })
        return

    try:
        body, _expires = read_claim_marker(path)
    except MarkerUnreadable as exc:
        raise ClaimError(
            EXIT_LOST_RACE,
            f"{ident}'s marker exists but its content is unreadable ({exc}) — cannot "
            f"prove {actor!r} owns it. Refusing to delete. "
            "REMEDY: fix the marker's permissions and retry.",
        ) from exc

    if not isinstance(body, dict) or body.get("actor") != actor:
        held_by = body.get("actor") if isinstance(body, dict) else "<empty/corrupt marker>"
        raise ClaimError(
            EXIT_LOST_RACE,
            f"{ident} is not provably held by {actor!r} (marker shows {held_by!r}) — "
            "refusing to delete. An empty/corrupt marker may be another actor's claim "
            "mid-creation; deleting it without positive ownership proof is exactly the "
            "bug this refusal exists to prevent. Abort and drop the work.",
        )

    path.unlink(missing_ok=True)
    _append_ledger(loops_root, {
        "ts": _iso(_now()), "role": role, "event": "released", "id": ident, "actor": actor,
        "detail": {},
    })


def wip(loops_root: Path) -> dict:
    """Read-only: current live-claim count and cap. No side effects, so a
    caller can check headroom before attempting an acquire it expects to
    fail. Raises ``ClaimError`` (instrument_error) under the same fail-closed
    conditions as ``acquire``."""
    cap = read_wip_cap(loops_root)
    live = count_live_claims(loops_root)
    return {"live": live, "cap": cap, "headroom": max(cap - live, 0)}


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--loops-root", required=True, type=Path)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_acq = sub.add_parser("acquire", help="claim an item, cap-checked, TTL'd")
    p_acq.add_argument("id")
    p_acq.add_argument("--actor", required=True)
    p_acq.add_argument("--ttl-seconds", type=int, default=DEFAULT_TTL_SECONDS)
    p_acq.add_argument("--role", default="implementer")

    p_rel = sub.add_parser("release", help="release a claim you hold")
    p_rel.add_argument("id")
    p_rel.add_argument("--actor", required=True)
    p_rel.add_argument("--role", default="implementer")

    sub.add_parser("wip", help="report live-claim count / cap, read-only")

    args = ap.parse_args(argv)

    try:
        if args.cmd == "acquire":
            path = acquire(args.loops_root, args.id, args.actor,
                            ttl_seconds=args.ttl_seconds, role=args.role)
            print(json.dumps({"claimed": args.id, "marker": str(path)}))
            return EXIT_OK
        if args.cmd == "release":
            release(args.loops_root, args.id, args.actor, role=args.role)
            print(json.dumps({"released": args.id}))
            return EXIT_OK
        if args.cmd == "wip":
            print(json.dumps(wip(args.loops_root)))
            return EXIT_OK
    except ClaimError as exc:
        print(f"REFUSE[{exc.code}] {exc}", file=sys.stderr)
        return exc.code

    return EXIT_INSTRUMENT_ERROR  # unreachable: argparse enforces cmd choices


if __name__ == "__main__":
    raise SystemExit(main())
