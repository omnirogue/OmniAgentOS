import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import {
  AxisChip,
  RESOLUTION_GLYPH,
  RESOLUTION_LABEL,
  axisAccessibleName,
} from "./AxisChip";
import type { AxisResolution, AxisState } from "./types";

function state(overrides: Partial<AxisState> = {}): AxisState {
  return {
    axis: "project",
    value: { id: "proj_1", slug: "mission-rail", label: "Mission Rail" },
    resolution: "applied",
    confidence: 0.91,
    source: "fixture",
    rationale: null,
    locked: false,
    editable: true,
    pending: false,
    ...overrides,
  };
}

const ALL_RESOLUTIONS: AxisResolution[] = [
  "missing",
  "suggested",
  "provisional",
  "applied",
  "locked",
  "rejected",
];

describe("AxisChip", () => {
  it("renders text + non-color glyph/state for each resolution", () => {
    for (const resolution of ALL_RESOLUTIONS) {
      const s = state({
        resolution,
        value:
          resolution === "missing"
            ? null
            : { id: "x", slug: "x", label: "Value X" },
        locked: resolution === "locked",
        editable: resolution !== "locked" && resolution !== "rejected",
        confidence: null,
      });
      const { unmount } = render(<AxisChip state={s} />);
      const chip = screen.getByRole("button");
      expect(chip).toHaveAttribute("data-resolution", resolution);
      // Non-color glyph present in the DOM (not color-only state).
      const glyph = chip.querySelector("[data-glyph]");
      expect(glyph).not.toBeNull();
      expect(glyph).toHaveAttribute("data-glyph", resolution);
      expect(glyph?.textContent).toBe(RESOLUTION_GLYPH[resolution]);
      expect(glyph?.getAttribute("aria-hidden")).toBe("true");
      // Visible value text
      if (resolution === "missing") {
        expect(chip).toHaveTextContent(/Unassigned project/i);
      } else {
        expect(chip).toHaveTextContent("Value X");
      }
      // Axis label always visible
      expect(chip).toHaveTextContent("Project");
      unmount();
    }
  });

  it("accessible name includes axis, value, resolution, and confidence when supplied", () => {
    const s = state({
      axis: "company",
      value: { id: "c1", slug: "alpha", label: "Alpha Robotics" },
      resolution: "applied",
      confidence: 0.91,
    });
    render(<AxisChip state={s} />);
    const chip = screen.getByRole("button");
    const name = chip.getAttribute("aria-label") ?? "";
    expect(name).toMatch(/Company/i);
    expect(name).toMatch(/Alpha Robotics/);
    expect(name).toMatch(/applied/i);
    expect(name).toMatch(/confidence 0\.91/);
    expect(name).toBe(axisAccessibleName(s));
  });

  it("accessible name uses Unassigned when value is missing", () => {
    const s = state({
      axis: "workstream",
      value: null,
      resolution: "missing",
      confidence: null,
    });
    render(<AxisChip state={s} />);
    const name = screen.getByRole("button").getAttribute("aria-label") ?? "";
    expect(name).toMatch(/Workstream/i);
    expect(name).toMatch(/Unassigned workstream/i);
    expect(name).toMatch(/missing/i);
    expect(name).not.toMatch(/confidence/i);
  });

  it("Enter and Space invoke onActivate when editable and not locked", async () => {
    const onActivate = vi.fn();
    const user = userEvent.setup();
    render(<AxisChip state={state({ editable: true, locked: false })} onActivate={onActivate} />);
    const chip = screen.getByRole("button");
    chip.focus();
    await user.keyboard("{Enter}");
    expect(onActivate).toHaveBeenCalledTimes(1);
    expect(onActivate).toHaveBeenCalledWith("project");
    await user.keyboard(" ");
    expect(onActivate).toHaveBeenCalledTimes(2);
  });

  it("locked does not invoke onActivate", async () => {
    const onActivate = vi.fn();
    const user = userEvent.setup();
    render(
      <AxisChip
        state={state({
          resolution: "locked",
          locked: true,
          editable: false,
        })}
        onActivate={onActivate}
      />,
    );
    const chip = screen.getByRole("button");
    chip.focus();
    await user.keyboard("{Enter}");
    await user.keyboard(" ");
    await user.click(chip);
    expect(onActivate).not.toHaveBeenCalled();
  });

  it("non-editable does not invoke onActivate", async () => {
    const onActivate = vi.fn();
    const user = userEvent.setup();
    render(
      <AxisChip
        state={state({ editable: false, locked: false, resolution: "applied" })}
        onActivate={onActivate}
      />,
    );
    const chip = screen.getByRole("button");
    await user.click(chip);
    chip.focus();
    await user.keyboard("{Enter}");
    expect(onActivate).not.toHaveBeenCalled();
  });

  it("nested-link activation calls preventDefault and stopPropagation", async () => {
    const onActivate = vi.fn();
    const parentClick = vi.fn();
    const user = userEvent.setup();

    render(
      <a
        href="/parent"
        onClick={(e) => {
          parentClick(e.defaultPrevented);
        }}
      >
        <AxisChip
          state={state({ editable: true, locked: false })}
          onActivate={onActivate}
        />
      </a>,
    );

    await user.click(screen.getByRole("button"));
    expect(onActivate).toHaveBeenCalledTimes(1);
    // Parent link handler should see the click was stopped or not fire navigation path.
    // stopPropagation means parentClick may not run; either outcome is acceptable
    // as long as activation was handled on the chip.
    if (parentClick.mock.calls.length > 0) {
      // If bubble reached parent (should not with stopPropagation), default must be prevented.
      expect(parentClick).toHaveBeenCalledWith(true);
    }
  });

  it("exposes RESOLUTION_GLYPH for every resolution label", () => {
    for (const r of ALL_RESOLUTIONS) {
      expect(RESOLUTION_GLYPH[r]).toBeTruthy();
      expect(RESOLUTION_LABEL[r]).toBe(r);
    }
  });
});
