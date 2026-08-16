"use client";

import { useEffect, useState, type FormEvent, type KeyboardEvent } from "react";
import { Button, Icon, Tooltip, cx } from "@/design";
import { apiUrl } from "@/lib/apiRoute";
import { API_BASE } from "@/lib/contracts";
import { fetchWithTimeout } from "@/lib/fetchTimeout";
import { VoiceConversationMode } from "@/features/voice/VoiceConversationMode";
import { useSpeechDictation } from "@/features/voice/useSpeechDictation";
import { SpeedSlider, type Speed } from "./SpeedSlider";
import styles from "./cockpit.module.css";

/**
 * The front-door composer: one big "tell me what you want" field with the existing
 * Web-Speech mic. Submitting sends the goal straight to the quick intake path; the
 * work lands on the live board below with no clarification or confirmation dialog.
 *
 * The routing controls collapsed (D12) to ONE speed↔quality slider: topology
 * (fastlane vs solo vs swarm) is the router's decision, so the composer sends
 * NO execute — the payload is exactly {goal, speed} (+ plan when Plan-first is
 * ticked). A leading "solo:" / "swarm:" in the brief is the force hatch. The
 * backend still tolerates the old execute/executor/priority/category/lane
 * fields for automation; the UI just stops sending them.
 */
const QUICK_TIMEOUT_MS = 15_000;

/** What each detent means for THIS brief — kept honest under either topology
 *  outcome (the router may still run any speed as solo or swarm). */
const SPEED_HINTS: Record<Speed, string> = {
  fast: "Fastest models with lighter review — the router still picks solo vs swarm per brief.",
  auto: "The router decides models, tiers and solo-vs-swarm. Start a brief with “solo:” or “swarm:” to force topology.",
  ultra: "Max reasoning with full review gates — up to 3 parallel Fable sessions when a swarm pays off.",
};

async function quickRequest(payload: {
  goal: string;
  speed: Speed;
  plan?: boolean;
}): Promise<{ message?: string }> {
  const response = await fetchWithTimeout(
    apiUrl(API_BASE, "/api/intake/quick", "POST"),
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      cache: "no-store",
    },
    QUICK_TIMEOUT_MS,
  );
  if (!response.ok) {
    let detail = response.statusText || "Dispatch failed";
    try {
      const body = (await response.json()) as { error?: { message?: string }; message?: string };
      detail = body.error?.message ?? body.message ?? detail;
    } catch {
      // A malformed error response still produces useful inline feedback.
    }
    throw new Error(detail);
  }
  return (await response.json()) as { message?: string };
}

export function CommandComposer() {
  const [text, setText] = useState("");
  const [speed, setSpeed] = useState<Speed>("auto");
  const [planFirst, setPlanFirst] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [feedback, setFeedback] = useState<{ tone: "success" | "error"; text: string } | null>(null);
  const [voiceConversationOpen, setVoiceConversationOpen] = useState(false);
  const dictation = useSpeechDictation((finalTranscript) => {
    setText((current) => (current.trim() ? `${current.trim()} ${finalTranscript}` : finalTranscript));
  });

  const trimmed = text.trim();

  useEffect(() => {
    if (feedback?.tone !== "success") return;
    const timer = window.setTimeout(() => setFeedback(null), 4_000);
    return () => window.clearTimeout(timer);
  }, [feedback]);

  const submitQuick = async () => {
    if (!trimmed || submitting) return;
    setSubmitting(true);
    setFeedback(null);
    try {
      const payload: Parameters<typeof quickRequest>[0] = {
        goal: trimmed,
        speed,
      };
      if (planFirst) payload.plan = true;
      const result = await quickRequest(payload);
      setText("");
      setFeedback({
        tone: "success",
        text: result.message ?? (planFirst ? "Plan is ready on the board ↓" : "On it — tracking on the board ↓"),
      });
    } catch (reason) {
      setFeedback({
        tone: "error",
        text: reason instanceof Error ? reason.message : "Dispatch failed. Please try again.",
      });
    } finally {
      setSubmitting(false);
    }
  };

  const onSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    void submitQuick();
  };

  // Enter submits; Shift+Enter inserts a newline (matches the "one field" feel).
  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      void submitQuick();
    }
  };

  const micTitle = dictation.supported
    ? dictation.listening
      ? "Stop dictation"
      : "Speak instead of typing"
    : "Voice input needs Chrome or Edge — type your request instead.";

  return (
    <>
      <form className={styles.composerCard} onSubmit={onSubmit}>
        <textarea
          className={styles.textarea}
          value={text}
          onChange={(event) => setText(event.target.value)}
          onKeyDown={onKeyDown}
          placeholder="Tell me what you want done…"
          aria-label="What do you want done?"
          rows={2}
          autoFocus
        />
        {dictation.listening ? (
          <p className={styles.dictationStatus} aria-live="polite">
            Listening… {dictation.interim || "say something"}
          </p>
        ) : null}
        {dictation.errorMessage ? (
          <p className={styles.dictationError} role="alert">
            {dictation.errorMessage}
          </p>
        ) : null}
        <div className={styles.composerControls} aria-label="Speed and plan-first controls">
          <SpeedSlider speed={speed} onSpeedChange={setSpeed} />
          <Tooltip content="Create a PENDING plan first, don't execute immediately.">
            <label className={styles.planToggle}>
              <input
                type="checkbox"
                checked={planFirst}
                onChange={(event) => setPlanFirst(event.target.checked)}
              />
              <span>Plan first</span>
            </label>
          </Tooltip>
        </div>
        <p className={styles.controlsHint}>{SPEED_HINTS[speed]}</p>
        {feedback ? (
          <p className={feedback.tone === "success" ? styles.dispatchSuccess : styles.dispatchError} role={feedback.tone === "error" ? "alert" : "status"}>
            {feedback.text}
          </p>
        ) : null}
        <div className={styles.composerRow}>
          <div className={styles.composerActions}>
            <Tooltip content={micTitle}>
              <button
                type="button"
                className={cx("ds-icon-btn", dictation.listening && "ds-icon-btn--recording")}
                onClick={dictation.toggle}
                disabled={!dictation.supported}
                aria-pressed={dictation.listening}
                aria-label={dictation.listening ? "Stop voice input" : "Start voice input"}
              >
                <Icon name="mic" size={18} />
              </button>
            </Tooltip>
            <Button type="button" variant="secondary" onClick={() => setVoiceConversationOpen(true)}>
              <Icon name="mic" size={16} /> Voice
            </Button>
            <Button type="submit" disabled={!trimmed || submitting}>
              <Icon name="sparkles" size={16} /> {submitting ? "Sending…" : "Get it done"}
            </Button>
          </div>
        </div>
      </form>
      <VoiceConversationMode open={voiceConversationOpen} onClose={() => setVoiceConversationOpen(false)} />
    </>
  );
}
