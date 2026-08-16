"use client";

import { useCallback, useEffect, useRef, useState } from "react";

export interface UseSpeechSynthesisResult {
  /** Whether the browser can speak responses without a network request. */
  supported: boolean;
  speaking: boolean;
  speak: (text: string) => Promise<void>;
  cancel: () => void;
}

function speechSupported(): boolean {
  return typeof window !== "undefined"
    && window.SpeechSynthesis !== undefined
    && window.speechSynthesis !== undefined;
}

/**
 * Small, dependency-free wrapper around the Web Speech synthesis API. Keeping
 * this separate from the conversation state machine means a backend voice can
 * fail independently and reliably fall back to this instant local path.
 */
export function useSpeechSynthesis(): UseSpeechSynthesisResult {
  const [supported, setSupported] = useState(false);
  const [speaking, setSpeaking] = useState(false);
  const resolveRef = useRef<(() => void) | null>(null);

  useEffect(() => {
    setSupported(speechSupported());
    return () => {
      if (speechSupported()) window.speechSynthesis.cancel();
      resolveRef.current?.();
      resolveRef.current = null;
    };
  }, []);

  const cancel = useCallback(() => {
    if (speechSupported()) window.speechSynthesis.cancel();
    setSpeaking(false);
    resolveRef.current?.();
    resolveRef.current = null;
  }, []);

  const speak = useCallback(async (text: string) => {
    if (!text.trim() || !speechSupported()) return;
    cancel();
    await new Promise<void>((resolve) => {
      const utterance = new SpeechSynthesisUtterance(text);
      const finish = () => {
        setSpeaking(false);
        if (resolveRef.current === finish) resolveRef.current = null;
        resolve();
      };
      resolveRef.current = finish;
      utterance.rate = 1;
      utterance.onend = finish;
      // Some engines report an error when cancel() interrupts a prior utterance;
      // for the loop it is equivalent to speech having ended.
      utterance.onerror = finish;
      setSpeaking(true);
      window.speechSynthesis.speak(utterance);
    });
  }, [cancel]);

  return { supported, speaking, speak, cancel };
}
