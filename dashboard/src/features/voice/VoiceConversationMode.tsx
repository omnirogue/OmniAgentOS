"use client";

import { useState } from "react";
import { Badge, Button, Dialog, Icon, Select, Tooltip } from "@/design";
import { type TtsProvider } from "./useTtsBackend";
import { useVoiceConversation } from "./useVoiceConversation";
import styles from "./voice.module.css";

export interface VoiceConversationModeProps {
  open: boolean;
  onClose: () => void;
}

const OUTPUT_OPTIONS = [
  { value: "browser", label: "Browser voice (fast default)" },
  { value: "elevenlabs", label: "ElevenLabs (when configured)" },
  { value: "xai", label: "xAI voice (when configured)" },
];

const STATE_LABEL = {
  idle: "Ready",
  listening: "Listening…",
  thinking: "Fable is thinking…",
  speaking: "Fable is speaking…",
  unsupported: "Voice input unavailable",
} as const;

function stateTone(state: keyof typeof STATE_LABEL): "neutral" | "running" | "awaiting" | "warn" {
  if (state === "listening") return "running";
  if (state === "thinking" || state === "speaking") return "awaiting";
  if (state === "unsupported") return "warn";
  return "neutral";
}

/** A focused, hands-free alternative to the cockpit's one-shot dictation. */
export function VoiceConversationMode({ open, onClose }: VoiceConversationModeProps) {
  const [outputProvider, setOutputProvider] = useState<TtsProvider>("browser");
  const voice = useVoiceConversation({ enabled: open, outputProvider });
  const canListen = voice.speechRecognitionSupported;

  return (
    <Dialog open={open} onClose={onClose} title="Talk with Fable" className={styles.voiceDialog}>
      <div className={styles.voiceConversation}>
        <div className={styles.voiceStatusRow}>
          <Badge tone={stateTone(voice.state)} aria-live="polite">{voice.muted ? "Mic muted" : STATE_LABEL[voice.state]}</Badge>
          <span className={styles.voiceStatusHint}>
            {voice.speechSynthesisSupported ? "Fable replies out loud, then listens again." : "Replies remain visible in the transcript."}
          </span>
        </div>

        {!canListen ? (
          <p className={styles.voiceUnavailable} role="status">
            Voice input is not available in this browser. Try a current Chrome or Edge browser, allow microphone access, or use the normal cockpit composer.
          </p>
        ) : null}
        {!voice.speechSynthesisSupported ? (
          <p className={styles.voiceUnavailable} role="status">
            Browser speech output is unavailable, so Fable&apos;s replies will stay on screen.
          </p>
        ) : null}
        {voice.errorMessage ? <p className={styles.error} role="alert">{voice.errorMessage}</p> : null}

        <div className={styles.transcript} role="log" aria-live="polite" aria-label="Conversation transcript">
          {voice.messages.length === 0 ? (
            <p className={styles.transcriptEmpty}>Start listening, then speak naturally. Fable will ask follow-up questions and listen again when it finishes speaking.</p>
          ) : voice.messages.map((message) => (
            <article key={message.id} className={message.speaker === "user" ? styles.userTurn : styles.fableTurn}>
              <span className={styles.turnLabel}>{message.speaker === "user" ? "You" : "Fable"}</span>
              <p>{message.text}</p>
            </article>
          ))}
          {voice.interim ? (
            <p className={styles.interimTranscript}><span>You&apos;re saying:</span> {voice.interim}</p>
          ) : null}
        </div>

        <div className={styles.voiceControls}>
          <Tooltip content={voice.paused ? "Resume listening" : "Pause listening"}>
            <Button variant={voice.paused ? "primary" : "secondary"} onClick={voice.toggleListening} disabled={!canListen} aria-pressed={!voice.paused}>
              <Icon name="mic" size={16} /> {voice.paused ? "Resume mic" : "Pause mic"}
            </Button>
          </Tooltip>
          <Button variant="ghost" onClick={voice.toggleMuted} disabled={!canListen} aria-pressed={voice.muted}>
            {voice.muted ? "Unmute mic" : "Mute mic"}
          </Button>
          <Button variant="ghost" onClick={voice.clear}>Clear conversation</Button>
        </div>

        <Select
          label="Speech output"
          value={outputProvider}
          onChange={(value) => setOutputProvider(value as TtsProvider)}
          options={OUTPUT_OPTIONS}
          aria-label="Speech output provider"
        />
        {outputProvider !== "browser" ? <p className={styles.hint}>If this optional voice provider is unavailable, Fable automatically uses your browser&apos;s voice instead.</p> : null}
      </div>
    </Dialog>
  );
}
