import type { ReactNode } from "react";
import { Button } from "./Button";
import { Icon } from "./Icon";
import { cx } from "./utils";

export type ErrorStateProps = {
  icon?: ReactNode;
  title?: string;
  message: string;
  onRetry?: () => void;
  retryLabel?: string;
  action?: ReactNode;
  className?: string;
};

export function ErrorState({
  icon = <Icon name="alertTriangle" size={20} />,
  title = "Something went wrong",
  message,
  onRetry,
  retryLabel = "Retry",
  action,
  className,
}: ErrorStateProps) {
  return (
    <div className={cx("ds-state", className)} role="alert">
      <div
        className="ds-state__icon"
        aria-hidden="true"
        style={{
          width: "2.5rem",
          height: "2.5rem",
          borderRadius: "var(--radius-pill)",
          display: "grid",
          placeItems: "center",
          background: "color-mix(in srgb, var(--danger) 16%, transparent)",
          color: "var(--danger)",
        }}
      >
        {icon}
      </div>
      <h3 className="ds-state__title">{title}</h3>
      <p className="ds-state__message">{message}</p>
      {action ??
        (onRetry ? (
          <Button variant="secondary" size="sm" onClick={onRetry}>
            {retryLabel}
          </Button>
        ) : null)}
    </div>
  );
}
