"use client";

/**
 * Data hooks for the project-hierarchy views. Each hook attempts the live
 * (frozen) API and, on failure — or when NEXT_PUBLIC_USE_PROJECTS_FIXTURES is
 * set — falls back to in-memory fixtures so the page always renders. `source`
 * lets the UI show a small "showing demo data" notice when the backend is down.
 *
 * When an API call fails, the hook sets source="error" and clears the data
 * (empty array/null), never substituting fixtures in the catch block. This ensures
 * errors are always visible to the user.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import type { EventType, TaskState } from "../../lib/contracts";
import { useEvents } from "../../lib/useEvents";
import {
  fetchConversation,
  fetchProjectActivity,
  fetchProjectTree,
  postConversationMessage,
  type ActivityEntry,
  type ConversationMessage,
  type ConversationRole,
  type ConversationScope,
  type ProjectTreeNode,
} from "./hierarchy";
import {
  USE_FIXTURES,
  appendFixtureMessage,
  fixtureActivity,
  fixtureConversation,
  FIXTURE_TREE,
} from "./hierarchyFixtures";

export type DataSource = "live" | "fixtures" | "error";

const TREE_EVENT_TYPES: EventType[] = ["task.updated"];

function errText(reason: unknown, fallback: string): string {
  return reason instanceof Error ? reason.message : fallback;
}

/** Immutable patch of a single task's state in the tree (SSE task.updated). */
function patchTaskState(
  nodes: ProjectTreeNode[],
  taskId: string,
  state: string,
): ProjectTreeNode[] {
  const walk = (list: ProjectTreeNode[]): { next: ProjectTreeNode[]; changed: boolean } => {
    let changed = false;
    const next = list.map((node) => {
      let taskChanged = false;
      const tasks = node.tasks.map((t) => {
        if (t.id !== taskId || t.state === state) return t;
        taskChanged = true;
        return { ...t, state: state as TaskState };
      });

      let sub_projects = node.sub_projects;
      if (node.sub_projects.length) {
        const sub = walk(node.sub_projects);
        if (sub.changed) {
          sub_projects = sub.next;
          changed = true;
        }
      }

      if (!taskChanged && sub_projects === node.sub_projects) return node;
      if (taskChanged) changed = true;
      return { ...node, tasks, sub_projects };
    });
    return { next, changed };
  };

  const result = walk(nodes);
  return result.changed ? result.next : nodes;
}

export type TreeState = {
  nodes: ProjectTreeNode[];
  loading: boolean;
  error: string | null;
  source: DataSource;
  refresh: () => Promise<void>;
};

