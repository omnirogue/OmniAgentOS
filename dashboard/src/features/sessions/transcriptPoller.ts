/**
 * Live-tail transcript polling, extracted from the sessions detail dialog so
 * the ordering guard is testable without React or fake timers.
 *
 * The transcript endpoint returns a full snapshot on every call (no server
 * sequence number), so two overlapping polls can resolve OUT OF ORDER — a
 * slow earlier tick resolving after a faster later tick must not roll the
 * rendered transcript backward. `poll()` assigns each dispatch a monotonic
 * request id and only applies a response when it is still the most recently
 * ISSUED request; `stop()` additionally discards any response that arrives
 * after the poller has been torn down (dialog closed / session switched /
 * live tail turned off) — the same "cancelled" guard the initial transcript
 * fetch effect already uses.
 */
export interface TranscriptPoller {
  poll(): Promise<void>;
  stop(): void;
}

export function createTranscriptPoller(params: {
  fetchTranscript: () => Promise<unknown[]>;
  onEntries: (entries: unknown[]) => void;
}): TranscriptPoller {
  let cancelled = false;
  let latestRequestId = 0;

  return {
    poll: async () => {
      const requestId = ++latestRequestId;
      try {
        const raw = await params.fetchTranscript();
        if (cancelled || requestId !== latestRequestId) return;
        params.onEntries(raw);
      } catch {
        // A transient failure while tailing shouldn't blow away the last
        // good transcript render or spam the error banner.
      }
    },
    stop: () => {
      cancelled = true;
    },
  };
}
