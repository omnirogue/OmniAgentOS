"use client";

/**
 * ModelPicker (chat-v2 §2.3.1) — popover model selector for the composer deck.
 *
 * Grouped by lineage, unavailable entries disabled with a reason tooltip,
 * footer "Just this message" toggle (off = persists via PATCH {model}; on =
 * rides POST messages {model} only), and a "Routing…" row opening the routing
 * preferences dialog (allow/deny/speed/effort/hint, §2.3.4).
 *
 * ⌘J toggles the popover (wired by the composer).
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { Badge, Button, Dialog, Input, Select, useToast } from "@/design";
import { useModels } from "@/features/models/useModels";
import type { ModelEntry, RoutingPrefs } from "./chatApi";
import styles from "./chats.module.css";

interface ModelPickerProps {
  /** Currently effective model id (null/"" = auto). */
  value: string | null;
  /** Persist-or-once: when true the choice rides only the next send. */
  justThisMessage: boolean;
  onJustThisMessageChange: (next: boolean) => void;
  onSelect: (modelId: string | null) => void;
  routing: RoutingPrefs;
  onRoutingSave: (routing: RoutingPrefs) => Promise<void>;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

export function ModelPicker({
  value,
  justThisMessage,
  onJustThisMessageChange,
  onSelect,
  routing,
  onRoutingSave,
  open,
  onOpenChange,
}: ModelPickerProps) {
  const { models, loading, fallback } = useModels();
  const [routingOpen, setRoutingOpen] = useState(false);
  const popoverRef = useRef<HTMLDivElement>(null);

  // Close on outside click / Esc (Esc chain: popover → draft → drawer, §2.7)
  useEffect(() => {
    if (!open) return;
    const onMouseDown = (e: MouseEvent) => {
      if (popoverRef.current && !popoverRef.current.contains(e.target as Node)) {
        onOpenChange(false);
      }
    };
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        onOpenChange(false);
      }
    };
    document.addEventListener("mousedown", onMouseDown);
    document.addEventListener("keydown", onKeyDown, true);
    return () => {
      document.removeEventListener("mousedown", onMouseDown);
      document.removeEventListener("keydown", onKeyDown, true);
    };
  }, [open, onOpenChange]);

  const groups = useMemo(() => {
    const map = new Map<string, ModelEntry[]>();
    for (const model of models) {
      const key = model.lineage ?? "other";
      const list = map.get(key) ?? [];
      list.push(model);
      map.set(key, list);
    }
    return [...map.entries()].sort(([a], [b]) => a.localeCompare(b));
  }, [models]);

  const currentLabel =
    models.find((m) => m.id === (value ?? "auto"))?.label ??
    (value ? value : "Auto — router decides");

  return (
    <div className={styles.modelPicker} ref={popoverRef}>
      <button
        type="button"
        className={styles.deckButton}
        onClick={() => onOpenChange(!open)}
        aria-expanded={open}
        aria-haspopup="listbox"
        title="Model (⌘J)"
      >
        <span className={styles.deckButtonLabel}>{currentLabel}</span>
        <span aria-hidden="true">▾</span>
        {fallback ? (
          <Badge tone="warn" className={styles.modelWarnBadge}>
            models degraded
          </Badge>
        ) : null}
      </button>

      {open ? (
        <div className={styles.modelPopover} role="listbox" aria-label="Model picker">
          {loading ? (
            <div className={styles.modelSkeletonList}>
              {[0, 1, 2, 3].map((i) => (
                <div key={i} className="ds-skeleton" />
              ))}
            </div>
          ) : models.length === 0 ? (
            <p className={styles.modelEmpty}>
              No models available — check Connections.
            </p>
          ) : (
            groups.map(([lineage, entries]) => (
              <div key={lineage} className={styles.modelGroup}>
                <span className={styles.modelGroupLabel}>{lineage}</span>
                {entries.map((model) => {
                  const selected = (value ?? "auto") === model.id || (!value && model.id === "auto");
                  return (
                    <button
                      key={model.id}
                      type="button"
                      role="option"
                      aria-selected={selected}
                      className={`${styles.modelOption}${selected ? ` ${styles.modelOptionActive}` : ""}`}
                      disabled={!model.available}
                      title={
                        model.available
                          ? model.label
                          : model.unavailable_reason ?? "Unavailable — check Connections"
                      }
                      onClick={() => {
                        onSelect(model.id === "auto" ? null : model.id);
                        onOpenChange(false);
                      }}
                    >
                      <span>{model.label}</span>
                      {!model.available ? (
                        <span className={styles.modelUnavailable}>unavailable</span>
                      ) : null}
                    </button>
                  );
                })}
              </div>
            ))
          )}

          <div className={styles.modelFooter}>
            <label className={styles.modelJustThis}>
              <input
                type="checkbox"
                checked={justThisMessage}
                onChange={(e) => onJustThisMessageChange(e.target.checked)}
              />
              Just this message
            </label>
            <button
              type="button"
              className={styles.modelRoutingLink}
              onClick={() => {
                onOpenChange(false);
                setRoutingOpen(true);
              }}
            >
              Routing…
            </button>
          </div>
        </div>
      ) : null}

      <RoutingDialog
        open={routingOpen}
        onClose={() => setRoutingOpen(false)}
        routing={routing}
        onSave={async (next) => {
          await onRoutingSave(next);
          setRoutingOpen(false);
        }}
      />
    </div>
  );
}

