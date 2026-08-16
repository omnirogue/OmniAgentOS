/**
 * L7 decisive acceptance: review-queue UI distinguishes three API states.
 *
 * Governing rule: unknown/absent/unparseable must never render as good.
 * A missing store ({error: store_unavailable}) must NEVER look like an empty
 * queue ({pending: 0}) — that exact collapse was fixed twice already.
 *
 * Decisive (one test per state):
 *   - pending N  → non-empty queue presentation
 *   - pending 0  → empty-queue presentation
 *   - store_unavailable → explicit error presentation
 *
 * Named counterfeits that must fail if reintroduced:
 *   - error response rendering as empty queue
 *   - unrecognised candidate status with success styling
 *
 * Revert-check: collapse the error branch into the empty branch → the
 * store_unavailable test fails (run manually by flipping the guard).
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { CandidateStatusBadge } from "./CandidateStatusBadge";
import { ReviewQueue } from "./ReviewQueue";
import type { MemlifeQueueView } from "./types";

function view(state: MemlifeQueueView) {
  return render(<ReviewQueue state={state} />);
}

describe("Memlife review queue — three distinct states", () => {
  it("pending N renders a non-empty queue (not empty, not store-unavailable)", () => {
    view({ kind: "ok", pending: 3 });

    // Distinct non-empty signal
    expect(screen.getByTestId("memlife-queue-pending")).toBeInTheDocument();
    expect(screen.getByText(/3 candidates? awaiting review/i)).toBeInTheDocument();
    expect(screen.getByText(/pending:\s*3/i)).toBeInTheDocument();

    // Must not collapse into either of the other two states
    expect(screen.queryByTestId("memlife-queue-empty")).not.toBeInTheDocument();
    expect(screen.queryByTestId("memlife-queue-store-unavailable")).not.toBeInTheDocument();
    expect(screen.queryByText(/store unavailable/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/no candidates pending/i)).not.toBeInTheDocument();
  });

  it("pending 0 renders the empty queue (not pending-N, not store-unavailable)", () => {
    view({ kind: "ok", pending: 0 });

    expect(screen.getByTestId("memlife-queue-empty")).toBeInTheDocument();
    expect(screen.getByText(/no candidates pending/i)).toBeInTheDocument();

    // Must not look like work to do or a broken store
    expect(screen.queryByTestId("memlife-queue-pending")).not.toBeInTheDocument();
    expect(screen.queryByTestId("memlife-queue-store-unavailable")).not.toBeInTheDocument();
    expect(screen.queryByText(/store unavailable/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/awaiting review/i)).not.toBeInTheDocument();
  });

  it("store_unavailable renders a distinct error — never an empty queue", () => {
    view({ kind: "store_unavailable" });

    const root = screen.getByTestId("memlife-queue-store-unavailable");
    expect(root).toBeInTheDocument();
    // ErrorState uses role="alert" — store-unavailable is an alert, never status/empty.
    expect(screen.getByRole("alert")).toBeInTheDocument();
    expect(screen.getByText(/store unavailable/i)).toBeInTheDocument();

    // COUNTERFEIT that must fail: error rendered as empty queue
    expect(screen.queryByTestId("memlife-queue-empty")).not.toBeInTheDocument();
    expect(screen.queryByText(/no candidates pending/i)).not.toBeInTheDocument();
    expect(screen.queryByTestId("memlife-queue-pending")).not.toBeInTheDocument();
  });
});

describe("Memlife review queue — named counterfeits", () => {
  it("counterfeit: store_unavailable must not share empty-queue markers", () => {
    view({ kind: "store_unavailable" });

    // If someone collapses error → empty, these empty markers appear and this fails.
    expect(screen.queryByTestId("memlife-queue-empty")).toBeNull();
    expect(screen.queryByText(/no candidates pending/i)).toBeNull();
    expect(screen.queryByText(/queue is empty/i)).toBeNull();
    expect(screen.getByRole("alert")).toHaveTextContent(/store unavailable/i);
  });

  it("counterfeit: unrecognised candidate status must not use success styling", () => {
    const { container } = render(<CandidateStatusBadge status="cancelled" />);

    expect(screen.getByText("UNKNOWN")).toBeInTheDocument();
    // Success tones used elsewhere for completed/graduated/ok/win — forbidden here
    const badge = container.querySelector(".ds-badge");
    expect(badge).toBeTruthy();
    const className = badge!.className;
    expect(className).not.toMatch(/ds-badge--(completed|ok|win|champion|promote)/);
    expect(className).toMatch(/ds-badge--(warn|neutral|invalid|danger)/);
  });

  it("missing / empty candidate status renders UNKNOWN, never success", () => {
    const { rerender, container } = render(<CandidateStatusBadge status={null} />);
    expect(screen.getByText("UNKNOWN")).toBeInTheDocument();
    expect(container.querySelector(".ds-badge")!.className).not.toMatch(
      /ds-badge--(completed|ok|win)/,
    );

    rerender(<CandidateStatusBadge status={undefined} />);
    expect(screen.getByText("UNKNOWN")).toBeInTheDocument();

    rerender(<CandidateStatusBadge status="" />);
    expect(screen.getByText("UNKNOWN")).toBeInTheDocument();
  });

  it("known graduated status may use success tone; staged does not", () => {
    const { rerender, container } = render(<CandidateStatusBadge status="graduated" />);
    expect(screen.getByText("GRADUATED")).toBeInTheDocument();
    expect(container.querySelector(".ds-badge")!.className).toMatch(/ds-badge--completed/);

    rerender(<CandidateStatusBadge status="staged" />);
    expect(screen.getByText("STAGED")).toBeInTheDocument();
    expect(container.querySelector(".ds-badge")!.className).not.toMatch(
      /ds-badge--(completed|ok|win)/,
    );
  });
});
