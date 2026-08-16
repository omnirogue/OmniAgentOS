import { describe, it, expect, vi, beforeEach } from "vitest";
import { fetchMemories, promoteMemory, fetchArtifacts } from "./api";
import { API_BASE } from "../../lib/contracts";

global.fetch = vi.fn();

describe("Memory Feature API", () => {
  beforeEach(() => {
    vi.resetAllMocks();
  });

  it("fetchMemories queries correct endpoint and returns memories", async () => {
    const mockMemories = [{ id: "mem_1", statement: "Test statement", promotion_status: "promoted" }];
    vi.mocked(fetch).mockResolvedValue({
      ok: true,
      json: async () => ({ memories: mockMemories }),
    } as Response);

    const res = await fetchMemories("promoted");
    expect(fetch).toHaveBeenCalledWith(
      `${API_BASE}/api/metacog/memory?promotion_status=promoted`,
      { cache: "no-store" }
    );
    expect(res).toEqual(mockMemories);
  });

  it("promoteMemory queries correct endpoint and handles success", async () => {
    const mockRecord = { id: "mem_1", promotion_status: "promoted" };
    vi.mocked(fetch).mockResolvedValue({
      ok: true,
      json: async () => mockRecord,
    } as Response);

    const res = await promoteMemory("mem_1", true);
    expect(fetch).toHaveBeenCalledWith(
      `${API_BASE}/api/metacog/memory/mem_1/promote`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ force: true }),
      }
    );
    expect(res).toEqual(mockRecord);
  });

  it("fetchArtifacts queries correct endpoint and returns artifacts", async () => {
    const mockArtifacts = [{ id: "art_1", artifact_type: "code_diff" }];
    vi.mocked(fetch).mockResolvedValue({
      ok: true,
      json: async () => ({ artifacts: mockArtifacts }),
    } as Response);

    const res = await fetchArtifacts();
    expect(fetch).toHaveBeenCalledWith(
      `${API_BASE}/api/metacog/artifacts`,
      { cache: "no-store" }
    );
    expect(res).toEqual(mockArtifacts);
  });
});
