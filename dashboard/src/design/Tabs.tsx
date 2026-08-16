"use client";

import {
  useCallback,
  useId,
  useState,
  type KeyboardEvent,
  type ReactNode,
} from "react";
import { cx } from "./utils";

export type TabItem = {
  id: string;
  label: ReactNode;
  content: ReactNode;
  disabled?: boolean;
};

export type TabsProps = {
  items: TabItem[];
  defaultValue?: string;
  value?: string;
  onValueChange?: (id: string) => void;
  className?: string;
  "aria-label"?: string;
};

export function Tabs({
  items,
  defaultValue,
  value: controlled,
  onValueChange,
  className,
  "aria-label": ariaLabel = "Tabs",
}: TabsProps) {
  const uid = useId();
  const firstEnabled = items.find((i) => !i.disabled)?.id ?? items[0]?.id ?? "";
  const [uncontrolled, setUncontrolled] = useState(defaultValue ?? firstEnabled);
  const active = controlled ?? uncontrolled;

  const setActive = useCallback(
    (id: string) => {
      if (controlled === undefined) setUncontrolled(id);
      onValueChange?.(id);
    },
    [controlled, onValueChange],
  );

  const enabledIds = items.filter((i) => !i.disabled).map((i) => i.id);

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    const idx = enabledIds.indexOf(active);
    if (idx < 0) return;
    let next = idx;
    if (e.key === "ArrowRight" || e.key === "ArrowDown") {
      e.preventDefault();
      next = (idx + 1) % enabledIds.length;
    } else if (e.key === "ArrowLeft" || e.key === "ArrowUp") {
      e.preventDefault();
      next = (idx - 1 + enabledIds.length) % enabledIds.length;
    } else if (e.key === "Home") {
      e.preventDefault();
      next = 0;
    } else if (e.key === "End") {
      e.preventDefault();
      next = enabledIds.length - 1;
    } else {
      return;
    }
    setActive(enabledIds[next]!);
    const el = document.getElementById(`${uid}-tab-${enabledIds[next]}`);
    el?.focus();
  };

  const activeItem = items.find((i) => i.id === active) ?? items[0];

  return (
    <div className={cx("ds-tabs", className)}>
      <div
        className="ds-tabs__list"
        role="tablist"
        aria-label={ariaLabel}
        onKeyDown={onKeyDown}
      >
        {items.map((item) => {
          const selected = item.id === active;
          return (
            <button
              key={item.id}
              type="button"
              role="tab"
              id={`${uid}-tab-${item.id}`}
              className="ds-tabs__tab"
              aria-selected={selected}
              aria-controls={`${uid}-panel-${item.id}`}
              tabIndex={selected ? 0 : -1}
              disabled={item.disabled}
              onClick={() => setActive(item.id)}
            >
              {item.label}
            </button>
          );
        })}
      </div>
      {activeItem ? (
        <div
          role="tabpanel"
          id={`${uid}-panel-${activeItem.id}`}
          aria-labelledby={`${uid}-tab-${activeItem.id}`}
          className="ds-tabs__panel"
          tabIndex={0}
        >
          {activeItem.content}
        </div>
      ) : null}
    </div>
  );
}
