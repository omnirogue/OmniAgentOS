"use client";

/**
 * TeamBoard (P5) — one section per person (the operator / Alice / Bob), each with
 * five stacked lists (Ready / Active / Blocked / Review / Done today), plus
 * a collapsed "Agents & unowned" section for everyone/everything else.
 *
 * Data: `GET /api/team/board` (`useTeamBoard`) owns the bucket assignment
 * and the two derived fields (`counts`, `ready_below_5`) — server-owned,
 * never re-derived here. Its per-card shape (`QueueCard`) is minimal
 * (id/title/ref/status/size), so cards are ENRICHED against the reconciled
 * board (`useLiveBoard`, already fetched everywhere else in the dashboard)
 * for priority/verified/blocked-reason — a best-effort merge by id that
 * degrades to the plain card when a match is not found (a fresh card the
 * enrichment feed has not caught up with yet, or a card `useLiveBoard`
 * filtered out).
 */

import { useMemo, useState } from "react";
import { Badge, Card, ErrorBoundary, ErrorState, Loading, Select } from "@/design";
import { useLiveBoard } from "@/features/collab/hooks";
import type { LiveBoardTask } from "@/features/collab/types";
import { blockedHint, priorityTone } from "@/features/board/BoardKanban";
import { useProjectTree } from "@/features/projects/hierarchyHooks";
import { buildAgentsBucket } from "./agentsBucket";
import { useTeamBoard } from "./hooks";
import { NAMED_EMPLOYEE_IDS, completionStateOf, employeeName } from "./types";
import { parseTeamBoard } from "./types";
import type {
  TeamQueueBuckets,
  TeamQueueCard,
  TeamQueueCounts,
  TeamQueuePool,
  TeamTaskAccountabilityFields,
} from "./types";
import styles from "./team.module.css";

/** Migration 132 widened `board_tasks` (and its `GET /api/board` list
 * projection, `_BOARD_LIST_COLUMNS`) with the verification-failure stamps.
 * `LiveBoardTask` is out of this package's ownership and has not been
 * widened for them, so the reconciled-board enrichment is read through this
 * local cast — same pattern `TaskOverview` uses. */
type EnrichedTeamTask = LiveBoardTask & TeamTaskAccountabilityFields;

const BUCKET_ORDER: Array<{ key: keyof TeamQueueCounts; label: string }> = [
  { key: "ready", label: "Ready" },
  { key: "active", label: "Active" },
  { key: "blocked", label: "Blocked" },
  { key: "review", label: "Review" },
  { key: "done_today", label: "Done today" },
];

const AGENTS_LABEL = "Agents & unowned";
const POOL_CARD_LIMIT = 50;
const ALL_COMPANIES_VALUE = "";
const ALL_PROJECTS_VALUE = "";

/** Does `card` pass the company/project filter? A card with no reconciled
 * enrichment always passes (degrade-open — the same philosophy the rest of
 * `TeamBoard` uses for a card `useLiveBoard` has not caught up with yet). */
function matchesTeamFilters(
  card: TeamQueueCard,
  taskMap: Map<string, LiveBoardTask>,
  companyFilter: string,
  projectFilter: string,
): boolean {
  if (!companyFilter && !projectFilter) return true;
  const enriched = taskMap.get(card.id);
  if (!enriched) return true;
  if (companyFilter && enriched.org?.organization_context?.company_slug !== companyFilter) return false;
  if (projectFilter && enriched.project_id !== projectFilter) return false;
  return true;
}

function filterCards(
  cards: TeamQueueCard[],
  taskMap: Map<string, LiveBoardTask>,
  companyFilter: string,
  projectFilter: string,
): TeamQueueCard[] {
  if (!companyFilter && !projectFilter) return cards;
  return cards.filter((card) => matchesTeamFilters(card, taskMap, companyFilter, projectFilter));
}

/** Filters a bucket's five card lists, leaving server-owned `counts` and the
 * boolean flags untouched — those are store-derived and not re-derived here. */
