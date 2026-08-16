import type { HTMLAttributes, ReactNode } from "react";
import { cx } from "./utils";

export type PageProps = HTMLAttributes<HTMLDivElement> & { children: ReactNode };

/**
 * Root wrapper for every route's content. Renders a plain <div> — never <main> —
 * because AppShell.tsx already renders the page's single <main className="ds-content">
 * landmark (WCAG 1.3.1 forbids nested landmarks of the same type). Provides the ONE vertical
 * rhythm between a page's top-level sections; width/max-width/padding come from
 * AppShell's .ds-content, so Page must never re-add its own padding or max-width.
 */
export function Page({ children, className, ...rest }: PageProps) {
  return (
    <div className={cx("ds-page", className)} {...rest}>
      {children}
    </div>
  );
}
