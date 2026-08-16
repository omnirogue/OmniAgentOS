"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { fetchProjects, ProjectApiError } from "./api";
import type { Project } from "./types";

const STORAGE_KEY = "oaos-active-project";

type ProjectContextValue = {
  projects: Project[];
  loading: boolean;
  error: string | null;
  /** Currently selected project id, or "" for "all projects" (unscoped). */
  activeProjectId: string;
  activeProject: Project | null;
  setActiveProjectId: (id: string) => void;
  refresh: () => Promise<void>;
};

const ProjectContext = createContext<ProjectContextValue | null>(null);

function readStored(): string {
  if (typeof window === "undefined") return "";
  try {
    return window.localStorage.getItem(STORAGE_KEY) ?? "";
  } catch {
    return "";
  }
}

export function ProjectProvider({ children }: { children: ReactNode }) {
  const [projects, setProjects] = useState<Project[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [activeProjectId, setActiveIdState] = useState("");

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const rows = await fetchProjects();
      setProjects(rows);
      setError(null);
    } catch (reason) {
      const message =
        reason instanceof ProjectApiError || reason instanceof Error
          ? reason.message
          : "Unable to load projects.";
      setError(message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    setActiveIdState(readStored());
    void refresh();
  }, [refresh]);

  const setActiveProjectId = useCallback((id: string) => {
    setActiveIdState(id);
    try {
      if (id) window.localStorage.setItem(STORAGE_KEY, id);
      else window.localStorage.removeItem(STORAGE_KEY);
    } catch {
      /* ignore */
    }
  }, []);

  // If the stored project vanished (deleted server-side), fall back to unscoped.
  useEffect(() => {
    if (activeProjectId && projects.length && !projects.some((p) => p.id === activeProjectId)) {
      setActiveProjectId("");
    }
  }, [activeProjectId, projects, setActiveProjectId]);

  const activeProject = useMemo(
    () => projects.find((p) => p.id === activeProjectId) ?? null,
    [projects, activeProjectId],
  );

  const value = useMemo(
    () => ({
      projects,
      loading,
      error,
      activeProjectId,
      activeProject,
      setActiveProjectId,
      refresh,
    }),
    [projects, loading, error, activeProjectId, activeProject, setActiveProjectId, refresh],
  );

  return <ProjectContext.Provider value={value}>{children}</ProjectContext.Provider>;
}

export function useProjectContext(): ProjectContextValue {
  const ctx = useContext(ProjectContext);
  if (!ctx) {
    throw new Error("useProjectContext must be used within ProjectProvider");
  }
  return ctx;
}
