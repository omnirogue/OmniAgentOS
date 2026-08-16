"""The pool worker: claim → clone → run → verify → report (SPEC §7, Phase 1).

One process, one slot. ``--slots N`` forks N of these and supervises them; the
parent does nothing but respawn and forward SIGTERM, so a crashed slot never
takes the box out of the pool.

WHY A UNIT IS RECLAIMED ON TTL EXPIRY, AND A WORKTREE IS NOT
------------------------------------------------------------
``lane-pool.sh`` (a scratch path, deliberately quoted here rather than cited so
the reasoning survives its garbage collection) states the opposite rule, and is
right about its own resource:

    "a lease recorded by a foreign host is never reclaimed, no matter its age.
     We have no local way to probe whether that pid is alive on the OTHER
     machine, and assuming 'can't see it locally' means 'dead' is exactly the
     fail-open bug being fixed here."

That is correct for a WORKTREE — a shared mutable directory on one filesystem
where two writers corrupt each other. It is WRONG for a work unit, and applying
it to a multi-machine pool would deadlock the pool the first time a Mac loses
power: nothing on this estate can prove a foreign pid is dead, so no lease would
ever be reclaimed and the queue would stall behind a machine that is never
coming back.

A unit is safe to reclaim on TTL because a unit is RE-EXECUTABLE BY
RECONSTRUCTION: it is fully described by (repo_url, base_sha, brief,
agent_profile, owned_paths) and every execution starts from a fresh checkout at
``base_sha``. The danger is not "two machines run it" — it is "two machines both
record a result", and that is closed by the fencing generation and by
``UNIQUE (unit_id, attempt)``, not by proving death.

**Do not "fix" this back to fail-closed.** It would stall the pool, and the
guarantee you would think you were adding is already held by the fence.

OTHER LOAD-BEARING CHOICES
--------------------------
* **Branch on the PROCESS exit code, never on a parsed receipt field** (§4.3).
  The estate has a recorded case of a receipt saying ``exit_code: 4`` while the
  process exited 2. For exit 2 the wrapped gate's printed class may REFINE which
  could-not-run class applies; it can never move a run across the 0/1/2 line.
* **The refusal ledger is always consulted before the GATE, and before the CLONE
  when that is provably the same key.** For a unit with no agent turn the graded
  tree is the pinned tree, so the key comes straight from the bare mirror
  (``fingerprint.input_key(..., pinned_at=...)``) and an already-refused input is
  refused in ~0.2 s having spent nothing at all. For a unit WITH an agent turn the
  key is taken after the agent has run, because §4.1 hashes the tree the gate
  actually grades: fingerprinting the pre-agent tree would give every attempt of
  that unit one key, so attempt 2 would be refused before the agent could produce
  anything different — and §4.6's lineage swap would never get to run.
* **The worker owns the ledger WRITE for a unit attempt**, and invokes
  ``scripts/accurate-gate.py --ledger off``. Only the worker knows the FINAL
  class: the §4.4b post-hoc classifier can override what the gate concluded, and
  a ledger row that records the pre-override class would send the next agent to
  debug the wrong thing. Standalone gate invocations keep their own bookkeeping.
* **One blocking bounded wait per child** (``start_new_session=True`` so the whole
  process tree can be killed by group). No 30 s poll chunks — the heartbeat
  thread is what keeps the lease alive while we block.
* **The claim cadence scales with this box's headroom** (``idle_poll_delay_s``,
  ROUTING-DECISIONS §3): above the load ceiling the worker claims nothing at
  all, and below it an idle box re-asks every 5 s while a nearly saturated one
  waits up to 60 s. That is how "route to the machine with the most available
  compute" is implemented WITHOUT a server-side placer — see the constant's
  docstring for why pull-based polling is the version that stays correct.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from omniagentos.path_containment import inode_relative_parts
from omniagentos.workqueue import alert as alert_transport
from omniagentos.workqueue import classify as classifier
from omniagentos.workqueue.fingerprint import InputKeyError, input_key
from omniagentos.workqueue.schema import (
    HEARTBEAT_INTERVAL_S,
    LeaseLost,
    new_boot_nonce,
    utc_now_iso,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = REPO_ROOT / "configs" / "workqueue.yaml"
GATE_SCRIPT = REPO_ROOT / "scripts" / "accurate-gate.py"

#: How long the capacity gate sleeps when the box is too loaded to claim (§ Lane B).
CAPACITY_SLEEP_S = 15
#: Idle poll interval when the queue had nothing for us — the floor of the
#: headroom-scaled range below, and the fixed delay after a claim ERROR (a
#: transport failure says nothing about this box's load).
IDLE_SLEEP_S = 5

#: Headroom-scaled claim cadence (ROUTING-DECISIONS §3, "route to the machine
#: with the MOST available compute"). ``load_ratio = load1 / (ceiling_fraction *
#: ncpu)`` is the same fraction the capacity gate thresholds at 1.0; below that
#: line the worker still claims, but an idle box polls every 5 s while a nearly
#: saturated one waits up to 60 s.
#:
#: WHY THIS IS STATISTICAL ROUTING AND NOT A PLACEMENT DECISION: claiming is
#: PULL-based, which is what makes the single-writer DB plus the fencing
#: generation sufficient — the machine that wants the work takes it under
#: BEGIN IMMEDIATE, and no third party has to be right about anything. A
#: server-side placer would have to choose a target from telemetry that is up to
#: one beat (30 s) stale, and a box that started three units in that window
#: would keep being chosen. Poll frequency needs no such correctness: whoever
#: asks first wins, idle boxes ask 12× more often, and being wrong about the
#: load costs a claim attempt, never a misplaced unit.
IDLE_POLL_MIN_S = 5.0
IDLE_POLL_MAX_S = 60.0
#: Below this ratio a box counts as genuinely idle and polls at the floor.
IDLE_POLL_IDLE_RATIO = 0.3
#: Machine telemetry (ncpu / perf_cores / ceiling_fraction) is refreshed hourly.
MACHINE_REFRESH_S = 3600
#: Grace between SIGTERM and SIGKILL when killing a child process GROUP.
KILL_GRACE_S = 5
#: Backoff before the supervisor respawns a slot, so a crash-looping child cannot
#: spin the box (quota/auth errors are terminal, not something to retry hot).
RESPAWN_BACKOFF_S = 5


# --------------------------------------------------------------------- config --
def load_config(path: str | Path | None = None) -> dict[str, Any]:
    """Read configs/workqueue.yaml fresh. A missing/broken config is fatal.

    Defaulting silently would let a worker run with an escalation table it does
    not have (favourable absence); the operator must see the failure.
    """
    import yaml

    cfg_path = Path(path or os.environ.get("WQ_CONFIG") or DEFAULT_CONFIG)
    data = yaml.safe_load(cfg_path.read_text()) or {}
    if not isinstance(data, dict):
        raise RuntimeError(f"{cfg_path}: expected a mapping at the top level")
    return data


def resolve_reserved_for(raw: str | None) -> str | None:
    """Fail CLOSED on a broken reservation (the operator's threat model: resource hygiene).

    A reserved worker claims ONLY its declared owner's OWN submissions — to keep
    other people's pool work off a machine reserved for one person under
    COOPERATIVE use. This is NOT a hard security boundary: ``submitted_by`` is
    self-declared under the single shared pool token, so a token-holder who
    deliberately submits AS the owner CAN still land work on the box. It prevents
    accidental contention, not deliberate forging (a hard boundary needs
    per-submitter auth tokens, out of scope).

    * ``raw is None`` (``--reserved-for`` NOT passed) ⇒ ``None``: a normal
      unrestricted worker. The operator explicitly chose not to reserve — a
      VISIBLE choice (the flag is simply absent), not a silent default.
    * ``raw`` given but empty/whitespace ⇒ the worker REFUSES TO START. A BROKEN
      reserved config must claim NOTHING rather than silently become an
      unrestricted worker (which is the fail-OPEN bug this redesign removes).
    """
    if raw is None:
        return None
    owner = raw.strip()
    if not owner:
        print(
            "wq-worker: FATAL — --reserved-for was given but is empty/whitespace. A "
            "reserved worker REFUSES TO START rather than silently claim everyone's "
            "work (fail closed). Pass a real owner (e.g. --reserved-for owner), or omit "
            "the flag entirely to run an unrestricted worker on purpose.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return owner


def wq_home() -> Path:
    home = Path(os.environ.get("WQ_HOME") or (Path.home() / "wq"))
    home.mkdir(parents=True, exist_ok=True)
    return home


def open_queue(server: str | None = None, db: str | None = None) -> Any:
    """Return an ``HttpQueueClient`` or a ``WorkQueueStore`` — identical surface.

    Imported by FULL MODULE PATH on purpose: ``omniagentos/workqueue/__init__.py``
    stays empty so the two build lanes cannot collide in it.
    """
    if server:
        from omniagentos.workqueue.client import HttpQueueClient

        return HttpQueueClient(server)
    if not db:
        raise SystemExit("worker: one of --server URL or --db PATH is required")
    from omniagentos.workqueue.store import WorkQueueStore

    return WorkQueueStore(db)


# ------------------------------------------------------------------ telemetry --
def _sh(
    args: list[str], timeout: float = 20.0, cwd: str | Path | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout,
        cwd=str(cwd) if cwd else None,
        check=False,
    )


def detect_cores() -> tuple[int, int]:
    """(ncpu, perf_cores). On Linux perf_cores == ncpu; on darwin ask sysctl."""
    ncpu = os.cpu_count() or 1
    perf = ncpu
    if sys.platform == "darwin":
        try:
            out = _sh(["sysctl", "-n", "hw.perflevel0.logicalcpu"], timeout=5).stdout.strip()
            if out.isdigit() and int(out) > 0:
                perf = int(out)
        except (OSError, subprocess.SubprocessError):
            pass
    return ncpu, perf


def detect_mem_gb() -> float | None:
    try:
        if sys.platform == "darwin":
            out = _sh(["sysctl", "-n", "hw.memsize"], timeout=5).stdout.strip()
            return round(int(out) / 1024**3, 1) if out.isdigit() else None
        meminfo = Path("/proc/meminfo").read_text()
        m = re.search(r"^MemTotal:\s+(\d+) kB", meminfo, re.MULTILINE)
        return round(int(m.group(1)) / 1024**2, 1) if m else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def mem_free_gb() -> float | None:
    """Best-effort free memory. Telemetry NEVER blocks or breaks a dispatch."""
    try:
        if sys.platform == "darwin":
            out = _sh(["vm_stat"], timeout=5).stdout
            page = re.search(r"page size of (\d+) bytes", out)
            free = re.search(r"Pages free:\s+(\d+)", out)
            spec = re.search(r"Pages speculative:\s+(\d+)", out)
            if not (page and free):
                return None
            pages = int(free.group(1)) + (int(spec.group(1)) if spec else 0)
            return round(pages * int(page.group(1)) / 1024**3, 2)
        meminfo = Path("/proc/meminfo").read_text()
        m = re.search(r"^MemAvailable:\s+(\d+) kB", meminfo, re.MULTILINE)
        return round(int(m.group(1)) / 1024**2, 2) if m else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def load1() -> float | None:
    try:
        return round(os.getloadavg()[0], 2)
    except OSError:  # pragma: no cover - getloadavg is unavailable on some kernels
        return None


def load5() -> float | None:
    try:
        return round(os.getloadavg()[1], 2)
    except OSError:  # pragma: no cover
        return None


def idle_poll_delay_s(load_ratio: float | None) -> float:
    """Seconds to wait before the next claim, from the box's headroom.

    Flat ``IDLE_POLL_MIN_S`` up to ``IDLE_POLL_IDLE_RATIO``, then linear to
    ``IDLE_POLL_MAX_S`` at ratio 1.0 (the capacity ceiling), and pinned at the
    maximum above it. Monotonic non-decreasing by construction — a busier box
    must never poll faster than an idler one, which is the whole mechanism.

    An unknown ratio (no ``getloadavg``, no cores) polls at the floor: telemetry
    that could not be measured must not throttle a machine that may be free.
    """
    if load_ratio is None:
        return IDLE_POLL_MIN_S
    if load_ratio <= IDLE_POLL_IDLE_RATIO:
        return IDLE_POLL_MIN_S
    if load_ratio >= 1.0:
        return IDLE_POLL_MAX_S
    span = (load_ratio - IDLE_POLL_IDLE_RATIO) / (1.0 - IDLE_POLL_IDLE_RATIO)
    return IDLE_POLL_MIN_S + span * (IDLE_POLL_MAX_S - IDLE_POLL_MIN_S)


def default_machine_id() -> str:
    if sys.platform == "darwin":
        out = _sh(["scutil", "--get", "LocalHostName"], timeout=5)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    out = _sh(["hostname", "-s"], timeout=5)
    return out.stdout.strip() or "unknown-machine"


# ------------------------------------------------------------------ scope glob --
def _segment_match(path: str, pattern: str) -> bool:
    """Match a repo-relative path against ONE glob, ``**`` crossing separators.

    Deliberately stricter than ``fnmatch`` alone: a bare ``*`` must not cross a
    ``/``, or ``omniagentos/*`` would silently authorise the whole subtree and
    the owned_paths contract would stop meaning anything.
    """
    if pattern in ("", "."):
        return False
    if pattern.endswith("/"):
        pattern += "**"
    p_parts = pattern.split("/")
    t_parts = path.split("/")

    def walk(pi: int, ti: int) -> bool:
        while pi < len(p_parts):
            if p_parts[pi] == "**":
                if pi + 1 == len(p_parts):
                    return True
                for skip in range(ti, len(t_parts) + 1):
                    if walk(pi + 1, skip):
                        return True
                return False
            if ti >= len(t_parts):
                return False
            if not fnmatch.fnmatchcase(t_parts[ti], p_parts[pi]):
                return False
            pi += 1
            ti += 1
        return ti == len(t_parts)

    return walk(0, 0)


def path_in_scope(path: str, owned: list[str]) -> bool:
    return any(_segment_match(path, pattern) for pattern in owned)


# ------------------------------------------------------------------- results ----
@dataclass
class UnitResult:
    outcome: str
    exit_code: int | None = None
    gate_exit_code: int | None = None
    retryable: int | None = None
    remedy: str | None = None
    input_key: str | None = None
    head_sha: str | None = None
    result_branch: str | None = None
    result_sha: str | None = None
    log_path: str | None = None


class WorkerFault(Exception):
    """A step failed in a way that already knows its class and remedy."""

    def __init__(self, outcome: str, remedy: str, retryable: int = 1) -> None:
        super().__init__(remedy)
        self.outcome = outcome
        self.remedy = remedy
        self.retryable = retryable


# ----------------------------------------------------------------- heartbeat ----
class _Heartbeat:
    """Fenced lease renewal on its own thread; LeaseLost kills the child tree.

    The renewal MUST run while the worker is blocked in one long ``wait()`` —
    that is what lets the worker avoid a poll loop without the lease going
    stale.
    """

    def __init__(
        self, queue: Any, unit_id: str, owner: str, generation: int, interval: int
    ) -> None:
        self._q = queue
        self._unit_id = unit_id
        self._owner = owner
        self._generation = generation
        self._interval = max(1, interval)
        self._stop = threading.Event()
        self.lease_lost = threading.Event()
        self._lock = threading.Lock()
        self._pgid: int | None = None
        self._thread = threading.Thread(target=self._run, name=f"wq-beat-{unit_id}", daemon=True)

    def track(self, pgid: int | None) -> None:
        with self._lock:
            self._pgid = pgid

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=self._interval + 5)

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self._q.heartbeat(self._unit_id, self._owner, self._generation)
            except LeaseLost:
                self.lease_lost.set()
                self._kill_tree()
                return
            except Exception as exc:  # transport hiccup: keep beating, never crash
                print(f"wq-worker: heartbeat error {type(exc).__name__}: {exc}", file=sys.stderr)

    def _kill_tree(self) -> None:
        with self._lock:
            pgid = self._pgid
        if pgid is None:
            return
        kill_process_group(pgid)


def kill_process_group(pgid: int) -> None:
    """SIGTERM the group, then SIGKILL what is left. Never raises."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            return
        if sig is signal.SIGTERM:
            time.sleep(KILL_GRACE_S)


# -------------------------------------------------------------------- worker ----
@dataclass
class MachineInfo:
    ncpu: int
    perf_cores: int
    ceiling_fraction: float
    max_concurrent: int = 1
    labels: list[str] = field(default_factory=list)
    fetched_at: float = 0.0


class Worker:
    def __init__(
        self,
        queue: Any,
        machine_id: str,
        *,
        config: dict[str, Any] | None = None,
        labels: list[str] | None = None,
        home: Path | None = None,
        reserved_for: str | None = None,
    ) -> None:
        self.q = queue
        self.machine_id = machine_id
        self.cfg = config if config is not None else load_config()
        self.home = home or wq_home()
        # WORKER-DECLARED resource hygiene: when set, this worker claims ONLY this
        # owner's units (store.claim adds `AND submitted_by = ?`). None ⇒ normal
        # unrestricted worker. A blank/invalid value never reaches here — main()
        # fails the worker CLOSED at startup via resolve_reserved_for().
        self.reserved_for = reserved_for
        self.worker_id = f"{machine_id}:{os.getpid()}:{new_boot_nonce()}"
        self.owner = f"{machine_id}:{self.worker_id}"
        self.labels = list(labels or [])
        self.stop = threading.Event()
        self.drain = False
        self.current_unit_id: str | None = None
        self._machine: MachineInfo | None = None
        self._last_beat = 0.0
        lease = self.cfg.get("lease") or {}
        self.beat_interval = int(lease.get("heartbeat_s", HEARTBEAT_INTERVAL_S))
        caps = self.cfg.get("caps") or {}
        self.storm_cap = int(caps.get("refusal_storm", 5))
        # Who sends the alerts this worker's writes authorise. Against a LOCAL
        # store, this process is the only one holding the payload, so it must.
        # Over HTTP the SERVER already sends every alert it authorised
        # (server.py: the result and refusal routes) and returns the payload only
        # so the caller can log it (client.py) — announcing it a second time here
        # would put two notifications on one park and break the 1:1 ratio §6
        # reads to detect a park nobody was told about.
        self.owns_alert_transport = not getattr(queue, "base_url", None)

    # -- machine row -----------------------------------------------------------
    def machine(self, *, force: bool = False) -> MachineInfo:
        now = time.time()
        if self._machine and not force and (now - self._machine.fetched_at) < MACHINE_REFRESH_S:
            return self._machine
        ncpu, perf = detect_cores()
        ceiling = float((self.cfg.get("capacity") or {}).get("ceiling_fraction_default", 0.75))
        info = MachineInfo(ncpu=ncpu, perf_cores=perf, ceiling_fraction=ceiling, fetched_at=now)
        try:
            rows = self.q.list_machines()
        except Exception as exc:  # telemetry must never block a dispatch
            print(
                f"wq-worker: list_machines failed ({exc}); using local detection", file=sys.stderr
            )
            rows = []
        row = next((r for r in rows if r.get("machine_id") == self.machine_id), None)
        if row is None:
            # Phase-1 acceptance runs workers on the primary without enroll.sh, so a
            # worker self-enrolls rather than refusing to start. Enrollment stays the
            # authority for labels/capacity the moment enroll.sh has run.
            self._self_enroll(ncpu, perf)
        else:
            info.ncpu = int(row.get("ncpu") or ncpu)
            info.perf_cores = int(row.get("perf_cores") or perf)
            info.ceiling_fraction = float(row.get("ceiling_fraction") or ceiling)
            info.max_concurrent = int(row.get("max_concurrent") or 1)
            info.labels = _as_list(row.get("labels"))
            if not self.labels:
                self.labels = list(info.labels)
        self._machine = info
        return info

    def _self_enroll(self, ncpu: int, perf: int) -> None:
        payload = {
            "machine_id": self.machine_id,
            "hostname": _sh(["hostname"], timeout=5).stdout.strip() or self.machine_id,
            "os": "darwin" if sys.platform == "darwin" else "linux",
            "labels": self.labels,
            "max_concurrent": 1,
            "ncpu": ncpu,
            "perf_cores": perf,
            "mem_gb": detect_mem_gb(),
            "ceiling_fraction": float(
                (self.cfg.get("capacity") or {}).get("ceiling_fraction_default", 0.75)
            ),
            "agent_version": _git_head(REPO_ROOT),
            "enrolled_at": utc_now_iso(),
        }
        try:
            self.q.enroll_machine(payload)
        except Exception as exc:
            print(f"wq-worker: self-enroll failed: {exc}", file=sys.stderr)

    # -- beats -----------------------------------------------------------------
    def beat(self, *, force: bool = False) -> None:
        now = time.time()
        if not force and (now - self._last_beat) < self.beat_interval:
            return
        self._last_beat = now
        payload = {
            "load1": load1(),
            "load5": load5(),
            "mem_free_gb": mem_free_gb(),
            "worker_id": self.worker_id,
            "pid": os.getpid(),
            "current_unit_id": self.current_unit_id,
        }
        try:
            reply = self.q.machine_beat(self.machine_id, payload) or {}
        except Exception as exc:
            print(f"wq-worker: machine_beat failed: {exc}", file=sys.stderr)
            return
        # Drain is delivered — and revoked — by the beat. A drained worker keeps
        # beating (slowly) on purpose: a worker that stops beating entirely can
        # never observe `wq drain <machine> --undo` and would need a restart.
        self.drain = bool(reply.get("drain"))

    # -- capacity gate ---------------------------------------------------------
    def load_ratio(self, current: float | None = None) -> float | None:
        """``load1 / (ceiling_fraction * ncpu)``. ``None`` when load is unknown.

        One number, two consumers: >1.0 stops claiming altogether (the capacity
        gate), and below that it sets the claim cadence. They must not drift
        apart — a box that claims but polls as if it were saturated would idle
        for no reason, and the reverse would defeat the ceiling.
        """
        current = load1() if current is None else current
        if current is None:
            return None
        info = self.machine()
        ceiling = info.ceiling_fraction * max(1, info.ncpu)
        if ceiling <= 0:  # pragma: no cover - ceiling_fraction is validated > 0
            return None
        return current / ceiling

    def capacity_ok(self) -> tuple[bool, float | None]:
        current = load1()
        ratio = self.load_ratio(current)
        return (ratio is None or ratio <= 1.0), current

    def idle_delay(self, current: float | None = None) -> float:
        """How long to wait after an empty claim — short when this box is free.

        Statistical routing, not placement: see ``idle_poll_delay_s``.
        """
        return idle_poll_delay_s(self.load_ratio(current))

    # -- main loop -------------------------------------------------------------
    def run_forever(self, *, once: bool = False) -> int:
        install_stop_handlers(self.stop)
        while not self.stop.is_set():
            self.beat()
            if self.drain:
                if once:
                    return 0
                self.stop.wait(self.beat_interval)
                continue
            ok, current = self.capacity_ok()
            if not ok:
                info = self.machine()
                print(
                    f"wq-worker: load1={current} over ceiling "
                    f"{info.ceiling_fraction}x{info.ncpu} — skipping claim",
                    file=sys.stderr,
                )
                self.beat(force=True)
                if once:
                    return 0
                self.stop.wait(CAPACITY_SLEEP_S)
                continue
            try:
                claim = self.q.claim(
                    self.machine_id, self.worker_id, self.labels, reserved_for=self.reserved_for
                )
            except Exception as exc:
                # A transport failure is evidence about the SERVER, not about
                # this box's headroom, so it waits the flat floor.
                print(f"wq-worker: claim failed: {exc}", file=sys.stderr)
                claim = None
                self.stop.wait(IDLE_SLEEP_S)
            if not claim:
                if once:
                    return 0
                # Nothing for us: back off in proportion to how busy we already
                # are, so the idlest machine in the pool asks most often.
                self.stop.wait(self.idle_delay(current))
                continue
            self.execute(claim, start_load=current)
            if once:
                return 0
        return 0

    # -- one unit --------------------------------------------------------------
    def execute(self, claim: dict[str, Any], *, start_load: float | None = None) -> UnitResult:
        unit = claim["unit"]
        generation = int(claim["lease_generation"])
        attempt = int(claim["attempt"])
        unit_id = unit["id"]
        self.current_unit_id = unit_id
        gate = unit.get("acceptance_gate") or "raw"
        log_path = self.home / "logs" / unit_id / f"{attempt}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        beat = _Heartbeat(self.q, unit_id, self.owner, generation, self.beat_interval)
        beat.start()
        try:
            result = self._execute_inner(unit, attempt, gate, log_path, beat, start_load)
        except WorkerFault as fault:
            result = UnitResult(
                outcome=fault.outcome,
                remedy=fault.remedy,
                retryable=fault.retryable,
                log_path=str(log_path),
            )
        except Exception as exc:  # an unexpected worker bug is an INSTRUMENT fact
            result = UnitResult(
                outcome="instrument-error",
                remedy=f"worker raised {type(exc).__name__}: {exc}",
                retryable=1,
                log_path=str(log_path),
            )
        finally:
            beat.stop()
            self.current_unit_id = None
        if beat.lease_lost.is_set():
            result = UnitResult(
                outcome="lease-lost",
                remedy="lease was adopted by another worker; child tree killed, no result written",
                retryable=1,
                log_path=str(log_path),
                input_key=result.input_key,
            )
        self._record(unit, generation, gate, result)
        return result

    def _execute_inner(
        self,
        unit: dict[str, Any],
        attempt: int,
        gate: str,
        log_path: Path,
        beat: _Heartbeat,
        start_load: float | None,
    ) -> UnitResult:
        deadline = time.time() + int(unit.get("timeout_s") or 3600)
        profile = self._profile(unit)
        has_agent_turn = bool(profile.get("cmd"))

        # 1/2 — mirror. We fetch ONLY when the pinned sha is missing: base_sha is a
        # sha, so an object we already have needs nothing from the network, and
        # that is what keeps the refusal path below local and sub-second.
        mirror = self._ensure_mirror(unit)

        # 3 — the fingerprint + the refusal ledger. WHEN the key can be computed
        # depends on whether anything will edit the tree before the gate sees it:
        #
        #  * no agent turn (`script`) — the graded tree IS the pinned tree, so the
        #    key is computable from the bare mirror and an already-refused input is
        #    refused in ~0.2 s having spent no clone at all. That is the §4 promise
        #    and the Phase-3 demo's timing assertion.
        #  * with an agent turn — the key MUST be taken after the agent has run,
        #    because §4.1 hashes the tree the gate actually grades. Hashing the
        #    pre-agent tree instead would make every attempt of an agent unit share
        #    one key, so attempt 2 would be refused as unchanged-retry before the
        #    agent could produce anything different — and §4.6's lineage swap (a
        #    DIFFERENT profile on the second defect) would never get to run. The
        #    expensive thing this protects is the gate, and the gate is still ahead.
        key: str | None = None
        if not has_agent_turn:
            key = self._input_key(unit, gate, mirror=mirror)
            refused = self._check_ledger(key, gate)
            if refused is not None:
                refused.input_key = key
                refused.log_path = str(log_path)
                return refused

        # loadavg at START is the one non-log row of the §4.4b table.
        info = self.machine()
        contention = classifier.classify_start_load(start_load, info.perf_cores)
        if contention is not None:
            return UnitResult(
                outcome=contention.outcome,
                retryable=contention.retryable,
                remedy=contention.remedy,
                input_key=key,
                log_path=str(log_path),
            )

        # 4 — plain local clone at the pinned sha (never --shared: scripts/new-lane.sh).
        workdir = self._clone(unit, mirror)
        head = _git_head(workdir)

        with log_path.open("a", encoding="utf-8") as log:
            log.write(f"=== wq unit {unit['id']} attempt {attempt} @ {utc_now_iso()} ===\n")
            log.flush()

            # 5 — the agent turn (absent for the `script` profile).
            agent_rc = self._run_agent(unit, profile, workdir, log, beat, deadline)

        if beat.lease_lost.is_set():
            return UnitResult(outcome="lease-lost", input_key=key, log_path=str(log_path))
        if agent_rc == "timeout":
            return UnitResult(
                outcome="timeout",
                retryable=0,
                remedy=f"agent turn exceeded timeout_s={unit.get('timeout_s')}; child tree killed",
                input_key=key,
                head_sha=head,
                log_path=str(log_path),
            )

        if has_agent_turn:
            # The tree the gate will grade now exists: fingerprint it, and refuse
            # BEFORE the gate is spent if this exact result was already refused —
            # an agent that produced a byte-identical tree twice is the storm.
            key = self._input_key(unit, gate, workdir=workdir)
            refused = self._check_ledger(key, gate)
            if refused is not None:
                refused.input_key = key
                refused.head_sha = head
                refused.log_path = str(log_path)
                return refused

        # 6 — scope. A unit that wrote outside owned_paths fails acceptance
        # regardless of exit code. For the `script` profile there is no agent turn,
        # so the writes (if any) come from the acceptance command and the check
        # runs after it instead.
        if has_agent_turn:
            violation = self._scope_check(unit, workdir)
            if violation is not None:
                violation.input_key = key
                violation.head_sha = _git_head(workdir)
                violation.log_path = str(log_path)
                return violation

        # 7 — acceptance.
        assert key is not None  # set on both the agent and non-agent paths above
        with log_path.open("a", encoding="utf-8") as log:
            rc, gate_rc, gate_class, gate_remedy = self._run_acceptance(
                unit, workdir, gate, key, log, beat, deadline
            )
        text = _read_tail(log_path)

        if beat.lease_lost.is_set():
            return UnitResult(outcome="lease-lost", input_key=key, log_path=str(log_path))

        if not has_agent_turn and rc != "timeout":
            violation = self._scope_check(unit, workdir)
            if violation is not None:
                violation.input_key = key
                violation.exit_code = rc if isinstance(rc, int) else None
                violation.head_sha = _git_head(workdir)
                violation.log_path = str(log_path)
                return violation

        head = _git_head(workdir)
        if rc == "timeout":
            return UnitResult(
                outcome="timeout",
                retryable=0,
                remedy=f"acceptance exceeded timeout_s={unit.get('timeout_s')}",
                input_key=key,
                head_sha=head,
                log_path=str(log_path),
            )

        # "timeout" is the only non-int _blocking_run can return, and it returned above.
        assert isinstance(rc, int)
        result = self._verdict(rc, gate_rc, gate_class, text, gate_remedy=gate_remedy)
        result.input_key = key
        result.head_sha = head
        result.log_path = str(log_path)

        # 8 — push, then report. A push failure is an INSTRUMENT fact; it must never
        # become a silent pass, because the branch is the only artefact a pass has.
        if result.outcome == "pass":
            self._push(unit, workdir, mirror, result)
        return result

    # -- verdict ---------------------------------------------------------------
    def _verdict(
        self,
        rc: int,
        gate_rc: int | None,
        gate_class: str | None,
        log_text: str,
        gate_remedy: str | None = None,
    ) -> UnitResult:
        """The 0/1/2 trichotomy comes from the PROCESS code, and nothing else."""
        if rc == 0:
            outcome, retryable, remedy = "pass", None, "accepted"
        elif rc == 1:
            outcome, retryable, remedy = (
                "candidate-defect",
                0,
                "read the log, fix the named cause; the next run's input_key differs "
                "and is admitted automatically.",
            )
        else:
            # COULD NOT RUN. The gate's own printed class may refine WHICH exit-2
            # class this is; it can never move the run across the 0/1/2 line.
            refine = {
                "instrument-error": 1,
                "environment": 1,
                "contention-flake": 1,
                "unchanged-retry": 0,
                "storm-parked": 0,
            }
            if gate_class in refine:
                outcome, retryable = gate_class, refine[gate_class]
            else:
                outcome, retryable = "instrument-error", 1
            # A refusal should name its own remedy: surface the gate's printed
            # remedy (which names the violated invariant) instead of discarding it.
            remedy = f"acceptance could not produce a verdict (process exit {rc})"
            if gate_remedy:
                remedy = f"{remedy}: {gate_remedy}"
        result = UnitResult(
            outcome=outcome,
            exit_code=rc,
            gate_exit_code=gate_rc,
            retryable=retryable,
            remedy=remedy,
        )
        # §4.4b — the post-hoc classifier overrides the exit code. It can only ever
        # turn a graded verdict into a re-run, never a failure into a pass.
        signature = classifier.classify_log(log_text)
        if signature is not None:
            result.outcome = signature.outcome
            result.retryable = signature.retryable
            result.remedy = signature.remedy
        return result

    # -- steps -----------------------------------------------------------------
    def _ensure_mirror(self, unit: dict[str, Any]) -> Path:
        mirror = self.home / "repos" / f"{unit['repo_slug']}.git"
        mirror.parent.mkdir(parents=True, exist_ok=True)
        if not mirror.exists():
            res = _sh(["git", "clone", "--mirror", unit["repo_url"], str(mirror)], timeout=1800)
            if res.returncode != 0:
                raise _git_fault("clone --mirror", res)
            return mirror
        if (
            _sh(
                ["git", "-C", str(mirror), "cat-file", "-e", f"{unit['base_sha']}^{{commit}}"]
            ).returncode
            == 0
        ):
            return mirror
        res = _sh(
            ["git", "-C", str(mirror), "fetch", "--prune"], timeout=900
        )  # ONE try, no retry loop
        if res.returncode != 0:
            raise _git_fault("fetch --prune", res)
        if (
            _sh(
                ["git", "-C", str(mirror), "cat-file", "-e", f"{unit['base_sha']}^{{commit}}"]
            ).returncode
            != 0
        ):
            raise WorkerFault(
                "instrument-error",
                f"base_sha {unit['base_sha'][:12]} is not in {mirror} after one fetch — "
                "the pinned commit was never pushed, or the mirror points at the wrong repo.",
                retryable=1,
            )
        return mirror

    def _profile(self, unit: dict[str, Any]) -> dict[str, Any]:
        """Resolve the unit's agent profile, or refuse with a named remedy.

        A unit names a profile the machine already TRUSTS; it can never inject a
        shell command onto another box. An undeclared profile is therefore an
        instrument fault with retryable=0 — re-running changes nothing until the
        machine's config does.
        """
        profiles = self.cfg.get("profiles") or {}
        name = unit["agent_profile"]
        if name not in profiles:
            raise WorkerFault(
                "instrument-error",
                f"agent_profile {name!r} is not declared in configs/workqueue.yaml on this "
                "machine — add the profile (and its lineage) there, then unpark. A unit can "
                "never inject a shell command onto another box.",
                retryable=0,
            )
        return profiles[name] or {}

    def _input_key(
        self,
        unit: dict[str, Any],
        gate: str,
        *,
        mirror: Path | None = None,
        workdir: Path | None = None,
    ) -> str:
        """SPEC §4.1. ``mirror`` fingerprints the PIN (pre-clone); ``workdir`` the
        live tree the gate will grade. For a clean checkout at base_sha the two are
        byte-identical — asserted in tests/workqueue_gate."""
        gated = bool(unit.get("acceptance_gate"))
        config_files = [str(REPO_ROOT / "configs" / "gates.d" / f"{gate}.yaml")] if gated else []
        try:
            return input_key(
                gate,
                str(workdir or ""),
                str(GATE_SCRIPT) if gated else None,
                config_files,
                unit["acceptance_cmd"],
                pinned_at=(str(mirror), unit["base_sha"]) if mirror is not None else None,
            )
        except InputKeyError as exc:
            raise WorkerFault("instrument-error", str(exc), retryable=1) from exc

    def _check_ledger(self, key: str, gate: str) -> UnitResult | None:
        try:
            row = self.q.refusal_check(key, gate)
        except Exception as exc:
            print(f"wq-worker: refusal_check failed: {exc}", file=sys.stderr)
            return None
        if not row:
            return None
        count = int(row.get("count") or 0)
        if count >= self.storm_cap:
            return UnitResult(
                outcome="storm-parked",
                retryable=0,
                remedy=(
                    f"{count} refusals for this exact input (class {row.get('refusal_class')}) — "
                    "PARKED, terminal. A human or a CHANGED INPUT unparks it; retrying is "
                    f"prohibited. Original remedy: {row.get('remedy')}"
                ),
            )
        # Two classes carry remedies that REQUIRE re-running the same key: the
        # instrument is not part of the fingerprint, so repairing it never changes
        # the key, and 'contention-flake' literally means "re-run once when quiet".
        if int(row.get("retryable") or 0) == 0 and row.get("refusal_class") not in (
            "instrument-error",
            "contention-flake",
        ):
            return UnitResult(
                outcome="unchanged-retry",
                retryable=0,
                remedy=(
                    f"identical input was refused {count}x (class {row.get('refusal_class')}, "
                    f"last {row.get('last_seen_at')}). Change the tree, the gate config or the "
                    f"gate itself; re-running unchanged is not a strategy. "
                    f"Original remedy: {row.get('remedy')}"
                ),
            )
        return None

    def _clone(self, unit: dict[str, Any], mirror: Path) -> Path:
        workdir = self.home / "work" / unit["id"]
        if workdir.exists():
            shutil.rmtree(workdir, ignore_errors=True)
        workdir.parent.mkdir(parents=True, exist_ok=True)
        # Plain local clone: hardlinks packs on APFS, no alternates, no gc hazard.
        # NEVER --shared (scripts/new-lane.sh:10-16) — a pruned parent would corrupt
        # every worktree that borrowed its objects.
        res = _sh(
            ["git", "clone", "--no-checkout", "--no-tags", str(mirror), str(workdir)], timeout=900
        )
        if res.returncode != 0:
            raise _git_fault("clone", res)
        res = _sh(
            ["git", "-C", str(workdir), "checkout", "-b", unit["branch"], unit["base_sha"]],
            timeout=300,
        )
        if res.returncode != 0:
            raise _git_fault("checkout -b", res)
        return workdir

    def _brief_file(self, unit: dict[str, Any], workdir: Path) -> str | None:
        if unit.get("brief_path"):
            # brief_path is submitter-controlled text that this worker turns into
            # a path it hands to an agent. It names a file INSIDE the checkout and
            # nothing else: absolute paths and ../ escapes (and symlinks that
            # resolve out, which is why this compares resolved paths) would let a
            # submitted unit read any file on any box in the pool.
            raw = str(unit["brief_path"])
            candidate = Path(raw)
            target = workdir / candidate
            # Containment decided by the shared INODE primitive, never a lexical
            # Path comparison (tests/acceptance/test_19_path_security_registry):
            # inode_relative_parts returns None when the target is not provably
            # inside the checkout — a ../ escape or a symlink that resolves out —
            # and fails closed on any filesystem error. is_absolute stays as a
            # belt so a brief must be a *relative* in-checkout path.
            if candidate.is_absolute() or inode_relative_parts(target, workdir) is None:
                raise WorkerFault(
                    "instrument-error",
                    f"brief_path escapes worktree: {raw!r} — a unit's brief must be a "
                    "relative path inside its own checkout. Fix the submitted unit; "
                    "re-running it unchanged cannot help.",
                    retryable=0,
                )
            return str(target.resolve())
        if not unit.get("brief_inline"):
            return None
        # Briefs live OUTSIDE the worktree: writing one inside would dirty the tree,
        # trip the git-clean invariant and show up as an owned_paths violation.
        briefs = self.home / "briefs"
        briefs.mkdir(parents=True, exist_ok=True)
        path = briefs / f"{unit['id']}.md"
        path.write_text(unit["brief_inline"], encoding="utf-8")
        return str(path)

    def _run_agent(
        self,
        unit: dict[str, Any],
        profile: dict[str, Any],
        workdir: Path,
        log: Any,
        beat: _Heartbeat,
        deadline: float,
    ) -> int | str:
        name = unit["agent_profile"]
        template = profile.get("cmd")
        if not template:
            log.write(f"[wq] profile {name} declares no agent turn; acceptance IS the unit\n")
            log.flush()
            return 0
        brief = self._brief_file(unit, workdir)
        argv = [
            str(part).replace("{workdir}", str(workdir)).replace("{brief}", brief or "")
            for part in template
        ]
        return self._blocking_run(argv, workdir, log, beat, deadline, label=f"agent:{name}")

    def _run_acceptance(
        self,
        unit: dict[str, Any],
        workdir: Path,
        gate: str,
        key: str,
        log: Any,
        beat: _Heartbeat,
        deadline: float,
    ) -> tuple[int | str, int | None, str | None, str | None]:
        if unit.get("acceptance_gate"):
            argv = [
                sys.executable,
                str(GATE_SCRIPT),
                "run",
                gate,
                "--ledger",
                "off",  # the WORKER writes the ledger — only it knows the post-override class
                "--input-key",
                key,
                "--var",
                f"workdir={workdir}",
                "--var",
                f"repo={REPO_ROOT}",
                "--var",
                f"wq_home={self.home}",
                "--var",
                f"base_sha={unit['base_sha']}",
                "--var",
                f"acceptance_cmd={unit['acceptance_cmd']}",
            ]
        else:
            argv = shlex.split(unit["acceptance_cmd"])
        rc = self._blocking_run(argv, workdir, log, beat, deadline, label="acceptance")
        gate_rc, gate_class, gate_remedy = (None, None, None)
        if unit.get("acceptance_gate"):
            gate_rc, gate_class, gate_remedy = _parse_gate_stdout(_read_tail(Path(log.name)))
        return rc, gate_rc, gate_class, gate_remedy

    def _blocking_run(
        self,
        argv: list[str],
        workdir: Path,
        log: Any,
        beat: _Heartbeat,
        deadline: float,
        *,
        label: str,
    ) -> int | str:
        """ONE blocking bounded wait. Never a 30 s poll loop.

        ``start_new_session=True`` puts the child in its own process GROUP, which
        is the only way to kill a codex/claude turn's whole subtree when the lease
        is lost or the unit times out.
        """
        remaining = max(1.0, deadline - time.time())
        log.write(f"[wq] {label}: {' '.join(shlex.quote(a) for a in argv)}\n")
        log.flush()
        env = _child_env(self.home)
        try:
            proc = subprocess.Popen(
                argv,
                cwd=str(workdir),
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                env=env,
            )
        except (OSError, ValueError) as exc:
            raise WorkerFault(
                "instrument-error",
                f"{label} could not start: {type(exc).__name__}: {exc}",
                retryable=1,
            ) from exc
        beat.track(proc.pid)  # start_new_session ⇒ pgid == pid
        try:
            return proc.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            kill_process_group(proc.pid)
            proc.wait(timeout=KILL_GRACE_S + 5)
            return "timeout"
        finally:
            beat.track(None)

    def _scope_check(self, unit: dict[str, Any], workdir: Path) -> UnitResult | None:
        """owned_paths enforcement via the repo's existing verifier — never a fourth
        path classifier (SPEC §2 field notes)."""
        from omniagentos.contracts import ScopeEnforcement
        from omniagentos.execution.verify import observe_working_tree, scope_verdict
        from omniagentos.reliability.governance import DiffEntry

        owned = _as_list(unit.get("owned_paths"))
        observed = observe_working_tree(workdir, base_ref=unit["base_sha"])
        entries = [
            DiffEntry(status=observed.status_by_path.get(path, "M"), path=path)
            for path in observed.paths
        ]
        declared = [path for path in observed.paths if path_in_scope(path, owned)]
        verdict = scope_verdict(
            declared=declared,
            create_roots=[],
            entries=entries,
            observed=observed,
            suggested_level=1,
            enforcement=ScopeEnforcement.ENFORCE,
            repo_root=workdir,
        )
        if verdict.get("degraded"):
            # H-13: 'unobserved' is NOT clean. An inconclusive snapshot is an
            # instrument fault — it must never mint a defect, and must never pass.
            return UnitResult(
                outcome="instrument-error",
                retryable=1,
                remedy=(
                    "the working tree could not be observed, so owned_paths could not be "
                    f"verified ({'; '.join(verdict.get('reasons') or [])}). Inconclusive is "
                    "not clean — repair git in the workdir and re-run the same key."
                ),
            )
        if verdict.get("missing"):
            return UnitResult(
                outcome="candidate-defect",
                exit_code=None,
                retryable=0,
                remedy=(
                    "wrote outside owned_paths: "
                    + ", ".join(verdict["missing"])
                    + f" (owned: {', '.join(owned)}). Acceptance fails regardless of exit code."
                ),
            )
        return None

    def _push(self, unit: dict[str, Any], workdir: Path, mirror: Path, result: UnitResult) -> None:
        branch = unit["branch"]
        res = _sh(
            ["git", "-C", str(workdir), "push", "origin", f"HEAD:refs/heads/{branch}"], timeout=600
        )
        if res.returncode != 0:
            fault = _git_fault("push to mirror", res)
            result.outcome, result.retryable, result.remedy = (
                fault.outcome,
                fault.retryable,
                fault.remedy,
            )
            return
        result.result_branch = branch
        result.result_sha = result.head_sha
        upstream = _sh(
            ["git", "-C", str(mirror), "remote", "get-url", "--push", "origin"], timeout=30
        )
        if upstream.returncode != 0 or not upstream.stdout.strip():
            return  # no push remote on this repo — a local-only demo repo, not a failure
        # Push the branch from the WORKDIR to the upstream URL, never `git -C <mirror>
        # push origin`: a --mirror clone carries remote.origin.mirror=true, which turns
        # any push into a mirror push — it refuses refspecs, and unrefused it would
        # DELETE every upstream ref this mirror has not fetched.
        res = _sh(
            [
                "git",
                "-C",
                str(workdir),
                "push",
                upstream.stdout.strip(),
                f"HEAD:refs/heads/{branch}",
            ],
            timeout=900,
        )
        if res.returncode != 0:
            fault = _git_fault("push to origin", res)
            result.outcome, result.retryable, result.remedy = (
                fault.outcome,
                fault.retryable,
                fault.remedy,
            )

    # -- report ----------------------------------------------------------------
    def _record(self, unit: dict[str, Any], generation: int, gate: str, result: UnitResult) -> None:
        # The ledger first: a pass DELETES the row (the ledger can only ever refuse
        # harder — it has no path that emits a cached pass), every refusal counts.
        if result.input_key and result.outcome not in ("lease-lost", "abandoned"):
            try:
                if result.outcome == "pass":
                    self.q.refusal_clear(result.input_key, gate)
                else:
                    row = self.q.refusal_record(
                        result.input_key,
                        gate,
                        result.outcome,
                        1 if result.retryable else 0,
                        result.remedy or "(no remedy recorded)",
                        unit_id=unit["id"],
                    )
                    # The ledger's own one-alert CAS fired: this call, and no
                    # other, holds the refusal-storm payload. SEND IT HERE, before
                    # record_result — the CAS is spent whatever happens next, and
                    # the park that would otherwise carry the news is not
                    # guaranteed to arrive. It does not when the unit was
                    # cancelled while this attempt was in flight (record_result
                    # routes to CANCELLED: no park, no alert) or when the lease
                    # was lost, and a storm that reaches nobody is the exact
                    # favourable-absence failure §4 exists to make impossible.
                    # record_result suppresses the duplicate park alert for this
                    # input_key (store._storm_already_announced), so this stays
                    # ONE alert for one event.
                    self._announce((row or {}).get("alert"))
            except Exception as exc:
                print(f"wq-worker: refusal ledger write failed: {exc}", file=sys.stderr)
        try:
            reply = self.q.record_result(
                unit["id"],
                self.owner,
                generation,
                result.outcome,
                exit_code=result.exit_code,
                gate_exit_code=result.gate_exit_code,
                input_key=result.input_key,
                retryable=result.retryable,
                remedy=result.remedy,
                head_sha=result.head_sha,
                result_branch=result.result_branch,
                result_sha=result.result_sha,
                log_path=result.log_path,
            )
        except LeaseLost:
            # A zombie writes NOTHING. The reaper marks the open attempt abandoned.
            print(
                f"wq-worker: lease lost for {unit['id']} — result discarded (fenced out)",
                file=sys.stderr,
            )
            return
        except Exception as exc:
            print(f"wq-worker: record_result failed: {exc}", file=sys.stderr)
            return
        # The store won the alerted_at CAS; exactly one alert per park.
        self._announce((reply or {}).get("alert"))

    def _announce(self, payload: dict[str, Any] | None) -> None:
        """Send a won-CAS alert payload. Never raises, never sends twice.

        The CAS that produced ``payload`` fired exactly once, so this is the only
        chance anyone gets to say it out loud — but only the holder of the
        transport may speak (see ``owns_alert_transport``).
        """
        if not payload or not self.owns_alert_transport:
            return
        try:
            alert_transport.send_alert(payload)
        except Exception as exc:  # pragma: no cover - send_alert is fail-soft by contract
            print(f"wq-worker: ALERT (transport failed: {exc}): {payload}", file=sys.stderr)


# ------------------------------------------------------------------- helpers ----
#: The one ``WQ_*`` variable a child is allowed to inherit. It names a directory
#: (logs, briefs, mirrors) the child legitimately needs; the rest are pool
#: CREDENTIALS and coordinates — ``WQ_TOKEN`` is the pool bearer token, and
#: ``WQ_SERVER``/``WQ_DB`` say where to spend it.
CHILD_ENV_KEEP = "WQ_HOME"


def _child_env(home: Path) -> dict[str, str]:
    """The environment an agent turn or acceptance command runs under.

    Every ``WQ_*`` variable except :data:`CHILD_ENV_KEEP` is REMOVED. A unit's
    ``acceptance_cmd`` is submitter-controlled text executed on someone else's
    Mac; with ``WQ_TOKEN`` in its environment, "run my tests" is also "print the
    pool's bearer token to a log I can read", which is claim/cancel/enqueue on
    every machine in the pool. Nothing else is filtered — PATH, HOME and the
    toolchain's own variables are what make the command runnable at all.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("WQ_")}
    env[CHILD_ENV_KEEP] = os.environ.get(CHILD_ENV_KEEP) or str(home)
    return env


def _as_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return [value] if value else []
        return [str(v) for v in parsed] if isinstance(parsed, list) else []
    return []


def _git_head(path: str | Path) -> str | None:
    res = _sh(["git", "-C", str(path), "rev-parse", "HEAD"], timeout=30)
    return res.stdout.strip() or None if res.returncode == 0 else None


def _git_fault(step: str, res: subprocess.CompletedProcess[str]) -> WorkerFault:
    """Every git failure is an INSTRUMENT fact until the classifier says otherwise."""
    text = (res.stdout or "") + "\n" + (res.stderr or "")
    signature = classifier.classify_log(text)
    if signature is not None:
        return WorkerFault(
            signature.outcome, f"git {step}: {signature.remedy}", signature.retryable
        )
    return WorkerFault(
        "instrument-error",
        f"git {step} failed (exit {res.returncode}): {text.strip()[:400]}",
        retryable=1,
    )


def _read_tail(path: Path, limit: int = 262_144) -> str:
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - limit))
            return fh.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def _parse_gate_stdout(text: str) -> tuple[int | None, str | None, str | None]:
    """Read the gate's printed JSON summary — REFINEMENT ONLY, never a verdict.

    Absence is recorded as ``None``: a fabricated measurement is worse than a
    missing one (the estate has a receipt claiming ``gate_exit_code: 3`` for a
    gate that was never started).
    """
    for match in reversed(re.findall(r"\{[^{}]*\"class\"[^{}]*\}", text, re.DOTALL)):
        try:
            payload = json.loads(match)
        except json.JSONDecodeError:
            continue
        gate_rc = payload.get("gate_exit_code")
        remedy = payload.get("remedy")
        return (
            (gate_rc if isinstance(gate_rc, int) else None),
            payload.get("class"),
            (remedy if isinstance(remedy, str) and remedy else None),
        )
    return None, None, None


def install_stop_handlers(stop: threading.Event) -> None:
    def _handler(signum: int, _frame: Any) -> None:
        stop.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _handler)
        except ValueError:  # pragma: no cover - not the main thread
            pass


# ---------------------------------------------------------------- supervisor ----
def supervise(slots: int, child: Any) -> int:
    """Fork ``slots`` children, restart them, and drain on SIGTERM.

    The parent runs no queue logic at all: a supervisor that could itself get
    stuck on a claim is a second failure mode for no benefit.
    """
    children: set[int] = set()
    state = {"stopping": False}

    def _term(signum: int, _frame: Any) -> None:
        state["stopping"] = True
        for pid in list(children):
            try:
                os.kill(pid, signal.SIGTERM)  # children finish in-flight work, then exit
            except ProcessLookupError:
                children.discard(pid)

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, _term)

    while True:
        while not state["stopping"] and len(children) < slots:
            pid = os.fork()
            if pid == 0:
                # Reset the inherited disposition FIRST. A child that keeps the
                # parent's handler would only set a flag the parent owns, and a
                # SIGTERM arriving in the window before the Worker installs its own
                # handler would be swallowed — the box would look drained while a
                # child sat there holding a slot. Until the Worker takes over,
                # default-terminate is the correct answer: nothing is claimed yet.
                for sig in (signal.SIGTERM, signal.SIGINT):
                    signal.signal(sig, signal.SIG_DFL)
                code = 1
                try:
                    code = int(child() or 0)
                finally:
                    os._exit(code)
            children.add(pid)
        if not children:
            return 0
        try:
            dead, _status = os.waitpid(-1, 0)  # ONE blocking wait, no poll loop
        except ChildProcessError:
            return 0
        children.discard(dead)
        if not state["stopping"]:
            time.sleep(RESPAWN_BACKOFF_S)


# ---------------------------------------------------------------------- main ----
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m omniagentos.workqueue.worker",
        description="Shared work-queue pool worker (claim → clone → run → verify → report).",
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--server", default=os.environ.get("WQ_SERVER"), help="wq-server base URL")
    source.add_argument(
        "--db", default=os.environ.get("WQ_DB"), help="path to var/workqueue.sqlite3"
    )
    parser.add_argument("--machine-id", default=os.environ.get("WQ_MACHINE_ID"))
    parser.add_argument("--slots", type=int, default=1, help="fork N worker processes")
    parser.add_argument("--labels", default="", help="comma-separated capability labels")
    parser.add_argument(
        "--reserved-for",
        default=os.environ.get("WQ_RESERVED_FOR"),
        help=(
            "Reserve this worker for ONE owner: it then claims ONLY units whose "
            "submitted_by equals <owner>. Resource hygiene to keep other people's pool "
            "work off a machine reserved for one person under COOPERATIVE use — NOT a "
            "security boundary: submitted_by is self-declared under the shared pool "
            "token, so a token-holder who submits as <owner> can still land work here "
            "(a hard boundary needs per-submitter auth tokens, out of scope). Given but "
            "empty/blank ⇒ the worker REFUSES TO START (fail closed). Omit it entirely "
            "to run a normal unrestricted worker."
        ),
    )
    parser.add_argument("--config", default=None, help="path to configs/workqueue.yaml")
    parser.add_argument("--wq-home", default=None, help="default $WQ_HOME or ~/wq")
    parser.add_argument("--once", action="store_true", help="claim at most one unit, then exit")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # Fail CLOSED before anything is claimed: a --reserved-for that is present but
    # blank aborts the worker here, so a broken reservation claims NOTHING rather
    # than silently becoming an unrestricted worker.
    reserved_for = resolve_reserved_for(args.reserved_for)
    if args.wq_home:
        os.environ["WQ_HOME"] = args.wq_home
    machine_id = args.machine_id or default_machine_id()
    labels = [chunk.strip() for chunk in args.labels.split(",") if chunk.strip()]
    config = load_config(args.config)

    def child() -> int:
        queue = open_queue(args.server, args.db)
        worker = Worker(queue, machine_id, config=config, labels=labels, reserved_for=reserved_for)
        return worker.run_forever(once=args.once)

    if args.slots > 1:
        return supervise(args.slots, child)
    return child()


if __name__ == "__main__":
    raise SystemExit(main())
