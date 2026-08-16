/**
 * Pure parsers for the /skills page — no fs/network here so these are directly
 * unit-testable (parse.test.ts) and shared between the server route
 * (api/local/skills-extra) and, if ever needed, the client.
 */

/** Only lowercase letters, digits, and hyphens — matches every real directory
 * name under `~/.claude/skills` and this repo's `skills/`. Used both to badge
 * the id as safe and, on the server, as a path-traversal allowlist for the
 * `[id]` detail rewire (no `.`, `/`, or anything else that could escape the
 * skills root). */
const SKILL_ID_RE = /^[a-z0-9-]+$/;

export function sanitizeSkillId(raw: string): string | null {
  if (typeof raw !== "string") return null;
  const trimmed = raw.trim();
  if (trimmed.length === 0 || trimmed.length > 128) return null;
  return SKILL_ID_RE.test(trimmed) ? trimmed : null;
}

export function truncate(text: string, max: number): string {
  const trimmed = text.trim();
  if (trimmed.length <= max) return trimmed;
  return `${trimmed.slice(0, max).trimEnd()}…`;
}

/** Strip a single or double YAML scalar quoting and unescape it. Unquoted
 * scalars are returned trimmed, verbatim. */
function unquoteYamlScalar(raw: string): string {
  const value = raw.trim();
  if (value.length >= 2 && value.startsWith('"') && value.endsWith('"')) {
    const inner = value.slice(1, -1);
    let out = "";
    for (let i = 0; i < inner.length; i += 1) {
      if (inner[i] === "\\" && i + 1 < inner.length) {
        const next = inner[i + 1];
        if (next === "n") {
          out += "\n";
          i += 1;
        } else if (next === "t") {
          out += "\t";
          i += 1;
        } else if (next === "u" && /^[0-9a-fA-F]{4}/.test(inner.slice(i + 2, i + 6))) {
          out += String.fromCharCode(parseInt(inner.slice(i + 2, i + 6), 16));
          i += 5; // \ u X X X X — 6 chars total, loop's own +1 covers the last one
        } else {
          out += next;
          i += 1;
        }
      } else {
        out += inner[i];
      }
    }
    return out;
  }
  if (value.length >= 2 && value.startsWith("'") && value.endsWith("'")) {
    return value.slice(1, -1).replace(/''/g, "'");
  }
  return value;
}

const BLOCK_SCALAR_MARKERS = new Set([">-", ">", "|-", "|"]);

/**
 * Minimal front-matter reader: `---\nkey: value\n...\n---\n<body>`. Supports
 * plain `key: value` (quoted or not) and YAML block scalars (`>-`/`>`/`|-`/`|`)
 * for multi-line values like a folded `summary:` — enough for the two
 * dialects this page reads (Claude Code skill wrappers, and this repo's
 * `skills/<id>/SKILL.md`). Returns null when the file doesn't open with `---`
 * or the block is never closed — callers fall back to whole-file handling.
 */
export function parseFrontmatter(raw: string): { data: Record<string, string>; body: string } | null {
  const lines = raw.split("\n");
  if (lines.length === 0 || lines[0]!.trim() !== "---") return null;

  const data: Record<string, string> = {};
  let i = 1;
  let closed = false;

  while (i < lines.length) {
    const line = lines[i]!;
    if (line.trim() === "---") {
      closed = true;
      i += 1;
      break;
    }
    const match = /^([A-Za-z_][\w-]*):(?:\s(.*))?$/.exec(line);
    if (!match) {
      i += 1;
      continue;
    }
    const key = match[1]!;
    const rest = (match[2] ?? "").trim();
    if (BLOCK_SCALAR_MARKERS.has(rest)) {
      const blockLines: string[] = [];
      i += 1;
      while (i < lines.length) {
        const candidate = lines[i]!;
        if (candidate.trim() === "---") break;
        if (candidate.trim() === "") {
          i += 1;
          continue;
        }
        if (!/^\s/.test(candidate)) break; // back to column 0 — next top-level key
        blockLines.push(candidate.trim());
        i += 1;
      }
      const joiner = rest.startsWith("|") ? "\n" : " ";
      data[key] = blockLines.join(joiner).trim();
      continue;
    }
    data[key] = unquoteYamlScalar(rest);
    i += 1;
  }

  if (!closed) return null;
  return { data, body: lines.slice(i).join("\n") };
}

export interface ClaudeSkillFrontmatter {
  name: string | null;
  description: string | null;
}

/** Dialect used by every `~/.claude/skills/<id>/SKILL.md` — `name:` + `description:`,
 * both usually a single (possibly very long) quoted line. */
export function parseClaudeSkillFrontmatter(raw: string): { data: ClaudeSkillFrontmatter; body: string } | null {
  const parsed = parseFrontmatter(raw);
  if (!parsed) return null;
  return {
    data: {
      name: parsed.data.name?.trim() || null,
      description: parsed.data.description?.trim() || null,
    },
    body: parsed.body,
  };
}

export interface RepoSkillDialectA {
  slug: string | null;
  category: string | null;
  subcategory: string | null;
  title: string;
  summary: string;
  status: string | null;
}

/** Dialect A — this repo's structured `skills/<id>/SKILL.md` frontmatter
 * (`slug`/`category`/`subcategory`/`title`/`summary`/`status`), as used by
 * `skills/long-horizon-delivery/SKILL.md`. `summary` is commonly a folded
 * block scalar (`summary: >-`). Returns null when the file has no frontmatter
 * at all, so callers can fall back to `parseRepoSkillPlain`. */
export function parseRepoSkillDialectA(raw: string): RepoSkillDialectA | null {
  const parsed = parseFrontmatter(raw);
  if (!parsed) return null;
  const { data } = parsed;
  if (!data.title && !data.slug) return null;
  return {
    slug: data.slug?.trim() || null,
    category: data.category?.trim() || null,
    subcategory: data.subcategory?.trim() || null,
    title: data.title?.trim() || data.slug?.trim() || "Untitled skill",
    summary: data.summary?.trim() || "",
    status: data.status?.trim() || null,
  };
}

export interface RepoSkillPlain {
  name: string;
  summary: string;
}

/** Dialect B (no frontmatter) — e.g. `skills/reflection-triage/SKILL.md`, a bare
 * prompt file with no `---` block at all: name comes from the directory, the
 * first non-empty line stands in for a summary. */
export function parseRepoSkillPlain(dirName: string, raw: string): RepoSkillPlain {
  const firstLine = raw.split("\n").map((line) => line.trim()).find((line) => line.length > 0) ?? "";
  return {
    name: dirName,
    summary: firstLine.replace(/^#+\s*/, ""),
  };
}
