import type { HTMLAttributes } from "react";
import { cx } from "./utils";

export type StatTone = "default" | "ok" | "warn" | "danger" | "accent" | "promote" | "reject";

export type StatProps = HTMLAttributes<HTMLDivElement> & {
  label: string;
  value: string | number;
  /** Absolute or relative change, e.g. "+2.4%" or -12 */
  delta?: string | number;
  /** Directional hint for delta color */
  deltaTrend?: "up" | "down" | "neutral";
  tone?: StatTone;
  /** Secondary caption under delta */
  hint?: string;
};

export function Stat({
  label,
  value,
  delta,
  deltaTrend = "neutral",
  tone = "default",
  hint,
  className,
  ...rest
}: StatProps) {
  return (
    <div
      className={cx("ds-stat", tone !== "default" && `ds-stat--${tone}`, className)}
      {...rest}
    >
      <span className="ds-stat__label">{label}</span>
      <span className="ds-stat__value">{value}</span>
      {delta != null && delta !== "" ? (
        <span className={cx("ds-stat__delta", `ds-stat__delta--${deltaTrend}`)}>
          {typeof delta === "number" && delta > 0 ? `+${delta}` : delta}
        </span>
      ) : null}
      {hint ? <span className="ds-stat__hint">{hint}</span> : null}
    </div>
  );
}
