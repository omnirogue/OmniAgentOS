"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { Badge, Card, ErrorState, Input, Loading, Page, PageHeader, Section } from "@/design";
import { relativizeHomePath } from "./lib";
import type { LocalClone, OwnerRepos, RemoteRepo, ReposPayload } from "./types";
import styles from "./repos.module.css";

const OWNERS = ["example-org", "Globex", "initech"] as const;
const HOME = "/Users/youruser";

function relativeTime(value: string | null): string {
  if (!value) return "never";
  const time = Date.parse(value);
  if (!Number.isFinite(time)) return "unknown";
  const seconds = Math.max(0, Math.floor((Date.now() - time) / 1_000));
  if (seconds < 60) return "just now";
  if (seconds < 3_600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86_400) return `${Math.floor(seconds / 3_600)}h ago`;
  if (seconds < 2_592_000) return `${Math.floor(seconds / 86_400)}d ago`;
  return `${Math.floor(seconds / 2_592_000)}mo ago`;
}

function byPushedAt(left: RemoteRepo, right: RemoteRepo): number {
  return (Date.parse(right.pushedAt ?? "") || 0) - (Date.parse(left.pushedAt ?? "") || 0);
}

function OwnerSection({ section, query }: { section: OwnerRepos; query: string }) {
  const repos = useMemo(
    () => (section.repos ?? []).filter((repo) => repo.name.toLowerCase().includes(query)).sort(byPushedAt),
    [query, section.repos],
  );
  return (
    <Card className={styles.ownerCard}>
      <div className={styles.cardHeading}>
        <h2>{section.owner}</h2>
        {section.repos ? <span>{section.repos.length} repositories</span> : null}
      </div>
      {section.error ? <p className={styles.unavailable}>unavailable: {section.error}</p> : null}
      {section.repos ? (
        repos.length > 0 ? (
          <ul className={styles.repoList}>
            {repos.map((repo) => (
              <li key={repo.name} className={repo.isArchived ? styles.archived : undefined}>
                <span className={styles.repoName}>{repo.name}</span>
                <Badge tone="neutral">{repo.visibility.toLowerCase()}</Badge>
                <span className={styles.pushed}>{relativeTime(repo.pushedAt)}</span>
              </li>
            ))}
          </ul>
        ) : <p className={styles.empty}>{query ? "No matching repositories." : "No repositories returned."}</p>
      ) : null}
    </Card>
  );
}

function LocalTable({ locals, query }: { locals: LocalClone[]; query: string }) {
  const rows = locals.filter((local) => local.path.toLowerCase().includes(query));
  return (
    <div className={styles.tableWrap}>
      <table className={styles.table}>
        <thead><tr><th>Path</th><th>Branch</th><th>Dirty</th><th>Origin</th></tr></thead>
        <tbody>
          {rows.map((local) => (
            <tr key={local.path}>
              <td className={styles.path}>{relativizeHomePath(local.path, HOME)}</td>
              <td>{local.branch ?? <span className={styles.faint}>unavailable</span>}</td>
              <td>
                {/* F03: `dirty === null` means `git status` itself failed --
                    render an honest amber "unknown", never a false-clean 0. */}
                {local.dirty === null ? (
                  <Badge tone="warn" title={local.dirtyError ?? undefined}>status unknown</Badge>
                ) : (
                  <Badge tone={local.dirty > 0 ? "danger" : "ok"}>{local.dirty}</Badge>
                )}
              </td>
              <td>{local.origin ?? <Badge tone="danger">NO ORIGIN</Badge>}</td>
            </tr>
          ))}
          {rows.length === 0 ? <tr><td colSpan={4} className={styles.empty}>No matching local clones.</td></tr> : null}
        </tbody>
      </table>
    </div>
  );
}

function isPayload(value: unknown): value is ReposPayload {
  return Boolean(value && typeof value === "object" && "owners" in value && "locals" in value && "violations" in value);
}

export function ReposDashboard() {
  const [data, setData] = useState<ReposPayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [query, setQuery] = useState("");
  const refresh = useCallback(async () => {
    try {
      const response = await fetch("/api/local/repos", { cache: "no-store" });
      const json: unknown = await response.json();
      if (!response.ok || !isPayload(json)) {
        throw new Error(json && typeof json === "object" && "error" in json ? String(json.error) : `HTTP ${response.status}`);
      }
      setData(json);
      setError(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setLoading(false);
    }
  }, []);
  useEffect(() => { void refresh(); }, [refresh]);

  const normalizedQuery = query.trim().toLowerCase();
  const sections = useMemo(
    () => OWNERS.map((owner) => data?.owners.find((section) => section.owner === owner) ?? { owner, error: "unavailable: owner section missing" }),
    [data],
  );

  return (
    <Page>
      <PageHeader eyebrow="Estate inventory" title="Repositories" lead="GitHub ownership, clone hygiene, and prototype repository coverage." meta={data ? <span>Scanned {new Date(data.scannedAt).toLocaleString()}</span> : undefined} />
      <Input label="Filter repositories and local paths" aria-label="Filter repositories and local paths" type="search" value={query} onChange={(event) => setQuery(event.target.value)} clearable onClear={() => setQuery("")} placeholder="repo or path name" />
      {loading && !data ? <Card><Loading label="Scanning repositories…" /></Card> : null}
      {error ? <ErrorState message={`Could not load repository inventory: ${error}`} onRetry={() => void refresh()} /> : null}
      {data?.violations.length ? (
        <section className={styles.violations} aria-label="Repository violations">
          <strong>VIOLATIONS · every prototype gets a GitHub repo</strong>
          <p>Dirty or origin-less local clones break the estate rule.</p>
          <ul>{data.violations.map((local) => <li key={local.path}>{relativizeHomePath(local.path, HOME)} — {local.reason}</li>)}</ul>
        </section>
      ) : null}
      {data?.truncated ? <p className={styles.truncated}>Local clone scan stopped at 40 repositories; results are truncated.</p> : null}
      {data ? <>
        <Section title="GitHub owners"><div className={styles.ownerGrid}>{sections.map((section) => <OwnerSection key={section.owner} section={section} query={normalizedQuery} />)}</div></Section>
        <Section title="Local clones" description="Working-copy status and the configured origin for every scanned clone."><Card padding="none"><LocalTable locals={data.locals} query={normalizedQuery} /></Card></Section>
      </> : null}
    </Page>
  );
}
