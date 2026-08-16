"use client";

import { useState, type ReactNode } from "react";
import {
  Badge,
  Button,
  Card,
  cx,
  EmptyState,
  ErrorState,
  Loading,
  Page,
  PageHeader,
  Section,
} from "@/design";
import {
  projectActivityTimeline,
  type ActivityTimelineEntry,
} from "./activityTimeline";
import { useControlPlane } from "./hooks";
import styles from "./controlplane.module.css";
import type {
  EngineApproval,
  EngineCapabilities,
  EngineEvidenceTest,
  EngineRunContext,
  EngineRunSnapshot,
  EngineRunSummary,
} from "./types";

/** The only schema this UI knows how to read. Anything else is shown as incompatible. */
const SUPPORTED_API_VERSION = "loopdeck-engine/v1";
const GITHUB_URL_PREFIX = "https://github.com/";

/**
 * A link is rendered as a navigable anchor ONLY for a GitHub URL. Evidence is
 * reported by agents, so an arbitrary URL is untrusted input; the entry is
 * still shown, just as inert text.
 */
function githubUrl(value: string | null | undefined): string | null {
  return typeof value === "string" && value.startsWith(GITHUB_URL_PREFIX) ? value : null;
}

/**
 * Accept only a concrete owner/name slug. Anything else cannot host a real
 * github.com destination, so we never invent org/repo from branch or SHA alone.
 */
function githubRepoSlug(repository: string | null | undefined): string | null {
  if (typeof repository !== "string") return null;
  const slug = repository.trim();
  // owner/name — exactly one slash, no spaces, no leading/trailing slash.
  if (!/^[^\s/]+\/[^\s/]+$/.test(slug)) return null;
  return slug;
}

function githubRepoUrl(repository: string | null | undefined): string | null {
  const slug = githubRepoSlug(repository);
  return slug ? `${GITHUB_URL_PREFIX}${slug}` : null;
}

function githubBranchUrl(
  repository: string | null | undefined,
  branch: string | null | undefined,
): string | null {
  const slug = githubRepoSlug(repository);
  if (!slug || typeof branch !== "string" || !branch.trim()) return null;
  // Preserve path segments (feature/foo); do not percent-encode slashes.
  return `${GITHUB_URL_PREFIX}${slug}/tree/${branch}`;
}

function githubCommitUrl(
  repository: string | null | undefined,
  sha: string | null | undefined,
): string | null {
  const slug = githubRepoSlug(repository);
  if (!slug || typeof sha !== "string" || !sha.trim()) return null;
  return `${GITHUB_URL_PREFIX}${slug}/commit/${sha}`;
}

/**
 * Evidence commit URL policy:
 * 1. Trust only an explicit https://github.com/ url from the engine.
 * 2. A backend-refused url (`url_refused`) is distinct from an absent one:
 *    the engine saw a real, non-GitHub url and rejected it. The client must
 *    NEVER derive a fabricated github.com link in that case -- stay inert.
 * 3. When url is truly absent (no url and no refusal) and repository is a
 *    real owner/name, derive the commit page.
 * 4. Never invent when repository is null.
 */
function commitEvidenceUrl(
  sha: string,
  url: string | null | undefined,
  urlRefused: boolean | null | undefined,
  repository: string | null | undefined,
): string | null {
  if (typeof url === "string" && url.trim()) {
    return githubUrl(url);
  }
  if (urlRefused) {
    return null;
  }
  return githubCommitUrl(repository, sha);
}

/**
 * Render any payload value as a single string. Payload strings are shown as
 * React text nodes and are never parsed as HTML — an agent message containing
 * markup must appear literally, not execute.
 */
function asText(value: unknown): string {
  if (typeof value === "string") return value;
  if (value === null || value === undefined) return "—";
  return JSON.stringify(value);
}

