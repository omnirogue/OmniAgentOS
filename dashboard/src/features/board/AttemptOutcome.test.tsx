import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type {
  BoardTaskSessions,
  LiveBoardTask,
  TaskAttempt,
} from "@/features/collab/types";
import { AttemptTimeline } from "./AttemptTimeline";
import { TaskOverview } from "./TaskOverview";

type AttemptFixture = TaskAttempt & { tier?: string | null };

function attempt(overrides: Partial<AttemptFixture> = {}): AttemptFixture {
  return {
    id: "swa_1",
    board_task_id: "btk_attempt_truth",
    seq: 1,
    session_id: "ses_1",
    harness: "grok",
    model: "grok-4.5",
    account_id: null,
    started_at: "2026-07-28T12:00:00Z",
    ended_at: "2026-07-28T12:01:00Z",
    end_reason: "review_denied",
    detail: "Acceptance not met: merge-base ancestor check fails.",
    tier: "simple",
    ...overrides,
  };
}

const task = {
  id: "btk_attempt_truth",
  title: "Attempt outcome truth",
  description: "",
  status: "blocked",
  work: null,
  pending_approval: null,
  park_state: null,
} as unknown as LiveBoardTask;

function sessionFixture(
  overrides: Record<string, unknown> = {},
): BoardTaskSessions {
  return {
    sessions: [
      {
        id: "ses_1",
        state: "completed",
        model: "grok-4.5",
        provider: "grok",
        title: null,
        created_at: "2026-07-28T12:00:00Z",
        updated_at: "2026-07-28T12:01:00Z",
        source: "swarm",
        end_reason: "review_denied",
        detail: "Acceptance not met: merge-base ancestor check fails.",
        tier: "standard",
        ...overrides,
      },
    ],
    orchestration: null,
    live_session_id: null,
  } as unknown as BoardTaskSessions;
}

describe("attempt count truth", () => {
  it("renders every recorded attempt and never the empty state", () => {
    const attempts = [
      attempt(),
      attempt({ id: "swa_2", seq: 2, model: "grok-4.5", tier: "standard" }),
      attempt({ id: "swa_3", seq: 3, model: "gpt-5.6-sol", tier: "complex" }),
    ];

    render(<AttemptTimeline attempts={attempts} />);

    expect(screen.getAllByText(/^Attempt #\d+$/)).toHaveLength(3);
    expect(screen.queryByText("Not attempted yet")).not.toBeInTheDocument();
    expect(
      screen.queryByText("No session attempts were recorded for this task."),
    ).not.toBeInTheDocument();
  });
});

describe("attempt outcome truth", () => {
  it("renders a review-denied attempt as DENIED with verdict and tier", () => {
    render(
      <AttemptTimeline
        attempts={[
          attempt({
            tier: "standard",
            detail: "Acceptance not met: HEAD is not based on the required task branch.",
          }),
        ]}
      />,
    );

    expect(screen.getByText("DENIED")).toBeInTheDocument();
    expect(screen.queryByText(/^COMPLETED$/i)).not.toBeInTheDocument();
    expect(
      screen.getByText(
        "Acceptance not met: HEAD is not based on the required task branch.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("standard")).toBeInTheDocument();
  });

  it("renders a terminal attempt with no end_reason as UNKNOWN, never success", () => {
    render(
      <AttemptTimeline
        attempts={[
          attempt({
            end_reason: null,
            detail: "",
          }),
        ]}
      />,
    );

    expect(screen.getByText("UNKNOWN")).toBeInTheDocument();
    expect(screen.queryByText(/^COMPLETED$/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/^ACCEPTED$/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/^IN PROGRESS$/i)).not.toBeInTheDocument();
  });

  it("does not confuse a cleanly exited session with its denied outcome", () => {
    render(
      <TaskOverview
        task={task}
        sessions={sessionFixture()}
        longhaul={null}
        eta={null}
      />,
    );

    expect(screen.getByText("DENIED")).toBeInTheDocument();
    expect(screen.queryByText(/^COMPLETED$/i)).not.toBeInTheDocument();
    expect(
      screen.getByText("Acceptance not met: merge-base ancestor check fails."),
    ).toBeInTheDocument();
    expect(screen.getByText("standard")).toBeInTheDocument();
  });

  it("shows UNKNOWN when a completed swarm session has no attempt end_reason", () => {
    render(
      <TaskOverview
        task={task}
        sessions={sessionFixture({ end_reason: null, detail: "" })}
        longhaul={null}
        eta={null}
      />,
    );

    expect(screen.getByText("UNKNOWN")).toBeInTheDocument();
    expect(screen.queryByText(/^COMPLETED$/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/^ACCEPTED$/i)).not.toBeInTheDocument();
  });
});
