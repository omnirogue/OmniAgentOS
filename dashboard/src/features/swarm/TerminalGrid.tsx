"use client";

import {
  Badge,
  Button,
  Card,
  EmptyState,
  Icon,
  Loading,
  StatusDot,
  Table,
  type BadgeTone,
  type StatusDotState,
  type TableColumn,
} from "@/design";
import type { Session } from "@/lib/contracts";
import styles from "./swarm.module.css";

const dotStates: Record<Session["state"], StatusDotState> = {
  starting: "queued",
  running: "running",
  awaiting_approval: "awaiting_approval",
  resuming: "running",
  completed: "completed",
  failed: "failed",
  cancelled: "cancelled",
  killed: "danger",
};

const badgeTones: Record<Session["state"], BadgeTone> = {
  starting: "queued",
  running: "running",
  awaiting_approval: "awaiting_approval",
  resuming: "running",
  completed: "completed",
  failed: "failed",
  cancelled: "cancelled",
  killed: "danger",
};

function shortId(id: string): string {
  return id.length > 18 ? `${id.slice(0, 17)}…` : id;
}

function runtime(session: Session): string {
  const start = Date.parse(session.created_at);
  if (!Number.isFinite(start)) return "—";
  const seconds = Math.max(0, Math.round((Date.now() - start) / 1000));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ${seconds % 60}s`;
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}

function cost(value: number | null): string {
  return value == null ? "—" : `$${value.toFixed(2)}`;
}

export function TerminalGrid({
  sessions,
  loading,
  unauthorized,
  stale,
  onOpenSession,
}: {
  sessions: Session[];
  loading: boolean;
  unauthorized: boolean;
  stale: boolean;
  onOpenSession: (session: Session) => void;
}) {
  if (loading && sessions.length === 0 && !unauthorized) {
    return <Loading variant="skeleton" label="Loading active terminals" lines={4} />;
  }

  if (unauthorized) {
    return (
      <Card>
        <EmptyState
          icon={<Icon name="plug" size={22} />}
          title="No local session token in this browser"
          message="Active terminals come from GET /api/sessions, which is only visible from the operator's authorized local session. This browser cannot show live session data — nothing is broken."
        />
      </Card>
    );
  }

  const columns: TableColumn<Session>[] = [
    {
      key: "session",
      header: "Terminal",
      sortable: true,
      sortValue: (row) => row.title ?? row.id,
      render: (row) => (
        <Button
          variant="ghost"
          size="sm"
          onClick={() => onOpenSession(row)}
          aria-label={`Open transcript for ${row.title ?? row.id}`}
        >
          {row.title ?? shortId(row.id)}
        </Button>
      ),
    },
    {
      key: "state",
      header: "State",
      sortable: true,
      sortValue: (row) => row.state,
      render: (row) => (
        <span className={styles.stateCell}>
          <StatusDot state={dotStates[row.state]} label={row.state.replaceAll("_", " ")} />
          <Badge tone={badgeTones[row.state]}>{row.state.replaceAll("_", " ")}</Badge>
        </span>
      ),
    },
    {
      key: "provider",
      header: "Provider / model",
      sortable: true,
      sortValue: (row) => row.provider,
      render: (row) => (
        <span className={styles.terminalCell}>
          <Badge tone="neutral">{row.provider}</Badge>
          {row.model ? <span className={styles.muted}>{row.model}</span> : null}
        </span>
      ),
    },
    {
      key: "runtime",
      header: "Runtime",
      align: "right",
      sortable: true,
      sortValue: (row) => Date.parse(row.created_at) || 0,
      render: (row) => runtime(row),
    },
    {
      key: "cost",
      header: "Cost",
      align: "right",
      sortable: true,
      sortValue: (row) => row.cost_usd,
      render: (row) => cost(row.cost_usd),
    },
  ];

  return (
    <>
      {stale ? (
        <div className={styles.banner} role="status">
          Reconnecting — terminal data may be stale
        </div>
      ) : null}
      {sessions.length === 0 ? (
        <Card>
          <EmptyState message="No active terminals right now. Running bridge and external sessions appear here." />
        </Card>
      ) : (
        <Card padding="none">
          <Table
            columns={columns}
            rows={sessions}
            rowKey={(row) => row.id}
            caption="Active swarm and session terminals"
          />
        </Card>
      )}
    </>
  );
}
