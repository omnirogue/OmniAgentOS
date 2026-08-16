import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

/**
 * The page this panel serves promises "search across every note" — an
 * incomplete result must say so explicitly, not silently render partial
 * hits under that promise. Pins the incomplete-results banner end to end
 * from `searchVault`'s `{ hits, truncated }` result through to the DOM.
 */

const { searchVault } = vi.hoisted(() => ({ searchVault: vi.fn() }));

vi.mock("./api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api")>();
  return { ...actual, searchVault };
});

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}));

import { SearchPanel } from "./SearchPanel";

describe("SearchPanel truncated-result signal", () => {
  beforeEach(() => {
    searchVault.mockReset();
    vi.useRealTimers();
  });

  it("shows an incomplete-results banner when the search was truncated", async () => {
    searchVault.mockResolvedValue({
      hits: [{ path: "a.md", title: "A", type: "run", snippet: "…" }],
      truncated: true,
    });
    const user = userEvent.setup();
    render(<SearchPanel />);

    await user.type(screen.getByRole("combobox"), "needle");

    await waitFor(() => expect(searchVault).toHaveBeenCalledWith("needle"));
    await waitFor(() => expect(screen.getByText(/partial result/i)).toBeInTheDocument());
    // The hit is still shown -- truncated means "may be incomplete", not "empty".
    expect(screen.getByText("A")).toBeInTheDocument();
  });

  it("shows no incomplete-results banner for a complete search", async () => {
    searchVault.mockResolvedValue({
      hits: [{ path: "a.md", title: "A", type: "run", snippet: "…" }],
      truncated: false,
    });
    const user = userEvent.setup();
    render(<SearchPanel />);

    await user.type(screen.getByRole("combobox"), "needle");

    await waitFor(() => expect(searchVault).toHaveBeenCalledWith("needle"));
    await waitFor(() => expect(screen.getByText("A")).toBeInTheDocument());
    expect(screen.queryByText(/partial result/i)).not.toBeInTheDocument();
  });

  it("shows the banner even on a truncated-but-empty result", async () => {
    searchVault.mockResolvedValue({ hits: [], truncated: true });
    const user = userEvent.setup();
    render(<SearchPanel />);

    await user.type(screen.getByRole("combobox"), "needle");

    await waitFor(() => expect(searchVault).toHaveBeenCalledWith("needle"));
    await waitFor(() => expect(screen.getByText(/partial result/i)).toBeInTheDocument());
  });
});
