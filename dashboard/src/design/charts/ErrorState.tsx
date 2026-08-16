import { ErrorState as PrimitiveError } from "../ErrorState";

export type ChartErrorProps = {
  message?: string;
  title?: string;
  onRetry?: () => void;
  className?: string;
};

export function ErrorState({
  message = "Failed to load chart data",
  title = "Chart error",
  onRetry,
  className,
}: ChartErrorProps) {
  return (
    <PrimitiveError
      title={title}
      message={message}
      onRetry={onRetry}
      className={className}
    />
  );
}
