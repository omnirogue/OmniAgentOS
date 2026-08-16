"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { Fragment, useEffect, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { EmptyState, ErrorState, Icon, Input, Loading } from "../../design";
import { searchVault, VaultApiError } from "./api";
import { fixtureSearch } from "./fixtures";
import { noteTypeColor, noteTypeLabel } from "./noteType";
import { vaultHref } from "./paths";
import type { FetchStatus, VaultSearchHit } from "./types";
import { USE_FIXTURES } from "./useVaultData";

const DEBOUNCE_MS = 250;

function escapeRegExp(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/** Wraps every case-insensitive occurrence of `query` in `text` with <mark>. */
function highlightMatches(text: string, query: string): ReactNode {
  const q = query.trim();
  if (!q) return text;
  const re = new RegExp(`(${escapeRegExp(q)})`, "ig");
  const parts = text.split(re);
  if (parts.length <= 1) return text;
  return parts.map((part, i) =>
    part.toLowerCase() === q.toLowerCase() ? (
      <mark key={i} style={{ background: "color-mix(in srgb, var(--accent) 38%, transparent)", color: "inherit", borderRadius: 2 }}>
        {part}
      </mark>
    ) : (
      <Fragment key={i}>{part}</Fragment>
    ),
  );
}

export interface SearchPanelProps {
  initialQuery?: string;
  autoFocus?: boolean;
  placeholder?: string;
  label?: string;
  onQueryChange?: (query: string) => void;
  onNavigate?: (path: string) => void;
}

/** Search box + results list, shared by the /vault sidebar and the /vault/search page. */
export function SearchPanel({ initialQuery = "", autoFocus, placeholder = "Search the vault…", label, onQueryChange, onNavigate }: SearchPanelProps) {
  const router = useRouter();
  const [query, setQuery] = useState(initialQuery);
  const [status, setStatus] = useState<FetchStatus>("empty");
  const [results, setResults] = useState<VaultSearchHit[]>([]);
  // The search may be INCOMPLETE (the backend's candidate-scan cap or
  // result-limit cap was hit) -- this must render an explicit signal, not
  // silently show partial hits under a page that promises completeness.
  const [truncated, setTruncated] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [highlightIndex, setHighlightIndex] = useState(0);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const generation = useRef(0);

  useEffect(() => {
    setQuery(initialQuery);
  }, [initialQuery]);

  useEffect(() => {
    onQueryChange?.(query);
    if (debounceRef.current) clearTimeout(debounceRef.current);
    const q = query.trim();
    if (!q) {
      setStatus("empty");
      setResults([]);
      setTruncated(false);
      setError(null);
      return;
    }
    setStatus("loading");
    debounceRef.current = setTimeout(() => {
      const gen = ++generation.current;
      if (USE_FIXTURES) {
        const hits = fixtureSearch(q);
        setResults(hits);
        setTruncated(false);
        setStatus(hits.length === 0 ? "empty" : "ready");
        return;
      }
      searchVault(q)
        .then(({ hits, truncated: incomplete }) => {
          if (generation.current !== gen) return;
          setResults(hits);
          setTruncated(incomplete);
          setStatus(hits.length === 0 ? "empty" : "ready");
        })
        .catch((reason: unknown) => {
          if (generation.current !== gen) return;
          const message =
            reason instanceof VaultApiError ? reason.message : reason instanceof Error ? reason.message : "Search failed.";
          setError(message);
          setTruncated(false);
          setStatus("error");
        });
    }, DEBOUNCE_MS);
    return () => {
      if (debounceRef.current) clearTimeout(debounceRef.current);
    };
    // onQueryChange is a caller-supplied callback, intentionally excluded so typing
    // doesn't need a stable identity from the caller.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [query]);

  useEffect(() => setHighlightIndex(0), [results]);

  const onKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (results.length === 0) return;
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setHighlightIndex((i) => Math.min(i + 1, results.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setHighlightIndex((i) => Math.max(i - 1, 0));
    } else if (e.key === "Enter") {
      e.preventDefault();
      const hit = results[highlightIndex];
      if (hit) {
        onNavigate?.(hit.path);
        router.push(vaultHref(hit.path));
      }
    }
  };

  return (
    <div>
      <Input
        label={label}
        aria-label={label ?? "Search the vault"}
        placeholder={placeholder}
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        onKeyDown={onKeyDown}
        clearable
        onClear={() => setQuery("")}
        autoFocus={autoFocus}
        role="combobox"
        aria-expanded={results.length > 0}
        aria-controls="vault-search-results"
      />
      <div id="vault-search-results" role="listbox" aria-label="Search results" style={{ marginTop: "var(--space-3)" }}>
        {status === "loading" ? <Loading variant="skeleton" lines={3} /> : null}
        {status === "error" ? <ErrorState message={error ?? "Search failed."} /> : null}
        {truncated && (status === "ready" || status === "empty") && query.trim() ? (
          <div
            role="status"
            style={{
              margin: "0 0 var(--space-2)",
              padding: "var(--space-2) var(--space-3)",
              borderRadius: "var(--radius-md)",
              background: "color-mix(in srgb, var(--warning, orange) 14%, transparent)",
              color: "var(--text-muted)",
              fontSize: "var(--text-small)",
            }}
          >
            Showing a partial result — this search was cut off before covering the
            whole vault (too many candidate notes, or too many matches to list).
            Narrow the query for a complete answer.
          </div>
        ) : null}
        {status === "empty" && query.trim() ? <EmptyState icon={<Icon name="search" size={22} />} message={`No notes match "${query.trim()}".`} /> : null}
        {status === "ready" ? (
          <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "flex", flexDirection: "column", gap: "var(--space-1)" }}>
            {results.map((hit, i) => (
              <li key={hit.path}>
                <Link
                  href={vaultHref(hit.path)}
                  role="option"
                  aria-selected={i === highlightIndex}
                  onClick={() => onNavigate?.(hit.path)}
                  onMouseEnter={() => setHighlightIndex(i)}
                  style={{
                    display: "block",
                    padding: "var(--space-2) var(--space-3)",
                    borderRadius: "var(--radius-md)",
                    background: i === highlightIndex ? "var(--overlay)" : "transparent",
                    color: "var(--text)",
                  }}
                >
                  <div style={{ display: "flex", alignItems: "center", gap: "var(--space-2)" }}>
                    <span
                      aria-hidden="true"
                      style={{ width: "0.5rem", height: "0.5rem", borderRadius: "50%", background: noteTypeColor(hit.type), flexShrink: 0 }}
                    />
                    <strong style={{ fontWeight: 600, fontSize: "var(--text-small)" }}>{highlightMatches(hit.title, query)}</strong>
                    <span style={{ fontSize: "var(--text-micro)", color: "var(--text-faint)" }}>{noteTypeLabel(hit.type)}</span>
                  </div>
                  {hit.snippet ? (
                    <p
                      style={{
                        margin: "0.25rem 0 0",
                        fontSize: "var(--text-small)",
                        color: "var(--text-muted)",
                        display: "-webkit-box",
                        WebkitLineClamp: 2,
                        WebkitBoxOrient: "vertical",
                        overflow: "hidden",
                      }}
                    >
                      {highlightMatches(hit.snippet, query)}
                    </p>
                  ) : null}
                </Link>
              </li>
            ))}
          </ul>
        ) : null}
      </div>
    </div>
  );
}
