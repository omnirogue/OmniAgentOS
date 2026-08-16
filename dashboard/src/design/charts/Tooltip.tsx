import type { CSSProperties, ReactNode } from "react";
import { cx } from "../utils";

export type ChartTooltipProps = {
  x: number;
  y: number;
  title?: ReactNode;
  rows?: Array<{ label: string; value: string; color?: string }>;
  children?: ReactNode;
  className?: string;
  visible?: boolean;
};

/** Absolute-positioned chart hover tooltip (parent should be position:relative). */
export function Tooltip({
  x,
  y,
  title,
  rows,
  children,
  className,
  visible = true,
}: ChartTooltipProps) {
  if (!visible) return null;
  const style: CSSProperties = {
    left: x,
    top: y,
    transform: "translate(-50%, calc(-100% - 8px))",
  };
  return (
    <div className={cx("ds-chart-tooltip", className)} style={style} role="tooltip">
      {title ? <div className="ds-chart-tooltip__title">{title}</div> : null}
      {rows?.map((r) => (
        <div key={r.label} className="ds-chart-tooltip__row">
          {r.color ? (
            <span
              style={{
                display: "inline-block",
                width: 8,
                height: 8,
                borderRadius: 2,
                background: r.color,
                marginRight: 6,
              }}
            />
          ) : null}
          {r.label}: {r.value}
        </div>
      ))}
      {children}
    </div>
  );
}