function phaseTone(
  phase: string | null,
): "ok" | "warn" | "danger" | "neutral" | "running" | "completed" | "failed" | "paused" {
  switch (phase) {
    case "done":
      return "completed";
    case "failed":
      return "failed";
    case "blocked":
    case "parked":
      return "warn";
    case "running":
      return "running";
    case "reviewing":
    case "planning":
      return "paused";
    default:
      return "neutral";
  }
}

function EngineStatus({
  capabilities,
  capabilitiesError,
}: {
  capabilities: EngineCapabilities | null;
  capabilitiesError: string | null;
}) {
  if (!capabilities) {
    return (
      <Card className={styles.engineUnavailable}>
        <p className={styles.subhead}>Status unknown</p>
        <p className={styles.meta}>
          {capabilitiesError ?? "The LoopDeck engine has not reported its capabilities."}
        </p>
      </Card>
    );
  }
  const compatible = capabilities.api_version === SUPPORTED_API_VERSION;
  return (
    <Card>
      <div className={styles.cardHead}>
        <p className={styles.subhead}>LoopDeck engine</p>
        <Badge tone={compatible ? "ok" : "warn"}>
          {compatible ? "connected" : "incompatible"}
        </Badge>
      </div>
      <p className={styles.meta}>
        Availability does not imply active work — this surface is read-only and reports
        only what the engine has confirmed.
      </p>
      <p className={cx(styles.mono, styles.wrapValue)}>{capabilities.api_version}</p>
    </Card>
  );
}

function ContextValue({
  label,
  value,
  href,
}: {
  label: string;
  value: string | null;
  href: string | null;
}) {
  return (
    <div>
      <dt className={styles.meta}>{label}</dt>
      <dd className={cx(styles.mono, styles.wrapValue)}>
        {value && href ? (
          <a href={href} target="_blank" rel="noreferrer noopener">
            {value}
          </a>
        ) : (
          value ?? "unknown"
        )}
      </dd>
    </div>
  );
}

function ContextRow({ context }: { context: EngineRunContext }) {
  const repository = context.repository ?? null;
  const branch = context.branch ?? null;
  const headSha = context.head_sha ?? null;

  return (
    <dl className={styles.contextRow}>
      <ContextValue label="Repository" value={repository} href={githubRepoUrl(repository)} />
      <ContextValue
        label="Branch"
        value={branch}
        href={githubBranchUrl(repository, branch)}
      />
      <ContextValue
        label="Current SHA"
        value={headSha}
        href={githubCommitUrl(repository, headSha)}
      />
    </dl>
  );
}

/** Approval is shown from the receipt alone; nothing else may imply it. */
function ApprovalRow({ approval }: { approval: EngineApproval }) {
  if (!approval.approved || !approval.receipt) {
    return (
      <>
        <p className={styles.subhead}>No approval recorded</p>
        <p className={styles.meta}>
          Artifacts, reports and passing tests are evidence, not approval.
        </p>
      </>
    );
  }
  const { reviewer, run_id: runId, head_sha: headSha } = approval.receipt;
  return (
    <p className={cx(styles.subhead, styles.wrapValue)}>
      Approved by {reviewer ?? "an unnamed reviewer"} — receipt bound to run{" "}
      {runId ?? "unknown"} at {headSha ?? "unknown"}
    </p>
  );
}

/**
 * Verification is a projection of evidence.tests only. Passing tests never
 * imply approval; empty tests stay unknown rather than green.
 */
function VerificationBand({ tests }: { tests: EngineEvidenceTest[] }) {
  let summary: string;
  if (tests.length === 0) {
    summary = "unknown — no tests reported";
  } else if (tests.some((test) => (test.status ?? "").toLowerCase() === "failed")) {
    summary = "failed";
  } else if (tests.every((test) => (test.status ?? "").toLowerCase() === "passed")) {
    summary = "all passed";
  } else {
    summary = "unknown — mixed or incomplete results";
  }

  return (
    <div
      role="status"
      aria-label="Verification"
      className={styles.verificationBand}
      data-state={
        tests.length === 0
          ? "unknown"
          : tests.some((test) => (test.status ?? "").toLowerCase() === "failed")
            ? "failed"
            : tests.every((test) => (test.status ?? "").toLowerCase() === "passed")
              ? "passed"
              : "unknown"
      }
    >
      <p className={styles.subhead}>Verification</p>
      <p className={styles.wrapValue}>{summary}</p>
    </div>
  );
}