function filterBucketCards(
  bucket: TeamQueueBuckets,
  taskMap: Map<string, LiveBoardTask>,
  companyFilter: string,
  projectFilter: string,
): TeamQueueBuckets {
  if (!companyFilter && !projectFilter) return bucket;
  return {
    ...bucket,
    ready: filterCards(bucket.ready, taskMap, companyFilter, projectFilter),
    active: filterCards(bucket.active, taskMap, companyFilter, projectFilter),
    blocked: filterCards(bucket.blocked, taskMap, companyFilter, projectFilter),
    review: filterCards(bucket.review, taskMap, companyFilter, projectFilter),
    done_today: filterCards(bucket.done_today, taskMap, companyFilter, projectFilter),
  };
}

function TeamMiniCard({
  card,
  enriched,
  onOpen,
  showOwner = false,
}: {
  card: TeamQueueCard;
  enriched?: LiveBoardTask;
  onOpen: (taskId: string) => void;
  /** Pool + "Agents & unowned" cards name their owner on the face — the
   * person sections already do that with their heading. */
  showOwner?: boolean;
}) {
  // `card.priority` (server-sent, when present) wins over the reconciled-board
  // enrichment so the chip does not vanish for a card `useLiveBoard` has not
  // caught up with yet. "normal" is the common case — suppress it to avoid a
  // chip on every single card.
  const priority = card.priority ?? enriched?.priority;
  const showPriority = Boolean(priority) && priority !== "normal";
  // Server-truth company (QueueCard widening, 2026-08-13) — name preferred,
  // slug as the fallback; absent on an un-upgraded server, so no chip.
  const company = card.company_name ?? card.company_slug ?? null;
  const owner = showOwner ? employeeName(card.owner_employee_id) : null;
  // Tri-state (migration 132): verified ✓ / failed_verification ✗ / unverified
  // ○ — `completionStateOf` returns `null` for any non-"done" card, so this
  // naturally renders only on done-today cards without a bucket-specific
  // gate. `enriched.status` (from the SAME reconciled-board row as the three
  // verification columns) wins over `card.status` (the QueueCard) so the two
  // can never disagree with each other the way a stale QueueCard status and
  // a fresher enrichment status could.
  //
  // NO enrichment yet is INDETERMINATE, not "unverified": spreading an
  // `undefined` `enrichedFields` into `completionStateOf` used to leave
  // `verified_at`/`verification_failed_at` both absent, which reads
  // identically to "the server confirmed neither is set" — the exact
  // favourable-absence bug a card `useLiveBoard` has not caught up with yet
  // would otherwise flash as "unverified" instead of showing nothing until a
  // real answer arrives. `enriched` gates the whole derivation: no
  // reconciled row, no badge.
  const enrichedFields = enriched as EnrichedTeamTask | undefined;
  // `enrichedFields.status` (always present once enrichment exists) drives
  // the "done" check — no `card.status` fallback needed inside this branch.
  const completion = enrichedFields ? completionStateOf(enrichedFields) : null;
  const blocked = card.status === "blocked" && enriched ? blockedHint(enriched) : null;
  return (
    <button
      type="button"
      className={styles.card}
      onClick={() => onOpen(card.id)}
      aria-label={`Open ${card.title}`}
    >
      <span className={styles.cardTags}>
        {card.ref ? <Badge tone="neutral" className={styles.refBadge}>{card.ref}</Badge> : null}
        <Badge tone="neutral" className={styles.sizeChip}>{card.size}</Badge>
        {company ? (
          <Badge tone="neutral" className={styles.companyChip} title="Company">{company}</Badge>
        ) : null}
        {showPriority ? <Badge tone={priorityTone(priority as string)}>{priority}</Badge> : null}
        {owner ? (
          <span className={styles.cardOwner} title={`Owner: ${owner}`}>{owner}</span>
        ) : null}
        {completion === "verified" ? (
          <span
            className={styles.verifiedMark}
            title={enrichedFields?.verified_by ? `Verified by ${employeeName(enrichedFields.verified_by) ?? enrichedFields.verified_by}` : "Verified"}
          >
            ✓
          </span>
        ) : completion === "failed_verification" ? (
          <span
            className={styles.failedMark}
            title={
              enrichedFields?.verification_failed_reason
                ? `Verification failed: ${enrichedFields.verification_failed_reason}`
                : "Verification failed"
            }
          >
            ✗
          </span>
        ) : completion === "unverified" ? (
          <span className={styles.unverifiedMark} title="Unverified">
            ○
          </span>
        ) : null}
      </span>
      <span className={styles.cardTitle}>{card.title}</span>
      {blocked ? (
        <span className={styles.cardBlocked} title={blocked.full}>
          ⚠ {blocked.short}
        </span>
      ) : null}
    </button>
  );
}

