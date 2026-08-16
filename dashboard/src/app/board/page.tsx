"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, type MouseEvent as ReactMouseEvent, useEffect, useMemo, useRef, useState } from "react";
import { Badge, Button, EmptyState, ErrorBoundary, ErrorState, Loading, Page, PageHeader, Select } from "@/design";
import { USE_FIXTURES } from "@/features/collab/fixtures";
import { BoardFilesDrawer } from "@/features/collab/BoardFilesDrawer";
import { collabApi, revealBoardWorkspace } from "@/features/collab/client";
import { api } from "@/lib/api";
import { useAgents, useLiveBoard } from "@/features/collab/hooks";
import type { LiveBoardTask, TaskCategory } from "@/features/collab/types";
import { AGENTS_OWNER_FILTER, deriveRunOptions, filterBoardTasks, isObservationalCard, missingRunOption, type BoardFilters } from "@/features/board/filters";
import { NAMED_EMPLOYEE_IDS, employeeName } from "@/features/team/types";
import { useProjectTree } from "@/features/projects/hierarchyHooks";
import { BoardKanban } from "@/features/board/BoardKanban";
import { InsightsStrip, ObservationalToggle, StalenessToggle } from "@/features/board/InsightsStrip";
import { OpenClaimsStrip } from "@/features/board/OpenClaimsStrip";
import { BoardProgress } from "@/features/board/BoardProgress";
import { NeedsResponseQueue } from "@/features/board/NeedsResponseQueue";
import { TaskDetailDrawer } from "@/features/board/TaskDetailDrawer";
import { summarizeBoard } from "@/features/board/summary";
import { runGranularityStaleHideSet, taskStaleness } from "@/features/board/staleness";
import { type PhaseOverlay, SETTLED_TASK_STATUSES } from "@/features/swarm/columns";
import { useSwarmFleet, useSwarmOverview } from "@/features/swarm/hooks";
import styles from "@/features/board/board.module.css";

/** The five companies (org_companies slugs) the board can scope to — a static
 * chip group like the owner one; cards match on server-truth `company_slug`. */
const COMPANY_CHIP_OPTIONS: Array<{ value: string; label: string }> = [
  { value: "", label: "All" },
  { value: "globex", label: "Globex" },
  { value: "acmeuni", label: "AcmeUni" },
  { value: "hooli", label: "Hooli" },
  { value: "initech", label: "Initech" },
  { value: "omniagentos", label: "OmniAgentOS" },
];

/** localStorage key for the "show terminal-session sighting cards" toggle
 * (default OFF — 668/3,710 cards at filing were auto-discovered sightings,
 * not real tasks). */
const SHOW_OBSERVATIONAL_KEY = "board.showObservationalCards";

/** F01: Web Storage can throw on read OR write (disabled by the user,
 * private-browsing quota, an iframe without storage permission, or simply
 * absent in a stricter host) -- never let that unmount the whole board.
 * Both helpers swallow the failure and treat it as "no preference stored". */
