import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it } from "vitest";
import { HealthFilters } from "./HealthFilters";
import { HealthTable } from "./HealthTable";
import { EMPTY_FILTERS, filterCapabilities } from "./logic";
import type { CapabilityHealth, HealthFiltersState } from "./types";

function makeCapability(overrides: Partial<CapabilityHealth>): CapabilityHealth {
  return {
    id: overrides.id ?? "cap",
    name: overrides.id ?? "cap",
    company: "estate",
    kind: "external-service",
    what_it_does: "",
    status: "OK",
    last_checked: null,
    last_good: null,
    metric: null,
    owner: "estate-ops",
    evidence: null,
    ...overrides,
  };
}

const FIXTURE: CapabilityHealth[] = [
  makeCapability({ id: "estate-ext", name: "estate-ext", company: "estate", kind: "external-service" }),
  makeCapability({ id: "estate-llm", name: "estate-llm", company: "estate", kind: "llm-loop" }),
  makeCapability({ id: "globex-ext", name: "globex-ext", company: "globex", kind: "external-service" }),
  makeCapability({ id: "globex-llm", name: "globex-llm", company: "globex", kind: "llm-loop" }),
];

/** Wires HealthFilters + filterCapabilities + HealthTable exactly as the
 * /health page does, so the filter test exercises the real composition path
 * (user picks a Select option -> filters state updates -> visible rows
 * narrow), not a standalone unit test of filterCapabilities alone. */
function FilteredTable() {
  const [filters, setFilters] = useState<HealthFiltersState>(EMPTY_FILTERS);
  const visible = filterCapabilities(FIXTURE, filters);
  return (
    <>
      <HealthFilters filters={filters} onChange={setFilters} />
      <HealthTable capabilities={visible} onSelect={() => {}} />
    </>
  );
}

async function selectOption(user: ReturnType<typeof userEvent.setup>, triggerLabel: string, optionLabel: string) {
  await user.click(screen.getByRole("button", { name: triggerLabel }));
  await user.click(screen.getByRole("option", { name: optionLabel }));
}

describe("HealthFilters composed with the table (company + kind narrow, and together)", () => {
  it("filtering by company alone narrows to that company's rows", async () => {
    const user = userEvent.setup();
    render(<FilteredTable />);

    await selectOption(user, "Filter by company", "Estate");

    expect(screen.getByText("estate-ext")).toBeInTheDocument();
    expect(screen.getByText("estate-llm")).toBeInTheDocument();
    expect(screen.queryByText("globex-ext")).not.toBeInTheDocument();
    expect(screen.queryByText("globex-llm")).not.toBeInTheDocument();
  });

  it("filtering by kind alone narrows to that kind's rows", async () => {
    const user = userEvent.setup();
    render(<FilteredTable />);

    await selectOption(user, "Filter by kind", "LLM loop");

    expect(screen.getByText("estate-llm")).toBeInTheDocument();
    expect(screen.getByText("globex-llm")).toBeInTheDocument();
    expect(screen.queryByText("estate-ext")).not.toBeInTheDocument();
    expect(screen.queryByText("globex-ext")).not.toBeInTheDocument();
  });

  it("company and kind filters compose: only rows matching BOTH survive", async () => {
    const user = userEvent.setup();
    render(<FilteredTable />);

    await selectOption(user, "Filter by company", "Estate");
    await selectOption(user, "Filter by kind", "LLM loop");

    expect(screen.getByText("estate-llm")).toBeInTheDocument();
    expect(screen.queryByText("estate-ext")).not.toBeInTheDocument();
    expect(screen.queryByText("globex-llm")).not.toBeInTheDocument();
    expect(screen.queryByText("globex-ext")).not.toBeInTheDocument();
  });
});
