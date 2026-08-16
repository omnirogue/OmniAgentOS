import type { ReactNode, SVGAttributes } from "react";

/**
 * OmniAgentOS icon set — inline SVG only (no icon font, no npm dependency). One
 * consistent hairline stroke language (currentColor, round joins, optically sized to
 * the type) across nav, tables, dialogs, theme toggle, and the vault/chat glyph marks —
 * replacing the prior grab-bag of Unicode/emoji characters that rendered with
 * mismatched weight, size and baseline across platforms.
 */
export type IconName =
  | "menu"
  | "close"
  | "search"
  | "chevronDown"
  | "chevronRight"
  | "sortAsc"
  | "sortDesc"
  | "sortNone"
  | "sun"
  | "moon"
  | "checkCircle"
  | "alertTriangle"
  | "plus"
  | "minus"
  | "scan"
  | "externalLink"
  | "hash"
  | "swap"
  | "radio"
  | "inbox"
  | "helpCircle"
  | "sparkles"
  | "plug"
  | "grid"
  | "flask"
  | "bracket"
  | "barChart"
  | "database"
  | "columns"
  | "message"
  | "users"
  | "palette"
  | "clock"
  | "mic"
  | "link";

export type IconProps = Omit<SVGAttributes<SVGSVGElement>, "name"> & {
  name: IconName;
  size?: number;
};

