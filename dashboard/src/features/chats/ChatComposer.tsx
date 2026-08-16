"use client";

/**
 * ChatComposer — the control deck, rebuilt on the prototype's composer card
 * (`.composer` + `.composer-foot`): one rounded raised card holding the
 * auto-grow textarea and, beneath it, the deck.
 *
 * Row 1: textarea (Enter send, ⇧Enter newline), @skill mentions, 📎 attach.
 *        ↑ on an empty composer recalls the last user message for editing.
 * Row 2 (the foot): ModelPicker (⌘J) · Proceed|Plan (⌘.) ·
 *        Balanced|Solo|Fan-out (⌘⇧M cycles) + fan-out count · send.
 *
 * CUT in this rebuild: the intent-suggestion row and the per-draft
 * `POST /api/chats/{id}/intent` it fired on every settled draft. It was a
 * shadow experiment whose result changed nothing the operator could see, and
 * it put an LLM call behind ordinary typing. Nothing else about send discipline
 * moved: the draft is still cleared ONLY on a successful send.
 *
 * The work-folder select is no longer here — it lives in the ctx-row directly
 * above this card (ChatSurface owns it), per the prototype.
 */

import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type KeyboardEvent,
  type ChangeEvent,
} from "react";
import { Icon } from "@/design";
import type { Attachment, OrchMode, RoutingPrefs, SkillTreeEntry } from "./chatApi";
import { ModelPicker } from "./ModelPicker";
import {
  applyMention,
  extractMentionQuery,
  searchSkills,
  type MentionQuery,
} from "./skillMention";
import styles from "./chatShell.module.css";

export interface DeckState {
  model: string | null;
  justThisMessage: boolean;
  planMode: boolean;
  orchMode: OrchMode;
  fanoutCount: number;
  /** Filesystem work folder for this chat's files (ctx-row). */
  workFolder: string | null;
}

interface ChatComposerProps {
  onSend: (content: string, deck: DeckState, attachments: Attachment[]) => Promise<void>;
  onUploadFiles: (files: File[]) => Promise<Array<{ name: string; path: string }>>;
  skills: SkillTreeEntry[];
  sending: boolean;
  /** Whether the chat has a companion task (enables file uploads). */
  hasBoardTask: boolean;
  deck: DeckState;
  onDeckChange: (patch: Partial<DeckState>) => void;
  routing: RoutingPrefs;
  onRoutingSave: (routing: RoutingPrefs) => Promise<void>;
  /** Steer-when-live hint (§2.5): the linked session is active. */
  steeringLive?: boolean;
  /** Last user message content, for the ↑ recall binding. */
  lastUserMessage?: string | null;
}

const ORCH_CYCLE: OrchMode[] = ["auto", "solo", "fanout"];

