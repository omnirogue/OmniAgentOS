import type { HTMLAttributes, ReactNode } from "react";
import { cx } from "./utils";

export type CardPadding = "none" | "sm" | "md" | "lg";

export type CardProps = HTMLAttributes<HTMLDivElement> & {
  children: ReactNode;
  raised?: boolean;
  flush?: boolean;
  padding?: CardPadding;
};

const PAD_CLASS: Record<CardPadding, string | undefined> = {
  none: "ds-card--flush",
  sm: "ds-card--pad-sm",
  md: undefined,
  lg: "ds-card--pad-lg",
};

export function Card({
  children,
  raised,
  flush,
  padding,
  className,
  ...rest
}: CardProps) {
  const pad = flush ? "none" : padding;
  return (
    <div
      className={cx(
        "ds-card",
        raised && "ds-card--raised",
        pad ? PAD_CLASS[pad] : undefined,
        className,
      )}
      {...rest}
    >
      {children}
    </div>
  );
}
