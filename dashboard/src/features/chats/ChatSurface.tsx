"use client";

/**
 * ChatSurface — the ONE conversation primitive, mounted on /chats
 * (variant="page") and in the board's task drawer (variant="panel").
 *
 * Rebuilt on the prototype's chat-main: empty state ("What are we working on?"
 * + suggestion chips), thread, then the composer zone — ctx-row (work folder)
 * directly above the composer card.
 *
 * Thread behaviour is unchanged and non-negotiable: the useChatThread reducer
 * (SSE + 6s stall + 3s poll fallback), provisional-id dedupe, grow-a-chat on
 * first send, promote dialog, plan mode + PlanCard + reattach, and send-failure
 * discipline (the draft is never cleared on a failed send).
 *
 * What is new here:
 *   • message bodies AND the live streaming buffer render markdown
 *     (ChatMarkdown) — agents reply in markdown and the thread used to print
 *     raw asterisks;
 *   • the model badge prefers the model persisted on the turn;
 *   • the streaming bubble carries an ActivityStrip (elapsed · last SSE event ·
 *     stalled) in place of a bare caret.
 */

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { Badge, Button, Dialog, ErrorState, Icon, useToast } from "@/design";
import type { Attachment, Chat, ChatComposerMeta, ChatMessage } from "./chatApi";
import * as chatApi from "./chatApi";
import { ActivityStrip } from "./ActivityStrip";
import { ChatComposer, type DeckState } from "./ChatComposer";
import { ChatMarkdown } from "./ChatMarkdown";
import { PlanCard } from "./PlanCard";
import { SpawnCard } from "./SpawnCard";
import { ProjectSuggestionBar } from "./ProjectSuggestionBar";
import { WorkFolderSelect } from "./WorkFolderSelect";
import { useChatThread, useSkillsForMention } from "./useChats";
import { teamApi } from "@/features/team/client";
import { employeeName } from "@/features/team/types";
import { looksLikeNlAssign } from "./nlAssignGrammar";
import styles from "./chatShell.module.css";

export interface ChatSurfaceProps {
  chatId: string | null;
  variant: "page" | "panel";
  boardTaskId?: string | null;
  /** Quiet "messages steer the live session" hint (§2.5). */
  steeringLive?: boolean;
  /** Dock: fired when the first send grew a chat on this card. */
  onChatCreated?: (chat: Chat) => void;
  /** Page: promote flow lives in a page-level dialog. */
  onPromote?: (chatId: string) => void;
  /** Page: refresh the sidebar list after archive/delete. */
  onChatMutated?: () => void;
  /** Page: toggle the workspace drawer (⌘I). */
  onToggleWorkspace?: () => void;
  /** Page: rendered at the far left of the header (phone sidebar reveal). */
  headerLead?: ReactNode;
  projectOptions?: Array<{ value: string; label: string }>;
}

const EXAMPLE_PROMPTS = [
  "Review the last run and tell me what broke",
  "Sketch the plan for the next milestone",
  "Summarize what's blocking the board right now",
];