function EvidenceEntry({
  primary,
  secondary,
  url,
}: {
  primary: string;
  secondary: string | null;
  url: string | null;
}) {
  const safeUrl = githubUrl(url);
  return (
    <li className={styles.row}>
      <span className={cx(styles.mono, styles.wrapValue)}>
        {safeUrl ? (
          <a href={safeUrl} target="_blank" rel="noreferrer noopener">
            {primary}
          </a>
        ) : (
          primary
        )}
      </span>
      {secondary ? <span className={cx(styles.meta, styles.wrapValue)}>{secondary}</span> : null}
    </li>
  );
}

function EvidenceGroup({
  title,
  empty,
  children,
  count,
}: {
  title: string;
  empty: string;
  count: number;
  children: ReactNode;
}) {
  return (
    <Card>
      <div className={styles.cardHead}>
        <p className={styles.subhead}>{title}</p>
      </div>
      {count === 0 ? (
        <p className={styles.meta}>{empty}</p>
      ) : (
        <ul className={styles.list}>{children}</ul>
      )}
    </Card>
  );
}

/**
 * One Loop card per engine run. Binding summary (repository/branch) is shown
 * only for the selected run when a snapshot context is available — never
 * invented for unbound runs.
 */
function LoopCard({
  run,
  selected,
  context,
  awaitingBinding,
  onSelect,
}: {
  run: EngineRunSummary;
  selected: boolean;
  /**
   * Only ever the context of the snapshot BOUND to this run (snapshot.run.id
   * === this run's id). The caller must pass null, never a stale snapshot's
   * context, while a newly selected run's fetch is still in flight.
   */
  context: EngineRunContext | null;
  /** Selected, but the bound snapshot has not arrived yet -- show a loading
   * indicator instead of the previous run's context. */
  awaitingBinding: boolean;
  onSelect: (id: string) => void;
}) {
  const goal = run.goal ?? run.id;
  return (
    <li
      className={cx(styles.loopCard, selected && styles.loopCardSelected)}
      aria-label={goal}
      aria-current={selected ? "true" : undefined}
    >
      <button type="button" className={styles.loopCardButton} onClick={() => onSelect(run.id)}>
        <div className={styles.cardHead}>
          <p className={cx(styles.title, styles.wrapValue)}>{goal}</p>
          <Badge tone={run.status === "done" || run.status === "completed" ? "ok" : "neutral"}>
            {run.status}
          </Badge>
        </div>
        {selected && context ? (
          <p className={cx(styles.meta, styles.wrapValue)}>
            {context.repository ?? "unknown"} · {context.branch ?? "unknown"}
          </p>
        ) : selected && awaitingBinding ? (
          <p className={styles.meta} role="status">
            Loading evidence…
          </p>
        ) : (
          <p className={styles.meta}>Select to inspect evidence</p>
        )}
      </button>
    </li>
  );
}

/**
 * Always-visible result prefers status/reason only. Free-text ``note`` stays
 * behind the raw disclosure so operator-only detail is not shown by default
 * (the pure projector still exposes note as a result fallback for callers
 * that want it).
 */
function visibleResult(row: ActivityTimelineEntry): string | null {
  const status = row.rawPayload.status;
  if (typeof status === "string" && status.trim()) return status;
  const reason = row.rawPayload.reason;
  if (typeof reason === "string" && reason.trim()) return reason;
  return null;
}

/**
 * One readable timeline row. Concise fields are always visible; the raw
 * payload is not mounted until the operator expands the disclosure, so
 * secret-ish keys stay out of the DOM (and out of getByText) by default.
 */
