"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useState, type ReactNode } from "react";
import { useAlertCount } from "../features/steward/hooks";
import stewardStyles from "../features/steward/steward.module.css";
import { NotificationsBell } from "../features/notifications";
import { ProjectSwitcher } from "../features/projects";
import { BottomNav } from "./BottomNav";
import { CmdKButton, CommandPaletteHost } from "./CommandPalette";
import { Icon, type IconName } from "./Icon";
import { ThemeToggle } from "./ThemeToggle";
import { cx } from "./utils";
import { useEscalationCount } from "../lib/useEscalationCount";

type NavLink = { href: string; label: string; icon: IconName };

/**
 * Flat primary navigation — the new 10-page IA (P0 shell rebuild, 2026-08-14).
 * Every surface the operator works from is one click away; nothing is nested
 * behind an accordion anymore because there is nothing left to hide — the
 * dormant/duplicate pages (Today, Chat, Swarm, Lab, Knowledge, Vault, Memory,
 * Pulse, Projects, Goals, Comms, Revenue, Reliability, System, Connections,
 * and the rest of the old Observatory/System accordions) were removed
 * outright rather than demoted.
 *
 * A handful of routes still exist but sit outside this list on purpose —
 * Accounts, Alerts, Briefing and the Design system page are reached from a
 * deep link (topbar bell, drill-downs, the footer link, the command
 * palette) rather than a permanent nav slot. See IMPLICIT_SECTIONS below for
 * how those keep a sane breadcrumb.
 */
const NAV_LINKS: NavLink[] = [
  { href: "/", label: "Status", icon: "barChart" },
  { href: "/companies", label: "Companies", icon: "grid" },
  { href: "/board", label: "Board", icon: "columns" },
  { href: "/team", label: "Team", icon: "grid" },
  { href: "/inbox", label: "Inbox", icon: "inbox" },
  { href: "/sessions", label: "Sessions", icon: "columns" },
  { href: "/loops", label: "Loops", icon: "clock" },
  { href: "/cash", label: "Cash", icon: "swap" },
  { href: "/skills", label: "Skills", icon: "hash" },
  { href: "/repos", label: "Repos", icon: "bracket" },
  { href: "/tests", label: "Tests", icon: "checkCircle" },
  { href: "/testing", label: "Testing", icon: "checkCircle" },
  { href: "/files", label: "Files", icon: "database" },
  { href: "/compute", label: "Compute", icon: "bracket" },
];

/** Routes that exist but have no slot in NAV_LINKS (no index page of their own
 * beyond a deep-linked one, or reached from elsewhere in the shell — the
 * topbar bell for Alerts, the footer link for Design). Attributed here so the
 * breadcrumb still names something sane instead of rendering nothing. The
 * legacy /approvals redirect stub maps to Inbox so a stale bookmark's
 * breadcrumb still makes sense during the instant before the redirect fires. */
const IMPLICIT_SECTIONS: Array<{ prefix: string; label: string; linkLabel: string }> = [
  { prefix: "/accounts", label: "Accounts", linkLabel: "Accounts" },
  { prefix: "/activity", label: "Board", linkLabel: "Activity" },
  { prefix: "/alerts", label: "Alerts", linkLabel: "Alerts" },
  { prefix: "/briefing", label: "Briefing", linkLabel: "Briefing" },
  { prefix: "/design", label: "Design system", linkLabel: "Design system" },
  { prefix: "/approvals", label: "Inbox", linkLabel: "Inbox" },
];

function isActive(pathname: string, href: string): boolean {
  if (href === "/") return pathname === "/";
  return pathname === href || pathname.startsWith(`${href}/`);
}

/** {sectionLabel, linkLabel} for the current route — the longest/most specific
 * href match wins. Powers the breadcrumb row under the topbar. */
function breadcrumbFor(pathname: string): { sectionLabel: string; linkLabel: string | null } | null {
  let best: { sectionLabel: string; linkLabel: string | null; hrefLen: number } | null = null;
  for (const link of NAV_LINKS) {
    if (isActive(pathname, link.href)) {
      const len = link.href.length;
      if (!best || len > best.hrefLen) best = { sectionLabel: link.label, linkLabel: null, hrefLen: len };
    }
  }
  if (best) return { sectionLabel: best.sectionLabel, linkLabel: best.linkLabel };
  const implicit = IMPLICIT_SECTIONS.find((im) => pathname === im.prefix || pathname.startsWith(`${im.prefix}/`));
  if (implicit) return { sectionLabel: implicit.label, linkLabel: implicit.linkLabel };
  return null;
}

