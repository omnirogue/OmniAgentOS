import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ToastProvider } from "@/design";
import { ChatSurface } from "./ChatSurface";
import type { Chat, ChatMessage } from "./chatApi";

const { useChatThread, useSkillsForMention, nlAssignMock } = vi.hoisted(() => ({
  useChatThread: vi.fn(),
  useSkillsForMention: vi.fn(() => ({ skills: [], loading: false, refresh: vi.fn() })),
  nlAssignMock: vi.fn(),
}));

vi.mock("./useChats", () => ({ useChatThread, useSkillsForMention }));
vi.mock("./ModelPicker", () => ({ ModelPicker: () => null }));
vi.mock("./WorkFolderSelect", () => ({ WorkFolderSelect: () => null }));
vi.mock("./ProjectSuggestionBar", () => ({ ProjectSuggestionBar: () => null }));
// The NL assign/propose intercept's own client — mocked here so the
// composer-intercept suite below controls its response without a network
// call; `employeeName`/`TeamApiError` stay real (harmless pure lookups).
vi.mock("@/features/team/client", () => ({
  teamApi: { nlAssign: nlAssignMock },
  TeamApiError: class TeamApiError extends Error {
    status: number;
    constructor(message: string, status: number) {
      super(message);
      this.status = status;
    }
  },
}));

const chat: Chat = {
  id: "chat_1",
  title: "Rebuild the chat",
  status: "active",
  project_id: null,
  project_name: null,
  board_task_id: null,
  preferred_model: null,
  orch_mode: "solo",
  plan_mode: false,
  routing: { allow: [], deny: [], speed: null, effort: null, hint: null },
  project_suggestion: null,
  message_count: 2,
  last_message_at: null,
  promoted_at: null,
  created_at: "2026-08-01T10:00:00Z",
  updated_at: "2026-08-01T10:00:00Z",
  meta: {},
};

function message(over: Partial<ChatMessage>): ChatMessage {
  return {
    id: "m1",
    seq: 1,
    role: "agent",
    content: "",
    model: null,
    created_at: "2026-08-01T10:00:00Z",
    ...over,
  };
}

const IDLE_TURN = {
  state: "idle" as const,
  text: "",
  model: null,
  turnSeq: null,
  sessionId: null,
  startedAt: null,
  lastDeltaAt: null,
  lastEventType: null,
  fanoutTaskIds: null,
};

function mockThread(over: Record<string, unknown> = {}) {
  useChatThread.mockReturnValue({
    chat,
    messages: [],
    loading: false,
    error: null,
    sending: false,
    turn: IDLE_TURN,
    suggestionDismissed: true,
    source: "live",
    refresh: vi.fn(),
    send: vi.fn(),
    patch: vi.fn().mockResolvedValue(chat),
    spawn: vi.fn(),
    promote: vi.fn(),
    uploadFiles: vi.fn(),
    dismissSuggestion: vi.fn(),
    applySuggestion: vi.fn(),
    startPlan: vi.fn(),
    confirmPlan: vi.fn(),
    discardPlan: vi.fn(),
    planJob: null,
    planBusy: false,
    planError: null,
    ...over,
  });
}

function renderSurface() {
  return render(
    <ToastProvider>
      <ChatSurface chatId="chat_1" variant="page" />
    </ToastProvider>,
  );
}

describe("ChatSurface — markdown", () => {
  beforeEach(() => {
    useChatThread.mockReset();
    useSkillsForMention.mockReturnValue({ skills: [], loading: false, refresh: vi.fn() });
  });

  it("renders an agent reply as markdown instead of raw asterisks", () => {
    mockThread({
      messages: [
        message({
          content: "## Findings\n\nThe **bridge** died.\n\n- one\n- two\n\n`npm test`",
        }),
      ],
    });
    renderSurface();

    expect(screen.getByRole("heading", { name: "Findings" })).toBeInTheDocument();
    expect(screen.getByText("bridge").tagName).toBe("STRONG");
    expect(screen.getAllByRole("listitem")).toHaveLength(2);
    expect(screen.getByText("npm test").tagName).toBe("CODE");
    expect(screen.queryByText(/\*\*bridge\*\*/)).not.toBeInTheDocument();
  });

  it("renders the LIVE streaming buffer as markdown too, including a half-written fence", () => {
    mockThread({
      turn: {
        ...IDLE_TURN,
        state: "running",
        text: "Working on it — **almost**:\n\n```py\nprint(1)",
        startedAt: Date.now() - 3000,
        lastEventType: "delta",
      },
    });
    renderSurface();

    expect(screen.getByText("almost").tagName).toBe("STRONG");
    expect(screen.getByText("print(1)").closest("pre")).not.toBeNull();
  });

  it("keeps a typed user message literal (no markdown mangling)", () => {
    mockThread({ messages: [message({ id: "u1", role: "user", content: "use 3 * 4 and _x_" })] });
    renderSurface();

    expect(screen.getByText("use 3 * 4 and _x_")).toBeInTheDocument();
  });

  it("badges the model persisted on the turn, including one carried in meta", () => {
    mockThread({
      messages: [
        message({ id: "a1", content: "col", model: "claude-opus-5" }),
        message({ id: "a2", seq: 2, content: "meta", meta: { model: "grok-5" } }),
      ],
    });
    renderSurface();

    expect(screen.getByText("claude-opus-5")).toBeInTheDocument();
    expect(screen.getByText("grok-5")).toBeInTheDocument();
  });
});