function safeLocalStorageGet(key: string): string | null {
  try {
    if (typeof window === "undefined") return null;
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

function safeLocalStorageSet(key: string, value: string): void {
  try {
    if (typeof window === "undefined") return;
    window.localStorage.setItem(key, value);
  } catch {
    // Best-effort only -- the toggle still works for the rest of this
    // session, it just won't persist across a reload.
  }
}

/**
 * The full board page: THE kanban (features/board/BoardKanban — shared with
 * the home cockpit's expandable board) plus the filter toolbar (title ·
 * discipline · category · project · swarm-run), selection/bulk-archive, and the
 * files drawer. Data is GET /api/board (each card reconciled from its linked
 * run), live via SSE + a 30s poll, conditional on the board ETag. /swarm is the
 * ops Command Center and links its runs here via ?run=.
 *
 * Three view toggles used to sit on this toolbar (Kanban · Matrix · Portfolio).
 * The board is the kanban; /orgdims keeps the matrix and portfolio views, which
 * is where a reader looking for them goes.
 */
function BoardPageContent() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const [showArchived, setShowArchived] = useState(false);
  const [projectFilter, setProjectFilter] = useState("");
  const { nodes: projectNodes } = useProjectTree();
  const { tasks, loading, error, hasLoaded, reconnecting, connected, needsResponseIds, refresh, archiveTask, archiveTasks } = useLiveBoard(showArchived, projectFilter || undefined);
  const { agents, error: agentsError } = useAgents();
  const [discipline, setDiscipline] = useState("");
  const [titleQuery, setTitleQuery] = useState("");
  const [categoryFilter, setCategoryFilter] = useState("");
  const [runFilter, setRunFilter] = useState("");
  const [ownerFilter, setOwnerFilter] = useState("");
  const [companyFilter, setCompanyFilter] = useState("");
  const [showStale, setShowStale] = useState(false);
  const [showObservational, setShowObservational] = useState(false);
  useEffect(() => {
    setShowObservational(safeLocalStorageGet(SHOW_OBSERVATIONAL_KEY) === "true");
  }, []);
  const toggleShowObservational = () => {
    setShowObservational((current) => {
      const next = !current;
      safeLocalStorageSet(SHOW_OBSERVATIONAL_KEY, String(next));
      return next;
    });
  };
  const [archivingId, setArchivingId] = useState<string | null>(null);
  const [pausingId, setPausingId] = useState<string | null>(null);
  const [approvingId, setApprovingId] = useState<string | null>(null);
  const [terminalOpeningId, setTerminalOpeningId] = useState<string | null>(null);
  const [selectionMode, setSelectionMode] = useState(false);
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [bulkArchiving, setBulkArchiving] = useState(false);
  const [archiveNotice, setArchiveNotice] = useState<string | null>(null);
  const [filesTaskId, setFilesTaskId] = useState<string | null>(null);
  const [archivedFilesTask, setArchivedFilesTask] = useState<LiveBoardTask | null>(null);
  const [categories, setCategories] = useState<TaskCategory[]>([]);
  const loadingCategoriesRef = useRef(false);

  // Live swarm phase overlay for the Review/Testing/Integration columns. Degrades
  // silently: on a 404/error the hook keeps `overview` null and the map is empty,
  // so every swarm card simply stays in Running — exactly the /swarm behaviour.
  const { overview } = useSwarmOverview();
  const phaseOverlay = useMemo<PhaseOverlay>(
    () => new Map((overview?.tasks ?? []).map((task) => [task.task_id, task.phase])),
    [overview],
  );
  // Fleet goals label the swarm-run select; null (endpoint absent) → short ids.
  const { fleet } = useSwarmFleet();
  const runGoals = useMemo(
    () => new Map((fleet?.runs ?? []).map((run) => [run.id, run.goal])),
    [fleet],
  );

  useEffect(() => {
    if (showArchived) {
      setSelectionMode(false);
      setSelectedIds([]);
    }
  }, [showArchived]);

  useEffect(() => {
    const taskId = searchParams.get("task");
    setFilesTaskId(searchParams.get("files") === "1" && taskId ? taskId : null);
    setArchivedFilesTask(null);
  }, [searchParams]);

  // Deep-link: /board?task=<id> opens the everything drawer (§2.5).
  const drawerTaskId = searchParams.get("task") ?? null;

  // Deep-link: /board?run=<id> (from a /swarm fleet click or an old Swarm badge)
  // seeds the swarm-run filter. The select is then the source of truth.
  const runParam = searchParams.get("run");
  useEffect(() => {
    if (runParam) setRunFilter(runParam);
  }, [runParam]);

  // Deep-link: /board?project=<id> scopes the board to a single project,
  // showing its promoted chats, companion tasks, and spawned sub-tasks.
  const projectParam = searchParams.get("project");
  useEffect(() => {
    if (projectParam) setProjectFilter(projectParam);
  }, [projectParam]);

  // Fetch categories on mount — feeds the category filter and the per-card pill.
  useEffect(() => {
    const fetchCategories = async () => {
      if (loadingCategoriesRef.current) return;
      loadingCategoriesRef.current = true;
      try {
        const cats = await collabApi.fetchCategories();
        setCategories(cats);
      } catch (reason) {
        // Silently fail if categories endpoint is not available
        console.debug("Categories not available:", reason);
      } finally {
        loadingCategoriesRef.current = false;
      }
    };
    void fetchCategories();
  }, []);

  const disciplines = useMemo(
    () => [...new Set(tasks.map((task) => task.discipline).filter((value): value is string => Boolean(value)))],
    [tasks],
  );
  const runOptions = useMemo(() => deriveRunOptions(tasks, runGoals), [tasks, runGoals]);
  // Flatten the project tree into a list of Select options for the project
  // scope filter. One level deep is sufficient for the toolbar — the tree
  // structure is navigable from /projects.
  const projectOptions = useMemo(() => {
    const options: Array<{ value: string; label: string }> = [{ value: "", label: "All projects" }];
    const flatten = (nodes: typeof projectNodes) => {
      for (const node of nodes) {
        options.push({ value: node.project.id, label: node.project.name });
        if (node.sub_projects?.length) flatten(node.sub_projects);
      }
    };
    flatten(projectNodes);
    return options;
  }, [projectNodes]);
  const categoryMap = useMemo(() => {
    const map: Record<string, TaskCategory> = {};
    categories.forEach((cat) => { map[cat.id] = cat; });
    return map;
  }, [categories]);

  const filters = useMemo<BoardFilters>(
    () => ({ discipline, titleQuery, categoryId: categoryFilter, runId: runFilter, projectId: projectFilter, owner: ownerFilter, companyId: companyFilter }),
    [discipline, titleQuery, categoryFilter, runFilter, projectFilter, ownerFilter, companyFilter],
  );
  const ownerChipOptions = useMemo(
    () => [
      { value: "", label: "All" },
      ...NAMED_EMPLOYEE_IDS.map((id) => ({ value: id, label: employeeName(id) ?? id })),
      { value: AGENTS_OWNER_FILTER, label: "Agents" },
    ],
    [],
  );
  const filteredTasks = useMemo(() => filterBoardTasks(tasks, filters), [tasks, filters]);
  // Auto-discovered terminal-session sighting cards ("[claude · external]
  // host · tty") are not real filed tasks — hidden by default, toggle-able,
  // count always visible. Applied client-side; the board fetch itself is
  // unchanged (shared API, no server-side filtering added here).
  const observationalHideCount = useMemo(
    () => filteredTasks.filter((task) => isObservationalCard(task.title)).length,
    [filteredTasks],
  );
  const observationalFilteredTasks = useMemo(
    () => showObservational ? filteredTasks : filteredTasks.filter((task) => !isObservationalCard(task.title)),
    [filteredTasks, showObservational],
  );
  // P0-1: preserve every member of a live swarm run. A run can disappear only
  // when all of its cards are terminal and stale; solo cards remain independent.
  //
  // F09: this MUST run over `filteredTasks` (every card the toolbar filters
  // let through, observational sightings included), never the further-
  // narrowed `observationalFilteredTasks`. The observational toggle is a
  // pure render-time hide; it must not feed the run-level "is this whole run
  // settled" decision. Otherwise a hidden-but-fresh observational sibling
  // silently drops out of `runTasks.every(isTerminalAndStale)`, and a swarm
  // run with a genuinely live member gets grouped as fully stale -- exactly
  // because we hid the one card that would have kept it visible. Hidden-count
  // (`observationalHideCount`, above) is the one legitimate exception: by
  // definition it counts against the pre-observational-filter population.
  const staleHideSet = useMemo(
    () => runGranularityStaleHideSet(
      filteredTasks,
      (task) => SETTLED_TASK_STATUSES.has(task.status) && taskStaleness(task.updated_at).stale,
    ),
    [filteredTasks],
  );
  const visibleTasks = useMemo(
    () => showStale ? observationalFilteredTasks : observationalFilteredTasks.filter((task) => !staleHideSet.has(task.id)),
    [observationalFilteredTasks, showStale, staleHideSet],
  );
  // Requested-task granularity: one unit per swarm run, one per solo card — so
  // the strip answers "how far along is what I asked for", not "how many cards
  // exist". Computed over the FILTERED set so hidden stale runs still count
  // toward the BoardProgress headline.
  const summary = useMemo(() => summarizeBoard(filteredTasks, phaseOverlay), [filteredTasks, phaseOverlay]);

  // A ?run= deep link can point at a run with no cards on the board (stale
  // link, archived run). The filter must stay VISIBLE and clearable: the run
  // select renders whenever the filter is active, surfacing the missing run as
  // a labelled "(not on board)" option rather than an invisible dead-end.
  const runFilterMissing = Boolean(runFilter) && !runOptions.some((run) => run.id === runFilter);
  const runSelectOptions = useMemo(() => {
    const options = [{ value: "", label: "All runs" }, ...runOptions.map((run) => ({ value: run.id, label: run.label }))];
    if (runFilterMissing) {
      const missing = missingRunOption(runFilter);
      options.push({ value: missing.id, label: missing.label });
    }
    return options;
  }, [runOptions, runFilter, runFilterMissing]);

  const clearFilters = () => {
    setDiscipline("");
    setTitleQuery("");
    setCategoryFilter("");
    setRunFilter("");
    setProjectFilter("");
    setOwnerFilter("");
    setCompanyFilter("");
  };

  const handleArchive = async (taskId: string, event: ReactMouseEvent) => {
    event.preventDefault();
    event.stopPropagation();
    setArchivingId(taskId);
    try {
      await archiveTask(taskId);
      setSelectedIds((current) => current.filter((id) => id !== taskId));
    } finally {
      setArchivingId(null);
    }
  };

  /** Approve the command parking a card, straight from the board. Without this the
   * operator has to find the session, open its dialog, and decide there -- which is
   * why a card sat blocked for hours. */
  const handleApprove = async (approvalId: string, _taskId: string, event: ReactMouseEvent) => {
    event.preventDefault();
    event.stopPropagation();
    setApprovingId(approvalId);
    setArchiveNotice(null);
    try {
      await api.decideApproval(approvalId, "approved");
      setArchiveNotice("Approved — the session resumes on its next poll.");
      refresh();
    } catch (reason) {
      setArchiveNotice(reason instanceof Error ? `Approve failed: ${reason.message}` : "Approve failed.");
    } finally {
      setApprovingId(null);
    }
  };

  /** Escape hatch for sessions whose output does not stream to the browser: open a
   * real terminal at the card's workspace. The SERVER opens it -- the browser cannot. */
  const handleOpenTerminal = async (taskId: string, event: ReactMouseEvent) => {
    event.preventDefault();
    event.stopPropagation();
    setTerminalOpeningId(taskId);
    setArchiveNotice(null);
    try {
      const result = await revealBoardWorkspace(taskId, { app: "terminal" });
      if (!result.revealed) setArchiveNotice("Terminal did not open — local reveal may be disabled on this host.");
    } catch (reason) {
      setArchiveNotice(reason instanceof Error ? `Terminal failed: ${reason.message}` : "Terminal failed.");
    } finally {
      setTerminalOpeningId(null);
    }
  };

  const handlePause = async (taskId: string, event: ReactMouseEvent) => {
    event.preventDefault();
    event.stopPropagation();
    setPausingId(taskId);
    setArchiveNotice(null);
    try {
      await collabApi.pauseTask(taskId);
      setArchiveNotice("Paused — resume any time from the card or its details page.");
      refresh();
    } catch (reason) {
      setArchiveNotice(reason instanceof Error ? `Pause failed: ${reason.message}` : "Pause failed.");
    } finally {
      setPausingId(null);
    }
  };

  const handleResume = async (taskId: string, event: ReactMouseEvent) => {
    event.preventDefault();
    event.stopPropagation();
    setPausingId(taskId);
    setArchiveNotice(null);
    try {
      await collabApi.retryTask(taskId);
      setArchiveNotice("Resuming…");
      refresh();
    } catch (reason) {
      setArchiveNotice(reason instanceof Error ? `Resume failed: ${reason.message}` : "Resume failed.");
    } finally {
      setPausingId(null);
    }
  };

  const toggleSelection = (taskId: string) => {
    setSelectedIds((current) => current.includes(taskId) ? current.filter((id) => id !== taskId) : [...current, taskId]);
  };

  const handleArchiveSelected = async () => {
    if (!selectedIds.length) return;
    setBulkArchiving(true);
    setArchiveNotice(null);
    try {
      const result = await archiveTasks(selectedIds);
      if (result.skipped.length) setArchiveNotice(`${result.archived.length} archived; ${result.skipped.length} skipped.`);
      else setArchiveNotice(`${result.archived.length} archived.`);
      setSelectedIds([]);
    } catch (reason) {
      setArchiveNotice(reason instanceof Error ? `Archive failed: ${reason.message}` : "Archive failed.");
    } finally {
      setBulkArchiving(false);
    }
  };

  const openFiles = (taskId: string) => {
    router.push(`/board?task=${encodeURIComponent(taskId)}&files=1`, { scroll: false });
  };

  // Selection state lives in the URL and is back-button-correct (§2.1): push
  // on open so Back returns to the previously viewed card.
  const openTask = (taskId: string) => {
    router.push(`/board?task=${encodeURIComponent(taskId)}`, { scroll: false });
  };

  const closeTask = () => {
    // Closing keeps the project scope in the URL (state and URL must agree).
    router.replace(
      projectFilter ? `/board?project=${encodeURIComponent(projectFilter)}` : "/board",
      { scroll: false },
    );
  };

  const closeFiles = () => {
    setFilesTaskId(null);
    router.replace(
      projectFilter ? `/board?project=${encodeURIComponent(projectFilter)}` : "/board",
      { scroll: false },
    );
  };

  const filesTask = filesTaskId ? tasks.find((task) => task.id === filesTaskId) ?? (archivedFilesTask?.id === filesTaskId ? archivedFilesTask : null) : null;

  // D-2 (LiveSim LS-004): the board has never successfully loaded AND the
  // current fetch failed -- the true card count is UNKNOWN, not zero. Must
  // never render "0 of 0 cards" here: that reads as a confirmed-empty board,
  // indistinguishable from a genuinely empty one, and this is the toolbar's
  // only permanent (non-dismissable) count readout. Once `hasLoaded` flips
  // true the count is real again, even if a later refresh errors (stale-but-
  // known beats hidden-but-unknown).
  const countUnknown = !hasLoaded && Boolean(error);

  useEffect(() => {
    if (!filesTaskId || !hasLoaded || tasks.some((task) => task.id === filesTaskId) || archivedFilesTask?.id === filesTaskId) return;
    let active = true;
    void collabApi.liveBoard({ archived: true }).then((archivedTasks) => {
      if (!active) return;
      const archivedTask = archivedTasks.find((task) => task.id === filesTaskId);
      if (archivedTask) {
        setArchivedFilesTask(archivedTask);
        return;
      }
      setArchiveNotice("Task not found — it may have been deleted");
      setFilesTaskId(null);
      router.replace("/board", { scroll: false });
    }).catch((reason) => {
      if (active) setArchiveNotice(reason instanceof Error ? `Could not check archived tasks: ${reason.message}` : "Could not check archived tasks.");
    });
    return () => { active = false; };
  }, [archivedFilesTask, filesTaskId, hasLoaded, router, tasks]);

  return (
    <Page className={styles.page}>
      <PageHeader
        eyebrow="Collaboration"
        title="Live task board"
        lead="Work dispatched from chat lands here and moves in real time as agents claim and complete it — one kanban for every card, swarm runs included."
        actions={
          USE_FIXTURES ? (
            <Badge tone="warn">Dev fixtures</Badge>
          ) : (
            <Badge tone={connected ? "ok" : "neutral"}>
              {connected ? "Live events connected" : "Live events reconnecting…"}
            </Badge>
          )
        }
      />
      <OpenClaimsStrip />
      {!loading && hasLoaded ? <InsightsStrip tasks={tasks} agents={agents} agentsError={agentsError} /> : null}
      {!loading && hasLoaded && !showArchived ? <BoardProgress summary={summary} /> : null}
      {/* §2: Needs Response rises above the kanban — strict predicate, real command. */}
      {!loading && hasLoaded && !showArchived ? (
        <NeedsResponseQueue
          tasks={tasks}
          hideWhenEmpty
          approvingId={approvingId}
          onApprove={handleApprove}
        />
      ) : null}
      <section className={styles.boardToolbar} aria-label="Board controls">
        <label className={styles.searchField}>
          <span className={styles.srOnly}>Search titles</span>
          <input
            type="search"
            placeholder="Search titles…"
            value={titleQuery}
            onChange={(event) => setTitleQuery(event.target.value)}
            aria-label="Search titles"
          />
        </label>
        <Select
          label="Discipline"
          value={discipline}
          onChange={setDiscipline}
          options={[{ value: "", label: "All disciplines" }, ...disciplines.map((value) => ({ value, label: value }))]}
        />
        {categories.length > 0 ? (
          <Select
            label="Category"
            value={categoryFilter}
            onChange={setCategoryFilter}
            options={[{ value: "", label: "All categories" }, ...categories.map((cat) => ({ value: cat.id, label: cat.name }))]}
          />
        ) : null}
        {projectOptions.length > 1 || projectFilter ? (
          <Select
            label="Project"
            value={projectFilter}
            onChange={setProjectFilter}
            options={projectOptions}
          />
        ) : null}
        {runOptions.length > 0 || runFilter ? (
          <Select
            label="Swarm run"
            value={runFilter}
            onChange={setRunFilter}
            options={runSelectOptions}
          />
        ) : null}
        <div className={styles.ownerChips} role="group" aria-label="Filter by owner">
          {ownerChipOptions.map((option) => (
            <button
              key={option.value || "all"}
              type="button"
              className={styles.ownerChip}
              aria-pressed={ownerFilter === option.value}
              onClick={() => setOwnerFilter((current) => (current === option.value ? "" : option.value))}
            >
              {option.label}
            </button>
          ))}
        </div>
        <div className={styles.ownerChips} role="group" aria-label="Filter by company">
          {COMPANY_CHIP_OPTIONS.map((option) => (
            <button
              key={option.value || "all"}
              type="button"
              className={styles.ownerChip}
              aria-pressed={companyFilter === option.value}
              onClick={() => setCompanyFilter((current) => (current === option.value ? "" : option.value))}
            >
              {option.label}
            </button>
          ))}
        </div>
      </section>
      <div className={styles.metaRow}>
        {staleHideSet.size > 0 ? (
          <StalenessToggle
            hiddenCount={staleHideSet.size}
            showStale={showStale}
            onToggle={() => setShowStale((current) => !current)}
          />
        ) : null}
        <ObservationalToggle
          hiddenCount={observationalHideCount}
          showObservational={showObservational}
          onToggle={toggleShowObservational}
        />
        <span className={styles.metaCount}>
          {countUnknown
            ? "Card count unavailable — could not reach the server."
            : `${visibleTasks.length} of ${tasks.length} card${tasks.length === 1 ? "" : "s"} ${showArchived ? "archived." : "on the board."}`}
        </span>
        <span className={styles.metaSpacer} />
        {!showArchived ? (
          <Button variant="ghost" size="sm" aria-pressed={selectionMode} onClick={() => { setSelectionMode((current) => !current); setSelectedIds([]); }}>
            {selectionMode ? "Done selecting" : "Select"}
          </Button>
        ) : null}
        <Button
          variant="ghost"
          size="sm"
          aria-pressed={showArchived}
          onClick={() => setShowArchived((current) => !current)}
        >
          {showArchived ? "Showing archived" : "Show archived"}
        </Button>
      </div>
      {!showArchived && selectionMode ? (
        <div className={styles.selectionToolbar} role="toolbar" aria-label="Bulk archive controls">
          <span>{selectedIds.length} selected</span>
          <Button variant="primary" size="sm" disabled={!selectedIds.length || bulkArchiving} onClick={() => void handleArchiveSelected()}>
            {bulkArchiving ? "Archiving…" : "Archive selected"}
          </Button>
        </div>
      ) : null}
      {archiveNotice ? <p className={styles.archiveNotice} role="status">{archiveNotice}</p> : null}

      {reconnecting && hasLoaded ? <div className={styles.reconnectingBanner} role="status">Reconnecting — data may be stale</div> : null}

      {loading ? <Loading variant="skeleton" label="Loading the board" lines={6} /> : null}
      {!loading && !hasLoaded && error ? <ErrorState message={error} onRetry={refresh} /> : null}
      {!loading && hasLoaded && tasks.length === 0 ? (
        projectFilter && !showArchived ? (
          // §2.8: with server-side scoping (§3.9) `tasks` IS the scoped list,
          // so an empty list means the project has no cards — the pinned
          // scoped empty state + pre-087 disclosure, not the global one.
          <EmptyState
            title="No cards in this project yet"
            message="Promote a chat to this project, or clear the filter. Cards created before project tracking aren't scoped — clear the filter to see everything."
            action={<Button variant="secondary" size="sm" onClick={clearFilters}>Clear filters</Button>}
          />
        ) : (
          <EmptyState
            title={showArchived ? "No archived tasks" : "No tasks on the board"}
            message={showArchived ? "Tasks you archive from the Completed column will show up here." : "Dispatch a task from chat and it will appear here in Backlog, then move across the columns as an agent works it."}
          />
        )
      ) : null}
      {!loading && hasLoaded && tasks.length > 0 && visibleTasks.length === 0 ? (
        <EmptyState
          title="No matching cards"
          message={
            runFilterMissing
              ? "The linked swarm run has no cards on the board — it may have finished and been archived. Clear the filters to see the full board."
              : "No cards match the current filters. Clear a filter to widen the view."
          }
          action={<Button variant="secondary" size="sm" onClick={clearFilters}>Clear filters</Button>}
        />
      ) : null}

      {/* A render error in ONE card used to unmount the whole tree and replace
          /board with Next's "Application error" screen (a swarm card read the
          run-detail `attempts` map as an array). The boundary keeps the toolbar,
          filters and summary alive and names what broke. */}
      {!loading && visibleTasks.length > 0 ? (
        <ErrorBoundary label="The kanban">
          <BoardKanban
            tasks={visibleTasks}
            agents={agents}
            categoryMap={categoryMap}
            phaseOverlay={phaseOverlay}
            showArchived={showArchived}
            selectionMode={selectionMode}
            selectedIds={selectedIds}
            onToggleSelection={toggleSelection}
            selectionBusy={bulkArchiving || Boolean(archivingId)}
            onOpenTask={openTask}
            onOpenFiles={openFiles}
            onArchive={(taskId, event) => void handleArchive(taskId, event)}
            archivingId={archivingId}
            onPause={(taskId, event) => void handlePause(taskId, event)}
            onResume={(taskId, event) => void handleResume(taskId, event)}
            pausingId={pausingId}
            onApprove={(approvalId, taskId, event) => void handleApprove(approvalId, taskId, event)}
            approvingId={approvingId}
            onOpenTerminal={(taskId, event) => void handleOpenTerminal(taskId, event)}
            terminalOpeningId={terminalOpeningId}
            onSwarmBadge={(runId) => setRunFilter((current) => (current === runId ? "" : runId))}
            activeRunFilter={runFilter}
            // The server owns the Needs You predicate; the client one stays as
            // the SSE-interim fallback inside the kanban.
            needsResponseIds={needsResponseIds}
            // ONE card per run everywhere — except when the user filtered TO a
            // run: then the members ARE the subject and render individually.
            aggregateSwarms={!runFilter}
            onRunMutated={refresh}
          />
        </ErrorBoundary>
      ) : null}
      <BoardFilesDrawer taskId={filesTask?.id ?? null} taskTitle={filesTask?.title ?? "Task"} open={Boolean(filesTaskId && filesTask)} onClose={closeFiles} />
      {drawerTaskId && !filesTaskId ? (
        <TaskDetailDrawer taskId={drawerTaskId} onClose={closeTask} />
      ) : null}
    </Page>
  );
}

export default function BoardPage() {
  return (
    <Suspense fallback={<Page><Loading variant="skeleton" label="Loading the board" lines={6} /></Page>}>
      <BoardPageContent />
    </Suspense>
  );
}
