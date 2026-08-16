import type { ReactNode } from "react";
import { EmptyState } from "../EmptyState";
import { Icon } from "../Icon";

export type ChartEmptyProps = {
  message?: string;
  title?: string;
  action?: ReactNode;
  className?: string;
};

export function Empty({
  message = "No data to display",
  title,
  action,
  className,
}: ChartEmptyProps) {
  return (
    <EmptyState
      icon={<Icon name="barChart" size={22} />}
      title={title}
      message={message}
      action={action}
      className={className}
    />
  );
}
