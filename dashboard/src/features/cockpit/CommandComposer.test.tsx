import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { CommandComposer } from "./CommandComposer";

const { fetchWithTimeout } = vi.hoisted(() => ({ fetchWithTimeout: vi.fn() }));

vi.mock("@/lib/fetchTimeout", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/fetchTimeout")>();
  return { ...actual, fetchWithTimeout };
});

// VoiceConversationMode pulls in Web-Speech/audio wiring irrelevant to the payload.
vi.mock("@/features/voice/VoiceConversationMode", () => ({
  VoiceConversationMode: () => null,
}));

function okJson(body: unknown) {
  return { ok: true, status: 200, statusText: "OK", json: async () => body } as Response;
}

/** Parses the JSON body of the single POST the composer fired. */
function lastPostBody(): Record<string, unknown> {
  const call = fetchWithTimeout.mock.calls.at(-1)!;
  const init = call[1] as RequestInit;
  return JSON.parse(init.body as string);
}

describe("CommandComposer payload", () => {
  beforeEach(() => {
    fetchWithTimeout.mockReset();
    fetchWithTimeout.mockResolvedValue(okJson({ message: "queued" }));
  });

  afterEach(() => vi.clearAllMocks());

  it("posts exactly {goal, speed:'auto'} in the default (Balanced) state", async () => {
    render(<CommandComposer />);
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("What do you want done?"), "Fix the bug");
    await user.click(screen.getByRole("button", { name: /Get it done/i }));

    await waitFor(() => expect(fetchWithTimeout).toHaveBeenCalledTimes(1));
    const [url] = fetchWithTimeout.mock.calls[0]!;
    expect(url).toBe("/api/intake/quick");
    expect(lastPostBody()).toEqual({ goal: "Fix the bug", speed: "auto" });
  });

  it("maps Max Quality + Plan-first onto speed:'ultra', plan:true", async () => {
    render(<CommandComposer />);
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("What do you want done?"), "Rebuild the pipeline");
    await user.click(screen.getByRole("radio", { name: "Max Quality" }));
    await user.click(screen.getByRole("checkbox"));
    await user.click(screen.getByRole("button", { name: /Get it done/i }));

    await waitFor(() => expect(fetchWithTimeout).toHaveBeenCalledTimes(1));
    expect(lastPostBody()).toEqual({
      goal: "Rebuild the pipeline",
      speed: "ultra",
      plan: true,
    });
  });

  it("never sends execute/executor/priority/category/lane (D11/D12 — topology is the router's)", async () => {
    render(<CommandComposer />);
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("What do you want done?"), "Do a thing");
    await user.click(screen.getByRole("button", { name: /Get it done/i }));

    await waitFor(() => expect(fetchWithTimeout).toHaveBeenCalledTimes(1));
    const body = lastPostBody();
    for (const dropped of ["execute", "executor", "priority", "category", "lane"]) {
      expect(body).not.toHaveProperty(dropped);
    }
  });
});
