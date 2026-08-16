"use client";
import { useCallback, useEffect, useState } from "react";
import { Badge, StatusDot } from "../design";
import { api } from "../lib/api";
import { startVisibilityPoll } from "../lib/pollWhenVisible";
import type { Health } from "../lib/contracts";
type HealthKind = "ok" | "worker_stale" | "api_down" | "loading";
function relative(iso: string) { const sec = Math.max(0, Math.round((Date.now() - Date.parse(iso)) / 1000)); return sec < 60 ? `${sec}s ago` : sec < 3600 ? `${Math.floor(sec / 60)}m ago` : new Date(iso).toLocaleString(); }
function classify(h: Health) { const kind: HealthKind = h.status !== "ok" || !h.db ? "api_down" : !h.worker?.alive ? "worker_stale" : "ok"; return { kind, label: kind === "ok" ? "API OK" : kind === "worker_stale" ? "Worker stale" : "API down", beat: h.worker?.last_beat_at }; }
export function HealthBadge() { const [view, setView] = useState({ kind: "loading" as HealthKind, label: "Loading", beat: null as string | null }); const refresh = useCallback(async () => { try { setView(classify(await api.health())); } catch { setView({ kind: "api_down", label: "API down", beat: null }); } }, []); useEffect(() => { void refresh(); return startVisibilityPoll(() => void refresh(), 10_000); }, [refresh]); const state = view.kind === "ok" ? "ok" : view.kind === "worker_stale" ? "warn" : "danger"; return <div role="status" aria-live="polite" title={view.beat ? `Last beat ${relative(view.beat)}` : view.label} className="ds-inline-row"><StatusDot state={state} /><Badge tone={state}>{view.label}</Badge>{view.beat && view.kind !== "api_down" ? <span>{relative(view.beat)}</span> : null}</div>; }
