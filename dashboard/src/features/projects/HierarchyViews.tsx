"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import {
  Badge,
  Button,
  Card,
  Dialog,
  EmptyState,
  ErrorState,
  Icon,
  Loading,
  Section,
  Select,
  StatusDot,
  Table,
  type BadgeTone,
  type StatusDotState,
  type TableColumn,
} from "@/design";
import {
  fetchProjectActivity,
  fetchProjectFiles,
  fetchProjectFileText,
  projectFileUrl,
  ProjectApiError,
  type ProjectFile,
  uploadProjectFiles,
} from "./api";
import { projectColorStyle } from "./colors";
import {
  nodeProgress,
  stateTone,
  type ConversationScope,
  type ProjectTreeNode,
} from "./hierarchy";
import { useConversation, type DataSource } from "./hierarchyHooks";
import { useModelOptions } from "@/features/models/useModels";
import type {
  ProjectActivity,
  ProjectActivityLogEntry,
  ProjectActivityRun,
  ProjectActivityTask,
} from "./types";
import { startVisibilityPoll } from "../../lib/pollWhenVisible";
import styles from "./hierarchy.module.css";
import opsStyles from "../ops/ops.module.css";

/* ------------------------------------------------------------------ utils -- */

export function relTime(iso: string): string {
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return iso;
  const mins = Math.floor((Date.now() - t) / 60_000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}

function humanState(state: string): string {
  return state.replace(/_/g, " ");
}

/* ------------------------------------------------------------- primitives -- */

export function StatePill({ state }: { state: string }) {
  const tone = stateTone(state);
  return (
    <span className={styles.statePill}>
      <StatusDot state={tone as StatusDotState} />
      <Badge tone={tone as BadgeTone} category="runState">
        {humanState(state)}
      </Badge>
    </span>
  );
}

export function Progress({ done, total }: { done: number; total: number }) {
  const pct = total > 0 ? Math.round((done / total) * 100) : 0;
  return (
    <div className={styles.progress} title={`${done} of ${total} tasks complete`}>
      <span className={styles.progressTrack} aria-hidden>
        <span className={styles.progressFill} style={{ width: `${pct}%` }} />
      </span>
      <span className={styles.progressLabel}>
        {total > 0 ? `${done}/${total}` : "—"}
      </span>
    </div>
  );
}

export function DataNotice({ source, error }: { source: DataSource; error: string | null }) {
  if (source === "fixtures") {
    return (
      <div className={styles.notice} role="status">
        <Icon name="alertTriangle" size={14} />
        <span>
          Showing demo data — the backend is unavailable
          {error ? ` (${error})` : ""}.
        </span>
      </div>
    );
  }
  if (source === "error") {
    return (
      <div className={styles.notice} role="status" style={{ color: "var(--danger)" }}>
        <Icon name="alertTriangle" size={14} />
        <span>
          {error || "Failed to load data — the backend may be unavailable."}
        </span>
      </div>
    );
  }
  return null;
}

/* -------------------------------------------------------------- tree view -- */

function kindLabel(depth: number): string {
  return depth === 0 ? "Project" : "Sub-project";
}

/** Dependency ids when the API supplies them; empty when the frozen payload omits them. */
function dependencyIds(entity: {
  depends_on?: string[];
  blocked_by?: string[];
}): string[] {
  const ids = [
    ...(entity.depends_on ?? []),
    ...(entity.blocked_by ?? []),
  ].filter(Boolean);
  return Array.from(new Set(ids));
}

function DependencyHint({
  dependsOn,
  labelLookup,
}: {
  dependsOn: string[];
  /** Optional map of id → display title for nicer labels. */
  labelLookup?: Map<string, string>;
}) {
  // Only render when the payload actually carries dependency data — never invent edges.
  if (!dependsOn.length) return null;
  return (
    <span className={styles.depHint} title="Depends on">
      <span className={styles.depLabel}>Depends on</span>
      {dependsOn.map((id) => (
        <Badge key={id} tone="neutral" category="neutral">
          {labelLookup?.get(id) ?? id}
        </Badge>
      ))}
    </span>
  );
}

export function ProjectTreeView({ nodes }: { nodes: ProjectTreeNode[] }) {
  if (!nodes.length) {
    return (
      <EmptyState
        title="No projects yet"
        message="Projects you create will appear here as an organised tree."
      />
    );
  }
  return (
    <div className={styles.tree}>
      {nodes.map((node) => (
        <TreeNode
          key={node.project.id}
          node={node}
          depth={0}
          rootProjectId={node.project.id}
        />
      ))}
    </div>
  );
}

function TreeNode({
  node,
  depth,
  rootProjectId,
}: {
  node: ProjectTreeNode;
  depth: number;
  /** Top-level project id — drives the stable subtree color. */
  rootProjectId: string;
}) {
  const [open, setOpen] = useState(depth < 1);
  const hasChildren = node.sub_projects.length > 0 || node.tasks.length > 0;
  const { done, total } = nodeProgress(node);
  const isRunning = node.status === "running";
  const colorStyle = depth === 0 ? projectColorStyle(rootProjectId) : undefined;
  const deps = dependencyIds(node);

  // Title map for dependency badges within this node's local tasks.
  const taskTitles = useMemo(() => {
    const map = new Map<string, string>();
    for (const t of node.tasks) map.set(t.id, t.title);
    return map;
  }, [node.tasks]);

  const nodeClass = [
    styles.node,
    depth > 0 ? styles.nodeSub : "",
    isRunning ? styles.nodeRunning : "",
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <div className={nodeClass} style={colorStyle}>
      <div className={styles.row}>
        <span className={styles.colorRail} aria-hidden />
        {hasChildren ? (
          <button
            type="button"
            className={styles.toggle}
            aria-expanded={open}
            aria-label={open ? "Collapse" : "Expand"}
            onClick={() => setOpen((v) => !v)}
          >
            <Icon name={open ? "chevronDown" : "chevronRight"} size={16} />
          </button>
        ) : (
          <span className={styles.toggleSpacer} aria-hidden />
        )}
        <span className={styles.nodeTitle}>
          <span className={styles.titleLine}>
            <Link
              href={`/projects/${encodeURIComponent(node.project.id)}`}
              className={styles.nodeName}
            >
              {node.project.name}
            </Link>
            <Badge tone="neutral" category="neutral">
              {kindLabel(depth)}
            </Badge>
          </span>
          <span className={styles.nodeMeta}>
            {node.sub_projects.length ? `${node.sub_projects.length} sub-projects · ` : ""}
            {node.tasks.length} tasks
            <DependencyHint dependsOn={deps} />
          </span>
        </span>
        <span className={styles.rowRight}>
          <Progress done={done} total={total} />
          <StatePill state={node.status} />
        </span>
      </div>

      {open && hasChildren ? (
        <div className={styles.children} role="group" aria-label="Children">
          {node.sub_projects.map((child) => (
            <TreeNode
              key={child.project.id}
              node={child}
              depth={depth + 1}
              rootProjectId={rootProjectId}
            />
          ))}
          {node.tasks.map((task) => {
            const taskRunning = task.state === "running";
            const taskDeps = dependencyIds(task);
            return (
              <div
                key={task.id}
                className={[
                  styles.taskRow,
                  taskRunning ? styles.taskRunning : "",
                ]
                  .filter(Boolean)
                  .join(" ")}
              >
                <span className={styles.colorDot} aria-hidden />
                <Icon name="checkCircle" size={14} />
                <span className={styles.taskMain}>
                  <span className={styles.titleLine}>
                    <Link
                      href={`/projects/task/${encodeURIComponent(task.id)}`}
                      className={styles.taskLink}
                    >
                      {task.title}
                    </Link>
                    <Badge tone="neutral" category="neutral">
                      Task
                    </Badge>
                    {taskRunning ? (
                      <span className={styles.livePulse} aria-label="Running live" title="Running" />
                    ) : null}
                  </span>
                  <DependencyHint dependsOn={taskDeps} labelLookup={taskTitles} />
                </span>
                <StatePill state={task.state} />
              </div>
            );
          })}
        </div>
      ) : null}
    </div>
  );
}

/* ---------------------------------------------------- children (detail) --- */

export function ChildrenPanel({ node }: { node: ProjectTreeNode }) {
  const empty = !node.sub_projects.length && !node.tasks.length;
  if (empty) {
    return (
      <EmptyState
        title="Nothing scheduled"
        message="This node has no sub-projects or tasks yet."
      />
    );
  }
  return (
    <div className={styles.tree}>
      {node.sub_projects.map((child) => {
        const { done, total } = nodeProgress(child);
        return (
          <div key={child.project.id} className={styles.taskRow}>
            <Icon name="grid" size={14} />
            <Link
              href={`/projects/${encodeURIComponent(child.project.id)}`}
              className={styles.taskLink}
            >
              {child.project.name}
            </Link>
            <Progress done={done} total={total} />
            <StatePill state={child.status} />
          </div>
        );
      })}
      {node.tasks.map((task) => (
        <div key={task.id} className={styles.taskRow}>
          <Icon name="checkCircle" size={14} />
          <Link
            href={`/projects/task/${encodeURIComponent(task.id)}`}
            className={styles.taskLink}
          >
            {task.title}
          </Link>
          <StatePill state={task.state} />
        </div>
      ))}
    </div>
  );
}

/* ------------------------------------------------------------- files ----- */

function fileSize(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return "—";
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB"];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(value >= 10 || unit === 0 ? 0 : 1)} ${units[unit]}`;
}

function fileDate(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

/** Project file browser and operator upload surface for this specific project. */
export function FilesPanel({ projectId }: { projectId: string }) {
  const [files, setFiles] = useState<ProjectFile[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<ProjectFile | null>(null);
  const [preview, setPreview] = useState<string | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [uploadFiles, setUploadFiles] = useState<File[]>([]);
  const [instructions, setInstructions] = useState("");
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [uploadSuccess, setUploadSuccess] = useState<string | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const data = await fetchProjectFiles(projectId);
      setFiles(data.files);
      setError(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not load project files.");
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    if (!selected || selected.type === "image") {
      setPreview(null);
      setPreviewError(null);
      setPreviewLoading(false);
      return;
    }
    let current = true;
    setPreview(null);
    setPreviewError(null);
    setPreviewLoading(true);
    void fetchProjectFileText(projectId, selected.path)
      .then((text) => {
        if (current) setPreview(text);
      })
      .catch((reason: unknown) => {
        if (current) setPreviewError(reason instanceof Error ? reason.message : "Could not preview this file.");
      })
      .finally(() => {
        if (current) setPreviewLoading(false);
      });
    return () => { current = false; };
  }, [projectId, selected]);

  const columns: TableColumn<ProjectFile>[] = [
    {
      key: "name",
      header: "Name",
      sortable: true,
      sortValue: (file) => file.path,
      render: (file) => <Button size="sm" variant="ghost" onClick={() => setSelected(file)}>{file.name}</Button>,
    },
    { key: "type", header: "Type", sortable: true, sortValue: (file) => file.type, render: (file) => <Badge tone="neutral">{file.type}</Badge> },
    { key: "size", header: "Size", align: "right", sortable: true, sortValue: (file) => file.size, render: (file) => fileSize(file.size) },
    { key: "modified", header: "Modified", sortable: true, sortValue: (file) => file.modified, render: (file) => fileDate(file.modified) },
  ];
  const selectedUrl = selected ? projectFileUrl(projectId, selected.path) : "";
  const selectedDownloadUrl = selected ? projectFileUrl(projectId, selected.path, true) : "";

  const chooseFiles = (nextFiles: FileList | null) => {
    setUploadFiles(nextFiles ? Array.from(nextFiles) : []);
    setUploadError(null);
    setUploadSuccess(null);
  };

  const handleUpload = async () => {
    if (!uploadFiles.length || uploading) return;
    setUploading(true);
    setUploadError(null);
    setUploadSuccess(null);
    try {
      const result = await uploadProjectFiles(projectId, uploadFiles, instructions);
      setUploadSuccess(`${result.uploaded.length} file${result.uploaded.length === 1 ? "" : "s"} uploaded.`);
      setUploadFiles([]);
      setInstructions("");
      if (fileInput.current) fileInput.current.value = "";
      await refresh();
    } catch (reason) {
      setUploadError(reason instanceof Error ? reason.message : "Could not upload files.");
    } finally {
      setUploading(false);
    }
  };

  return (
    <div style={{ display: "grid", gap: "var(--space-3)" }}>
      <Card padding="md" style={{ display: "grid", gap: "var(--space-3)" }}>
        <div>
          <strong>Upload files for Fable</strong>
          <p style={{ color: "var(--text-muted)", margin: "var(--space-1) 0 0" }}>
            Files are saved to this project&apos;s uploads folder for agents to reference.
          </p>
        </div>
        <div
          onDragOver={(event) => event.preventDefault()}
          onDrop={(event) => {
            event.preventDefault();
            chooseFiles(event.dataTransfer.files);
          }}
          style={{ border: "1px dashed var(--border)", borderRadius: "var(--radius-md)", padding: "var(--space-4)", textAlign: "center" }}
        >
          <input
            ref={fileInput}
            type="file"
            multiple
            onChange={(event) => chooseFiles(event.target.files)}
            style={{ display: "none" }}
          />
          <p style={{ margin: "0 0 var(--space-2)" }}>Drag files here, or choose files to upload.</p>
          <Button variant="secondary" onClick={() => fileInput.current?.click()} disabled={uploading}>
            Choose files
          </Button>
          {uploadFiles.length ? (
            <p style={{ color: "var(--text-muted)", margin: "var(--space-2) 0 0" }}>
              {uploadFiles.map((file) => file.name).join(", ")}
            </p>
          ) : null}
        </div>
        <label style={{ display: "grid", gap: "var(--space-1)" }}>
          <span>Instructions for Fable <span style={{ color: "var(--text-muted)" }}>(optional)</span></span>
          <textarea
            value={instructions}
            onChange={(event) => setInstructions(event.target.value)}
            placeholder="Explain how agents should use these files…"
            rows={3}
            disabled={uploading}
            style={{ boxSizing: "border-box", minHeight: 80, padding: "var(--space-2)", resize: "vertical", width: "100%" }}
          />
        </label>
        <div style={{ display: "flex", alignItems: "center", gap: "var(--space-2)", flexWrap: "wrap" }}>
          <Button onClick={() => void handleUpload()} disabled={!uploadFiles.length || uploading}>
            {uploading ? "Uploading…" : "Upload files"}
          </Button>
          {uploadSuccess ? <span role="status" style={{ color: "var(--ok)", fontSize: "var(--font-size-sm)" }}>{uploadSuccess}</span> : null}
        </div>
        {uploadError ? <ErrorState title="Upload failed" message={uploadError} /> : null}
      </Card>
      {loading ? <Loading variant="skeleton" label="Loading project files…" lines={4} /> : null}
      {!loading && error ? <ErrorState message={error} onRetry={() => void refresh()} /> : null}
      {!loading && !error && !files.length ? <EmptyState title="No project files yet" message="Files agents create in this project's granted directories will appear here." /> : null}
      {!loading && !error && files.length ? <Table columns={columns} rows={files} rowKey={(file) => `${file.path}:${file.modified}`} caption="Project files" /> : null}

      <Dialog
        open={selected !== null}
        onClose={() => setSelected(null)}
        title={selected?.name ?? "File preview"}
        footer={selected ? <div style={{ display: "flex", justifyContent: "flex-end", gap: "var(--space-2)" }}><Button variant="ghost" onClick={() => setSelected(null)}>Close</Button><a href={selectedDownloadUrl} target="_blank" rel="noreferrer" style={{ color: "var(--accent)", alignSelf: "center" }}>Open / download</a></div> : undefined}
      >
        {selected ? <div style={{ display: "grid", gap: "var(--space-3)" }}>
          <p style={{ color: "var(--text-muted)", margin: 0 }}>{selected.path} · {fileSize(selected.size)} · {fileDate(selected.modified)}</p>
          {selected.type === "image" ? (
            // This is a same-origin, user-selected project file rather than an
            // optimizable app image, so it intentionally bypasses next/image.
            // eslint-disable-next-line @next/next/no-img-element
            <img src={selectedUrl} alt={selected.name} style={{ maxWidth: "100%", maxHeight: "65vh", objectFit: "contain" }} />
          ) : null}
          {previewLoading ? <Loading label="Loading preview…" /> : null}
          {previewError ? <ErrorState title="Preview unavailable" message={previewError} /> : null}
          {preview !== null ? <pre style={{ margin: 0, maxHeight: "65vh", overflow: "auto", whiteSpace: "pre-wrap", fontSize: "var(--font-size-sm)" }}>{preview}</pre> : null}
        </div> : null}
      </Dialog>
    </div>
  );
}

/* ------------------------------------------------------- conversation ----- */

const roleClass: Record<string, string> = {
  user: styles.msgUser,
  system: styles.msgSystem,
};

export function ConversationPanel({
  scope,
  id,
}: {
  scope: ConversationScope;
  id: string;
}) {
  const { messages, loading, error, source, append, sending } = useConversation(scope, id);
  const modelOptions = useModelOptions();
  const [draft, setDraft] = useState("");
  const [model, setModel] = useState("");
  const [sendError, setSendError] = useState<string | null>(null);

  const submit = async () => {
    if (!draft.trim() || sending) return;
    setSendError(null);
    try {
      await append("user", draft, model || null);
      setDraft("");
    } catch (reason) {
      setSendError(reason instanceof Error ? reason.message : "Could not send.");
    }
  };

  return (
    <div>
      <DataNotice source={source} error={error} />
      {loading && !messages.length ? (
        <Loading label="Loading conversation…" />
      ) : messages.length ? (
        <div className={styles.thread}>
          {messages.map((m) => (
            <div key={m.id} className={`${styles.msg} ${roleClass[m.role] ?? ""}`}>
              <div className={styles.msgHead}>
                <span className={styles.msgRole}>{m.role}</span>
                {m.model ? <Badge tone="neutral">{m.model}</Badge> : null}
                <span>·</span>
                <span>{relTime(m.created_at)}</span>
              </div>
              <div className={styles.msgBody}>{m.content}</div>
            </div>
          ))}
        </div>
      ) : (
        <EmptyState
          title="No messages yet"
          message="Start the thread with the composer below."
        />
      )}

      <div className={styles.composer}>
        <textarea
          className={styles.composerText}
          placeholder="Add a message to this thread…"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
              e.preventDefault();
              void submit();
            }
          }}
          aria-label="Message"
        />
        <div className={styles.composerRow}>
          <div className={styles.composerControls}>
            <Select
              aria-label="Model"
              value={model}
              onChange={(value) => setModel(value)}
              options={modelOptions}
            />
          </div>
          <Button
            variant="primary"
            onClick={() => void submit()}
            disabled={sending || !draft.trim()}
          >
            {sending ? "Sending…" : "Send"}
          </Button>
        </div>
        {sendError ? (
          <span style={{ color: "var(--danger)", fontSize: "var(--font-size-sm)" } as CSSProperties}>
            {sendError}
          </span>
        ) : null}
      </div>
    </div>
  );
}