export type AppShellProps = {
  children: ReactNode;
  /** Single topbar system indicator (run state + API health + freshness). */
  statusSlot?: ReactNode;
  /** @deprecated Superseded by `statusSlot`; still rendered if supplied. */
  pauseSlot?: ReactNode;
  /** @deprecated Superseded by `statusSlot`; still rendered if supplied. */
  healthSlot?: ReactNode;
};

export function AppShell({ children, statusSlot, pauseSlot, healthSlot }: AppShellProps) {
  const pathname = usePathname() ?? "/";
  const [navOpen, setNavOpen] = useState(false);
  const { count: openAlertCount } = useAlertCount();
  const { count: escalationCount } = useEscalationCount();
  const crumb = breadcrumbFor(pathname);

  const renderLink = (link: NavLink) => {
    const active = isActive(pathname, link.href);
    return (
      <Link
        key={link.href}
        href={link.href}
        className={cx("ds-sidebar__link", active && "ds-sidebar__link--active")}
        aria-current={active ? "page" : undefined}
        onClick={() => setNavOpen(false)}
      >
        <Icon name={link.icon} size={16} />
        <span>
          {link.label}
          {link.href === "/" && escalationCount > 0 ? (
            <span style={{ marginLeft: "0.5rem", fontSize: "0.75rem", color: "#ef4444", fontWeight: "bold" }}>
              ({escalationCount})
            </span>
          ) : null}
        </span>
      </Link>
    );
  };

  return (
    <div className="ds-shell">
      {navOpen ? (
        <button
          type="button"
          className="ds-sidebar-backdrop"
          aria-label="Close navigation"
          onClick={() => setNavOpen(false)}
        />
      ) : null}

      <aside
        className={cx("ds-sidebar", navOpen && "ds-sidebar--open")}
        aria-label="Primary"
      >
        <Link href="/" className="ds-sidebar__brand" onClick={() => setNavOpen(false)}>
          <span className="ds-sidebar__brand-mark" aria-hidden="true" />
          <span>OmniAgentOS</span>
        </Link>

        <nav className="ds-sidebar__nav">{NAV_LINKS.map(renderLink)}</nav>

        <div className="ds-sidebar__footer">
          <Link
            href="/design"
            className={cx(
              "ds-sidebar__link",
              "ds-sidebar__footer-link",
              isActive(pathname, "/design") && "ds-sidebar__link--active",
            )}
            aria-current={isActive(pathname, "/design") ? "page" : undefined}
            onClick={() => setNavOpen(false)}
          >
            Design system
          </Link>
        </div>
      </aside>

      <div className="ds-main-col">
        <header className="ds-topbar">
          <button
            type="button"
            className={cx("ds-icon-btn", "ds-menu-btn")}
            aria-label="Open navigation"
            aria-expanded={navOpen}
            onClick={() => setNavOpen(true)}
          >
            <Icon name="menu" size={18} />
          </button>

          <div className="ds-topbar__slots">
            <ProjectSwitcher />
            {statusSlot}
            {pauseSlot}
            {healthSlot}
          </div>

          <div className="ds-topbar__actions">
            <Link
              href="/alerts"
              className={cx("ds-icon-btn", stewardStyles.bellButton)}
              aria-label={openAlertCount > 0 ? `${openAlertCount} open alerts` : "Alerts"}
            >
              <Icon name="alertTriangle" size={16} />
              {openAlertCount > 0 ? (
                <span className={stewardStyles.bellCount} aria-hidden="true">
                  {openAlertCount > 99 ? "99+" : openAlertCount}
                </span>
              ) : null}
            </Link>
            <NotificationsBell />
            <ThemeToggle />
            <CmdKButton />
          </div>
        </header>

        {crumb ? (
          <div aria-label="Breadcrumb" className="ds-breadcrumb">
            <span>{crumb.sectionLabel}</span>
            {crumb.linkLabel ? (
              <>
                <Icon name="chevronRight" size={11} />
                <span className="ds-breadcrumb__leaf">{crumb.linkLabel}</span>
              </>
            ) : null}
          </div>
        ) : null}

        <main className="ds-content">{children}</main>
      </div>

      {/* Phone-only (<768px). "More" drives the SAME drawer state as the hamburger,
          so the existing sidebar drawer + backdrop keep working untouched. The Inbox
          badge reuses the escalation count the shell already fetched (sessions parked
          for approval + pending loop approvals — literally what /inbox lists), so
          the bar opens no new requests and never double-counts the alerts bell. */}
      <BottomNav
        pathname={pathname}
        onMore={() => setNavOpen(true)}
        moreOpen={navOpen}
        counts={{ inbox: escalationCount }}
      />

      <CommandPaletteHost />
    </div>
  );
}