export function ChatComposer({
  onSend,
  onUploadFiles,
  skills,
  sending,
  hasBoardTask,
  deck,
  onDeckChange,
  routing,
  onRoutingSave,
  steeringLive = false,
  lastUserMessage = null,
}: ChatComposerProps) {
  const [draft, setDraft] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [modelPickerOpen, setModelPickerOpen] = useState(false);

  // Mention popover
  const [mentionQuery, setMentionQuery] = useState<MentionQuery | null>(null);
  const [mentionResults, setMentionResults] = useState<SkillTreeEntry[]>([]);
  const [mentionActive, setMentionActive] = useState(0);
  const [showMention, setShowMention] = useState(false);

  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const mentionPopoverRef = useRef<HTMLDivElement>(null);

  // Auto-resize textarea
  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 220)}px`;
  }, [draft]);

  // Deck keyboard map (§2.7): ⌘J picker · ⌘. mode · ⌘⇧M orchestration cycle
  useEffect(() => {
    const handler = (e: KeyboardEvent | globalThis.KeyboardEvent) => {
      if (!(e.metaKey || e.ctrlKey)) return;
      // An open dialog (rename / routing / delete) owns the keyboard.
      if (document.querySelector('[role="dialog"]')) return;
      if (e.key === "j") {
        e.preventDefault();
        setModelPickerOpen((prev) => !prev);
      } else if (e.key === ".") {
        e.preventDefault();
        onDeckChange({ planMode: !deck.planMode });
      } else if ((e.key === "M" || (e.key === "m" && e.shiftKey)) && e.shiftKey) {
        e.preventDefault();
        const next =
          ORCH_CYCLE[(ORCH_CYCLE.indexOf(deck.orchMode) + 1) % ORCH_CYCLE.length];
        onDeckChange({ orchMode: next });
      }
    };
    document.addEventListener("keydown", handler as EventListener);
    return () => document.removeEventListener("keydown", handler as EventListener);
  }, [deck.planMode, deck.orchMode, onDeckChange]);

  // ── Mention detection ──

  const handleInputChange = useCallback(
    (e: ChangeEvent<HTMLTextAreaElement>) => {
      const value = e.target.value;
      setDraft(value);
      setError(null);

      const cursorPos = e.target.selectionStart ?? value.length;
      const query = extractMentionQuery(value, cursorPos);
      if (query) {
        setMentionQuery(query);
        const results = searchSkills(skills, query.query);
        setMentionResults(results);
        setMentionActive(0);
        setShowMention(results.length > 0);
      } else {
        setShowMention(false);
        setMentionQuery(null);
      }
    },
    [skills],
  );

  const insertMention = useCallback(
    (skill: SkillTreeEntry) => {
      if (!mentionQuery) return;
      const result = applyMention(draft, mentionQuery, skill);
      setDraft(result.text);
      setShowMention(false);
      setMentionQuery(null);

      setAttachments((prev) => [
        ...prev,
        { kind: "skill" as const, ref: skill.id, label: `@${skill.name}` },
      ]);

      requestAnimationFrame(() => {
        if (textareaRef.current) {
          textareaRef.current.selectionStart = result.cursor;
          textareaRef.current.selectionEnd = result.cursor;
          textareaRef.current.focus();
        }
      });
    },
    [draft, mentionQuery],
  );

  // ── Send ──
  // Declared before handleKeyDown and wrapped in useCallback so the Enter
  // handler never calls a STALE copy (an Enter right after attaching a file
  // used to send without the attachment; a stale `sending` allowed doubles).
  const handleSend = useCallback(async () => {
    const content = draft.trim();
    if (!content || sending) return;
    setError(null);
    try {
      await onSend(content, deck, attachments);
      // Draft clears ONLY on success (§2.8: a failed send never clears it)
      setDraft("");
      setAttachments([]);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to send message.");
    }
  }, [draft, sending, onSend, deck, attachments]);

  // ── Keyboard handling ──

  const handleKeyDown = useCallback(
    (e: KeyboardEvent<HTMLTextAreaElement>) => {
      if (showMention && mentionResults.length > 0) {
        if (e.key === "ArrowDown") {
          e.preventDefault();
          setMentionActive((prev) => (prev + 1) % mentionResults.length);
          return;
        }
        if (e.key === "ArrowUp") {
          e.preventDefault();
          setMentionActive((prev) => (prev - 1 + mentionResults.length) % mentionResults.length);
          return;
        }
        if (e.key === "Enter" || e.key === "Tab") {
          e.preventDefault();
          const skill = mentionResults[mentionActive];
          if (skill) insertMention(skill);
          return;
        }
        if (e.key === "Escape") {
          e.preventDefault();
          setShowMention(false);
          return;
        }
      }

      // ↑ on an empty composer recalls the last user message (§2.7)
      if (e.key === "ArrowUp" && !draft && lastUserMessage) {
        e.preventDefault();
        setDraft(lastUserMessage);
        return;
      }

      // Esc clears the draft (Esc chain: mention → draft → drawer/dialog)
      if (e.key === "Escape" && draft) {
        e.preventDefault();
        e.stopPropagation();
        setDraft("");
        return;
      }

      // Send on Enter (not Shift+Enter)
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        void handleSend();
      }
    },
    [showMention, mentionResults, mentionActive, insertMention, draft, lastUserMessage, handleSend],
  );

  // Close mention popover on outside click
  useEffect(() => {
    if (!showMention) return;
    const handler = (e: MouseEvent) => {
      if (
        mentionPopoverRef.current &&
        !mentionPopoverRef.current.contains(e.target as Node) &&
        textareaRef.current &&
        !textareaRef.current.contains(e.target as Node)
      ) {
        setShowMention(false);
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [showMention]);

  // ── File upload ──

  const handleFileSelect = useCallback(
    async (e: ChangeEvent<HTMLInputElement>) => {
      const files = Array.from(e.target.files ?? []);
      if (!files.length) return;
      try {
        const saved = await onUploadFiles(files);
        const newAttachments: Attachment[] = saved.map((s) => ({
          kind: "file" as const,
          ref: s.path,
          label: s.name,
        }));
        setAttachments((prev) => [...prev, ...newAttachments]);
      } catch (err) {
        setError(err instanceof Error ? err.message : "Could not upload files.");
      }
      if (fileInputRef.current) fileInputRef.current.value = "";
    },
    [onUploadFiles],
  );

  const removeAttachment = useCallback((index: number) => {
    setAttachments((prev) => prev.filter((_, i) => i !== index));
  }, []);

  return (
    <div className={styles.composer}>
      {steeringLive ? (
        <p className={styles.steerHint} role="status">
          agent running — messages steer the live session
        </p>
      ) : null}

      {/* Attachment chips */}
      {attachments.length > 0 && (
        <div className={styles.composerAttachments}>
          {attachments.map((att, i) => (
            <span key={`${att.kind}-${att.ref}-${i}`} className={styles.composerAttachmentChip}>
              {att.kind === "skill" ? "@" : "📎"} {att.label}
              <button
                className={styles.chipDismiss}
                onClick={() => removeAttachment(i)}
                aria-label={`Remove ${att.label}`}
              >
                ×
              </button>
            </span>
          ))}
        </div>
      )}

      <div className={styles.composerInputWrap}>
        <textarea
          ref={textareaRef}
          className={styles.composerInput}
          placeholder="Message… type @skill to attach a skill"
          value={draft}
          onChange={handleInputChange}
          onKeyDown={handleKeyDown}
          disabled={sending}
          rows={1}
        />
        {showMention && mentionResults.length > 0 && mentionQuery && (
          <div ref={mentionPopoverRef} className={styles.mentionPopover} role="listbox">
            {mentionResults.map((skill, i) => (
              <button
                key={skill.id}
                className={`${styles.mentionItem}${
                  i === mentionActive ? ` ${styles.mentionItemActive}` : ""
                }`}
                role="option"
                aria-selected={i === mentionActive}
                onClick={() => insertMention(skill)}
              >
                <span className={styles.mentionItemName}>{skill.name}</span>
                {skill.description && (
                  <span className={styles.mentionItemDesc}>{skill.description}</span>
                )}
              </button>
            ))}
          </div>
        )}
      </div>

      {/* The deck (prototype .composer-foot) */}
      <div className={styles.composerFoot}>
        <ModelPicker
          value={deck.model}
          justThisMessage={deck.justThisMessage}
          onJustThisMessageChange={(next) => onDeckChange({ justThisMessage: next })}
          onSelect={(modelId) => onDeckChange({ model: modelId })}
          routing={routing}
          onRoutingSave={onRoutingSave}
          open={modelPickerOpen}
          onOpenChange={setModelPickerOpen}
        />

        <div className={styles.seg} role="group" aria-label="Plan gate">
          {(["proceed", "plan"] as const).map((mode) => (
            <button
              key={mode}
              type="button"
              className={`${styles.segBtn}${
                (deck.planMode ? "plan" : "proceed") === mode ? ` ${styles.segBtnOn}` : ""
              }`}
              aria-pressed={(deck.planMode ? "plan" : "proceed") === mode}
              onClick={() => onDeckChange({ planMode: mode === "plan" })}
              title="Plan gate (⌘.)"
            >
              {mode === "proceed" ? "Proceed" : "Plan"}
            </button>
          ))}
        </div>

        <div className={styles.seg} role="group" aria-label="Orchestration">
          {ORCH_CYCLE.map((mode) => (
            <button
              key={mode}
              type="button"
              className={`${styles.segBtn}${deck.orchMode === mode ? ` ${styles.segBtnOn}` : ""}`}
              aria-pressed={deck.orchMode === mode}
              onClick={() => onDeckChange({ orchMode: mode })}
              title="Orchestration (⌘⇧M cycles)"
            >
              {mode === "auto" ? "Balanced" : mode === "solo" ? "Solo" : "Fan-out"}
            </button>
          ))}
        </div>

        {deck.orchMode === "fanout" ? (
          <div className={styles.stepper}>
            <button
              type="button"
              className={styles.stepperButton}
              aria-label="Fewer agents"
              onClick={() => onDeckChange({ fanoutCount: Math.max(1, deck.fanoutCount - 1) })}
            >
              −
            </button>
            <span className={styles.stepperValue} aria-label="Fan-out count">
              {deck.fanoutCount}
            </span>
            <button
              type="button"
              className={styles.stepperButton}
              aria-label="More agents"
              onClick={() => onDeckChange({ fanoutCount: Math.min(10, deck.fanoutCount + 1) })}
            >
              +
            </button>
          </div>
        ) : null}

        {hasBoardTask && (
          <>
            <input
              ref={fileInputRef}
              type="file"
              multiple
              className={styles.hiddenInput}
              onChange={(e) => void handleFileSelect(e)}
              tabIndex={-1}
            />
            <button
              type="button"
              className={styles.stepperButton}
              onClick={() => fileInputRef.current?.click()}
              aria-label="Attach files"
              title="Attach files"
            >
              <Icon name="plus" size={14} />
            </button>
          </>
        )}

        <span className={styles.composerHint}>⏎ send · ⇧⏎ newline</span>

        <button
          type="button"
          className={styles.sendBtn}
          onClick={() => void handleSend()}
          disabled={sending || !draft.trim()}
          aria-label="Send"
          title="Send (⏎)"
        >
          {sending ? "…" : "↑"}
        </button>
      </div>

      {error && (
        <span className={styles.composerError} role="alert">
          {error}
        </span>
      )}
    </div>
  );
}