/* ---------------------------------------------------------- activity ------ */
/*
 * The Logs/Activity tab is wired to GET /api/projects/{id}/activity (the
 * project-activity endpoint main already ships — see
 * omniagentos/api/routes/projects.py:get_project_activity), NOT the
 * hierarchy tree's runs-fallback feed. This preserves the richer
 * task -> runs -> steps + merged activity-log display main's standalone
 * project page had, now surfaced inside this tab.
 */

const ACTIVITY_POLL_MS = 10000;

function activityDate(value: string | null | undefined): string {
  if (!value) return "-";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
}

function activityTime(value: string | null | undefined): string {
  if (!value) return "-";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleTimeString();
}

function activitySummaryLine(activity: ProjectActivity): string {
  const { summary } = activity;
  const parts = [`${summary.tasks} task${summary.tasks === 1 ? "" : "s"}`];
  if (summary.running) parts.push(`${summary.running} running`);
  if (summary.awaiting_approval) parts.push(`${summary.awaiting_approval} awaiting approval`);
  if (summary.completed) parts.push(`${summary.completed} completed`);
  if (summary.failed) parts.push(`${summary.failed} failed`);
  return parts.join(" · ");
}

function ActivityRunRow({ run }: { run: ProjectActivityRun }) {
  const stepLabel = run.current_step
    ? `Step ${run.current_step.seq + 1} of ${run.steps_total}: ${run.current_step.name}`
    : run.steps_total
      ? `${run.steps_done} of ${run.steps_total} steps done`
      : "No steps recorded yet";
  return (
    <Link
      href={`/runs/${run.id}`}
      className={opsStyles.runCard}
      style={{
        textDecoration: "none",
        border: "1px solid var(--border)",
        borderRadius: "var(--radius-md)",
        padding: "var(--space-3)",
      }}
    >
      <div className={opsStyles.runCardTop}>
        <span className={opsStyles.runCardMeta}>
          <StatusDot state={run.state} />
          <span className={opsStyles.runCardId}>{run.id}</span>
        </span>
        <Badge tone={run.state}>{humanState(run.state)}</Badge>
      </div>
      <p className={opsStyles.runCardTask}>
        {run.harness}
        {run.model ? ` · ${run.model}` : ""}
        {` · ${stepLabel}`}
      </p>
      {run.error ? (
        <p className={opsStyles.runCardTask} style={{ color: "var(--danger)" }}>
          {run.error}
        </p>
      ) : null}
      <span className={opsStyles.runCardAge}>Queued {activityDate(run.queued_at)}</span>
    </Link>
  );
}

