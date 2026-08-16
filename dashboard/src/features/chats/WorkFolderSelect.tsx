"use client";

/**
 * ctx-row Work-folder select (prototype `.ctx-row`, sitting directly above the
 * composer).
 *
 * Replaces ChatScopePicker: the Company axis was ratified OUT of the chat
 * surface, so the row carries exactly one control — the filesystem work folder
 * a chat's files belong to. Chat folders (the sidebar registry) are a separate
 * concept and are NOT selectable here.
 *
 * The tree comes from GET /api/workfs/tree through the same-origin proxy. That
 * route is session-token-gated upstream, so it 401'd for the browser until
 * "workfs" joined the proxy's authorized-read allowlist — a failure here is
 * therefore surfaced with a Retry rather than silently leaving an empty menu.
 */

import { useCallback, useEffect, useState } from "react";
import { Select } from "@/design";
import { fetchWorkFolderTree, type WorkFolderTreeNode } from "./chatApi";
import styles from "./chatShell.module.css";

export interface WorkFolderSelectProps {
  value: string | null;
  onChange: (path: string | null) => void;
  disabled?: boolean;
}

const NO_FOLDER = "";
/** NBSP: a native option label collapses ordinary leading spaces. */
const INDENT = "\u00a0\u00a0";

/** Depth-first flatten; the label is indented so nesting survives a flat menu. */
export function flattenWorkFolders(
  nodes: WorkFolderTreeNode[],
  depth = 0,
): Array<{ value: string; label: string }> {
  const out: Array<{ value: string; label: string }> = [];
  for (const node of nodes) {
    out.push({ value: node.path, label: `${INDENT.repeat(depth)}${node.name}` });
    if (node.children?.length) out.push(...flattenWorkFolders(node.children, depth + 1));
  }
  return out;
}

/**
 * One tree read per page load, shared by every mounted surface (/chats and the
 * board's task dock can both be alive at once). A failure is NOT cached, so
 * Retry — and the next mount — really re-request.
 */
let treeRequest: Promise<Array<{ value: string; label: string }>> | null = null;

function loadWorkFolderOptions(force = false) {
  if (force || !treeRequest) {
    treeRequest = fetchWorkFolderTree()
      .then((tree) => flattenWorkFolders(Array.isArray(tree.entries) ? tree.entries : []))
      .catch((reason) => {
        treeRequest = null;
        throw reason;
      });
  }
  return treeRequest;
}

/** Tests own the module cache; nothing in the app calls this. */
export function resetWorkFolderCache() {
  treeRequest = null;
}

export function WorkFolderSelect({ value, onChange, disabled }: WorkFolderSelectProps) {
  const [options, setOptions] = useState<Array<{ value: string; label: string }>>([]);
  const [loading, setLoading] = useState(false);
  const [failed, setFailed] = useState(false);

  const load = useCallback((force = false) => {
    setLoading(true);
    setFailed(false);
    let live = true;
    loadWorkFolderOptions(force)
      .then((next) => {
        if (live) setOptions(next);
      })
      .catch(() => {
        if (live) setFailed(true);
      })
      .finally(() => {
        if (live) setLoading(false);
      });
    return () => {
      live = false;
    };
  }, []);

  useEffect(() => load(), [load]);

  // A folder persisted on the chat must stay selected even if the tree read
  // failed — otherwise the row would silently claim "No work folder".
  const withCurrent =
    value && !options.some((option) => option.value === value)
      ? [{ value, label: value }, ...options]
      : options;

  return (
    <div className={styles.ctxRow}>
      <span className={styles.ctxLabel} id="chat-work-folder-label">
        Work folder
      </span>
      <Select
        aria-label="Work folder"
        value={value ?? NO_FOLDER}
        onChange={(next) => onChange(next === NO_FOLDER ? null : next)}
        disabled={disabled}
        options={[{ value: NO_FOLDER, label: "No work folder" }, ...withCurrent]}
      />
      {loading ? <span className={styles.ctxStatus}>Loading…</span> : null}
      {failed ? (
        <span className={styles.ctxStatus}>
          Couldn&apos;t load work folders.{" "}
          <button type="button" className={styles.ctxRetry} onClick={() => load(true)}>
            Retry
          </button>
        </span>
      ) : null}
    </div>
  );
}
