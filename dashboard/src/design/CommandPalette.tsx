"use client";

import {
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";
import { useRouter } from "next/navigation";
import { Icon } from "./Icon";
import { cx } from "./utils";

export type CommandItem = {
  id: string;
  label: string;
  group?: string;
  keywords?: string;
  href?: string;
  onSelect?: () => void;
  icon?: ReactNode;
};

// Route table — every page in the 10-page IA (P0 shell rebuild, 2026-08-14) is
// reachable via Cmd+K, plus the handful of routes that still exist but have no
// permanent sidebar slot (Accounts, Alerts, Briefing, Design system): the
// palette is the guarantee that a route with no nav entry never becomes
// unreachable.
//
// Groups are functional, not historical: Primary mirrors the sidebar exactly
// (same order, same 10 entries), Other is everything else still standing.
// Every dormant page removed in the prune (Today, Chat, Swarm, Lab,
// Knowledge, Vault, Memory, Memlife, Pulse, Graph, Improvements, Suggestions,
// Updates, Projects, Goals, Comms, Revenue, Reliability, System, Connections,
// Capabilities, Dimensions, Grok ops, Agents, Runs, Artifacts, Team,
// Organization, Control plane, Executions) has no entry here — the palette
// must never dangle a link the sidebar already dropped.
export const DEFAULT_COMMANDS: CommandItem[] = [
  { id: "status", label: "Status", group: "Primary", href: "/", keywords: "dashboard home cockpit overview loops queue", icon: <Icon name="barChart" size={15} /> },
  { id: "companies", label: "Companies", group: "Primary", href: "/companies", keywords: "brands acmeuni globex initech hooli revenue goals", icon: <Icon name="grid" size={15} /> },
  { id: "board", label: "Board", group: "Primary", href: "/board", keywords: "kanban tasks columns project", icon: <Icon name="columns" size={15} /> },
  { id: "inbox", label: "Inbox", group: "Primary", href: "/inbox", keywords: "approvals decisions human review governance queue", icon: <Icon name="inbox" size={15} /> },
  { id: "team", label: "Team", group: "Primary", href: "/team", keywords: "developers accountability workqueue commitments scoreboard", icon: <Icon name="users" size={15} /> },
  { id: "sessions", label: "Sessions", group: "Primary", href: "/sessions", keywords: "terminals live agent sessions", icon: <Icon name="columns" size={15} /> },
  { id: "cash", label: "Cash", group: "Primary", href: "/cash", keywords: "liquidity flow burn banking revenue", icon: <Icon name="swap" size={15} /> },
  { id: "skills", label: "Skills", group: "Primary", href: "/skills", keywords: "library tree versions proposals", icon: <Icon name="hash" size={15} /> },
  { id: "repos", label: "Repos", group: "Primary", href: "/repos", keywords: "github clones dirty origin", icon: <Icon name="bracket" size={15} /> },
  { id: "tests", label: "Tests", group: "Primary", href: "/tests", keywords: "gate ci landings suite", icon: <Icon name="checkCircle" size={15} /> },
  { id: "testing", label: "Testing", group: "Primary", href: "/testing", keywords: "tests testing north star memcert diagnostics coverage analytics", icon: <Icon name="checkCircle" size={15} /> },
  { id: "files", label: "Files", group: "Primary", href: "/files", keywords: "machine artifacts storage filesearch", icon: <Icon name="database" size={15} /> },
  { id: "compute", label: "Compute", group: "Primary", href: "/compute", keywords: "estate machines pool runners cpu load capacity fleet", icon: <Icon name="bracket" size={15} /> },
  { id: "accounts", label: "Accounts", group: "Other", href: "/accounts", keywords: "provider credentials", icon: <Icon name="users" size={15} /> },
  { id: "alerts", label: "Alerts", group: "Other", href: "/alerts", keywords: "notifications warnings steward", icon: <Icon name="alertTriangle" size={15} /> },
  { id: "briefing", label: "Briefing", group: "Other", href: "/briefing", keywords: "daily digest summary", icon: <Icon name="inbox" size={15} /> },
  { id: "design", label: "Design system", group: "Other", href: "/design", keywords: "primitives showcase tokens", icon: <Icon name="palette" size={15} /> },
];

/** Scoped commands available after `#project` selection. */
export const SCOPED_COMMANDS: CommandItem[] = [
  { id: "scope-open", label: "Open project", group: "In project", keywords: "detail", icon: <Icon name="externalLink" size={15} /> },
  { id: "scope-board", label: "Board (filtered)", group: "In project", href: "/board", keywords: "kanban columns", icon: <Icon name="columns" size={15} /> },
  { id: "scope-inbox", label: "Inbox", group: "In project", href: "/inbox", keywords: "approvals human review", icon: <Icon name="inbox" size={15} /> },
];

export type CommandPaletteProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  commands?: CommandItem[];
  /** Projects for `#` fuzzy scope (Phase C). */
  projects?: CommandItem[];
  /** Commands offered when query starts with `/` (optionally project-scoped). */
  slashCommands?: CommandItem[];
  placeholder?: string;
};