const PATHS: Record<IconName, ReactNode> = {
  menu: (
    <>
      <line x1="3.5" y1="6" x2="20.5" y2="6" />
      <line x1="3.5" y1="12" x2="20.5" y2="12" />
      <line x1="3.5" y1="18" x2="20.5" y2="18" />
    </>
  ),
  close: (
    <>
      <line x1="6" y1="6" x2="18" y2="18" />
      <line x1="18" y1="6" x2="6" y2="18" />
    </>
  ),
  search: (
    <>
      <circle cx="10.8" cy="10.8" r="6.8" />
      <line x1="20.5" y1="20.5" x2="15.8" y2="15.8" />
    </>
  ),
  chevronDown: <polyline points="6 9.5 12 15.5 18 9.5" />,
  chevronRight: <polyline points="9.5 6 15.5 12 9.5 18" />,
  sortAsc: (
    <>
      <line x1="12" y1="19" x2="12" y2="6" />
      <polyline points="6.5 11.5 12 6 17.5 11.5" />
    </>
  ),
  sortDesc: (
    <>
      <line x1="12" y1="5" x2="12" y2="18" />
      <polyline points="6.5 12.5 12 18 17.5 12.5" />
    </>
  ),
  sortNone: (
    <>
      <polyline points="7.5 10 12 5.5 16.5 10" />
      <polyline points="7.5 14 12 18.5 16.5 14" />
    </>
  ),
  sun: (
    <>
      <circle cx="12" cy="12" r="4" />
      <line x1="12" y1="2.5" x2="12" y2="4.8" />
      <line x1="12" y1="19.2" x2="12" y2="21.5" />
      <line x1="4.4" y1="4.4" x2="6.1" y2="6.1" />
      <line x1="17.9" y1="17.9" x2="19.6" y2="19.6" />
      <line x1="2.5" y1="12" x2="4.8" y2="12" />
      <line x1="19.2" y1="12" x2="21.5" y2="12" />
      <line x1="4.4" y1="19.6" x2="6.1" y2="17.9" />
      <line x1="17.9" y1="6.1" x2="19.6" y2="4.4" />
    </>
  ),
  moon: <path d="M19 13.5A7.5 7.5 0 1 1 10.5 5a6 6 0 0 0 8.5 8.5Z" />,
  checkCircle: (
    <>
      <circle cx="12" cy="12" r="8.5" />
      <polyline points="8 12.3 10.7 15 16 9.3" />
    </>
  ),
  alertTriangle: (
    <>
      <polygon points="12 3.3 21.3 19.8 2.7 19.8" />
      <line x1="12" y1="9.3" x2="12" y2="14.3" />
      <circle cx="12" cy="17" r="0.9" fill="currentColor" stroke="none" />
    </>
  ),
  plus: (
    <>
      <line x1="12" y1="5.5" x2="12" y2="18.5" />
      <line x1="5.5" y1="12" x2="18.5" y2="12" />
    </>
  ),
  minus: <line x1="5.5" y1="12" x2="18.5" y2="12" />,
  scan: (
    <>
      <polyline points="4 9 4 4 9 4" />
      <polyline points="15 4 20 4 20 9" />
      <polyline points="20 15 20 20 15 20" />
      <polyline points="9 20 4 20 4 15" />
    </>
  ),
  externalLink: (
    <>
      <line x1="7" y1="17" x2="17" y2="7" />
      <polyline points="9 7 17 7 17 15" />
    </>
  ),
  hash: (
    <>
      <line x1="9.2" y1="4" x2="7.2" y2="20" />
      <line x1="16.8" y1="4" x2="14.8" y2="20" />
      <line x1="4.5" y1="9" x2="20.5" y2="9" />
      <line x1="3.5" y1="15" x2="19.5" y2="15" />
    </>
  ),
  swap: (
    <>
      <line x1="3.5" y1="8" x2="17.5" y2="8" />
      <polyline points="14 4.3 17.7 8 14 11.7" />
      <line x1="20.5" y1="16" x2="6.5" y2="16" />
      <polyline points="10 12.3 6.3 16 10 19.7" />
    </>
  ),
  radio: (
    <>
      <circle cx="12" cy="12" r="8.3" />
      <circle cx="12" cy="12" r="3" fill="currentColor" stroke="none" />
    </>
  ),
  inbox: (
    <>
      <rect x="4" y="9.5" width="16" height="10" rx="1.5" />
      <polyline points="4 14 8.2 14 9.4 16.2 14.6 16.2 15.8 14 20 14" />
      <polyline points="8 9.5 9 5 15 5 16 9.5" />
    </>
  ),
  helpCircle: (
    <>
      <circle cx="12" cy="12" r="8.5" />
      <path d="M9.6 9.8a2.4 2.4 0 1 1 3.6 2.1c-.7.45-1.2.9-1.2 1.85" />
      <circle cx="12" cy="17.1" r="0.9" fill="currentColor" stroke="none" />
    </>
  ),
  sparkles: (
    <>
      <line x1="12" y1="3" x2="12" y2="7.5" />
      <line x1="12" y1="16.5" x2="12" y2="21" />
      <line x1="3" y1="12" x2="7.5" y2="12" />
      <line x1="16.5" y1="12" x2="21" y2="12" />
      <line x1="6" y1="6" x2="9" y2="9" />
      <line x1="15" y1="15" x2="18" y2="18" />
      <line x1="18" y1="6" x2="15" y2="9" />
      <line x1="9" y1="15" x2="6" y2="18" />
    </>
  ),
  plug: (
    <>
      <line x1="9" y1="3" x2="9" y2="8.2" />
      <line x1="15" y1="3" x2="15" y2="8.2" />
      <path d="M6.8 8.2h10.4l-.9 5.6a4.3 4.3 0 0 1-4.3 3.6a4.3 4.3 0 0 1-4.3-3.6L6.8 8.2Z" />
      <line x1="12" y1="17.4" x2="12" y2="21" />
    </>
  ),
  link: (
    <>
      <path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71" />
      <path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71" />
    </>
  ),
  grid: (
    <>
      <rect x="3.5" y="3.5" width="7.5" height="7.5" rx="1.2" />
      <rect x="13" y="3.5" width="7.5" height="7.5" rx="1.2" />
      <rect x="3.5" y="13" width="7.5" height="7.5" rx="1.2" />
      <rect x="13" y="13" width="7.5" height="7.5" rx="1.2" />
    </>
  ),
  flask: (
    <>
      <line x1="9.3" y1="3.5" x2="14.7" y2="3.5" />
      <line x1="10.1" y1="3.5" x2="10.1" y2="9.3" />
      <line x1="13.9" y1="3.5" x2="13.9" y2="9.3" />
      <path d="M10.1 9.3 5.2 17.6a1.8 1.8 0 0 0 1.6 2.7h10.4a1.8 1.8 0 0 0 1.6-2.7L13.9 9.3" />
      <line x1="7.6" y1="15" x2="16.4" y2="15" />
    </>
  ),
  bracket: (
    <>
      <circle cx="6" cy="6" r="2.1" />
      <circle cx="6" cy="18" r="2.1" />
      <circle cx="18" cy="12" r="2.1" />
      <path d="M8.1 6H12a4 4 0 0 1 4 4" />
      <path d="M8.1 18H12a4 4 0 0 0 4-4" />
    </>
  ),
  barChart: (
    <>
      <line x1="4" y1="20" x2="20" y2="20" />
      <line x1="7.2" y1="20" x2="7.2" y2="13.5" />
      <line x1="12" y1="20" x2="12" y2="8.5" />
      <line x1="16.8" y1="20" x2="16.8" y2="5" />
    </>
  ),
  database: (
    <>
      <ellipse cx="12" cy="5.8" rx="7.5" ry="2.5" />
      <path d="M4.5 5.8v12.4c0 1.4 3.4 2.5 7.5 2.5s7.5-1.1 7.5-2.5V5.8" />
      <path d="M4.5 12c0 1.4 3.4 2.5 7.5 2.5s7.5-1.1 7.5-2.5" />
    </>
  ),
  columns: (
    <>
      <rect x="3.5" y="4.5" width="5" height="15" rx="1.3" />
      <rect x="9.5" y="4.5" width="5" height="10" rx="1.3" />
      <rect x="15.5" y="4.5" width="5" height="15" rx="1.3" />
    </>
  ),
  message: (
    <>
      <rect x="3.5" y="4.5" width="17" height="12" rx="2.5" />
      <polygon points="8 16.5 8 20.3 11.8 16.5" />
    </>
  ),
  users: (
    <>
      <circle cx="8.2" cy="8" r="3" />
      <path d="M2.5 19.5a5.7 5.7 0 0 1 11.4 0" />
      <circle cx="17" cy="9.3" r="2.3" />
      <path d="M13.6 19.5a4.4 4.4 0 0 1 8.8 0" />
    </>
  ),
  palette: (
    <>
      <circle cx="12" cy="12" r="8.5" />
      <circle cx="8.3" cy="10.2" r="1.1" fill="currentColor" stroke="none" />
      <circle cx="11" cy="7.3" r="1.1" fill="currentColor" stroke="none" />
      <circle cx="15.2" cy="8.3" r="1.1" fill="currentColor" stroke="none" />
      <circle cx="16.3" cy="12.5" r="1.1" fill="currentColor" stroke="none" />
    </>
  ),
  clock: (
    <>
      <circle cx="12" cy="12" r="8.5" />
      <polyline points="12 7.5 12 12 15.5 14" />
    </>
  ),
  mic: (
    <>
      <rect x="9" y="3" width="6" height="11" rx="3" />
      <path d="M6 11a6 6 0 0 0 12 0" />
      <line x1="12" y1="17" x2="12" y2="21" />
      <line x1="8.5" y1="21" x2="15.5" y2="21" />
    </>
  ),
};

export function Icon({ name, size = 16, strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={strokeWidth}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      {...rest}
    >
      {PATHS[name]}
    </svg>
  );
}