// ── Routing preferences dialog (§2.3.4) ──────────────────────

const SPEED_OPTIONS = [
  { value: "", label: "No preference" },
  { value: "fast", label: "Fast" },
  { value: "auto", label: "Auto" },
  { value: "ultra", label: "Ultra" },
];

const EFFORT_OPTIONS = [
  { value: "", label: "No preference" },
  { value: "low", label: "Low" },
  { value: "medium", label: "Medium" },
  { value: "high", label: "High" },
];

function RoutingDialog({
  open,
  onClose,
  routing,
  onSave,
}: {
  open: boolean;
  onClose: () => void;
  routing: RoutingPrefs;
  onSave: (next: RoutingPrefs) => Promise<void>;
}) {
  const { push } = useToast();
  const [allow, setAllow] = useState(routing.allow.join(", "));
  const [deny, setDeny] = useState(routing.deny.join(", "));
  const [speed, setSpeed] = useState(routing.speed ?? "");
  const [effort, setEffort] = useState(routing.effort ?? "");
  const [hint, setHint] = useState(routing.hint ?? "");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (open) {
      setAllow(routing.allow.join(", "));
      setDeny(routing.deny.join(", "));
      setSpeed(routing.speed ?? "");
      setEffort(routing.effort ?? "");
      setHint(routing.hint ?? "");
    }
  }, [open, routing]);

  const parseList = (raw: string): string[] =>
    raw
      .split(/[,\n]/)
      .map((item) => item.trim())
      .filter(Boolean);

  const handleSave = async () => {
    setSaving(true);
    try {
      await onSave({
        allow: parseList(allow),
        deny: parseList(deny),
        speed: (speed || null) as RoutingPrefs["speed"],
        effort: (effort || null) as RoutingPrefs["effort"],
        hint: hint.trim() || null,
      });
      push({ tone: "success", message: "Routing preferences saved" });
    } catch (reason) {
      push({
        tone: "error",
        title: "Could not save routing",
        message: reason instanceof Error ? reason.message : "Unknown error",
      });
    } finally {
      setSaving(false);
    }
  };

  return (
    <Dialog
      open={open}
      onClose={onClose}
      title="Routing preferences"
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="primary" onClick={() => void handleSave()} disabled={saving}>
            {saving ? "Saving…" : "Save"}
          </Button>
        </>
      }
    >
      <div className={styles.routingForm}>
        <Input
          label="Tell it what models we want (free text — threaded into dispatch)"
          value={hint}
          onChange={(e) => setHint(e.target.value)}
          placeholder="e.g. prefer grok for code, claude for prose"
        />
        <Input
          label="Allow (comma-separated lineages or models)"
          value={allow}
          onChange={(e) => setAllow(e.target.value)}
          placeholder="e.g. grok, claude"
        />
        <Input
          label="Deny"
          value={deny}
          onChange={(e) => setDeny(e.target.value)}
          placeholder="e.g. gpt"
        />
        <div className={styles.routingGrid}>
          <Select
            label="Speed"
            value={speed}
            onChange={(v) => setSpeed(v as typeof speed)}
            options={SPEED_OPTIONS}
          />
          <Select
            label="Effort"
            value={effort}
            onChange={(v) => setEffort(v as typeof effort)}
            options={EFFORT_OPTIONS}
          />
        </div>
      </div>
    </Dialog>
  );
}
