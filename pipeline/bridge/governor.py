#!/usr/bin/env python3
"""The governor. Non-agentic, deliberately dumb, and the only writer of budget.json.

CONTRACT.md §9 tells every loop to read this file before each iteration and stop
when a limit binds. Nothing was writing it, so `spent_today` and `codex_window_pct`
stayed null forever and the fail-closed rule stopped every loop on the first tick.
This is that missing writer.

It measures three things and refuses to guess at any of them:

  disk / load        cheap, exact, read every tick
  codex window %     newest rollout's token_count "used_percent"
  claude slots       how many pool slots answer a probe (capacity, not balance)

**Every unmeasurable value is written as null, never as zero.** A zero reads as
headroom, and headroom is the wrong direction for the one number that bounds
runaway work. Metered dollars stay null on this estate on purpose: the ledger
holds only $5.00-unmeasured flat estimates for two minor providers, so a sum
would be a confident wrong number rather than an honest unknown.

Claude probes are expensive (a real API round-trip each), so they run on their
own slower cadence and the last result is reused until it goes stale — with the
staleness recorded, so a consumer can tell a fresh unknown from a rotten one.

Usage:
    governor.py --loops-root var/loopqueue [--once] [--probe-claude]
"""

from __future__ import annotations

import argparse
import calendar
import json
import math
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path


class MalformedLimit(ValueError):
    """A governor limit that is present but cannot be used as a number."""


def finite_limit(value, *, what: str) -> float:
    """THE shared coercion for every numeric governor limit.

    Rejects the three shapes that each defeated a different guard here:

      * non-numeric (``""``, ``"abc"``, ``[]``) — used to be swallowed by an
        ``X or DEFAULT`` idiom and read as a favourable default, or (once that
        idiom was removed) to escape as an unhandled TypeError/ValueError;
      * ``inf`` — ``json.loads`` parses ``1e400`` to ``float('inf')`` without
        error, and ``int(inf)`` raises **OverflowError**, which is NOT a
        subclass of ValueError and so slipped straight through an
        ``except (TypeError, ValueError)`` written to catch exactly this;
      * ``NaN`` — the dangerous one, because EVERY comparison against it is
        False. A NaN ceiling silently defeats ``load <= ceiling``,
        ``load > ceiling`` and a ``ceiling <= 0`` halt check simultaneously,
        so it produces no verdict anywhere rather than an obvious error.

    One implementation, imported by governor/claim/integrity, because a
    per-module copy of this is how the family it guards against arose.
    """
    try:
        num = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise MalformedLimit(f"{what}={value!r} is not a number") from exc
    if not math.isfinite(num):
        raise MalformedLimit(f"{what}={value!r} is not finite")
    return num

TICK_SECONDS = 60
CLAUDE_PROBE_SECONDS = 1800  # 30 min: each probe is a real round-trip per slot
CODEX_SESSIONS = Path.home() / ".codex" / "sessions"

# How stale a load reading may be before it is refused rather than used.
#
# Measured 2026-08-08: this daemon runs `--once` from launchd with
# StartInterval=300, so budget.json's `load_avg_1m` is up to 300 s old at the
# moment a consumer reads it. Over one 300 s refresh the value moves by a
# median of 3.3, p90 27.3, max 69.8. Compared against a 30 s ground-truth
# trace, the file's verdict at the ceiling disagreed with reality on 23% of
# reads — 7% false blocks and, worse, 17% false PASSES, which is the direction
# that dispatches a gate onto a box at load 90.
#
# So: a load DECISION never reads the file. It samples. The file stays as the
# published telemetry it always was.
LOAD_READING_MAX_AGE_S = 30
# Real directories, never symlinks: Claude Code canonicalises CLAUDE_CONFIG_DIR,
# so a symlinked path resolves to a DIFFERENT identity than its target. Measured:
# the same account read LIVE via ~/.claude-account-2 and "expired" via a symlink
# to it, with the same 838-byte credentials file visible through both paths.
ACCOUNT_DIRS = sorted(
    (p for p in Path.home().glob(".claude-account-*") if p.is_dir() and not p.is_symlink()),
    key=lambda p: int(p.name.rsplit("-", 1)[-1]) if p.name.rsplit("-", 1)[-1].isdigit() else 999,
)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def read_disk_free_gb(path: Path) -> float | None:
    try:
        return shutil.disk_usage(path).free / 1e9
    except OSError:
        return None


