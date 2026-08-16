/**
 * Shared client-side fetch timeout (AC-reliability pass).
 *
 * Every dashboard page reads through one of the small `req`/`getJson`/`postJson`
 * helpers in a feature's `api.ts` (or this directory's `api.ts`/`serverProxy.ts`).
 * Before this file existed, every one of those helpers called the bare
 * `fetch()`, which has no timeout of its own — a wedged backend (a hung DB
 * connection, a process that still accepts the TCP connection but never
 * answers, a network partition) left the calling hook's promise pending
 * forever. Since every hook's `loading` state only ever flips to `false`
 * inside a `.then`/`.catch`/`finally` on that promise, a promise that never
 * settles means a page that spins forever — exactly the failure reported.
 *
 * Every fetch call site now goes through `fetchWithTimeout` instead, so a
 * non-responding endpoint surfaces as a clear, bounded-time error (default
 * 10s) rather than an infinite spinner.
 */

// 20s (was 10s): the control-plane reads are normally single-digit ms, but this
// machine can be CPU-saturated by other heavy local apps, which momentarily starves
// the FastAPI process and pushed board polls past a 10s cutoff ("api down" for a
// transient spike). 20s absorbs those spikes while still surfacing a truly wedged
// backend in bounded time.
export const DEFAULT_FETCH_TIMEOUT_MS = 20_000;

/** Thrown in place of the platform's AbortError so every call site gets a
 * stable, `instanceof`-checkable type and a message that reads as "timed
 * out" rather than a generic/opaque abort. */
export class FetchTimeoutError extends Error {
  constructor(timeoutMs: number, input: string) {
    super(`Request to ${input} timed out after ${timeoutMs}ms`);
    this.name = "FetchTimeoutError";
  }
}

/**
 * `fetch()` with a hard client-side timeout. Behaves exactly like `fetch`
 * otherwise (same return type, same errors for anything other than the
 * timeout itself), so every existing call site only needs `fetch(...)`
 * swapped for `fetchWithTimeout(...)`.
 *
 * Safe to import from both client components and Node-runtime Next.js route
 * handlers — it only touches `fetch`/`AbortController`/`setTimeout`, all of
 * which exist in both environments.
 */
export async function fetchWithTimeout(
  input: string,
  init?: RequestInit,
  timeoutMs: number = DEFAULT_FETCH_TIMEOUT_MS,
): Promise<Response> {
  const controller = new AbortController();
  let timedOut = false;
  const timer = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, timeoutMs);
  const callerSignal = init?.signal;
  const abortFromCaller = () => controller.abort(callerSignal?.reason);
  if (callerSignal?.aborted) {
    abortFromCaller();
  } else {
    callerSignal?.addEventListener("abort", abortFromCaller, { once: true });
  }
  try {
    return await fetch(input, { ...init, signal: controller.signal });
  } catch (reason) {
    if (timedOut) {
      throw new FetchTimeoutError(timeoutMs, input);
    }
    throw reason;
  } finally {
    clearTimeout(timer);
    callerSignal?.removeEventListener("abort", abortFromCaller);
  }
}
