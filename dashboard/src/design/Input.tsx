"use client";

import {
  useId,
  type InputHTMLAttributes,
  type ReactNode,
  type ChangeEvent,
} from "react";
import { Icon } from "./Icon";
import { cx } from "./utils";

export type InputProps = Omit<InputHTMLAttributes<HTMLInputElement>, "size"> & {
  label?: string;
  hint?: string;
  error?: string;
  icon?: ReactNode;
  clearable?: boolean;
  onClear?: () => void;
};

export function Input({
  label,
  hint,
  error,
  icon,
  clearable,
  onClear,
  className,
  id: idProp,
  disabled,
  value,
  defaultValue,
  onChange,
  ...rest
}: InputProps) {
  const genId = useId();
  const id = idProp ?? genId;
  const hintId = `${id}-hint`;
  const hasValue =
    (typeof value === "string" && value.length > 0) ||
    (typeof value === "number") ||
    (typeof defaultValue === "string" && defaultValue.length > 0 && value === undefined);

  const handleClear = () => {
    if (onClear) {
      onClear();
      return;
    }
    // Synthetic clear for uncontrolled/controlled without onClear
    if (onChange) {
      const fake = {
        target: { value: "" },
        currentTarget: { value: "" },
      } as ChangeEvent<HTMLInputElement>;
      onChange(fake);
    }
  };

  return (
    <div className={cx("ds-input-wrap", className)}>
      {label ? (
        <label className="ds-input-label" htmlFor={id}>
          {label}
        </label>
      ) : null}
      <div
        className={cx(
          "ds-input-field",
          error && "ds-input-field--error",
          disabled && "ds-input-field--disabled",
        )}
      >
        {icon ? <span className="ds-input__icon">{icon}</span> : null}
        <input
          id={id}
          className="ds-input"
          disabled={disabled}
          value={value}
          defaultValue={defaultValue}
          onChange={onChange}
          aria-invalid={error ? true : undefined}
          aria-describedby={error || hint ? hintId : undefined}
          {...rest}
        />
        {clearable && hasValue && !disabled ? (
          <button
            type="button"
            className="ds-input__clear"
            onClick={handleClear}
            aria-label="Clear input"
            tabIndex={-1}
          >
            <Icon name="close" size={13} />
          </button>
        ) : null}
      </div>
      {error || hint ? (
        <span
          id={hintId}
          className={cx("ds-input__hint", error && "ds-input__hint--error")}
          role={error ? "alert" : undefined}
        >
          {error ?? hint}
        </span>
      ) : null}
    </div>
  );
}
