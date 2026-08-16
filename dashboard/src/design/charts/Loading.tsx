import { Loading as PrimitiveLoading } from "../Loading";

export type ChartLoadingProps = {
  label?: string;
  className?: string;
};

export function Loading({ label = "Loading chart…", className }: ChartLoadingProps) {
  return <PrimitiveLoading variant="spinner" label={label} className={className} />;
}
