"use client";

import { useEffect, useState } from "react";
import {
  Badge,
  Button,
  Card,
  Dialog,
  EmptyState,
  ErrorState,
  Icon,
  Loading,
  Table,
  type TableColumn,
} from "@/design";
import { fetchMountFileText, looksLikeImage, mountFileUrl, MountApiError } from "../client";
import { useMountDir } from "../hooks";
import type { Mount, MountDirEntry } from "../types";

function fileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(value < 10 ? 1 : 0)} ${units[unit]}`;
}

/** `mtime` is raw epoch SECONDS (see types.ts) — unlike features/projects's
 * ISO `modified` field, this needs the *1000 conversion before `Date`. */
function fileDate(mtime: number): string {
  const date = new Date(mtime * 1000);
  return Number.isNaN(date.getTime()) ? "-" : date.toLocaleString();
}

function joinPath(dir: string, name: string): string {
  return dir ? `${dir}/${name}` : name;
}

/** {label, path} for the root + every path segment, for the breadcrumb row. */
function crumbsFor(mount: Mount, path: string): Array<{ label: string; path: string }> {
  const crumbs = [{ label: mount.label, path: "" }];
  if (!path) return crumbs;
  const segments = path.split("/").filter(Boolean);
  let acc = "";
  for (const segment of segments) {
    acc = acc ? `${acc}/${segment}` : segment;
    crumbs.push({ label: segment, path: acc });
  }
  return crumbs;
}

/**
 * One mount's directory browser: breadcrumbs, a dirs-first table (lazy
 * per-directory fetch via `useMountDir`), offset/limit pagination with a
 * "load more" affordance while `truncated`, and a file-click preview dialog
 * mirroring FilesPanel's text/image preview UX in
 * features/projects/HierarchyViews.tsx (kept local rather than shared, since
 * the wire shapes differ — mtime is epoch seconds here, not ISO).
 */
export function MountBrowser({ mount, onBack }: { mount: Mount; onBack: () => void }) {
  const [path, setPath] = useState("");
  const { entries, total, truncated, loading, loadingMore, error, loadMore, refresh } = useMountDir(
    mount.id,
    path,
  );
  const [selected, setSelected] = useState<MountDirEntry | null>(null);
  const [preview, setPreview] = useState<string | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewError, setPreviewError] = useState<string | null>(null);

  // Reset the browser to the mount's own root whenever a different mount is
  // selected (MountBrowser is remounted per-mount by the /files page's key,
  // but this also protects a future direct-navigation caller).
  useEffect(() => {
    setPath("");
    setSelected(null);
  }, [mount.id]);

  const selectedIsImage = selected ? looksLikeImage(selected.name) : false;
  const selectedPath = selected ? joinPath(path, selected.name) : "";

  useEffect(() => {
    if (!selected || selectedIsImage) {
      setPreview(null);
      setPreviewError(null);
      setPreviewLoading(false);
      return;
    }
    let current = true;
    setPreview(null);
    setPreviewError(null);
    setPreviewLoading(true);
    fetchMountFileText(mount.id, selectedPath)
      .then((text) => {
        if (current) setPreview(text);
      })
      .catch((reason: unknown) => {
        if (!current) return;
        const message =
          reason instanceof MountApiError && reason.status === 415
            ? "This file type has no preview. Use Open / download instead."
            : reason instanceof Error
              ? reason.message
              : "Could not preview this file.";
        setPreviewError(message);
      })
      .finally(() => {
        if (current) setPreviewLoading(false);
      });
    return () => {
      current = false;
    };
  }, [mount.id, selectedPath, selected, selectedIsImage]);

  const columns: TableColumn<MountDirEntry>[] = [
    {
      key: "name",
      header: "Name",
      sortable: true,
      sortValue: (entry) => `${entry.is_dir ? "0" : "1"}${entry.name.toLowerCase()}`,
      render: (entry) => (
        <Button
          size="sm"
          variant="ghost"
          onClick={() => (entry.is_dir ? setPath(joinPath(path, entry.name)) : setSelected(entry))}
        >
          <span style={{ display: "inline-flex", alignItems: "center", gap: "var(--space-1)" }}>
            {entry.is_dir ? <Icon name="grid" size={14} /> : null}
            {entry.name}
          </span>
        </Button>
      ),
    },
    {
      key: "size",
      header: "Size",
      align: "right",
      sortable: true,
      sortValue: (entry) => (entry.is_dir ? -1 : entry.size),
      render: (entry) => (entry.is_dir ? "-" : fileSize(entry.size)),
    },
    {
      key: "modified",
      header: "Modified",
      sortable: true,
      sortValue: (entry) => entry.mtime,
      render: (entry) => fileDate(entry.mtime),
    },
  ];

  const selectedUrl = selected ? mountFileUrl(mount.id, selectedPath) : "";
  const selectedDownloadUrl = selected ? mountFileUrl(mount.id, selectedPath, true) : "";

  return (
    <div style={{ display: "grid", gap: "var(--space-3)" }}>
      <div style={{ display: "flex", alignItems: "center", gap: "var(--space-2)", flexWrap: "wrap" }}>
        <Button size="sm" variant="ghost" onClick={onBack}>
          <Icon name="chevronRight" size={14} style={{ transform: "rotate(180deg)" }} /> All mounts
        </Button>
        <nav aria-label="Breadcrumb" style={{ display: "flex", alignItems: "center", gap: "0.35rem", flexWrap: "wrap" }}>
          {crumbsFor(mount, path).map((crumb, idx, all) => (
            <span key={crumb.path} style={{ display: "inline-flex", alignItems: "center", gap: "0.35rem" }}>
              <Button size="sm" variant="ghost" onClick={() => setPath(crumb.path)}>
                {crumb.label}
              </Button>
              {idx < all.length - 1 ? <Icon name="chevronRight" size={12} /> : null}
            </span>
          ))}
        </nav>
        {mount.cloud ? <Badge tone="warn">cloud — files may download on read</Badge> : null}
        {mount.read_only ? <Badge tone="neutral">read-only</Badge> : null}
      </div>

      {loading ? <Loading variant="skeleton" label="Loading directory…" lines={5} /> : null}
      {!loading && error ? <ErrorState message={error} onRetry={() => void refresh()} /> : null}
      {!loading && !error && !entries.length ? (
        <EmptyState title="Empty directory" message="Nothing to show here." />
      ) : null}
      {!loading && !error && entries.length ? (
        <Card padding="none">
          <Table
            columns={columns}
            rows={entries}
            rowKey={(entry) => entry.name}
            caption={`${mount.label}${path ? `/${path}` : ""}`}
          />
        </Card>
      ) : null}
      {!loading && truncated ? (
        <div style={{ display: "flex", alignItems: "center", gap: "var(--space-2)" }}>
          <Button size="sm" variant="secondary" onClick={() => void loadMore()} disabled={loadingMore}>
            {loadingMore ? "Loading…" : `Load more (${entries.length} of ${total})`}
          </Button>
        </div>
      ) : null}

      <Dialog
        open={selected !== null}
        onClose={() => setSelected(null)}
        title={selected?.name ?? "File preview"}
        footer={
          selected ? (
            <div style={{ display: "flex", justifyContent: "flex-end", gap: "var(--space-2)" }}>
              <Button variant="ghost" onClick={() => setSelected(null)}>
                Close
              </Button>
              <a href={selectedDownloadUrl} target="_blank" rel="noreferrer" style={{ color: "var(--accent)", alignSelf: "center" }}>
                Open / download
              </a>
            </div>
          ) : undefined
        }
      >
        {selected ? (
          <div style={{ display: "grid", gap: "var(--space-3)" }}>
            <p style={{ color: "var(--text-muted)", margin: 0 }}>
              {selectedPath} · {fileSize(selected.size)} · {fileDate(selected.mtime)}
            </p>
            {selectedIsImage ? (
              // Same-origin, user-selected machine file — intentionally bypasses
              // next/image, same rationale as FilesPanel's preview <img>.
              // eslint-disable-next-line @next/next/no-img-element
              <img src={selectedUrl} alt={selected.name} style={{ maxWidth: "100%", maxHeight: "65vh", objectFit: "contain" }} />
            ) : null}
            {previewLoading ? <Loading label="Loading preview…" /> : null}
            {previewError ? <ErrorState title="Preview unavailable" message={previewError} /> : null}
            {preview !== null ? (
              <pre style={{ margin: 0, maxHeight: "65vh", overflow: "auto", whiteSpace: "pre-wrap", fontSize: "var(--font-size-sm)" }}>
                {preview}
              </pre>
            ) : null}
          </div>
        ) : null}
      </Dialog>
    </div>
  );
}
