import type { HTMLAttributes, ReactNode } from "react";
import { cx } from "./utils";

export type DefinitionItem = { label: ReactNode; value: ReactNode; key?: string };

export type DefinitionListProps = Omit<HTMLAttributes<HTMLDListElement>, "children"> & {
  items: DefinitionItem[];
  /** Single label/value column (default) or a 2-up grid for longer lists. */
  columns?: 1 | 2;
};

/**
 * A styled key/value list — replaces bare browser-default <dl>/<dt>/<dd> (40px indent,
 * no rhythm) with hairline-divided rows matching the rest of the system.
 */
export function DefinitionList({ items, columns = 1, className, ...rest }: DefinitionListProps) {
  return (
    <dl className={cx("ds-deflist", columns === 2 && "ds-deflist--2col", className)} {...rest}>
      {items.map((item, i) => (
        <div className="ds-deflist__row" key={item.key ?? i}>
          <dt className="ds-deflist__label">{item.label}</dt>
          <dd className="ds-deflist__value">{item.value}</dd>
        </div>
      ))}
    </dl>
  );
}
