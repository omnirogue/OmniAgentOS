import { describe, expect, it } from "vitest";
import type {
  ConnectionIntegration,
  ConnectionsResponse,
} from "./types";
import {
  filterConnections,
  flattenIntegrations,
  statusBadgeTone,
  statusSummaryLabel,
} from "./logic";

function integration(
  overrides: Partial<ConnectionIntegration> = {},
): ConnectionIntegration {
  return {
    id: "test",
    name: "Test Service",
    logo: "plug",
    status: "connected",
    instances: [],
    detail: "All keys configured",
    docs_url: null,
    ...overrides,
  };
}

function buildResponse(
  integrations: ConnectionIntegration[],
): ConnectionsResponse {
  return {
    categories: [
      {
        id: "cat",
        label: "Category",
        integrations,
      },
    ],
    connected_count: integrations.filter((i) => i.status === "connected").length,
    total_count: integrations.length,
  };
}

describe("filterConnections", () => {
  it("returns the input unchanged for an empty query", () => {
    const r = buildResponse([
      integration({ id: "a", name: "Alpha" }),
      integration({ id: "b", name: "Beta" }),
    ]);
    const out = filterConnections(r, "   ");
    expect(out.categories).toHaveLength(1);
    expect(out.categories[0].integrations).toHaveLength(2);
    expect(out.total_count).toBe(2);
  });

  it("matches by integration name (case-insensitive)", () => {
    const r = buildResponse([
      integration({ id: "anthropic", name: "Anthropic" }),
      integration({ id: "openai", name: "OpenAI" }),
      integration({ id: "stripe", name: "Stripe" }),
    ]);
    const out = filterConnections(r, "anthro");
    expect(out.categories[0].integrations).toHaveLength(1);
    expect(out.categories[0].integrations[0].id).toBe("anthropic");
    expect(out.total_count).toBe(1);
    expect(out.connected_count).toBe(1);
  });

  it("matches by integration id", () => {
    const r = buildResponse([
      integration({ id: "anthropic", name: "Anthropic" }),
      integration({ id: "openai", name: "OpenAI" }),
    ]);
    const out = filterConnections(r, "openai");
    expect(out.categories[0].integrations).toHaveLength(1);
    expect(out.categories[0].integrations[0].name).toBe("OpenAI");
  });

  it("matches by instance label (multi-instance)", () => {
    const r = buildResponse([
      integration({
        id: "piedpiper",
        name: "PiedPiper",
        instances: [
          { label: "AcmeUni", status: "connected" },
          { label: "INITECH", status: "configured" },
          { label: "GLOBEX", status: "not_configured" },
        ],
      }),
      integration({ id: "stripe", name: "Stripe" }),
    ]);
    const out = filterConnections(r, "acmeuni");
    expect(out.categories[0].integrations).toHaveLength(1);
    expect(out.categories[0].integrations[0].id).toBe("piedpiper");
  });

  it("matches by category label", () => {
    const multiCat: ConnectionsResponse = {
      categories: [
        {
          id: "ai",
          label: "AI Providers",
          integrations: [integration({ id: "a", name: "Alpha" })],
        },
        {
          id: "payments",
          label: "Payments",
          integrations: [integration({ id: "s", name: "Stripe" })],
        },
      ],
      connected_count: 2,
      total_count: 2,
    };
    const out = filterConnections(multiCat, "AI Providers");
    expect(out.categories).toHaveLength(1);
    expect(out.categories[0].id).toBe("ai");
  });

  it("drops empty categories from the output", () => {
    const multiCat: ConnectionsResponse = {
      categories: [
        {
          id: "ai",
          label: "AI Providers",
          integrations: [integration({ id: "a", name: "Anthropic" })],
        },
        {
          id: "payments",
          label: "Payments",
          integrations: [integration({ id: "s", name: "Stripe" })],
        },
      ],
      connected_count: 2,
      total_count: 2,
    };
    const out = filterConnections(multiCat, "stripe");
    expect(out.categories).toHaveLength(1);
    expect(out.categories[0].id).toBe("payments");
  });

  it("recomputes connected_count to match the filtered set", () => {
    const r = buildResponse([
      integration({ id: "a", name: "Anthropic", status: "connected" }),
      integration({ id: "b", name: "OpenAI", status: "not_configured" }),
      integration({ id: "s", name: "Stripe", status: "connected" }),
    ]);
    const out = filterConnections(r, "stripe");
    expect(out.connected_count).toBe(1);
    expect(out.total_count).toBe(1);
  });
});

describe("statusBadgeTone", () => {
  it("maps status values to semantic badge tones", () => {
    expect(statusBadgeTone("connected")).toBe("ok");
    expect(statusBadgeTone("configured")).toBe("running");
    expect(statusBadgeTone("not_configured")).toBe("neutral");
    expect(statusBadgeTone("error")).toBe("warn");
  });
});

describe("statusSummaryLabel", () => {
  it("returns a human label for single-instance integrations", () => {
    expect(
      statusSummaryLabel(integration({ status: "connected" })),
    ).toBe("Connected");
    expect(
      statusSummaryLabel(integration({ status: "configured" })),
    ).toBe("Partially configured");
    expect(
      statusSummaryLabel(integration({ status: "not_configured" })),
    ).toBe("Not configured");
    expect(
      statusSummaryLabel(integration({ status: "error" })),
    ).toBe("Vault error");
  });

  it("returns an 'N/M instances' label for multi-instance integrations", () => {
    const i = integration({
      instances: [
        { label: "AcmeUni", status: "connected" },
        { label: "INITECH", status: "not_configured" },
        { label: "GLOBEX", status: "connected" },
      ],
    });
    expect(statusSummaryLabel(i)).toBe("2/3 instances");
  });
});

describe("flattenIntegrations", () => {
  it("returns integrations in category order", () => {
    const r: ConnectionsResponse = {
      categories: [
        {
          id: "a",
          label: "First",
          integrations: [integration({ id: "1" }), integration({ id: "2" })],
        },
        { id: "b", label: "Second", integrations: [integration({ id: "3" })] },
      ],
      connected_count: 3,
      total_count: 3,
    };
    const flat = flattenIntegrations(r);
    expect(flat.map((x) => x.integration.id)).toEqual(["1", "2", "3"]);
    expect(flat[0].categoryLabel).toBe("First");
    expect(flat[2].categoryLabel).toBe("Second");
  });
});
