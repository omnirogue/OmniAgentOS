import { describe, expect, it } from "vitest";

/**
 * Folder logic tests (088): palette tokens, response parsing (new + legacy
 * shapes), chat grouping, and the client-side name rules that mirror
 * ChatStore._validate_folder_name.
 */

import type { Chat } from "./chatApi";
import {
  FOLDER_COLORS,
  buildFolderGroups,
  chatFolder,
  folderColorName,
  folderNameError,
  getDefaultFolderColor,
  isFolderColor,
  parseFoldersResponse,
} from "./folders";

function chat(partial: Partial<Chat> & { id: string }): Chat {
  return {
    title: partial.id,
    status: "active",
    project_id: null,
    project_name: null,
    board_task_id: null,
    preferred_model: null,
    orch_mode: "solo",
    plan_mode: false,
    routing: { allow: [], deny: [], speed: null, effort: null, hint: null },
    project_suggestion: null,
    message_count: 0,
    last_message_at: null,
    promoted_at: null,
    created_at: "2026-07-01T00:00:00Z",
    updated_at: "2026-07-01T00:00:00Z",
    meta: {},
    ...partial,
  };
}

describe("palette tokens", () => {
  it("pins the 8 API color tokens in order", () => {
    expect(FOLDER_COLORS).toEqual([
      "gray",
      "red",
      "orange",
      "yellow",
      "green",
      "teal",
      "blue",
      "violet",
    ]);
  });

  it("guards tokens and coerces anything else to gray", () => {
    expect(isFolderColor("teal")).toBe(true);
    expect(isFolderColor("#ff0000")).toBe(false);
    expect(isFolderColor(42)).toBe(false);
    expect(folderColorName("violet")).toBe("violet");
    expect(folderColorName("crimson")).toBe("gray");
    expect(folderColorName(undefined)).toBe("gray");
  });

  it("returns deterministic defaults spread across the non-gray tokens", () => {
    const names = ["Folder 0", "Folder 1", "Folder 2", "Folder 3", "Folder 4", "Folder 5", "Folder 6"];
    const colors = names.map(getDefaultFolderColor);
    expect(getDefaultFolderColor("Alpha")).toBe(getDefaultFolderColor("Alpha"));
    expect(new Set(colors)).toEqual(
      new Set(["red", "orange", "yellow", "green", "teal", "blue", "violet"]),
    );
  });
});

describe("parseFoldersResponse", () => {
  it("parses the 088 registry shape", () => {
    expect(
      parseFoldersResponse({
        folders: [
          { name: "Research", color: "teal", chat_count: 3 },
          { name: "Ops", color: "red", chat_count: 0 },
        ],
      }),
    ).toEqual([
      { name: "Research", color: "teal", chat_count: 3 },
      { name: "Ops", color: "red", chat_count: 0 },
    ]);
  });

  it("tolerates the legacy {folders:[string]} shape (gray, count 0)", () => {
    expect(parseFoldersResponse({ folders: ["backend", " design "] })).toEqual([
      { name: "backend", color: "gray", chat_count: 0 },
      { name: "design", color: "gray", chat_count: 0 },
    ]);
  });

  it("normalizes bad colors/counts and drops malformed entries", () => {
    expect(
      parseFoldersResponse({
        folders: [
          { name: "A", color: "#fff", chat_count: -2 },
          { name: "  ", color: "red", chat_count: 1 },
          { color: "red" },
          null,
          7,
        ],
      }),
    ).toEqual([{ name: "A", color: "gray", chat_count: 0 }]);
  });

  it("returns [] for garbage payloads", () => {
    expect(parseFoldersResponse(null)).toEqual([]);
    expect(parseFoldersResponse("nope")).toEqual([]);
    expect(parseFoldersResponse({})).toEqual([]);
    expect(parseFoldersResponse({ folders: "x" })).toEqual([]);
  });
});

describe("chatFolder", () => {
  it("returns the trimmed folder or null", () => {
    expect(chatFolder(chat({ id: "a", meta: { folder: " Research " } }))).toBe(
      "Research",
    );
    expect(chatFolder(chat({ id: "b", meta: { folder: "  " } }))).toBeNull();
    expect(chatFolder(chat({ id: "c", meta: {} }))).toBeNull();
    expect(chatFolder(chat({ id: "d", meta: { folder: 3 } }))).toBeNull();
  });
});

describe("buildFolderGroups", () => {
  const registry = [
    { name: "Ops", color: "red" as const, chat_count: 1 },
    { name: "Research", color: "teal" as const, chat_count: 0 },
  ];

  it("keeps registry order first, then unregistered folders alphabetically", () => {
    const groups = buildFolderGroups(
      [
        chat({ id: "1", meta: { folder: "zeta" } }),
        chat({ id: "2", meta: { folder: "Alpha" } }),
        chat({ id: "3", meta: { folder: "Ops" } }),
      ],
      registry,
    );
    expect(groups.map((g) => g.name)).toEqual(["Ops", "Research", "Alpha", "zeta"]);
    expect(groups.map((g) => g.color)).toEqual([
      "red",
      "teal",
      getDefaultFolderColor("Alpha"),
      getDefaultFolderColor("zeta"),
    ]);
  });

  it("lets registered colors win, including explicit gray", () => {
    const groups = buildFolderGroups(
      [chat({ id: "1", meta: { folder: "Gray folder" } })],
      [{ name: "Gray folder", color: "gray", chat_count: 1 }],
    );
    expect(groups[0].color).toBe("gray");
  });

  it("includes registered folders even when empty (drop targets)", () => {
    const groups = buildFolderGroups([], registry);
    expect(groups).toEqual([
      { name: "Ops", color: "red", chats: [] },
      { name: "Research", color: "teal", chats: [] },
    ]);
  });

  it("files chats under their folder, newest first, ignoring unfiled chats", () => {
    const older = chat({
      id: "old",
      meta: { folder: "Ops" },
      updated_at: "2026-07-01T00:00:00Z",
    });
    const newer = chat({
      id: "new",
      meta: { folder: "Ops" },
      updated_at: "2026-07-02T00:00:00Z",
    });
    const unfiled = chat({ id: "loose" });
    const groups = buildFolderGroups([older, unfiled, newer], registry);
    expect(groups[0].chats.map((c) => c.id)).toEqual(["new", "old"]);
    expect(groups.flatMap((g) => g.chats.map((c) => c.id))).not.toContain("loose");
  });
});

describe("folderNameError", () => {
  it("mirrors the server rules", () => {
    expect(folderNameError("Research")).toBeNull();
    expect(folderNameError("  padded  ")).toBeNull();
    expect(folderNameError("")).toMatch(/required/);
    expect(folderNameError("   ")).toMatch(/required/);
    expect(folderNameError("a/b")).toMatch(/cannot contain/);
    expect(folderNameError("x".repeat(101))).toMatch(/too long/);
  });
});
