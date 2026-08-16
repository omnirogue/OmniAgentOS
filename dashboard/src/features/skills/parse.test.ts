import { describe, expect, it } from "vitest";
import {
  parseClaudeSkillFrontmatter,
  parseRepoSkillDialectA,
  parseRepoSkillPlain,
  sanitizeSkillId,
  truncate,
} from "./parse";
import { deriveDomainCategory } from "./categorize";

describe("sanitizeSkillId", () => {
  it("accepts a plain lowercase/hyphen id", () => {
    expect(sanitizeSkillId("headlines-and-leads")).toBe("headlines-and-leads");
  });

  it("rejects path traversal and separators", () => {
    expect(sanitizeSkillId("../../etc/passwd")).toBeNull();
    expect(sanitizeSkillId("skills/../../secret")).toBeNull();
    expect(sanitizeSkillId("foo/bar")).toBeNull();
  });

  it("rejects empty, uppercase, and non-string input", () => {
    expect(sanitizeSkillId("")).toBeNull();
    expect(sanitizeSkillId("   ")).toBeNull();
    expect(sanitizeSkillId("Fusion")).toBeNull();
    expect(sanitizeSkillId(null as unknown as string)).toBeNull();
  });

  it("trims surrounding whitespace before validating", () => {
    expect(sanitizeSkillId("  fusion  ")).toBe("fusion");
  });
});

describe("truncate", () => {
  it("leaves short text untouched", () => {
    expect(truncate("short", 200)).toBe("short");
  });

  it("truncates and appends an ellipsis past the limit", () => {
    const long = "a".repeat(250);
    const result = truncate(long, 200);
    expect(result.length).toBe(201); // 200 chars + ellipsis
    expect(result.endsWith("…")).toBe(true);
  });
});

describe("parseClaudeSkillFrontmatter (~/.claude/skills dialect)", () => {
  it("parses name + description from a quoted single-line description", () => {
    const raw = [
      "---",
      "name: scraper",
      'description: Mac Automation Team — delegate browsing/scraping goals.',
      "---",
      "",
      "# Scraper",
      "Body text.",
    ].join("\n");
    const result = parseClaudeSkillFrontmatter(raw);
    expect(result).not.toBeNull();
    expect(result?.data.name).toBe("scraper");
    expect(result?.data.description).toContain("Mac Automation Team");
    expect(result?.body.trim()).toBe("# Scraper\nBody text.");
  });

  it("unescapes a double-quoted description with embedded escapes", () => {
    const raw = [
      "---",
      "name: headlines-and-leads",
      'description: "Run the jobs \\u2014 in order, with \\"quotes\\" inside."',
      "disable-model-invocation: true",
      "---",
      "# Headlines and leads",
    ].join("\n");
    const result = parseClaudeSkillFrontmatter(raw);
    expect(result?.data.name).toBe("headlines-and-leads");
    expect(result?.data.description).toContain('"quotes"');
    // — must decode to an actual em dash, not the literal text "u2014"
    // (14 of the 55 real ~/.claude/skills SKILL.md files use this escape).
    expect(result?.data.description).toContain("jobs — in order");
  });

  it("returns null for a file that never opens with a frontmatter block", () => {
    expect(parseClaudeSkillFrontmatter("# Just a heading\nNo frontmatter here.")).toBeNull();
  });

  it("returns null when the frontmatter block is never closed", () => {
    const raw = ["---", "name: broken", "description: unterminated"].join("\n");
    expect(parseClaudeSkillFrontmatter(raw)).toBeNull();
  });
});

describe("parseRepoSkillDialectA (skills/*/SKILL.md structured dialect)", () => {
  it("parses slug/category/subcategory/title/status and folds a block-scalar summary", () => {
    const raw = [
      "---",
      "slug: long-horizon-delivery",
      "category: Orchestration",
      "subcategory: Long-Horizon Delivery",
      "title: Long-horizon real-world delivery (objective to live outcome)",
      "summary: >-",
      "  Repeatable, autonomous runbook for taking a broad real-world objective all the",
      "  way to a verified live outcome.",
      "status: active",
      "preferred_method: this-runbook",
      "---",
      "",
      "# Long-horizon real-world delivery",
    ].join("\n");
    const result = parseRepoSkillDialectA(raw);
    expect(result).not.toBeNull();
    expect(result?.slug).toBe("long-horizon-delivery");
    expect(result?.category).toBe("Orchestration");
    expect(result?.subcategory).toBe("Long-Horizon Delivery");
    expect(result?.status).toBe("active");
    // Folded block scalar joins its lines with a single space and no trailing newline.
    expect(result?.summary).toBe(
      "Repeatable, autonomous runbook for taking a broad real-world objective all the way to a verified live outcome.",
    );
  });

  it("returns null for a file with no frontmatter (the dialect-B fallback case)", () => {
    const raw = "Create a reflection-triage skill encoding the watchdog checklist.";
    expect(parseRepoSkillDialectA(raw)).toBeNull();
  });
});

describe("parseRepoSkillPlain (no-frontmatter fallback)", () => {
  it("uses the directory name and the first non-empty line as the summary", () => {
    const raw = "\n\nCreate a reflection-triage skill encoding the watchdog checklist as an ordered runbook.\nSecond line ignored.";
    const result = parseRepoSkillPlain("reflection-triage", raw);
    expect(result.name).toBe("reflection-triage");
    expect(result.summary).toBe(
      "Create a reflection-triage skill encoding the watchdog checklist as an ordered runbook.",
    );
  });

  it("strips a leading markdown heading marker from the summary line", () => {
    const result = parseRepoSkillPlain("some-skill", "# Some Skill\nDetails follow.");
    expect(result.summary).toBe("Some Skill");
  });

  it("returns an empty summary for a genuinely empty file", () => {
    const result = parseRepoSkillPlain("empty-skill", "   \n\n  ");
    expect(result.summary).toBe("");
  });
});

describe("deriveDomainCategory", () => {
  it("groups a symlinked domain skill by its ~/Work brand directory", () => {
    const category = deriveDomainCategory(
      "headlines-and-leads",
      true,
      "/Users/youruser/Work/Initech/Marketing/Ad-Webinar-Toolkit/skills/claude-only/headlines-and-leads",
    );
    expect(category).toBe("Initech — domain skill");
  });

  it("groups the scraper symlink under Automation", () => {
    expect(deriveDomainCategory("scraper", true, "/Users/youruser/Desktop/scraper/skill")).toBe(
      "Automation — domain skill",
    );
  });

  it("groups a known native skill by its explicit membership list", () => {
    expect(deriveDomainCategory("fusionbuild", false, null)).toBe("Fusion Orchestration");
    expect(deriveDomainCategory("wrangler", false, null)).toBe("Cloudflare & Web Platform");
    expect(deriveDomainCategory("archi", false, null)).toBe("Dev Tooling");
  });

  it("falls back to an uncategorized bucket for an unrecognized native skill", () => {
    expect(deriveDomainCategory("some-brand-new-skill", false, null)).toBe("Repo Tooling — uncategorized");
  });
});
