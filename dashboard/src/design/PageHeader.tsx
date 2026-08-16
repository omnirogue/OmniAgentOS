import type { HTMLAttributes, ReactNode } from "react";
import { cx } from "./utils";

export type PageHeaderProps = Omit<HTMLAttributes<HTMLElement>, "title"> & {
  /** Rendered above the eyebrow/title block — breadcrumbs, arm/status badge rows. */
  before?: ReactNode;
  /** Small-caps label above the title. One spec app-wide: accent, 0.1em tracking. */
  eyebrow?: ReactNode;
  title: ReactNode;
  /** Rendered inline immediately after the title (e.g. a run-state badge). */
  titleAdornment?: ReactNode;
  /** Supporting paragraph under the title. One spec app-wide: muted, 46rem max-width. */
  lead?: ReactNode;
  /** Small caption row under the lead (mono ids, timestamps). */
  meta?: ReactNode;
  /** Right-aligned actions (buttons, connection/status badges). */
  actions?: ReactNode;
};

/**
 * THE page header — eyebrow + title + lead + actions, one spec used by every route so
 * the app reads as one product instead of five (design review HD-014 follow-up). Title
 * is always --text-h1: list pages, detail/record pages (run, experiment) and the /design
 * reference page all share the exact same size, weight and tracking. Contextual chrome
 * (breadcrumbs, status badges, mono ids) composes via the `before`/`titleAdornment`/
 * `meta` slots instead of ad-hoc per-page markup.
 */
export function PageHeader({
  before,
  eyebrow,
  title,
  titleAdornment,
  lead,
  meta,
  actions,
  className,
  ...rest
}: PageHeaderProps) {
  return (
    <header className={cx("ds-page-header", className)} {...rest}>
      <div className="ds-page-header__text">
        {before ? <div className="ds-page-header__before">{before}</div> : null}
        {eyebrow ? <p className="ds-page-header__eyebrow">{eyebrow}</p> : null}
        <div className="ds-page-header__title-row">
          <h1 className="ds-page-header__title">{title}</h1>
          {titleAdornment}
        </div>
        {lead ? <p className="ds-page-header__lead">{lead}</p> : null}
        {meta ? <div className="ds-page-header__meta">{meta}</div> : null}
      </div>
      {actions ? <div className="ds-page-header__actions">{actions}</div> : null}
    </header>
  );
}
