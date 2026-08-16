import type { HTMLAttributes, ReactNode } from "react";
import { cx } from "./utils";

/** Semantic tone maps to CSS badge classes (domain colors from tokens). */
export type BadgeTone =
  | "neutral"
  | "champion"
  | "challenger"
  | "promote"
  | "reject"
  | "win"
  | "loss"
  | "draw"
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
  | "danger"
  | "invalid"
  | "human_review";

/** Optional semantic category (documentation / future mapping); tone drives color. */
export type BadgeCategory = "arm" | "disposition" | "result" | "runState" | "neutral";

export type BadgeProps = HTMLAttributes<HTMLSpanElement> & {
  tone?: BadgeTone;
  /** Semantic domain group — arm | disposition | result | runState */
  category?: BadgeCategory | string;
  children: ReactNode;
};

export function Badge({ tone = "neutral", category, className, children, ...rest }: BadgeProps) {
  return (
    <span
      className={cx("ds-badge", `ds-badge--${tone}`, className)}
      data-category={category}
      {...rest}
    >
      {children}
    </span>
  );
}
