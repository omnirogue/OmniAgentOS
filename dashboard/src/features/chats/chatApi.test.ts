import { describe, expect, it, vi } from "vitest";

/**
 * Chat API fixture-path tests. These verify that the fixture fallback
 * behaves correctly (create, list, send, spawn, promote, delete) without
 * touching the network. The live-path is integration-tested via the
 * verification ladder.
 */

// Force the fixtures flag BEFORE importing the module. `vi.hoisted` runs
// ahead of the (hoisted) static imports below — a plain top-level assignment
// would execute AFTER chatApi.ts evaluates USE_CHAT_FIXTURES at module scope.
vi.hoisted(() => {
  process.env.NEXT_PUBLIC_USE_CHATS_FIXTURES = "true";
});

import {
  listChats,
  createChat,
  getChat,
  updateChat,
  deleteChat,
  listMessages,
  sendMessage,
  spawnChat,
  promoteChat,
  classifyChat,
  seedPlan,
  fetchPlanJob,
  fetchModels,
  fetchSkillsTree,
  fetchWorkFolderTree,
} from "./chatApi";

describe("chatApi — fixture path", () => {
  it("listChats returns fixture chats with the ChatDTO shape", async () => {
    const chats = await listChats();
    expect(chats.length).toBeGreaterThanOrEqual(3);
    const chat = chats[0]!;
    expect(chat.id).toBeTruthy();
    expect(chat.orch_mode).toBe("solo");
    expect(chat.plan_mode).toBe(false);
    expect(chat.routing).toEqual({
      allow: [],
      deny: [],
      speed: null,
      effort: null,
      hint: null,
    });
    expect(typeof chat.message_count).toBe("number");
  });

  it("listChats filters by project", async () => {
    const platform = await listChats("prj_platform");
    expect(platform.every((c) => c.project_id === "prj_platform")).toBe(true);
    expect(platform.length).toBeGreaterThanOrEqual(1);
  });

  it("listChats with project='' returns project-less chats", async () => {
    const recents = await listChats("");
    expect(recents.every((c) => !c.project_id)).toBe(true);
  });

  it("createChat adds a new chat to fixtures", async () => {
    const chat = await createChat({ title: "Test Chat" });
    expect(chat.title).toBe("Test Chat");
    expect(chat.status).toBe("active");
    expect(chat.id).toBeTruthy();
    const found = await getChat(chat.id);
    expect(found.id).toBe(chat.id);
  });

  it("updateChat patches model / orch / plan / routing", async () => {
    const chat = await createChat({ title: "Patch me" });
    const patched = await updateChat(chat.id, {
      model: "grok-4",
      orch_mode: "fanout",
      plan_mode: true,
      routing: { speed: "fast", hint: "prefer grok" },
    });
    expect(patched.preferred_model).toBe("grok-4");
    expect(patched.orch_mode).toBe("fanout");
    expect(patched.plan_mode).toBe(true);
    expect(patched.routing.speed).toBe("fast");
    expect(patched.routing.hint).toBe("prefer grok");
  });

  it("sendMessage returns the SendResult envelope", async () => {
    const chat = await createChat({ title: "Send test" });
    const result = await sendMessage(chat.id, {
      content: "hello agent",
      meta: {
        attachments: [{ kind: "skill", ref: "skill_pdf", label: "@pdf" }],
        work_folder: "Acme/Product",
      },
    });
    expect(result.message.role).toBe("user");
    expect(result.message.content).toBe("hello agent");
    expect(result.message.meta?.attachments?.[0]?.ref).toBe("skill_pdf");
    expect(result.message.meta?.work_folder).toBe("Acme/Product");
    expect(result.dispatch?.session_id).toBeTruthy();
    expect(result.dispatch?.steered).toBe(false);

    const messages = await listMessages(chat.id);
    expect(messages.some((m) => m.id === result.message.id)).toBe(true);
  });

  it("sendMessage fan-out returns task_ids", async () => {
    const chat = await createChat({ title: "Fanout test" });
    const result = await sendMessage(chat.id, {
      content: "research competitors",
      orch_mode: "fanout",
      count: 3,
    });
    expect(result.task_ids?.length).toBe(3);
  });

  it("spawnChat returns task ids", async () => {
    const chat = await createChat({ title: "Spawn test" });
    const result = await spawnChat(chat.id, "build the thing", 2);
    expect(result.task_ids.length).toBe(2);
  });

  it("promoteChat returns project and task ids", async () => {
    const chat = await createChat({ title: "Promote test" });
    const result = await promoteChat(chat.id, null, "New Project");
    expect(result.project_id).toBeTruthy();
    expect(result.task_ids.length).toBeGreaterThanOrEqual(1);
  });

  it("classifyChat returns a suggestion shape", async () => {
    const result = await classifyChat("chat_003");
    expect(result.project_id).toBe("prj_platform");
    expect(result.confidence).toBeGreaterThanOrEqual(0.5);
  });

  it("fetchWorkFolderTree returns the bounded work tree", async () => {
    const tree = await fetchWorkFolderTree();
    expect(tree.entries[0]?.path).toBe("Acme");
  });

  it("seedPlan + fetchPlanJob round-trip", async () => {
    const chat = await createChat({ title: "Plan test" });
    const seed = await seedPlan(chat.id, "build a dock");
    expect(seed.status).toBe("running");
    const job = await fetchPlanJob(seed.job_id);
    expect(job.status).toBe("ready");
    expect(job.plan?.project_name).toBeTruthy();
  });

  it("grow-a-chat + plan mode: seedPlan persists plan_job_id in meta", async () => {
    // Simulates the ChatSurface handleSend path: create chat → seedPlan →
    // updateChat with plan_job_id — so the re-attach effect can pick it up.
    const chat = await createChat({ title: "New chat", board_task_id: "tsk_xyz" });
    expect(chat.board_task_id).toBe("tsk_xyz");
    const seed = await seedPlan(chat.id, "design the thing");
    expect(seed.job_id).toBeTruthy();
    // Persist the job id in meta — this is what the ChatSurface fix does.
    const updated = await updateChat(chat.id, {
      meta: { plan_job_id: seed.job_id },
    });
    expect((updated.meta as Record<string, unknown>).plan_job_id).toBe(seed.job_id);
    // getChat round-trip keeps the meta
    const fetched = await getChat(chat.id);
    expect((fetched.meta as Record<string, unknown>).plan_job_id).toBe(seed.job_id);
  });

  it("deleteChat soft-deletes", async () => {
    const chat = await createChat({ title: "Delete me" });
    await deleteChat(chat.id);
    const chats = await listChats();
    expect(chats.some((c) => c.id === chat.id)).toBe(false);
  });

  it("fetchModels + fetchSkillsTree return entries", async () => {
    const models = await fetchModels();
    expect(models.length).toBeGreaterThanOrEqual(2);
    expect(models.some((m) => !m.available)).toBe(true);
    const skills = await fetchSkillsTree();
    expect(skills.length).toBeGreaterThanOrEqual(1);
  });
});