function ActivityTimelineRow({ row }: { row: ActivityTimelineEntry }) {
  const [rawOpen, setRawOpen] = useState(false);
  const result = visibleResult(row);
  const summary = result ? `${row.action} — ${result}` : row.action;

  return (
    <li className={styles.timelineItem}>
      <div className={styles.timelineMeta}>
        <time className={cx(styles.mono, styles.wrapValue)} dateTime={row.timestampIso ?? undefined}>
          {row.timestampLabel}
        </time>
        {row.phase ? <Badge tone={phaseTone(row.phase)}>{row.phase}</Badge> : null}
      </div>

      <div className={styles.timelineBody}>
        <div className={styles.timelinePrimary}>
          <span className={cx(styles.role, styles.wrapValue)}>{row.role ?? "—"}</span>
          <span className={cx(styles.meta, styles.wrapValue)}>{row.modelAccountLabel}</span>
        </div>

        <p className={cx(styles.timelineSummary, styles.wrapValue)}>{summary}</p>

        {row.evidenceLinks.length > 0 ? (
          <ul className={styles.evidenceLinks}>
            {row.evidenceLinks.map((link) => (
              <li key={`${link.href ?? "refused"}:${link.label}`}>
                {link.href ? (
                  <a href={link.href} target="_blank" rel="noreferrer noopener">
                    {link.label}
                  </a>
                ) : (
                  // Refused destination: the evidence label stays visible as
                  // inert text — never an anchor the browser could follow.
                  <span className={cx(styles.meta, styles.wrapValue)}>{link.label}</span>
                )}
              </li>
            ))}
          </ul>
        ) : null}

        {row.hasRawPayload ? (
          <div className={styles.rawPayload}>
            <button
              type="button"
              className={styles.rawPayloadSummary}
              aria-expanded={rawOpen}
              onClick={() => setRawOpen((open) => !open)}
            >
              Raw payload
            </button>
            {rawOpen ? (
              <dl className={styles.pairs}>
                {Object.entries(row.rawPayload).map(([key, value]) => (
                  <div key={key}>
                    <dt className={styles.meta}>{key}</dt>
                    <dd className={cx(styles.mono, styles.wrapValue)}>{asText(value)}</dd>
                  </div>
                ))}
              </dl>
            ) : null}
          </div>
        ) : null}
      </div>
    </li>
  );
}

function ActivityTimeline({ activity }: { activity: EngineRunSnapshot["activity"] }) {
  const rows = projectActivityTimeline(activity);
  if (rows.length === 0) {
    return <p className={styles.meta}>No agent messages recorded</p>;
  }
  return (
    <ul className={styles.timeline} aria-label="Agent activity timeline">
      {rows.map((row) => (
        <ActivityTimelineRow key={row.id} row={row} />
      ))}
    </ul>
  );
}

function RunDetails({ snapshot }: { snapshot: EngineRunSnapshot }) {
  const { evidence, activity } = snapshot;
  const repository = snapshot.context.repository ?? null;

  return (
    <>
      <Section title="Repository context">
        <Card>
          <ContextRow context={snapshot.context} />
        </Card>
      </Section>

      <Section title="Verification">
        <Card>
          <VerificationBand tests={evidence.tests} />
        </Card>
      </Section>

      <Section title="Approval">
        <Card>
          <ApprovalRow approval={snapshot.approval} />
        </Card>
      </Section>

      <Section
        title="Agent conversations"
        description="Readable activity timeline with role, model/account, phase, and concise action/result."
      >
        <Card>
          <ActivityTimeline activity={activity} />
        </Card>
      </Section>

      <Section title="Evidence">
        <div className={styles.stack}>
          <EvidenceGroup title="Commits" empty="No commits reported" count={evidence.commits.length}>
            {evidence.commits.map((commit) => (
              <EvidenceEntry
                key={commit.sha}
                primary={commit.sha}
                secondary={commit.message ?? null}
                url={commitEvidenceUrl(commit.sha, commit.url, commit.url_refused, repository)}
              />
            ))}
          </EvidenceGroup>

          <EvidenceGroup title="Files" empty="No files reported" count={evidence.files.length}>
            {evidence.files.map((file) => (
              <EvidenceEntry
                key={file.path}
                primary={file.path}
                secondary={null}
                url={file.url ?? null}
              />
            ))}
          </EvidenceGroup>

          <EvidenceGroup title="Tests" empty="No tests reported" count={evidence.tests.length}>
            {evidence.tests.map((test) => (
              <EvidenceEntry
                key={test.name}
                primary={test.name}
                secondary={test.status ?? null}
                url={test.url ?? null}
              />
            ))}
          </EvidenceGroup>

          <EvidenceGroup title="Reports" empty="No reports reported" count={evidence.reports.length}>
            {evidence.reports.map((report) => (
              <EvidenceEntry
                key={report.name}
                primary={report.name}
                secondary={report.status ?? null}
                url={report.url ?? null}
              />
            ))}
          </EvidenceGroup>
        </div>
      </Section>
    </>
  );
}

