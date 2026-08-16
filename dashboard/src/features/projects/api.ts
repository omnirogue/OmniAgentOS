/** Fetch client for the W2 projects API — see omniagentos/api/routes/projects.py.
 * Kept in the feature (not lib/api) so the frozen lib client is untouched. */

import { apiUrl } from "../../lib/apiRoute";
import { API_BASE, type Approval, type RunSummary, type TaskRow } from "../../lib/contracts";
import { fetchWithTimeout } from "../../lib/fetchTimeout";
import type { CreateProjectInput, Project, ProjectActivity } from "./types";

export type ProjectFile = {
  name: string;
  path: string;
  size: number;
  modified: string;
  type: string;
};

export class ProjectApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
    this.name = "ProjectApiError";
  }
}

async function req<T>(
  path: string,
  init?: RequestInit,
  authorizedRead = false,
  parseResponse: (response: Response) => Promise<T> = (response) => response.json() as Promise<T>,
): Promise<T> {
  // Mutations and filesystem-capability reads go same-origin so the Next.js
  // proxy attaches the session token without exposing it to browser code.
  const url = authorizedRead ? path : apiUrl(API_BASE, path, init?.method);
  const headers = init?.body instanceof FormData
    ? { ...(init?.headers ?? {}) }
    : { "Content-Type": "application/json", ...(init?.headers ?? {}) };
  const res = await fetchWithTimeout(url, {
    ...init,
    headers,
    cache: "no-store",
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body?.error?.message ?? detail;
    } catch {
      /* keep statusText */
    }
    throw new ProjectApiError(detail, res.status);
  }
  return parseResponse(res);
}

function qs(params: Record<string, string>): string {
  const entries = Object.entries(params).filter(([, value]) => value !== "");
  return entries.length ? `?${new URLSearchParams(entries).toString()}` : "";
}

export function fetchProjects(): Promise<Project[]> {
  return req<Project[]>("/api/projects");
}

export function fetchProject(id: string): Promise<Project> {
  return req<Project>(`/api/projects/${encodeURIComponent(id)}`);
}

export function createProject(input: CreateProjectInput): Promise<Project> {
  return req<Project>("/api/projects", { method: "POST", body: JSON.stringify(input) });
}

/** PATCH /api/projects/{id} — currently just the company axis (org_company_id).
 * Accepts an org_companies id OR slug; server resolves+stores the id. `null`
 * clears the assignment. See omniagentos/api/routes/projects.py:reparent_project. */
export function patchProject(
  id: string,
  input: { org_company_id?: string | null },
): Promise<Project> {
  return req<Project>(`/api/projects/${encodeURIComponent(id)}`, {
    method: "PATCH",
    body: JSON.stringify(input),
  });
}

/** Project-scoped reads for the board/runs/approvals views. These reuse the
 * existing endpoints' optional project_id query parameter. */
export function fetchProjectTasks(projectId: string): Promise<TaskRow[]> {
  return req<TaskRow[]>(`/api/tasks${qs({ project_id: projectId })}`);
}

export function fetchProjectRuns(projectId: string): Promise<RunSummary[]> {
  return req<RunSummary[]>(`/api/runs${qs({ project_id: projectId })}`);
}

export function fetchProjectApprovals(projectId: string, state = "pending"): Promise<Approval[]> {
  return req<Approval[]>(`/api/approvals${qs({ project_id: projectId, state })}`);
}

/** A project's live progress: tasks -> runs -> steps + a recent-activity tail.
 * See omniagentos/api/routes/projects.py:get_project_activity. */
export function fetchProjectActivity(projectId: string): Promise<ProjectActivity> {
  return req<ProjectActivity>(`/api/projects/${encodeURIComponent(projectId)}/activity`);
}

export function fetchProjectFiles(projectId: string): Promise<{ files: ProjectFile[] }> {
  return req<{ files: ProjectFile[] }>(
    `/api/projects/${encodeURIComponent(projectId)}/files`,
    undefined,
    true,
  );
}

export function uploadProjectFiles(
  projectId: string,
  files: File[],
  instructions = "",
): Promise<{ uploaded: Array<{ name: string; path: string }>; instructions_saved: boolean }> {
  const formData = new FormData();
  files.forEach((file) => formData.append("files", file));
  if (instructions.trim()) formData.append("instructions", instructions);
  return req(
    `/api/projects/${encodeURIComponent(projectId)}/files/upload`,
    { method: "POST", body: formData },
  );
}

/** A safe, server-validated file URL. `path` is always project-relative. */
export function projectFileUrl(projectId: string, path: string, download = false): string {
  const query = download ? "?download=true" : "";
  return `/api/projects/${encodeURIComponent(projectId)}/files/${path.split("/").map(encodeURIComponent).join("/")}${query}`;
}

export async function fetchProjectFileText(projectId: string, path: string): Promise<string> {
  return req<string>(
    projectFileUrl(projectId, path),
    undefined,
    true,
    (response) => response.text(),
  );
}
