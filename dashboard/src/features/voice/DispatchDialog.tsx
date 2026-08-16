"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { Badge, Button, Dialog, Input, Select, useToast } from "@/design";
import { createProject } from "@/features/projects/api";
import { fetchProjectTree, type ProjectTreeNode } from "@/features/projects/hierarchy";
import { findNode } from "@/features/projects/hierarchyHooks";
import { SpeedSlider, type Speed } from "@/features/cockpit/SpeedSlider";
import {
  intakeApi,
  MAX_TASK_TITLE_LENGTH,
  planApi,
  voiceDispatchApi,
  type ClarifyTurn,
  type PlanJobState,
  type ProjectPlan,
  type RefinedSpec,
} from "./client";
import styles from "./voice.module.css";

export interface DispatchResult {
  taskId: string;
  runId: string | null;
}

export interface DispatchDialogProps {
  open: boolean;
  text: string;
  onClose: () => void;
  /** Called after a successful task (and optional run) creation. */
  onDispatched: (result: DispatchResult) => void;
}

type Phase = "start" | "planning" | "plan" | "clarifying" | "confirm";

// Sentinels for the project-picker Select's flat string value; anything else is a
// real project/sub-project id (server ids are prefixed e.g. "proj_…", never these).
const AUTO_PROJECT = "auto";
const NO_PROJECT = "none";
const NEW_PROJECT = "new";

const PLANNING_STAGES = [
  "Reading your goal…",
  "Drafting the plan…",
  "Weighing sub-projects and tasks…",
  "Deciding where this belongs…",
];
const PLAN_POLL_MS = 1500;
// Backend planning budget tops out at 5 minutes (Fable effort max); give polling a
// hard ceiling above that so the dialog can never spin forever even if a job is
// somehow stuck.
const PLAN_MAX_WAIT_MS = 360_000;
const PLAN_MAX_POLL_FAILURES = 4;
// Fake-but-honest determinate progress: eases toward the cap over this window,
// then holds there until the job actually finishes (which snaps it to 100%).
const PLAN_PROGRESS_EASE_MS = 45_000;
const PLAN_PROGRESS_CAP = 92;

type FlatProjectOption = { value: string; label: string };

function flattenProjectTree(nodes: ProjectTreeNode[], depth = 0): FlatProjectOption[] {
  const out: FlatProjectOption[] = [];
  for (const node of nodes) {
    const indent = depth > 0 ? `${"  ".repeat(depth - 1)}↳ ` : "";
    out.push({ value: node.project.id, label: `${indent}${node.project.name}` });
    out.push(...flattenProjectTree(node.sub_projects, depth + 1));
  }
  return out;
}

function planTaskTotal(plan: ProjectPlan): number {
  return plan.tasks.length + plan.sub_projects.reduce((n, sp) => n + sp.tasks.length, 0);
}

/**
 * Chat → task intake. The PRIMARY path is the Fable PLAN flow: a goal becomes a
 * background plan job (POST /api/intake/plan), the dialog polls it, then renders
 * Fable's plan (sub-projects + tasks) and route decision (new project vs. nest
 * under an existing one) for the operator to confirm — which materialises the
 * whole hierarchy and dispatches every task. "Clarify with agent" (a short
 * clarifying conversation before a single task) and "Quick dispatch" (immediate,
 * unscoped) remain available as secondary paths.
 *
 * Routing controls collapsed (D10/D11) to a single speed dial (Fast/Auto/Ultra);
 * the old Lane / Execution / Priority selects are gone. Speed rides into
 * intakeApi.dispatch and planApi.confirm; the harness/execution posture fall back
 * to their safe backend defaults (default lane, read-only).
 */
