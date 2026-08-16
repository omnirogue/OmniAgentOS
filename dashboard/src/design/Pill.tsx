import type { HTMLAttributes, ReactNode } from "react";
import { Icon } from "./Icon";
import { cx } from "./utils";

export type PillTone = "neutral" | "accent" | "ok" | "warn" | "danger" | "promote" | "reject";

export type PillProps = HTMLAttributes<HTMLSpanElement> & {
  children: ReactNode;
  tone?: PillTone;
  onDismiss?: () => void;
  dismissLabel?: string;
};

export function Pill({
  children,
  tone = "neutral",
  onDismiss,
  dismissLabel = "Remove",
  className,
  ...rest
}: PillProps) {
  return (
    <span className={cx("ds-pill", `ds-pill--${tone}`, className)} {...rest}>
      <span className="ds-pill__label">{children}</span>
      {onDismiss ? (
        <button
          type="button"
          className="ds-pill__dismiss"
          onClick={onDismiss}
          aria-label={dismissLabel}
        >
          <Icon name="close" size={11} strokeWidth={2} />
        </button>
      ) : null}
    </span>
  );
}
