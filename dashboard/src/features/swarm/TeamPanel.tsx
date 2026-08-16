"use client";

import { Badge, EmptyState, Loading, StatusDot } from "@/design";
import type { SwarmTeam } from "./types";
import styles from "./swarm.module.css";

function roleTone(role: string): "ok" | "warn" | "neutral" {
  switch (role) {
    case "integrator":
    case "reviewer":
      return "ok";
    case "leader":
      return "warn";
    default:
      return "neutral";
  }
}

function fmtTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "—";
  const mins = Math.round((Date.now() - t) / 60_000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  return `${Math.floor(mins / 60)}h ${mins % 60}m ago`;
}

/**
 * Live team board: how many agents are running, who the leader/formation is,
 * and which workers were spawned. Cheap REST poll — not a token stream.
 */
export function TeamPanel({
  team,
  loading,
  stale,
  onOpenSession,
}: {
  team: SwarmTeam | null;
  loading: boolean;
  stale: boolean;
  onOpenSession?: (sessionId: string) => void;
}) {
  if (loading && !team) {
    return <Loading variant="skeleton" label="Loading team" lines={3} />;
  }

  if (!team || (team.active_swarms === 0 && team.running_agents === 0)) {
    return (
      <EmptyState
        title="No parallel agents running"
        message="When a swarm leader plans and spawns workers, they appear here with provider, model, and session — without streaming every token."
      />
    );
  }

  const live = team.live_workers?.length ? team.live_workers : team.workers.filter((w) => w.live);

  return (
    <div className={styles.teamPanel}>
      {stale ? (
        <div className={styles.banner} role="status">
          Team view may be stale — last poll failed
        </div>
      ) : null}

      <div className={styles.utilRow}>
        <div className={styles.teamStat}>
          <span className={styles.teamStatValue}>{team.running_agents}</span>
          <span className={styles.teamStatLabel}>running now</span>
        </div>
        <div className={styles.teamStat}>
          <span className={styles.teamStatValue}>{team.claimed_agents}</span>
          <span className={styles.teamStatLabel}>claimed</span>
        </div>
        <div className={styles.teamStat}>
          <span className={styles.teamStatValue}>{team.active_swarms}</span>
          <span className={styles.teamStatLabel}>active swarms</span>
        </div>
      </div>

      {team.leaders.length > 0 ? (
        <div className={styles.teamSection}>
          <h4 className={styles.teamSectionTitle}>Leaders / formations</h4>
          <ul className={styles.teamList}>
            {team.leaders.map((leader) => (
              <li key={leader.swarm_run_id} className={styles.teamRow}>
                <StatusDot
                  state={leader.status === "running" ? "running" : "awaiting"}
                  title={leader.status ?? "unknown"}
                />
                <div className={styles.teamMain}>
                  <div className={styles.teamTitle}>
                    {leader.goal || leader.swarm_run_id}
                  </div>
                  <div className={styles.teamMeta}>
                    {leader.formation ? (
                      <>
                        formation <strong>{leader.formation.id}</strong>
                        {leader.formation.implementers?.length
                          ? ` · implementers ${leader.formation.implementers.join(", ")}`
                          : null}
                        {leader.formation.reviewer
                          ? ` · reviewer ${leader.formation.reviewer}`
                          : null}
                        {leader.formation.topology
                          ? ` · topo ${leader.formation.topology}`
                          : null}
                      </>
                    ) : (
                      "formation not recorded"
                    )}
                    {leader.target_concurrency != null
                      ? ` · target ×${leader.target_concurrency}`
                      : null}
                  </div>
                </div>
                {leader.formation?.low_confidence ? (
                  <Badge tone="warn" title={leader.formation.reason ?? "low confidence formation"}>
                    low conf
                    {leader.formation.confidence != null
                      ? ` ${(leader.formation.confidence * 100).toFixed(0)}%`
                      : ""}
                  </Badge>
                ) : null}
                <Badge tone="neutral">{leader.status ?? "—"}</Badge>
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      <div className={styles.teamSection}>
        <h4 className={styles.teamSectionTitle}>
          Agents {live.length ? `(${live.length} live)` : ""}
        </h4>
        {live.length === 0 ? (
          <EmptyState
            title="No live workers"
            message="Workers will list here as the leader spawns them."
          />
        ) : (
          <ul className={styles.teamList}>
            {live.map((w) => (
              <li
                key={`${w.task_id}-${w.attempt_id ?? w.session_id ?? "x"}`}
                className={styles.teamRow}
              >
                <StatusDot state={w.live ? "running" : "queued"} label={w.role} />
                <div className={styles.teamMain}>
                  <div className={styles.teamTitle}>{w.task_title || w.task_id}</div>
                  <div className={styles.teamMeta}>
                    <Badge tone={roleTone(w.role)}>{w.role}</Badge>{" "}
                    {[w.provider, w.model].filter(Boolean).join(" · ") || "unassigned"}
                    {w.tier ? ` · tier ${w.tier}` : null}
                    {w.started_at ? ` · ${fmtTime(w.started_at)}` : null}
                  </div>
                </div>
                {w.session_id && onOpenSession ? (
                  <button
                    type="button"
                    className={styles.linkButton}
                    onClick={() => onOpenSession(w.session_id!)}
                  >
                    Open
                  </button>
                ) : (
                  <span className={styles.mono}>{w.session_id?.slice(0, 10) ?? "—"}</span>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>

      {team.recent_spawns.length > 0 ? (
        <div className={styles.teamSection}>
          <h4 className={styles.teamSectionTitle}>Recent spawns</h4>
          <ul className={styles.teamFeed}>
            {team.recent_spawns.slice(0, 12).map((ev, i) => {
              const p = ev.payload || {};
              const title =
                (p.task_title as string) ||
                (p.task_id as string) ||
                ev.action;
              const who = [p.provider, p.model].filter(Boolean).join(" · ");
              return (
                <li key={`${ev.event_id ?? i}-${ev.ts}`} className={styles.teamFeedItem}>
                  <span className={styles.mono}>{fmtTime(ev.ts)}</span>
                  <span>
                    spawned <strong>{String(title).slice(0, 80)}</strong>
                    {who ? ` → ${who}` : null}
                    {p.role ? ` (${String(p.role)})` : null}
                    {p.running_count != null
                      ? ` · fleet now ${String(p.running_count)}`
                      : null}
                  </span>
                </li>
              );
            })}
          </ul>
        </div>
      ) : null}

      {team.leader_updates.length > 0 ? (
        <div className={styles.teamSection}>
          <h4 className={styles.teamSectionTitle}>Leader updates</h4>
          <ul className={styles.teamFeed}>
            {team.leader_updates.slice(0, 10).map((ev, i) => (
              <li key={`${ev.event_id ?? i}-lu`} className={styles.teamFeedItem}>
                <span className={styles.mono}>{fmtTime(ev.ts)}</span>
                <span>
                  <strong>{ev.action}</strong>
                  {ev.payload?.reason
                    ? ` — ${String(ev.payload.reason).slice(0, 120)}`
                    : null}
                  {ev.payload?.task_id
                    ? ` · task ${String(ev.payload.task_id).slice(0, 24)}`
                    : null}
                </span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </div>
  );
}
