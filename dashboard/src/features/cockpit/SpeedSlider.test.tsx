import { useState } from "react";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { SpeedSlider, type Speed } from "./SpeedSlider";

/** A tiny controlled host so interactions actually move the (controlled) slider. */
function Host({
  onSpeed,
  initialSpeed = "auto",
}: {
  onSpeed?: (s: Speed) => void;
  initialSpeed?: Speed;
}) {
  const [speed, setSpeed] = useState<Speed>(initialSpeed);
  return (
    <SpeedSlider
      speed={speed}
      onSpeedChange={(s) => { setSpeed(s); onSpeed?.(s); }}
    />
  );
}

describe("SpeedSlider", () => {
  it("defaults to Balanced", () => {
    render(<Host />);
    expect(screen.getByRole("radio", { name: "Balanced" })).toHaveAttribute("aria-checked", "true");
    expect(screen.getByRole("radio", { name: "Fastest" })).toHaveAttribute("aria-checked", "false");
    expect(screen.getByRole("radio", { name: "Max Quality" })).toHaveAttribute("aria-checked", "false");
  });

  it("is one labelled radiogroup — the old mode segment is gone", () => {
    render(<Host />);
    expect(screen.getByRole("radiogroup", { name: "Speed vs quality" })).toBeInTheDocument();
    expect(screen.getAllByRole("radiogroup")).toHaveLength(1);
    expect(screen.queryByRole("radio", { name: "Swarm" })).not.toBeInTheDocument();
    expect(screen.queryByRole("radio", { name: "Single Task" })).not.toBeInTheDocument();
  });

  it("selects a detent on click", async () => {
    const onSpeed = vi.fn();
    render(<Host onSpeed={onSpeed} />);
    const user = userEvent.setup();
    await user.click(screen.getByRole("radio", { name: "Max Quality" }));
    expect(onSpeed).toHaveBeenCalledWith("ultra");
    expect(screen.getByRole("radio", { name: "Max Quality" })).toHaveAttribute("aria-checked", "true");
  });

  it("moves with arrow keys (roving tabindex radiogroup)", async () => {
    const onSpeed = vi.fn();
    render(<Host onSpeed={onSpeed} />);
    const user = userEvent.setup();
    // The checked radio is the focus target (tabindex 0).
    screen.getByRole("radio", { name: "Balanced" }).focus();
    await user.keyboard("{ArrowRight}");
    expect(onSpeed).toHaveBeenLastCalledWith("ultra");
    await user.keyboard("{ArrowLeft}");
    expect(onSpeed).toHaveBeenLastCalledWith("auto");
    await user.keyboard("{Home}");
    expect(onSpeed).toHaveBeenLastCalledWith("fast");
    await user.keyboard("{End}");
    expect(onSpeed).toHaveBeenLastCalledWith("ultra");
  });

  it("clamps arrow-key movement at the ends", async () => {
    const onSpeed = vi.fn();
    render(<Host onSpeed={onSpeed} initialSpeed="fast" />);
    const user = userEvent.setup();
    screen.getByRole("radio", { name: "Fastest" }).focus();
    await user.keyboard("{ArrowLeft}");
    // Already leftmost — no change fires (commit is a no-op when value is equal).
    expect(onSpeed).not.toHaveBeenCalled();
  });

  it("only the checked radio is keyboard-tabbable", () => {
    render(<Host />);
    expect(screen.getByRole("radio", { name: "Balanced" })).toHaveAttribute("tabindex", "0");
    expect(screen.getByRole("radio", { name: "Fastest" })).toHaveAttribute("tabindex", "-1");
    expect(screen.getByRole("radio", { name: "Max Quality" })).toHaveAttribute("tabindex", "-1");
  });

  it("exposes each detent's caption as its accessible description", () => {
    render(<Host />);
    expect(screen.getByRole("radio", { name: "Balanced" })).toHaveAccessibleDescription(
      "Router picks models, tiers and solo-vs-swarm",
    );
  });
});
