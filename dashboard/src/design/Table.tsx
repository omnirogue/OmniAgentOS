"use client";

import {
  useMemo,
  useState,
  type HTMLAttributes,
  type ReactNode,
  type ThHTMLAttributes,
} from "react";
import { Icon } from "./Icon";
import { cx } from "./utils";

export type SortDir = "asc" | "desc" | null;

export type TableColumn<T> = {
  key: string;
  header: ReactNode;
  sortable?: boolean;
  align?: "left" | "right" | "center";
  render: (row: T) => ReactNode;
  sortValue?: (row: T) => string | number | boolean | null | undefined;
};

export type TableProps<T> = {
  columns: TableColumn<T>[];
  rows: T[];
  rowKey: (row: T) => string;
  className?: string;
  dense?: boolean;
  emptyMessage?: string;
  caption?: string;
};

export function Table<T>({
  columns,
  rows,
  rowKey,
  className,
  emptyMessage = "No rows",
  caption,
}: TableProps<T>) {
  const [sortKey, setSortKey] = useState<string | null>(null);
  const [sortDir, setSortDir] = useState<SortDir>(null);

  const sorted = useMemo(() => {
    if (!sortKey || !sortDir) return rows;
    const col = columns.find((c) => c.key === sortKey);
    if (!col) return rows;
    const get = col.sortValue ?? ((row: T) => {
      const v = col.render(row);
      return typeof v === "string" || typeof v === "number" ? v : String(v ?? "");
    });
    const copy = [...rows];
    copy.sort((a, b) => {
      const av = get(a);
      const bv = get(b);
      if (av == null && bv == null) return 0;
      if (av == null) return 1;
      if (bv == null) return -1;
      if (typeof av === "number" && typeof bv === "number") {
        return sortDir === "asc" ? av - bv : bv - av;
      }
      const as = String(av);
      const bs = String(bv);
      return sortDir === "asc" ? as.localeCompare(bs) : bs.localeCompare(as);
    });
    return copy;
  }, [columns, rows, sortDir, sortKey]);

  const onSort = (key: string) => {
    if (sortKey !== key) {
      setSortKey(key);
      setSortDir("asc");
      return;
    }
    if (sortDir === "asc") setSortDir("desc");
    else if (sortDir === "desc") {
      setSortKey(null);
      setSortDir(null);
    } else setSortDir("asc");
  };

  return (
    <div className={cx("ds-table-wrap", className)}>
      <table className="ds-table">
        {caption ? <caption className="sr-only">{caption}</caption> : null}
        <thead>
          <tr>
            {columns.map((col) => {
              const active = sortKey === col.key ? sortDir : null;
              const ariaSort =
                active === "asc" ? "ascending" : active === "desc" ? "descending" : "none";
              return (
                <th
                  key={col.key}
                  scope="col"
                  aria-sort={col.sortable ? ariaSort : undefined}
                  style={col.align ? { textAlign: col.align } : undefined}
                >
                  {col.sortable ? (
                    <button
                      type="button"
                      className="ds-table__sort"
                      onClick={() => onSort(col.key)}
                    >
                      {col.header}
                      <span className="ds-table__sort-icon" aria-hidden="true">
                        <Icon
                          name={active === "asc" ? "sortAsc" : active === "desc" ? "sortDesc" : "sortNone"}
                          size={12}
                        />
                      </span>
                    </button>
                  ) : (
                    col.header
                  )}
                </th>
              );
            })}
          </tr>
        </thead>
        <tbody>
          {sorted.length === 0 ? (
            <tr>
              <td colSpan={columns.length} style={{ color: "var(--text-muted)", textAlign: "center" }}>
                {emptyMessage}
              </td>
            </tr>
          ) : (
            sorted.map((row) => (
              <tr key={rowKey(row)}>
                {columns.map((col) => (
                  <td
                    key={col.key}
                    style={col.align ? { textAlign: col.align } : undefined}
                  >
                    {col.render(row)}
                  </td>
                ))}
              </tr>
            ))
          )}
        </tbody>
      </table>
    </div>
  );
}

/** Low-level table primitives for custom layouts */
export function TableRoot({ className, children, ...rest }: HTMLAttributes<HTMLTableElement>) {
  return (
    <div className="ds-table-wrap">
      <table className={cx("ds-table", className)} {...rest}>
        {children}
      </table>
    </div>
  );
}

export function Th({
  sortable,
  sorted,
  onSort,
  children,
  className,
  ...rest
}: ThHTMLAttributes<HTMLTableCellElement> & {
  sortable?: boolean;
  sorted?: SortDir;
  onSort?: () => void;
}) {
  if (!sortable) {
    return (
      <th className={className} {...rest}>
        {children}
      </th>
    );
  }
  const ariaSort =
    sorted === "asc" ? "ascending" : sorted === "desc" ? "descending" : "none";
  return (
    <th className={className} aria-sort={ariaSort} {...rest}>
      <button type="button" className="ds-table__sort" onClick={onSort}>
        {children}
        <span className="ds-table__sort-icon" aria-hidden="true">
          <Icon name={sorted === "asc" ? "sortAsc" : sorted === "desc" ? "sortDesc" : "sortNone"} size={12} />
        </span>
      </button>
    </th>
  );
}
