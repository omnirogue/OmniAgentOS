"use client";

import Link from "next/link";
import { useEffect } from "react";
import { Badge, Button, Card, DefinitionList, StatusDot } from "@/design";
import type { LiveBoardTask } from "@/features/collab/types";
import { attemptModels, elapsedLabel, flattenAttempts, liveSessionCount, type SwarmRunGroup } from "./runAggregate";
import type { SwarmRunDetail } from "./types";

/**
 * The run drill-down (level 2 of the user's three-level design: card →
 * THIS modal → full-screen details). Left: the member-task TODOLIST with live
 * "Now:" lines. Right: run stats (sessions, models, elapsed, cost, budget,
 * concurrency). Footer: Open full view (root card's details page — every
 * transcript), Pause run, Archive run.
 */

function memberIcon(status: string): { state: "queued" | "running" | "completed" | "failed"; dim: boolean } {
  if (["claimed", "in_progress"].includes(status)) return { state: "running", dim: false };
  if (status === "done") return { state: "completed", dim: false };
  if (["blocked", "cancelled"].includes(status)) return { state: "failed", dim: false };
  return { state: "queued", dim: true };
}

export function SwarmRunModal({
  group,
  detail,
  onClose,
  onPause,
  onArchive,
  busy,
}: {
  group: SwarmRunGroup;
  detail: SwarmRunDetail | null;
  onClose: () => void;
  onPause: (group: SwarmRunGroup) => void;
  onArchive: (group: SwarmRunGroup) => void;
  busy: boolean;
}) {
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const run = detail?.run;
  const attempts = flattenAttempts(detail?.attempts);
  const sessions = liveSessionCount(attempts);
  const models = attemptModels(attempts);
  const elapsed = elapsedLabel(run?.started_at ?? run?.created_at, run?.finished_at);
  const rootId = group.root?.id ?? run?.board_task_id ?? null;
  const live = group.running > 0;
  const statusLabel = String(run?.status ?? (live ? "running" : group.column)).replace(/_/g, " ");
  const members: Array<Pick<LiveBoardTask, "id" | "title" | "status"> & { now?: string | null }> = group.members.map((task) => ({
    id: task.id,
    title: task.title,
    status: task.status,
    now: task.work?.current_step ?? null,
  }));

  return (
    <div
      role="presentation"
      onClick={onClose}
      style={{ position: "fixed", inset: 0, background: "color-mix(in srgb, var(--text) 45%, transparent)", display: "flex", alignItems: "center", justifyContent: "center", padding: "var(--space-4)", zIndex: 50 }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label={`Swarm run ${group.title}`}
        onClick={(event) => event.stopPropagation()}
        style={{ width: "min(940px, 96vw)", maxHeight: "90vh", display: "flex", flexDirection: "column", background: "var(--surface)", borderRadius: "var(--radius-lg, 14px)", border: "1px solid var(--border)", boxShadow: "0 24px 64px -12px color-mix(in srgb, var(--text) 40%, transparent)", overflow: "hidden" }}
      >
        <div style={{ padding: "var(--space-4) var(--space-4) 0", display: "flex", flexDirection: "column", gap: "var(--space-2)" }}>
          <div style={{ display: "flex", alignItems: "center", gap: "var(--space-2)" }}>
            <Badge tone="promote">⣿ Swarm</Badge>
            <code style={{ fontSize: "0.72rem", color: "var(--text-faint)" }}>{group.runId}</code>
            <span style={{ flex: 1 }} />
            <Button variant="ghost" size="sm" aria-label="Close" onClick={onClose}>✕</Button>
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: "var(--space-3)", flexWrap: "wrap" }}>
            <h2 style={{ margin: 0, fontSize: "var(--font-size-lg)", lineHeight: 1.3 }}>{group.title}</h2>
            <Badge tone={live ? "running" : group.column === "completed" ? "completed" : group.column === "blocked" ? "danger" : "neutral"}>{statusLabel}</Badge>
          </div>
        </div>
        <div style={{ display: "flex", flex: 1, minHeight: 0, borderTop: "1px solid var(--border)", marginTop: "var(--space-3)" }}>
          <div style={{ flex: 1.55, minWidth: 0, padding: "var(--space-3) var(--space-4)", overflowY: "auto", display: "flex", flexDirection: "column", gap: "var(--space-3)" }}>
            <div>
              <div style={{ display: "flex", alignItems: "baseline", gap: "var(--space-2)", marginBottom: "var(--space-2)" }}>
                <span style={{ fontSize: "0.72rem", fontWeight: 700, letterSpacing: ".07em", color: "var(--text-faint)" }}>SUB-TASKS</span>
                <span style={{ fontSize: "var(--font-size-sm)", color: "var(--text-muted)", fontVariantNumeric: "tabular-nums" }}>{group.done} of {group.total} complete</span>
              </div>
              <Card padding="sm">
                <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "grid", gap: "var(--space-2)" }}>
                  {members.map((member) => {
                    const icon = memberIcon(member.status);
                    return (
                      <li key={member.id} style={{ display: "flex", alignItems: "flex-start", gap: "var(--space-2)", opacity: icon.dim ? 0.6 : 1 }}>
                        <StatusDot state={icon.state} />
                        <span style={{ minWidth: 0 }}>
                          <Link href={`/activity/${encodeURIComponent(member.id)}?kind=board`} style={{ color: "inherit" }}>
                            <span style={{ fontWeight: ["claimed", "in_progress"].includes(member.status) ? 600 : 400, textDecoration: member.status === "done" ? "line-through" : "none" }}>{member.title}</span>
                          </Link>
                          {member.now ? <span style={{ display: "block", color: "var(--text-muted)", fontSize: "var(--font-size-sm)" }}>Now: {member.now}</span> : null}
                        </span>
                        <Badge tone={icon.state === "running" ? "running" : icon.state === "completed" ? "completed" : icon.state === "failed" ? "danger" : "neutral"}>{member.status.replace(/_/g, " ")}</Badge>
                      </li>
                    );
                  })}
                  {!members.length ? <li style={{ color: "var(--text-muted)" }}>Planning — member tasks appear as the planner provisions them.</li> : null}
                </ul>
              </Card>
            </div>
            {run?.error ? (
              <Card padding="sm"><p style={{ margin: 0, color: "var(--danger)", fontSize: "var(--font-size-sm)" }}>{run.error}</p></Card>
            ) : null}
          </div>
          <div style={{ width: 295, flex: "none", borderLeft: "1px solid var(--border)", background: "var(--overlay)", padding: "var(--space-3) var(--space-4)", overflowY: "auto", display: "flex", flexDirection: "column", gap: "var(--space-3)" }}>
            <div>
              <div style={{ fontSize: "0.72rem", fontWeight: 700, letterSpacing: ".07em", color: "var(--text-faint)", marginBottom: "var(--space-2)" }}>RUN STATS</div>
              <DefinitionList items={[
                { label: "Terminal sessions", value: attempts.length ? `${sessions} live · ${attempts.length} total` : "—" },
                { label: "Models", value: models.length ? models.join(", ") : "—" },
                { label: "Elapsed", value: elapsed ?? "—" },
                { label: "Cost", value: typeof run?.cost_usd === "number" ? `$${run.cost_usd.toFixed(2)}` : "—" },
                { label: "Budget", value: typeof run?.budget_usd_max === "number" && run.budget_usd_max > 0 ? `$${run.budget_usd_max.toFixed(2)}` : "—" },
                { label: "Concurrency", value: run?.target_concurrency ? String(run.target_concurrency) : "—" },
                { label: "Progress", value: `${group.done}/${group.total} · ${group.running} running${group.blocked ? ` · ${group.blocked} blocked` : ""}` },
              ]} />
            </div>
          </div>
        </div>
        <div style={{ padding: "var(--space-3) var(--space-4)", borderTop: "1px solid var(--border)", display: "flex", alignItems: "center", gap: "var(--space-2)" }}>
          {rootId ? (
            <Link href={`/activity/${encodeURIComponent(rootId)}?kind=board`} style={{ fontWeight: 600, fontSize: "var(--font-size-sm)" }}>
              Open full view ↗
            </Link>
          ) : null}
          <span style={{ flex: 1 }} />
          {live || ["backlog", "ready"].includes(group.column) ? (
            <Button variant="secondary" size="sm" disabled={busy} onClick={() => onPause(group)}>{busy ? "Working…" : "Pause run"}</Button>
          ) : null}
          <Button variant="secondary" size="sm" disabled={busy} onClick={() => onArchive(group)}>{busy ? "Working…" : "Archive run"}</Button>
        </div>
      </div>
    </div>
  );
}