describe("ChatSurface — activity strip", () => {
  beforeEach(() => {
    useChatThread.mockReset();
    useSkillsForMention.mockReturnValue({ skills: [], loading: false, refresh: vi.fn() });
  });

  it("reports elapsed time and the last SSE event while streaming", () => {
    mockThread({
      turn: {
        ...IDLE_TURN,
        state: "running",
        text: "partial",
        startedAt: Date.now() - 12_000,
        lastEventType: "delta",
      },
    });
    renderSurface();

    const strip = screen.getByLabelText("Turn activity");
    expect(strip).toHaveTextContent("12s");
    expect(strip).toHaveTextContent("streaming");
    expect(strip).not.toHaveTextContent("no live output");
  });

  it("says it is waiting when a queued turn has produced no event yet", () => {
    mockThread({
      turn: { ...IDLE_TURN, state: "queued", startedAt: Date.now() },
    });
    renderSurface();

    expect(screen.getByLabelText("Turn activity")).toHaveTextContent("queued");
  });

  it("names the poll fallback once the turn stalls", () => {
    mockThread({
      turn: {
        ...IDLE_TURN,
        state: "stalled",
        startedAt: Date.now() - 65_000,
        lastEventType: "started",
      },
    });
    renderSurface();

    const strip = screen.getByLabelText("Turn activity");
    expect(strip).toHaveTextContent("1m 05s");
    expect(strip).toHaveTextContent("no live output — polling for the reply");
  });

  it("is gone once the turn completes", () => {
    mockThread({
      messages: [message({ content: "done" })],
      turn: { ...IDLE_TURN, state: "completed", lastEventType: "completed" },
    });
    renderSurface();

    expect(screen.queryByLabelText("Turn activity")).not.toBeInTheDocument();
  });
});

describe("ChatSurface — NL assign/propose composer intercept", () => {
  beforeEach(() => {
    useChatThread.mockReset();
    useSkillsForMention.mockReturnValue({ skills: [], loading: false, refresh: vi.fn() });
    nlAssignMock.mockReset();
  });

  it("renders the PROPOSAL confirmation — never the assignment toast — for a propose sentence", async () => {
    const user = userEvent.setup();
    nlAssignMock.mockResolvedValue({
      kind: "automation_proposal",
      task_id: "btk_1",
      title: "draft the weekly digest",
      category: null,
      assignee_hint: null,
      goal_id: "goal_1",
      acceptance_criteria: "draft the weekly digest",
      status: "awaiting_approval",
      message: "Proposed: draft the weekly digest — awaiting the operator's approval.",
    });
    const send = vi.fn();
    mockThread({ send });
    renderSurface();

    await user.type(screen.getByRole("textbox"), "propose an automation to draft the weekly digest");
    await user.click(screen.getByRole("button", { name: "Send" }));

    await waitFor(() =>
      expect(nlAssignMock).toHaveBeenCalledWith("propose an automation to draft the weekly digest"),
    );
    expect(
      await screen.findByText("📝 Proposed: draft the weekly digest — awaiting the operator's approval."),
    ).toBeInTheDocument();
    // Never the owner-assignment confirmation — a proposal names no owner.
    expect(screen.queryByText(/^✅ Task/)).not.toBeInTheDocument();
    // The sentence never reached the chat/LLM turn.
    expect(send).not.toHaveBeenCalled();
    expect(screen.getByRole("textbox")).toHaveValue("");
  });

  it("renders the ASSIGNMENT confirmation for an assign sentence", async () => {
    const user = userEvent.setup();
    nlAssignMock.mockResolvedValue({
      task_id: "btk_2",
      owner_employee_id: "emp_bob",
      title: "fix the login page",
      acceptance_criteria: "fix the login page",
      goal_id: null,
      due_date: null,
      message: "Assigned to emp_bob: fix the login page.",
    });
    const send = vi.fn();
    mockThread({ send });
    renderSurface();

    await user.type(screen.getByRole("textbox"), "assign bob fix the login page");
    await user.click(screen.getByRole("button", { name: "Send" }));

    expect(await screen.findByText("✅ Task btk_2 created for Bob: fix the login page")).toBeInTheDocument();
    expect(screen.queryByText(/^📝/)).not.toBeInTheDocument();
    expect(send).not.toHaveBeenCalled();
  });
});
