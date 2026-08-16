import { API_BASE } from "@/lib/contracts";

export interface MemoryRecord {
  id: string;
  type: string;
  statement: string;
  promotion_status: string;
  confidence: number;
  evidence: string[];
  applicability: Record<string, unknown>;
  helpfulness_score: number;
  created_at: string;
  updated_at: string;
}

export interface ArtifactEnvelope {
  id: string;
  artifact_type: string;
  task_id?: string;
  run_id?: string;
  format: string;
  content_uri: string;
  created_at: string;
}

export async function fetchMemories(status?: string): Promise<MemoryRecord[]> {
  const url = `${API_BASE}/api/metacog/memory${status ? `?promotion_status=${status}` : ""}`;
  const res = await fetch(url, { cache: "no-store" });
  if (!res.ok) throw new Error(`Failed to fetch memories: ${res.statusText}`);
  const data = await res.json();
  return data.memories || [];
}

export async function promoteMemory(id: string, force = false): Promise<MemoryRecord> {
  const url = `${API_BASE}/api/metacog/memory/${id}/promote`;
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ force }),
  });
  if (!res.ok) {
    const errorData = await res.json().catch(() => ({}));
    const message = errorData?.error?.message || res.statusText;
    throw new Error(`Failed to promote memory: ${message}`);
  }
  return res.json();
}

export async function fetchArtifacts(taskId?: string): Promise<ArtifactEnvelope[]> {
  const url = `${API_BASE}/api/metacog/artifacts${taskId ? `?task_id=${taskId}` : ""}`;
  const res = await fetch(url, { cache: "no-store" });
  if (!res.ok) throw new Error(`Failed to fetch artifacts: ${res.statusText}`);
  const data = await res.json();
  return data.artifacts || [];
}
