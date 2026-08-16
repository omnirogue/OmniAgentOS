/**
 * Best-effort category grouping for `~/.claude/skills` entries. There is no
 * `category:` field in this frontmatter dialect (verified: none of the 55
 * on-disk SKILL.md files declare one), so this derives a label instead of
 * inventing one out of thin air:
 *
 *  - Symlinked (domain) skills are grouped by the brand/source directory
 *    their target resolves under (e.g. `~/Initech/...` → "Initech").
 *  - Native (repo-authored, non-symlink) skills are grouped by an explicit,
 *    reviewable membership list below. Anything not on the list — including
 *    every new skill added after this file was written — falls into the
 *    "Repo Tooling — uncategorized" bucket rather than being mis-labeled or
 *    silently dropped.
 */

const FUSION_ORCHESTRATION = new Set([
  "fusion", "fusionbuild", "ultrabuild", "superfast", "ultrafast", "orchestrate",
  "review", "improve", "improveswarm",
  "fimibuild3", "fimibuild5", "fimiplan3", "fimiplan5",
  "fimiswarmplan", "fimiswarmplan3", "ultrafimiplan",
  "fable-domain", "fable-judge", "fable-loop", "fable-method",
  "heartbeat", "accelerate", "turnstile-spin",
]);

const CLOUDFLARE_WEB = new Set([
  "cloudflare", "cloudflare-email-service", "cloudflare-one", "cloudflare-one-migrations",
  "durable-objects", "wrangler", "workers-best-practices", "web-perf",
]);

const DEV_TOOLING = new Set(["agents-sdk", "sandbox-sdk", "go", "archi", "deepresearch"]);

const FALLBACK_CATEGORY = "Repo Tooling — uncategorized";

function brandFromTarget(target: string): string | null {
  const workMatch = /\/Work\/([^/]+)\//.exec(target);
  if (workMatch) return `${workMatch[1]} — domain skill`;
  if (target.includes("/Desktop/")) return "Automation — domain skill";
  return null;
}

export function deriveDomainCategory(dirName: string, isSymlink: boolean, target: string | null): string {
  if (isSymlink) {
    return (target && brandFromTarget(target)) ?? "External — domain skill";
  }
  if (FUSION_ORCHESTRATION.has(dirName)) return "Fusion Orchestration";
  if (CLOUDFLARE_WEB.has(dirName)) return "Cloudflare & Web Platform";
  if (DEV_TOOLING.has(dirName)) return "Dev Tooling";
  return FALLBACK_CATEGORY;
}
