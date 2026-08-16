import type { HTMLAttributes } from "react";
import { cx } from "./utils";

export type LoadingProps = HTMLAttributes<HTMLDivElement> & {
  /** skeleton bars vs spinner */
  variant?: "spinner" | "skeleton";
  label?: string;
  /** skeleton line count */
  lines?: number;
};

export function Loading({
  variant = "spinner",
  label = "Loading…",
  lines = 3,
  className,
  ...rest
}: LoadingProps) {
  if (variant === "skeleton") {
    return (
      <div
        className={cx("ds-state", className)}
        role="status"
        aria-busy="true"
        aria-label={label}
        style={{ alignItems: "stretch", width: "100%", gap: "var(--space-2)" }}
        {...rest}
      >
        {Array.from({ length: lines }, (_, i) => (
          <div
            key={i}
            className="ds-skeleton"
            style={{
              height: "0.75rem",
              width: i === lines - 1 ? "60%" : "100%",
            }}
          />
        ))}
        <span className="sr-only">{label}</span>
      </div>
    );
  }

  return (
    <div
      className={cx("ds-state", className)}
      role="status"
      aria-busy="true"
      aria-label={label}
      {...rest}
    >
      <div className="ds-spinner" aria-hidden="true" />
      <p className="ds-state__message">{label}</p>
    </div>
  );
}
