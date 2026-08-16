import { describe, expect, it } from "vitest";
import {
  confirmationFriction,
  actionClassTone,
  isElevatedRisk,
  riskClassWarning,
  riskTone,
} from "./format";
import type { RiskClass } from "./types";

const irreversible = "irreversible" as RiskClass;
const unknownRiskClass = "something-unrecognized" as RiskClass;

describe("Steward risk display and confirmation defaults", () => {
  it("renders an empty action class with a danger tone", () => {
    expect(actionClassTone("")).toBe("danger");
  });

  it("renders an unrecognized action class with a danger tone", () => {
    expect(actionClassTone("bogus")).toBe("danger");
  });

  it("renders a read-only action class with an ok tone", () => {
    expect(actionClassTone("read_only")).toBe("ok");
  });

  it("renders irreversible risk with a danger tone", () => {
    expect(riskTone(irreversible)).toBe("danger");
  });

  it("renders unrecognized risk with a danger tone", () => {
    expect(riskTone(unknownRiskClass)).toBe("danger");
  });

  it("requires maximum confirmation friction for irreversible risk", () => {
    const friction = confirmationFriction(irreversible);
    expect(friction).toBe(confirmationFriction("consequential"));
    expect(friction).not.toBe("none");
    expect(isElevatedRisk(irreversible)).toBe(true);
  });

  it("requires maximum confirmation friction for unrecognized risk", () => {
    const friction = confirmationFriction(unknownRiskClass);
    expect(friction).toBe(confirmationFriction("consequential"));
    expect(friction).not.toBe("none");
    expect(isElevatedRisk(unknownRiskClass)).toBe(true);
  });

  it("shows an irreversible-risk warning", () => {
    expect(riskClassWarning(irreversible)).toEqual(expect.any(String));
  });

  it("shows a warning for unrecognized risk", () => {
    expect(riskClassWarning(unknownRiskClass)).toEqual(expect.any(String));
  });

  // Regression: the hardened "fail closed on unknown" defaults must not
  // cry-wolf on the two genuinely benign classes. A reflex click on every
  // read_only/sandboxed_creation card must remain frictionless — cry-wolf
  // desensitizes operators to real irreversible warnings (security-negative),
  // and a false "unrecognized risk class" banner on every benign card would
  // contradict DES-002's stated benign-tier behavior above.
  it("does not add friction or a warning to read_only suggestions", () => {
    expect(confirmationFriction("read_only")).toBe("none");
    expect(riskClassWarning("read_only")).toBeNull();
    expect(isElevatedRisk("read_only")).toBe(false);
  });

  it("does not add friction or a warning to sandboxed_creation suggestions", () => {
    expect(confirmationFriction("sandboxed_creation")).toBe("none");
    expect(riskClassWarning("sandboxed_creation")).toBeNull();
    expect(isElevatedRisk("sandboxed_creation")).toBe(false);
  });
});
