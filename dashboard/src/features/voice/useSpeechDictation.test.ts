import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MAX_INTERIM_LENGTH, useSpeechDictation } from "./useSpeechDictation";

type Handler<E> = ((event: E) => void) | null;

/** Minimal fake of the browser SpeechRecognition constructor for tests. */
class MockSpeechRecognition {
  lang = "";
  continuous = false;
  interimResults = false;
  maxAlternatives = 1;
  onstart: Handler<Event> = null;
  onend: Handler<Event> = null;
  onresult: Handler<SpeechRecognitionEvent> = null;
  onerror: Handler<SpeechRecognitionErrorEvent> = null;

  start = vi.fn(() => {
    this.onstart?.(new Event("start"));
  });
  stop = vi.fn(() => {
    this.onend?.(new Event("end"));
  });
  abort = vi.fn();
}

function makeResultEvent(
  chunks: Array<{ transcript: string; isFinal: boolean }>,
  resultIndex = 0,
): SpeechRecognitionEvent {
  const results = chunks.map((chunk) => {
    const alt = [{ transcript: chunk.transcript }];
    return Object.assign(alt, { isFinal: chunk.isFinal, 0: alt[0] });
  });
  return { resultIndex, results } as unknown as SpeechRecognitionEvent;
}

describe("useSpeechDictation", () => {
  let lastInstance: MockSpeechRecognition | null = null;

  beforeEach(() => {
    lastInstance = null;
    delete (window as unknown as { SpeechRecognition?: unknown }).SpeechRecognition;
    // A constructor function that explicitly returns an object is honored by `new`
    // regardless of the wrapping mock, so this stays simple and prototype-free.
    // Must use `function`, not an arrow, so `new Ctor()` remains constructible.
    (window as unknown as { webkitSpeechRecognition?: unknown }).webkitSpeechRecognition = vi.fn(
      function ctor() {
        const instance = new MockSpeechRecognition();
        lastInstance = instance;
        return instance;
      },
    );
  });

  afterEach(() => {
    delete (window as unknown as { SpeechRecognition?: unknown }).SpeechRecognition;
    delete (window as unknown as { webkitSpeechRecognition?: unknown }).webkitSpeechRecognition;
  });

  it("reports unsupported when neither SpeechRecognition constructor exists", () => {
    delete (window as unknown as { webkitSpeechRecognition?: unknown }).webkitSpeechRecognition;
    const { result } = renderHook(() => useSpeechDictation(vi.fn()));
    expect(result.current.supported).toBe(false);
  });

  it("reports supported when webkitSpeechRecognition is present", () => {
    const { result } = renderHook(() => useSpeechDictation(vi.fn()));
    expect(result.current.supported).toBe(true);
  });

  it("start() creates a recognition instance and flips to listening", () => {
    const { result } = renderHook(() => useSpeechDictation(vi.fn()));
    act(() => result.current.start());
    expect(lastInstance?.start).toHaveBeenCalledTimes(1);
    expect(result.current.listening).toBe(true);
    expect(result.current.status).toBe("listening");
  });

  it("does not create a second recognition instance on rapid repeated start() calls", () => {
    const ctorSpy = (window as unknown as { webkitSpeechRecognition: ReturnType<typeof vi.fn> })
      .webkitSpeechRecognition;
    const { result } = renderHook(() => useSpeechDictation(vi.fn()));
    act(() => {
      result.current.start();
      result.current.start();
      result.current.start();
    });
    expect(ctorSpy).toHaveBeenCalledTimes(1);
  });

  it("toggle() starts when idle and stops when listening", () => {
    const { result } = renderHook(() => useSpeechDictation(vi.fn()));
    act(() => result.current.toggle());
    expect(result.current.listening).toBe(true);
    act(() => result.current.toggle());
    expect(lastInstance?.stop).toHaveBeenCalledTimes(1);
    expect(result.current.listening).toBe(false);
  });

  it("sets status=denied with a user-facing message on not-allowed error", () => {
    const { result } = renderHook(() => useSpeechDictation(vi.fn()));
    act(() => result.current.start());
    act(() => {
      lastInstance?.onerror?.({ error: "not-allowed", message: "" } as SpeechRecognitionErrorEvent);
    });
    expect(result.current.status).toBe("denied");
    expect(result.current.errorMessage).toMatch(/microphone/i);
  });

  it("treats no-speech as a non-error (no message surfaced)", () => {
    const { result } = renderHook(() => useSpeechDictation(vi.fn()));
    act(() => result.current.start());
    act(() => {
      lastInstance?.onerror?.({ error: "no-speech", message: "" } as SpeechRecognitionErrorEvent);
    });
    expect(result.current.status).toBe("listening");
    expect(result.current.errorMessage).toBeNull();
  });

  it("surfaces a generic error message for unrecognized error codes", () => {
    const { result } = renderHook(() => useSpeechDictation(vi.fn()));
    act(() => result.current.start());
    act(() => {
      lastInstance?.onerror?.({ error: "network", message: "" } as SpeechRecognitionErrorEvent);
    });
    expect(result.current.status).toBe("error");
    expect(result.current.errorMessage).toMatch(/network/);
  });

  it("calls onFinalResult with trimmed final transcript chunks", () => {
    const onFinalResult = vi.fn();
    const { result } = renderHook(() => useSpeechDictation(onFinalResult));
    act(() => result.current.start());
    act(() => {
      lastInstance?.onresult?.(
        makeResultEvent([{ transcript: "  hello world  ", isFinal: true }]),
      );
    });
    expect(onFinalResult).toHaveBeenCalledWith("hello world");
  });

  it("does not call onFinalResult for interim-only (non-final) results", () => {
    const onFinalResult = vi.fn();
    const { result } = renderHook(() => useSpeechDictation(onFinalResult));
    act(() => result.current.start());
    act(() => {
      lastInstance?.onresult?.(
        makeResultEvent([{ transcript: "still speaking", isFinal: false }]),
      );
    });
    expect(onFinalResult).not.toHaveBeenCalled();
    expect(result.current.interim).toBe("still speaking");
  });

  it("caps the interim transcript display at MAX_INTERIM_LENGTH characters", () => {
    const onFinalResult = vi.fn();
    const longTranscript = "a".repeat(MAX_INTERIM_LENGTH + 50);
    const { result } = renderHook(() => useSpeechDictation(onFinalResult));
    act(() => result.current.start());
    act(() => {
      lastInstance?.onresult?.(makeResultEvent([{ transcript: longTranscript, isFinal: false }]));
    });
    expect(result.current.interim.startsWith("a".repeat(MAX_INTERIM_LENGTH))).toBe(true);
    expect(result.current.interim.startsWith(longTranscript)).toBe(false);
    expect(result.current.interim).toContain("capped at");
  });

  it("resets to idle and clears interim on recognition end", () => {
    const { result } = renderHook(() => useSpeechDictation(vi.fn()));
    act(() => result.current.start());
    act(() => {
      lastInstance?.onresult?.(makeResultEvent([{ transcript: "partial", isFinal: false }]));
    });
    expect(result.current.interim).toBe("partial");
    act(() => result.current.stop());
    expect(result.current.listening).toBe(false);
    expect(result.current.interim).toBe("");
    expect(result.current.status).toBe("idle");
  });

  it("stops any active recognition on unmount (no leaked listener/session)", () => {
    const { result, unmount } = renderHook(() => useSpeechDictation(vi.fn()));
    act(() => result.current.start());
    const instance = lastInstance;
    unmount();
    expect(instance?.stop).toHaveBeenCalledTimes(1);
  });
});
