"use client";

/**
 * Memory Galaxy — the interactive knowledge graph. Inline SVG only (no external graph/
 * force library). Layout is computed once per data load by layout.ts (deterministic);
 * this component only handles rendering + interaction (hover-highlight neighbors, click
 * to select, keyboard activation, wheel-zoom + drag-pan with button fallbacks).
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { Button, ChartTooltip, EmptyState, ErrorState, Icon, Loading, cx } from "../../design";
import { buildGraph, type GraphNode } from "./layout";
import type { VaultLinkIndex } from "./linkResolver";
import { noteTypeColor, noteTypeLabel } from "./noteType";
import type { AnyNoteType, FetchStatus, VaultTreeNote } from "./types";

const WIDTH = 1000;
const HEIGHT = 700;
const MIN_W = WIDTH * 0.2;
const MAX_W = WIDTH * 2.4;
const LABEL_THRESHOLD = 34;

function clamp(v: number, lo: number, hi: number): number {
  return Math.max(lo, Math.min(hi, v));
}

function truncateLabel(s: string, n = 28): string {
  return s.length > n ? `${s.slice(0, n - 1)}…` : s;
}

export interface GalaxyGraphProps {
  notes: VaultTreeNote[];
  linkIndex: VaultLinkIndex;
  status: FetchStatus;
  errorMessage?: string | null;
  onRetry?: () => void;
  selectedPath?: string | null;
  onSelectNote: (note: VaultTreeNote) => void;
  hiddenTypes?: ReadonlySet<AnyNoteType>;
  height?: number;
  className?: string;
}

export function GalaxyGraph({
  notes,
  linkIndex,
  status,
  errorMessage,
  onRetry,
  selectedPath = null,
  onSelectNote,
  hiddenTypes,
  height = 520,
  className,
}: GalaxyGraphProps) {
  const graph = useMemo(() => buildGraph(notes, linkIndex, { width: WIDTH, height: HEIGHT }), [notes, linkIndex]);

  const containerRef = useRef<HTMLDivElement>(null);
  const svgRef = useRef<SVGSVGElement>(null);
  const dragRef = useRef<{ startX: number; startY: number; viewX: number; viewY: number } | null>(null);
  const didDragRef = useRef(false);

  const [view, setView] = useState({ x: 0, y: 0, w: WIDTH, h: HEIGHT });
  const [dragging, setDragging] = useState(false);
  const [hoverPath, setHoverPath] = useState<string | null>(null);
  const [focusPath, setFocusPath] = useState<string | null>(null);
  const [tooltip, setTooltip] = useState<{ x: number; y: number; node: GraphNode } | null>(null);

  useEffect(() => {
    setView({ x: 0, y: 0, w: WIDTH, h: HEIGHT });
  }, [graph]);

  useEffect(() => {
    const el = svgRef.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const rect = el.getBoundingClientRect();
      if (rect.width === 0 || rect.height === 0) return;
      setView((v) => {
        const mx = ((e.clientX - rect.left) / rect.width) * v.w + v.x;
        const my = ((e.clientY - rect.top) / rect.height) * v.h + v.y;
        const scale = e.deltaY > 0 ? 1.12 : 1 / 1.12;
        const w = clamp(v.w * scale, MIN_W, MAX_W);
        const h = w * (HEIGHT / WIDTH);
        const x = mx - ((mx - v.x) / v.w) * w;
        const y = my - ((my - v.y) / v.h) * h;
        return { x, y, w, h };
      });
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, []);

  useEffect(() => {
    const onMove = (e: MouseEvent) => {
      if (!dragRef.current || !svgRef.current) return;
      const rect = svgRef.current.getBoundingClientRect();
      if (rect.width === 0 || rect.height === 0) return;
      const dxScreen = e.clientX - dragRef.current.startX;
      const dyScreen = e.clientY - dragRef.current.startY;
      if (Math.hypot(dxScreen, dyScreen) > 3) didDragRef.current = true;
      const dx = (dxScreen / rect.width) * view.w;
      const dy = (dyScreen / rect.height) * view.h;
      setView((v) => ({ ...v, x: dragRef.current!.viewX - dx, y: dragRef.current!.viewY - dy }));
    };
    const onUp = () => {
      dragRef.current = null;
      setDragging(false);
    };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
    return () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
  }, [view.w, view.h]);

  const zoomBy = (factor: number) => {
    setView((v) => {
      const ccx = v.x + v.w / 2;
      const ccy = v.y + v.h / 2;
      const w = clamp(v.w * factor, MIN_W, MAX_W);
      const h = w * (HEIGHT / WIDTH);
      return { x: ccx - w / 2, y: ccy - h / 2, w, h };
    });
  };
  const resetView = () => setView({ x: 0, y: 0, w: WIDTH, h: HEIGHT });

  const nodeByPath = useMemo(() => new Map(graph.nodes.map((n) => [n.path, n])), [graph]);

  const visibleNodes = useMemo(
    () => (hiddenTypes && hiddenTypes.size > 0 ? graph.nodes.filter((n) => !hiddenTypes.has(n.type)) : graph.nodes),
    [graph, hiddenTypes],
  );
  const visiblePaths = useMemo(() => new Set(visibleNodes.map((n) => n.path)), [visibleNodes]);
  const visibleEdges = useMemo(
    () => graph.edges.filter((e) => visiblePaths.has(e.source) && visiblePaths.has(e.target)),
    [graph, visiblePaths],
  );

  const highlightPath = hoverPath ?? focusPath;
  const neighbors = highlightPath ? graph.adjacency.get(highlightPath) : undefined;
  const showAllLabels = visibleNodes.length <= LABEL_THRESHOLD;

  if (status === "loading") {
    return (
      <div className={cx("ds-chart", className)} style={{ minHeight: height }}>
        <Loading label="Loading the vault…" />
      </div>
    );
  }
  if (status === "error") {
    return (
      <div className={cx("ds-chart", className)} style={{ minHeight: height }}>
        <ErrorState message={errorMessage ?? "Could not load the vault."} onRetry={onRetry} />
      </div>
    );
  }
  if (status === "empty" || notes.length === 0) {
    return (
      <div className={cx("ds-chart", className)} style={{ minHeight: height }}>
        <EmptyState
          icon={<Icon name="sparkles" size={22} />}
          title="The vault is empty"
          message="Once the lab writes vault notes (runs, experiments, learnings…) they will appear here as a graph."
        />
      </div>
    );
  }
  if (visibleNodes.length === 0) {
    return (
      <div className={cx("ds-chart", className)} style={{ minHeight: height }}>
        <EmptyState icon={<Icon name="search" size={22} />} message="No notes match the current type filters." />
      </div>
    );
  }

  return (
    <div
      ref={containerRef}
      className={cx("ds-chart", className)}
      style={{ position: "relative" }}
      onMouseLeave={() => {
        setHoverPath(null);
        setTooltip(null);
      }}
    >
      <div style={{ position: "absolute", top: "var(--space-2)", right: "var(--space-2)", zIndex: 1, display: "flex", gap: "var(--space-1)" }}>
        <Button variant="ghost" size="sm" onClick={() => zoomBy(1 / 1.3)} aria-label="Zoom in" title="Zoom in">
          <Icon name="plus" size={14} />
        </Button>
        <Button variant="ghost" size="sm" onClick={() => zoomBy(1.3)} aria-label="Zoom out" title="Zoom out">
          <Icon name="minus" size={14} />
        </Button>
        <Button variant="ghost" size="sm" onClick={resetView} aria-label="Reset view" title="Reset view">
          <Icon name="scan" size={14} />
        </Button>
      </div>

      <svg
        ref={svgRef}
        viewBox={`${view.x} ${view.y} ${view.w} ${view.h}`}
        aria-label={`Vault memory galaxy: ${visibleNodes.length} notes, ${visibleEdges.length} links between them`}
        style={{
          width: "100%",
          height,
          display: "block",
          touchAction: "none",
          cursor: dragging ? "grabbing" : "grab",
          background: "var(--canvas)",
          borderRadius: "var(--radius-lg)",
          border: "1px solid var(--border)",
        }}
        onMouseDown={(e) => {
          if (e.button !== 0) return;
          didDragRef.current = false;
          setDragging(true);
          dragRef.current = { startX: e.clientX, startY: e.clientY, viewX: view.x, viewY: view.y };
        }}
      >
        <g>
          {visibleEdges.map((e, i) => {
            const s = nodeByPath.get(e.source);
            const t = nodeByPath.get(e.target);
            if (!s || !t) return null;
            const touches = highlightPath === e.source || highlightPath === e.target;
            const dim = highlightPath != null && !touches;
            return (
              <line
                key={i}
                x1={s.x}
                y1={s.y}
                x2={t.x}
                y2={t.y}
                style={{
                  stroke: touches ? "var(--accent)" : "var(--border-strong)",
                  strokeWidth: touches ? 1.6 : 0.9,
                  opacity: dim ? 0.12 : touches ? 0.85 : 0.5,
                  transition: "opacity 120ms ease, stroke 120ms ease",
                }}
              />
            );
          })}
        </g>
        <g>
          {visibleNodes.map((n) => {
            const isSelected = n.path === selectedPath;
            const isActive = n.path === highlightPath;
            const isNeighbor = highlightPath ? (neighbors?.has(n.path) ?? false) : false;
            const dim = highlightPath != null && !isActive && !isNeighbor && !isSelected;
            const r = 6 + Math.min(9, Math.sqrt(n.degree) * 2.6);
            const color = noteTypeColor(n.type);
            const showLabel = showAllLabels || isActive || isSelected;
            return (
              <g
                key={n.path}
                tabIndex={0}
                role="button"
                aria-label={`${n.title} — ${noteTypeLabel(n.type)}${isSelected ? " (open)" : ""}`}
                aria-current={isSelected || undefined}
                onMouseEnter={() => setHoverPath(n.path)}
                onMouseLeave={() => setHoverPath((p) => (p === n.path ? null : p))}
                onMouseMove={(e) => {
                  const parent = containerRef.current?.getBoundingClientRect();
                  if (!parent) return;
                  setTooltip({ x: e.clientX - parent.left, y: e.clientY - parent.top, node: n });
                }}
                onFocus={() => setFocusPath(n.path)}
                onBlur={() => setFocusPath((p) => (p === n.path ? null : p))}
                onClick={() => {
                  if (didDragRef.current) return;
                  onSelectNote(n);
                }}
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    onSelectNote(n);
                  }
                }}
                style={{ cursor: "pointer", opacity: dim ? 0.25 : 1, transition: "opacity 120ms ease" }}
              >
                {isSelected || isActive ? (
                  <circle
                    cx={n.x}
                    cy={n.y}
                    r={r + 5}
                    fill="none"
                    style={{ stroke: isSelected ? "var(--accent)" : "var(--text-muted)", strokeWidth: isSelected ? 2.5 : 1.5 }}
                  />
                ) : null}
                <circle cx={n.x} cy={n.y} r={r} style={{ fill: color, stroke: "var(--surface)", strokeWidth: 1.5 }} />
                {showLabel ? (
                  <text
                    x={n.x}
                    y={n.y - r - 6}
                    textAnchor="middle"
                    style={{ fill: "var(--text)", fontSize: 11, fontFamily: "var(--font-sans)", pointerEvents: "none" }}
                  >
                    {truncateLabel(n.title)}
                  </text>
                ) : null}
              </g>
            );
          })}
        </g>
      </svg>

      {tooltip ? (
        <ChartTooltip
          x={tooltip.x}
          y={tooltip.y}
          title={tooltip.node.title}
          rows={[
            { label: "type", value: noteTypeLabel(tooltip.node.type), color: noteTypeColor(tooltip.node.type) },
            ...(tooltip.node.discipline ? [{ label: "discipline", value: tooltip.node.discipline }] : []),
            { label: "links", value: String(tooltip.node.degree) },
          ]}
        />
      ) : null}

      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          marginTop: "var(--space-2)",
          fontSize: "var(--text-micro)",
          color: "var(--text-faint)",
        }}
      >
        <span>
          {visibleNodes.length} notes · {visibleEdges.length} links
        </span>
        <span>scroll to zoom · drag to pan</span>
      </div>
    </div>
  );
}
