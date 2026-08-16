"use client";

import { useCallback, useRef, useState } from "react";
import { Button, EmptyState, ErrorState, Icon, Loading, Page, PageHeader } from "@/design";
import { FileSearchPanel } from "@/features/filesearch";
import { MountBrowser, MountGrid, useMounts, type Mount } from "@/features/mounts";

/**
 * /files — search-first. A single search box (Name or Semantic mode), root +
 * category filters and a ranked results table sit at the top and give the
 * operator the same file-finding reach the agents have. The original read-only
 * mounts browser is kept below in a disclosure that collapses itself the first
 * time a search goes active, so browsing stays one click away without competing
 * with the search flow.
 */
export default function FilesPage() {
  const [mountsOpen, setMountsOpen] = useState(true);
  // Auto-collapse the mounts browser the first time a search goes active, but
  // only once — after that the operator's own toggle wins.
  const autoCollapsed = useRef(false);

  const handleActiveChange = useCallback((active: boolean) => {
    if (active && !autoCollapsed.current) {
      autoCollapsed.current = true;
      setMountsOpen(false);
    }
  }, []);

  return (
    <Page>
      <PageHeader
        eyebrow="Files"
        title="Find files"
        lead="Search every filesystem root configured on this machine — Desktop, iCloud, Google Drive and the repo — by name or by meaning. Read-only; nothing here writes to disk."
      />

      <FileSearchPanel onActiveChange={handleActiveChange} />

      <section
        aria-label="Browse machine mounts"
        style={{
          marginTop: "var(--space-6)",
          borderTop: "1px solid var(--border)",
          paddingTop: "var(--space-4)",
        }}
      >
        <Button
          variant="ghost"
          size="sm"
          aria-expanded={mountsOpen}
          onClick={() => setMountsOpen((open) => !open)}
        >
          <span style={{ display: "inline-flex", alignItems: "center", gap: "var(--space-1)" }}>
            <Icon
              name="chevronRight"
              size={14}
              style={{
                transform: mountsOpen ? "rotate(90deg)" : "none",
                transition: "transform var(--motion-fast, 120ms) ease",
              }}
            />
            Browse machine mounts
          </span>
        </Button>
        {mountsOpen ? <MountsBrowseSection /> : null}
      </section>
    </Page>
  );
}

/** The original read-only mounts browser: a grid of every configured machine
 * root, then a directory browser once one is selected. Unchanged behavior —
 * lifted out of the page body so the search surface reads first. */
function MountsBrowseSection() {
  const { mounts, loading, error, refresh } = useMounts();
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const selectedMount: Mount | null = selectedId
    ? (mounts.find((m) => m.id === selectedId) ?? null)
    : null;

  return (
    <div style={{ marginTop: "var(--space-3)", display: "grid", gap: "var(--space-3)" }}>
      {selectedMount ? (
        <p style={{ margin: 0, color: "var(--text-muted)", fontSize: "var(--text-small)" }}>
          {selectedMount.path}
        </p>
      ) : (
        <p style={{ margin: 0, color: "var(--text-muted)", fontSize: "var(--text-small)" }}>
          Every filesystem root configured in configs/mounts.yaml — local drives and
          cloud-synced folders. Read-only.
        </p>
      )}

      {loading ? <Loading variant="skeleton" label="Loading mounts…" lines={4} /> : null}
      {!loading && error ? <ErrorState message={error} onRetry={() => void refresh()} /> : null}

      {!loading && !error ? (
        mounts.length === 0 ? (
          <EmptyState title="No mounts configured" message="No filesystem roots are declared in configs/mounts.yaml." />
        ) : selectedMount ? (
          <MountBrowser key={selectedMount.id} mount={selectedMount} onBack={() => setSelectedId(null)} />
        ) : (
          <MountGrid mounts={mounts} onSelect={(mount) => setSelectedId(mount.id)} />
        )
      ) : null}
    </div>
  );
}
