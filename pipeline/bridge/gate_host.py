#!/usr/bin/env python3
"""Where should a gate run — this box, or the twin?

THE PROBLEM, MEASURED
---------------------

Gate work has been running on the loaded box while an idle twin sat there.
Across the recorded gate history in `var/gate-evidence/records/merge-gate/`,
essentially every run executed locally; over the same period `mw0001-owner` idled
between 1.2 and 1.7 1-min load while this host ran at a median of 11 and a p75
of 35 (governor.log, 217 samples, 18.7 h).

That matters twice over. Local load is not only higher, it is *dirtier*: a
desktop carries irreducible interactive noise — Chrome, `suggestd`, Backblaze —
that a headless twin does not. The gate's own instrument records what that
costs: its counterfeit control "never passed under load>10 (4 runs, 2
machines)", and contention on a nominally-quiet box has already been
misclassified as a candidate defect and had to be cleared by an operator by
hand. A clean room is not a luxury for this workload; it is the difference
between a verdict about the code and a verdict about the desktop.

THE RULE THIS FILE ENCODES
--------------------------

1. **Probe at dispatch, or do not dispatch.** Never route on a cached reading.
   A load number is refused outright once it is older than
   `governor.LOAD_READING_MAX_AGE_S`. This is the same defect as Defect 1 seen
   from the other end: budget.json is up to 300 s stale and disagreed with
   ground truth on 23% of reads. Twice on 2026-08-08 work was sent to a "quiet"
   box on a reading that was minutes old and already wrong.

2. **One preflight, then abort.** A single `ssh -o BatchMode=yes` probe decides
   reachability. If it fails the gate runs locally — it never fans out N
   attempts to rediscover a dead credential.

3. **Route by SUITE SENSITIVITY, not only by load.** The twin is a twin, not a
   clone. See `PARITY` below for what was measured rather than assumed.

4. **Prefer local on a tie.** Remote costs an rsync of the evidence store back
   before anything can land. Idle local wins.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

try:                                    # module or script
    from bridge.governor import LOAD_READING_MAX_AGE_S, perf_core_count, read_load_1m
except ImportError:                     # pragma: no cover
    from governor import LOAD_READING_MAX_AGE_S, perf_core_count, read_load_1m

try:                                    # PyYAML ships in the gate venv
    import yaml
except ImportError:                     # pragma: no cover - config then unreadable
    yaml = None


TWIN_HOST = "mw0001-owner"
SSH_PROBE_TIMEOUT_S = 8
#: `ssh -G` expands LOCAL config only — no packet leaves the box — so this is a
#: bound on a fork+exec, not on a network round trip. It is generous on purpose:
#: the fork that matters is the FIRST (cold) `ssh` in a freshly-spawned
#: `gate_loop.py --once` process, whose dynamic linker must fault in libssh and
#: libcrypto and read the ssh config from disk. On a gate box that sits at 1-min
#: load 35 (see the module docstring) that cold fork has been observed to overrun
#: the old 3 s budget — and because identity resolution runs on the DISPATCHER,
#: a local CPU spike must NEVER be allowed to look like a remote twin with "no
#: identity" and drop a healthy 24-core box (the mw0001-owner regression,
#: 2026-08-12: 1 drop in 1187 ticks, always the FIRST-listed twin).
SSH_IDENTITY_TIMEOUT_S = 5
#: Identity resolution is retried a bounded number of times on a TRANSIENT local
#: failure (a timed-out or non-zero fork), because that is exactly the fault the
#: regression was: attempt 1 overruns while cold and under load; attempt 2 — with
#: libssh already resident and the config already in the page cache — returns in
#: milliseconds. This is NOT the network "retry storm" the load probe forbids
#: (rule 2): no packet leaves the box, the cost is a local fork, and re-running
#: it is how a momentary spike on the dispatcher stops masquerading as a box that
#: cannot be proven distinct. Fail-safe is untouched: exhausting every attempt
#: still returns None, and None still DROPS the host.
SSH_IDENTITY_ATTEMPTS = 3
SSH_IDENTITY_RETRY_BACKOFF_S = 0.25

#: Width of the gate's test ladder — the burst of parallel workers a dispatched
#: gate runs. THE single source of truth: admission (a twin must have at least
#: this much headroom, gate_host) and the dispatch env (MERGE_GATE_LADDER_WORKERS,
#: gate_loop) both derive from this name, so the admission bar and the actual
#: burst can never drift apart silently.
GATE_LADDER_WORKERS = 8


def effective_ladder_workers() -> int:
    """Return the operator-selected ladder width, safely bounded for admission.

    The value is consumed both while admitting a twin and while constructing the
    gate's environment.  A malformed override must therefore fail closed to the
    documented default rather than raise in the scheduler or silently admit a
    host for an unbounded test burst.
    """
    raw = os.environ.get("MERGE_GATE_LADDER_WORKERS")
    if raw is None:
        return GATE_LADDER_WORKERS
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logging.getLogger(__name__).warning(
            "invalid MERGE_GATE_LADDER_WORKERS=%r; using default %s",
            raw, GATE_LADDER_WORKERS)
        return GATE_LADDER_WORKERS
    if not 1 <= value <= 64:
        logging.getLogger(__name__).warning(
            "out-of-range MERGE_GATE_LADDER_WORKERS=%r; using default %s",
            raw, GATE_LADDER_WORKERS)
        return GATE_LADDER_WORKERS
    return value

#: Paths on the TWIN for a remotely-dispatched gate. Same layout as local
#: (both hosts run as `youruser` and the gate stack was installed to the
#: same prefix) but named rather than assumed, because "it is a twin so the
#: path is the same" is exactly the class of assumption that produced the
#: parity surprises on 2026-08-08. Centralised here (not in bridge/integration.py)
#: so the twin-host constants live in one place with TWIN_HOST above.
REMOTE_GATE_WORKSPACE = "/Users/youruser/OmniAgentOS-gate"
REMOTE_EVIDENCE_ROOT = "/Users/youruser/OmniAgentOS/var/gate-evidence"


@dataclass(frozen=True)
class TwinSpec:
    """One remote grading box, with the paths that are TRUE ON THAT BOX.

    The paths are per-twin rather than global because the second twin is not a
    user-level clone of the first: MW0001 runs as `youruser`, MW0002 only has
    a `cloud` account (creating `youruser` there needs a sudo password nobody
    holds). Assuming one prefix for both is the same "it is a twin so the path
    is the same" mistake the 2026-08-08 parity surprises came from, so the
    prefix travels WITH the host instead of being reconstructed at each site.
    """
    host: str
    workspace: str
    evidence_root: str
    #: PERFORMANCE cores on that box, measured not assumed. Used to rank twins by
    #: HEADROOM rather than by raw load1: MW0002 has 12 to MW0001's 16, so a bare
    #: load comparison picks the smaller box when it actually has less room, and
    #: the resulting contention surfaces as a test failure charged to the
    #: candidate (cross-lineage review 2026-08-10, finding 4).
    perf_cores: int = 16


#: The twin pool, in PREFERENCE ORDER. Ties break toward the earlier entry, so
#: MW0001 — the box with measured parity and months of receipts — keeps winning
#: an even race, and MW0002 is used as additional capacity rather than as a
#: silent replacement for a proven host.
#:
#: MW0002 is NOT identical to MW0001 and the difference is recorded rather than
#: smoothed over. Measured 2026-08-10, all three boxes on macOS 26.6 build
#: 25G72, node v26.7.0 on both twins (local is v22.22.0 — a pre-existing
#: divergence), TZ pinned at dispatch so it cannot be observed:
#:
#:   MW0001   24 logical / 16 performance cores
#:   MW0002   16 logical / 12 performance cores   <-- SMALLER
#:
#: The gate derives its ladder-worker count from performance cores, so a gate
#: on MW0002 is correct but slower. That is a capacity fact, not a parity
#: defect; it must not be reported against a candidate.
BUILTIN_TWIN_SPECS: tuple = (
    TwinSpec(TWIN_HOST, REMOTE_GATE_WORKSPACE, REMOTE_EVIDENCE_ROOT,
             perf_cores=16),
    TwinSpec("mw0002",
             "/Users/cloud/OmniAgentOS-gate",
             "/Users/cloud/OmniAgentOS/var/gate-evidence",
             perf_cores=12),
)

#: Where the pool is DECLARED. Adding a gate box is a config edit — the shipped
#: file is a description of `BUILTIN_TWIN_SPECS`, not a change to it, so an
#: installation that never touches the file behaves exactly as before.
#: `GATE_HOSTS_CONFIG` overrides the path (tests, and per-installation layouts).
DEFAULT_GATE_HOSTS_CONFIG = Path(__file__).resolve().parents[2] / "configs" / "gate-hosts.yaml"


def _config_warn(message: str) -> None:
    """Config faults are INSTRUMENT facts an operator must see the same tick.

    Same `gate-host CONFIG:` prefix as the active-pool warnings, so one grep
    over the daemon log finds every reason the pool is not what was intended.
    """
    print(f"gate-host CONFIG: {message}", file=sys.stderr)


def _twin_spec_from_entry(entry) -> TwinSpec | None:
    """One validated pool entry, or None when the entry cannot be trusted.

    Validation is deliberately strict and positive. Every field here ends up in
    an ssh destination or a remote path, so a permissive parse is how a typo
    becomes a gate dispatched at a directory that does not exist on that box —
    which returns a workspace error that READS LIKE A CANDIDATE DEFECT.
    """
    if not isinstance(entry, dict):
        return None
    host = entry.get("host")
    if not isinstance(host, str):
        return None
    host = host.strip()
    # A name with whitespace/controls, or one that could be read as an ssh
    # option, must never reach a command line.
    if (not host or host.startswith("-")
            or any(ch.isspace() or ord(ch) < 32 for ch in host)):
        return None
    workspace = entry.get("workspace", REMOTE_GATE_WORKSPACE)
    evidence_root = entry.get("evidence_root", REMOTE_EVIDENCE_ROOT)
    for value in (workspace, evidence_root):
        if not isinstance(value, str) or not value.startswith("/"):
            return None
    cores = entry.get("perf_cores", 16)
    # bool is an int subclass; `perf_cores: true` is a mistake, not a core count.
    if isinstance(cores, bool) or not isinstance(cores, int) or not 1 <= cores <= 256:
        return None
    return TwinSpec(host, workspace.strip(), evidence_root.strip(), perf_cores=cores)


def load_twin_specs(*, config_path=None, environ: dict | None = None) -> tuple:
    """The DECLARED twin pool, in preference order.

    An explicitly empty `twins:` (``null`` or ``[]``) is a declaration — a
    local-only installation — and is honoured. Anything the parser cannot trust
    (missing file, unreadable, bad YAML, wrong shape, one invalid entry) falls
    back to `BUILTIN_TWIN_SPECS`: a broken config must leave the installation
    running exactly as it did before the file existed, never silently repoint a
    gate box on the strength of a half-written file.

    Identity is NOT resolved here. Membership is what the operator declares;
    which declarations are DISTINCT PHYSICAL BOXES is decided by
    `collapse_to_physical_boxes`, and only that decision may create a slot.
    """
    env = os.environ if environ is None else environ
    chosen = Path(config_path or env.get("GATE_HOSTS_CONFIG")
                  or DEFAULT_GATE_HOSTS_CONFIG)
    if yaml is None:                                    # pragma: no cover
        return BUILTIN_TWIN_SPECS
    try:
        raw = yaml.safe_load(chosen.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return BUILTIN_TWIN_SPECS
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        _config_warn(f"{chosen} is unreadable ({exc.__class__.__name__}) — "
                     "using the built-in twin pool")
        return BUILTIN_TWIN_SPECS
    if not isinstance(raw, dict) or "twins" not in raw:
        _config_warn(f"{chosen} has no top-level `twins:` list — "
                     "using the built-in twin pool")
        return BUILTIN_TWIN_SPECS
    declared = raw["twins"]
    if declared is None or declared == []:
        return ()
    if not isinstance(declared, list):
        _config_warn(f"{chosen} `twins:` is not a list — using the built-in twin pool")
        return BUILTIN_TWIN_SPECS
    specs: list = []
    seen: set = set()
    for entry in declared:
        spec = _twin_spec_from_entry(entry)
        if spec is None:
            _config_warn(f"{chosen} entry {entry!r} is invalid — "
                         "using the built-in twin pool rather than a partial one")
            return BUILTIN_TWIN_SPECS
        if spec.host in seen:
            # First wins. A later duplicate NAME must not be able to repoint an
            # already-declared box at a different workspace.
            _config_warn(f"{chosen} lists {spec.host!r} twice — keeping the first entry")
            continue
        seen.add(spec.host)
        specs.append(spec)
    return tuple(specs)


_ALL_TWIN_SPECS: tuple = load_twin_specs()


#: Every twin this installation KNOWS ABOUT. The pool actually used is
#: TWIN_SPECS below, which may be a subset.
KNOWN_TWIN_SPECS: tuple = _ALL_TWIN_SPECS


def _active_twin_specs() -> tuple:
    """The twins this installation should actually use.

    `THREELOOPS_ACTIVE_TWINS` (comma-separated hosts) narrows the pool; unset
    means all of them. Without this there is no way back to single-twin
    operation, so an installation with one box would keep claiming a slot on an
    absent host and converting it into an instrument failure every tick
    (cross-lineage review 2026-08-10, finding 5).

    REVERSED 2026-08-10 (F-A2, round-2 review): a nonempty value that names NO
    known host — a typo like `mwTYPO` — used to fall back to "unset" (all
    twins active). That is the WORSE failure, not the safer one: it silently
    ACTIVATES hosts the operator never selected, on nothing but a spelling
    mistake, and remote gating then dispatches to boxes the config explicitly
    did not name. Fail closed instead — `()`, i.e. no remote twins — and say so
    loudly on stderr (`gate-host CONFIG:` prefix, daemon-log-visible) so the
    typo is an instrument fact an operator sees the same tick, not a silent
    downgrade to local-only that nobody notices. A PARTIAL match (some tokens
    known, some not) keeps the known subset active and still warns, naming the
    dropped tokens, so one bad entry in a list does not disable the whole pool.
    """
    raw = os.environ.get("THREELOOPS_ACTIVE_TWINS", "").strip()
    if not raw:
        return _ALL_TWIN_SPECS
    wanted = [h.strip() for h in raw.split(",") if h.strip()]
    known_hosts = {spec.host for spec in _ALL_TWIN_SPECS}
    chosen = tuple(spec for spec in _ALL_TWIN_SPECS if spec.host in wanted)
    unknown = [h for h in wanted if h not in known_hosts]
    if not chosen:
        import sys
        print(f"gate-host CONFIG: THREELOOPS_ACTIVE_TWINS={raw!r} matched no known "
              f"twin (known: {sorted(known_hosts)}) — activating NO remote twins "
              "rather than guessing at unselected hosts.", file=sys.stderr)
        return ()
    if unknown:
        import sys
        print(f"gate-host CONFIG: THREELOOPS_ACTIVE_TWINS dropped unknown token(s) "
              f"{unknown} — active pool is {[spec.host for spec in chosen]}.",
              file=sys.stderr)
    return chosen


# ------------------------------------------------------------ PHYSICAL IDENTITY
#
# ONE GATE PER PHYSICAL BOX is the invariant this scheduler exists to hold: two
# ~12-minute gates on one Mac is the CPU-overload class that has already been
# misreported as a candidate defect. Config-driven membership is where that
# invariant is easiest to lose, because a pool is a list of NAMES and a name is
# not a machine — on this installation `mw0001` and `mw0001-owner` are two names
# for 203.0.113.10.
#
# Identity is resolved LOCALLY with `ssh -G`, which expands
# Host/Match/Include/ProxyJump exactly as the real dial would and prints the
# `hostname` it would connect to, without sending a packet. The remote's own
# idea of its hostname is deliberately never consulted: two Macs both answer
# `Mac-Studio.local`, which would collapse two real boxes into one slot and
# waste a paid machine — the mirror-image failure.
# ------------------------------------------------------------------------------

#: Destinations that mean "this very box". A configured remote that expands to
#: one of these is not extra capacity — `local` already owns that slot.
_LOOPBACK_IDS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0", "0000:0000:0000:0000:0000:0000:0000:0001"})


def _normalise_identity(value: str) -> str:
    """Compare destinations the way DNS does: case- and trailing-dot-blind."""
    return value.strip().rstrip(".").lower()


#: Successful expansions only. A FAILURE is never cached: it would freeze a
#: transient fork failure into a permanently-dropped box, and re-probing costs
#: one local fork. Lives for the process; the daemon re-reads ssh config on
#: restart, which is also when a pool edit is picked up.
_IDENTITY_CACHE: dict = {}


def resolve_ssh_physical_id(host: str, *,
                            timeout_s: int = SSH_IDENTITY_TIMEOUT_S,
                            attempts: int = SSH_IDENTITY_ATTEMPTS) -> str | None:
    """The destination `ssh <host>` would actually dial, or None if unprovable.

    None is a REFUSAL, not an error to paper over: a host whose identity cannot
    be established cannot be shown to be a different machine from one already
    gating, so it must not buy a scheduler slot.

    The expansion is LOCAL (`ssh -G`, no packet leaves the box) and a TRANSIENT
    fork failure — a timeout, an OSError, or a non-zero exit — is retried a
    bounded number of times before the host is declared unprovable. That retry
    is the fix for the 2026-08-12 regression: the pool's FIRST entry bears the
    cold-fork cost of the first `ssh` in a freshly-spawned `--once` process, and
    on a gate box at load 35 that cold fork occasionally overran the old 3 s /
    no-retry budget and returned None, dropping the 24-core mw0001-owner twin for
    the tick on nothing but a CPU spike on the DISPATCHER. A distinct, healthy
    box must survive a momentary local fork stall; an identity that is still
    unresolvable after every attempt fails closed to None (dropped), so the
    never-two-gates-per-box guarantee is untouched.

    A clean expansion that carries NO `hostname` line is not transient — it is a
    stable, well-formed refusal — so it returns None immediately rather than
    re-running an identical, non-transient command.
    """
    cached = _IDENTITY_CACHE.get(host)
    if cached is not None:
        return cached
    attempts = max(1, int(attempts))
    for attempt in range(attempts):
        proc = None
        try:
            proc = subprocess.run(["ssh", "-G", host], capture_output=True,
                                  text=True, timeout=timeout_s, check=False)
        except (OSError, subprocess.SubprocessError):
            proc = None
        if proc is not None and proc.returncode == 0:
            for line in (proc.stdout or "").splitlines():
                key, _, value = line.partition(" ")
                if key.lower() == "hostname" and value.strip():
                    identity = _normalise_identity(value)
                    _IDENTITY_CACHE[host] = identity
                    return identity
            # rc==0 with no hostname: a stable refusal, never a transient stall —
            # a retry would buy the identical answer, so do not spend one.
            return None
        # Transient failure (timeout / OSError / non-zero rc). Back off briefly
        # and retry, except after the final attempt.
        if attempt < attempts - 1:
            time.sleep(SSH_IDENTITY_RETRY_BACKOFF_S)
    return None


def collapse_to_physical_boxes(specs, *, resolve=None) -> tuple:
    """`specs` reduced to ONE ENTRY PER PHYSICAL MACHINE, in preference order.

    Three ways an entry loses its slot, all of them conservative — the cost is
    capacity (recoverable, and loudly announced), the alternative is two gates
    on one box (an outage, discovered as a false candidate defect):

      * its identity cannot be resolved at all;
      * it expands to a machine an EARLIER entry already claimed;
      * it expands back to this box, whose slot `local` already holds.

    Each host is probed exactly once per pool build. If `ssh` is unavailable
    altogether the pool empties and gating goes local-only — which is not just
    the safe answer but the correct one, since remote dispatch runs over that
    same `ssh`.
    """
    resolver = resolve or resolve_ssh_physical_id
    kept: list = []
    first_by_identity: dict = {}
    for spec in specs:
        identity = resolver(spec.host)
        if not identity:
            _config_warn(f"{spec.host!r} has no provable physical identity from a "
                         "local `ssh -G` — dropped; it cannot be shown to be a "
                         "box that is not already gating")
            continue
        identity = _normalise_identity(identity)
        if identity in _LOOPBACK_IDS:
            _config_warn(f"{spec.host!r} resolves to this very box ({identity}) — "
                         "dropped; `local` already holds that gate slot")
            continue
        first = first_by_identity.get(identity)
        if first is not None:
            _config_warn(f"{first!r} and {spec.host!r} are two names for one machine "
                         f"({identity}) — keeping {first!r}; one gate per box")
            continue
        first_by_identity[identity] = spec.host
        kept.append(spec)
    return tuple(kept)


def busy_physical_hosts(in_flight, *, specs=None, resolve=None) -> set:
    """Pool hosts occupied by the gates named in `in_flight`.

    Occupancy is recorded in gate-state files as the NAME a gate was dispatched
    under. After a config edit — a rename, an added alias — that name need not
    be the pool's current name for the same machine, and a name-equality busy
    check would then hand that box a second concurrent gate. Compare by
    resolved identity instead.

    An in-flight name that cannot be resolved marks the WHOLE pool busy: an
    unidentifiable running gate may be on any box, and deferring a tick is the
    cheap error.
    """
    resolver = resolve or resolve_ssh_physical_id
    pool = tuple(TWIN_SPECS if specs is None else specs)
    names = {str(name) for name in in_flight if name}
    if not names:
        return set()
    identities: dict = {}
    for spec in pool:
        identity = resolver(spec.host)
        if identity:
            identities.setdefault(_normalise_identity(identity), set()).add(spec.host)
    busy: set = set()
    for name in names:
        identity = resolver(name)
        if not identity:
            _config_warn(f"a gate is in flight under {name!r}, whose physical identity "
                         "is unprovable — treating every configured box as busy")
            return {spec.host for spec in pool}
        busy |= identities.get(_normalise_identity(identity), set())
    return busy


#: The pool the scheduler may actually dispatch to: declared, narrowed by
#: `THREELOOPS_ACTIVE_TWINS`, then reduced to DISTINCT PHYSICAL MACHINES.
#: `gate_loop.MAX_CONCURRENT_GATES` is `1 + len(TWIN_SPECS)`, and
#: `scripts/gate-watch` kills excess gates against that same number, so the
#: collapse must happen HERE — before either of them counts.
TWIN_SPECS: tuple = collapse_to_physical_boxes(_active_twin_specs())


def twin_spec(host: str) -> TwinSpec:
    """The registered spec for `host`. RAISES KeyError for an unknown host.

    There is deliberately no default. Handing back a plausible-looking prefix
    for an unrecognised box gates against a path that does not exist there and
    returns a workspace error that reads like a candidate defect — so callers
    get an explicit failure they must classify as an instrument fault instead.
    """
    for spec in KNOWN_TWIN_SPECS:
        if spec.host == host:
            return spec
    raise KeyError(f"no twin spec registered for host {host!r}; "
                   f"known twins: {[s.host for s in KNOWN_TWIN_SPECS]}")


#: Below this the local box is quiet enough that shipping the gate away — and
#: rsyncing the evidence store back — buys nothing. Expressed as a fraction of
#: the performance-core ceiling.
LOCAL_QUIET_FRACTION = 0.5

#: The twin is only worth using if it is materially quieter than here.
TWIN_MARGIN = 4.0


# --------------------------------------------------------------------------
# PARITY — what was MEASURED on 2026-08-08, not what was assumed.
#
#   same    M2 Ultra, 24 logical / 16 performance cores, macOS 26.6 build 25G72
#   same    gate venv interpreter: CPython 3.12.13 (the gate runs
#           {workspace}/.venv/bin/python, never the system python3)
#   same    gtimeout present at /opt/homebrew/bin/gtimeout
#   same    full gate stack, gate workspaces and the evidence signing key
#   DIFFERS node        v22.22.0 local, v26.7.0 twin
#   DIFFERS system TZ   America/New_York local, America/Los_Angeles twin
#   DIFFERS git         2.43.0 local, 2.39.5 (Apple) twin
#   DIFFERS system py3  3.12.1 local, 3.9.6 twin  (not used by the gate)
#
# The timezone divergence was tested rather than believed, and the belief was
# WRONG. `tests/test_grandfather_clock_gate.py` was expected to be the suite
# that must stay local. It is not clock-FRAGILE, it is clock-PINNED: it sets
# `CLOCK_ZONE = "America/New_York"` and calls
# `moment.astimezone(ZoneInfo(CLOCK_ZONE))`, so it never reads the host zone.
# Executed under America/New_York, America/Los_Angeles, Pacific/Midway and
# Pacific/Kiritimati — a 25-hour span that straddles the calendar day — it
# returned `54 passed, 18 deselected` every time, and returned exactly that on
# the twin as well. `tests/scheduler/` likewise: 742 passed under both zones.
#
# Two conclusions follow, and they point opposite ways:
#
#   * The named carve-out is not needed for the reason given, so encoding it as
#     a blanket "clock suites stay local" would cost throughput for nothing.
#   * The divergence is still real for any suite that does NOT pin its zone,
#     and the cheap fix is to stop the divergence existing: remote dispatch
#     pins TZ to this host's zone explicitly. A suite then cannot tell the two
#     boxes apart, whether or not it pins its own.
#
# What genuinely cannot be neutralised by an environment variable is
# HOST-STATE: a suite that reads a path outside the repo, or that depends on a
# host binary whose MAJOR version differs. That is what LOCAL_ONLY_PATTERNS is
# for, and it is deliberately short — every entry needs a measurement.
# --------------------------------------------------------------------------

#: (regex, why). Matched against the gate's target list AND its command string.
LOCAL_ONLY_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"tests/counterfeits/",
     ("the counterfeit corpus discriminates on host binaries. Measured "
      "2026-08-08: with node absent on the twin, cf-clock-engine-probe-dropped "
      "was undetectable — real code and counterfeit both returned None — and "
      "the lost discriminating power was reported as a CANDIDATE DEFECT. node "
      "is now installed but the MAJOR version still differs (22 local / 26 "
      "twin), so the corpus is not yet proven to grade identically.")),
)

#: Environment pinned on every remote dispatch so the twin cannot differ from
#: this host on anything an env var can fix. TZ is filled in at dispatch time
#: from the LOCAL host, never hard-coded.
def remote_env_pins() -> dict:
    tz = os.environ.get("TZ") or _system_tz() or "America/New_York"
    return {"TZ": tz, "LC_ALL": "C"}


def _system_tz() -> str | None:
    try:
        link = os.readlink("/etc/localtime")
    except OSError:
        return None
    m = re.search(r"zoneinfo/(.+)$", link)
    return m.group(1) if m else None


# ------------------------------------------------------------------ probes --


@dataclass
class LoadReading:
    host: str
    load1: float | None
    taken_at: float                      # time.monotonic() when the probe returned
    error: str = ""

    @property
    def age_s(self) -> float:
        return time.monotonic() - self.taken_at

    @property
    def usable(self) -> bool:
        """A reading is usable only while it is FRESH and NUMERIC.

        The staleness bound is the whole point: routing on a minutes-old
        reading is what sent work to a busy box twice on 2026-08-08.
        """
        return self.load1 is not None and self.age_s <= LOAD_READING_MAX_AGE_S

    def as_dict(self) -> dict:
        return {"host": self.host, "load1": self.load1,
                "age_s": round(self.age_s, 1), "usable": self.usable,
                "error": self.error}


def probe_local_load() -> LoadReading:
    load = read_load_1m()
    return LoadReading("local", load, time.monotonic(),
                       "" if load is not None else "os.getloadavg() unavailable")


def probe_remote_load(host: str = TWIN_HOST, timeout_s: int = SSH_PROBE_TIMEOUT_S) -> LoadReading:
    """ONE probe. Reachability and load in the same round-trip.

    `vm.loadavg` is read rather than `uptime` because its format is fixed:
    `{ 1.57 1.51 1.50 }`. Parsing `uptime` means parsing a localised,
    version-dependent sentence.
    """
    try:
        proc = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={timeout_s}",
             "-o", "StrictHostKeyChecking=accept-new", host, "sysctl -n vm.loadavg"],
            capture_output=True, text=True, timeout=timeout_s + 5, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return LoadReading(host, None, time.monotonic(), f"{type(exc).__name__}: {exc}")
    if proc.returncode != 0:
        return LoadReading(host, None, time.monotonic(),
                           f"ssh rc={proc.returncode}: {(proc.stderr or '').strip()[:160]}")
    parts = proc.stdout.replace("{", " ").replace("}", " ").split()
    for tok in parts:
        try:
            return LoadReading(host, float(tok), time.monotonic())
        except ValueError:
            continue
    return LoadReading(host, None, time.monotonic(),
                       f"unparseable vm.loadavg: {proc.stdout.strip()[:80]!r}")


# ------------------------------------------------------------------ router --


@dataclass
class HostChoice:
    host: str                             # "local" or an ssh destination
    reason: str
    local: LoadReading | None = None
    remote: LoadReading | None = None
    sensitivity: list = field(default_factory=list)
    env_pins: dict = field(default_factory=dict)
    #: Paths on the CHOSEN host. None for a local choice. Carried on the choice
    #: so a caller cannot pair host A with host B's prefix.
    workspace: str | None = None
    evidence_root: str | None = None
    #: Every twin that was probed and why it lost, so an operator reading the
    #: summary can tell "the pool was busy" from "the pool was unreachable".
    considered: list = field(default_factory=list)

    @property
    def is_remote(self) -> bool:
        return self.host != "local"

    def as_dict(self) -> dict:
        return {"host": self.host, "reason": self.reason,
                "local": self.local.as_dict() if self.local else None,
                "remote": self.remote.as_dict() if self.remote else None,
                "sensitivity": self.sensitivity,
                "env_pins": self.env_pins,
                "workspace": self.workspace,
                "evidence_root": self.evidence_root,
                "considered": self.considered}


def suite_sensitivity(targets) -> list:
    """Which local-only rules a target set trips, and why. Empty = routable."""
    blob = " ".join(str(t) for t in (targets or []))
    return [{"pattern": pat, "why": why}
            for pat, why in LOCAL_ONLY_PATTERNS if re.search(pat, blob)]


def choose_gate_host(targets=None, *, ceiling: float | None = None,
                     twin: str | None = None,
                     exclude: tuple = (),
                     probe_local=probe_local_load,
                     probe_remote=probe_remote_load) -> HostChoice:
    """Pick a host for one gate run. Probes happen HERE, at dispatch time.

    Order is deliberate: the carve-out is evaluated before any probe, so a
    local-only suite never pays for an ssh round-trip, and no load reading can
    ever talk the router into shipping a suite the twin cannot grade.

    `twin` pins the decision to ONE named host (the pre-pool behaviour, still
    used by the `--twin` CLI flag and by tests). Left unset, every twin in
    TWIN_SPECS that is not in `exclude` is considered and the quietest usable
    one wins.

    ONE PROBE PER HOST, still never a retry storm. The original rule was
    written when the pool had exactly one member, so "one probe" and "one probe
    per host" were the same sentence; with two twins they are not, and the rule
    that carries the meaning is the second. A host that fails its single probe
    is passed over for this dispatch and is NOT retried — the storm this
    forbids is asking the SAME box twice, not asking a different box once.
    """
    if ceiling is None:
        ceiling = float(perf_core_count() or os.cpu_count() or 8)

    sens = suite_sensitivity(targets)
    if sens:
        return HostChoice("local",
                          "suite is host-state sensitive: "
                          + "; ".join(s["pattern"] for s in sens),
                          sensitivity=sens)

    local = probe_local()
    if not local.usable:
        # Unknown local load is not idle local load. Stay put: dispatching
        # remotely on an unmeasurable premise is the favourable-absence defect.
        return HostChoice("local", f"local load unusable ({local.error or 'stale'}) — "
                                   "staying local rather than routing on an unknown",
                          local=local)

    if local.load1 <= ceiling * LOCAL_QUIET_FRACTION:
        return HostChoice("local",
                          f"local is quiet ({local.load1:.2f} <= "
                          f"{ceiling * LOCAL_QUIET_FRACTION:.1f}); remote costs an "
                          "evidence rsync for nothing",
                          local=local)

    if twin is not None:
        # An unregistered host gets NO synthesised spec. Handing it the first
        # twin's prefix is the exact host/path mismatch this registry exists to
        # make impossible: MW0002's gate lives under /Users/cloud, so guessing
        # /Users/youruser would gate against a path that does not exist and
        # return a workspace error shaped like a candidate defect. Staying local
        # is the fail-closed answer — cross-lineage review 2026-08-10, blocker 3,
        # which caught this contradicting twin_spec's own docstring.
        try:
            pool = [twin_spec(twin)]
        except KeyError as exc:
            return HostChoice("local",
                              f"unregistered twin {twin!r} (instrument, not a candidate "
                              f"fact): {exc}. Staying local rather than guessing a prefix.",
                              local=local)
    else:
        pool = [s for s in TWIN_SPECS if s.host not in tuple(exclude)]

    if not pool:
        return HostChoice("local",
                          "every twin is already gating — no free remote slot",
                          local=local,
                          considered=[{"host": h, "why": "already in flight"}
                                      for h in tuple(exclude)])

    # Probe each candidate ONCE, then decide. Losers are recorded with a reason
    # so an unreachable twin is visibly an instrument fact, not an absence.
    best = None
    best_reading = None
    best_headroom = float('-inf')
    considered = []
    for spec in pool:
        reading = probe_remote(spec.host)
        if not reading.usable:
            considered.append({"host": spec.host,
                               "why": f"unreachable/unreadable ({reading.error or 'stale'})"
                                      " — one probe, no retry"})
            continue
        # Rank by HEADROOM, not by raw load. The twins are not the same size —
        # MW0002 has 12 performance cores to MW0001's 16 — so comparing load1
        # directly picks the smaller box exactly when it has LESS room (5/12 is
        # tighter than 6/16 despite the lower number). The gate then runs its
        # 8-wide ladder into contention and the resulting failure is charged to
        # the candidate. Cross-lineage review 2026-08-10, finding 4.
        headroom = spec.perf_cores - reading.load1
        # m2 (frozen schema): every probed-and-usable twin is `admitted` here —
        # there is deliberately NO absolute per-twin floor inside this
        # function (I2', which replaces v1 F-A1's one-sided absolute floor:
        # that floor picked the WORSE host in the reviewer's own repro,
        # because it was enforced on twins but not on local — see
        # $RUN/.fusion/repro/B3-admission-floor-picks-the-worse-host.py). The
        # ladder-floor admission bar lives in `pick_twin` (I7), the daemon's
        # actual claim function; this router only ever compares COMPARATIVE
        # headroom, below.
        considered.append({"host": spec.host, "load1": reading.load1,
                           "perf_cores": spec.perf_cores,
                           "headroom": round(headroom, 2),
                           "admitted": True, "reason": ""})
        # Strict improvement keeps the preference order meaningful: an exact
        # tie leaves the earlier (proven) twin in front rather than flapping.
        if best_reading is None or headroom > best_headroom:
            best, best_reading, best_headroom = spec, reading, headroom

    if best is None:
        names = ", ".join(s.host for s in pool)
        return HostChoice("local",
                          f"no usable twin ({names}) — one probe each, no retry storm",
                          local=local, considered=considered)

    # COMPARATIVE headroom (I2'), not a raw-load or absolute-floor test: the
    # twin must have MATERIALLY more room than local, in the same headroom
    # units both sides are already measured in, or local wins. Comparing
    # raw load1 (the pre-I2' rule) mixed units across boxes of different
    # size and had no local-side floor at all, so it could route to a twin
    # with STRICTLY LESS headroom than local — the reviewer's own repro
    # (local 16/16 cores, only mw0002 active at 11/12) is exactly that case.
    local_headroom = ceiling - local.load1
    if best_headroom < local_headroom + TWIN_MARGIN:
        return HostChoice("local",
                          f"twin is not materially quieter (twin headroom "
                          f"{best_headroom:.2f} vs local headroom {local_headroom:.2f}, "
                          f"margin {TWIN_MARGIN:g})",
                          local=local, remote=best_reading, considered=considered)

    return HostChoice(best.host,
                      f"local headroom {local_headroom:.2f} trails twin headroom "
                      f"{best_headroom:.2f} by at least margin {TWIN_MARGIN:g} — "
                      "routing to the clean room",
                      local=local, remote=best_reading, env_pins=remote_env_pins(),
                      workspace=best.workspace, evidence_root=best.evidence_root,
                      considered=considered)


# ---------------------------------------------------------------- dispatch --


#: Everything the twin must be able to prove BEFORE a gate is sent to it. Each
#: entry is (label, remote shell test). A gate is ~12-90 minutes; discovering a
#: missing workspace at minute 40 wastes the whole window, and — worse — comes
#: back looking like a verdict about the candidate.
READINESS_CHECKS: tuple[tuple[str, str], ...] = (
    ("gate workspace exists", "test -d {workspace}"),
    ("gate workspace is a clean checkout",
     "cd {workspace} && test -z \"$(git status --porcelain=v1 --untracked-files=all)\""),
    ("gate script present", "test -f {workspace}/scripts/merge-gate.sh"),
    ("minter travels with the judge", "test -f {workspace}/scripts/mint-merge-candidate.py"),
    ("gate interpreter present", "test -x {workspace}/.venv/bin/python"),
    ("evidence signing key present", "test -f {evidence_root}/signing.key"),
    ("gtimeout on PATH", "command -v gtimeout >/dev/null"),
    ("node on PATH", "command -v node >/dev/null"),
    ("zoneinfo resolves America/New_York",
     "{workspace}/.venv/bin/python -c 'from zoneinfo import ZoneInfo; ZoneInfo(\"America/New_York\")'"),
    ("candidate ref reachable", "cd {workspace} && git rev-parse --verify {candidate}^{{commit}}"),
)


def preflight_remote(host: str, *, workspace: str, evidence_root: str,
                     candidate: str, expected_base: str | None = None,
                     timeout_s: int = 60) -> dict:
    """Prove the twin can grade this candidate. One ssh round-trip, not ten.

    Returns {"ready": bool, "failed": [...], "checked": n}. A failure here is an
    INSTRUMENT fact and must be reported as one — never as a candidate defect.
    That inversion has already happened on this twin: a missing `node` made a
    counterfeit undetectable and the lost discriminating power was filed against
    the candidate's code.
    """
    # Quote EVERY interpolated value. `candidate` is a branch/ref name that can
    # carry shell metacharacters and it is shipped as the literal argument to
    # `ssh host <script>`, so an unquoted `;` or backtick would execute on the
    # twin. remote_gate_command already quotes the same value; this path must
    # match it. Quoting a path like '/ws' inside '/ws/scripts/x' is still valid
    # shell (adjacent quoted+bareword concatenate), so every check keeps working.
    checks = list(READINESS_CHECKS)
    if expected_base:
        # The signed §0 receipt is bound to the merge-base measured on the
        # dispatching host. A twin whose HEAD is anywhere else recomputes a
        # different merge-base and refuses the receipt 12 minutes in — assert
        # the base pin actually took, in the ~1s preflight instead.
        checks.append((
            "gate workspace HEAD is pinned to the receipt merge-base",
            'cd {workspace} && test "$(git rev-parse HEAD)" = {expected_base}'))
    script_parts = []
    for label, test in checks:
        body = test.format(workspace=_shquote(workspace),
                           evidence_root=_shquote(evidence_root),
                           candidate=_shquote(candidate),
                           expected_base=_shquote(expected_base or ""))
        script_parts.append(f"if {body} >/dev/null 2>&1; then echo 'OK\t{label}'; "
                            f"else echo 'FAIL\t{label}'; fi")
    script = "; ".join(script_parts)
    try:
        proc = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={SSH_PROBE_TIMEOUT_S}",
             host, script],
            capture_output=True, text=True, timeout=timeout_s, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ready": False, "failed": [f"preflight unrunnable: {type(exc).__name__}: {exc}"],
                "checked": 0}
    failed = [ln.split("\t", 1)[1] for ln in proc.stdout.splitlines()
              if ln.startswith("FAIL")]
    checked = len([ln for ln in proc.stdout.splitlines() if "\t" in ln])
    if checked != len(checks):
        failed.append(f"preflight returned {checked}/{len(checks)} results — "
                      "an incomplete preflight is a failed preflight")
    return {"ready": not failed, "failed": failed, "checked": checked}


def remote_gate_command(host: str, *, workspace: str, candidate: str, receipt: str,
                        evidence_root: str, env_pins: dict | None = None,
                        extra_env: dict | None = None,
                        bound_tests: list | None = None,
                        timeout_s: int | None = None) -> list:
    """Build the argv for a remote gate run. Pure — no I/O, so it is testable.

    The env is pinned explicitly rather than inherited. `TZ` is the reason this
    function exists at all: the twin's system zone is America/Los_Angeles and
    this host's is America/New_York, and rather than carve out every suite that
    might read the host zone, the divergence is simply removed at dispatch.

    `bound_tests` mirrors the local gate's closure bindings (gate_loop.
    local_gate_command): one REPEATED `--bound-test` flag per binding, so a train
    graded on the twin closes exactly the findings it would have closed locally.
    Each id is shell-quoted like every other value here — a pytest node id
    routinely carries `[param]` brackets and spaces, which an unquoted assembly
    would split into flags of its own.

    `timeout_s`, when set, wraps the inner command in `gtimeout -k 30 <N>` so the
    TWIN bounds itself rather than relying solely on the dispatcher's own
    timeout — `gtimeout` is already proven present by `READINESS_CHECKS`
    ("gtimeout on PATH"). Left unset (the default) the argv is byte-identical to
    the pre-I6 shape; nothing that already calls this function is affected.
    """
    env = dict(env_pins or remote_env_pins())
    env.update({
        "MERGE_GATE_PINNED": "1",
        "OMNIAGENTOS_GATE_WORKSPACE": workspace,
        "MERGE_GATE_EVIDENCE_ROOT": evidence_root,
        "MERGE_GATE_PY": f"{workspace}/.venv/bin/python",
    })
    env.update(extra_env or {})
    assign = " ".join(f"{k}={_shquote(v)}" for k, v in sorted(env.items()))
    bound = f"gtimeout -k 30 {int(timeout_s)} " if timeout_s is not None else ""
    inner = (f"cd {_shquote(workspace)} && env {assign} "
             f"{bound}bash {_shquote(workspace)}/scripts/merge-gate.sh "
             f"--candidate {_shquote(candidate)} --emit-receipt {_shquote(receipt)}")
    for node in bound_tests or ():
        inner += f" --bound-test {_shquote(node)}"
    return ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={SSH_PROBE_TIMEOUT_S}",
            host, inner]


def pick_twin(exclude: set = frozenset(), probe=probe_remote_load,
              readings: dict | None = None):
    """The DAEMON's claim function. Production never calls `choose_gate_host` —
    that is the one-shot CLI/dry-run router; the daemon that actually owns twin
    slots claims one here.

    Probes every ACTIVE twin (`TWIN_SPECS`) not in `exclude`, ONCE each, same
    no-retry doctrine as `choose_gate_host`. A twin is admissible iff its
    reading is usable AND its headroom clears the ladder floor:

        spec.perf_cores - reading.load1 >= min(effective_ladder_workers(), spec.perf_cores)

    The `min(...)` clamp matters: `merge-gate.sh` itself clamps the ladder width
    to the host's performance-core count, so a small box is never held to a
    floor wider than the ladder it will actually run — demanding 8-wide headroom
    from a 4-core box would make it permanently inadmissible for a ladder it can
    perfectly well run at its own (narrower) width.

    Returns `(spec_or_None, considered)`. The FIRST admissible twin in
    TWIN_SPECS preference order is returned (every candidate is still probed,
    so `considered` is complete for the caller's defer log); `None` means the
    caller must DEFER the gate rather than dispatch it to an inadmissible box.

    `considered` entries follow the frozen schema (m2): a probed-and-usable
    twin is `{host, load1, perf_cores, headroom, admitted, reason}`; an
    unreachable/unusable one is the distinct `{host, why}` shape.
    """
    chosen = None
    considered = []
    for spec in TWIN_SPECS:
        if spec.host in exclude:
            continue
        reading = readings.get(spec.host) if readings is not None else None
        if reading is None:
            reading = probe(spec.host)
        if not reading.usable:
            considered.append({"host": spec.host,
                               "why": f"unreachable/unreadable ({reading.error or 'stale'})"
                                      " — one probe, no retry"})
            continue
        headroom = spec.perf_cores - reading.load1
        workers = effective_ladder_workers()
        floor = min(workers, spec.perf_cores)
        admitted = headroom >= floor
        reason = "" if admitted else (
            f"headroom {headroom:.2f} < ladder floor {floor:g} "
            f"(min(effective_ladder_workers={workers}, perf_cores={spec.perf_cores}))")
        considered.append({"host": spec.host, "load1": reading.load1,
                           "perf_cores": spec.perf_cores, "headroom": round(headroom, 2),
                           "admitted": admitted, "reason": reason})
        if admitted and chosen is None:
            chosen = spec
    return chosen, considered


def _shquote(value) -> str:
    import shlex
    return shlex.quote(str(value))


def main(argv: list | None = None) -> int:
    """CLI entry point. `argv=None` reads `sys.argv` as usual; an explicit list
    (as tests pass) makes this testable without a subprocess.

    `--twin` defaults to None (m1): pool routing is exercisable straight from
    the CLI instead of always pinning to `TWIN_HOST`. `--workspace` /
    `--evidence-root` default to None too and resolve from the NAMED twin's
    OWN registered spec (`twin_spec`) rather than from MW0001-shaped literals —
    the bug this closes (F-A3) is `--twin mw0002 --preflight` silently probing
    MW0002 with MW0001's paths unless an operator remembered to pass both path
    flags by hand. An explicit `--workspace`/`--evidence-root` still wins over
    the resolved default.

    An unknown `--twin` is a hard, fast failure: nonzero exit, an
    instrument-shaped stderr line, NO preflight, and — critically — NO guessed
    path. There is nothing safe to fall back to; guessing a prefix is exactly
    the class of mistake `twin_spec` exists to make impossible.
    """
    import argparse
    import json
    import sys

    ap = argparse.ArgumentParser(description="Decide where a gate should run.")
    ap.add_argument("--target", action="append", default=[],
                    help="a gate target/suite path (repeatable)")
    ap.add_argument("--twin", default=None,
                    help="pin to one twin by host name; unset lets the pool decide")
    ap.add_argument("--preflight", metavar="CANDIDATE",
                    help="also prove the twin could grade this candidate ref")
    ap.add_argument("--workspace", default=None,
                    help="override the twin workspace path (default: the "
                         "named twin's own registered spec)")
    ap.add_argument("--evidence-root", default=None,
                    help="override the twin evidence-root path (default: the "
                         "named twin's own registered spec)")
    args = ap.parse_args(argv)

    resolved_spec = None
    if args.twin is not None:
        try:
            resolved_spec = twin_spec(args.twin)
        except KeyError as exc:
            print(f"gate-host CONFIG: unknown --twin {args.twin!r}: {exc}",
                 file=sys.stderr)
            return 2

    choice = choose_gate_host(args.target, twin=args.twin)
    out = choice.as_dict()

    if args.preflight:
        # An explicit --twin is honoured for --preflight even when the router
        # chose local for it (operators use --preflight to diagnose a refused
        # twin) — but ALWAYS against that twin's own resolved paths, never the
        # other twin's and never a guessed default. With no --twin at all,
        # there is nothing named to diagnose unless the router itself picked a
        # remote host, in which case that host's already-resolved paths (on
        # `choice`) are used.
        preflight_host = args.twin if resolved_spec is not None else (
            choice.host if choice.is_remote else None)
        if preflight_host is None:
            out["preflight"] = {
                "ready": False,
                "failed": ["no twin to preflight: --twin was not given and the "
                          "router chose local"],
                "checked": 0,
            }
        else:
            spec = resolved_spec or twin_spec(preflight_host)
            workspace = args.workspace if args.workspace is not None else spec.workspace
            evidence_root = (args.evidence_root if args.evidence_root is not None
                             else spec.evidence_root)
            out["preflight"] = preflight_remote(
                preflight_host, workspace=workspace, evidence_root=evidence_root,
                candidate=args.preflight)

    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
