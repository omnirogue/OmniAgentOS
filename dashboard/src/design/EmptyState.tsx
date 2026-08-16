import type { ReactNode } from "react";
import { Icon } from "./Icon";
import { cx } from "./utils";

export type EmptyStateProps = {
  icon?: ReactNode;
  title?: string;
  message: string;
  action?: ReactNode;
  className?: string;
};

export function EmptyState({
  icon = <Icon name="inbox" size={22} />,
  title,
  message,
  action,
  className,
}: EmptyStateProps) {
  return (
    <div className={cx("ds-state", className)} role="status">
      <div className="ds-state__icon" aria-hidden="true">
        {icon}
      </div>
      {title ? <h3 className="ds-state__title">{title}</h3> : null}
      <p className="ds-state__message">{message}</p>
      {action ? <div>{action}</div> : null}
    </div>
  );
}
