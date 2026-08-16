"use client";

import {
  useEffect,
  useId,
  useRef,
  useState,
  type KeyboardEvent,
} from "react";
import { Icon } from "./Icon";
import { cx } from "./utils";

export type SelectOption = {
  value: string;
  label: string;
  disabled?: boolean;
};

export type SelectProps = {
  options: SelectOption[];
  value?: string;
  defaultValue?: string;
  onChange?: (value: string) => void;
  placeholder?: string;
  disabled?: boolean;
  label?: string;
  className?: string;
  id?: string;
  "aria-label"?: string;
};

export function Select({
  options,
  value: controlled,
  defaultValue,
  onChange,
  placeholder = "Select…",
  disabled,
  label,
  className,
  id: idProp,
  "aria-label": ariaLabel,
}: SelectProps) {
  const genId = useId();
  const id = idProp ?? genId;
  const listId = `${id}-listbox`;
  const [open, setOpen] = useState(false);
  const [uncontrolled, setUncontrolled] = useState(defaultValue ?? "");
  const [highlight, setHighlight] = useState(0);
  const rootRef = useRef<HTMLDivElement>(null);
  const value = controlled ?? uncontrolled;

  const selected = options.find((o) => o.value === value);
  const enabled = options.filter((o) => !o.disabled);

  useEffect(() => {
    if (!open) return;
    const idx = Math.max(
      0,
      enabled.findIndex((o) => o.value === value),
    );
    setHighlight(idx);
  }, [open, value, enabled]);

  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [open]);

  const commit = (v: string) => {
    if (controlled === undefined) setUncontrolled(v);
    onChange?.(v);
    setOpen(false);
  };

  const onKeyDown = (e: KeyboardEvent) => {
    if (disabled) return;
    if (!open && (e.key === "ArrowDown" || e.key === "ArrowUp" || e.key === "Enter" || e.key === " ")) {
      e.preventDefault();
      setOpen(true);
      return;
    }
    if (!open) return;
    if (e.key === "Escape") {
      e.preventDefault();
      setOpen(false);
      return;
    }
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setHighlight((h) => Math.min(h + 1, enabled.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setHighlight((h) => Math.max(h - 1, 0));
    } else if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      const opt = enabled[highlight];
      if (opt) commit(opt.value);
    } else if (e.key === "Home") {
      e.preventDefault();
      setHighlight(0);
    } else if (e.key === "End") {
      e.preventDefault();
      setHighlight(enabled.length - 1);
    }
  };

  return (
    <div className={cx("ds-select", className)} ref={rootRef}>
      {label ? (
        <label className="ds-input-label" htmlFor={id} style={{ display: "block", marginBottom: "var(--space-1)" }}>
          {label}
        </label>
      ) : null}
      <button
        type="button"
        id={id}
        className="ds-select__trigger"
        disabled={disabled}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={open ? listId : undefined}
        aria-label={ariaLabel ?? label}
        onClick={() => setOpen((o) => !o)}
        onKeyDown={onKeyDown}
      >
        <span
          className={cx(
            "ds-select__value",
            !selected && "ds-select__value--placeholder",
          )}
        >
          {selected?.label ?? placeholder}
        </span>
        <span className="ds-select__chevron" aria-hidden="true">
          <Icon name="chevronDown" size={14} />
        </span>
      </button>
      {open ? (
        <ul
          id={listId}
          className="ds-select__list"
          role="listbox"
          aria-labelledby={id}
          tabIndex={-1}
        >
          {options.map((opt) => {
            const enabledIdx = enabled.findIndex((e) => e.value === opt.value);
            const isHighlighted = !opt.disabled && enabledIdx === highlight;
            return (
              <li
                key={opt.value}
                role="option"
                aria-selected={opt.value === value}
                aria-disabled={opt.disabled || undefined}
                data-highlighted={isHighlighted ? "true" : undefined}
                className="ds-select__option"
                onMouseEnter={() => {
                  if (!opt.disabled && enabledIdx >= 0) setHighlight(enabledIdx);
                }}
                onClick={() => {
                  if (!opt.disabled) commit(opt.value);
                }}
              >
                {opt.label}
              </li>
            );
          })}
        </ul>
      ) : null}
    </div>
  );
}
