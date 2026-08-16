import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { WorkFolderSelect, flattenWorkFolders, resetWorkFolderCache } from "./WorkFolderSelect";

const { fetchWorkFolderTree } = vi.hoisted(() => ({ fetchWorkFolderTree: vi.fn() }));
vi.mock("./chatApi", () => ({ fetchWorkFolderTree }));

const NESTED = {
  root: "/work",
  max_depth: 3,
  entries: [
    {
      name: "Acme",
      path: "Acme",
      children: [
        {
          name: "Product",
          path: "Acme/Product",
          children: [{ name: "Q3", path: "Acme/Product/Q3", children: [] }],
        },
      ],
    },
  ],
};

describe("WorkFolderSelect (ctx-row)", () => {
  beforeEach(() => {
    fetchWorkFolderTree.mockReset();
    resetWorkFolderCache();
  });

  it("shares ONE tree read across simultaneously mounted surfaces", async () => {
    fetchWorkFolderTree.mockResolvedValue(NESTED);
    render(
      <>
        <WorkFolderSelect value={null} onChange={vi.fn()} />
        <WorkFolderSelect value={null} onChange={vi.fn()} />
      </>,
    );

    await waitFor(() => expect(fetchWorkFolderTree).toHaveBeenCalledTimes(1));
  });

  it("reads the work tree once through the proxy and keeps the complete nested path", async () => {
    const user = userEvent.setup();
    fetchWorkFolderTree.mockResolvedValue(NESTED);
    const onChange = vi.fn();
    render(<WorkFolderSelect value={null} onChange={onChange} />);

    await waitFor(() => expect(fetchWorkFolderTree).toHaveBeenCalledTimes(1));

    await user.click(screen.getByRole("button", { name: "Work folder" }));
    await user.click(await screen.findByRole("option", { name: "Q3" }));

    expect(onChange).toHaveBeenCalledWith("Acme/Product/Q3");
  });

  it("has NO company control — that axis was ratified out of the chat surface", async () => {
    fetchWorkFolderTree.mockResolvedValue(NESTED);
    render(<WorkFolderSelect value={null} onChange={vi.fn()} />);

    await waitFor(() => expect(fetchWorkFolderTree).toHaveBeenCalled());
    expect(screen.queryByLabelText(/company/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/No company/i)).not.toBeInTheDocument();
  });

  it("clearing the selection reports null, not an empty string", async () => {
    const user = userEvent.setup();
    fetchWorkFolderTree.mockResolvedValue(NESTED);
    const onChange = vi.fn();
    render(<WorkFolderSelect value="Acme" onChange={onChange} />);

    await waitFor(() => expect(fetchWorkFolderTree).toHaveBeenCalled());
    await user.click(screen.getByRole("button", { name: "Work folder" }));
    await user.click(await screen.findByRole("option", { name: "No work folder" }));

    expect(onChange).toHaveBeenCalledWith(null);
  });

  it("surfaces a failed tree read with a Retry instead of an empty menu", async () => {
    const user = userEvent.setup();
    fetchWorkFolderTree
      .mockRejectedValueOnce(new Error("401 unauthorized"))
      .mockResolvedValueOnce(NESTED);
    render(<WorkFolderSelect value={null} onChange={vi.fn()} />);

    expect(await screen.findByText(/Couldn't load work folders/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Retry" }));

    await waitFor(() => expect(fetchWorkFolderTree).toHaveBeenCalledTimes(2));
    await waitFor(() =>
      expect(screen.queryByText(/Couldn't load work folders/)).not.toBeInTheDocument(),
    );
  });

  it("keeps a folder persisted on the chat selectable even when the tree read failed", async () => {
    fetchWorkFolderTree.mockRejectedValue(new Error("down"));
    render(<WorkFolderSelect value="Acme/Product" onChange={vi.fn()} />);

    expect(await screen.findByText("Acme/Product")).toBeInTheDocument();
  });
});

describe("flattenWorkFolders", () => {
  it("keeps every node's full path and indents by depth", () => {
    expect(flattenWorkFolders(NESTED.entries)).toEqual([
      { value: "Acme", label: "Acme" },
      { value: "Acme/Product", label: "\u00a0\u00a0Product" },
      { value: "Acme/Product/Q3", label: "\u00a0\u00a0\u00a0\u00a0Q3" },
    ]);
  });
});
