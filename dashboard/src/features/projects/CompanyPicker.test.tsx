import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ToastProvider } from "@/design";
import { CompanyPicker } from "./CompanyPicker";
import type { Project } from "./types";

const { companies, patchProject } = vi.hoisted(() => ({
  companies: vi.fn(),
  patchProject: vi.fn(),
}));

vi.mock("@/features/orgdims/api", () => ({
  orgdimsApi: { companies },
}));

vi.mock("./api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api")>();
  return { ...actual, patchProject };
});

function project(overrides: Partial<Project> = {}): Project {
  return {
    id: "proj_1",
    name: "Acme Delivery",
    root_dirs: [],
    vault_subfolder: "",
    budget_usd: null,
    allowed_tools: [],
    allowed_dirs: [],
    created_at: "2026-01-01T00:00:00Z",
    grants: [],
    org_company_id: null,
    ...overrides,
  };
}

const COMPANY_ROWS = [
  { id: "co_acme", slug: "acme", name: "ACME Corp" },
  { id: "co_widgets", slug: "widgets", name: "Widgets Inc" },
];

function renderPicker(overrides?: Partial<Project>) {
  const onUpdated = vi.fn();
  const utils = render(
    <ToastProvider>
      <CompanyPicker project={project(overrides)} onUpdated={onUpdated} />
    </ToastProvider>,
  );
  return { ...utils, onUpdated };
}

describe("CompanyPicker", () => {
  beforeEach(() => {
    companies.mockReset();
    patchProject.mockReset();
    companies.mockResolvedValue({ companies: COMPANY_ROWS });
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it("renders options from the API list, data-driven (never hardcoded)", async () => {
    renderPicker();
    const trigger = screen.getByRole("button", { name: /company/i });
    await userEvent.click(trigger);

    // The API-supplied companies appear as options...
    expect(await screen.findByRole("option", { name: "ACME Corp" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Widgets Inc" })).toBeInTheDocument();
    // ...and "Unassigned" is always present as an explicit clearing option.
    expect(screen.getByRole("option", { name: "Unassigned" })).toBeInTheDocument();
  });

  it("shows Unassigned selected when the project has no company", async () => {
    renderPicker({ org_company_id: null });
    await waitFor(() => expect(companies).toHaveBeenCalled());
    expect(screen.getByRole("button", { name: /company/i })).toHaveTextContent("Unassigned");
  });

  it("shows the resolved company name selected via id -> slug mapping", async () => {
    renderPicker({ org_company_id: "co_widgets" });
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /company/i })).toHaveTextContent("Widgets Inc"),
    );
  });

  it("PATCHes org_company_id with the slug when a company is chosen", async () => {
    patchProject.mockResolvedValue(project({ org_company_id: "co_acme" }));
    const { onUpdated } = renderPicker();
    await userEvent.click(screen.getByRole("button", { name: /company/i }));
    await userEvent.click(await screen.findByRole("option", { name: "ACME Corp" }));

    await waitFor(() =>
      expect(patchProject).toHaveBeenCalledWith("proj_1", { org_company_id: "acme" }),
    );
    await waitFor(() => expect(onUpdated).toHaveBeenCalledWith(project({ org_company_id: "co_acme" })));
  });

  it("PATCHes org_company_id: null when Unassigned is chosen", async () => {
    patchProject.mockResolvedValue(project({ org_company_id: null }));
    renderPicker({ org_company_id: "co_acme" });
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /company/i })).toHaveTextContent("ACME Corp"),
    );
    await userEvent.click(screen.getByRole("button", { name: /company/i }));
    await userEvent.click(await screen.findByRole("option", { name: "Unassigned" }));

    await waitFor(() =>
      expect(patchProject).toHaveBeenCalledWith("proj_1", { org_company_id: null }),
    );
  });
});
