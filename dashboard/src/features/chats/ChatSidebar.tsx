"use client";

/**
 * ChatSidebar — projects + color-coded folders, restyled to the prototype's
 * `.chat-list` (300px rail, folder tree with carets and counts, Recents in day
 * buckets, "New folder" affordance).
 *
 * The prototype FLATTENS the tree; we keep nesting (projects and folders are
 * real containers here) and adopt only its visual language.
 *
 * Sections: search → project groups (useProjectTree; collapsible; drop
 * targets) → Folders (088 registry: colored dot per folder, options menu
 * with inline rename + 8-token color swatch row + delete, "+ New folder"
 * with a color picker) → Recents (project-less, bucketed Today / Yesterday /
 * Previous 7 days / Older) → + New chat.
 *
 * Folder membership rides chats.meta.folder (server registry adds color +
 * order). A chat with a folder renders under that folder — folder wins over
 * project for sidebar placement; moving a chat to a project clears its
 * folder so every chat has exactly one home.
 *
 * Every drag has a menu equivalent (a11y/keyboard). Native prompt()/confirm()
 * are gone — rename/archive use the surface header menu; archive from the
 * sidebar keyboard map is a toast with undo (§2.7).
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Badge, Button, Dialog, EmptyState, Icon, cx, useToast } from "@/design";
import type { ProjectTreeNode } from "@/features/projects/hierarchy";
import type { Chat } from "./chatApi";
import * as chatApi from "./chatApi";
import {
  FOLDER_COLORS,
  buildFolderGroups,
  chatFolder,
  folderNameError,
  type FolderColor,
  type FolderInfo,
} from "./folders";
import styles from "./chatShell.module.css";

interface ChatSidebarProps {
  chats: Chat[];
  selectedChatId: string | null;
  loading: boolean;
  collapsed: boolean;
  onSelectChat: (chatId: string) => void;
  onCreateChat: () => void;
  onToggleCollapse: () => void;
  onChatsChanged: () => void;
  /** Project tree owned by the page (one fetch per mount — the sidebar used
   * to fire a second, identical GET /api/projects/tree). */
  projectNodes: ProjectTreeNode[];
  /** Refetch the project tree after creating a project. */
  onProjectsChanged: () => void | Promise<void>;
  /** Phone (<36rem): the sidebar is a slide-over the page reveals. Deliberately
   * independent of `collapsed` (⌘\), which is a desktop-width control. */
  mobileOpen?: boolean;
}

const RECENTS_KEY = "__recents__";
const FOLDER_KEY_PREFIX = "fld:";

/** Drop-target/select key for a folder (distinct from project ids). */
const folderKey = (name: string) => `${FOLDER_KEY_PREFIX}${name}`;

/** One color class + one shape class per element (chats.module.css). */
const FOLDER_COLOR_CLASS: Record<FolderColor, string> = {
  gray: styles.folderColorGray,
  red: styles.folderColorRed,
  orange: styles.folderColorOrange,
  yellow: styles.folderColorYellow,
  green: styles.folderColorGreen,
  teal: styles.folderColorTeal,
  blue: styles.folderColorBlue,
  violet: styles.folderColorViolet,
};

type MoveDest =
  | { kind: "project"; id: string }
  | { kind: "folder"; name: string }
  | { kind: "recents" };

type Bucket = "Today" | "Yesterday" | "Previous 7 days" | "Older";

function bucketFor(iso: string): Bucket {
  const time = new Date(iso).getTime();
  if (Number.isNaN(time)) return "Older";
  const dayMs = 86_400_000;
  const now = new Date();
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  if (time >= startOfToday) return "Today";
  if (time >= startOfToday - dayMs) return "Yesterday";
  if (time >= startOfToday - 7 * dayMs) return "Previous 7 days";
  return "Older";
}

function formatTime(value: string): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const now = new Date();
  const diffMins = Math.floor((now.getTime() - date.getTime()) / 60000);
  if (diffMins < 1) return "now";
  if (diffMins < 60) return `${diffMins}m`;
  const diffHours = Math.floor(diffMins / 60);
  if (diffHours < 24) return `${diffHours}h`;
  const diffDays = Math.floor(diffHours / 24);
  if (diffDays === 1) return "yesterday";
  if (diffDays < 7) return `${diffDays}d`;
  return date.toLocaleDateString([], { month: "short", day: "numeric" });
}

