import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ToastProvider } from "@/design";
import type { ProjectTreeNode } from "@/features/projects/hierarchy";
import { DispatchDialog, type DispatchResult } from "./DispatchDialog";
import type { PlanJobState, PlanProvisionResult } from "./client";

const {
  createTask,
  createRun,
  disciplines,
  clarify,
  dispatch,
  planStart,
  planStatus,
  planConfirm,
  fetchProjectTree,
  createProject,
} = vi.hoisted(() => ({
  createTask: vi.fn(),
  createRun: vi.fn(),
  disciplines: vi.fn(),
  clarify: vi.fn(),
  dispatch: vi.fn(),
  planStart: vi.fn(),
  planStatus: vi.fn(),
  planConfirm: vi.fn(),
  fetchProjectTree: vi.fn(),
  createProject: vi.fn(),
}));

vi.mock("./client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./client")>();
  return {
    ...actual,
    voiceDispatchApi: { createTask, createRun, disciplines },
    intakeApi: { clarify, dispatch },
    planApi: { start: planStart, status: planStatus, confirm: planConfirm },
  };
});

vi.mock("@/features/projects/hierarchy", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/features/projects/hierarchy")>();
  return { ...actual, fetchProjectTree };
});

vi.mock("@/features/projects/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/features/projects/api")>();
  return { ...actual, createProject };
});

// A small real tree (Growth Engine -> SEO Content) used by every test that
// exercises the project selector / route-target-name resolution.
const FIXTURE_TREE: ProjectTreeNode[] = [
  {
    project: { id: "prj_growth", name: "Growth Engine" },
    status: "active",
    sub_projects: [
      { project: { id: "prj_seo", name: "SEO Content" }, status: "active", sub_projects: [], tasks: [] },
    ],
    tasks: [],
  },
];

function renderDialog(overrides?: Partial<Parameters<typeof DispatchDialog>[0]>) {
  const onClose = vi.fn();
  const onDispatched = vi.fn<(result: DispatchResult) => void>();
  const utils = render(
    <ToastProvider>
      <DispatchDialog open text="Ship the thing" onClose={onClose} onDispatched={onDispatched} {...overrides} />
    </ToastProvider>,
  );
  return { ...utils, onClose, onDispatched };
}

async function chooseSpeed(user: ReturnType<typeof userEvent.setup>, name: RegExp) {
  await user.click(screen.getByRole("radio", { name }));
}

