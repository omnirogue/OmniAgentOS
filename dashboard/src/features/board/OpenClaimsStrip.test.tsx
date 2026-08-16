import { act, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { LedgerClaim } from "@/features/collab/types";
import { OpenClaimsStrip } from "./OpenClaimsStrip";

const { fetchLedgerClaims } = vi.hoisted(() => ({ fetchLedgerClaims: vi.fn() }));

vi.mock("@/features/collab/client", () => ({
  collabApi: { fetchLedgerClaims },
  CollabApiError: class CollabApiError extends Error {
    status: number;
    constructor(message: string, status: number) {
      super(message);
      this.status = status;
    }
  },
}));

function claim(overrides: Partial<LedgerClaim> = {}): LedgerClaim {
  return {
    project: "OmniAgentOS",
    surface: "dashboard",
    session: "session-B-omniagentos",
    agent: "fable",
    since: "2026-08-04T10:00:00.000000+00:00",
    lease_until: null,
    stale: false,
    id: "a".repeat(64),
    ...overrides,
  };
}

afterEach(() => {
  fetchLedgerClaims.mockReset();
  vi.useRealTimers();
});

describe("OpenClaimsStrip", () => {
  it("renders one chip per open claim as project/surface — session", async () => {
    fetchLedgerClaims.mockResolvedValue([claim()]);

    render(<OpenClaimsStrip />);

    await waitFor(() =>
      expect(screen.getByText("OmniAgentOS/dashboard — session-B-omniagentos")).toBeInTheDocument(),
    );
  });

  it("marks a stale claim", async () => {
    fetchLedgerClaims.mockResolvedValue([claim({ stale: true })]);

    render(<OpenClaimsStrip />);

    await waitFor(() =>
      expect(
        screen.getByText("OmniAgentOS/dashboard — session-B-omniagentos · stale"),
      ).toBeInTheDocument(),
    );
  });

  it("falls back to the project alone when the claim has no surface", async () => {
    fetchLedgerClaims.mockResolvedValue([claim({ surface: null })]);

    render(<OpenClaimsStrip />);

    await waitFor(() =>
      expect(screen.getByText("OmniAgentOS — session-B-omniagentos")).toBeInTheDocument(),
    );
  });

  it("renders a owner-held claim as held/redacted, never absent (pre-merge review decision)", async () => {
    // Called with no owner-related flag at all: the CLI's own default already
    // shows a owner-held surface (redacting only its attached correction
    // text) -- hiding it would present it as FREE. This component must
    // never filter a visibility:"owner" claim back out.
    fetchLedgerClaims.mockResolvedValue([
      claim({
        project: "Personal",
        surface: "taxes-2026",
        session: "human:owner",
        id: "b".repeat(64),
        visibility: "owner",
      }),
    ]);

    render(<OpenClaimsStrip />);

    await waitFor(() =>
      expect(screen.getByText("Personal/taxes-2026 — human:owner")).toBeInTheDocument(),
    );
  });

  it("renders nothing when there are no open claims", async () => {
    fetchLedgerClaims.mockResolvedValue([]);

    const { container } = render(<OpenClaimsStrip />);

    await waitFor(() => expect(fetchLedgerClaims).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });

  it("degrades to rendering nothing (never an error banner) when the ledger relay fails", async () => {
    fetchLedgerClaims.mockRejectedValue(new Error("503: ledger_unavailable"));

    const { container } = render(<OpenClaimsStrip />);

    await waitFor(() => expect(fetchLedgerClaims).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });

  it("polls once per 10s total, not per card", async () => {
    vi.useFakeTimers();
    fetchLedgerClaims.mockResolvedValue([claim()]);

    render(<OpenClaimsStrip />);
    // The initial refresh() call happens synchronously in the mount effect
    // (before its internal `await` yields), so the call count is already 1
    // the instant render() returns -- no waiting needed to observe it.
    expect(fetchLedgerClaims).toHaveBeenCalledTimes(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(9_999);
    });
    expect(fetchLedgerClaims).toHaveBeenCalledTimes(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });
    expect(fetchLedgerClaims).toHaveBeenCalledTimes(2);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    expect(fetchLedgerClaims).toHaveBeenCalledTimes(3);
  });
});
