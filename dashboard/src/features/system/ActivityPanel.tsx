"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Badge, Button, EmptyState, ErrorState, Icon, Input, Loading, Select, TableRoot } from "@/design";
import { fetchAgentActivity } from "./client";
import type { ActivityRow } from "./types";
import { fmtDate } from "./shared";
import styles from "./system.module.css";

const SOURCE_TONES: Record<string, "queued" | "challenger" | "promote"> = {
  session: "queued",
  attempt: "challenger",
  improvement: "promote",
};

export function ActivityPanel({ agentNames }: { agentNames: string[] }) {
  const [agent, setAgent] = useState("");
  const [day, setDay] = useState("");
  const [rows, setRows] = useState<ActivityRow[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const seq = useRef(0);

  const refresh = useCallback(async () => {
    const my = ++seq.current;
    setLoading(true);
    try {
      const result = await fetchAgentActivity({
        agent: agent || undefined,
        day: day || undefined,
        limit: 200,
      });
      if (my !== seq.current) return;
      setRows(result.activity);
      setError(null);
    } catch (reason) {
      if (my !== seq.current) return;
      setError(reason instanceof Error ? reason.message : "Unable to load activity.");
    } finally {
      if (my === seq.current) setLoading(false);
    }
  }, [agent, day]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return (
    <div>
      <div className={styles.filterBar}>
        <div className={styles.filterField}>
          <span className={styles.filterLabel}>Agent</span>
          <Select
            aria-label="Filter by agent"
            value={agent}
            onChange={setAgent}
            placeholder="All agents"
            options={[{ value: "", label: "All agents" }, ...agentNames.map((n) => ({ value: n, label: n }))]}
          />
        </div>
        <div className={styles.filterField}>
          <span className={styles.filterLabel}>Day</span>
          <Input
            aria-label="Filter by day"
            type="date"
            value={day}
            onChange={(e) => setDay(e.target.value)}
          />
        </div>
        <Button size="sm" variant="ghost" onClick={() => void refresh()}>
          <Icon name="radio" size={14} /> Refresh
        </Button>
      </div>

      {loading && !rows.length ? <Loading label="Loading activity…" /> : null}
      {error ? <ErrorState message={`Could not load activity: ${error}`} onRetry={() => void refresh()} /> : null}

      {!loading && !error && !rows.length ? (
        <EmptyState
          icon={<Icon name="radio" size={22} />}
          title="No activity"
          message={
            agent || day
              ? "No sessions, swarm attempts, or improvement-log entries match these filters."
              : "No agent activity recorded yet — sessions and swarm attempts populate this timeline as they run."
          }
        />
      ) : rows.length ? (
        <TableRoot>
          <thead>
            <tr>
              <th scope="col">When</th>
              <th scope="col">Source</th>
              <th scope="col">Model / by</th>
              <th scope="col">Effort</th>
              <th scope="col">Status</th>
              <th scope="col">Detail</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row, i) => (
              <tr key={`${row.ref_id ?? row.source}-${row.ts}-${i}`}>
                <td className={styles.mutedCell}>{fmtDate(row.ts)}</td>
                <td>
                  <Badge tone={SOURCE_TONES[row.source] ?? "neutral"}>{row.source}</Badge>
                </td>
                <td className={styles.mutedCell}>{row.model ?? row.improver ?? row.provider ?? "-"}</td>
                <td className={styles.mutedCell}>{row.effort ?? "-"}</td>
                <td className={styles.mutedCell}>{row.status ?? "-"}</td>
                <td className={styles.summaryCell}>
                  {row.summary || (row.change_count != null ? `${row.change_count} changes` : "")}
                </td>
              </tr>
            ))}
          </tbody>
        </TableRoot>
      ) : null}
    </div>
  );
}
