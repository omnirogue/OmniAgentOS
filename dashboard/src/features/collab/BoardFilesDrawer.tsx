"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Badge, Button, Dialog, EmptyState, ErrorState, Loading } from "@/design";
import {
  boardArchiveUrl,
  boardFileDownloadUrl,
  boardFilesShareUrl,
  fetchBoardFiles,
  revealBoardWorkspace,
  uploadBoardFiles,
  CollabApiError,
  type RevealApp,
} from "./client";
import type { BoardFile } from "./types";
import styles from "./collab.module.css";

const REVEAL_APP_LABELS: Record<RevealApp, string> = {
  finder: "Finder",
  vscode: "VS Code",
  cursor: "Cursor",
  terminal: "Terminal",
};

function fileSize(size: number): string {
  if (!Number.isFinite(size) || size < 1024) return `${Math.max(0, size || 0)} B`;
  const units = ["KB", "MB", "GB"];
  let value = size / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(value >= 10 ? 0 : 1)} ${units[unit]}`;
}

function fileDate(value: number): string {
  const date = new Date(value * 1000);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
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

function fileKind(file: BoardFile): { label: string; tone: "neutral" | "ok" | "warn" } {
  if (file.kind === "upload") return { label: "Upload", tone: "warn" };
  if (file.kind === "output") return { label: "Output", tone: "ok" };
  return { label: file.kind.replace(/_/g, " "), tone: "neutral" };
}

export function BoardFilesDrawer({
  taskId,
  taskTitle,
  open,
  onClose,
}: {
  taskId: string | null;
  taskTitle: string;
  open: boolean;
  onClose: () => void;
}) {
  const [files, setFiles] = useState<BoardFile[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [hasWorkspace, setHasWorkspace] = useState(true);
  const [uploadFiles, setUploadFiles] = useState<File[]>([]);
  const [instructions, setInstructions] = useState("");
  const [uploading, setUploading] = useState(false);
  const [uploadProgress, setUploadProgress] = useState(0);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [revealing, setRevealing] = useState<RevealApp | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const requestGeneration = useRef(0);

  const refresh = useCallback(async (): Promise<number> => {
    if (!taskId) return requestGeneration.current;
    const generation = ++requestGeneration.current;
    setLoading(true);
    try {
      const result = await fetchBoardFiles(taskId);
      if (generation !== requestGeneration.current) return generation;
      setFiles(Array.isArray(result.files) ? result.files : []);
      setHasWorkspace(Boolean(result.workspace));
      setError(null);
    } catch (reason) {
      if (generation !== requestGeneration.current) return generation;
      if (reason instanceof CollabApiError && reason.status === 404) {
        setFiles([]);
        setHasWorkspace(false);
        setError(null);
      } else {
        setError(reason instanceof Error ? reason.message : "Could not load files.");
      }
    } finally {
      if (generation === requestGeneration.current) setLoading(false);
    }
    return generation;
  }, [taskId]);

  useEffect(() => {
    requestGeneration.current += 1;
    setFiles([]);
    setLoading(false);
    setError(null);
    setHasWorkspace(true);
    setUploadFiles([]);
    setInstructions("");
    setUploading(false);
    setUploadProgress(0);
    setUploadError(null);
    setNotice(null);
    setRevealing(null);
    if (fileInput.current) fileInput.current.value = "";
  }, [taskId]);

  useEffect(() => {
    if (!open) return;
    setNotice(null);
    setUploadError(null);
    void refresh();
  }, [open, refresh]);

  const chooseFiles = (nextFiles: FileList | null) => {
    setUploadFiles(nextFiles ? Array.from(nextFiles) : []);
    setUploadError(null);
    setNotice(null);
  };

  const handleUpload = async () => {
    if (!taskId || !uploadFiles.length || uploading) return;
    const gen = requestGeneration.current;
    let completionGeneration = gen;
    setUploading(true);
    setUploadProgress(0);
    setUploadError(null);
    setNotice(null);
    try {
      const result = await uploadBoardFiles(taskId, uploadFiles, instructions, (progress) => {
        if (gen === requestGeneration.current) setUploadProgress(progress);
      });
      if (gen !== requestGeneration.current) return;
      setUploadFiles([]);
      setInstructions("");
      if (fileInput.current) fileInput.current.value = "";
      setNotice(`${result.saved.length} file${result.saved.length === 1 ? "" : "s"} uploaded.`);
      completionGeneration = await refresh();
    } catch (reason) {
      if (gen !== requestGeneration.current) return;
      setUploadError(reason instanceof Error ? reason.message : "Could not upload files.");
    } finally {
      if (completionGeneration === requestGeneration.current) setUploading(false);
    }
  };

  const shareUrl = taskId ? boardFilesShareUrl(taskId) : "";
  const handleCopy = async (value: string, message: string) => {
    try {
      await copyText(value);
      setNotice(message);
    } catch {
      setNotice("Could not copy the link.");
    }
  };

  const handleReveal = async (app: RevealApp) => {
    if (!taskId || !hasWorkspace || revealing) return;
    setRevealing(app);
    setNotice(null);
    setUploadError(null);
    try {
      await revealBoardWorkspace(taskId, { app });
      setNotice(app === "finder" ? "Revealed in Finder." : `Opened in ${REVEAL_APP_LABELS[app]}.`);
    } catch (reason) {
      if (reason instanceof CollabApiError && reason.status === 501) {
        setNotice("Opening locally is only available on the host machine.");
      } else if (reason instanceof CollabApiError && reason.status === 403) {
        setNotice("Opening locally is not available for this workspace.");
      } else {
        setNotice(reason instanceof Error ? reason.message : "Could not open locally.");
      }
    } finally {
      setRevealing(null);
    }
  };

  return (
    <Dialog open={open && Boolean(taskId)} onClose={onClose} title={`Files · ${taskTitle}`} className={styles.filesDialog}>
      <div className={styles.filesPanel}>
        <div className={styles.filesActions}>
          <Button variant="secondary" size="sm" onClick={() => void handleCopy(shareUrl, "Drawer link copied.")}>Copy link</Button>
          {taskId ? <Button variant="secondary" size="sm" disabled={!hasWorkspace || files.length === 0} onClick={() => { window.location.assign(boardArchiveUrl(taskId)); }}>Download all (.zip)</Button> : null}
          <Button variant="secondary" size="sm" disabled={!hasWorkspace || Boolean(revealing)} onClick={() => void handleReveal("finder")}>
            {revealing === "finder" ? "Revealing…" : "Reveal in Finder"}
          </Button>
          <span className={styles.muted}>Open in</span>
          <Button variant="ghost" size="sm" disabled={!hasWorkspace || Boolean(revealing)} onClick={() => void handleReveal("vscode")}>{revealing === "vscode" ? "Opening…" : "VS Code"}</Button>
          <Button variant="ghost" size="sm" disabled={!hasWorkspace || Boolean(revealing)} onClick={() => void handleReveal("cursor")}>{revealing === "cursor" ? "Opening…" : "Cursor"}</Button>
          <Button variant="ghost" size="sm" disabled={!hasWorkspace || Boolean(revealing)} onClick={() => void handleReveal("terminal")}>{revealing === "terminal" ? "Opening…" : "Terminal"}</Button>
        </div>
        {notice ? <span role="status" className={styles.successNotice}>{notice}</span> : null}

        {loading ? <Loading variant="skeleton" label="Loading files" lines={3} /> : null}
        {!loading && error ? <ErrorState title="Files unavailable" message={error} onRetry={() => void refresh()} /> : null}
        {!loading && !error && (!hasWorkspace || files.length === 0) ? (
          <EmptyState title="No files yet" message="Files uploaded for this task and deliverables created by its run will appear here." />
        ) : null}
        {!loading && !error && files.length > 0 ? (
          <div className={styles.fileList} aria-label="Task files">
            {files.map((file) => {
              const kind = fileKind(file);
              const downloadUrl = taskId ? boardFileDownloadUrl(taskId, file.rel) : "";
              return (
                <div key={`${file.rel}:${file.mtime}`} className={styles.fileRow}>
                  <div className={styles.fileIdentity}>
                    <a href={downloadUrl} download className={styles.fileName} title={file.rel}>{file.rel.split("/").pop() ?? file.rel}</a>
                    <span className={styles.fileMeta}>{fileSize(file.size)} · {fileDate(file.mtime)}</span>
                  </div>
                  <div className={styles.fileRowActions}>
                    <Badge tone={kind.tone}>{kind.label}</Badge>
                    <Button variant="ghost" size="sm" onClick={() => void handleCopy(typeof window === "undefined" ? downloadUrl : `${window.location.origin}${downloadUrl}`, "File link copied.")}>Copy link</Button>
                  </div>
                </div>
              );
            })}
          </div>
        ) : null}

        <div className={styles.uploadPanel}>
          <strong>Upload files</strong>
          <p className={styles.muted}>Add context or deliverables to this task&apos;s workspace.</p>
          <div
            className={styles.uploadDropzone}
            onDragOver={(event) => event.preventDefault()}
            onDrop={(event) => { event.preventDefault(); chooseFiles(event.dataTransfer.files); }}
            onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") fileInput.current?.click(); }}
            role="button"
            tabIndex={0}
          >
            <input ref={fileInput} type="file" multiple onChange={(event) => chooseFiles(event.target.files)} hidden />
            <span>Drop files here, or</span>
            <Button variant="secondary" size="sm" onClick={() => fileInput.current?.click()} disabled={uploading}>Choose files</Button>
            {uploadFiles.length ? <span className={styles.muted}>{uploadFiles.map((file) => file.name).join(", ")}</span> : null}
          </div>
          <label className={styles.instructionsField}>
            <span>Instructions <span className={styles.muted}>(optional)</span></span>
            <textarea value={instructions} onChange={(event) => setInstructions(event.target.value)} rows={3} disabled={uploading} placeholder="Explain how agents should use these files…" />
          </label>
          <div className={styles.uploadActions}>
            <Button onClick={() => void handleUpload()} disabled={!uploadFiles.length || uploading}>
              {uploading ? `Uploading${uploadProgress ? ` ${uploadProgress}%` : "…"}` : "Upload files"}
            </Button>
          </div>
          {uploadError ? <p className={styles.uploadError} role="alert">{uploadError}</p> : null}
        </div>
      </div>
    </Dialog>
  );
}
