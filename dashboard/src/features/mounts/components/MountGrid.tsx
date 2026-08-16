"use client";

import { Badge, Button, Card, EmptyState, Tooltip } from "@/design";
import { MOUNT_KIND_LABEL, type Mount } from "../types";

export function mountKindLabel(kind: string): string {
  return MOUNT_KIND_LABEL[kind] ?? kind;
}

/** Grid of mount cards — the /files landing view. Clicking a card (or its
 * "Browse" button) opens that mount's directory browser. A mount with
 * `exists: false` (e.g. iCloud not enabled on this machine) is still listed
 * but disabled, so the registry stays legible even when a drive is absent. */
export function MountGrid({ mounts, onSelect }: { mounts: Mount[]; onSelect: (mount: Mount) => void }) {
  if (!mounts.length) {
    return <EmptyState title="No mounts configured" message="configs/mounts.yaml declares no machine roots." />;
  }

  return (
    <div
      style={{
        display: "grid",
        gridTemplateColumns: "repeat(auto-fill, minmax(260px, 1fr))",
        gap: "var(--space-3)",
      }}
    >
      {mounts.map((mount) => (
        <Card
          key={mount.id}
          raised
          padding="md"
          style={{
            display: "grid",
            gap: "var(--space-2)",
            opacity: mount.exists ? 1 : 0.6,
            cursor: mount.exists ? "pointer" : "default",
          }}
          onClick={mount.exists ? () => onSelect(mount) : undefined}
        >
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: "var(--space-2)" }}>
            <strong>{mount.label}</strong>
            {!mount.exists ? <Badge tone="danger">not present</Badge> : null}
          </div>
          <div style={{ display: "flex", flexWrap: "wrap", gap: "var(--space-1)" }}>
            <Badge tone="neutral">{mountKindLabel(mount.kind)}</Badge>
            {mount.cloud ? (
              <Tooltip content="Dataless files may download on first read — expect latency.">
                <Badge tone="warn">cloud</Badge>
              </Tooltip>
            ) : null}
            {mount.read_only ? <Badge tone="neutral">read-only</Badge> : null}
            {!mount.grantable ? (
              <Tooltip content="Browse-only — never a project write-grant root.">
                <Badge tone="neutral">not grantable</Badge>
              </Tooltip>
            ) : null}
          </div>
          <p style={{ color: "var(--text-muted)", margin: 0, fontSize: "var(--font-size-sm)", wordBreak: "break-all" }}>
            {mount.path}
          </p>
          {mount.notes ? (
            <p style={{ color: "var(--text-faint)", margin: 0, fontSize: "var(--font-size-sm)" }}>{mount.notes}</p>
          ) : null}
          <div>
            <Button size="sm" variant="secondary" disabled={!mount.exists} onClick={() => onSelect(mount)}>
              Browse
            </Button>
          </div>
        </Card>
      ))}
    </div>
  );
}
