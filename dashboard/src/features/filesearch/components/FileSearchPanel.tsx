"use client";

import { useEffect, useState } from "react";
import {
  Badge,
  Button,
  Card,
  EmptyState,
  ErrorState,
  Icon,
  Input,
  Loading,
  Select,
  Table,
  useToast,
  type TableColumn,
} from "@/design";
import { FileSearchApiError, revealFile } from "../client";
import { useFileSearch, ALL, type CategoryFilter, type RootFilter } from "../hooks";
import {
  FILE_CATEGORIES,
  FILE_ROOTS,
  categoryLabel,
  rootLabel,
  type FileHit,
  type SearchMode,
} from "../types";

function fileSize(bytes: number | null | undefined): string {
  if (bytes == null || !Number.isFinite(bytes)) return "—";
  if (bytes < 1024) return `${Math.max(0, Math.round(bytes))} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(value < 10 ? 1 : 0)} ${units[unit]}`;
}

/** Relative age from raw epoch SECONDS (the mounts convention). */
function relTime(mtime: number | null | undefined): string {
  if (mtime == null || !Number.isFinite(mtime)) return "—";
  const ms = mtime * 1000;
  const mins = Math.floor(Math.max(0, Date.now() - ms) / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.floor(hrs / 24);
  if (days < 30) return `${days}d ago`;
  const months = Math.floor(days / 30);
  if (months < 12) return `${months}mo ago`;
  return `${Math.floor(days / 365)}y ago`;
}

function basename(path: string): string {
  const trimmed = path.replace(/\/+$/, "");
  const idx = trimmed.lastIndexOf("/");
  return idx >= 0 ? trimmed.slice(idx + 1) : trimmed;
}

async function copyText(value: string): Promise<void> {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(value);
    return;
  }
  const input = document.createElement("textarea");
  input.value = value;
  input.style.position = "fixed";
  input.style.opacity = "0";
  document.body.appendChild(input);
  input.select();
  document.execCommand("copy");
  input.remove();
}

/** A single-select facet rendered as house filter chips (board/page.tsx idiom:
 * primary when active, secondary otherwise, aria-pressed for a11y). */
function ChipGroup<T extends string>({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: T;
  options: ReadonlyArray<{ value: T; label: string }>;
  onChange: (next: T) => void;
}) {
  return (
    <div
      role="group"
      aria-label={label}
      style={{ display: "flex", alignItems: "center", gap: "var(--space-1)", flexWrap: "wrap" }}
    >
      <span
        style={{
          fontSize: "var(--text-small)",
          color: "var(--text-muted)",
          marginRight: "var(--space-1)",
        }}
      >
        {label}
      </span>
      {options.map((opt) => {
        const active = opt.value === value;
        return (
          <Button
            key={opt.value}
            size="sm"
            variant={active ? "primary" : "secondary"}
            aria-pressed={active}
            onClick={() => onChange(opt.value)}
          >
            {opt.label}
          </Button>
        );
      })}
    </div>
  );
}

const ROOT_OPTIONS: ReadonlyArray<{ value: RootFilter; label: string }> = [
  { value: ALL, label: "All" },
  ...FILE_ROOTS,
];
const CATEGORY_OPTIONS: ReadonlyArray<{ value: CategoryFilter; label: string }> = [
  { value: ALL, label: "All" },
  ...FILE_CATEGORIES,
];

const SEARCH_INPUT_ID = "filesearch-query";

/** The search-first header: one search box with a Name/Semantic mode toggle,
 * root + category filter chips, a name-mode sort control, and a results table
 * with Reveal in Finder / Copy path row actions. */
