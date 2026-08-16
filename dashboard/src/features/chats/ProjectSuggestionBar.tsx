"use client";

/**
 * ProjectSuggestionBar (chat-v2 §2.4) — the auto-classify suggestion.
 *
 * Renders ONLY when confidence ≥ 0.5 AND the chat has no project AND the user
 * hasn't dismissed it. NEVER auto-moves: Move applies the suggestion, Choose…
 * opens a picker, Not now dismisses (§2.8: hidden on error, never pending).
 */

import { useState } from "react";
import { Button, Select } from "@/design";
import type { Chat } from "./chatApi";
import styles from "./chats.module.css";

interface ProjectSuggestionBarProps {
  chat: Chat;
  projectOptions: Array<{ value: string; label: string }>;
  onApply: (projectId: string) => Promise<void>;
  onDismiss: () => void;
  variant?: "page" | "panel";
}

export function ProjectSuggestionBar({
  chat,
  projectOptions,
  onApply,
  onDismiss,
}: ProjectSuggestionBarProps) {
  const [choosing, setChoosing] = useState(false);
  const [choice, setChoice] = useState("");
  const [busy, setBusy] = useState(false);

  const suggestion = chat.project_suggestion;
  if (
    !suggestion ||
    suggestion.confidence < 0.5 ||
    chat.project_id !== null ||
    !suggestion.project_id
  ) {
    return null;
  }

  const apply = async (projectId: string) => {
    setBusy(true);
    try {
      await onApply(projectId);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className={styles.suggestionBar} role="status">
      <span className={styles.suggestionText}>
        Looks like <strong>{suggestion.name}</strong>.
        {suggestion.rationale ? (
          <span className={styles.suggestionRationale}>
            {" "}
            — {suggestion.rationale}
          </span>
        ) : null}
      </span>
      <span className={styles.suggestionActions}>
        {choosing ? (
          <>
            <Select
              aria-label="Choose project"
              value={choice}
              onChange={setChoice}
              options={[{ value: "", label: "Choose…" }, ...projectOptions]}
            />
            <Button
              variant="primary"
              size="sm"
              disabled={!choice || busy}
              onClick={() => void apply(choice)}
            >
              Move
            </Button>
          </>
        ) : (
          <>
            <Button
              variant="primary"
              size="sm"
              disabled={busy}
              onClick={() => void apply(suggestion.project_id!)}
            >
              Move
            </Button>
            <Button variant="ghost" size="sm" onClick={() => setChoosing(true)}>
              Choose…
            </Button>
          </>
        )}
        <Button variant="ghost" size="sm" onClick={onDismiss}>
          Not now
        </Button>
      </span>
    </div>
  );
}