export function ControlPlaneView() {
  const {
    runs,
    runId,
    setRunId,
    capabilities,
    capabilitiesError,
    snapshot,
    snapshotError,
    loading,
    hasLoaded,
    error,
    stale,
    refresh,
  } = useControlPlane();

  // Nothing has been confirmed yet: show the blocking state, never an empty
  // page that would read as "there is no work".
  if (!hasLoaded) {
    return (
      <Page>
        <PageHeader title="LoopDeck control plane" />
        {error ? <ErrorState message={error} onRetry={refresh} /> : <Loading />}
      </Page>
    );
  }

  // CP-002: the run card AND the detail panel must both bind strictly to the
  // currently selected runId. `snapshot` can still hold the previous run's
  // data while a newly selected run's fetch is in flight (or after a change
  // of selection before the poll settles) -- only a snapshot whose own
  // `run.id` matches the selection is ever treated as "this run's" data.
  const boundSnapshot = snapshot && snapshot.run.id === runId ? snapshot : null;
  const awaitingBinding = Boolean(runId) && boundSnapshot === null;

  return (
    <Page>
      <PageHeader
        title="LoopDeck control plane"
        lead="Read-only evidence projected from the LoopDeck engine."
        actions={
          <Button variant="secondary" size="sm" onClick={refresh}>
            Refresh
          </Button>
        }
      />

      {stale ? (
        <p className={styles.banner}>
          The latest refresh failed. Showing the last confirmed snapshot.
        </p>
      ) : null}

      <EngineStatus capabilities={capabilities} capabilitiesError={capabilitiesError} />

      <Section title="Runs" description="Select the run whose evidence you want to inspect.">
        {runs.length === 0 ? (
          <EmptyState
            title="No LoopDeck runs"
            message="The engine has not reported any runs yet."
          />
        ) : (
          <ul className={styles.loopCardList} aria-label="Loop runs">
            {runs.map((run) => (
              <LoopCard
                key={run.id}
                run={run}
                selected={run.id === runId}
                context={run.id === runId ? boundSnapshot?.context ?? null : null}
                awaitingBinding={run.id === runId && awaitingBinding}
                onSelect={setRunId}
              />
            ))}
          </ul>
        )}
      </Section>

      {runs.length > 0 && !runId ? (
        <EmptyState
          title="No engine run bound"
          message="Select a run above to inspect its evidence."
        />
      ) : null}

      {snapshotError ? <p className={styles.banner}>{snapshotError}</p> : null}

      {runId && boundSnapshot ? (
        <RunDetails snapshot={boundSnapshot} />
      ) : runId && awaitingBinding ? (
        <Section title="Evidence">
          <Card>
            <p role="status" aria-label="Loading run evidence" className={styles.meta}>
              {loading ? "Loading evidence for the selected run…" : "Waiting to bind evidence for the selected run…"}
            </p>
          </Card>
        </Section>
      ) : null}
    </Page>
  );
}