def read_load_1m() -> float | None:
    try:
        return os.getloadavg()[0]
    except OSError:
        return None


def perf_core_count() -> int | None:
    """PERFORMANCE cores — not logical CPUs. None when it cannot be read.

    This is the reconciliation of the two ceilings that disagreed.

    Every document defines the load ceiling the same way. CONTRACT.md §9 writes
    `"load_avg_1m_max": 12,  // host performance-core count`, and
    PROMPT-implementer-loop.md tells the model "1-min load > host
    performance-core count". AccurateGate's merge-gate.yaml encodes
    `quiesce.load1_max: 16` — which is that contract, read correctly, on this
    host.

    Two implementations read the LOGICAL count instead: bootstrap.sh took
    `sysctl -n hw.ncpu` and this file took `os.cpu_count()`. Both return 24 on
    this M2 Ultra, because it is 16 performance + 8 efficiency cores
    (`hw.perflevel0.logicalcpu` = 16, `hw.perflevel1.logicalcpu` = 8). An
    efficiency core does not carry a gate worker at performance-core
    throughput, so 24 overstates the box by 50%.

    The gate's own signed receipts have been recording `host_perf_cores: 16`
    for 192 runs. The ceilings never disagreed on intent — one of them was
    reading the wrong sysctl.
    """
    for key in ("hw.perflevel0.logicalcpu", "hw.perflevel0.physicalcpu"):
        try:
            out = subprocess.run(["sysctl", "-n", key], capture_output=True,
                                 text=True, timeout=5, check=False).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            return None
        if out.isdigit() and int(out) > 0:
            return int(out)
    # No perflevel split (Intel, or a VM): every core is a performance core.
    try:
        out = subprocess.run(["sysctl", "-n", "hw.logicalcpu"], capture_output=True,
                             text=True, timeout=5, check=False).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return None
    return int(out) if out.isdigit() and int(out) > 0 else None


# --------------------------------------------------------------- load gate --

#: A sample must be at least 3% below its predecessor to count as "falling".
#: Anything smaller is inside the noise of a 1-minute exponential average, and
#: treating noise as a trend is how a blind wait gets re-invented.
FALLING_RATIO = 0.97


@dataclass
class LoadCheck:
    """The result of a live load decision. Every field is evidence."""

    clear: bool
    ceiling: float
    load: float | None = None          # the reading the verdict rests on
    samples: list = field(default_factory=list)
    waited_s: float = 0.0
    reason: str = ""

    def as_dict(self) -> dict:
        return {"clear": self.clear, "ceiling": self.ceiling, "load": self.load,
                "samples": self.samples, "waited_s": round(self.waited_s, 1),
                "reason": self.reason}