function relTime(value: string): string {
  if (!value) return "";
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? value
    : date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function parseAttachments(message: ChatMessage): Attachment[] {
  const atts = message.meta?.attachments;
  return Array.isArray(atts) ? atts : [];
}

/** The persisted model for a turn: the column when the server set it, else the
 * model recorded in the turn's meta (SSE-built rows carry it there). */
export function messageModel(message: ChatMessage): string | null {
  if (message.model) return message.model;
  const metaModel = message.meta?.model;
  return typeof metaModel === "string" && metaModel ? metaModel : null;
}

function composerMeta(deck: DeckState): ChatComposerMeta {
  return { work_folder: deck.workFolder };
}

export function ChatSurface({
  chatId: chatIdProp,
  variant,
  boardTaskId = null,
  steeringLive = false,
  onChatCreated,
  onPromote,
  onChatMutated,
  onToggleWorkspace,
  headerLead = null,
  projectOptions = [],
}: ChatSurfaceProps) {
  // Dock grow-a-chat: the first send creates the chat, then binds it.
  const [grownChatId, setGrownChatId] = useState<string | null>(null);
  const chatId = chatIdProp ?? grownChatId;
  const { push } = useToast();

  const {
    chat,
    messages,
    loading,
    error,
    sending,
    turn,
    suggestionDismissed,
    refresh,
    send,
    patch,
    uploadFiles,
    dismissSuggestion,
    applySuggestion,
    startPlan,
    confirmPlan,
    discardPlan,
    planJob,
    planBusy,
    planError,
  } = useChatThread(chatId);
  const { skills } = useSkillsForMention();

  const [deck, setDeck] = useState<DeckState>({
    model: null,
    justThisMessage: false,
    planMode: false,
    orchMode: "solo",
    fanoutCount: 3,
    workFolder: null,
  });
  const deckRef = useRef(deck);
  const lastKnownGoodFolder = useRef<string | null>(deck.workFolder);
  const scopeMutation = useRef(0);
  const [menuOpen, setMenuOpen] = useState(false);
  const [renameOpen, setRenameOpen] = useState(false);
  const [renameDraft, setRenameDraft] = useState("");
  const [deleteOpen, setDeleteOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);

  // Sticky deck state follows the loaded chat (§2.3: per-chat sticky)
  useEffect(() => {
    if (!chat) return;
    setDeck((prev) => {
      const next = {
        ...prev,
        model: chat.preferred_model,
        planMode: chat.plan_mode,
        orchMode: chat.orch_mode,
        workFolder: typeof chat.meta.work_folder === "string" ? chat.meta.work_folder : null,
      };
      deckRef.current = next;
      lastKnownGoodFolder.current = next.workFolder;
      return next;
    });
  }, [chat?.id, chat?.preferred_model, chat?.plan_mode, chat?.orch_mode]); // eslint-disable-line react-hooks/exhaustive-deps

  const handleDeckChange = useCallback(
    (patchPartial: Partial<DeckState>) => {
      const next = { ...deckRef.current, ...patchPartial };
      deckRef.current = next;
      setDeck(next);
      if (!chatId) return;

      // Persist sticky controls (model persists only when not "just this message")
      if (patchPartial.orchMode !== undefined) {
        void patch({ orch_mode: patchPartial.orchMode }).catch(() => undefined);
      }
      if (patchPartial.planMode !== undefined) {
        void patch({ plan_mode: patchPartial.planMode }).catch(() => undefined);
      }
      if (patchPartial.model !== undefined && !next.justThisMessage) {
        void patch({ model: patchPartial.model }).catch(() => undefined);
      }
      if (patchPartial.workFolder !== undefined) {
        const mutation = ++scopeMutation.current;
        const workFolder = next.workFolder;
        void patch({ meta: { work_folder: workFolder } })
          .then(() => {
            if (scopeMutation.current === mutation) lastKnownGoodFolder.current = workFolder;
          })
          .catch(() => {
            if (scopeMutation.current !== mutation) return;
            const restored = { ...deckRef.current, workFolder: lastKnownGoodFolder.current };
            deckRef.current = restored;
            setDeck(restored);
            push({ tone: "error", message: "Couldn't save the work folder" });
          });
      }
    },
    [chatId, patch, push],
  );

  // Anchored auto-scroll: only follow streaming within 64px of the bottom
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    if (distanceFromBottom < 64) {
      el.scrollTop = el.scrollHeight;
    }
  }, [messages, turn.text, sending]);

  // Close the ⋯ menu on outside click
  useEffect(() => {
    if (!menuOpen) return;
    const handler = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setMenuOpen(false);
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [menuOpen]);

  const lastUserMessage = useMemo(
    () => [...messages].reverse().find((m) => m.role === "user")?.content ?? null,
    [messages],
  );

  // ── Send routing: plan mode → plan job; fan-out → spawn path; else turn ──

  const handleSend = useCallback(
    async (content: string, deckState: DeckState, attachments: Attachment[]) => {
      // NL assignment/proposal intercept (M6 + automation backlog): a
      // sentence that reads as "give X a task to Y" / "assign X a task Y" /
      // "/task ..." / "propose an automation to Y" never reaches the chat
      // backend — it goes to the deterministic endpoint instead, and a
      // confirmation toast replaces the turn. The response is a
      // discriminated union (`TeamNlAssignResult`): a PROPOSAL
      // (`kind: "automation_proposal"`, no owner, awaiting the operator's approval)
      // gets its OWN confirmation — never the owner-assignment toast, which
      // would claim a person is now responsible for work nobody has
      // approved yet. Both variants reuse the server's own `message` text
      // (it already carries the disposition — category, assignee hint,
      // "awaiting the operator's approval") rather than re-deriving it client-side.
      // A failure here (an unparseable sentence, unknown teammate, unknown
      // #company/#category) is left to THROW: the composer's own send
      // handler already shows a thrown message inline and preserves the
      // draft on failure (§2.8) — exactly the behaviour a rejected sentence
      // should get, since the operator needs the text back to fix it, not a
      // chat message sent.
      if (looksLikeNlAssign(content)) {
        const result = await teamApi.nlAssign(content);
        if (result.kind === "automation_proposal") {
          push({ tone: "info", message: `📝 ${result.message}` });
        } else {
          push({
            tone: "success",
            message: `✅ Task ${result.task_id} created for ${employeeName(result.owner_employee_id) ?? result.owner_employee_id}: ${result.title}`,
          });
        }
        return;
      }
      if (!chatId) {
        // Grow-a-chat (§2.5): first send on a chatless card creates the chat
        // LINKED to the card — no companion task. The direct API call is
        // required here: the thread hook's send is still bound to chatId=null
        // for this render; its SSE/poll wiring picks the new id up next render.
        const created = await chatApi.createChat({
          title: "New chat",
          board_task_id: boardTaskId,
          meta: { work_folder: deckState.workFolder },
        });
        if (deckState.planMode) {
          // Persist the plan job id in meta so the re-attach effect in
          // useChatThread picks it up on next render (the plan was started
          // via seedPlan, not startPlan, since startPlan needs chatId to
          // already be bound to the hook).
          const seed = await chatApi.seedPlan(created.id, content);
          await chatApi
            .updateChat(created.id, {
              meta: { ...composerMeta(deckState), plan_job_id: seed.job_id },
            })
            .catch(() => undefined);
        } else {
          await chatApi.sendMessage(created.id, {
            content,
            model: deckState.model ?? undefined,
            orch_mode: deckState.orchMode === "solo" ? undefined : deckState.orchMode,
            count: deckState.orchMode === "fanout" ? deckState.fanoutCount : undefined,
            meta: {
              ...composerMeta(deckState),
              ...(attachments.length ? { attachments } : {}),
            },
          });
        }
        setGrownChatId(created.id);
        onChatCreated?.(created);
        return;
      }
      if (deckState.planMode) {
        await startPlan(content);
        return;
      }
      await send(content, {
        model: deckState.model,
        orchMode: deckState.orchMode === "solo" ? null : deckState.orchMode,
        count: deckState.orchMode === "fanout" ? deckState.fanoutCount : undefined,
        attachments,
        meta: composerMeta(deckState),
      });
      // The server titles the chat from the first message — refresh the
      // sidebar so it shows the real title instead of "New chat".
      if (!chat?.title || chat.title === "New chat") onChatMutated?.();
    },
    [chatId, boardTaskId, onChatCreated, onChatMutated, send, startPlan, chat?.title, push],
  );

  const retryLastMessage = useCallback(() => {
    if (lastUserMessage) {
      void handleSend(lastUserMessage, deck, []);
    }
  }, [lastUserMessage, handleSend, deck]);

  // ── Header actions ──

  const handleRename = useCallback(async () => {
    const title = renameDraft.trim();
    if (!title) return;
    try {
      await patch({ title });
      setRenameOpen(false);
      onChatMutated?.();
    } catch (reason) {
      push({
        tone: "error",
        title: "Could not rename",
        message: reason instanceof Error ? reason.message : "Unknown error",
      });
    }
  }, [renameDraft, patch, onChatMutated, push]);

  const handleArchive = useCallback(async () => {
    try {
      await patch({ status: "archived" });
      push({ tone: "success", message: "Chat archived" });
      onChatMutated?.();
    } catch (reason) {
      push({
        tone: "error",
        title: "Could not archive",
        message: reason instanceof Error ? reason.message : "Unknown error",
      });
    }
  }, [patch, onChatMutated, push]);

  const handleDelete = useCallback(async () => {
    if (!chatId) return;
    try {
      await chatApi.deleteChat(chatId);
      push({ tone: "success", message: "Chat deleted" });
      setDeleteOpen(false);
      onChatMutated?.();
    } catch (reason) {
      push({
        tone: "error",
        title: "Could not delete",
        message: reason instanceof Error ? reason.message : "Unknown error",
      });
    }
  }, [chatId, onChatMutated, push]);

  // ── Render ──

  const panel = variant === "panel";
  const turnActive =
    turn.state === "running" || turn.state === "queued" || turn.state === "stalled";

  const composerZone = (
    <div className={styles.composerZone}>
      <WorkFolderSelect
        value={deck.workFolder}
        onChange={(workFolder) => handleDeckChange({ workFolder })}
      />
      <ChatComposer
        onSend={handleSend}
        onUploadFiles={uploadFiles}
        skills={skills}
        sending={sending || turnActive}
        hasBoardTask={Boolean(chat?.board_task_id ?? boardTaskId)}
        deck={deck}
        onDeckChange={handleDeckChange}
        routing={chat?.routing ?? { allow: [], deny: [], speed: null, effort: null, hint: null }}
        onRoutingSave={async (next) => {
          await patch({ routing: next });
        }}
        steeringLive={steeringLive}
        lastUserMessage={lastUserMessage}
      />
    </div>
  );

  if (!chatId) {
    // No chat bound: the dock grows one on the card (§2.5); on /chats the
    // first message creates a fresh chat and the page binds it via
    // onChatCreated (URL + sidebar catch up immediately).
    return (
      <div className={`${styles.surface} ${panel ? styles.surfacePanel : ""}`}>
        {headerLead ? <div className={styles.mainToolbar}>{headerLead}</div> : null}
        <div className={styles.empty}>
          <span className={styles.emptyIcon} aria-hidden="true">
            <Icon name="sparkles" size={24} />
          </span>
          <h2 className={styles.emptyTitle}>
            {panel ? "Start a conversation about this task" : "What are we working on?"}
          </h2>
          <p className={styles.emptyText}>
            {panel
              ? "Your first message creates a chat linked to this card — it stays a visible board card."
              : "Pick a chat from the sidebar, or just start typing — your first message opens a new chat (⌘N works too)."}
          </p>
          <div className={styles.suggest}>
            {EXAMPLE_PROMPTS.map((prompt) => (
              <button
                key={prompt}
                type="button"
                className={styles.chip}
                onClick={() => void handleSend(prompt, deck, [])}
              >
                {prompt}
              </button>
            ))}
          </div>
        </div>
        {composerZone}
      </div>
    );
  }

  if (error && !messages.length && !loading) {
    return (
      <div className={`${styles.surface} ${panel ? styles.surfacePanel : ""}`}>
        <ErrorState title="Could not load chat" message={error} onRetry={refresh} />
      </div>
    );
  }

  const header = (
    <div className={`${styles.header} ${panel ? styles.headerPanel : ""}`}>
      {headerLead}
      <div className={styles.headerTitleWrap}>
        <span className={styles.headerTitle}>{chat?.title ?? "…"}</span>
        <div className={styles.headerSubline}>
          {chat?.project_name ? (
            <a
              className={styles.projectChip}
              href={chat.project_id ? `/board?project=${encodeURIComponent(chat.project_id)}` : "/board"}
            >
              {chat.project_name}
            </a>
          ) : null}
          {chat?.status === "promoted" ? <Badge tone="completed">promoted</Badge> : null}
          {chat?.status === "archived" ? <Badge tone="neutral">archived</Badge> : null}
        </div>
      </div>
      <div className={styles.headerActions}>
        {onToggleWorkspace ? (
          <Button variant="ghost" size="sm" onClick={onToggleWorkspace} title="Workspace (⌘I)">
            <Icon name="columns" size={14} />
          </Button>
        ) : null}
        {variant === "page" ? (
          <div className={styles.headerMenuWrap} ref={menuRef}>
            <Button
              variant="ghost"
              size="sm"
              onClick={() => setMenuOpen((prev) => !prev)}
              aria-haspopup="menu"
              aria-expanded={menuOpen}
              title="Chat actions"
            >
              ⋯
            </Button>
            {menuOpen ? (
              <div className={styles.headerMenu} role="menu">
                <button
                  className={styles.headerMenuItem}
                  role="menuitem"
                  onClick={() => {
                    setRenameDraft(chat?.title ?? "");
                    setRenameOpen(true);
                    setMenuOpen(false);
                  }}
                >
                  Rename…
                </button>
                {onPromote && chat?.status !== "promoted" ? (
                  <button
                    className={styles.headerMenuItem}
                    role="menuitem"
                    onClick={() => {
                      onPromote(chatId);
                      setMenuOpen(false);
                    }}
                  >
                    Promote to Board (⌘⇧P)
                  </button>
                ) : null}
                <button
                  className={styles.headerMenuItem}
                  role="menuitem"
                  disabled={chat?.status === "archived"}
                  onClick={() => {
                    void handleArchive();
                    setMenuOpen(false);
                  }}
                >
                  Archive
                </button>
                <button
                  className={styles.headerMenuItem}
                  role="menuitem"
                  onClick={() => {
                    setDeleteOpen(true);
                    setMenuOpen(false);
                  }}
                >
                  Delete…
                </button>
              </div>
            ) : null}
          </div>
        ) : null}
      </div>
    </div>
  );

  return (
    <div className={`${styles.surface} ${panel ? styles.surfacePanel : ""}`}>
      {header}

      {chat && !suggestionDismissed ? (
        <ProjectSuggestionBar
          chat={chat}
          projectOptions={projectOptions}
          onApply={async (projectId) => {
            await applySuggestion(projectId);
            onChatMutated?.();
          }}
          onDismiss={dismissSuggestion}
          variant={variant}
        />
      ) : null}

      <div className={styles.thread} ref={scrollRef}>
        <div className={styles.threadInner}>
          {loading && messages.length === 0 ? (
            <div className={styles.threadSkeletons} aria-label="Loading the thread">
              <div className={`${styles.skeletonBubble} ds-skeleton`} />
              <div className={`${styles.skeletonBubble} ${styles.skeletonBubbleAgent} ds-skeleton`} />
              <div className={`${styles.skeletonBubble} ds-skeleton`} />
            </div>
          ) : messages.length === 0 && turn.state === "idle" ? (
            <div className={styles.empty}>
              <span className={styles.emptyIcon} aria-hidden="true">
                <Icon name="sparkles" size={24} />
              </span>
              <h2 className={styles.emptyTitle}>What are we working on?</h2>
              <p className={styles.emptyText}>
                {`${deck.model ?? "auto"} · ${deck.planMode ? "Plan" : "Proceed"} · ${
                  deck.orchMode === "fanout"
                    ? "Fan-out"
                    : deck.orchMode === "auto"
                      ? "Balanced"
                      : "Solo"
                }`}
              </p>
              <div className={styles.suggest}>
                {EXAMPLE_PROMPTS.map((prompt) => (
                  <button
                    key={prompt}
                    type="button"
                    className={styles.chip}
                    onClick={() => void handleSend(prompt, deck, [])}
                  >
                    {prompt}
                  </button>
                ))}
              </div>
            </div>
          ) : (
            <>
              {messages.map((m) => (
                <MessageBubble key={m.id} message={m} />
              ))}

              {planJob || planBusy || planError ? (
                <PlanCard
                  job={planJob}
                  busy={planBusy}
                  error={planError}
                  projectOptions={projectOptions}
                  onConfirm={async (override) => {
                    await confirmPlan(override);
                    push({ tone: "success", title: "Plan confirmed", message: "Dispatched to the board." });
                  }}
                  onDiscard={discardPlan}
                />
              ) : null}

              {turn.fanoutTaskIds ? <SpawnCard taskIds={turn.fanoutTaskIds} /> : null}

              {/* Streaming agent reply */}
              {turnActive ? (
                <div className={`${styles.msg} ${styles.msgAgent}`}>
                  <div className={styles.msgHead}>
                    <span className={styles.msgRole}>Agent</span>
                    {turn.model ? <Badge tone="neutral">{turn.model}</Badge> : null}
                  </div>
                  <div className={`${styles.msgBubble} ${styles.msgStreaming}`}>
                    {turn.text ? (
                      <div className={styles.msgMarkdown}>
                        <ChatMarkdown text={turn.text} />
                        <span className={styles.streamCursor} aria-hidden="true" />
                      </div>
                    ) : (
                      <span className={styles.streamCursor} aria-hidden="true" />
                    )}
                    <ActivityStrip
                      startedAt={turn.startedAt}
                      lastEventType={turn.lastEventType}
                      state={turn.state}
                    />
                  </div>
                </div>
              ) : null}

              {turn.state === "timed_out" ? (
                <div className={styles.turnNotice} role="status">
                  <Badge tone="warn">Turn timed out</Badge>
                  <Button variant="secondary" size="sm" onClick={retryLastMessage}>
                    Retry
                  </Button>
                </div>
              ) : null}

              {error && messages.length ? (
                <div className={styles.turnNotice} role="alert">
                  <span className={styles.turnError}>The agent stopped: {error}</span>
                  <Button variant="secondary" size="sm" onClick={retryLastMessage}>
                    Retry
                  </Button>
                </div>
              ) : null}
            </>
          )}
        </div>
      </div>

      {composerZone}

      {/* Rename dialog (native prompt() is a charter violation) */}
      <Dialog
        open={renameOpen}
        onClose={() => setRenameOpen(false)}
        title="Rename chat"
        footer={
          <>
            <Button variant="ghost" onClick={() => setRenameOpen(false)}>
              Cancel
            </Button>
            <Button variant="primary" onClick={() => void handleRename()} disabled={!renameDraft.trim()}>
              Rename
            </Button>
          </>
        }
      >
        <input
          className={styles.dialogInput}
          value={renameDraft}
          onChange={(e) => setRenameDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") void handleRename();
          }}
          autoFocus
        />
      </Dialog>

      {/* Delete dialog (native confirm() is a charter violation) */}
      <Dialog
        open={deleteOpen}
        onClose={() => setDeleteOpen(false)}
        title="Delete this chat?"
        footer={
          <>
            <Button variant="ghost" onClick={() => setDeleteOpen(false)}>
              Cancel
            </Button>
            <Button variant="primary" onClick={() => void handleDelete()}>
              Delete
            </Button>
          </>
        }
      >
        <p className={styles.dialogHelp}>
          The chat is soft-deleted and disappears from the sidebar. Its companion
          board card stays.
        </p>
      </Dialog>
    </div>
  );
}

