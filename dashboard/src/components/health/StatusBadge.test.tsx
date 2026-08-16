import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { STATUS_TONE, StatusBadge } from "./StatusBadge";
import type { CapabilityStatus } from "./types";

const ALL_STATUSES: CapabilityStatus[] = ["OK", "DEGRADED", "DOWN", "CANNOT_EVALUATE", "UNVERIFIED", "STALE"];

describe("StatusBadge", () => {
  it("renders every status with its own distinct tone — only OK gets the green 'ok' tone", () => {
    for (const status of ALL_STATUSES) {
      if (status === "OK") {
        expect(STATUS_TONE[status]).toBe("ok");
      } else {
        expect(STATUS_TONE[status]).not.toBe("ok");
      }
    }
  });

  it("UNVERIFIED and CANNOT_EVALUATE never share a tone with OK, with each other, or with DOWN/DEGRADED", () => {
    const distinctTones = new Set(ALL_STATUSES.map((status) => STATUS_TONE[status]));
    expect(distinctTones.size).toBe(ALL_STATUSES.length);
  });

  it("renders the UNVERIFIED badge with the ds-badge--challenger class, never ds-badge--ok", () => {
    render(<StatusBadge status="UNVERIFIED" />);
    const badge = screen.getByText("Unverified");
    expect(badge.className).toContain("ds-badge--challenger");
    expect(badge.className).not.toContain("ds-badge--ok");
  });

  it("renders the CANNOT_EVALUATE badge distinctly from OK and DOWN", () => {
    render(<StatusBadge status="CANNOT_EVALUATE" />);
    const badge = screen.getByText("Cannot evaluate");
    expect(badge.className).not.toContain("ds-badge--ok");
    expect(badge.className).not.toContain("ds-badge--danger");
  });

  it("renders OK with the green tone", () => {
    render(<StatusBadge status="OK" />);
    expect(screen.getByText("OK").className).toContain("ds-badge--ok");
  });
});
