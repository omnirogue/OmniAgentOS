/**
 * Skills page — data types.
 *
 * Three independent sources feed the /skills page (see
 * `api/local/skills-extra/route.ts`):
 *
 *  - `library`  — every directory in `~/.claude/skills` (the real, invocable
 *    Claude Code skill set). 19 of these are symlinks into domain sources
 *    (18 copywriting/webinar/media-buying skills under Initech's
 *    Ad-Webinar-Toolkit, 1 scraper skill under ~/Desktop/scraper).
 *  - `repoSkills` — the 2 skills that live in this repo at `skills/<id>/SKILL.md`,
 *    which use a different frontmatter dialect (or none at all).
 *  - `dormant` — the `skills` DB table's webinar/infrastructure seed rows,
 *    which are NOT wired into the live library above and must never be
 *    presented as if they were.
 *
 * Each section reports its own `source`/`error` — a failure in one must never
 * blank the other two (see the route's independent try/catch per section).
 */

export type SectionSource = "live" | "error";

/** One entry from `~/.claude/skills/<id>/SKILL.md`. */
export interface DomainSkillEntry {
  /** Directory name under `~/.claude/skills` — also the URL id for `/skills/[id]`. */
  id: string;
  /** Frontmatter `name:`, falling back to the directory name. */
  name: string;
  /** Frontmatter `description:`, truncated to 200 chars. Empty string if absent/unreadable. */
  description: string;
  /** Derived grouping — see `categorize.ts`. There is no `category:` field in this
   * dialect's frontmatter, so this is a best-effort heuristic, not authoritative. */
  category: string;
  /** True when the directory entry is a symlink (a "domain skill" sourced elsewhere). */
  isSymlink: boolean;
  /** Resolved absolute target path for a symlink entry, or null (not a symlink, or the
   * link target could not be resolved — e.g. dangling). */
  symlinkTarget: string | null;
}

export interface SkillLibrarySection {
  source: SectionSource;
  error: string | null;
  skills: DomainSkillEntry[];
  scannedAt: string | null;
}

export type RepoSkillDialect = "frontmatter" | "plain";

/** One entry from this repo's `skills/<id>/SKILL.md`. */
export interface RepoSkillEntry {
  id: string;
  name: string;
  summary: string;
  /** `status:` from dialect-A frontmatter (long-horizon-delivery style); null for the
   * plain (no-frontmatter) dialect. */
  status: string | null;
  dialect: RepoSkillDialect;
  path: string;
}

export interface RepoSkillsSection {
  source: SectionSource;
  error: string | null;
  skills: RepoSkillEntry[];
}

/** One row from the `skills` DB table's dormant webinar/infrastructure seed rows —
 * never injected into any live selection path, content digests untouched since
 * migration. Shown for transparency only. */
export interface DormantSeedEntry {
  id: string;
  category: string;
  status: string;
}

export interface DormantSection {
  source: SectionSource;
  error: string | null;
  seeds: DormantSeedEntry[];
}

export interface SkillsExtraPayload {
  generatedAt: string;
  library: SkillLibrarySection;
  repoSkills: RepoSkillsSection;
  dormant: DormantSection;
  /** BLOCKER 2 (cross-lineage review round 2, 2026-08-14): set only when the
   * route serves a previous snapshot because a fresh build did not settle
   * within its deadline — the data is real but may be older than the normal
   * cache TTL. Absent (not `false`) on every normal response; purely
   * additive, existing consumers that don't check it are unaffected. */
  stale?: boolean;
}

/** GET /api/local/skills-extra?id=<id> — the [id] detail rewire. */
export interface SkillDetailPayload {
  id: string;
  exists: boolean;
  isSymlink: boolean;
  symlinkTarget: string | null;
  name: string;
  description: string;
  /** Raw SKILL.md body after the frontmatter block (or the whole file if there is none). */
  body: string;
  error?: string;
}
