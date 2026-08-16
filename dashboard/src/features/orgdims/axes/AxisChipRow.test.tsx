import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { AxisChipRow } from "./AxisChipRow";
import {
  fixtureEmptyAxes,
  fixtureFullAppliedAxes,
  fixtureUnknownWorkKind,
} from "./fixtures";
import { AXIS_LABELS, AXIS_ORDER, type AxisKey, type AxisState } from "./types";

describe("AxisChipRow", () => {
  it("renders exactly four chips in stable order Company → Project → Workstream → Work kind", () => {
    render(<AxisChipRow axes={fixtureFullAppliedAxes()} />);
    const chips = screen.getAllByRole("button");
    expect(chips).toHaveLength(4);

    const axes = chips.map((c) => c.getAttribute("data-axis"));
    expect(axes).toEqual(["company", "project", "workstream", "work_kind"]);

    // Stable human labels present in order
    const labels = AXIS_ORDER.map((k) => AXIS_LABELS[k]);
    expect(labels).toEqual(["Company", "Project", "Workstream", "Work kind"]);
    for (const label of labels) {
      expect(screen.getByText(label)).toBeInTheDocument();
    }

    // Document order of axis labels matches AXIS_ORDER
    const texts = chips.map((c) => c.textContent ?? "");
    let lastIndex = -1;
    for (const label of labels) {
      const idx = texts.findIndex((t) => t.includes(label));
      expect(idx).toBeGreaterThan(lastIndex);
      lastIndex = idx;
    }
  });

  it("bare/missing record → four Unassigned labels (four_axes_explicit_unknown)", () => {
    render(<AxisChipRow axes={fixtureEmptyAxes()} />);
    const chips = screen.getAllByRole("button");
    expect(chips).toHaveLength(4);

    expect(screen.getByText(/Unassigned company/i)).toBeInTheDocument();
    expect(screen.getByText(/Unassigned project/i)).toBeInTheDocument();
    expect(screen.getByText(/Unassigned workstream/i)).toBeInTheDocument();
    expect(screen.getByText(/Unassigned work kind/i)).toBeInTheDocument();

    for (const chip of chips) {
      expect(chip).toHaveAttribute("data-resolution", "missing");
      expect(chip.querySelector("[data-unassigned='true']")).not.toBeNull();
    }
  });

  it("null/undefined axes still render four Unassigned chips", () => {
    const { rerender } = render(<AxisChipRow axes={null} />);
    expect(screen.getAllByRole("button")).toHaveLength(4);
    expect(screen.getAllByText(/Unassigned/i).length).toBeGreaterThanOrEqual(4);

    rerender(<AxisChipRow />);
    expect(screen.getAllByRole("button")).toHaveLength(4);
  });

  it("320px-safe markup: wrapping row, no fixed pixel width that clips", () => {
    const { container } = render(<AxisChipRow axes={fixtureFullAppliedAxes()} />);
    const row = container.querySelector("[data-axis-row='true']");
    expect(row).not.toBeNull();

    const style = (row as HTMLElement).getAttribute("style") ?? "";
    const className = (row as HTMLElement).className;

    // Layout is owned by the CSS module's fluid, wrapping `.row` rule.
    expect(className.length).toBeGreaterThan(0);
    expect(style).toBe("");

    // No fixed pixel width that would clip at 320px
    expect(style).not.toMatch(/width:\s*\d+px/);
    expect(style).not.toMatch(/min-width:\s*\d{3,}px/);
    expect((row as HTMLElement).getAttribute("data-axis-count")).toBe("4");
  });

  it("unknown work_kind legal_review renders verbatim (registry_not_hardcoded)", () => {
    // Option injected via state/registry — not a hardcoded component filter list.
    const { axes, registry } = fixtureUnknownWorkKind();
    render(<AxisChipRow axes={axes} registry={registry} />);

    expect(screen.getByText("legal_review")).toBeInTheDocument();
    const workKind = screen
      .getAllByRole("button")
      .find((b) => b.getAttribute("data-axis") === "work_kind");
    expect(workKind).toBeTruthy();
    expect(workKind?.textContent).toMatch(/legal_review/);
    // Source code of components must not contain a hardcoded allowlist that
    // would drop unknown slugs — rendering legal_review proves injection works.
  });

  it("partial record fills missing axes as Unassigned", () => {
    const partial: Partial<Record<AxisKey, AxisState>> = {
      company: {
        axis: "company",
        value: { id: "c", slug: "alpha", label: "Alpha" },
        resolution: "applied",
        confidence: null,
        source: null,
        rationale: null,
        locked: false,
        editable: true,
        pending: false,
      },
    };
    render(<AxisChipRow axes={partial} />);
    expect(screen.getAllByRole("button")).toHaveLength(4);
    expect(screen.getByText("Alpha")).toBeInTheDocument();
    expect(screen.getByText(/Unassigned project/i)).toBeInTheDocument();
    expect(screen.getByText(/Unassigned workstream/i)).toBeInTheDocument();
    expect(screen.getByText(/Unassigned work kind/i)).toBeInTheDocument();
  });
});