export function FileSearchPanel({ onActiveChange }: { onActiveChange?: (active: boolean) => void }) {
  const search = useFileSearch();
  const { push } = useToast();
  const [revealing, setRevealing] = useState<string | null>(null);

  // Let the page collapse the mounts browser once a search is active.
  useEffect(() => {
    onActiveChange?.(search.active);
  }, [search.active, onActiveChange]);

  // Keyboard: "/" focuses the search from anywhere (unless already typing).
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "/") return;
      const el = document.activeElement as HTMLElement | null;
      const typing =
        el &&
        (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.isContentEditable);
      if (typing) return;
      event.preventDefault();
      document.getElementById(SEARCH_INPUT_ID)?.focus();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const handleCopy = async (path: string) => {
    try {
      await copyText(path);
      push({ message: "Path copied to clipboard.", tone: "success" });
    } catch {
      push({ message: "Could not copy the path.", tone: "error" });
    }
  };

  const handleReveal = async (hit: FileHit) => {
    if (revealing) return;
    const key = `${hit.root}:${hit.path}`;
    setRevealing(key);
    try {
      await revealFile({ path: hit.path, root: hit.root });
      push({ message: "Revealed in Finder.", tone: "success" });
    } catch (reason) {
      if (reason instanceof FileSearchApiError && reason.status === 404) {
        push({
          title: "Reveal unavailable",
          message: "Local reveal isn't deployed on this build yet.",
          tone: "warn",
        });
      } else if (reason instanceof FileSearchApiError && reason.status === 403) {
        push({ message: "This file can't be revealed locally.", tone: "warn" });
      } else if (reason instanceof FileSearchApiError && reason.status === 501) {
        push({ message: "Opening locally is only available on the host machine.", tone: "warn" });
      } else {
        push({
          message: reason instanceof Error ? reason.message : "Could not reveal the file.",
          tone: "error",
        });
      }
    } finally {
      setRevealing(null);
    }
  };

  const semantic = search.mode === "semantic";

  const columns: TableColumn<FileHit>[] = [
    {
      key: "name",
      header: "Name",
      render: (hit) => (
        <div style={{ display: "grid", gap: "0.15rem", minWidth: 0 }}>
          <span style={{ fontWeight: 600, wordBreak: "break-word" }}>{basename(hit.path)}</span>
          <span
            title={hit.path}
            style={{
              fontFamily: "var(--font-mono)",
              fontSize: "var(--text-small)",
              color: "var(--text-faint)",
              wordBreak: "break-all",
            }}
          >
            {hit.path}
          </span>
          {semantic && hit.excerpt ? (
            <span
              style={{
                fontSize: "var(--text-small)",
                color: "var(--text-muted)",
                marginTop: "0.15rem",
                display: "-webkit-box",
                WebkitLineClamp: 2,
                WebkitBoxOrient: "vertical",
                overflow: "hidden",
              }}
            >
              {typeof hit.score === "number" ? (
                <span style={{ color: "var(--accent-text)", marginRight: "0.4rem" }}>
                  {hit.score.toFixed(2)}
                </span>
              ) : null}
              {hit.excerpt}
            </span>
          ) : null}
        </div>
      ),
    },
    {
      key: "category",
      header: "Category",
      render: (hit) => <Badge tone="neutral">{categoryLabel(hit.category)}</Badge>,
    },
    {
      key: "root",
      header: "Root",
      render: (hit) => <Badge tone="neutral">{rootLabel(hit.root)}</Badge>,
    },
    {
      key: "modified",
      header: "Modified",
      align: "right",
      render: (hit) => (
        <span style={{ color: "var(--text-muted)", whiteSpace: "nowrap" }}>{relTime(hit.mtime)}</span>
      ),
    },
    {
      key: "size",
      header: "Size",
      align: "right",
      render: (hit) => (
        <span style={{ color: "var(--text-muted)", whiteSpace: "nowrap" }}>{fileSize(hit.size)}</span>
      ),
    },
    {
      key: "actions",
      header: "",
      align: "right",
      render: (hit) => {
        const key = `${hit.root}:${hit.path}`;
        return (
          <div style={{ display: "inline-flex", gap: "var(--space-1)", justifyContent: "flex-end" }}>
            <Button
              size="sm"
              variant="ghost"
              disabled={Boolean(revealing)}
              onClick={() => void handleReveal(hit)}
              title="Reveal in Finder"
            >
              <span style={{ display: "inline-flex", alignItems: "center", gap: "0.3rem" }}>
                <Icon name="externalLink" size={13} />
                {revealing === key ? "Revealing…" : "Reveal"}
              </span>
            </Button>
            <Button
              size="sm"
              variant="ghost"
              onClick={() => void handleCopy(hit.path)}
              title="Copy full path"
            >
              <span style={{ display: "inline-flex", alignItems: "center", gap: "0.3rem" }}>
                <Icon name="columns" size={13} />
                Copy path
              </span>
            </Button>
          </div>
        );
      },
    },
  ];

  return (
    <div style={{ display: "grid", gap: "var(--space-4)" }}>
      {/* Search box + mode toggle */}
      <div style={{ display: "flex", alignItems: "flex-end", gap: "var(--space-2)", flexWrap: "wrap" }}>
        <div style={{ flex: "1 1 22rem", minWidth: "16rem" }}>
          <Input
            id={SEARCH_INPUT_ID}
            value={search.query}
            onChange={(event) => search.setQuery(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Escape" && search.query) {
                event.preventDefault();
                search.clear();
              }
            }}
            clearable
            onClear={search.clear}
            icon={<Icon name="search" size={15} />}
            placeholder={
              semantic
                ? "Describe what you're looking for…"
                : "Search files by name across every drive…   ( / to focus )"
            }
            aria-label="Search files"
          />
        </div>
        <div role="group" aria-label="Search mode" style={{ display: "inline-flex", gap: "var(--space-1)" }}>
          {(["name", "semantic"] as SearchMode[]).map((m) => {
            const active = search.mode === m;
            return (
              <Button
                key={m}
                size="md"
                variant={active ? "primary" : "secondary"}
                aria-pressed={active}
                onClick={() => search.setMode(m)}
              >
                <span style={{ display: "inline-flex", alignItems: "center", gap: "0.35rem" }}>
                  <Icon name={m === "semantic" ? "sparkles" : "hash"} size={14} />
                  {m === "semantic" ? "Semantic" : "Name"}
                </span>
              </Button>
            );
          })}
        </div>
      </div>

      {/* Facets */}
      <div style={{ display: "grid", gap: "var(--space-2)" }}>
        <ChipGroup<RootFilter>
          label="Root"
          value={search.root}
          options={ROOT_OPTIONS}
          onChange={search.setRoot}
        />
        <ChipGroup<CategoryFilter>
          label="Type"
          value={search.category}
          options={CATEGORY_OPTIONS}
          onChange={search.setCategory}
        />
        {!semantic ? (
          <div style={{ display: "flex", alignItems: "center", gap: "var(--space-2)" }}>
            <span style={{ fontSize: "var(--text-small)", color: "var(--text-muted)" }}>Sort</span>
            <Select
              aria-label="Sort results"
              value={search.sort}
              onChange={(value) => search.setSort(value === "name" ? "name" : "recency")}
              options={[
                { value: "recency", label: "Most recent" },
                { value: "name", label: "Name (A–Z)" },
              ]}
            />
          </div>
        ) : (
          <span style={{ fontSize: "var(--text-small)", color: "var(--text-faint)" }}>
            Semantic results are ranked by relevance.
          </span>
        )}
      </div>

      {/* Results */}
      {search.loading ? <Loading variant="skeleton" label="Searching files…" lines={5} /> : null}

      {!search.loading && search.error ? (
        <ErrorState title="Search failed" message={search.error} />
      ) : null}

      {!search.loading && !search.error && search.notDeployed ? (
        <EmptyState
          icon={<Icon name="database" size={22} />}
          title="Backend indexing not yet deployed"
          message="File search is ready in the dashboard, but the machine-wide index isn't running on this build yet. Once the filesearch indexer is deployed, results will appear here."
        />
      ) : null}

      {!search.loading && !search.error && !search.notDeployed && search.active && search.searched && search.hits.length === 0 ? (
        <EmptyState
          title="No matching files"
          message={
            semantic
              ? "No files came back for that description. Try broadening it, or switch to Name search."
              : "No files matched that name. Try fewer or different terms, or clear a filter."
          }
        />
      ) : null}

      {!search.loading && !search.error && search.hits.length > 0 ? (
        <Card padding="none">
          <Table
            columns={columns}
            rows={search.hits}
            rowKey={(hit) => `${hit.root}:${hit.path}`}
            caption={`${search.hits.length} result${search.hits.length === 1 ? "" : "s"}`}
          />
        </Card>
      ) : null}

      {!search.active ? (
        <EmptyState
          icon={<Icon name="search" size={22} />}
          title="Search every drive"
          message="Find files by name or by meaning across Desktop, iCloud, Google Drive and the repo — the same reach the agents have. Press / to start typing."
        />
      ) : null}
    </div>
  );
}
