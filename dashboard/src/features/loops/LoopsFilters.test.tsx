import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { emptyFilterState } from "./filterSort";
import { LoopsFilters } from "./LoopsFilters";

describe("LoopsFilters accessible group semantics", () => {
  it("exposes the Category and Health chip toggles as labelled groups, not bare buttons in a div", () => {
    render(
      <LoopsFilters
        categories={["Backlog", "Comms"]}
        filter={emptyFilterState()}
        onFilterChange={vi.fn()}
        sort="default"
        onSortChange={vi.fn()}
      />,
    );
    const categoryGroup = screen.getByRole("group", { name: "Category" });
    const healthGroup = screen.getByRole("group", { name: "Health" });
    expect(categoryGroup).toBeInTheDocument();
    expect(healthGroup).toBeInTheDocument();
    // Every chip toggle for a category lives inside the labelled category
    // group (screen reader users get "Category, group" context, not a flat
    // list of unlabelled toggle buttons).
    expect(categoryGroup).toHaveTextContent("Backlog");
    expect(categoryGroup).toHaveTextContent("Comms");
  });
});
