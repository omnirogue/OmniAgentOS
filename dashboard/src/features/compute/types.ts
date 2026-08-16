/**
 * Wire contract for the Observatory /compute (Estate Compute) surface —
 * verbatim field names from the backend lane's actual reader/route
 * implementation (`omniagentos/compute/readers.py` +
 * `omniagentos/api/routes/compute.py`, verified against source during the
 * CROSS-1 review fixes):
 *
 *   GET /api/compute/estate
 *
 * Read-only: no writes here, absence rendered as absence. Every numeric
 * field is typed nullable — the wire contract's own note is explicit:
 * "Numbers may be null when unmeasured → render '—', never 0, never
 * success-styled (Rule 2)."
 *
 * `pool`, `runners`, AND `local` are each a DISCRIMINATED UNION on
 * `available` (CROSS-1 review fixes, 2026-08-14): the backend's
 * `_absent(reason)`/`_safe_read()` helpers return ONLY
 * `{available: false, reason: string}` on that branch — machines/depth/
 * capacity/refusals_24h (or runners, or hostname/load1/load5/ncpu/
 * load_ratio) are not present on the wire at all when an envelope is down,
 * not empty placeholders. `read_local()` itself is pure stdlib (no I/O to
 * fail) but `compute.py`'s route handler wraps EVERY collector — `local`
 * included — in `_safe_read()`: any exception it raises
 * (`os.getloadavg()`/`socket.gethostname()` are not universally available)
 * degrades to the same flat absent shape, so `local` is exactly as capable
 * of reporting unavailable as the other two envelopes.
 *
 * Identity fields on a pool machine (`machine_id`/`hostname`/`os`/`labels`)
 * are fetched from the upstream pool JSON via a plain `dict.get(field)` with
 * NO validation (`_pool_machine` in readers.py) — a field the pool server
 * omitted comes back `None` on the wire. `machine_id`/`hostname` are
 * therefore nullable too; shaping code must never use a possibly-null field
 * as a React key (CROSS-1) and must fall back to a stable synthetic key
 * instead. `CiRunner.name` is widened the same way on the same principle —
 * gh's `--jq` flatten can emit `null` for a field a runner object omits.
 */

export type ComputeMachine = {
  machine_id: string | null;
  hostname: string | null;
  os: string | null;
  labels: string[] | null;
  max_concurrent: number | null;
  in_flight: number | null;
  drain: number | null;
  last_seen_at: string | null;
  last_seen_age_s: number | null;
  ncpu: number | null;
  perf_cores: number | null;
  mem_gb: number | null;
  load1: number | null;
  load5: number | null;
  mem_free_gb: number | null;
  load_ratio: number | null;
  done_1h: number | null;
  done_6h: number | null;
};

export type ComputePoolDepth = {
  queued: number | null;
  claimed: number | null;
  running: number | null;
  done: number | null;
  parked: number | null;
  cancelled: number | null;
};

export type ComputePoolCapacity = {
  total_cores: number | null;
  total_slots: number | null;
  free_slots: number | null;
  in_flight: number | null;
};

/** Refusal counts over the trailing 24h, keyed by refusal reason — shape is
 * backend-owned and open-ended; not rendered by this lane's UI today, kept
 * here only so the envelope's typing matches the wire contract in full. */
export type ComputeRefusals24h = Record<string, number>;

/** Discriminated on `available` (CROSS-1) — the `false` variant carries
 * ONLY `reason`, matching `_absent()`'s actual wire shape exactly; there is
 * no `machines`/`depth`/`capacity`/`refusals_24h` to read on that branch. */
export type ComputePool =
  | {
      available: true;
      reason: null;
      machines: ComputeMachine[];
      depth: ComputePoolDepth;
      capacity: ComputePoolCapacity;
      refusals_24h: ComputeRefusals24h;
    }
  | { available: false; reason: string };

export type CiRunner = {
  name: string | null;
  labels: string[];
  status: string;
  busy: boolean;
};

/** Discriminated on `available` (CROSS-1) — same `_absent()` shape as
 * `ComputePool`: the `false` variant carries only `reason`. */
export type ComputeRunners =
  | { available: true; reason: null; runners: CiRunner[] }
  | { available: false; reason: string };

/**
 * Discriminated on `available`, same as `ComputePool`/`ComputeRunners`
 * (CROSS-1 remainder review fix). `read_local()` itself is pure stdlib and
 * has no I/O to fail — but the route handler (`omniagentos/api/routes/
 * compute.py`) wraps EVERY collector, `read_local` included, in
 * `_safe_read()`: any exception it raises (`os.getloadavg()`/
 * `socket.gethostname()` are not universally available) becomes
 * `{"available": False, "reason": "temporarily unavailable"}` — the exact
 * same flat `_absent()` shape pool/runners use, not empty placeholders.
 * `local` is therefore just as capable of being unavailable on the wire as
 * the other two envelopes; shaping code already branched on
 * `local.available === false` (UI-3), so this type change is the only
 * correction needed — no shaping-logic change follows from it.
 */
export type ComputeLocal =
  | {
      available: true;
      reason: null;
      hostname: string | null;
      load1: number | null;
      load5: number | null;
      ncpu: number | null;
      load_ratio: number | null;
    }
  | { available: false; reason: string };

/** GET /api/compute/estate */
export type ComputeEstate = {
  generated_at: string;
  pool: ComputePool;
  runners: ComputeRunners;
  local: ComputeLocal;
};