def sample_load_until_clear(ceiling: float, *, attempts: int = 4, interval: float = 15.0,
                            sampler=read_load_1m, sleeper=time.sleep) -> LoadCheck:
    """Live-sample the 1-min load, retrying WITHIN the caller's iteration.

    Two rules, both of them measured rather than assumed.

    1. **Sample, never read a file.** See LOAD_READING_MAX_AGE_S.

    2. **Only wait while load is FALLING.** A blind wait-and-hope loses. On the
       one fully-observed over-ceiling excursion in the 30 s gate trace
       (2026-08-08 08:47→09:00, load 12 → 94.8 → 13.3) a blind retry cleared
       9% of cases within 60 s and 17% within 120 s, while a discard costs one
       iteration (552 s measured mean). Expected value says a blind wait of W
       only pays if W < clearance(W) x 552 s, and at every W measured it did
       not. What separates the two populations is the DERIVATIVE: a decaying
       transient clears, a rising spike does not and never did. So this waits
       through a falling series and abandons a flat or rising one after a
       single interval — cheap in the case that will not clear, patient in the
       case that will.

    `sampler`/`sleeper` are injected so the policy is testable against a
    recorded trace instead of against wall-clock luck.
    """
    attempts = max(attempts, 1)

    # A ceiling of 0 (or below) is an operator HALT: no real load reading can
    # ever satisfy it, so the decay ladder is pure wall-clock — a measured 15s
    # flat / up to 45s decaying, paid on every bridge/run-loop.sh iteration and
    # every read_governor(live_load=True), for the whole duration of the halt.
    # The halt is exactly when a tick should be cheapest.
    #
    # This lives HERE, in the single shared sampler, and not in the callers.
    # governor.check() and integration.read_governor() both reach it, and a
    # short-circuit copied into one of them is the incomplete-propagation
    # defect this module's own header warns about — the cross-lineage review
    # caught precisely that: an earlier draft fixed check() alone.
    if ceiling <= 0:
        return LoadCheck(False, ceiling, None, [], 0.0,
                         f"ceiling {ceiling:g} — an explicit halt; not sampling")

    waited = 0.0
    samples: list[float] = []
    prev: float | None = None

    for attempt in range(attempts):
        load = sampler()
        if load is None:
            # Unmeasurable load is not idle load. Refuse; never read as headroom.
            return LoadCheck(False, ceiling, None, samples, waited,
                             "load is unmeasurable — UNKNOWN never passes")
        samples.append(round(load, 2))

        if load <= ceiling:
            return LoadCheck(
                True, ceiling, load, samples, waited,
                f"1-min load {load:.2f} <= ceiling {ceiling:g}"
                + (f" after {waited:.0f}s of re-sampling" if waited else ""))

        if attempt == attempts - 1:
            break

        # Wait only if the previous step actually decayed. FALLING_RATIO leaves
        # room for load-average noise: a 1-2% wobble is not a downward trend.
        if prev is not None and load > prev * FALLING_RATIO:
            return LoadCheck(
                False, ceiling, load, samples, waited,
                f"1-min load {load:.2f} > ceiling {ceiling:g} and not decaying "
                f"({prev:.2f} -> {load:.2f}, needs <{prev * FALLING_RATIO:.2f}) — "
                "waiting would not clear it")

        prev = load
        sleeper(interval)
        waited += interval

    load = samples[-1] if samples else None
    return LoadCheck(False, ceiling, load, samples, waited,
                     f"1-min load {load:.2f} > ceiling {ceiling:g} after "
                     f"{waited:.0f}s of re-sampling ({len(samples)} samples)")


# -------------------------------------------------------------- admission --

#: A twin reading unavailable in-process at all. `admit()` treats a `None`
#: twin_reader the same as one that answers unmeasurable — see below.
_NO_TWIN = object()