export function DispatchDialog({ open, text, onClose, onDispatched }: DispatchDialogProps) {
  const { push } = useToast();
  const [projectTree, setProjectTree] = useState<ProjectTreeNode[]>([]);
  const [projectTreeLoading, setProjectTreeLoading] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [phase, setPhase] = useState<Phase>("start");
  const [history, setHistory] = useState<ClarifyTurn[]>([]);
  const [questions, setQuestions] = useState<string[]>([]);
  const [answers, setAnswers] = useState<string[]>([]);
  const [spec, setSpec] = useState<RefinedSpec | null>(null);
  const [round, setRound] = useState(0);
  // The Mode dial's speed knob — set once up front, applied to whichever path the
  // operator chooses (plan / clarify / quick).
  const [speed, setSpeed] = useState<Speed>("auto");

  // Clarify-confirm's project selector — simple (no Fable routing ran for this
  // path, so there is no "Auto" to defer to): "No project" / an existing
  // project-or-sub-project id / "+ Create new project".
  const [clarifyProjectSelection, setClarifyProjectSelection] = useState(NO_PROJECT);
  const [clarifyNewProjectName, setClarifyNewProjectName] = useState("");

  // Fable plan flow state.
  const [planJobId, setPlanJobId] = useState<string | null>(null);
  const [planJob, setPlanJob] = useState<PlanJobState | null>(null);
  const [planElapsedMs, setPlanElapsedMs] = useState(0);
  const planStartedAtRef = useRef<number | null>(null);
  // The project-override selector shown on the plan-preview screen: "Auto" (defer
  // to Fable's own route decision) / an existing project-or-sub-project id /
  // "+ Create new project".
  const [planProjectSelection, setPlanProjectSelection] = useState(AUTO_PROJECT);
  const [planNewProjectName, setPlanNewProjectName] = useState("");

  const inFlightRef = useRef(false);

  useEffect(() => {
    if (!open) return;
    let active = true;
    setProjectTreeLoading(true);
    void fetchProjectTree()
      .then((nodes) => { if (active) setProjectTree(nodes); })
      .catch(() => {
        if (!active) return;
        setProjectTree([]);
        push({ title: "Couldn't load projects", message: "Project list failed to load — you can still dispatch without one, or type a new project name.", tone: "warn" });
      })
      .finally(() => { if (active) setProjectTreeLoading(false); });
    return () => { active = false; };
  }, [open, push]);

  useEffect(() => {
    if (open) {
      setError(null); setSpeed("auto");
      setPhase("start"); setHistory([]); setQuestions([]); setAnswers([]); setSpec(null); setRound(0);
      setClarifyProjectSelection(NO_PROJECT); setClarifyNewProjectName("");
      setPlanJobId(null); setPlanJob(null); setPlanElapsedMs(0); planStartedAtRef.current = null;
      setPlanProjectSelection(AUTO_PROJECT); setPlanNewProjectName("");
    }
  }, [open]);

  // Ticks the "determinate-feeling" progress bar while a plan job is running.
  useEffect(() => {
    if (phase !== "planning") return;
    const id = setInterval(() => {
      const startedAt = planStartedAtRef.current;
      setPlanElapsedMs(startedAt == null ? 0 : Date.now() - startedAt);
    }, 250);
    return () => clearInterval(id);
  }, [phase]);

  // Polls the plan job until it's ready/errored, with a bounded retry on transient
  // network blips and a hard wall-clock ceiling — this can never spin forever.
  useEffect(() => {
    if (phase !== "planning" || !planJobId) return;
    let cancelled = false;
    let failures = 0;
    let timer: ReturnType<typeof setTimeout> | undefined;

    const poll = async () => {
      if (cancelled) return;
      try {
        const job = await planApi.status(planJobId);
        if (cancelled) return;
        failures = 0;
        setPlanJob(job);
        if (job.status === "ready") {
          setPhase("plan");
          return;
        }
        if (job.status === "error") {
          setError(`Planning failed: ${job.error ?? "unknown error"}`);
          setPhase("start");
          return;
        }
        const startedAt = planStartedAtRef.current;
        if (startedAt != null && Date.now() - startedAt > PLAN_MAX_WAIT_MS) {
          setError("Planning is taking far longer than expected. Try again, or use Clarify / Quick dispatch instead.");
          setPhase("start");
          return;
        }
        timer = setTimeout(() => void poll(), PLAN_POLL_MS);
      } catch (reason) {
        if (cancelled) return;
        failures += 1;
        if (failures >= PLAN_MAX_POLL_FAILURES) {
          setError(`Lost contact with the planner: ${reason instanceof Error ? reason.message : "network error"}`);
          setPhase("start");
          return;
        }
        timer = setTimeout(() => void poll(), PLAN_POLL_MS);
      }
    };

    // Check immediately — Fable may already be done (a heuristic fallback plan
    // resolves fast); only back off to the poll interval for subsequent checks.
    void poll();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [phase, planJobId]);

  const trimmed = text.trim();
  const overLength = trimmed.length > MAX_TASK_TITLE_LENGTH;

  const projectOptions = useMemo(() => flattenProjectTree(projectTree), [projectTree]);
  const planProjectOptions = useMemo(
    () => [
      { value: AUTO_PROJECT, label: "Auto — Fable assigns to a project/sub-project" },
      ...projectOptions,
      { value: NEW_PROJECT, label: "+ Create new project" },
    ],
    [projectOptions],
  );
  const simpleProjectOptions = useMemo(
    () => [
      { value: NO_PROJECT, label: "No project" },
      ...projectOptions,
      { value: NEW_PROJECT, label: "+ Create new project" },
    ],
    [projectOptions],
  );

  const planData = planJob?.plan ?? null;
  const routeData = planJob?.route ?? null;
  const routeTargetLabel = (() => {
    if (!routeData || routeData.decision !== "existing") return "";
    if (planJob?.route_target_name) return planJob.route_target_name;
    const found = routeData.parent_project_id ? findNode(projectTree, routeData.parent_project_id) : null;
    return found?.project.name ?? "an existing project";
  })();

  const planStageText = PLANNING_STAGES[Math.min(PLANNING_STAGES.length - 1, Math.floor(planElapsedMs / 4000))];
  const planProgressPct = Math.min(PLAN_PROGRESS_CAP, Math.round((planElapsedMs / PLAN_PROGRESS_EASE_MS) * PLAN_PROGRESS_CAP));

  /** Resolves a project selector's choice to a real project id, creating a new
   * project first if the operator picked "+ Create new project". Returns
   * undefined for "No project" so callers omit project_id entirely. */
  const resolveSimpleProjectId = async (
    selection: string,
    newName: string,
  ): Promise<string | undefined> => {
    if (selection === NEW_PROJECT) {
      const name = newName.trim();
      if (!name) return undefined;
      const created = await createProject({ name });
      return created.id;
    }
    if (selection === NO_PROJECT || !selection) return undefined;
    return selection;
  };

  // --- Fable plan flow -----------------------------------------------------

  const onStartPlan = async () => {
    if (!trimmed || overLength || inFlightRef.current) return;
    inFlightRef.current = true;
    setSubmitting(true);
    setError(null);
    try {
      const { job_id } = await planApi.start(trimmed);
      setPlanJobId(job_id);
      setPlanJob(null);
      planStartedAtRef.current = Date.now();
      setPlanElapsedMs(0);
      setPhase("planning");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not start planning");
    } finally {
      inFlightRef.current = false;
      setSubmitting(false);
    }
  };

  const onConfirmPlan = async () => {
    if (!planData || !planJobId || inFlightRef.current) return;
    inFlightRef.current = true;
    setSubmitting(true);
    setError(null);
    try {
      const projectOverride =
        planProjectSelection === AUTO_PROJECT
          ? AUTO_PROJECT
          : planProjectSelection === NEW_PROJECT
            ? `new:${planNewProjectName.trim() || planData.project_name}`
            : planProjectSelection;
      const result = await planApi.confirm(planJobId, { projectOverride, speed });
      const taskCount = result.task_count;
      push({
        title: "Project created",
        message: `${planData.project_name} is live with ${taskCount} task${taskCount === 1 ? "" : "s"}.`,
        tone: "success",
      });
      const first = result.dispatched[0];
      onDispatched(first ? { taskId: first.task_id, runId: first.run_id } : { taskId: result.root_project_id, runId: null });
      onClose();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not create the project");
    } finally {
      inFlightRef.current = false;
      setSubmitting(false);
    }
  };

  // --- clarify path -------------------------------------------------------

  const runClarify = async (nextHistory: ClarifyTurn[]) => {
    if (inFlightRef.current) return;
    inFlightRef.current = true;
    setSubmitting(true);
    setError(null);
    try {
      const result = await intakeApi.clarify(trimmed, nextHistory);
      setHistory(nextHistory);
      setRound(result.round);
      if (result.mode === "questions" && result.questions.length > 0) {
        setQuestions(result.questions);
        setAnswers(result.questions.map(() => ""));
        setPhase("clarifying");
      } else if (result.spec) {
        setSpec(result.spec);
        setPhase("confirm");
        if (result.forced) push({ title: "Spec ready", message: "The agent proposed a spec after the question budget was reached.", tone: "info" });
      } else {
        setError("The clarifying agent returned nothing usable. Try quick dispatch.");
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Clarify failed");
    } finally {
      inFlightRef.current = false;
      setSubmitting(false);
    }
  };

  const onStartClarify = () => {
    if (!trimmed || overLength) return;
    void runClarify([]);
  };

  const onSendAnswers = () => {
    const answered = questions.map((q, i) => ({ q, a: answers[i]?.trim() ?? "" }));
    void runClarify([...history, ...answered]);
  };

  const onCreateFromSpec = async () => {
    if (!spec || inFlightRef.current) return;
    inFlightRef.current = true;
    setSubmitting(true);
    setError(null);
    try {
      const projectId = await resolveSimpleProjectId(clarifyProjectSelection, clarifyNewProjectName);
      // harness left to the backend default lane; execute stays read-only — the
      // dial exposes only speed now.
      const result = await intakeApi.dispatch(spec, undefined, projectId, "readonly", speed);
      push({
        title: "Task dispatched",
        message:
          result.execute === "tools"
            ? `${result.board_task.id} is on the board — it will pause for your approval before running tools.`
            : `${result.board_task.id} is on the board and queued.`,
        tone: "success",
      });
      onDispatched({ taskId: result.task_id, runId: result.run_id });
      onClose();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Dispatch failed");
    } finally {
      inFlightRef.current = false;
      setSubmitting(false);
    }
  };

  // --- quick path (immediate, unscoped single task) -----------------------

  const onQuickDispatch = async () => {
    if (!trimmed || overLength || inFlightRef.current) return;
    inFlightRef.current = true;
    setSubmitting(true);
    setError(null);
    try {
      const task = await voiceDispatchApi.createTask({ title: trimmed });
      push({
        title: "Task dispatched",
        message: `${task.id} created — pick a lane on the board to run it.`,
        tone: "success",
      });
      onDispatched({ taskId: task.id, runId: null });
      onClose();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Dispatch failed");
    } finally {
      inFlightRef.current = false;
      setSubmitting(false);
    }
  };

  const updateSpec = (patch: Partial<RefinedSpec>) => setSpec((current) => (current ? { ...current, ...patch } : current));

  const footer = () => {
    if (phase === "planning") {
      // Always enabled — planning can take minutes; the operator must never be
      // stuck watching a progress bar with no way out.
      return <Button variant="secondary" onClick={() => setPhase("start")}>Back</Button>;
    }
    if (phase === "plan") {
      return (
        <>
          <Button variant="secondary" onClick={() => setPhase("start")} disabled={submitting}>Back</Button>
          <Button onClick={() => void onConfirmPlan()} disabled={submitting || !planData}>{submitting ? "Creating…" : "Confirm & create"}</Button>
        </>
      );
    }
    if (phase === "clarifying") {
      return (
        <>
          <Button variant="secondary" onClick={() => setPhase("start")} disabled={submitting}>Back</Button>
          <Button onClick={() => void onSendAnswers()} disabled={submitting}>{submitting ? "Thinking…" : "Send answers"}</Button>
        </>
      );
    }
    if (phase === "confirm") {
      return (
        <>
          <Button variant="secondary" onClick={() => setPhase("start")} disabled={submitting}>Back</Button>
          <Button onClick={() => void onCreateFromSpec()} disabled={submitting || !spec?.title.trim()}>{submitting ? "Creating…" : "Create task"}</Button>
        </>
      );
    }
    return (
      <>
        <Button variant="secondary" onClick={onClose} disabled={submitting}>Cancel</Button>
        <Button variant="secondary" onClick={() => void onQuickDispatch()} disabled={submitting || !trimmed || overLength}>Quick dispatch</Button>
        <Button variant="secondary" onClick={onStartClarify} disabled={submitting || !trimmed || overLength}>{submitting ? "Clarifying…" : "Clarify with agent"}</Button>
        <Button onClick={() => void onStartPlan()} disabled={submitting || !trimmed || overLength}>{submitting ? "Starting…" : "Plan with Fable"}</Button>
      </>
    );
  };

  return (
    <Dialog open={open} onClose={onClose} title="Dispatch as task" footer={footer()}>
      <div className={styles.form}>
        {phase === "start" ? (
          <>
            <div>
              <span className={styles.previewLabel}>Task text</span>
              <p className={styles.preview}>
                {trimmed || <span className={styles.previewEmpty}>Dictate or type a message first.</span>}
              </p>
              <p className={overLength ? styles.charCountError : styles.charCount}>{trimmed.length} / {MAX_TASK_TITLE_LENGTH}</p>
            </div>
            <p className={styles.hint}>
              “Plan with Fable” breaks this into a project — sub-projects and tasks — and shows you where it will land before anything is created.
              “Clarify with agent” refines it into a precise spec first. “Quick dispatch” creates a single task immediately, unscoped to a project.
            </p>
            <SpeedSlider speed={speed} onSpeedChange={setSpeed} idPrefix="dispatch" />
            {overLength ? <p className={styles.error} role="alert">Task text is {trimmed.length - MAX_TASK_TITLE_LENGTH} characters over the {MAX_TASK_TITLE_LENGTH} limit — trim it before dispatching.</p> : null}
          </>
        ) : null}

        {phase === "planning" ? (
          <div className={styles.planningCard}>
            <p className={styles.planningStage} aria-live="polite">{planStageText}</p>
            <div
              className={styles.progressTrack}
              role="progressbar"
              aria-valuemin={0}
              aria-valuemax={100}
              aria-valuenow={planProgressPct}
              aria-label="Planning progress"
            >
              <div className={styles.progressFill} style={{ "--pct": `${planProgressPct}%` } as React.CSSProperties} />
            </div>
            <p className={styles.planningHint}>Fable is planning your project — this can take a few minutes for a complex goal. You can go back at any time; planning keeps running.</p>
          </div>
        ) : null}

        {phase === "plan" && planData && routeData ? (
          <>
            <div className={styles.planSummary}>
              <span className={styles.previewLabel}>Fable&rsquo;s plan</span>
              <h3 className={styles.planTitle}>{planData.project_name}</h3>
              {planData.description ? <p className={styles.planDescription}>{planData.description}</p> : null}
              <div className={styles.planMeta}>
                <Badge tone="neutral">{planData.complexity}</Badge>
                <Badge tone="neutral">effort: {planData.effort}</Badge>
                <Badge tone="neutral">{planTaskTotal(planData)} task{planTaskTotal(planData) === 1 ? "" : "s"}</Badge>
                {planData.sub_projects.length ? <Badge tone="neutral">{planData.sub_projects.length} sub-project{planData.sub_projects.length === 1 ? "" : "s"}</Badge> : null}
              </div>
            </div>

            <p className={styles.routeBanner} role="status">
              {routeData.decision === "new"
                ? <>Fable will create a new project &ldquo;{routeData.project_name || planData.project_name}&rdquo;.</>
                : <>Fable will nest this under existing project &ldquo;{routeTargetLabel}&rdquo;.</>}
              {routeData.reason ? <span className={styles.routeReason}> — {routeData.reason}</span> : null}
            </p>

            <div className={styles.planTasksScroll}>
              {planData.sub_projects.length ? (
                planData.sub_projects.map((sp, i) => (
                  <div key={i} className={styles.planGroup}>
                    <p className={styles.planGroupTitle}>{sp.name}</p>
                    {sp.description ? <p className={styles.planGroupDesc}>{sp.description}</p> : null}
                    {sp.tasks.length ? (
                      <ul className={styles.taskList}>{sp.tasks.map((t, j) => <li key={j}>{t.title}</li>)}</ul>
                    ) : null}
                  </div>
                ))
              ) : (
                <ul className={styles.taskList}>{planData.tasks.map((t, i) => <li key={i}>{t.title}</li>)}</ul>
              )}
            </div>

            <Select label="Project" value={planProjectSelection} onChange={setPlanProjectSelection} disabled={projectTreeLoading}
              options={planProjectOptions} />
            {planProjectSelection === NEW_PROJECT ? (
              <Input label="New project name" value={planNewProjectName} onChange={(e) => setPlanNewProjectName(e.target.value)} placeholder={planData.project_name} />
            ) : null}
          </>
        ) : null}

        {phase === "clarifying" ? (
          <>
            <span className={styles.roundBadge}>Clarifying round {round + 1} — answer what you can; the agent bounds itself to a couple of rounds.</span>
            {questions.map((q, i) => (
              <div key={i} className={styles.question}>
                <p className={styles.questionText}>{q}</p>
                <textarea className={styles.textarea} value={answers[i] ?? ""} placeholder="Your answer (optional)"
                  onChange={(e) => setAnswers((cur) => cur.map((a, idx) => (idx === i ? e.target.value : a)))} />
              </div>
            ))}
          </>
        ) : null}

        {phase === "confirm" && spec ? (
          <>
            <div>
              <span className={styles.specTag}>Refined spec — edit before creating</span>
            </div>
            <Input label="Title" value={spec.title} onChange={(e) => updateSpec({ title: e.target.value })} />
            <div>
              <span className={styles.previewLabel}>Description</span>
              <textarea className={styles.textarea} value={spec.description} onChange={(e) => updateSpec({ description: e.target.value })} />
            </div>
            {spec.acceptance_criteria.length ? (
              <div>
                <span className={styles.previewLabel}>Acceptance criteria</span>
                <ul className={styles.criteria}>{spec.acceptance_criteria.map((c, i) => <li key={i}>{c}</li>)}</ul>
              </div>
            ) : null}
            <Select label="Project (optional)" value={clarifyProjectSelection} onChange={setClarifyProjectSelection} disabled={projectTreeLoading}
              options={simpleProjectOptions} />
            {clarifyProjectSelection === NEW_PROJECT ? (
              <Input label="New project name" value={clarifyNewProjectName} onChange={(e) => setClarifyNewProjectName(e.target.value)} />
            ) : null}
            {spec.suggested_discipline ? <Badge tone="neutral">Suggested discipline: {spec.suggested_discipline}</Badge> : null}
          </>
        ) : null}

        {error ? <p className={styles.error} role="alert">{error}</p> : null}
      </div>
    </Dialog>
  );
}
