"use client";

import { useId, useState, type ReactNode } from "react";
import { cx } from "./utils";

export type TooltipProps = {
  content: ReactNode;
  children: ReactNode;
  side?: "top" | "bottom" | "left" | "right";
  className?: string;
};

export function Tooltip({ content, children, side = "top", className }: TooltipProps) {
  const [open, setOpen] = useState(false);
  const id = useId();

  return (
    <span
      className={cx("ds-tooltip-wrap", className)}
      onMouseEnter={() => setOpen(true)}
      onMouseLeave={() => setOpen(false)}
      onFocus={() => setOpen(true)}
      onBlur={() => setOpen(false)}
    >
      <span aria-describedby={open ? id : undefined}>{children}</span>
      {open ? (
        <span role="tooltip" id={id} className={cx("ds-tooltip", `ds-tooltip--${side}`)}>
          {content}
          <span className="ds-tooltip__arrow" aria-hidden="true" />
        </span>
      ) : null}
    </span>
  );
}
