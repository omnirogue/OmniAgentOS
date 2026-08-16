import { describe, expect, it } from "vitest";
import {
  extractMentionQuery,
  searchSkills,
  applyMention,
} from "./skillMention";
import type { SkillTreeEntry } from "./chatApi";

const SKILLS: SkillTreeEntry[] = [
  { id: "s1", name: "pdf", version: "1.0", path: "/skills/pdf", description: "Parse PDFs" },
  { id: "s2", name: "xlsx", version: "1.0", path: "/skills/xlsx", description: "Read Excel files" },
  { id: "s3", name: "dataviz", version: "2.0", path: "/skills/dataviz", description: "Charts and graphs" },
  { id: "s4", name: "new-app", version: "1.0", path: "/skills/new-app", description: "Scaffold new apps" },
  { id: "s5", name: "review", version: "1.4", path: "/skills/review", description: "Code review" },
];

describe("extractMentionQuery", () => {
  it("returns null when cursor is at position 0", () => {
    expect(extractMentionQuery("", 0)).toBeNull();
  });

  it("detects @ at the start of text", () => {
    const result = extractMentionQuery("@pd", 3);
    expect(result).not.toBeNull();
    expect(result!.query).toBe("pd");
    expect(result!.start).toBe(0);
    expect(result!.end).toBe(3);
  });

  it("detects @ after whitespace", () => {
    const result = extractMentionQuery("hello @da", 9);
    expect(result).not.toBeNull();
    expect(result!.query).toBe("da");
    expect(result!.start).toBe(6);
    expect(result!.end).toBe(9);
  });

  it("returns null when @ is preceded by non-whitespace (e.g. email)", () => {
    const result = extractMentionQuery("user@example", 12);
    expect(result).toBeNull();
  });

  it("returns null when a space is between @ and cursor", () => {
    const result = extractMentionQuery("@pdf test", 9);
    expect(result).toBeNull();
  });

  it("handles @ followed by nothing (empty query)", () => {
    const result = extractMentionQuery("hello @", 7);
    expect(result).not.toBeNull();
    expect(result!.query).toBe("");
    expect(result!.start).toBe(6);
    expect(result!.end).toBe(7);
  });

  it("does not trigger after newline", () => {
    const result = extractMentionQuery("line1\n@pd", 9);
    expect(result).not.toBeNull();
    expect(result!.query).toBe("pd");
  });

  it("returns null for cursor past the @ segment", () => {
    const result = extractMentionQuery("@pdf extra", 10);
    expect(result).toBeNull();
  });
});

describe("searchSkills", () => {
  it("returns all skills for empty query", () => {
    expect(searchSkills(SKILLS, "")).toHaveLength(5);
  });

  it("filters by name substring", () => {
    const result = searchSkills(SKILLS, "pdf");
    expect(result).toHaveLength(1);
    expect(result[0]!.name).toBe("pdf");
  });

  it("is case-insensitive", () => {
    const result = searchSkills(SKILLS, "DAT");
    expect(result).toHaveLength(1);
    expect(result[0]!.name).toBe("dataviz");
  });

  it("matches description", () => {
    const result = searchSkills(SKILLS, "scaffold");
    expect(result).toHaveLength(1);
    expect(result[0]!.name).toBe("new-app");
  });

  it("returns empty array for no matches", () => {
    expect(searchSkills(SKILLS, "zzzzz")).toHaveLength(0);
  });

  it("returns multiple matches for broad query", () => {
    const result = searchSkills(SKILLS, "e");
    expect(result.length).toBeGreaterThan(1);
  });
});

describe("applyMention", () => {
  it("replaces @query with @{name} and trailing space", () => {
    const mention = { query: "pd", start: 6, end: 9 };
    const result = applyMention("hello @pdmore", mention, SKILLS[0]!);
    expect(result.text).toBe("hello @{pdf} more");
    expect(result.cursor).toBe("hello @{pdf} ".length);
  });

  it("works at the start of text", () => {
    // Replaces only the typed query span [start, end) — text after the cursor
    // ("view this") is preserved, exactly as extractMentionQuery would drive it
    // for cursor position 3 in "@review this".
    const mention = { query: "re", start: 0, end: 3 };
    const result = applyMention("@review this", mention, SKILLS[4]!);
    expect(result.text).toBe("@{review} view this");
  });

  it("works with empty query (just @)", () => {
    const mention = { query: "", start: 5, end: 6 };
    const result = applyMention("test @after", mention, SKILLS[2]!);
    expect(result.text).toBe("test @{dataviz} after");
  });
});
