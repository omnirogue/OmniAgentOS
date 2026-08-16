"use client";

import { useState, type ReactNode } from "react";
import { Icon } from "@/design";
import styles from "./system.module.css";

/** Relative age like the reliability page's `age()`, plus a "never" sentinel. */
export function relTime(iso: string | null | undefined): string {
  if (!iso) return "never observed";
  const ms = Date.parse(iso);
  if (Number.isNaN(ms)) return "unknown";
  const mins = Math.floor(Math.max(0, Date.now() - ms) / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.floor(hrs / 24);
  return `${days}d ago`;
}

export function fmtDate(iso: string | null | undefined): string {
  if (!iso) return "-";
  const ms = Date.parse(iso);
  return Number.isNaN(ms) ? iso : new Date(ms).toLocaleString();
}

/** Forward-looking companion to `relTime`, e.g. "in 3h" — for next-fire estimates. */
export function relFuture(iso: string | null | undefined): string {
  if (!iso) return "unscheduled";
  const ms = Date.parse(iso);
  if (Number.isNaN(ms)) return "unknown";
  const mins = Math.floor((ms - Date.now()) / 60000);
  if (mins <= 0) return "imminent";
  if (mins < 60) return `in ${mins}m`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `in ${hrs}h`;
  const days = Math.floor(hrs / 24);
  return `in ${days}d`;
}

/** A mono file path with a copy-to-clipboard button — the "edit this file" affordance. */
export function PathChip({ path, label }: { path: string; label?: string }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(path);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1400);
    } catch {
      /* clipboard blocked — the path is still visible to select manually */
    }
  };
  return (
    <span className={styles.pathChip}>
      {label ? <span className={styles.pathLabel}>{label}</span> : null}
      <code className={styles.pathCode} title={path}>
        {path}
      </code>
      <button type="button" className={styles.copyBtn} onClick={copy} aria-label="Copy path">
        <Icon name={copied ? "checkCircle" : "columns"} size={13} />
        {copied ? "Copied" : "Copy"}
      </button>
    </span>
  );
}

export function EditableNote({ children }: { children: ReactNode }) {
  return <p className={styles.editableNote}>{children}</p>;
}