describe("DispatchDialog", () => {
  beforeEach(() => {
    createTask.mockReset();
    createRun.mockReset();
    disciplines.mockReset();
    clarify.mockReset();
    dispatch.mockReset();
    planStart.mockReset();
    planStatus.mockReset();
    planConfirm.mockReset();
    fetchProjectTree.mockReset();
    createProject.mockReset();
    disciplines.mockResolvedValue([]);
    fetchProjectTree.mockResolvedValue([]);
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it("shows a toast and falls back to an empty project list when the project tree fails to load", async () => {
    fetchProjectTree.mockRejectedValueOnce(new Error("network down"));
    renderDialog();
    await waitFor(() => expect(screen.getByText(/Couldn't load projects/i)).toBeInTheDocument());
    // Dialog remains usable — Plan with Fable (the primary action) still works.
    expect(screen.getByRole("button", { name: "Plan with Fable" })).toBeEnabled();
  });

  it("quick-dispatches a single unscoped, run-less task", async () => {
    createTask.mockResolvedValueOnce({ id: "task-1", title: "Ship the thing", state: "queued", discipline_id: null });
    const { onClose, onDispatched } = renderDialog();
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Quick dispatch" }));
    await waitFor(() => expect(onDispatched).toHaveBeenCalledWith({ taskId: "task-1", runId: null }));
    // The lane picker is gone (D11): quick dispatch never creates a run itself.
    expect(createRun).not.toHaveBeenCalled();
    expect(onClose).toHaveBeenCalled();
  });

  it("shows a plain error when createTask fails", async () => {
    createTask.mockRejectedValueOnce(new Error("500: db unavailable"));
    renderDialog();
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Quick dispatch" }));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(/db unavailable/i));
  });

  it("rejects dispatch and disables the button when text exceeds the max length", async () => {
    const longText = "a".repeat(2001);
    renderDialog({ text: longText });
    const dispatchButton = screen.getByRole("button", { name: "Quick dispatch" });
    expect(dispatchButton).toBeDisabled();
    expect(screen.getByText(/1 characters over the 2000 limit/i)).toBeInTheDocument();
    expect(createTask).not.toHaveBeenCalled();
  });

  it("shows a live character count under the preview", () => {
    renderDialog({ text: "hello" });
    expect(screen.getByText("5 / 2000")).toBeInTheDocument();
  });

  it("only fires a single createTask call under rapid repeated dispatch clicks", async () => {
    let resolveTask: (value: { id: string; title: string; state: string; discipline_id: string | null }) => void = () => {};
    createTask.mockReturnValueOnce(
      new Promise((resolve) => {
        resolveTask = resolve;
      }),
    );
    renderDialog();
    const dispatchButton = screen.getByRole("button", { name: "Quick dispatch" });

    // Fire two clicks in the same tick, before React re-renders `disabled` — exercises the
    // ref-based single-flight guard rather than relying on the disabled prop alone.
    await act(async () => {
      dispatchButton.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      dispatchButton.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
    expect(createTask).toHaveBeenCalledTimes(1);

    await act(async () => {
      resolveTask({ id: "task-3", title: "Ship the thing", state: "queued", discipline_id: null });
    });
  });

  describe("Fable plan flow", () => {
    it("plans, previews the plan + a NEW-project route decision, and confirms to create it", async () => {
      planStart.mockResolvedValueOnce({ job_id: "planjob_1", status: "running" });
      const readyJob: PlanJobState = {
        job_id: "planjob_1",
        status: "ready",
        plan: {
          project_name: "Thing Shipping Initiative",
          description: "Get it done end to end.",
          complexity: "standard",
          effort: "high",
          tasks: [],
          sub_projects: [
            {
              name: "Backend",
              description: "",
              tasks: [
                { title: "Wire the API", description: "", acceptance_criteria: [], suggested_discipline: null, suggested_priority: "normal" },
              ],
            },
          ],
        },
        route: { decision: "new", parent_project_id: null, project_name: "Thing Shipping Initiative", reason: "no existing projects to nest under" },
        route_target_name: null,
        error: null,
      };
      planStatus.mockResolvedValueOnce(readyJob);
      const provisionResult: PlanProvisionResult = {
        root_project_id: "prj_new",
        route: readyJob.route!,
        sub_project_ids: { Backend: "prj_backend" },
        task_count: 1,
        dispatched: [
          {
            board_task: { id: "bt_1", title: "Wire the API", status: "open", run_id: "run_1" },
            task_id: "tsk_1",
            run_id: "run_1",
            execute: "readonly",
            working_dir: null,
          },
        ],
      };
      planConfirm.mockResolvedValueOnce(provisionResult);

      const { onClose, onDispatched } = renderDialog();
      const user = userEvent.setup();

      await user.click(screen.getByRole("button", { name: "Plan with Fable" }));
      expect(planStart).toHaveBeenCalledWith("Ship the thing");

      await waitFor(() => expect(screen.getByRole("heading", { name: "Thing Shipping Initiative" })).toBeInTheDocument());
      expect(screen.getByText(/Fable will create a new project/i)).toBeInTheDocument();
      expect(screen.getByText("Wire the API")).toBeInTheDocument();

      await user.click(screen.getByRole("button", { name: "Confirm & create" }));

      await waitFor(() => expect(onDispatched).toHaveBeenCalledWith({ taskId: "tsk_1", runId: "run_1" }));
      expect(planConfirm).toHaveBeenCalledWith("planjob_1", { projectOverride: "auto", speed: "auto" });
      expect(onClose).toHaveBeenCalled();
    });

    it("threads the chosen speed into planApi.confirm", async () => {
      planStart.mockResolvedValueOnce({ job_id: "planjob_s", status: "running" });
      const readyJob: PlanJobState = {
        job_id: "planjob_s",
        status: "ready",
        plan: {
          project_name: "Speedy",
          description: "",
          complexity: "simple",
          effort: "high",
          tasks: [{ title: "Do it", description: "", acceptance_criteria: [], suggested_discipline: null, suggested_priority: "normal" }],
          sub_projects: [],
        },
        route: { decision: "new", parent_project_id: null, project_name: "Speedy", reason: "" },
        route_target_name: null,
        error: null,
      };
      planStatus.mockResolvedValueOnce(readyJob);
      planConfirm.mockResolvedValueOnce({
        root_project_id: "prj_s",
        route: readyJob.route!,
        sub_project_ids: {},
        task_count: 1,
        dispatched: [],
      });

      renderDialog();
      const user = userEvent.setup();
      await chooseSpeed(user, /Max Quality/i);
      await user.click(screen.getByRole("button", { name: "Plan with Fable" }));
      await waitFor(() => expect(screen.getByRole("heading", { name: "Speedy" })).toBeInTheDocument());
      await user.click(screen.getByRole("button", { name: "Confirm & create" }));

      await waitFor(() => expect(planConfirm).toHaveBeenCalledWith("planjob_s", { projectOverride: "auto", speed: "ultra" }));
    });

    it("resolves the EXISTING-project route target name and lets the operator override the destination from the real tree", async () => {
      fetchProjectTree.mockResolvedValue(FIXTURE_TREE);
      planStart.mockResolvedValueOnce({ job_id: "planjob_2", status: "running" });
      const readyJob: PlanJobState = {
        job_id: "planjob_2",
        status: "ready",
        plan: {
          project_name: "New checkout flow",
          description: "",
          complexity: "simple",
          effort: "high",
          tasks: [{ title: "Build it", description: "", acceptance_criteria: [], suggested_discipline: null, suggested_priority: "normal" }],
          sub_projects: [],
        },
        route: { decision: "existing", parent_project_id: "prj_growth", project_name: "New checkout flow", reason: "fits under Growth" },
        route_target_name: "Growth Engine",
        error: null,
      };
      planStatus.mockResolvedValueOnce(readyJob);
      planConfirm.mockResolvedValueOnce({
        root_project_id: "prj_growth_child",
        route: readyJob.route!,
        sub_project_ids: {},
        task_count: 1,
        dispatched: [
          {
            board_task: { id: "bt_2", title: "Build it", status: "open", run_id: "run_2" },
            task_id: "tsk_2",
            run_id: "run_2",
            execute: "readonly",
            working_dir: null,
          },
        ],
      });

      renderDialog();
      const user = userEvent.setup();
      await user.click(screen.getByRole("button", { name: "Plan with Fable" }));
      await waitFor(() => expect(screen.getByText(/Fable will nest this under existing project/i)).toBeInTheDocument());
      expect(screen.getByText(/Growth Engine/)).toBeInTheDocument();

      // Override: pick a different destination from the real, live project tree.
      await user.click(screen.getByRole("button", { name: "Project" }));
      await user.click(await screen.findByRole("option", { name: /SEO Content/i }));
      await user.click(screen.getByRole("button", { name: "Confirm & create" }));

      await waitFor(() =>
        expect(planConfirm).toHaveBeenCalledWith("planjob_2", { projectOverride: "prj_seo", speed: "auto" }),
      );
    });

    it("surfaces a clear error and returns to start when the plan job fails", async () => {
      planStart.mockResolvedValueOnce({ job_id: "planjob_3", status: "running" });
      planStatus.mockResolvedValueOnce({
        job_id: "planjob_3",
        status: "error",
        plan: null,
        route: null,
        route_target_name: null,
        error: "Fable model unavailable",
      });

      renderDialog();
      const user = userEvent.setup();
      await user.click(screen.getByRole("button", { name: "Plan with Fable" }));

      await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(/Planning failed: Fable model unavailable/i));
      // Back on the start screen, still usable — never a dead end.
      expect(screen.getByRole("button", { name: "Plan with Fable" })).toBeEnabled();
    });
  });

  describe("clarify path project wiring", () => {
    it("wires the chosen project to project_id (not suggested_discipline) when confirming a clarified spec", async () => {
      fetchProjectTree.mockResolvedValue(FIXTURE_TREE);
      clarify.mockResolvedValueOnce({
        mode: "spec",
        questions: [],
        spec: { title: "Ship the thing", description: "", acceptance_criteria: [], suggested_discipline: "backend", suggested_priority: "normal" },
        round: 0,
        forced: false,
      });
      dispatch.mockResolvedValueOnce({
        board_task: { id: "bt_3", title: "Ship the thing", status: "open", run_id: "run_3" },
        task_id: "tsk_3",
        run_id: "run_3",
        execute: "readonly",
        working_dir: null,
      });

      const { onDispatched } = renderDialog();
      const user = userEvent.setup();
      await user.click(screen.getByRole("button", { name: "Clarify with agent" }));
      await waitFor(() => expect(screen.getByLabelText("Title")).toBeInTheDocument());

      await user.click(screen.getByRole("button", { name: /Project \(optional\)/i }));
      await user.click(await screen.findByRole("option", { name: /Growth Engine/i }));

      await user.click(screen.getByRole("button", { name: "Create task" }));

      await waitFor(() => expect(onDispatched).toHaveBeenCalledWith({ taskId: "tsk_3", runId: "run_3" }));
      expect(dispatch).toHaveBeenCalledWith(
        expect.objectContaining({ title: "Ship the thing" }),
        undefined,
        "prj_growth",
        "readonly",
        "auto",
      );
    });
  });
});
