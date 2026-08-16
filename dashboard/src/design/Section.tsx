import type { HTMLAttributes, ReactNode } from "react";
import { cx } from "./utils";

export type SectionProps = HTMLAttributes<HTMLElement> & {
  eyebrow?: ReactNode;
  title?: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
};

/**
 * A titled content group within a Page. Renders a <section> — never <main> — so nesting
 * it inside AppShell's single <main> landmark stays WCAG-clean. Gives every sub-heading
 * (below the page's h1) one shared eyebrow/h2/description/actions treatment.
 */
export function Section({
  eyebrow,
  title,
  description,
  actions,
  children,
  className,
  ...rest
}: SectionProps) {
  const hasHead = Boolean(eyebrow || title || actions);
  return (
    <section className={cx("ds-section", className)} {...rest}>
      {hasHead ? (
        <div className="ds-section__head">
          <div className="ds-section__text">
            {eyebrow ? <p className="ds-section__eyebrow">{eyebrow}</p> : null}
            {title ? <h2 className="ds-section__title">{title}</h2> : null}
            {description ? <p className="ds-section__description">{description}</p> : null}
          </div>
          {actions ? <div className="ds-section__actions">{actions}</div> : null}
        </div>
      ) : null}
      {children}
    </section>
  );
}
