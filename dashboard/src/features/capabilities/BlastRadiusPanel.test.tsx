import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { GrantSummary } from "./types";
import { BlastRadiusPanel } from "./BlastRadiusPanel";

const irreversibleSummary: GrantSummary = {
  total: 1,
  by_action_class: { irreversible: 1 },
  connectors: ["dangerous-tools"],
  groups: ["danger"],
  auto_writes: [],
  needs_approval: [],
  always_human: ["dangerous.delete"],
  touches_danger_group: [],
  not_yet_callable: [],
  env_names: [],
};

describe("BlastRadiusPanel irreversible grants", () => {
  it("shows an irreversible grant in the active Always human row", () => {
    render(<BlastRadiusPanel summary={irreversibleSummary} groupLabels={{}} />);

    const humanRow = screen.getByRole("alert");
    expect(within(humanRow).getByText("Always human")).toBeInTheDocument();
    expect(within(humanRow).getByText("1")).toBeInTheDocument();
    expect(humanRow).toHaveTextContent(
      "1 capability can move money, spend ad budget, or touch infrastructure",
    );
    expect(screen.getByText("Irreversible")).toBeInTheDocument();
  });
});
