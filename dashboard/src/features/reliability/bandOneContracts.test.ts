/**
 * Band 1 regression contract — an executable test, not a grep transcript.
 *
 * Five review rounds accepted shell-grep output as the witness for these three
 * properties. A transcript proves what was true when someone ran it; a test
 * proves it on every run. These assert the properties the Band 1 brief named:
 *
 *   (i)   ONE flag governs every fixture surface (no second flag, no hardcode)
 *   (ii)  nothing imports the deleted reliability org client
 *   (iii) the vendor-direct TTS routes are gone and callers use /api/voice/speak
 *
 * They read source text on purpose: the subject IS the source, and importing the
 * modules would only prove they load, not that the retired shapes are absent.
 */
import { readdirSync, readFileSync, statSync, existsSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const SRC = join(__dirname, "..", "..");

function sourceFiles(dir: string, out: string[] = []): string[] {
  for (const entry of readdirSync(dir)) {
    if (entry === "node_modules" || entry.startsWith(".")) continue;
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) sourceFiles(full, out);
    else if (/\.(ts|tsx)$/.test(entry)) out.push(full);
  }
  return out;
}

const FILES = sourceFiles(SRC);
const read = (f: string) => readFileSync(f, "utf8");

describe("Band 1: fixture honesty", () => {
  // Scoped to the surfaces item 3 owns (leaderboard/tournaments/playbook/lab).
  // pulse is excluded on purpose: it has its own long-standing
  // NEXT_PUBLIC_USE_PULSE_FIXTURES flag and the brief only asked to retire a stale
  // comment there, not to unify its flag.
  // Other features have their own long-standing
  // fixture flags; unifying those was never in this lane's contract, and asserting a
  // repo-wide property the brief did not claim is the same overbroad error this batch
  // kept making in prose.
  const OWNED = ["leaderboard", "tournaments", "playbook", "experiments"];
  const owned = (f: string) => OWNED.some((seg) => f.includes(`/${seg}`));

  it("has no hardcoded fixture switch on the surfaces this lane owns", () => {
    const offenders = FILES.filter(owned)
      .filter((f) => !f.endsWith("bandOneContracts.test.ts"))
      .filter((f) => /const\s+USE_FIXTURES\s*(:\s*boolean\s*)?=\s*true/.test(read(f)));
    expect(offenders, "these surfaces must not hardcode fixtures on").toEqual([]);
  });

  it("derives the owned fixture flags FROM the shared lab flag", () => {
    // Absence of bad shapes is not the invariant. `export const USE_FIXTURES =
    // process.env.NODE_ENV !== "production"` trips neither of the checks above
    // while making these surfaces independent of the lab flag — the exact drift
    // this lane closed. So require the derivation itself.
    const fixtureModules = FILES.filter(owned).filter((f) => /fixtures\.ts$/.test(f));
    expect(fixtureModules.length, "expected at least one owned fixture module").toBeGreaterThan(0);
    const undeclared = fixtureModules.filter((f) => {
      const src = read(f);
      if (!/export\s+const\s+USE_FIXTURES\b/.test(src)) return false; // nothing to derive
      const importsLab =
        /import\s*\{[^}]*USE_FIXTURES[^}]*\}\s*from\s*["'][^"']*experiments\/client["']/.test(src) ||
        /NEXT_PUBLIC_LAB_USE_FIXTURES/.test(src);
      return !importsLab;
    });
    expect(undeclared, "owned fixture modules must derive from the lab flag").toEqual([]);
  });

  it("routes those surfaces through the one shared lab flag", () => {
    const wrongFlag: string[] = [];
    for (const f of FILES.filter(owned)) {
      if (f.endsWith("bandOneContracts.test.ts")) continue;
      for (const flag of read(f).match(/NEXT_PUBLIC_[A-Z0-9_]*FIXTURE[A-Z0-9_]*/g) ?? []) {
        if (flag !== "NEXT_PUBLIC_LAB_USE_FIXTURES") wrongFlag.push(`${f} (${flag})`);
      }
    }
    expect(wrongFlag, "owned surfaces must use NEXT_PUBLIC_LAB_USE_FIXTURES").toEqual([]);
  });
});

describe("Band 1: the reliability org client is retired", () => {
  const DELETED = [
    "fetchOrgTree",
    "fetchOrgAgents",
    "toggleAgent",
    "fetchAgentActivity",
    "useOrganizationDashboard",
    "fetchAgentRequests",
    "createAgentRequest",
    "decideAgentRequest",
  ];

  it("exports none of the deleted symbols from features/reliability", () => {
    const surface = ["api.ts", "hooks.ts", "index.ts"]
      .map((f) => join(SRC, "features", "reliability", f))
      .filter(existsSync)
      .map(read)
      .join("\n");
    const still = DELETED.filter((sym) =>
      new RegExp(`export\\s+(async\\s+)?(function|const)\\s+${sym}\\b`).test(surface) ||
      new RegExp(`\\b${sym}\\b\\s*,?\\s*\\n?[^\\n]*}\\s*from`).test(surface),
    );
    expect(still, "deleted org symbols must not be re-exported").toEqual([]);
  });

  it("has no importer of those symbols from the reliability feature", () => {
    const importers: string[] = [];
    for (const f of FILES) {
      if (f.endsWith("bandOneContracts.test.ts")) continue;
      const src = read(f);
      // only imports FROM the reliability client count; unrelated same-named
      // helpers elsewhere (e.g. features/system) are legitimately different.
      const re = /import\s*(?:type\s*)?\{([^}]*)\}\s*from\s*["'][^"']*features\/reliability[^"']*["']/g;
      for (const m of src.matchAll(re)) {
        for (const sym of m[1].split(",").map((s) => s.trim().split(" as ")[0].trim())) {
          if (DELETED.includes(sym)) importers.push(`${f} -> ${sym}`);
        }
      }
    }
    expect(importers, "nothing may import the deleted reliability org client").toEqual([]);
  });
});

describe("Band 1: TTS is unified on the backend voice route", () => {
  it("removed the vendor-direct app/api/tts routes", () => {
    for (const vendor of ["xai", "elevenlabs"]) {
      expect(
        existsSync(join(SRC, "app", "api", "tts", vendor, "route.ts")),
        `app/api/tts/${vendor} must not exist`,
      ).toBe(false);
    }
  });

  it("has no caller of the retired vendor routes, and does call /api/voice/speak", () => {
    const callers: string[] = [];
    let speakCallers = 0;
    for (const f of FILES) {
      if (f.endsWith("bandOneContracts.test.ts")) continue;
      const src = read(f);
      if (/["'`][^"'`]*\/api\/tts\/(xai|elevenlabs)/.test(src)) callers.push(f);
      if (/\/api\/voice\/speak/.test(src)) speakCallers += 1;
    }
    expect(callers, "no caller may hit the retired vendor TTS routes").toEqual([]);
    expect(speakCallers, "at least one caller must use /api/voice/speak").toBeGreaterThan(0);
  });
});
