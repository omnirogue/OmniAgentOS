import type { HTMLAttributes } from "react";
import { cx } from "./utils";

export type StatusDotState =
  | "queued"
  | "running"
  | "awaiting"
  | "awaiting_approval"
  | "validating"
  | "paused"
  | "completed"
  | "failed"
  | "cancelled"
  | "ok"
  | "warn"
  | "danger";

export type StatusDotProps = HTMLAttributes<HTMLSpanElement> & {
  state?: StatusDotState;
  label?: string;
};

const LABELS: Record<StatusDotState, string> = {
  queued: "Queued",
  running: "Running",
  awaiting: "Awaiting",
  awaiting_approval: "Awaiting approval",
  validating: "Validating",
  paused: "Paused",
  completed: "Completed",
  failed: "Failed",
  cancelled: "Cancelled",
  ok: "OK",
  warn: "Warning",
  danger: "Danger",
};

export function StatusDot({
  state = "queued",
  label,
  className,
  ...rest
}: StatusDotProps) {
  const aria = label ?? LABELS[state];
  return (
    <span
      className={cx("ds-status-dot", `ds-status-dot--${state}`, className)}
      role="status"
      aria-label={aria}
      title={aria}
      {...rest}
    />
  );
}
