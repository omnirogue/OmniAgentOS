import { beforeEach, describe, expect, it, vi } from "vitest";

const { fetchWithTimeoutMock } = vi.hoisted(() => ({
  fetchWithTimeoutMock: vi.fn(),
}));

vi.mock("@/lib/fetchTimeout", () => ({
  fetchWithTimeout: fetchWithTimeoutMock,
}));

import { fetchWithTimeout } from "@/lib/fetchTimeout";
import { teamApi, TeamApiError } from "./client";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("teamApi request paths", () => {
  beforeEach(() => {
    vi.mocked(fetchWithTimeout).mockReset();
    vi.mocked(fetchWithTimeout).mockResolvedValue(jsonResponse({}));
  });

  it("GET /api/team/board with no owner", async () => {
    await teamApi.board();
    expect(fetchWithTimeout).toHaveBeenCalledWith("/api/team/board", expect.anything());
  });

  it("GET /api/team/board?owner= scopes to one employee", async () => {
    await teamApi.board("emp_owner");
    expect(fetchWithTimeout).toHaveBeenCalledWith("/api/team/board?owner=emp_owner", expect.anything());
  });

  it("GET /api/team/tree", async () => {
    await teamApi.tree();
    expect(fetchWithTimeout).toHaveBeenCalledWith("/api/team/tree", expect.anything());
  });

  it("POST /api/team/tasks/{id}/verify sends the verifier and encodes a weird id", async () => {
    await teamApi.verifyTask("weird/id?x=1", "operator");
    expect(fetchWithTimeout).toHaveBeenCalledWith(
      "/api/team/tasks/weird%2Fid%3Fx%3D1/verify",
      expect.objectContaining({ method: "POST", body: JSON.stringify({ verifier: "operator" }) }),
    );
  });

  it("POST /api/team/tasks/{id}/unverify sends the actor", async () => {
    await teamApi.unverifyTask("t1", "operator");
    expect(fetchWithTimeout).toHaveBeenCalledWith(
      "/api/team/tasks/t1/unverify",
      expect.objectContaining({ method: "POST", body: JSON.stringify({ actor: "operator" }) }),
    );
  });

  it("PATCH /api/team/evidence/{id} reattributes to a task", async () => {
    await teamApi.reattributeEvidence("tev_1", "t1", "operator");
    expect(fetchWithTimeout).toHaveBeenCalledWith(
      "/api/team/evidence/tev_1",
      expect.objectContaining({ method: "PATCH", body: JSON.stringify({ task_id: "t1", actor: "operator" }) }),
    );
  });

  it("PATCH /api/team/evidence/{id} with task_id: null marks mis-attributed", async () => {
    await teamApi.reattributeEvidence("tev_1", null, "operator");
    expect(fetchWithTimeout).toHaveBeenCalledWith(
      "/api/team/evidence/tev_1",
      expect.objectContaining({ body: JSON.stringify({ task_id: null, actor: "operator" }) }),
    );
  });

  it("GET /api/team/evidence/unattributed with a limit", async () => {
    await teamApi.unattributedEvidence(10);
    expect(fetchWithTimeout).toHaveBeenCalledWith("/api/team/evidence/unattributed?limit=10", expect.anything());
  });

  it("GET /api/team/scoreboard", async () => {
    await teamApi.scoreboard();
    expect(fetchWithTimeout).toHaveBeenCalledWith("/api/team/scoreboard", expect.anything());
  });

  it("GET /api/team/scoreboard?detail=1", async () => {
    await teamApi.scoreboard(true);
    expect(fetchWithTimeout).toHaveBeenCalledWith(
      "/api/team/scoreboard?detail=1",
      expect.anything(),
    );
  });
});

describe("teamApi error handling", () => {
  it("throws TeamApiError carrying the server's detail string on a 400", async () => {
    vi.mocked(fetchWithTimeout).mockResolvedValue(
      jsonResponse(
        { error: { code: "validation", message: "emp_bob cannot verify their own task" } },
        400,
      ),
    );
    await expect(teamApi.verifyTask("t1", "emp_bob")).rejects.toMatchObject({
      status: 400,
      message: "400: emp_bob cannot verify their own task",
    });
  });

  it("rejected promise is an instance of TeamApiError", async () => {
    vi.mocked(fetchWithTimeout).mockResolvedValue(jsonResponse({ error: { message: "not found" } }, 404));
    await expect(teamApi.evidence("missing")).rejects.toBeInstanceOf(TeamApiError);
  });
});
