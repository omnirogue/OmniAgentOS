"use client";

import Link from "next/link";
import type { IconName } from "./Icon";
import { Icon } from "./Icon";
import { cx } from "./utils";

/**
 * Phone bottom navigation (<768px) — the four surfaces a day is actually run from,
 * plus an escape hatch into the full rail.
 *
 * It is a peer of the sidebar, not a replacement: "More" opens the SAME drawer the
 * hamburger opens, so everything the rail reaches stays one tap away. Above 768px the
 * whole bar is display:none and the desktop shell is untouched.
 */

export type BottomNavKey = "status" | "board" | "inbox" | "sessions";

type BottomNavItem = { key: BottomNavKey; href: string; label: string; icon: IconName };

/** Icons deliberately mirror the sidebar's, so a route looks the same on both.
 * The four daily-use surfaces from the new 10-page nav (P0 shell rebuild,
 * 2026-08-14) — Companies/Cash/Skills/Repos/Tests/Files stay one tap away via
 * "More", which opens the same drawer the sidebar hamburger opens. */
export const BOTTOM_NAV_ITEMS: readonly BottomNavItem[] = [
  { key: "status", href: "/", label: "Status", icon: "barChart" },
  { key: "board", href: "/board", label: "Board", icon: "columns" },
  { key: "inbox", href: "/inbox", label: "Inbox", icon: "inbox" },
  { key: "sessions", href: "/sessions", label: "Sessions", icon: "columns" },
] as const;

/**
 * Extra routes that belong to a tab but do not live under its href. These mirror the
 * sidebar's IMPLICIT_SECTIONS attributions verbatim (narrowed to these four tabs) so a
 * route is never claimed by one nav and disowned by the other — including the legacy
 * /approvals redirect stub, so the Inbox tab does not blink off mid-redirect.
 */
const ALIASES: Record<BottomNavKey, readonly string[]> = {
  status: [],
  board: ["/activity"],
  inbox: ["/approvals", "/alerts", "/briefing"],
  sessions: [],
};

function matches(pathname: string, base: string): boolean {
  return pathname === base || pathname.startsWith(`${base}/`);
}

/**
 * Which tab a route lights up, or null when the route lives outside the four tabs
 * (deep Observatory/System pages) — in that case NO tab is highlighted rather than a
 * misleading one, and the route is reachable through More.
 *
 * The most specific match wins, so a longer alias can never lose to a shorter href.
 */
export function bottomNavActiveKey(pathname: string | null | undefined): BottomNavKey | null {
  if (!pathname) return null;
  // Normalise a trailing slash so "/board/" behaves like "/board".
  const path = pathname.length > 1 && pathname.endsWith("/") ? pathname.slice(0, -1) : pathname;
  let bestKey: BottomNavKey | null = null;
  let bestLen = -1;
  for (const item of BOTTOM_NAV_ITEMS) {
    for (const base of [item.href, ...ALIASES[item.key]]) {
      if (matches(path, base) && base.length > bestLen) {
        bestKey = item.key;
        bestLen = base.length;
      }
    }
  }
  return bestKey;
}

export type BottomNavProps = {
  pathname: string;
  /** Opens the existing sidebar drawer (the same state the hamburger toggles). */
  onMore: () => void;
  /** True while that drawer is open — drives aria-expanded on More. */
  moreOpen?: boolean;
  /** Optional per-tab counts (already-fetched by the shell; no extra requests). */
  counts?: Partial<Record<BottomNavKey, number>>;
};

export function BottomNav({ pathname, onMore, moreOpen = false, counts }: BottomNavProps) {
  const activeKey = bottomNavActiveKey(pathname);

  return (
    <nav className="ds-bottom-nav" aria-label="Primary (compact)">
      {BOTTOM_NAV_ITEMS.map((item) => {
        const active = item.key === activeKey;
        const count = counts?.[item.key] ?? 0;
        return (
          <Link
            key={item.key}
            href={item.href}
            className={cx("ds-bottom-nav__item", active && "ds-bottom-nav__item--active")}
            aria-current={active ? "page" : undefined}
          >
            <span className="ds-bottom-nav__icon">
              <Icon name={item.icon} size={20} />
              {count > 0 ? (
                <span className="ds-bottom-nav__count" aria-hidden="true">
                  {count > 99 ? "99+" : count}
                </span>
              ) : null}
            </span>
            <span className="ds-bottom-nav__label">{item.label}</span>
            {count > 0 ? <span className="sr-only">{`${count} waiting`}</span> : null}
          </Link>
        );
      })}

      <button
        type="button"
        className="ds-bottom-nav__item"
        onClick={onMore}
        aria-expanded={moreOpen}
        aria-label="More — open navigation"
      >
        <span className="ds-bottom-nav__icon">
          <Icon name="menu" size={20} />
        </span>
        <span className="ds-bottom-nav__label">More</span>
      </button>
    </nav>
  );
}