// ── MessageBubble ─────────────────────────────────────────────

function MessageBubble({ message }: { message: ChatMessage }) {
  const attachments = parseAttachments(message);
  const isUser = message.role === "user";
  const isSystem = message.role === "system";
  const model = messageModel(message);

  return (
    <div
      className={`${styles.msg} ${isUser ? styles.msgUser : isSystem ? styles.msgSystem : styles.msgAgent}`}
    >
      <div className={styles.msgHead}>
        <span className={styles.msgRole}>{isUser ? "You" : isSystem ? "System" : "Agent"}</span>
        {model ? <Badge tone="neutral">{model}</Badge> : null}
        {message.meta?.timed_out ? <Badge tone="warn">timed out</Badge> : null}
        <span className={styles.msgTime}>{relTime(message.created_at)}</span>
      </div>
      <div className={styles.msgBubble}>
        {isUser ? (
          // A typed message is literal text: rendering it as markdown would
          // eat an operator's asterisks and underscores.
          <div className={styles.msgPlain}>{message.content}</div>
        ) : (
          <div className={styles.msgMarkdown}>
            <ChatMarkdown text={message.content} />
          </div>
        )}
      </div>
      {attachments.length > 0 ? (
        <div className={styles.attachmentRow}>
          {attachments.map((att, i) => (
            <span key={`${att.kind}-${att.ref}-${i}`} className={styles.attachmentPill}>
              {att.kind === "skill" ? "@" : att.kind === "file" ? "📎" : "🔗"} {att.label}
            </span>
          ))}
        </div>
      ) : null}
    </div>
  );
}
