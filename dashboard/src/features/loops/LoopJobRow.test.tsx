import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { SystemJob } from "@/features/routines/systemJobs";
import { LoopJobRow } from "./LoopJobRow";

const NOW = new Date("2026-08-15T12:00:00Z");

function job(overrides: Partial<SystemJob> = {}): SystemJob {
  return {
    key: "job",
    name: "Backlog executor",
    executor: "launchd",
    category: "Backlog",
    label: null,
    purpose: "does a thing",
    source: "scripts/x.sh",
    module: null,
    schedule: { kind: "interval", seconds: 60, description: "every minute" },
    env_overrides: [],
    loaded: true,
    plist_present: true,
    last_exit_status: 0,
    last_run_at: null,
    next_fire_at: null,
    health: "healthy",
    health_reason: "",
    managed_candidate: false,
    candidate_reason: "",
    ...overrides,
  };
}

describe("LoopJobRow last_result rendering", () => {
  it("renders the real value when last_result is a non-empty string", () => {
    render(<LoopJobRow job={job({ last_result: "exit 0, ok" })} now={NOW} />);
    expect(screen.getByText("Last result: exit 0, ok")).toBeInTheDocument();
  });

  it("renders a labelled, muted dash — never omits the row — when last_result is null", () => {
    render(<LoopJobRow job={job({ last_result: null })} now={NOW} />);
    const el = screen.getByText("Last result: —");
    expect(el).toBeInTheDocument();
    expect(el).toHaveAttribute("title", "No last result captured for this run.");
  });

  it("renders a distinct labelled dash when last_result is genuinely blank", () => {
    render(<LoopJobRow job={job({ last_result: "" })} now={NOW} />);
    const el = screen.getByText("Last result: —");
    expect(el).toHaveAttribute("title", "Last result was blank.");
  });

  it("renders a distinct labelled dash when last_result is absent entirely (older snapshot)", () => {
    render(<LoopJobRow job={job()} now={NOW} />);
    const el = screen.getByText("Last result: —");
    expect(el).toHaveAttribute("title", "This snapshot did not report a last result.");
  });
});

describe("LoopJobRow unknown executor", () => {
  it("never crashes on a hostile/arbitrary executor value and renders it generically", () => {
    render(<LoopJobRow job={job({ executor: "__proto__" })} now={NOW} />);
    expect(screen.getByText("proto")).toBeInTheDocument();
  });
});
