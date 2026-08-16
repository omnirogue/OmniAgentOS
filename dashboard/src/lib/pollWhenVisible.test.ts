import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { startVisibilityPoll } from "./pollWhenVisible";

describe("startVisibilityPoll (L-09)", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    Object.defineProperty(document, "hidden", {
      configurable: true,
      get: () => false,
    });
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("invokes the callback on interval while the tab is visible", () => {
    const fn = vi.fn();
    const stop = startVisibilityPoll(fn, 1000);
    expect(fn).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1000);
    expect(fn).toHaveBeenCalledTimes(1);
    vi.advanceTimersByTime(2000);
    expect(fn).toHaveBeenCalledTimes(3);
    stop();
  });

  it("does not invoke the callback while the tab is hidden", () => {
    Object.defineProperty(document, "hidden", {
      configurable: true,
      get: () => true,
    });
    const fn = vi.fn();
    const stop = startVisibilityPoll(fn, 1000);
    vi.advanceTimersByTime(5000);
    expect(fn).not.toHaveBeenCalled();
    stop();
  });

  it("stops after cleanup (unmount)", () => {
    const fn = vi.fn();
    const stop = startVisibilityPoll(fn, 1000);
    stop();
    vi.advanceTimersByTime(5000);
    expect(fn).not.toHaveBeenCalled();
  });
});