function ActivityTaskCard({ task }: { task: ProjectActivityTask }) {
  const tone = stateTone(task.state) as BadgeTone;
  return (
    <Card>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          gap: "var(--space-2)",
          marginBottom: "var(--space-3)",
        }}
      >
        <h3 className={opsStyles.cardTitle} style={{ margin: 0 }}>
          {task.title}
        </h3>
        <Badge tone={tone}>{humanState(task.state)}</Badge>
      </div>
      {task.runs.length ? (
        <div className={opsStyles.runList}>
          {task.runs.map((run) => (
            <ActivityRunRow key={run.id} run={run} />
          ))}
        </div>
      ) : (
        <p style={{ color: "var(--text-faint)", fontSize: "var(--text-small)", margin: 0 }}>
          No runs yet.
        </p>
      )}
    </Card>
  );
}

function ActivityLogList({ entries }: { entries: ProjectActivityLogEntry[] }) {
  if (!entries.length) {
    return (
      <EmptyState title="No activity yet" message="Nothing has happened in this project yet." />
    );
  }
  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        gap: "var(--space-2)",
        maxHeight: "24rem",
        overflowY: "auto",
      }}
    >
      {entries.map((entry) => (
        <div
          key={`${entry.id}-${entry.task_id ?? ""}-${entry.line}`}
          style={{ display: "flex", gap: "var(--space-3)", fontSize: "var(--text-small)" }}
        >
          <span
            style={{
              color: "var(--text-faint)",
              fontFamily: "var(--font-mono)",
              flexShrink: 0,
              minWidth: "5rem",
            }}
          >
            {activityTime(entry.ts)}
          </span>
          <span style={{ color: "var(--text)" }}>{entry.line}</span>
        </div>
      ))}
    </div>
  );
}

