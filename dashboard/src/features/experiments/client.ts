import { fixtureChampions, fixtureExperiments, fixtureSurfaces } from "./fixtures";
import type {
  Champion,
  DisciplineSummary,
  DispositionRequest,
  Experiment,
  ExperimentDetail,
  ExperimentFilters,
  RunExperimentRequest,
  RunExperimentResponse,
  Surface,
  SurfaceDetail,
  SurfaceFilters,
  SurfaceKind,
} from "./types";
import { apiUrl } from "../../lib/apiRoute";
import { API_BASE } from "../../lib/contracts";
import { fetchWithTimeout } from "../../lib/fetchTimeout";

/** Showcase data is available only in non-production builds and must be explicitly enabled. */
export const USE_FIXTURES =
  process.env.NODE_ENV !== "production" && process.env.NEXT_PUBLIC_LAB_USE_FIXTURES === "true";

export class LabApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "LabApiError";
  }
}

function queryString(values: Record<string, string | undefined>): string {
  const params = new URLSearchParams();
  Object.entries(values).forEach(([key, value]) => {
    if (value) params.set(key, value);
  });
  const query = params.toString();
  return query ? `?${query}` : "";
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  // fix7: lab mutations (create experiment/tournament, run, disposition,
  // rollback, curate) go same-origin (token proxy); reads stay direct.
  const response = await fetchWithTimeout(apiUrl(API_BASE, `/api/lab${path}`, init?.method), {
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
  });
  if (!response.ok) {
    let detail = `Lab API returned ${response.status}`;
    try {
      const body = (await response.json()) as { detail?: string; message?: string };
      detail = body.detail ?? body.message ?? detail;
    } catch {
      // Preserve the status-based fallback for non-JSON error responses.
    }
    throw new LabApiError(detail, response.status);
  }
  return (await response.json()) as T;
}

const copy = <T,>(value: T): T => structuredClone(value);

export const labClient = {
  async getExperiments(filters: ExperimentFilters = {}): Promise<Experiment[]> {
    if (USE_FIXTURES) {
      return copy(fixtureExperiments).filter(
        (experiment) =>
          (!filters.discipline || experiment.discipline === filters.discipline) &&
          (!filters.status || experiment.status === filters.status),
      );
    }
    return request<Experiment[]>(`/experiments${queryString({ discipline: filters.discipline, status: filters.status })}`);
  },

  async getExperiment(id: string): Promise<ExperimentDetail> {
    if (USE_FIXTURES) {
      const experiment = fixtureExperiments.find((item) => item.id === id);
      if (!experiment) throw new LabApiError(`Experiment ${id} was not found`, 404);
      return copy(experiment);
    }
    return request<ExperimentDetail>(`/experiments/${encodeURIComponent(id)}`);
  },

  async runExperiment(id: string, body: RunExperimentRequest = {}): Promise<RunExperimentResponse> {
    if (USE_FIXTURES) return { status: "running_baseline" };
    return request<RunExperimentResponse>(`/experiments/${encodeURIComponent(id)}/run`, {
      method: "POST",
      body: JSON.stringify(body),
    });
  },

  async setDisposition(id: string, body: DispositionRequest): Promise<Experiment> {
    if (USE_FIXTURES) {
      const experiment = await this.getExperiment(id);
      return { ...experiment, disposition: body.decision, status: "decided" };
    }
    return request<Experiment>(`/experiments/${encodeURIComponent(id)}/disposition`, {
      method: "POST",
      body: JSON.stringify(body),
    });
  },

  async getSurfaces(filters: SurfaceFilters = {}): Promise<Surface[]> {
    if (USE_FIXTURES) {
      return copy(fixtureSurfaces).filter(
        (surface) =>
          (!filters.discipline || surface.discipline === filters.discipline) &&
          (!filters.kind || surface.kind === filters.kind),
      );
    }
    return request<Surface[]>(`/surfaces${queryString({ discipline: filters.discipline, kind: filters.kind })}`);
  },

  async getSurface(id: string): Promise<SurfaceDetail> {
    if (USE_FIXTURES) {
      const surface = fixtureSurfaces.find((item) => item.id === id);
      if (!surface) throw new LabApiError(`Surface ${id} was not found`, 404);
      return copy(surface);
    }
    return request<SurfaceDetail>(`/surfaces/${encodeURIComponent(id)}`);
  },

  async getChampions(discipline?: string): Promise<Champion[]> {
    if (USE_FIXTURES) {
      return copy(fixtureChampions).filter((item) => !discipline || item.discipline === discipline);
    }
    return request<Champion[]>(`/champions${queryString({ discipline })}`);
  },

  async getDisciplines(limit = 100): Promise<DisciplineSummary[]> {
    return request<DisciplineSummary[]>(`/disciplines?limit=${Math.max(1, Math.min(500, Math.floor(limit)))}`);
  },

  async rollbackChampion(discipline: string, kind: SurfaceKind): Promise<Champion> {
    if (USE_FIXTURES) {
      const champion = fixtureChampions.find(
        (item) => item.discipline === discipline && item.surface_kind === kind,
      );
      if (!champion) throw new LabApiError("No rollback target is registered", 404);
      return { ...copy(champion), surface_id: champion.rollback_to_surface_id ?? champion.surface_id };
    }
    return request<Champion>(
      `/champions/${encodeURIComponent(discipline)}/${encodeURIComponent(kind)}/rollback`,
      { method: "POST", body: "{}" },
    );
  },
};
