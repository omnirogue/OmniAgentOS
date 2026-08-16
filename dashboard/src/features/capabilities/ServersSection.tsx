"use client";

import { useMemo } from "react";
import { Badge, EmptyState, ErrorState, Loading, Section, Table, type BadgeTone, type TableColumn } from "@/design";
import styles from "./capabilities.module.css";
import { useServers } from "./hooks";
import type { ServerInfo } from "./types";

/** Status is free text (e.g. "ACTIVE (live commerce)") -- group and badge by prefix. */
type StatusGroup = "ACTIVE" | "EPHEMERAL" | "LEGACY-STALE" | "OTHER";

function statusGroup(status: string): StatusGroup {
  if (status.startsWith("ACTIVE")) return "ACTIVE";
  if (status.startsWith("EPHEMERAL")) return "EPHEMERAL";
  if (status.startsWith("LEGACY-STALE")) return "LEGACY-STALE";
  return "OTHER";
}

const STATUS_ORDER: StatusGroup[] = ["ACTIVE", "EPHEMERAL", "LEGACY-STALE", "OTHER"];

const STATUS_LABEL: Record<StatusGroup, string> = {
  ACTIVE: "Active fleet",
  EPHEMERAL: "Ephemeral",
  "LEGACY-STALE": "Legacy / stale",
  OTHER: "Other",
};

const STATUS_TONE: Record<StatusGroup, BadgeTone> = {
  ACTIVE: "ok",
  EPHEMERAL: "warn",
  "LEGACY-STALE": "neutral",
  OTHER: "neutral",
};

function serverColumns(): TableColumn<ServerInfo>[] {
  return [
    {
      key: "alias",
      header: "Alias",
      sortable: true,
      render: (row) => <span style={{ fontWeight: 600 }}>{row.alias}</span>,
      sortValue: (row) => row.alias,
    },
    { key: "host", header: "IP / host", render: (row) => <span className={styles.connectorEnv}>{row.host}</span> },
    { key: "user", header: "User", render: (row) => row.user },
    { key: "key", header: "Key", render: (row) => <span className={styles.connectorEnv}>{row.key}</span> },
    { key: "purpose", header: "Runs", render: (row) => <span className={styles.muted}>{row.purpose || "—"}</span> },
    { key: "sites", header: "Sites", render: (row) => <span className={styles.muted}>{row.sites || "—"}</span> },
    {
      key: "status",
      header: "Status",
      render: (row) => {
        const group = statusGroup(row.status);
        return (
          <Badge tone={STATUS_TONE[group]} title={row.status}>
            {row.status}
          </Badge>
        );
      },
    },
  ];
}

/**
 * the operator's server fleet (vault/servers/inventory.md via GET /api/access/servers), grouped
 * ACTIVE / EPHEMERAL / LEGACY-STALE. Key material is never fetched or rendered here --
 * only the key FILENAME (e.g. "~/.ssh/example_a.pem"), same boundary the capability catalogue
 * keeps for connector env var NAMES.
 */
export function ServersSection() {
  const { servers, loading, error, refresh } = useServers();

  const grouped = useMemo(() => {
    const byGroup = new Map<StatusGroup, ServerInfo[]>();
    for (const server of servers) {
      const group = statusGroup(server.status);
      const list = byGroup.get(group) ?? [];
      list.push(server);
      byGroup.set(group, list);
    }
    return byGroup;
  }, [servers]);

  const columns = useMemo(() => serverColumns(), []);

  return (
    <Section
      eyebrow="Infrastructure"
      title="Servers"
      description="the operator's server fleet, from vault/servers/inventory.md. Key paths only -- never key material."
    >
      {loading ? <Loading variant="skeleton" label="Loading server inventory" lines={4} /> : null}
      {!loading && error ? <ErrorState message={error} onRetry={refresh} /> : null}
      {!loading && !error && servers.length === 0 ? (
        <EmptyState title="No servers documented" message="vault/servers/inventory.md has no rows." />
      ) : null}
      {!loading && !error && servers.length > 0
        ? STATUS_ORDER.map((group) => {
            const rows = grouped.get(group);
            if (!rows || rows.length === 0) return null;
            return (
              <div key={group} className={styles.catalogGroup}>
                <div className={styles.catalogGroupHead}>
                  <h3 className={styles.connectorTitle}>{STATUS_LABEL[group]}</h3>
                  <Badge tone={STATUS_TONE[group]}>{rows.length}</Badge>
                </div>
                <Table
                  columns={columns}
                  rows={rows}
                  rowKey={(row) => `${row.alias}-${row.host}`}
                  emptyMessage="No servers in this group"
                />
              </div>
            );
          })
        : null}
    </Section>
  );
}