const FOCUSABLE =
  'a[href],button:not([disabled]),textarea:not([disabled]),input:not([disabled]),select:not([disabled]),[tabindex]:not([tabindex="-1"])';

function matchCommand(item: CommandItem, q: string): boolean {
  if (!q) return true;
  const hay = `${item.label} ${item.group ?? ""} ${item.keywords ?? ""}`.toLowerCase();
  return q.split(/\s+/).every((token) => hay.includes(token));
}

export function CommandPalette({
  open,
  onOpenChange,
  commands = DEFAULT_COMMANDS,
  projects = [],
  slashCommands = SCOPED_COMMANDS,
  placeholder = "Search…  # project  / command",
}: CommandPaletteProps) {
  const router = useRouter();
  const inputRef = useRef<HTMLInputElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const previousFocus = useRef<HTMLElement | null>(null);
  const listId = useId();
  const [query, setQuery] = useState("");
  const [highlight, setHighlight] = useState(0);
  /** Project scoped via `#…` selection — used when typing `/` next. */
  const [scope, setScope] = useState<CommandItem | null>(null);

  const filtered = useMemo(() => {
    const raw = query.trim();
    const lower = raw.toLowerCase();

    // `#` — fuzzy project scope over name/path keywords.
    if (raw.startsWith("#")) {
      const q = lower.slice(1).trim();
      const matched = projects.filter((p) => matchCommand(p, q));
      return matched.length > 0
        ? matched
        : projects.length === 0
          ? [
              {
                id: "no-projects",
                label: "No projects loaded",
                group: "Projects",
                keywords: "",
              },
            ]
          : [
              {
                id: "no-match",
                label: "No matching projects",
                group: "Projects",
                keywords: "",
              },
            ];
    }

    // `/` — commands in the current scope (or global slash set).
    if (raw.startsWith("/")) {
      const q = lower.slice(1).trim();
      const pool = scope
        ? slashCommands.map((c) =>
            c.id === "scope-open" && scope.href
              ? { ...c, href: scope.href, label: `Open ${scope.label}` }
              : c,
          )
        : commands;
      return pool.filter((c) => matchCommand(c, q));
    }

    if (!lower) {
      // Idle palette: navigate commands + a hint row for prefixes.
      const hints: CommandItem[] = [
        {
          id: "hint-hash",
          label: "# Jump to a project",
          group: "Hints",
          keywords: "scope project",
          onSelect: () => {
            setQuery("#");
            requestAnimationFrame(() => inputRef.current?.focus());
          },
        },
        {
          id: "hint-slash",
          label: scope
            ? `/ Command in ${scope.label}`
            : "/ Run a command",
          group: "Hints",
          keywords: "command slash",
          onSelect: () => {
            setQuery("/");
            requestAnimationFrame(() => inputRef.current?.focus());
          },
        },
      ];
      return [...hints, ...commands];
    }

    // Free text: commands first, then projects.
    const cmdHits = commands.filter((c) => matchCommand(c, lower));
    const projHits = projects.filter((p) => matchCommand(p, lower));
    return [...cmdHits, ...projHits];
  }, [commands, projects, query, scope, slashCommands]);

  // Store the trigger element on open and restore focus to it on close — mirrors
  // Dialog.tsx's pattern so closing the palette (Escape, backdrop, selecting a command)
  // never strands focus at the top of the page.
  useEffect(() => {
    if (!open) return;
    previousFocus.current = document.activeElement as HTMLElement | null;
    setQuery("");
    setHighlight(0);
    // Keep scope across open sessions within the page; clear only when closed fully? Spec: scope sticks while palette is used.
    requestAnimationFrame(() => inputRef.current?.focus());
    return () => {
      previousFocus.current?.focus?.();
    };
  }, [open]);

  useEffect(() => {
    setHighlight(0);
  }, [query]);

  useEffect(() => {
    if (!open) return;
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = prev;
    };
  }, [open]);

  const run = useCallback(
    (item: CommandItem) => {
      // Hint rows only rewrite the query — keep palette open.
      if (item.id === "hint-hash" || item.id === "hint-slash") {
        item.onSelect?.();
        return;
      }
      // `#` project pick: set scope for the `/` slash commands below (Board
      // filtered, Inbox). There is no per-project detail page anymore (the
      // /projects route was retired in the P0 prune), so picking a project
      // only sets scope — it does not navigate anywhere on its own.
      if (item.group === "Projects" && !item.id.startsWith("no-")) {
        setScope(item);
        onOpenChange(false);
        item.onSelect?.();
        return;
      }
      if (item.id === "no-projects" || item.id === "no-match") {
        return;
      }
      onOpenChange(false);
      item.onSelect?.();
      if (item.href) router.push(item.href);
    },
    [onOpenChange, router],
  );

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Escape") {
      e.preventDefault();
      onOpenChange(false);
      return;
    }
    if (e.key === "Tab") {
      // Focus trap: aria-modal="true" tells assistive tech everything behind the
      // palette is inert, so Tab must never be allowed to escape it (WCAG 2.1.2 /
      // dialog pattern) — cycle between the search input and the visible commands.
      const panel = panelRef.current;
      if (!panel) return;
      const nodes = Array.from(panel.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
        (el) => !el.hasAttribute("disabled") && el.offsetParent !== null,
      );
      if (nodes.length === 0) {
        e.preventDefault();
        return;
      }
      const firstEl = nodes[0]!;
      const lastEl = nodes[nodes.length - 1]!;
      if (e.shiftKey && document.activeElement === firstEl) {
        e.preventDefault();
        lastEl.focus();
      } else if (!e.shiftKey && document.activeElement === lastEl) {
        e.preventDefault();
        firstEl.focus();
      }
      return;
    }
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setHighlight((h) => Math.min(h + 1, Math.max(filtered.length - 1, 0)));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setHighlight((h) => Math.max(h - 1, 0));
    } else if (e.key === "Enter") {
      e.preventDefault();
      const item = filtered[highlight];
      if (item) run(item);
    }
  };

  if (!open || typeof document === "undefined") return null;

  const groups = new Map<string, CommandItem[]>();
  for (const item of filtered) {
    const g = item.group ?? "Commands";
    const list = groups.get(g) ?? [];
    list.push(item);
    groups.set(g, list);
  }

  let flatIndex = -1;

  return createPortal(
    <div
      className="ds-cmd-backdrop"
      role="presentation"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onOpenChange(false);
      }}
    >
      <div
        ref={panelRef}
        className="ds-cmd"
        role="dialog"
        aria-modal="true"
        aria-label="Command palette"
        onKeyDown={onKeyDown}
      >
        <div className="ds-cmd__search">
          <span aria-hidden="true" style={{ color: "var(--text-faint)", display: "inline-flex" }}>
            <Icon name="search" size={15} />
          </span>
          <input
            ref={inputRef}
            className="ds-cmd__input"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder={placeholder}
            aria-autocomplete="list"
            aria-controls={listId}
            aria-activedescendant={
              filtered[highlight] ? `${listId}-opt-${filtered[highlight]!.id}` : undefined
            }
            autoComplete="off"
            spellCheck={false}
          />
          <kbd className="ds-cmd__kbd">esc</kbd>
        </div>
        <ul id={listId} className="ds-cmd__list" role="listbox">
          {filtered.length === 0 ? (
            <li className="ds-cmd__empty">No matching commands</li>
          ) : (
            Array.from(groups.entries()).map(([group, items]) => (
              <li key={group} role="presentation">
                <div className="ds-cmd__group-label">{group}</div>
                <ul role="group" aria-label={group} style={{ listStyle: "none", margin: 0, padding: 0 }}>
                  {items.map((item) => {
                    flatIndex += 1;
                    const idx = flatIndex;
                    return (
                      <li key={item.id} role="presentation">
                        <button
                          type="button"
                          id={`${listId}-opt-${item.id}`}
                          role="option"
                          aria-selected={idx === highlight}
                          data-highlighted={idx === highlight ? "true" : undefined}
                          className="ds-cmd__item"
                          onMouseEnter={() => setHighlight(idx)}
                          onClick={() => run(item)}
                        >
                          {item.icon ? <span aria-hidden="true">{item.icon}</span> : null}
                          <span>{item.label}</span>
                        </button>
                      </li>
                    );
                  })}
                </ul>
              </li>
            ))
          )}
        </ul>
      </div>
    </div>,
    document.body,
  );
}

