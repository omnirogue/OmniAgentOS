"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  Badge,
  Button,
  Card,
  EmptyState,
  ErrorState,
  Loading,
  Stat,
} from "@/design";
import { grokOpsApi } from "./api";
import styles from "./omni-ops.module.css";
import { isAnswerableInteractionStatus } from "./types";
import type { GrokInteraction } from "./types";

function InteractionCard({
  interaction,
  onAnswer,
  answering,
}: {
  interaction: GrokInteraction;
  onAnswer: (id: string, body: string) => void;
  answering: boolean;
}) {
  const [body, setBody] = useState("");
  const [showReply, setShowReply] = useState(false);

  const blocking = interaction.blocking_policy !== "none";
  const canAnswer = isAnswerableInteractionStatus(interaction.status);

  return (
    <Card>
      <div className={styles.interactionCard}>
        <div className={styles.interactionHeader}>
          <div>
            <div className={styles.interactionMeta}>
              <Badge tone={blocking ? "warn" : "neutral"}>{interaction.kind}</Badge>
              <Badge tone={interaction.direction === "user_to_agent" ? "ok" : "neutral"}>
                {interaction.direction}
              </Badge>
              {blocking ? <Badge tone="warn">blocking</Badge> : null}
            </div>
            <p className={styles.toolbarCount}>
              {interaction.work_ref_type}:{interaction.work_ref_id}
              {interaction.author ? ` · from ${interaction.author}` : null}
            </p>
          </div>
          <Badge category="runState" tone={interaction.status === "answered" ? "ok" : "warn"}>
            {interaction.status}
          </Badge>
        </div>

        <p className={styles.interactionBody}>{interaction.body}</p>

        {canAnswer ? (
          <div className={styles.interactionActions}>
            {showReply ? (
              <div className={styles.answerInput}>
                <textarea
                  aria-label="Answer text"
                  value={body}
                  onChange={(e) => setBody(e.target.value)}
                  placeholder="Type your response…"
                />
                <div className={styles.toolbar}>
                  <Button
                    size="sm"
                    variant="primary"
                    disabled={answering || !body.trim()}
                    onClick={() => {
                      onAnswer(interaction.id, body);
                      setBody("");
                      setShowReply(false);
                    }}
                  >
                    {answering ? "Sending…" : "Send answer"}
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    disabled={answering}
                    onClick={() => setShowReply(false)}
                  >
                    Cancel
                  </Button>
                </div>
              </div>
            ) : (
              <Button size="sm" variant="secondary" onClick={() => setShowReply(true)}>
                Reply
              </Button>
            )}
          </div>
        ) : null}
      </div>
    </Card>
  );
}

export function InteractionsInbox() {
  const [interactions, setInteractions] = useState<GrokInteraction[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [answering, setAnswering] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [blockingOnly, setBlockingOnly] = useState(false);
  const seqRef = useRef(0);

  const refresh = useCallback(async () => {
    const seq = ++seqRef.current;
    setLoading(true);
    setError(null);
    try {
      const data = await grokOpsApi.interactions({ blocking_only: blockingOnly });
      if (seq === seqRef.current) {
        setInteractions(data);
        setError(null);
      }
    } catch (err) {
      if (seq === seqRef.current) {
        setError(err instanceof Error ? err.message : "Failed to load interactions");
      }
    } finally {
      if (seq === seqRef.current) setLoading(false);
    }
  }, [blockingOnly]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const handleAnswer = async (id: string, body: string) => {
    setAnswering(true);
    setNotice(null);
    try {
      await grokOpsApi.answerInteraction(id, body, "operator");
      setNotice("Answer sent");
      await refresh();
    } catch (err) {
      setNotice(err instanceof Error ? err.message : "Failed to send answer");
    } finally {
      setAnswering(false);
    }
  };

  const blockingCount = interactions.filter(
    (i) => i.blocking_policy !== "none" && i.status !== "answered",
  ).length;
  // D-2 (LiveSim LS-004) audit find: `interactions` is never cleared on a
  // failed refresh, so "0 pending / 0 blocking" only means "confirmed empty"
  // when `error` is unset -- this is the same "invisible backlog" stakes as
  // the Approvals inbox: an unknown blocking count must never read as zero.
  const interactionsUnknown = Boolean(error) && interactions.length === 0;

  return (
    <>
      <div className={styles.statGrid}>
        <Stat label="Pending interactions" value={interactionsUnknown ? "—" : interactions.length} />
        <Stat label="Blocking" value={interactionsUnknown ? "—" : blockingCount} tone={blockingCount > 0 ? "warn" : "default"} />
      </div>

      {notice ? <div className={styles.notice}>{notice}</div> : null}

      <div className={styles.toolbar}>
        <Button
          size="sm"
          variant={blockingOnly ? "primary" : "secondary"}
          onClick={() => setBlockingOnly((v) => !v)}
        >
          {blockingOnly ? "Showing blocking only" : "Show blocking only"}
        </Button>
        <Button size="sm" variant="ghost" onClick={() => void refresh()} disabled={loading}>
          Refresh
        </Button>
      </div>

      {loading ? <Loading label="Loading interactions…" /> : null}
      {error ? <ErrorState title="Interactions unavailable" message={error} onRetry={refresh} /> : null}
      {!loading && !error && interactions.length === 0 ? (
        <EmptyState
          title="No pending interactions"
          message="The interaction inbox is clear. Agent nudges, questions, and blocking-policy interactions will appear here when they need operator input."
        />
      ) : null}
      {!loading && interactions.length > 0 ? (
        <div className={styles.cardGrid}>
          {interactions.map((interaction) => (
            <InteractionCard
              key={interaction.id}
              interaction={interaction}
              onAnswer={handleAnswer}
              answering={answering}
            />
          ))}
        </div>
      ) : null}
    </>
  );
}