export function ActivityPanel({ projectId }: { projectId: string }) {
  const [activity, setActivity] = useState<ProjectActivity | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    if (!projectId) return;
    setLoading(true);
    try {
      setActivity(await fetchProjectActivity(projectId));
      setError(null);
    } catch (reason) {
      setError(
        reason instanceof ProjectApiError || reason instanceof Error
          ? reason.message
          : "Unable to load this project's activity.",
      );
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  // Always resolves (the try/finally always clears `loading`), and once
  // `activity` has loaded once, later polls only ever update in place —
  // never reverting the tab to a full spinner.
  useEffect(() => {
    void refresh();
    return startVisibilityPoll(() => void refresh(), ACTIVITY_POLL_MS);
  }, [refresh]);

  if (loading && !activity) {
    return <Loading label="Loading project activity…" />;
  }
  if (error && !activity) {
    return (
      <ErrorState
        message={`Could not load this project's activity: ${error}`}
        onRetry={() => void refresh()}
      />
    );
  }
  if (!activity) {
    return (
      <EmptyState title="No activity yet" message="Nothing has run in this project yet." />
    );
  }

  const hasAnyActivity = activity.tasks.length > 0 || activity.activity_log.length > 0;

  return (
    <div>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          gap: "var(--space-3)",
          marginBottom: "var(--space-3)",
        }}
      >
        <span style={{ color: "var(--text-faint)", fontSize: "var(--text-small)" }}>
          {activitySummaryLine(activity)}
        </span>
        <Button variant="ghost" size="sm" onClick={() => void refresh()} disabled={loading}>
          {loading ? "Refreshing…" : "Refresh"}
        </Button>
      </div>

      {error ? (
        <ErrorState message={`Latest update failed: ${error}`} onRetry={() => void refresh()} />
      ) : null}

      {!hasAnyActivity ? (
        <EmptyState
          title="No activity yet"
          message="Nothing has run in this project yet. Dispatch a task to see its progress here."
        />
      ) : (
        <>
          <Section title="Tasks">
            {activity.tasks.length ? (
              <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-3)" }}>
                {activity.tasks.map((task) => (
                  <ActivityTaskCard key={task.id} task={task} />
                ))}
              </div>
            ) : (
              <EmptyState title="No tasks yet" message="This project has no tasks yet." />
            )}
          </Section>

          <Section title="Recent activity" description="Newest first, live-updating.">
            <Card>
              <ActivityLogList entries={activity.activity_log} />
            </Card>
          </Section>
        </>
      )}
    </div>
  );
}