def admit(kind: str, cost, *, load_reader=read_load_1m, core_reader=perf_core_count,
         ceiling: float | None = None, twin_reader=_NO_TWIN) -> dict:
    """Should a caller spawn `kind` (costing `cost`) right now? the operator 2026-08-09,
    "stop load-171 recurring".

    Load-171 was a blind spawn dispatched onto a box whose load could not be
    read — the exact defect this function exists to make unrepresentable.
    Every unmeasurable or exceptional input below resolves to ``wait``, never
    ``go``. This mirrors ``sample_load_until_clear``'s "UNKNOWN never passes"
    rule, but for a single point-in-time admission decision rather than a
    retrying sampler — so it is PURE: no sleeping, no subprocess spawn, no
    budget.json mutation. It reads (via the injected readers) and decides.

    Verdicts, in the order they are tried:

      go          local 1-min load <= ceiling (== ceiling counts, same as the
                  load gate's own boundary rule).
      route-twin  local load is over ceiling AND a twin/remote box is
                  measurably free (also <= ceiling). The twin reading is
                  behind an injectable seam (`twin_reader`) precisely so a
                  later wiring lane can supply the real remote probe without
                  this module growing a network call of its own — governor.py
                  makes no SSH or HTTP calls, on purpose (see module docstring).
      wait        everything else: local load unmeasurable or its reader
                  raised, core count unavailable or its reader raised, the
                  ceiling is malformed (NaN, inf, non-numeric), no twin_reader
                  was supplied, the twin is itself unmeasurable/raises, or the
                  twin is also over ceiling. Fail-safe is the point: an
                  unknown anywhere in this decision must not be read as
                  headroom.

    `twin_reader` defaults to the sentinel `_NO_TWIN` (rather than `None`) so
    a caller can explicitly pass `twin_reader=None` — "I checked, there is no
    twin available" — and it is handled identically: no twin evidence means
    `wait`, never a silent `go`.
    """
    reason_bits = []

    try:
        core = core_reader()
    except Exception as exc:  # noqa: BLE001 — any reader exception fails closed
        return {"verdict": "wait", "kind": kind, "cost": cost, "load": None,
               "ceiling": None, "reason": f"core count reader raised "
               f"{type(exc).__name__}: {exc} — UNKNOWN never passes"}
    if not core:
        return {"verdict": "wait", "kind": kind, "cost": cost, "load": None,
               "ceiling": None,
               "reason": "performance-core count is unmeasurable — UNKNOWN never passes"}

    raw_ceiling = core if ceiling is None else ceiling
    try:
        ceil = finite_limit(raw_ceiling, what="admit ceiling")
    except MalformedLimit as exc:
        return {"verdict": "wait", "kind": kind, "cost": cost, "load": None,
               "ceiling": None, "reason": f"{exc} — fail closed"}

    try:
        load = load_reader()
    except Exception as exc:  # noqa: BLE001 — any reader exception fails closed
        return {"verdict": "wait", "kind": kind, "cost": cost, "load": None,
               "ceiling": ceil, "reason": f"load reader raised "
               f"{type(exc).__name__}: {exc} — UNKNOWN never passes"}
    if load is None:
        return {"verdict": "wait", "kind": kind, "cost": cost, "load": None,
               "ceiling": ceil,
               "reason": "load is unmeasurable — UNKNOWN never passes"}

    if load <= ceil:
        return {"verdict": "go", "kind": kind, "cost": cost, "load": load,
               "ceiling": ceil,
               "reason": f"1-min load {load:.2f} <= ceiling {ceil:g}"}

    reason_bits.append(f"1-min load {load:.2f} > ceiling {ceil:g}")

    if twin_reader is _NO_TWIN:
        reason_bits.append("no twin reading available — UNKNOWN never passes")
        return {"verdict": "wait", "kind": kind, "cost": cost, "load": load,
               "ceiling": ceil, "reason": "; ".join(reason_bits)}

    try:
        twin_load = twin_reader()
    except Exception as exc:  # noqa: BLE001 — any reader exception fails closed
        reason_bits.append(f"twin reader raised {type(exc).__name__}: {exc} "
                           "— UNKNOWN never passes")
        return {"verdict": "wait", "kind": kind, "cost": cost, "load": load,
               "ceiling": ceil, "reason": "; ".join(reason_bits)}

    if twin_load is None:
        reason_bits.append("twin load is unmeasurable — UNKNOWN never passes")
        return {"verdict": "wait", "kind": kind, "cost": cost, "load": load,
               "ceiling": ceil, "reason": "; ".join(reason_bits)}

    if twin_load <= ceil:
        reason_bits.append(f"twin 1-min load {twin_load:.2f} <= ceiling {ceil:g}")
        return {"verdict": "route-twin", "kind": kind, "cost": cost, "load": load,
               "ceiling": ceil, "reason": "; ".join(reason_bits)}

    reason_bits.append(f"twin 1-min load {twin_load:.2f} > ceiling {ceil:g} too")
    return {"verdict": "wait", "kind": kind, "cost": cost, "load": load,
           "ceiling": ceil, "reason": "; ".join(reason_bits)}


