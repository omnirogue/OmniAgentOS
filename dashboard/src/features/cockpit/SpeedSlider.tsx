"use client";

import { useCallback, useRef, type KeyboardEvent, type PointerEvent } from "react";
import { cx } from "@/design";
import styles from "./SpeedSlider.module.css";

/**
 * The composer's single routing control (D12): one speed↔quality slider.
 *
 * Topology (fastlane vs solo vs swarm) belongs to the ROUTER now — the old
 * Single|Swarm segment is gone, and a leading "solo:" / "swarm:" in the brief
 * is the remaining force hatch. The slider answers the STAKES question only:
 * how much model, review and retry budget the work deserves.
 *
 * Three snap detents map onto the orchestrator's speed knob (fast|auto|ultra).
 * Every position is a real, tested backend config — the levers behind them
 * (quality gate, retries, executor tiers, planner chains, account fan-out) are
 * discrete ladders, so detents are the honest design, not a compromise.
 *
 * A real radiogroup (arrow keys / Home / End, roving tabindex, aria-checked)
 * that also accepts click and pointer-drag on the track. Dragging commits the
 * NEAREST detent as the pointer moves — the thumb never floats between
 * positions, so aria-checked always tells the truth.
 */

export type Speed = "fast" | "auto" | "ultra";

const SPEEDS: ReadonlyArray<{ value: Speed; name: string; caption: string }> = [
  {
    value: "fast",
    name: "Fastest",
    caption: "Fastest models, lighter review — Gemini 3.6 planning",
  },
  {
    value: "auto",
    name: "Balanced",
    caption: "Router picks models, tiers and solo-vs-swarm",
  },
  {
    value: "ultra",
    name: "Max Quality",
    caption: "Fable max planning, full gates — up to 3 Fable sessions when swarming",
  },
];

export interface SpeedSliderProps {
  speed: Speed;
  onSpeedChange: (speed: Speed) => void;
  /** Disambiguates ids/labels when more than one slider mounts on a page. */
  idPrefix?: string;
}

export function SpeedSlider({
  speed,
  onSpeedChange,
  idPrefix = "speedslider",
}: SpeedSliderProps) {
  const trackRef = useRef<HTMLDivElement>(null);
  const draggingRef = useRef(false);
  // F4: roving tabindex — keyboard selection must MOVE DOM FOCUS to the newly
  // selected radio (a roving-tabindex group whose focus stays behind strands
  // the keyboard user on a tabindex=-1 element).
  const tickRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const selectedIndex = Math.max(0, SPEEDS.findIndex((s) => s.value === speed));

  const commitByIndex = useCallback(
    (index: number, moveFocus = false) => {
      const clamped = Math.min(SPEEDS.length - 1, Math.max(0, index));
      if (moveFocus) tickRefs.current[clamped]?.focus();
      const next = SPEEDS[clamped]!.value;
      if (next !== speed) onSpeedChange(next);
    },
    [onSpeedChange, speed],
  );

  const onTrackKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    let handled = true;
    switch (event.key) {
      case "ArrowLeft":
      case "ArrowUp":
        commitByIndex(selectedIndex - 1, true);
        break;
      case "ArrowRight":
      case "ArrowDown":
        commitByIndex(selectedIndex + 1, true);
        break;
      case "Home":
        commitByIndex(0, true);
        break;
      case "End":
        commitByIndex(SPEEDS.length - 1, true);
        break;
      default:
        handled = false;
    }
    if (handled) event.preventDefault();
  };

  // Map a pointer x to the nearest of the three slots, so a click or drag
  // anywhere on the track lands on a real position (one-control feel).
  const indexFromPointer = (clientX: number): number => {
    const el = trackRef.current;
    if (!el) return selectedIndex;
    const rect = el.getBoundingClientRect();
    if (rect.width <= 0) return selectedIndex;
    const ratio = (clientX - rect.left) / rect.width;
    return Math.min(SPEEDS.length - 1, Math.max(0, Math.floor(ratio * SPEEDS.length)));
  };

  const onPointerDown = (event: PointerEvent<HTMLDivElement>) => {
    if (event.button !== 0) return;
    draggingRef.current = true;
    event.currentTarget.setPointerCapture?.(event.pointerId);
    commitByIndex(indexFromPointer(event.clientX));
  };

  const onPointerMove = (event: PointerEvent<HTMLDivElement>) => {
    if (!draggingRef.current) return;
    commitByIndex(indexFromPointer(event.clientX));
  };

  const endDrag = (event: PointerEvent<HTMLDivElement>) => {
    if (!draggingRef.current) return;
    draggingRef.current = false;
    event.currentTarget.releasePointerCapture?.(event.pointerId);
  };

  return (
    <div className={styles.slider}>
      <div className={styles.axis} aria-hidden="true">
        <span>Speed</span>
        <span>Quality</span>
      </div>
      <div
        ref={trackRef}
        role="radiogroup"
        aria-label="Speed vs quality"
        className={styles.track}
        onKeyDown={onTrackKeyDown}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={endDrag}
        onPointerCancel={endDrag}
      >
        <div
          className={styles.thumb}
          style={{ ["--pos" as string]: String(selectedIndex) }}
          aria-hidden="true"
        />
        {SPEEDS.map((s, index) => {
          const active = s.value === speed;
          const nameId = `${idPrefix}-speed-${s.value}-name`;
          const captionId = `${idPrefix}-speed-${s.value}-caption`;
          return (
            <button
              key={s.value}
              type="button"
              role="radio"
              aria-checked={active}
              // F4: the caption is part of the radio's accessible surface —
              // name from the visible label, description from the caption
              // (an aria-label here would OVERRIDE both spans away).
              aria-labelledby={nameId}
              aria-describedby={captionId}
              tabIndex={active ? 0 : -1}
              ref={(el) => {
                tickRefs.current[index] = el;
              }}
              className={cx(styles.tick, active && styles.tickActive)}
              onClick={() => onSpeedChange(s.value)}
            >
              <span id={nameId} className={styles.tickName}>{s.name}</span>
              <span id={captionId} className={styles.tickCaption}>{s.caption}</span>
            </button>
          );
        })}
      </div>
    </div>
  );
}
