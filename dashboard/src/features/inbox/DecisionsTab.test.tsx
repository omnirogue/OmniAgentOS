import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { normalizeDecision, NO_RECOMMENDATION_SENTINEL } from "../decisions";
import type { UseDecisions } from "../decisions";
import { groupDecisions } from "../decisions";
import { DecisionsTab } from "./DecisionsTab";

function feed(decisions: ReturnType<typeof normalizeDecision>[], decide = vi.fn()): UseDecisions {
  const groups = groupDecisions(decisions);
  return {
    decisions,
    groups,
    badgeCount: groups.urgent.length + groups.needsOwner.length,
    loading: false,
    error: null,
    deciding: false,
    refresh: vi.fn(),
    decide,
  };
}

describe("DecisionsTab", () => {
  it("renders urgent + needs-me queues and the recommended action", () => {
    render(
      <DecisionsTab
        feed={feed([
          normalizeDecision({
            id: "u",
            title: "Stripe payment failing",
            classification: "urgent",
            recommended: { human_line: "Update the card on Stripe now" },
            available_actions: ["dismiss"],
          }),
        ])}
      />,
    );
    expect(screen.getByText("Stripe payment failing")).toBeInTheDocument();
    expect(screen.getByText("Update the card on Stripe now")).toBeInTheDocument();
  });

  it("shows the loud sentinel when a decision has no recommendation", () => {
    render(
      <DecisionsTab feed={feed([normalizeDecision({ id: "u", title: "No rec", classification: "urgent" })])} />,
    );
    expect(screen.getByText(NO_RECOMMENDATION_SENTINEL)).toBeInTheDocument();
  });

  it("keeps MAYBE in a collapsed section that expands on click", () => {
    render(
      <DecisionsTab
        feed={feed([normalizeDecision({ id: "m", title: "Cold sales email", classification: "maybe", available_actions: ["edit", "dismiss"] })])}
      />,
    );
    // Collapsed: the header is present but the card body is not rendered yet.
    const header = screen.getByRole("button", { name: /Maybe — 1 held back for review/ });
    expect(screen.queryByText("Cold sales email")).not.toBeInTheDocument();
    fireEvent.click(header);
    expect(screen.getByText("Cold sales email")).toBeInTheDocument();
  });

  it("runs the ONE decide mutation through the generic dialog", async () => {
    const decide = vi.fn().mockResolvedValue(normalizeDecision({ id: "u" }));
    render(
      <DecisionsTab
        feed={feed(
          [
            normalizeDecision({
              id: "u",
              title: "Dismiss me",
              classification: "urgent",
              available_actions: ["dismiss"],
            }),
          ],
          decide,
        )}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    fireEvent.click(screen.getByRole("button", { name: "Confirm" }));
    await waitFor(() => expect(decide).toHaveBeenCalledWith("u", { action: "dismiss" }));
  });

  it("renders an empty state when there is nothing to decide", () => {
    render(<DecisionsTab feed={feed([])} />);
    expect(screen.getByText("Nothing needs a decision right now.")).toBeInTheDocument();
  });
});
