import { apiUrl } from "@/lib/apiRoute";
import { fetchWithTimeout } from "@/lib/fetchTimeout";
import { API_BASE } from "@/lib/contracts";
import type {
  TeamAccountabilityResponse,
  TeamEvidence,
  TeamEvent,
  TeamNlAssignResult,
  TeamScoreboardResponse,
  TeamTaskRow,
  TeamTree,
  VerifyTaskBody,
} from "./types";

export class TeamApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
    this.name = "TeamApiError";
  }
}

/** Same request/error-parsing shape as `features/collab/client.ts`'s `req` —
 * the FastAPI `/api/team/*` namespace answers `{"error":{"code","message",
 * "detail"}}` on failure (see `omniagentos.api.services.ApiError` +
 * `api_error_handler`), identical to the collab/board error envelope. Reads
 * AND mutations both go same-origin through the Next proxy (`apiUrl` drops
 * the FastAPI base unconditionally); the proxy attaches the session token —
 * `/api/team` is a wholly gated read namespace server-side
 * (`_GATED_READ_NAMESPACES` in omniagentos/api/main.py), so it also needs the
 * `team` prefix registered in the proxy's `AUTHORIZED_READ_PREFIXES` (see the
 * one-line addition in `app/api/[...path]/route.ts` — flagged as a deviation
 * in the P5 handoff: outside this package's literal ownership list, but the
 * whole feature 401s on every GET without it). */
async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetchWithTimeout(apiUrl(API_BASE, path, init?.method), {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    cache: "no-store",
  });
  if (!res.ok) {
    let detail = "";
    try {
      const body = (await res.json()) as { error?: { message?: string } };
      detail = body?.error?.message ?? JSON.stringify(body);
    } catch {
      detail = res.statusText;
    }
    throw new TeamApiError(`${res.status}: ${detail}`, res.status);
  }
  return (await res.json()) as T;
}

function teamPath(suffix: string): string {
  return `/api/team${suffix}`;
}

export const teamApi = {
  /** GET /api/team/board[?owner=] — every person's queue, or one person's. */
  board: (owner?: string) =>
    req<unknown>(teamPath(`/board${owner ? `?owner=${encodeURIComponent(owner)}` : ""}`)),

  /** GET /api/team/tree — company → goal → task → subtask. */
  tree: () => req<TeamTree>(teamPath("/tree")),

  /** POST /api/team/tasks/{id}/verify. Accepts either the legacy plain
   * verifier string (outcome defaults to "pass" server-side, byte-identical
   * to every caller from before migration 132) or a full `VerifyTaskBody` for
   * a fail verdict — `{verifier, outcome: "fail", reason}`, `reason` REQUIRED.
   * Throws `TeamApiError` on the self-verification / baseline-immutable /
   * missing-reason refusals — `.message` carries the server's detail string
   * verbatim (`"400: <detail>"`). */
  verifyTask: (taskId: string, body: string | VerifyTaskBody) =>
    req<TeamTaskRow>(teamPath(`/tasks/${encodeURIComponent(taskId)}/verify`), {
      method: "POST",
      body: JSON.stringify(typeof body === "string" ? { verifier: body } : body),
    }),

  /** POST /api/team/tasks/{id}/unverify. Baseline cards are immutable and
   * answer 400; other cards may be withdrawn by any named actor. */
  unverifyTask: (taskId: string, actor: string) =>
    req<TeamTaskRow>(teamPath(`/tasks/${encodeURIComponent(taskId)}/unverify`), {
      method: "POST",
      body: JSON.stringify({ actor }),
    }),

  /** GET /api/team/tasks/{id}/evidence — this card's attributed artifacts. */
  evidence: (taskId: string) =>
    req<TeamEvidence[]>(teamPath(`/tasks/${encodeURIComponent(taskId)}/evidence`)),

  /** GET /api/team/tasks/{id}/events — this card's append-only audit trail. */
  events: (taskId: string) =>
    req<TeamEvent[]>(teamPath(`/tasks/${encodeURIComponent(taskId)}/events`)),

  /** PATCH /api/team/evidence/{id} — reattribute (or clear, `task_id: null`
   * = "mark mis-attributed") one evidence row. */
  reattributeEvidence: (evidenceId: string, taskId: string | null, actor: string) =>
    req<TeamEvidence>(teamPath(`/evidence/${encodeURIComponent(evidenceId)}`), {
      method: "PATCH",
      body: JSON.stringify({ task_id: taskId, actor }),
    }),

  /** GET /api/team/evidence/unattributed[?limit=] — the reattribution inbox. */
  unattributedEvidence: (limit?: number) =>
    req<TeamEvidence[]>(teamPath(`/evidence/unattributed${limit ? `?limit=${limit}` : ""}`)),

  /** GET /api/team/scoreboard. The default is the thin dashboard contract;
   * `detail=1` adds each person's counted/excluded breakdown. */
  scoreboard: (detail = false) =>
    req<TeamScoreboardResponse>(teamPath(`/scoreboard${detail ? "?detail=1" : ""}`)),

  /** GET /api/team/accountability[?day=] — per-active-dev daily view
   * (migration 132, spec §8): commitments, done-today tri-state, blocked/
   * overdue, the improvement-of-day slot, learning captures, points pace.
   * `day` defaults server-side to today's LOCAL date when omitted. */
  accountability: (day?: string) =>
    req<TeamAccountabilityResponse>(
      teamPath(`/accountability${day ? `?day=${encodeURIComponent(day)}` : ""}`),
    ),

  /** POST /api/team/nl-assign — deterministic grammar, no model call. Checks
   * the PROPOSE shapes ("propose an automation to <title>") before the
   * ASSIGN shapes server-side, so the response is a discriminated union:
   * `TeamNlAssignProposal` (`kind: "automation_proposal"`, no owner —
   * `awaiting_approval` for the operator) or `TeamNlAssignAssignment` (an owner, no
   * `kind`). Throws `TeamApiError` on an unparseable sentence, unknown
   * teammate, or unknown `#company`/`#category` — `.message` carries the
   * server's help text verbatim. */
  nlAssign: (text: string) =>
    req<TeamNlAssignResult>(teamPath("/nl-assign"), {
      method: "POST",
      body: JSON.stringify({ text }),
    }),

  /** PATCH /api/collab/board/{id} — automation_maturity/automation_note are
   * the two migration-132 board_tasks columns that ARE directly patchable
   * (`CollabStore.update_board_task`'s allowlist; every other 131 column is
   * server-owned). This is a narrow, two-field helper for TaskOverview, not a
   * general board-task PATCH client (`features/collab/client.ts` owns none
   * for board-task field edits today, and that file is out of this
   * package's ownership). `automation_maturity: null` clears the field —
   * the vocabulary select's "—" option must translate its empty string to
   * `null` before calling this, or the server 400s on `""`. */
  updateTaskAutomation: (
    taskId: string,
    fields: { automation_maturity?: string | null; automation_note?: string | null },
  ) =>
    req<TeamTaskRow>(`/api/collab/board/${encodeURIComponent(taskId)}`, {
      method: "PATCH",
      body: JSON.stringify(fields),
    }),
};