function PersonSection({
  label,
  bucket,
  taskMap,
  onOpen,
  collapsible = false,
  defaultCollapsed = false,
  showOwner = false,
}: {
  label: string;
  bucket: TeamQueueBuckets | null;
  taskMap: Map<string, LiveBoardTask>;
  onOpen: (taskId: string) => void;
  collapsible?: boolean;
  defaultCollapsed?: boolean;
  showOwner?: boolean;
}) {
  const [collapsed, setCollapsed] = useState(defaultCollapsed);
  if (!bucket) return null;
  const total = BUCKET_ORDER.reduce((sum, { key }) => sum + bucket.counts[key], 0);

  return (
    <Card className={styles.personCard} padding="sm">
      <div className={styles.personHead}>
        {collapsible ? (
          <button
            type="button"
            className={styles.personToggle}
            aria-expanded={!collapsed}
            onClick={() => setCollapsed((current) => !current)}
          >
            <span className={styles.chevron} aria-hidden="true">{collapsed ? "›" : "⌄"}</span>
            <h3 className={styles.personName}>{label}</h3>
          </button>
        ) : (
          <h3 className={styles.personName}>{label}</h3>
        )}
        {bucket.active_below_5 ? (
          <Badge tone="warn">below target: {bucket.counts.active}/5 active</Badge>
        ) : null}
        <span className={styles.personCount}>{total} card{total === 1 ? "" : "s"}</span>
        {bucket.ready_below_5 ? (
          <Badge tone="warn" title="Fewer than 5 Ready cards">Low on Ready</Badge>
        ) : null}
      </div>
      {!collapsed ? (
        <div className={styles.bucketGrid}>
          {BUCKET_ORDER.map(({ key, label: bucketLabel }) => (
            <div key={key} className={styles.bucketCol}>
              <div className={styles.bucketHead}>
                <span>{bucketLabel}</span>
                <span className={styles.bucketCount}>{bucket.counts[key]}</span>
              </div>
              <div className={styles.bucketBody}>
                {bucket[key].length === 0 ? (
                  <p className={styles.bucketEmpty}>Nothing here</p>
                ) : (
                  bucket[key].map((card) => (
                    <TeamMiniCard key={card.id} card={card} enriched={taskMap.get(card.id)} onOpen={onOpen} showOwner={showOwner} />
                  ))
                )}
              </div>
            </div>
          ))}
        </div>
      ) : null}
    </Card>
  );
}

function PoolColumn({ pool, taskMap, onOpen }: { pool: TeamQueuePool; taskMap: Map<string, LiveBoardTask>; onOpen: (taskId: string) => void }) {
  const visibleCards = pool.cards.slice(0, POOL_CARD_LIMIT);
  const moreCount = Math.max(0, pool.depth - visibleCards.length);
  return (
    <Card className={styles.personCard} padding="sm">
      <div className={styles.bucketHead}>
        <h3 className={styles.personName}>Pool</h3>
        <span className={styles.bucketCount}>{pool.depth}</span>
      </div>
      {pool.low ? <Badge tone="warn" title="The pool is running low">Low pool</Badge> : null}
      <div className={styles.bucketGrid}>
        <div className={styles.bucketCol}>
          <div className={styles.bucketBody}>
            {visibleCards.length === 0 ? (
              <p className={styles.bucketEmpty}>Nothing here</p>
            ) : (
              visibleCards.map((card) => <TeamMiniCard key={card.id} card={card} enriched={taskMap.get(card.id)} onOpen={onOpen} showOwner />)
            )}
            {moreCount > 0 ? <p className={styles.bucketEmpty}>+{moreCount} more</p> : null}
          </div>
        </div>
      </div>
    </Card>
  );
}

