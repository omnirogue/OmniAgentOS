/**
 * API client for the nightly self-learning reflection loop.
 */

import { API_BASE } from "../../lib/contracts";
import { fetchWithTimeout } from "../../lib/fetchTimeout";

export interface ApiReflectionProposal {
  id: string;
  kind: string;
  target: unknown;
  current: unknown;
  proposed: unknown;
  rationale: string;
  evidence_refs: string[];
  predicted_impact: string | null;
  risk_class: string;
  status: string;
  created_at: string;
  updated_at: string;
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const url = `${API_BASE}${path}`;
  const response = await fetchWithTimeout(url, {
    cache: "no-store",
    ...options,
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
      ...(options?.headers || {}),
    },
  });

  if (!response.ok) {
    let errMsg = `Request failed with status ${response.status}`;
    try {
      const errBody = await response.json() as Record<string, unknown>;
      if (errBody && errBody.detail) {
        errMsg = String(errBody.detail);
      } else if (errBody && errBody.error && typeof errBody.error === "object" && errBody.error !== null) {
        const errorObj = errBody.error as Record<string, unknown>;
        if (typeof errorObj.message === "string") {
          errMsg = errorObj.message;
        }
      }
    } catch {
      // ignore
    }
    throw new Error(errMsg);
  }

  return response.json() as Promise<T>;
}

export async function fetchReflectionProposals(status?: string): Promise<ApiReflectionProposal[]> {
  const query = status ? `?status=${encodeURIComponent(status)}` : "";
  return request<ApiReflectionProposal[]>(`/api/reflection/proposals${query}`);
}

export async function approveReflectionProposal(id: string): Promise<unknown> {
  return request<unknown>(`/api/reflection/${encodeURIComponent(id)}/approve`, {
    method: "POST",
    body: JSON.stringify({ decided_by: "human" }),
  });
}

export async function rejectReflectionProposal(id: string): Promise<unknown> {
  return request<unknown>(`/api/reflection/${encodeURIComponent(id)}/reject`, {
    method: "POST",
    body: JSON.stringify({ decided_by: "human" }),
  });
}
