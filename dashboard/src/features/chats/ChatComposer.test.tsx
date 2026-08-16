import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ChatComposer, type DeckState } from "./ChatComposer";

vi.mock("./ModelPicker", () => ({ ModelPicker: () => null }));

const deck: DeckState = {
  model: null,
  justThisMessage: false,
  planMode: false,
  orchMode: "solo",
  fanoutCount: 3,
  workFolder: null,
};

type Props = Parameters<typeof ChatComposer>[0];
type SendMock = ReturnType<typeof vi.fn<Props["onSend"]>>;

function renderComposer(overrides: Omit<Partial<Props>, "onSend"> & { onSend?: SendMock } = {}) {
  const onSend: SendMock = overrides.onSend ?? vi.fn<Props["onSend"]>().mockResolvedValue(undefined);
  render(
    <ChatComposer
      onUploadFiles={vi.fn().mockResolvedValue([])}
      skills={[]}
      sending={false}
      hasBoardTask={false}
      deck={deck}
      onDeckChange={vi.fn()}
      routing={{ allow: [], deny: [], speed: null, effort: null, hint: null }}
      onRoutingSave={vi.fn().mockResolvedValue(undefined)}
      {...overrides}
      onSend={onSend}
    />,
  );
  return { onSend };
}

describe("ChatComposer", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.reject(new Error("no network in tests"))));
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("fires NO request while typing and shows no intent row (the shadow experiment is cut)", async () => {
    const user = userEvent.setup();
    renderComposer();

    await user.type(
      screen.getByRole("textbox"),
      "Build a project plan for the Q3 launch with the engineering team",
    );
    // The old composer debounced an intent POST 800ms after a settled draft.
    await new Promise((resolve) => setTimeout(resolve, 1200));

    expect(fetch).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "project" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "loop" })).not.toBeInTheDocument();
    expect(screen.queryByText(/Classifying intent/)).not.toBeInTheDocument();
  });

  it("sends the draft with the deck and clears it on success", async () => {
    const user = userEvent.setup();
    const { onSend } = renderComposer();

    await user.type(screen.getByRole("textbox"), "ship it");
    await user.click(screen.getByRole("button", { name: "Send" }));

    await waitFor(() => expect(onSend).toHaveBeenCalledTimes(1));
    expect(onSend.mock.calls[0][0]).toBe("ship it");
    expect(onSend.mock.calls[0][1]).toEqual(deck);
    expect(screen.getByRole("textbox")).toHaveValue("");
  });

  it("NEVER clears the draft when the send fails, and ↑ recalls the last message", async () => {
    const user = userEvent.setup();
    const onSend = vi.fn<Props["onSend"]>().mockRejectedValue(new Error("bridge down"));
    renderComposer({ onSend, lastUserMessage: "the previous question" });

    const textbox = screen.getByRole("textbox");
    await user.type(textbox, "retry me");
    await user.click(screen.getByRole("button", { name: "Send" }));

    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("bridge down"));
    expect(textbox).toHaveValue("retry me");

    // Esc clears it; ↑ on the now-empty composer recalls the last user message.
    await user.type(textbox, "{Escape}");
    expect(textbox).toHaveValue("");
    await user.type(textbox, "{ArrowUp}");
    expect(textbox).toHaveValue("the previous question");
  });
});
