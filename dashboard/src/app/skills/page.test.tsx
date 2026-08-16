import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import SkillsPage from "./page";

/**
 * OC11 (cross-lineage review round 3, 2026-08-14): `stale` was a write-only
 * field — the API sets it on the BLOCKER 2 deadline-fallback path, but
 * nothing rendered it, so a user looking at a stale-but-valid snapshot saw
 * an ordinary page with no signal that the live read had timed out.
 * Surfaced in the existing "Scanned" meta line — no banner redesign.
 */

const SCANNED_AT = "2026-08-14T12:00:00.000Z";

function payload(stale: boolean) {
  return {
    generatedAt: SCANNED_AT,
    library: { source: "live", error: null, skills: [], scannedAt: SCANNED_AT },
    repoSkills: { source: "live", error: null, skills: [] },
    dormant: { source: "live", error: null, seeds: [] },
    ...(stale ? { stale: true } : {}),
  };
}

describe("SkillsPage — stale marker surfaced in the Scanned meta line (OC11)", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("shows the staleness marker when the payload is stale", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(new Response(JSON.stringify(payload(true)), { status: 200 }))),
    );

    render(<SkillsPage />);

    const meta = await screen.findByText(/stale \(live read timed out\)/);
    expect(meta).toBeTruthy();
  });

  it("does not show the staleness marker for a normal, fresh payload", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(new Response(JSON.stringify(payload(false)), { status: 200 }))),
    );

    render(<SkillsPage />);

    const meta = await screen.findByText(/^Scanned /);
    expect(meta.textContent).not.toMatch(/stale/);
  });
});
