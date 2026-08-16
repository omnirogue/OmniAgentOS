import { describe, expect, it } from "vitest";
import {
  canDecideAgentRequest,
  DECIDABLE_AGENT_REQUEST_STATUS,
  isOpenAgentRequest,
  OPERATOR_DECIDED_BY,
} from "./orgActions";

describe("organization action visibility (H-17)", () => {
  it("only marks backend-valid pending requests as decidable", () => {
    expect(canDecideAgentRequest("pending")).toBe(true);
    expect(canDecideAgentRequest(DECIDABLE_AGENT_REQUEST_STATUS)).toBe(true);
  });

  it("hides approve/reject for designing, awaiting_approval, and decided states", () => {
    expect(canDecideAgentRequest("designing")).toBe(false);
    expect(canDecideAgentRequest("awaiting_approval")).toBe(false);
    expect(canDecideAgentRequest("approved")).toBe(false);
    expect(canDecideAgentRequest("rejected")).toBe(false);
    expect(canDecideAgentRequest(null)).toBe(false);
    expect(canDecideAgentRequest(undefined)).toBe(false);
    expect(canDecideAgentRequest("")).toBe(false);
  });

  it("lists open (non-terminal) statuses for the pending section without implying actions", () => {
    expect(isOpenAgentRequest("pending")).toBe(true);
    expect(isOpenAgentRequest("designing")).toBe(true);
    expect(isOpenAgentRequest("awaiting_approval")).toBe(true);
    expect(isOpenAgentRequest("rejected")).toBe(false);
  });

  it("uses the operator decided_by identity required by the backend body schema", () => {
    expect(OPERATOR_DECIDED_BY).toBe("operator");
  });
});
