import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { LedgerEvent } from "@/features/collab/types";
import { LedgerTimeline } from "./LedgerTimeline";

function event(overrides: Partial<LedgerEvent> = {}): LedgerEvent {
  return {
    v: 1,
    at: "2026-08-04T23:30:53.630089+00:00",
    ts: "2026-08-04T23:30:53.630089+00:00",
    session: "session-B-omniagentos",
    agent: "fable",
    event: "note",
    project: "t-smoke",
    summary: "a factual sentence",
    id: "a".repeat(64),
    ...overrides,
  };
}

describe("LedgerTimeline", () => {
  it("shows a loading skeleton while the ledger fetch is in flight", () => {
    render(<LedgerTimeline events={null} loading />);

    expect(screen.getByText("Loading ledger events")).toBeInTheDocument();
    expect(screen.queryByText(/^No ledger events/)).not.toBeInTheDocument();
    expect(screen.queryByText(/^Ledger unavailable/)).not.toBeInTheDocument();
  });

  it("shows the explicit empty state when there are no events (loaded, genuinely empty)", () => {
    render(<LedgerTimeline events={[]} />);

    expect(screen.getByText("No ledger events for this card.")).toBeInTheDocument();
  });

  it("shows an unavailable state on null (503/timeout) -- distinct from genuine-empty, never lied about as empty", () => {
    render(<LedgerTimeline events={null} />);

    expect(
      screen.getByText("Ledger unavailable — the session-ledger CLI did not answer."),
    ).toBeInTheDocument();
    expect(screen.queryByText("No ledger events for this card.")).not.toBeInTheDocument();
  });

  it("renders at · agent · event · summary for every row, in the order given (never re-sorted)", () => {
    const rows = [
      event({ id: "a".repeat(64), summary: "first", agent: "fable", event: "note" }),
      event({ id: "b".repeat(64), summary: "second", agent: "sol", event: "branch_ready" }),
    ];
    render(<LedgerTimeline events={rows} />);

    const firstRow = screen.getByText("first").closest("div");
    expect(firstRow).not.toBeNull();
    expect(firstRow).toHaveTextContent("fable");
    expect(firstRow).toHaveTextContent("note");

    const secondRow = screen.getByText("second").closest("div");
    expect(secondRow).not.toBeNull();
    expect(secondRow).toHaveTextContent("sol");
    expect(secondRow).toHaveTextContent("branch_ready");

    // DOM order mirrors array order (the CLI already sorts newest-last;
    // this component must never re-sort it).
    const summaries = screen.getAllByText(/^(first|second)$/).map((node) => node.textContent);
    expect(summaries).toEqual(["first", "second"]);
  });

  it("renders a correction attached to its target row, exactly as the CLI gives it", () => {
    const rows = [
      event({
        id: "a".repeat(64),
        summary: "original claim",
        _corrections: [
          { id: "c".repeat(64), summary: "actually it was X", at: "2026-08-04T12:00:00.000000+00:00" },
        ],
      }),
    ];
    render(<LedgerTimeline events={rows} />);

    const targetRow = screen.getByText("original claim").closest("div");
    const correctionText = screen.getByText("actually it was X");
    expect(targetRow).not.toBeNull();
    // The correction is nested inside the target row's DOM subtree, not a
    // separate top-level row -- "attached", not merely adjacent.
    expect(targetRow?.contains(correctionText)).toBe(true);
  });

  it("does not render a corrections block when a row has none", () => {
    render(<LedgerTimeline events={[event({ summary: "plain uncorrected row" })]} />);

    expect(screen.queryByText(/correction/i)).not.toBeInTheDocument();
  });

  it("renders the timestamp as a semantic <time> carrying the FULL ISO instant, year and seconds never lost", () => {
    const at = "2026-08-04T23:30:53.630089+00:00";
    render(<LedgerTimeline events={[event({ at, summary: "audited row" })]} />);

    const row = screen.getByText("audited row").closest("div");
    const timeEl = row?.querySelector("time");
    expect(timeEl).not.toBeNull();
    // The full-precision instant survives untouched in dateTime/title,
    // regardless of how the visible text rounds it for display.
    expect(timeEl).toHaveAttribute("dateTime", at);
    expect(timeEl).toHaveAttribute("title", at);
    // The VISIBLE text must also carry full precision -- year and seconds,
    // not abbreviated away (an audit trail that drops them is not
    // trustworthy).
    expect(timeEl?.textContent).toContain("2026");
    expect(timeEl?.textContent).toMatch(/:\d{2}:\d{2}/);
  });

  it("renders a correction's timestamp the same full-precision way", () => {
    const correctionAt = "2026-08-04T12:00:00.000000+00:00";
    render(
      <LedgerTimeline
        events={[
          event({
            summary: "original claim",
            _corrections: [{ id: "c".repeat(64), summary: "actually it was X", at: correctionAt }],
          }),
        ]}
      />,
    );

    const correctionText = screen.getByText("actually it was X");
    const correctionRow = correctionText.closest("div");
    const timeEl = correctionRow?.querySelector("time");
    expect(timeEl).toHaveAttribute("dateTime", correctionAt);
    expect(timeEl?.textContent).toContain("2026");
  });
});
