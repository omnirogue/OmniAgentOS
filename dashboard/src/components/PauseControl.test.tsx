import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { pauseMock, setPauseMock, useEventChannelMock } = vi.hoisted(() => ({
  pauseMock: vi.fn(),
  setPauseMock: vi.fn(),
  useEventChannelMock: vi.fn(),
}));

vi.mock("../lib/api", () => ({
  api: {
    pause: pauseMock,
    setPause: setPauseMock,
  },
}));

vi.mock("../lib/pollWhenVisible", () => ({
  startVisibilityPoll: vi.fn(() => () => {}),
}));

vi.mock("../lib/useEventChannel", () => ({
  useEventChannel: useEventChannelMock,
}));

import { PauseControl } from "./PauseControl";

const running = {
  paused: false,
  reason: "",
  updated_at: "2026-08-09T12:00:00Z",
};

describe("PauseControl", () => {
  let lastEvent: Record<string, unknown> | null;

  beforeEach(() => {
    lastEvent = null;
    pauseMock.mockReset();
    pauseMock.mockResolvedValue(running);
    setPauseMock.mockReset();
    setPauseMock.mockResolvedValue(running);
    useEventChannelMock.mockImplementation(() => ({
      lastEvent,
      events: [],
      connected: true,
      error: false,
    }));
  });

  it("loads and renders the current running state", async () => {
    render(<PauseControl />);

    await waitFor(() => expect(pauseMock).toHaveBeenCalledOnce());
    expect(screen.getByText("Running")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Pause" })).toBeInTheDocument();
    expect(screen.getByRole("status", { name: "OK" })).toBeInTheDocument();
  });

  it("applies a pause.changed event and exposes Resume", async () => {
    const view = render(<PauseControl />);
    await waitFor(() => expect(pauseMock).toHaveBeenCalledOnce());

    lastEvent = {
      type: "pause.changed",
      ts: "2026-08-09T12:02:00Z",
      payload: { paused: true, reason: "Safety hold" },
    };
    view.rerender(<PauseControl />);

    expect(await screen.findByText("Paused — Safety hold")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Resume" })).toBeInTheDocument();
    expect(screen.getByRole("status", { name: "Paused" })).toBeInTheDocument();
  });

  it("sends an authenticated resume mutation with an empty reason", async () => {
    const view = render(<PauseControl />);
    await waitFor(() => expect(pauseMock).toHaveBeenCalledOnce());

    lastEvent = {
      type: "pause.changed",
      ts: "2026-08-09T12:02:00Z",
      payload: { paused: true, reason: "Safety hold" },
    };
    view.rerender(<PauseControl />);
    fireEvent.click(await screen.findByRole("button", { name: "Resume" }));

    await waitFor(() => expect(setPauseMock).toHaveBeenCalledWith(false, ""));
    expect(await screen.findByText("Running")).toBeInTheDocument();
  });
});