export function useProjectTree(): TreeState {
  const [nodes, setNodes] = useState<ProjectTreeNode[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [source, setSource] = useState<DataSource>("live");
  // Live task state: SSE task.updated patches the tree so concurrent RUNNING
  // work pulses without a full refresh. Fixtures skip the stream.
  const { lastEvent } = useEvents(TREE_EVENT_TYPES);

  const refresh = useCallback(async () => {
    setLoading(true);
    if (USE_FIXTURES) {
      setNodes(FIXTURE_TREE);
      setSource("fixtures");
      setError(null);
      setLoading(false);
      return;
    }
    try {
      const data = await fetchProjectTree();
      setNodes(data);
      setSource("live");
      setError(null);
    } catch (reason) {
      // On error: clear nodes and set source to "error" (not fixtures)
      setNodes([]);
      setSource("error");
      setError(errText(reason, "Could not load the project tree."));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    if (!lastEvent || lastEvent.type !== "task.updated") return;
    if (source === "fixtures" || source === "error" || USE_FIXTURES) return;
    const payload = lastEvent.payload ?? {};
    const taskId =
      (typeof payload.task_id === "string" && payload.task_id) ||
      (lastEvent.target_type === "task" ? lastEvent.target_id : "") ||
      "";
    const state = typeof payload.state === "string" ? payload.state : "";
    if (!taskId || !state) return;
    setNodes((prev) => patchTaskState(prev, taskId, state));
  }, [lastEvent, source]);

  return { nodes, loading, error, source, refresh };
}

/** Walk a loaded tree to find a project/sub-project node by id. */
export function findNode(nodes: ProjectTreeNode[], id: string): ProjectTreeNode | null {
  for (const node of nodes) {
    if (node.project.id === id) return node;
    const nested = findNode(node.sub_projects, id);
    if (nested) return nested;
  }
  return null;
}

/** Find a task in the tree, returning it plus its owning project id (for the
 * activity feed, which is project-scoped). */
export function findTaskInTree(
  nodes: ProjectTreeNode[],
  taskId: string,
): { task: ProjectTreeNode["tasks"][number]; projectId: string } | null {
  for (const node of nodes) {
    const task = node.tasks.find((t) => t.id === taskId);
    if (task) return { task, projectId: node.project.id };
    const nested = findTaskInTree(node.sub_projects, taskId);
    if (nested) return nested;
  }
  return null;
}

export type ConversationState = {
  messages: ConversationMessage[];
  loading: boolean;
  error: string | null;
  source: DataSource;
  refresh: () => Promise<void>;
  append: (role: ConversationRole, content: string, model?: string | null) => Promise<void>;
  sending: boolean;
};

export function useConversation(scope: ConversationScope, id: string): ConversationState {
  const [messages, setMessages] = useState<ConversationMessage[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [source, setSource] = useState<DataSource>("live");
  const [sending, setSending] = useState(false);
  const sourceRef = useRef<DataSource>("live");
  sourceRef.current = source;

  const refresh = useCallback(async () => {
    if (!id) return;
    setLoading(true);
    if (USE_FIXTURES) {
      setMessages(fixtureConversation(scope, id));
      setSource("fixtures");
      setError(null);
      setLoading(false);
      return;
    }
    try {
      const data = await fetchConversation(scope, id);
      setMessages(data);
      setSource("live");
      setError(null);
    } catch (reason) {
      // On error: clear messages and set source to "error" (not fixtures)
      setMessages([]);
      setSource("error");
      setError(errText(reason, "Could not load the conversation."));
    } finally {
      setLoading(false);
    }
  }, [scope, id]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const append = useCallback(
    async (role: ConversationRole, content: string, model?: string | null) => {
      if (!content.trim() || !id) return;
      setSending(true);
      // Offline (fixtures) path: append locally so the composer still works.
      if (USE_FIXTURES || sourceRef.current === "fixtures") {
        const created = appendFixtureMessage(scope, id, role, content.trim(), model ?? null);
        setMessages((prev) => [...prev, created]);
        setSending(false);
        return;
      }
      try {
        const created = await postConversationMessage(scope, id, {
          role,
          content: content.trim(),
          model: model ?? undefined,
        });
        setMessages((prev) => [...prev, created]);
        setError(null);
      } catch (reason) {
        setError(errText(reason, "Could not send the message."));
        throw reason;
      } finally {
        setSending(false);
      }
    },
    [scope, id],
  );

  return { messages, loading, error, source, refresh, append, sending };
}

export type ActivityState = {
  entries: ActivityEntry[];
  loading: boolean;
  error: string | null;
  source: DataSource;
  refresh: () => Promise<void>;
};

export function useActivity(projectId: string): ActivityState {
  const [entries, setEntries] = useState<ActivityEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [source, setSource] = useState<DataSource>("live");

  const refresh = useCallback(async () => {
    if (!projectId) return;
    setLoading(true);
    if (USE_FIXTURES) {
      setEntries(fixtureActivity(projectId));
      setSource("fixtures");
      setError(null);
      setLoading(false);
      return;
    }
    try {
      const data = await fetchProjectActivity(projectId);
      setEntries(data);
      setSource("live");
      setError(null);
    } catch (reason) {
      // On error: clear entries and set source to "error" (not fixtures)
      setEntries([]);
      setSource("error");
      setError(errText(reason, "Could not load activity."));
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return { entries, loading, error, source, refresh };
}

/**
 * useModelOptions moved to `@/features/models/useModels` (chat-v2 §2.3.1) —
 * the ONE model-list hook for the app. Import it from there.
 */
