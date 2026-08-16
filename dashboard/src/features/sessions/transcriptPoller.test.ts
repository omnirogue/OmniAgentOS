import { describe, expect, it, vi } from "vitest";
import { createTranscriptPoller } from "./transcriptPoller";

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

describe("createTranscriptPoller", () => {
  it("discards a slow earlier response that resolves after a faster later one (out-of-order poll ticks)", async () => {
    const first = deferred<unknown[]>();
    const second = deferred<unknown[]>();
    const responses = [first.promise, second.promise];
    let call = 0;
    const onEntries = vi.fn();
    const poller = createTranscriptPoller({
      fetchTranscript: () => responses[call++]!,
      onEntries,
    });

    const p1 = poller.poll(); // tick 1, dispatched first, resolves last
    const p2 = poller.poll(); // tick 2, dispatched second, resolves first

    second.resolve([{ id: "b" }]);
    await p2;
    expect(onEntries).toHaveBeenCalledTimes(1);
    expect(onEntries).toHaveBeenCalledWith([{ id: "b" }]);

    first.resolve([{ id: "a" }]); // stale — must be discarded, not roll the transcript back
    await p1;
    expect(onEntries).toHaveBeenCalledTimes(1);
  });

  it("stops applying responses once stop() has been called (dialog closed / session switched / live tail off)", async () => {
    const response = deferred<unknown[]>();
    const onEntries = vi.fn();
    const poller = createTranscriptPoller({
      fetchTranscript: () => response.promise,
      onEntries,
    });

    const pending = poller.poll();
    poller.stop();
    response.resolve([{ id: "a" }]);
    await pending;

    expect(onEntries).not.toHaveBeenCalled();
  });

  it("swallows fetch failures without throwing or calling onEntries", async () => {
    const onEntries = vi.fn();
    const poller = createTranscriptPoller({
      fetchTranscript: () => Promise.reject(new Error("network down")),
      onEntries,
    });
    await expect(poller.poll()).resolves.toBeUndefined();
    expect(onEntries).not.toHaveBeenCalled();
  });
});