export function ChatSidebar({
  chats,
  selectedChatId,
  loading,
  collapsed,
  onSelectChat,
  onCreateChat,
  onToggleCollapse,
  onChatsChanged,
  projectNodes,
  onProjectsChanged,
  mobileOpen = false,
}: ChatSidebarProps) {
  const { push } = useToast();
  const [query, setQuery] = useState("");
  const [dropTarget, setDropTarget] = useState<string | null>(null);
  const [draggingChatId, setDraggingChatId] = useState<string | null>(null);
  const [collapsedProjects, setCollapsedProjects] = useState<Set<string>>(new Set());
  const [collapsedFolders, setCollapsedFolders] = useState<Set<string>>(new Set());
  const [recentsCollapsed, setRecentsCollapsed] = useState(false);
  const [focusIndex, setFocusIndex] = useState(-1);
  const [newProjectOpen, setNewProjectOpen] = useState(false);
  const [newProjectName, setNewProjectName] = useState("");
  const [creatingProject, setCreatingProject] = useState(false);
  const searchRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);

  // ── Folder registry (088) ──
  const [folderRegistry, setFolderRegistry] = useState<FolderInfo[]>([]);
  const [folderMenuFor, setFolderMenuFor] = useState<string | null>(null);
  const [renamingFolder, setRenamingFolder] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState("");
  const [newFolderOpen, setNewFolderOpen] = useState(false);
  const [newFolderName, setNewFolderName] = useState("");
  const [newFolderColor, setNewFolderColor] = useState<FolderColor>("blue");
  const [creatingFolder, setCreatingFolder] = useState(false);
  const [deleteFolderTarget, setDeleteFolderTarget] = useState<string | null>(null);
  const [deletingFolder, setDeletingFolder] = useState(false);
  const folderMenuRef = useRef<HTMLDivElement>(null);
  const folderMenuTriggerRef = useRef<HTMLButtonElement | null>(null);

  const refreshFolders = useCallback(async () => {
    try {
      setFolderRegistry(await chatApi.listFolders());
    } catch {
      /* silent — the sidebar still groups by the folders found on chats */
    }
  }, []);

  useEffect(() => {
    void refreshFolders();
  }, [refreshFolders, chats]);

  // Flatten the project tree (one level is enough for the sidebar grouping)
  const projects = useMemo(() => {
    const flat: Array<{ id: string; name: string }> = [];
    const walk = (nodes: typeof projectNodes) => {
      for (const node of nodes) {
        flat.push({ id: node.project.id, name: node.project.name });
        if (node.sub_projects?.length) walk(node.sub_projects);
      }
    };
    walk(projectNodes);
    return flat;
  }, [projectNodes]);

  const projectName = useCallback(
    (id: string | null) => projects.find((p) => p.id === id)?.name ?? null,
    [projects],
  );

  // ── Group + filter chats ──

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    // Archived leaves the active list (undo toast is the recovery path);
    // deleted never arrives from the server but stays filtered for fixtures.
    const live = chats.filter((c) => c.status !== "deleted" && c.status !== "archived");
    if (!q) return live;
    return live.filter((c) => c.title.toLowerCase().includes(q));
  }, [chats, query]);

  const folderGroups = useMemo(
    () => buildFolderGroups(filtered, folderRegistry),
    [filtered, folderRegistry],
  );

  const grouped = useMemo(() => {
    const map = new Map<string, Chat[]>();
    for (const project of projects) map.set(project.id, []);
    map.set(RECENTS_KEY, []);
    for (const chat of filtered) {
      // Folder wins: chats filed in a folder render there, nowhere else.
      if (chatFolder(chat)) continue;
      const key = chat.project_id && map.has(chat.project_id) ? chat.project_id : RECENTS_KEY;
      map.get(key)!.push(chat);
    }
    for (const list of map.values()) {
      list.sort((a, b) => new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime());
    }
    return map;
  }, [filtered, projects]);

  const recentBuckets = useMemo(() => {
    const buckets = new Map<Bucket, Chat[]>();
    for (const chat of grouped.get(RECENTS_KEY) ?? []) {
      const bucket = bucketFor(chat.updated_at);
      buckets.set(bucket, [...(buckets.get(bucket) ?? []), chat]);
    }
    return buckets;
  }, [grouped]);

  // Flat ordered list of visible chats for ↑↓ keyboard nav (render order:
  // projects → folders → recents)
  const flatChats = useMemo(() => {
    const out: Chat[] = [];
    for (const project of projects) {
      if (collapsedProjects.has(project.id)) continue;
      out.push(...(grouped.get(project.id) ?? []));
    }
    for (const group of folderGroups) {
      if (collapsedFolders.has(group.name)) continue;
      out.push(...group.chats);
    }
    for (const bucket of ["Today", "Yesterday", "Previous 7 days", "Older"] as Bucket[]) {
      out.push(...(recentBuckets.get(bucket) ?? []));
    }
    return out;
  }, [projects, grouped, folderGroups, recentBuckets, collapsedProjects, collapsedFolders]);

  // ── Move chat (drop or menu) ──

  const moveChat = useCallback(
    async (chatId: string, dest: MoveDest) => {
      // A project (or Recents) move clears the folder and vice versa isn't
      // needed: folder wins for placement, so setting one home clears the
      // other and every chat stays in exactly one sidebar location.
      const patch =
        dest.kind === "folder"
          ? { folder: dest.name }
          : dest.kind === "project"
            ? { project_id: dest.id, folder: "" }
            : { project_id: null, folder: "" };
      try {
        await chatApi.updateChat(chatId, patch);
        onChatsChanged();
        void refreshFolders();
        const label =
          dest.kind === "folder"
            ? dest.name
            : dest.kind === "project"
              ? projectName(dest.id)
              : null;
        push({
          tone: "success",
          message: label ? `Moved to ${label}` : "Moved to Recents",
        });
      } catch (reason) {
        push({
          tone: "error",
          title: "Could not move chat",
          message: reason instanceof Error ? reason.message : "Unknown error",
        });
      }
    },
    [onChatsChanged, projectName, push, refreshFolders],
  );

  const destForKey = useCallback((key: string): MoveDest => {
    if (key === RECENTS_KEY) return { kind: "recents" };
    if (key.startsWith(FOLDER_KEY_PREFIX)) {
      return { kind: "folder", name: key.slice(FOLDER_KEY_PREFIX.length) };
    }
    return { kind: "project", id: key };
  }, []);

  const archiveChat = useCallback(
    async (chat: Chat) => {
      try {
        await chatApi.updateChat(chat.id, { status: "archived" });
        onChatsChanged();
        // design Toast has no action slot, so the undo lives in a transient
        // sidebar banner (auto-dismisses) — same undo pattern, no design change.
        setUndoArchive({ chat, until: Date.now() + 5000 });
      } catch (reason) {
        push({
          tone: "error",
          title: "Could not archive",
          message: reason instanceof Error ? reason.message : "Unknown error",
        });
      }
    },
    [onChatsChanged, push],
  );

  const [undoArchive, setUndoArchive] = useState<{ chat: Chat; until: number } | null>(null);
  useEffect(() => {
    if (!undoArchive) return;
    const remaining = undoArchive.until - Date.now();
    const timer = setTimeout(() => setUndoArchive(null), Math.max(remaining, 0));
    return () => clearTimeout(timer);
  }, [undoArchive]);

  const undoArchiveChat = useCallback(async () => {
    if (!undoArchive) return;
    try {
      await chatApi.updateChat(undoArchive.chat.id, { status: "active" });
      onChatsChanged();
    } catch {
      /* silent — the next refresh shows the true state */
    }
    setUndoArchive(null);
  }, [undoArchive, onChatsChanged]);

  // ── Create project (⌘⇧N) ──

  const createProject = useCallback(async () => {
    const name = newProjectName.trim();
    if (!name) return;
    setCreatingProject(true);
    try {
      await chatApi.createProject(name);
      setNewProjectOpen(false);
      setNewProjectName("");
      await onProjectsChanged();
      push({ tone: "success", title: "Project created", message: name });
    } catch (reason) {
      push({
        tone: "error",
        title: "Could not create project",
        message: reason instanceof Error ? reason.message : "Unknown error",
      });
    } finally {
      setCreatingProject(false);
    }
  }, [newProjectName, onProjectsChanged, push]);

  // ── Folder actions (088): create / recolor / rename / delete ──

  const createFolder = useCallback(async () => {
    const name = newFolderName.trim();
    const error = folderNameError(name);
    if (error) {
      push({ tone: "error", title: "Invalid folder name", message: error });
      return;
    }
    setCreatingFolder(true);
    try {
      // Registering a color IS folder creation (upsert) — the folder then
      // shows as an empty drop target until chats are filed into it.
      await chatApi.setFolderColor(name, newFolderColor);
      setNewFolderOpen(false);
      setNewFolderName("");
      setNewFolderColor("blue");
      await refreshFolders();
      push({ tone: "success", title: "Folder created", message: name });
    } catch (reason) {
      push({
        tone: "error",
        title: "Could not create folder",
        message: reason instanceof Error ? reason.message : "Unknown error",
      });
    } finally {
      setCreatingFolder(false);
    }
  }, [newFolderName, newFolderColor, push, refreshFolders]);

  const applyFolderColor = useCallback(
    async (name: string, color: FolderColor) => {
      setFolderMenuFor(null);
      folderMenuTriggerRef.current?.focus();
      try {
        await chatApi.setFolderColor(name, color);
        await refreshFolders();
      } catch (reason) {
        push({
          tone: "error",
          title: "Could not set folder color",
          message: reason instanceof Error ? reason.message : "Unknown error",
        });
      }
    },
    [push, refreshFolders],
  );

  const startRenameFolder = useCallback((name: string) => {
    setFolderMenuFor(null);
    setRenamingFolder(name);
    setRenameValue(name);
  }, []);

  const commitRenameFolder = useCallback(
    async (name: string) => {
      const next = renameValue.trim();
      setRenamingFolder(null);
      if (!next || next === name) return;
      const error = folderNameError(next);
      if (error) {
        push({ tone: "error", title: "Invalid folder name", message: error });
        return;
      }
      try {
        await chatApi.renameFolder(name, next);
        onChatsChanged();
        await refreshFolders();
        push({ tone: "success", message: `Renamed to ${next}` });
      } catch (reason) {
        push({
          tone: "error",
          title: "Could not rename folder",
          message: reason instanceof Error ? reason.message : "Unknown error",
        });
      }
    },
    [renameValue, onChatsChanged, push, refreshFolders],
  );

  const confirmDeleteFolder = useCallback(async () => {
    if (!deleteFolderTarget) return;
    setDeletingFolder(true);
    try {
      const result = await chatApi.deleteFolder(deleteFolderTarget);
      setDeleteFolderTarget(null);
      onChatsChanged();
      await refreshFolders();
      push({
        tone: "success",
        title: "Folder deleted",
        message:
          result.chats_moved > 0
            ? `${result.chats_moved} chat${result.chats_moved === 1 ? "" : "s"} moved to Recents`
            : "It was empty",
      });
    } catch (reason) {
      push({
        tone: "error",
        title: "Could not delete folder",
        message: reason instanceof Error ? reason.message : "Unknown error",
      });
    } finally {
      setDeletingFolder(false);
    }
  }, [deleteFolderTarget, onChatsChanged, push, refreshFolders]);

  // Folder options menu: focus first item on open; Escape/click-outside close
  useEffect(() => {
    if (!folderMenuFor) return;
    folderMenuRef.current?.querySelector("button")?.focus();
    const onDown = (e: MouseEvent) => {
      if (folderMenuRef.current && !folderMenuRef.current.contains(e.target as Node)) {
        setFolderMenuFor(null);
      }
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setFolderMenuFor(null);
        folderMenuTriggerRef.current?.focus();
      }
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [folderMenuFor]);

  // ── Drag & drop ──

  const handleDragStart = useCallback((e: React.DragEvent, chatId: string) => {
    setDraggingChatId(chatId);
    e.dataTransfer.setData("text/plain", chatId);
    e.dataTransfer.effectAllowed = "move";
  }, []);

  const handleDrop = useCallback(
    (e: React.DragEvent, key: string) => {
      e.preventDefault();
      const chatId = e.dataTransfer.getData("text/plain");
      if (chatId) {
        void moveChat(chatId, destForKey(key));
      }
      setDropTarget(null);
      setDraggingChatId(null);
    },
    [moveChat, destForKey],
  );

  const dropHandlers = useCallback(
    (key: string) => ({
      onDragOver: (e: React.DragEvent) => {
        e.preventDefault();
        e.dataTransfer.dropEffect = "move";
        setDropTarget(key);
      },
      onDragLeave: () => setDropTarget(null),
      onDrop: (e: React.DragEvent) => handleDrop(e, key),
    }),
    [handleDrop],
  );

  // ── Keyboard map (§2.7): ⌘F search · ↑↓ navigate · Enter open · ⌫ archive ──

  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      // A dialog (new project / new folder / delete folder) owns the keys
      // while open — ⌫ must never archive a chat behind the modal.
      if (document.querySelector('[role="dialog"]')) return;
      const meta = e.metaKey || e.ctrlKey;
      if (meta && e.key === "f") {
        e.preventDefault();
        searchRef.current?.focus();
        return;
      }
      if (meta && e.shiftKey && (e.key === "N" || e.key === "n")) {
        e.preventDefault();
        setNewProjectOpen(true);
        return;
      }
      const target = e.target as HTMLElement | null;
      const typing =
        target instanceof HTMLInputElement ||
        target instanceof HTMLTextAreaElement ||
        target instanceof HTMLSelectElement ||
        target?.isContentEditable;
      if (typing) return;
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        if (!flatChats.length) return;
        e.preventDefault();
        setFocusIndex((prev) => {
          const next =
            e.key === "ArrowDown"
              ? Math.min(prev + 1, flatChats.length - 1)
              : Math.max(prev - 1, 0);
          return next;
        });
      } else if (e.key === "Enter" && focusIndex >= 0 && flatChats[focusIndex]) {
        e.preventDefault();
        onSelectChat(flatChats[focusIndex].id);
      } else if ((e.key === "Backspace" || e.key === "Delete") && focusIndex >= 0 && flatChats[focusIndex]) {
        e.preventDefault();
        void archiveChat(flatChats[focusIndex]);
      }
    };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, [flatChats, focusIndex, onSelectChat, archiveChat]);

  useEffect(() => {
    setFocusIndex(-1);
  }, [query]);

  // Keep the focused row visible
  useEffect(() => {
    if (focusIndex < 0 || !listRef.current) return;
    const el = listRef.current.querySelector(`[data-chat-id="${flatChats[focusIndex]?.id}"]`);
    el?.scrollIntoView({ block: "nearest" });
  }, [focusIndex, flatChats]);

  const toggleProject = useCallback((id: string) => {
    setCollapsedProjects((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  const toggleFolder = useCallback((name: string) => {
    setCollapsedFolders((prev) => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
  }, []);

  // ── Render ──

  const sidebarClassName = `${styles.list}${collapsed ? ` ${styles.listCollapsed}` : ""}`;

  const renderChatRow = (chat: Chat) => {
    const currentFolder = chatFolder(chat);
    return (
      <div
        key={chat.id}
        data-chat-id={chat.id}
        className={cx(
          styles.item,
          chat.id === selectedChatId && styles.itemActive,
          chat.id === draggingChatId && styles.itemDragging,
          flatChats[focusIndex]?.id === chat.id && styles.itemFocused,
        )}
        draggable
        onDragStart={(e) => handleDragStart(e, chat.id)}
        onDragEnd={() => setDraggingChatId(null)}
        onClick={() => onSelectChat(chat.id)}
        role="button"
        tabIndex={0}
        onKeyDown={(e) => {
          if (e.key === "Enter") onSelectChat(chat.id);
        }}
      >
        <span className={styles.itemText}>
          <span className={styles.itemTitle}>{chat.title || "Untitled"}</span>
          <span className={styles.itemMeta}>
            {formatTime(chat.last_message_at ?? chat.updated_at)}
            {typeof chat.message_count === "number" && chat.message_count > 0
              ? ` · ${chat.message_count} message${chat.message_count === 1 ? "" : "s"}`
              : ""}
          </span>
        </span>
        {chat.status === "promoted" ? <Badge tone="completed">promoted</Badge> : null}
        {/* Menu equivalent of drag: move to folder / project / recents */}
        <select
          className={styles.itemMove}
          aria-label={`Move ${chat.title} to a folder or project`}
          value={currentFolder ? folderKey(currentFolder) : (chat.project_id ?? "")}
          onClick={(e) => e.stopPropagation()}
          onChange={(e) => {
            e.stopPropagation();
            void moveChat(chat.id, destForKey(e.target.value || RECENTS_KEY));
          }}
        >
          <option value="">Recents</option>
          {folderGroups.length ? (
            <optgroup label="Folders">
              {folderGroups.map((g) => (
                <option key={g.name} value={folderKey(g.name)}>
                  {g.name}
                </option>
              ))}
            </optgroup>
          ) : null}
          {projects.length ? (
            <optgroup label="Projects">
              {projects.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </optgroup>
          ) : null}
        </select>
      </div>
    );
  };

  const renderSwatchRow = (
    groupName: string,
    activeColor: FolderColor,
    onPick: (color: FolderColor) => void,
  ) => (
    <div className={styles.swatchRow} role="group" aria-label={`Color for ${groupName}`}>
      {FOLDER_COLORS.map((color) => (
        <button
          key={color}
          type="button"
          role="menuitemradio"
          aria-checked={color === activeColor}
          aria-label={`Set ${groupName} color to ${color}`}
          className={cx(
            styles.swatch,
            FOLDER_COLOR_CLASS[color],
            color === activeColor && styles.swatchActive,
          )}
          onClick={() => onPick(color)}
        />
      ))}
    </div>
  );

  const recentsCount = (["Today", "Yesterday", "Previous 7 days", "Older"] as Bucket[]).reduce(
    (total, bucket) => total + (recentBuckets.get(bucket)?.length ?? 0),
    0,
  );

  return (
    <aside className={sidebarClassName} data-mobile-open={mobileOpen ? "true" : "false"}>
      <div className={styles.listHead}>
        <div className={styles.listHeadRow}>
          <h2 className={styles.listTitle}>Chats</h2>
          <div className={styles.listHeadActions}>
            <Button
              variant="ghost"
              size="sm"
              onClick={() => setNewProjectOpen(true)}
              title="New project (⌘⇧N)"
              aria-label="New project"
            >
              <Icon name="grid" size={14} />
            </Button>
            <Button
              variant="ghost"
              size="sm"
              onClick={onToggleCollapse}
              aria-label={collapsed ? "Expand sidebar (⌘\\)" : "Collapse sidebar (⌘\\)"}
            >
              <Icon name="menu" size={14} />
            </Button>
            <Button variant="primary" size="sm" onClick={onCreateChat} title="New chat (⌘N)">
              <Icon name="plus" size={12} />
              New chat
            </Button>
          </div>
        </div>

        <div className={styles.searchField}>
          <Icon name="search" size={13} />
          <input
            ref={searchRef}
            type="search"
            className={styles.searchInput}
            placeholder="Search chats…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            aria-label="Search chats"
          />
          <kbd className={styles.searchKbd}>⌘F</kbd>
        </div>
      </div>

      {undoArchive ? (
        <div className={styles.undoBanner} role="status">
          <span>Archived “{undoArchive.chat.title}”</span>
          <Button variant="ghost" size="sm" onClick={() => void undoArchiveChat()}>
            Undo
          </Button>
        </div>
      ) : null}

      <div className={styles.scroll} ref={listRef}>
        {loading && chats.length === 0 ? (
          <div className={styles.sidebarSkeletons} aria-label="Loading chats">
            {[0, 1, 2, 3, 4, 5].map((i) => (
              <div key={i} className={`${styles.sidebarSkeletonRow} ds-skeleton`} />
            ))}
          </div>
        ) : filtered.length === 0 && folderGroups.length === 0 ? (
          query.trim() ? (
            <EmptyState
              title={`No chats match “${query.trim()}”`}
              message="Clear the search to see every chat."
            />
          ) : (
            <EmptyState
              title="No chats yet — press ⌘N."
              message=""
              action={
                <Button variant="secondary" size="sm" onClick={onCreateChat}>
                  New chat
                </Button>
              }
            />
          )
        ) : (
          <>
            {/* Projects keep their nesting — the prototype flattens the tree, we
                adopt only its visual language. */}
            {projects.map((project) => {
              const projectChats = grouped.get(project.id) ?? [];
              const isCollapsed = collapsedProjects.has(project.id);
              const isDropTarget = dropTarget === project.id;
              return (
                <div key={project.id} className={styles.folder}>
                  <div
                    className={cx(styles.folderHead, isDropTarget && styles.folderDropTarget)}
                    {...dropHandlers(project.id)}
                  >
                    <button
                      className={styles.folderCaret}
                      aria-expanded={!isCollapsed}
                      aria-label={`${isCollapsed ? "Expand" : "Collapse"} ${project.name}`}
                      onClick={() => toggleProject(project.id)}
                    >
                      <Icon name={isCollapsed ? "chevronRight" : "chevronDown"} size={12} />
                    </button>
                    <Icon name="grid" size={12} />
                    <span className={styles.folderName}>{project.name}</span>
                    <span className={styles.folderCount}>{projectChats.length}</span>
                  </div>
                  {!isCollapsed ? (
                    <div className={styles.folderBody}>
                      {projectChats.length ? (
                        projectChats.map(renderChatRow)
                      ) : (
                        <p className={styles.folderEmpty}>
                          Drop a chat here to add it to {project.name}.
                        </p>
                      )}
                    </div>
                  ) : null}
                </div>
              );
            })}

            {folderGroups.map((group) => {
              const isCollapsed = collapsedFolders.has(group.name);
              const key = folderKey(group.name);
              const isDropTarget = dropTarget === key;
              const isRenaming = renamingFolder === group.name;
              return (
                <div key={key} className={styles.folder}>
                  <div
                    className={cx(
                      styles.folderHead,
                      FOLDER_COLOR_CLASS[group.color],
                      isDropTarget && styles.folderDropTarget,
                    )}
                    {...dropHandlers(key)}
                  >
                    <button
                      className={styles.folderCaret}
                      aria-expanded={!isCollapsed}
                      aria-label={`${isCollapsed ? "Expand" : "Collapse"} folder ${group.name}`}
                      onClick={() => toggleFolder(group.name)}
                    >
                      <Icon name={isCollapsed ? "chevronRight" : "chevronDown"} size={12} />
                    </button>
                    <span
                      className={cx(styles.folderDot, FOLDER_COLOR_CLASS[group.color])}
                      aria-hidden
                    />
                    {isRenaming ? (
                      <input
                        className={styles.folderRenameInput}
                        value={renameValue}
                        onChange={(e) => setRenameValue(e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === "Enter") {
                            e.preventDefault();
                            void commitRenameFolder(group.name);
                          } else if (e.key === "Escape") {
                            e.stopPropagation();
                            setRenamingFolder(null);
                          }
                        }}
                        onBlur={() => setRenamingFolder(null)}
                        aria-label={`Rename folder ${group.name}`}
                        autoFocus
                      />
                    ) : (
                      <span className={styles.folderName}>{group.name}</span>
                    )}
                    <span className={styles.folderCount}>{group.chats.length}</span>
                    <button
                      className={styles.folderMenuBtn}
                      aria-label={`Folder ${group.name} options`}
                      aria-haspopup="menu"
                      aria-expanded={folderMenuFor === group.name}
                      onClick={(e) => {
                        folderMenuTriggerRef.current = e.currentTarget;
                        setFolderMenuFor(folderMenuFor === group.name ? null : group.name);
                      }}
                    >
                      <Icon name="palette" size={12} />
                    </button>
                  </div>
                  {folderMenuFor === group.name ? (
                    <div
                      ref={folderMenuRef}
                      role="menu"
                      aria-label={`Folder ${group.name} options`}
                      className={styles.folderMenu}
                    >
                      <button
                        role="menuitem"
                        className={styles.folderMenuItem}
                        onClick={() => startRenameFolder(group.name)}
                      >
                        Rename…
                      </button>
                      {renderSwatchRow(group.name, group.color, (color) =>
                        void applyFolderColor(group.name, color),
                      )}
                      <button
                        role="menuitem"
                        className={cx(styles.folderMenuItem, styles.folderMenuDanger)}
                        onClick={() => {
                          setFolderMenuFor(null);
                          setDeleteFolderTarget(group.name);
                        }}
                      >
                        Delete folder…
                      </button>
                    </div>
                  ) : null}
                  {!isCollapsed ? (
                    <div className={styles.folderBody}>
                      {group.chats.length ? (
                        group.chats.map(renderChatRow)
                      ) : (
                        <p className={styles.folderEmpty}>
                          Drop a chat here to file it in {group.name}.
                        </p>
                      )}
                    </div>
                  ) : null}
                </div>
              );
            })}

            <button className={styles.newFolderBtn} onClick={() => setNewFolderOpen(true)}>
              <Icon name="plus" size={12} />
              New folder
            </button>

            <div className={cx(styles.folder, styles.recents)}>
              <div
                className={cx(
                  styles.folderHead,
                  dropTarget === RECENTS_KEY && styles.folderDropTarget,
                )}
                {...dropHandlers(RECENTS_KEY)}
              >
                <button
                  className={styles.folderCaret}
                  aria-expanded={!recentsCollapsed}
                  aria-label={`${recentsCollapsed ? "Expand" : "Collapse"} Recents`}
                  onClick={() => setRecentsCollapsed((prev) => !prev)}
                >
                  <Icon name={recentsCollapsed ? "chevronRight" : "chevronDown"} size={12} />
                </button>
                <Icon name="clock" size={12} />
                <span className={styles.folderName}>Recents</span>
                <span className={styles.folderCount}>{recentsCount}</span>
              </div>
              {!recentsCollapsed ? (
                <div className={styles.folderBody}>
                  {recentsCount === 0 ? (
                    <p className={styles.folderEmpty}>Drop a chat here to unfile it.</p>
                  ) : null}
                  {(["Today", "Yesterday", "Previous 7 days", "Older"] as Bucket[]).map(
                    (bucket) => {
                      const bucketChats = recentBuckets.get(bucket) ?? [];
                      if (!bucketChats.length) return null;
                      return (
                        <div key={bucket}>
                          <span className={styles.dayLabel}>{bucket}</span>
                          {bucketChats.map(renderChatRow)}
                        </div>
                      );
                    },
                  )}
                </div>
              ) : null}
            </div>
          </>
        )}

        <button className={styles.newChatBtn} onClick={onCreateChat}>
          <Icon name="plus" size={14} />
          New chat
        </button>
      </div>

      {/* New project dialog */}
      <Dialog
        open={newProjectOpen}
        onClose={() => setNewProjectOpen(false)}
        title="New project"
        footer={
          <>
            <Button variant="ghost" onClick={() => setNewProjectOpen(false)}>
              Cancel
            </Button>
            <Button
              variant="primary"
              onClick={() => void createProject()}
              disabled={creatingProject || !newProjectName.trim()}
            >
              {creatingProject ? "Creating…" : "Create"}
            </Button>
          </>
        }
      >
        <div className={styles.dialogField}>
          <label className={styles.dialogLabel}>Name</label>
          <input
            className={styles.dialogInput}
            value={newProjectName}
            onChange={(e) => setNewProjectName(e.target.value)}
            placeholder="e.g. Platform"
            onKeyDown={(e) => {
              if (e.key === "Enter") void createProject();
            }}
            autoFocus
          />
        </div>
      </Dialog>

      {/* New folder dialog (088): name + color token picker */}
      <Dialog
        open={newFolderOpen}
        onClose={() => setNewFolderOpen(false)}
        title="New folder"
        footer={
          <>
            <Button variant="ghost" onClick={() => setNewFolderOpen(false)}>
              Cancel
            </Button>
            <Button
              variant="primary"
              onClick={() => void createFolder()}
              disabled={creatingFolder || folderNameError(newFolderName) !== null}
            >
              {creatingFolder ? "Creating…" : "Create"}
            </Button>
          </>
        }
      >
        <div className={styles.dialogField}>
          <label className={styles.dialogLabel}>Name</label>
          <input
            className={styles.dialogInput}
            value={newFolderName}
            onChange={(e) => setNewFolderName(e.target.value)}
            placeholder="e.g. Research"
            onKeyDown={(e) => {
              if (e.key === "Enter") void createFolder();
            }}
            autoFocus
          />
        </div>
        <div className={styles.dialogField}>
          <span className={styles.dialogLabel}>Color</span>
          {renderSwatchRow("the new folder", newFolderColor, setNewFolderColor)}
        </div>
      </Dialog>

      {/* Delete folder confirm (088): chats fall back to Recents */}
      <Dialog
        open={deleteFolderTarget !== null}
        onClose={() => setDeleteFolderTarget(null)}
        title={`Delete “${deleteFolderTarget ?? ""}”?`}
        footer={
          <>
            <Button variant="ghost" onClick={() => setDeleteFolderTarget(null)}>
              Cancel
            </Button>
            <Button
              variant="danger"
              onClick={() => void confirmDeleteFolder()}
              disabled={deletingFolder}
            >
              {deletingFolder ? "Deleting…" : "Delete folder"}
            </Button>
          </>
        }
      >
        <p className={styles.folderEmpty}>
          Chats in this folder move back to Recents. The folder’s color is
          discarded.
        </p>
      </Dialog>
    </aside>
  );
}
