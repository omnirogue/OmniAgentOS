"use client";

/**
 * TaskFilesPanel (§2.6 Files tab) — the BoardFilesDrawer content inlined as a
 * tab: file list with download links + upload. Same collab client functions,
 * same empty/error states; no dialog wrapper.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { Badge, Button, EmptyState, ErrorState, Loading } from "@/design";
import {
  boardFileDownloadUrl,
  fetchBoardFiles,
  uploadBoardFiles,
  CollabApiError,
} from "@/features/collab/client";
import type { BoardFile } from "@/features/collab/types";
import styles from "./board.module.css";

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

function fileKind(file: BoardFile): { label: string; tone: "neutral" | "ok" | "warn" } {
  if (file.kind === "upload") return { label: "Upload", tone: "warn" };
  if (file.kind === "output") return { label: "Output", tone: "ok" };
  return { label: file.kind.replace(/_/g, " "), tone: "neutral" };
}

export function TaskFilesPanel({ taskId }: { taskId: string }) {
  const [files, setFiles] = useState<BoardFile[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [hasWorkspace, setHasWorkspace] = useState(true);
  const [uploadFiles, setUploadFiles] = useState<File[]>([]);
  const [uploading, setUploading] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const generation = useRef(0);

  const refresh = useCallback(async () => {
    const gen = ++generation.current;
    setLoading(true);
    try {
      const result = await fetchBoardFiles(taskId);
      if (gen !== generation.current) return;
      setFiles(Array.isArray(result.files) ? result.files : []);
      setHasWorkspace(Boolean(result.workspace));
      setError(null);
    } catch (reason) {
      if (gen !== generation.current) return;
      if (reason instanceof CollabApiError && reason.status === 404) {
        setFiles([]);
        setHasWorkspace(false);
        setError(null);
      } else {
        setError(reason instanceof Error ? reason.message : "Could not load files.");
      }
    } finally {
      if (gen === generation.current) setLoading(false);
    }
  }, [taskId]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const handleUpload = async () => {
    if (!uploadFiles.length || uploading) return;
    setUploading(true);
    setUploadError(null);
    try {
      const result = await uploadBoardFiles(taskId, uploadFiles);
      setUploadFiles([]);
      if (fileInput.current) fileInput.current.value = "";
      setNotice(`${result.saved.length} file${result.saved.length === 1 ? "" : "s"} uploaded.`);
      await refresh();
    } catch (reason) {
      setUploadError(reason instanceof Error ? reason.message : "Could not upload files.");
    } finally {
      setUploading(false);
    }
  };

  if (loading) {
    return <Loading variant="skeleton" label="Loading files" lines={3} />;
  }
  if (error) {
    return <ErrorState title="Files unavailable" message={error} onRetry={() => void refresh()} />;
  }

  return (
    <div className={styles.filesPanel}>
      {!hasWorkspace || files.length === 0 ? (
        <EmptyState
          title="No files yet"
          message="Files uploaded for this task and deliverables created by its run will appear here."
        />
      ) : (
        <div className={styles.fileList} aria-label="Task files">
          {files.map((file) => {
            const kind = fileKind(file);
            return (
              <div key={`${file.rel}:${file.mtime}`} className={styles.fileRow}>
                <a
                  href={boardFileDownloadUrl(taskId, file.rel)}
                  download
                  className={styles.fileName}
                  title={file.rel}
                >
                  {file.rel.split("/").pop() ?? file.rel}
                </a>
                <span className={styles.muted}>{fileSize(file.size)}</span>
                <Badge tone={kind.tone}>{kind.label}</Badge>
              </div>
            );
          })}
        </div>
      )}

      <div className={styles.uploadRow}>
        <input
          ref={fileInput}
          type="file"
          multiple
          onChange={(e) => setUploadFiles(e.target.files ? Array.from(e.target.files) : [])}
          hidden
        />
        <Button variant="secondary" size="sm" onClick={() => fileInput.current?.click()} disabled={uploading}>
          Choose files
        </Button>
        {uploadFiles.length ? (
          <span className={styles.muted}>{uploadFiles.map((f) => f.name).join(", ")}</span>
        ) : null}
        <Button
          variant="primary"
          size="sm"
          onClick={() => void handleUpload()}
          disabled={!uploadFiles.length || uploading}
        >
          {uploading ? "Uploading…" : "Upload"}
        </Button>
      </div>
      {notice ? <p className={styles.successNotice} role="status">{notice}</p> : null}
      {uploadError ? <p className={styles.uploadError} role="alert">{uploadError}</p> : null}
    </div>
  );
}