def read_codex_window_pct() -> float | None:
    """`used_percent` from the newest rollout. None when it cannot be read.

    Never 0.0 on failure: an unreadable window must not look like an empty one.
    """
    try:
        rollouts = sorted(
            CODEX_SESSIONS.rglob("rollout-*.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return None
    for rollout in rollouts[:3]:  # newest few; older ones may predate the field
        try:
            text = rollout.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # last occurrence wins — the window grows through the session
        marker = '"used_percent":'
        idx = text.rfind(marker)
        if idx == -1:
            continue
        tail = text[idx + len(marker) : idx + len(marker) + 24]
        num = ""
        for ch in tail.lstrip():
            if ch.isdigit() or ch == ".":
                num += ch
            else:
                break
        if num:
            try:
                return float(num)
            except ValueError:
                continue
    return None


def probe_claude_slots(max_slot: int = 0, timeout_s: int = 20) -> dict:
    """Distinct Claude accounts, via the estate's own `claudeN` launchers.

    Three earlier approaches here were all wrong, and each failed *silently*:

    1. A symlink farm under ~/.claude-accounts. OmniAgentOS's own
       var/claude-accounts/README.md has warned since 2026-07-30 that a login is
       bound to the CANONICAL config-dir path, so a symlinked CLAUDE_CONFIG_DIR
       makes a logged-in profile report logged OUT. Produced 8 phantom
       "expired" accounts.
    2. Reading .credentials.json. Not the authoritative store — profiles with an
       empty token file are fully logged in.
    3. Counting probes that answer. Every path answered because they all reached
       one working credential: 13 "live accounts", one real account.

    `claudeN auth status --json` is the authority: it reports the EMAIL, so
    capacity is countable by distinct identity rather than by responsive path.
    """
    import shutil as _sh

    accounts, errors = {}, []
    for n in range(1, 10):
        launcher = f"claude{n}"
        if not _sh.which(launcher):
            continue
        try:
            proc = subprocess.run([launcher, "auth", "status", "--json"],
                                  capture_output=True, text=True,
                                  timeout=timeout_s, check=False)
            info = json.loads(proc.stdout or "{}")
        except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
            errors.append(launcher)
            continue
        if info.get("loggedIn") and info.get("email"):
            accounts.setdefault(info["email"], []).append(launcher)
        else:
            errors.append(launcher)

    return {
        # DISTINCT identities — the only number that means capacity.
        "distinct_accounts": len(accounts),
        "by_email": {e: p for e, p in accounts.items()},
        "profiles_logged_in": sum(len(p) for p in accounts.values()),
        "not_logged_in": errors,
        "live_count": len(accounts),
        "probed_at": _now(),
    }


def build_budget(root: Path, previous: dict, *, probe_claude: bool) -> dict:
    budget = json.loads(json.dumps(previous)) if previous else {}

    budget["disk_free_gb"] = read_disk_free_gb(root)
    budget["load_avg_1m"] = read_load_1m()
    budget.setdefault("disk_free_gb_min", 20)
    # Performance cores, not logical CPUs — see perf_core_count(). `setdefault`
    # would leave the wrong 24 that bootstrap.sh already wrote into the live
    # file, so the correction is applied to an existing key too, and only when
    # the value is one the two known-buggy readers produce.
    perf = perf_core_count()
    if perf:
        current = budget.get("load_avg_1m_max")
        if current is None or current == (os.cpu_count() or 8):
            budget["load_avg_1m_max"] = perf
    else:
        budget.setdefault("load_avg_1m_max", os.cpu_count() or 8)
    budget["load_avg_1m_max_basis"] = (
        "host performance-core count (CONTRACT §9; matches AccurateGate "
        "merge-gate.yaml quiesce.load1_max)" if perf else "logical CPU count (perflevel unreadable)")
    budget.setdefault("wip_cap", 4)

    sub = budget.setdefault("subscription", {})
    accounts = sub.setdefault("accounts", {})
    for key, acct in accounts.items():
        # "// note" keys are documentation, not accounts — their values are strings.
        if key.startswith("//") or not isinstance(acct, dict):
            continue
        if acct.get("provider") == "codex":
            acct["window_pct"] = read_codex_window_pct()
            acct.setdefault("window_pct_max", 85)

    prev_claude = (previous or {}).get("subscription", {}).get("claude_pool", {})
    if probe_claude:
        sub["claude_pool"] = probe_claude_slots()
    elif prev_claude:
        sub["claude_pool"] = prev_claude
        age = time.time() - calendar.timegm(time.strptime(prev_claude["probed_at"], "%Y-%m-%dT%H:%M:%SZ"))
        # Say so rather than letting an old count pass as current.
        sub["claude_pool"]["stale"] = age > CLAUDE_PROBE_SECONDS * 2
    else:
        sub["claude_pool"] = {"live_count": None, "probed_at": None, "stale": True}

    # Metered dollars stay null unless something real reports them. See module docstring.
    budget.setdefault("metered_usd", {})
    budget["updated_at"] = _now()
    budget["updated_by"] = "governor"
    return budget


def write_atomic(path: Path, obj: dict) -> None:
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    tmp.rename(path)


CHECK_CLEAR = 0
CHECK_BLOCKED = 3
EXIT_COULD_NOT_RUN = 2   # CONTRACT §8 rule (a): the instrument could not evaluate its
                         # input. main() already returns 2 for a missing queue dir (:509).


class BudgetUnreadable(Exception):
    """budget.json is PRESENT but could not be turned into a policy dict."""


def load_previous(budget_path: Path) -> dict:
    """Three cases the old one-liner conflated into one.

    (i)   ABSENT            -> {}   first run; the built-in defaults ARE correct
    (ii)  PRESENT, unusable -> raise  the previous policy is UNKNOWN
    (iii) PRESENT, an object-> merge normally

    No exists() pre-check: exists()-then-read is a TOCTOU, and a file deleted
    between the two belongs in (i), not (ii).
    """
    try:
        raw = budget_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError) as exc:
        raise BudgetUnreadable(f"{type(exc).__name__}: {exc}") from exc
    try:
        previous = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BudgetUnreadable(f"JSONDecodeError: {exc}") from exc
    if not isinstance(previous, dict):
        # The refusal all three READERS already make and the WRITER never did:
        # claim.py:210-214, integration.py:342-343. Measured at tip: `[]` and a
        # zero-byte file rebuilt SILENTLY at exit 0; `[1,2]` and `"nope"` escaped
        # build_budget as an uncaught TypeError.
        raise BudgetUnreadable(f"not a JSON object ({type(previous).__name__})")
    return previous


def refuse(loops_root: Path, budget_path: Path, why: str) -> None:
    """Case (ii). Do NOT write. The file on disk IS the last known-good value.

    Preserving policy 'in memory' is not available in production: the launchd job
    runs `--once` with StartInterval=300 and no KeepAlive, so every tick is a fresh
    process with no memory of the last good budget. The only surviving copy is the
    file, and the way to preserve it is to not overwrite it.
    """
    msg = (f"governor REFUSED to rewrite {budget_path}: present but unreadable ({why}). "
           "The previous policy is UNKNOWN, so rebuilding from defaults would silently "
           "replace the operator ruling — measured: wip_cap 60->4 (a halt at wip=54 on "
           "2026-08-11T06:45Z), disk_free_gb_min 40->20, load_avg_1m_max 12->16, and "
           "metered_usd + subscription.accounts emptied, removing every dollar and "
           "window ceiling. THE FILE IS UNCHANGED. REMEDY: repair the JSON to keep the "
           "current policy, or DELETE the file to accept the built-in defaults — an "
           "absent file is a first run and rebuilds cleanly on the next tick. Until one "
           "of those happens updated_at stops advancing and the loops will stop on the "
           "existing staleness guard.")
    print(json.dumps({"instrument_error": "budget_unreadable", "path": str(budget_path),
                      "why": why, "wrote": False, "at": _now()}))
    try:
        st = budget_path.stat()
        fingerprint = f"{st.st_size}:{st.st_mtime_ns}"
    except OSError:
        fingerprint = "unstattable"
    try:
        # FUNCTION-LOCAL, and it must stay that way: claim.py:78 imports THIS module at
        # module scope, so a module-scope `import claim` here is circular. Do not solve
        # that by copying alert_once into this file — claim.py:75-77 names the local
        # copy as the exact defect the shared helper exists to prevent.
        try:
            from bridge.claim import alert_once
        except ModuleNotFoundError:
            from claim import alert_once
        alert_once(loops_root, f"governor-budget-unreadable:{fingerprint}", msg,
                   source="governor.py")
    except Exception:
        pass   # alerting must never take the only writer of budget.json down
    try:
        try:
            from bridge.ledger_write import append_event
        except ModuleNotFoundError:
            from ledger_write import append_event
        append_event(loops_root, {"ts": _now(), "role": "external",
                                  "event": "instrument_error", "actor": "governor.py",
                                  "detail": {"kind": "budget_unreadable", "why": why,
                                             "path": str(budget_path), "wrote": False}})
    except Exception:
        pass


def check(budget_path: Path) -> int:
    """Should the LOOP back off? Answer with an exit code, and name the reason.

    This exists because a backoff has to key on something the system EMITS.

    run-loop.sh used to decide by grepping its own log:

        grep -qiE "governor|blocked|disk|load|wip|cap|UNKNOWN, so stop" <(tail -40 "$LOG")

    which matches the model's own prose. Every iteration in which the model
    merely *wrote the word* "load" or "cap" — i.e. almost every iteration —
    scored as a governor block. Measured on the live logs 2026-08-08: it fired
    on 59 of 61 implementer iterations, 7 of 14 planning, and 4 of 5 reviewer,
    and the backoff it then announced was `0s` every single time, so the loop
    respawned immediately and the message described nothing that happened.

    A phantom block in the log costs an operator an afternoon chasing a
    governor problem that does not exist.

    ONLY HARD LIMITS BIND HERE, and load is deliberately not one of them.
    Backing the loop off is only correct when the next iteration could achieve
    nothing — an unreadable budget or a full disk. High load does not mean that:
    an iteration still admits candidates, replays the ledger and rebuilds
    queue.json, all of which are file reads costing milliseconds. Load bounds
    the GATE, and integration.py enforces it there (`Governor.may_gate`), where
    it can defer one expensive step instead of idling the whole loop. Load is
    still SAMPLED and reported below as `gate_clear`, because an operator
    reading this wants to know — it just does not set the exit code.
    """
    stops, notes = [], []
    try:
        budget = json.loads(budget_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        budget = {}
        stops.append(f"budget unreadable ({type(exc).__name__}) — fail closed")

    free = read_disk_free_gb(budget_path.parent)
    # PRE-EXISTING on main, same class as the malformed-ceiling handling below:
    # a `disk_free_gb_min` that will not compare (a string, a list) raised an
    # UNCAUGHT TypeError out of check() — `'<' not supported between instances
    # of 'float' and 'str'` — so the governor died rather than stopping. Fixed
    # here because it is the same value ("a malformed limit must fail closed,
    # never crash and never read as headroom") that this change is about.
    try:
        floor = finite_limit(budget.get("disk_free_gb_min", 20), what="disk_free_gb_min")
    except MalformedLimit as exc:
        stops.append(f"{exc} — fail closed")
        floor = float("inf")
    if free is None:
        stops.append("disk free is unmeasurable — UNKNOWN never passes")
    elif free < floor:
        stops.append(f"disk {free:.1f} GB < floor {floor:g} GB")

    # Same falsy-zero family as wip_cap (F6/F7, and claim.py). An explicit
    # `load_avg_1m_max: 0` means "defer on any load at all" — a halt — and the
    # `or` chain silently replaced it with the host's core count. Only a
    # genuinely ABSENT key may fall through to the discovered default.
    # integration.py:338-341 already reads this key the same way.
    raw_ceiling = budget.get("load_avg_1m_max")
    if raw_ceiling is None:
        raw_ceiling = perf_core_count() or (os.cpu_count() or 8)
    try:
        ceiling = finite_limit(raw_ceiling, what="load_avg_1m_max")
    except MalformedLimit as exc:
        # Present but unusable. Fail CLOSED, consistent with the unreadable-
        # budget branch above: a malformed ceiling must never read as headroom.
        # NaN in particular must land here rather than reaching the sampler,
        # where every comparison against it is False and it therefore produces
        # a 45s full-ladder wait and a nonsense "ceiling nan" note.
        stops.append(f"{exc} — fail closed")
        ceiling = 0.0

    # The ceiling<=0 halt short-circuit lives inside sample_load_until_clear,
    # so this caller and integration.read_governor() share one implementation.
    lc = sample_load_until_clear(ceiling)
    notes.append(lc.reason + ("" if lc.clear else "  [defers the GATE only, not the loop]"))

    print(json.dumps({"clear": not stops, "stops": stops, "notes": notes,
                      "gate_clear": lc.clear, "load": lc.as_dict()}))
    return CHECK_BLOCKED if stops else CHECK_CLEAR


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--loops-root", required=True, type=Path)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--probe-claude", action="store_true", help="force a slot probe this tick")
    ap.add_argument("--check", action="store_true",
                    help="exit 0 if no limit binds, 3 if one does. Writes nothing. "
                         "This is the CARRIER a backoff may key on.")
    args = ap.parse_args()

    budget_path = args.loops_root / "state" / "budget.json"
    if not budget_path.parent.is_dir():
        print(f"no such queue: {budget_path.parent}", file=os.sys.stderr)
        return 2

    if args.check:
        return check(budget_path)

    last_probe = 0.0
    while True:
        try:
            previous = load_previous(budget_path)
        except BudgetUnreadable as exc:
            refuse(args.loops_root, budget_path, str(exc))
            if args.once:
                return EXIT_COULD_NOT_RUN
            time.sleep(TICK_SECONDS)
            continue

        due = args.probe_claude or (time.time() - last_probe) > CLAUDE_PROBE_SECONDS
        budget = build_budget(args.loops_root, previous, probe_claude=due)
        if due:
            last_probe = time.time()
        write_atomic(budget_path, budget)

        pool = budget["subscription"]["claude_pool"]
        print(json.dumps({
            "updated_at": budget["updated_at"],
            "disk_free_gb": round(budget["disk_free_gb"], 1) if budget["disk_free_gb"] else None,
            "load_avg_1m": budget["load_avg_1m"],
            "claude_live_slots": pool.get("live_count"),
        }))

        if args.once:
            return 0
        time.sleep(TICK_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
