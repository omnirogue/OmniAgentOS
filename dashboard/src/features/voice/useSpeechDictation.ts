"use client";

import { useCallback, useEffect, useRef, useState } from "react";

export type DictationStatus = "idle" | "listening" | "unsupported" | "denied" | "error";

/** Cap on the live interim transcript shown to the user (UI hint only — final chunks are unaffected). */
export const MAX_INTERIM_LENGTH = 500;

export interface UseSpeechDictationResult {
  /** False when window.SpeechRecognition/webkitSpeechRecognition isn't available. */
  supported: boolean;
  listening: boolean;
  status: DictationStatus;
  /** Live, not-yet-final transcript for the current utterance (UI hint only). */
  interim: string;
  errorMessage: string | null;
  start: () => void;
  stop: () => void;
  toggle: () => void;
}

function recognitionCtor(): SpeechRecognitionConstructor | null {
  if (typeof window === "undefined") return null;
  return window.SpeechRecognition ?? window.webkitSpeechRecognition ?? null;
}

/**
 * Thin wrapper over the browser's Web Speech API. Zero new dependencies —
 * v1 dictation only. v2 (OpenAI Realtime API / whisper.cpp) can replace this
 * hook's internals with a server-backed recognizer without touching call
 * sites, since the return shape (supported/listening/start/stop) is generic.
 */
export function useSpeechDictation(
  onFinalResult: (transcript: string) => void,
  lang = "en-US",
): UseSpeechDictationResult {
  const [supported, setSupported] = useState(false);
  const [listening, setListening] = useState(false);
  const [status, setStatus] = useState<DictationStatus>("idle");
  const [interim, setInterim] = useState("");
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const recognitionRef = useRef<SpeechRecognition | null>(null);
  const onFinalResultRef = useRef(onFinalResult);
  onFinalResultRef.current = onFinalResult;

  useEffect(() => {
    setSupported(recognitionCtor() !== null);
  }, []);

  useEffect(() => {
    return () => {
      recognitionRef.current?.stop();
      recognitionRef.current = null;
    };
  }, []);

  const start = useCallback(() => {
    if (recognitionRef.current) return;
    const Ctor = recognitionCtor();
    if (!Ctor) {
      setSupported(false);
      setStatus("unsupported");
      return;
    }
    const recognition = new Ctor();
    recognition.lang = lang;
    recognition.continuous = true;
    recognition.interimResults = true;
    recognition.maxAlternatives = 1;

    recognition.onstart = () => {
      setListening(true);
      setStatus("listening");
      setErrorMessage(null);
    };
    recognition.onresult = (event) => {
      let finalChunk = "";
      let interimChunk = "";
      for (let i = event.resultIndex; i < event.results.length; i += 1) {
        const result = event.results[i];
        const transcript = result?.[0]?.transcript ?? "";
        if (result?.isFinal) finalChunk += transcript;
        else interimChunk += transcript;
      }
      if (finalChunk.trim()) onFinalResultRef.current(finalChunk.trim());
      const trimmedInterim = interimChunk.trim();
      setInterim(
        trimmedInterim.length > MAX_INTERIM_LENGTH
          ? `${trimmedInterim.slice(0, MAX_INTERIM_LENGTH)}… (capped at ${MAX_INTERIM_LENGTH} chars — pause to commit this chunk)`
          : trimmedInterim,
      );
    };
    recognition.onerror = (event) => {
      if (event.error === "not-allowed" || event.error === "service-not-allowed") {
        setStatus("denied");
        setErrorMessage("Microphone access was blocked. Allow it in your browser's site settings, then try again.");
      } else if (event.error === "no-speech" || event.error === "aborted") {
        // Not an error worth surfacing — the user just paused or stopped.
      } else {
        setStatus("error");
        setErrorMessage(`Voice dictation stopped (${event.error}).`);
      }
    };
    recognition.onend = () => {
      setListening(false);
      setInterim("");
      recognitionRef.current = null;
      setStatus((current) => (current === "denied" || current === "error" ? current : "idle"));
    };

    recognitionRef.current = recognition;
    try {
      recognition.start();
    } catch {
      recognitionRef.current = null;
      setListening(false);
    }
  }, [lang]);

  const stop = useCallback(() => {
    recognitionRef.current?.stop();
  }, []);

  const toggle = useCallback(() => {
    if (listening) stop();
    else start();
  }, [listening, start, stop]);

  return { supported, listening, status, interim, errorMessage, start, stop, toggle };
}
