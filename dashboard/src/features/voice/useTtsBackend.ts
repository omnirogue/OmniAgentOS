"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { fetchWithTimeout } from "@/lib/fetchTimeout";
import { API_BASE } from "@/lib/contracts";

export type TtsProvider = "browser" | "elevenlabs" | "xai";

export interface UseTtsBackendResult {
  playing: boolean;
  /** True when provider audio played; false signals the caller to use browser TTS. */
  play: (text: string, provider: TtsProvider, preferences?: { voice?: string; speed?: number }) => Promise<boolean>;
  stop: () => void;
}

const TTS_TIMEOUT_MS = 30_000;

/**
 * Optional server-hosted voices. API keys never leave the route handlers; a
 * failed or unconfigured provider simply returns false so callers can speak
 * through the browser immediately instead.
 */
export function useTtsBackend(): UseTtsBackendResult {
  const [playing, setPlaying] = useState(false);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const objectUrlRef = useRef<string | null>(null);
  const resolveRef = useRef<((played: boolean) => void) | null>(null);

  const stop = useCallback(() => {
    audioRef.current?.pause();
    audioRef.current = null;
    if (objectUrlRef.current) URL.revokeObjectURL(objectUrlRef.current);
    objectUrlRef.current = null;
    setPlaying(false);
    resolveRef.current?.(false);
    resolveRef.current = null;
  }, []);

  useEffect(() => stop, [stop]);

  const play = useCallback(async (
    text: string,
    provider: TtsProvider,
    preferences: { voice?: string; speed?: number } = {},
  ): Promise<boolean> => {
    if (provider === "browser" || !text.trim()) return false;
    stop();
    try {
      const response = await fetchWithTimeout(`${API_BASE}/api/voice/speak`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          text,
          provider,
          voice_id: preferences.voice,
        }),
      }, TTS_TIMEOUT_MS);
      if (!response.ok) return false;
      const data = await response.json() as { ok?: boolean; artifact_id?: string };
      if (!data.ok || !data.artifact_id) return false;

      const audioResponse = await fetchWithTimeout(`${API_BASE}/api/voice/audio/${encodeURIComponent(data.artifact_id)}`, {
        method: "GET",
      }, TTS_TIMEOUT_MS);
      if (!audioResponse.ok || !audioResponse.headers.get("content-type")?.startsWith("audio/")) return false;

      const url = URL.createObjectURL(await audioResponse.blob());
      objectUrlRef.current = url;
      const audio = new Audio(url);
      audioRef.current = audio;
      setPlaying(true);
      return await new Promise<boolean>((resolve) => {
        const finish = (played: boolean) => {
          if (audioRef.current === audio) audioRef.current = null;
          if (objectUrlRef.current === url) {
            URL.revokeObjectURL(url);
            objectUrlRef.current = null;
          }
          setPlaying(false);
          if (resolveRef.current === finish) resolveRef.current = null;
          resolve(played);
        };
        resolveRef.current = finish;
        audio.onended = () => finish(true);
        audio.onerror = () => finish(false);
        void audio.play().catch(() => finish(false));
      });
    } catch {
      return false;
    }
  }, [stop]);

  return { playing, play, stop };
}
