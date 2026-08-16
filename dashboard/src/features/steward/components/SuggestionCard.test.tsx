import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ToastProvider } from "../../../design";
import type { Suggestion } from "../types";
import { SuggestionCard } from "./SuggestionCard";

const irreversibleSuggestion: Suggestion = {
  id: "sug_irreversible",
  title: "Delete the production ledger",
  rationale: "The ledger is no longer needed.",
  evidence: [{ source: "test", note: "synthetic" }],
  proposed_plan: {
    prompt: "Delete the production ledger.",
    harness: "cli-claude",
    action_class: "irreversible",
  },
  risk_class: "irreversible",
  source: "test",
  goal_id: null,
  alert_id: null,
  outcome: {},
  state: "open",
  created_at: "2026-08-07T00:00:00Z",
  decided_at: null,
  decided_by: null,
  task_id: null,
  run_id: null,
};

function renderCard(onApprove = vi.fn().mockResolvedValue({})) {
  return render(
    <ToastProvider>
      <SuggestionCard
        suggestion={irreversibleSuggestion}
        onApprove={onApprove}
        onDismiss={vi.fn().mockResolvedValue(irreversibleSuggestion)}
      />
    </ToastProvider>,
  );
}

describe("SuggestionCard irreversible approval guard", () => {
  it("keeps irreversible approvals maximally gated and visibly dangerous", async () => {
    const onApprove = vi.fn().mockResolvedValue({});
    renderCard(onApprove);

    expect(screen.getByText("irreversible", { selector: "span.ds-badge" })).toHaveClass(
      "ds-badge--danger",
    );

    fireEvent.click(screen.getByRole("button", { name: "Approve" }));

    const dialog = screen.getByRole("dialog", { name: "Approve suggestion" });
    expect(within(dialog).getByText("irreversible", { selector: "span.ds-badge" })).toHaveClass(
      "ds-badge--danger",
    );
    expect(screen.getByRole("alert")).toHaveTextContent(
      "This action is irreversible and cannot be undone",
    );

    const approveButton = within(dialog).getByRole("button", { name: "Approve" });
    expect(approveButton).toBeDisabled();

    fireEvent.change(screen.getByLabelText("Your name (approved_by)"), {
      target: { value: "alice" },
    });
    expect(approveButton).toBeDisabled();

    fireEvent.change(
      screen.getByLabelText(`Type the suggestion title to confirm: "${irreversibleSuggestion.title}"`),
      { target: { value: irreversibleSuggestion.title } },
    );
    expect(approveButton).not.toBeDisabled();

    fireEvent.click(approveButton);
    await waitFor(() => expect(onApprove).toHaveBeenCalledWith("sug_irreversible", "alice"));
  });
});