export function TeamBoard({ onOpenTask }: { onOpenTask: (taskId: string) => void }) {
  const { board, loading, error, hasLoaded, refresh } = useTeamBoard();
  const { tasks: liveTasks } = useLiveBoard();
  const { nodes: projectNodes } = useProjectTree();
  const parsedBoard = useMemo(() => parseTeamBoard(board), [board]);
  const [companyFilter, setCompanyFilter] = useState(ALL_COMPANIES_VALUE);
  const [projectFilter, setProjectFilter] = useState(ALL_PROJECTS_VALUE);

  const taskMap = useMemo(
    () => new Map(liveTasks.map((task) => [task.id, task] as const)),
    [liveTasks],
  );

  const companyOptions = useMemo(() => {
    const slugs = new Set<string>();
    for (const task of liveTasks) {
      const slug = task.org?.organization_context?.company_slug;
      if (slug) slugs.add(slug);
    }
    return [
      { value: ALL_COMPANIES_VALUE, label: "All companies" },
      ...[...slugs].sort().map((slug) => ({ value: slug, label: slug })),
    ];
  }, [liveTasks]);

  // One level deep, same idiom as /board's project scope filter — the tree
  // structure itself is navigable from /projects.
  const projectOptions = useMemo(() => {
    const options: Array<{ value: string; label: string }> = [{ value: ALL_PROJECTS_VALUE, label: "All projects" }];
    const flatten = (nodes: typeof projectNodes) => {
      for (const node of nodes) {
        options.push({ value: node.project.id, label: node.project.name });
        if (node.sub_projects?.length) flatten(node.sub_projects);
      }
    };
    flatten(projectNodes);
    return options;
  }, [projectNodes]);

  const namedSections = NAMED_EMPLOYEE_IDS.map((id) => {
    const bucket = parsedBoard.buckets[id] ?? null;
    return {
      id,
      bucket: bucket ? filterBucketCards(bucket, taskMap, companyFilter, projectFilter) : null,
    };
  });
  const poolCardIds = useMemo(() => new Set(parsedBoard.pool?.cards.map((card) => card.id) ?? []), [parsedBoard.pool]);
  const otherBuckets = useMemo(
    () =>
      Object.entries(parsedBoard.buckets)
        .filter(([id]) => !(NAMED_EMPLOYEE_IDS as readonly string[]).includes(id))
        .map(([, bucket]) => bucket as TeamQueueBuckets),
    [parsedBoard],
  );
  const agentsBucket = useMemo(
    () => buildAgentsBucket(otherBuckets, liveTasks, undefined, poolCardIds),
    [otherBuckets, liveTasks, poolCardIds],
  );
  const filteredAgentsBucket = useMemo(
    () => (agentsBucket ? filterBucketCards(agentsBucket, taskMap, companyFilter, projectFilter) : null),
    [agentsBucket, taskMap, companyFilter, projectFilter],
  );
  const filteredPool = useMemo(
    () =>
      parsedBoard.pool
        ? { ...parsedBoard.pool, cards: filterCards(parsedBoard.pool.cards, taskMap, companyFilter, projectFilter) }
        : null,
    [parsedBoard.pool, taskMap, companyFilter, projectFilter],
  );

  if (loading && !hasLoaded) {
    return <Loading variant="skeleton" label="Loading the team board" lines={6} />;
  }
  if (error && !hasLoaded) {
    return <ErrorState message={error} onRetry={() => void refresh()} />;
  }

  return (
    <ErrorBoundary label="The team board">
      <div className={styles.filterRow} role="group" aria-label="Filter the team board">
        <Select label="Company" value={companyFilter} onChange={setCompanyFilter} options={companyOptions} />
        <Select label="Project" value={projectFilter} onChange={setProjectFilter} options={projectOptions} />
      </div>
      <div className={styles.board} aria-label="Team board">
        {filteredPool ? <PoolColumn pool={filteredPool} taskMap={taskMap} onOpen={onOpenTask} /> : null}
        {namedSections.map(({ id, bucket }) => (
          <PersonSection
            key={id}
            label={employeeName(id) ?? id}
            bucket={bucket}
            taskMap={taskMap}
            onOpen={onOpenTask}
          />
        ))}
        <PersonSection
          label={AGENTS_LABEL}
          bucket={filteredAgentsBucket}
          taskMap={taskMap}
          onOpen={onOpenTask}
          collapsible
          defaultCollapsed
          showOwner
        />
      </div>
    </ErrorBoundary>
  );
}