/** Global Cmd-K / Ctrl-K listener + palette host.
 * Loads portfolio projects once so `#` fuzzy scope works without each page
 * wiring its own command list. */
export function CommandPaletteHost({
  commands,
}: {
  commands?: CommandItem[];
}) {
  const [open, setOpen] = useState(false);
  const [projects, setProjects] = useState<CommandItem[]>([]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setOpen((o) => !o);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        // Dynamic import keeps the design package free of a hard feature cycle
        // at module-eval time; portfolio api is feature-local.
        const { fetchPortfolio } = await import("../features/portfolio/api");
        const data = await fetchPortfolio();
        if (cancelled) return;
        // No `href`: the /projects detail page was retired in the P0 prune, so
        // picking one of these only sets `#` scope for the `/` slash commands
        // (Board filtered, Inbox) — see the `run()` handler above.
        const items: CommandItem[] = [
          ...data.projects,
          ...data.scratch,
        ].map((p) => ({
          id: `proj-${p.id}`,
          label: p.name,
          group: "Projects",
          keywords: (p.path ?? []).join(" "),
          icon: <Icon name="grid" size={15} />,
        }));
        setProjects(items);
      } catch {
        /* palette still works for navigate commands */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <>
      <CommandPalette
        open={open}
        onOpenChange={setOpen}
        commands={commands}
        projects={projects}
      />
      {/* Expose open for top-bar button via custom event */}
      <CommandPaletteBridge onOpen={() => setOpen(true)} />
    </>
  );
}

function CommandPaletteBridge({ onOpen }: { onOpen: () => void }) {
  useEffect(() => {
    const handler = () => onOpen();
    window.addEventListener("oaos:open-command-palette", handler);
    return () => window.removeEventListener("oaos:open-command-palette", handler);
  }, [onOpen]);
  return null;
}

export function openCommandPalette() {
  if (typeof window !== "undefined") {
    window.dispatchEvent(new Event("oaos:open-command-palette"));
  }
}

export function CmdKButton({ className }: { className?: string }) {
  return (
    <button
      type="button"
      className={cx("ds-icon-btn", "ds-icon-btn--wide", className)}
      onClick={() => openCommandPalette()}
      aria-label="Open command palette"
      title="Command palette (⌘K)"
    >
      <Icon name="search" size={14} />
      <span aria-hidden="true">⌘K</span>
    </button>
  );
}
