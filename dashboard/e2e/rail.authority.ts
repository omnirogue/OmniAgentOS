import ts from "typescript";
import fs from "node:fs";
import path from "node:path";
import { test } from "@playwright/test";

export const E2E_PORT = 3002;
export const E2E_BASE_URL = `http://127.0.0.1:${E2E_PORT}`;
export const REQUIRED_MODE_ENV = "PLAYWRIGHT_REQUIRED";
export const EVENT_SOURCE_BUDGET = 8;

// The LOCAL live dashboard (com.omniagentos.dashboard), not a remote
// deployment — a loopback address on this machine, same as E2E_BASE_URL's
// cold-started :3002 dev server, but already running and never spawned by us.
export const E2E_PRODUCTION_PORT = 3003;
export const E2E_PRODUCTION_BASE_URL = `http://127.0.0.1:${E2E_PRODUCTION_PORT}`;

// When set to "1", playwright.config.ts omits the top-level `webServer` entry
// entirely (Playwright's webServer is config-global, not per-project — there is
// no way to spawn it for the default projects but not for `production`). This
// keeps an isolated `--project=production` run from cold-starting the :3002 dev
// server it has no use for. Unset (the default) reproduces today's config byte
// for byte, so every existing lane — including this rail check — is unaffected.
export const SKIP_DEV_SERVER_ENV = "E2E_SKIP_DEV_SERVER";

export const PROJECTS = [
  { name: "chromium", match: /eventsource\.chromium\.spec\.ts$/ },
  { name: "node-harness", match: /eventsource\.node\.spec\.ts$/ },
  // "new-ia" added 2026-08-14 (P7, new-ia IA sweep): the 10-page-nav smoke
  // spec needs the same Desktop Chrome + :3002 dev-server project as the
  // other product-qa specs, so it is folded into the same pattern rather
  // than minting a new project (see docs/TESTING-E2E-NOTES.md §2 "Playwright
  // configuration project registration gotcha").
  { name: "product-qa", match: /(product|board|team|new-ia|testing|compute)\.spec\.ts$/ },
  { name: "@visual", match: /visual\.spec\.ts$/ },
  { name: "rail", match: /rail\.spec\.ts$/ },
  // Reserved for e2e/production.spec.ts: read-only smoke checks (page loads,
  // navigation, SSE connect — nothing that mutates state) run against the LOCAL
  // live dashboard at E2E_PRODUCTION_BASE_URL, selected only via
  // `--project=production` from the feature-health tier3 runner, never in a
  // default `npx playwright test`. No such spec file exists yet in this change;
  // this entry exists so that when one lands, `assertSpecOwnership` (below)
  // rejects it as an orphan if it is ever added without this registration, and
  // rejects a collision if some other project's match also claims it.
  { name: "production", match: /production\.spec\.ts$/ },
];

export function missingPreconditionDisposition(envValue?: string): "fail" | "skip" {
  return envValue === "1" ? "fail" : "skip";
}

export interface EffectSeam {
  fail: (reason: string) => never;
  skip: (reason: string) => void;
}

export const defaultEffectSeam: EffectSeam = {
  fail: (reason: string) => { throw new Error(`[REQUIRED MODE] ${reason}`); },
  skip: (reason: string) => { test.skip(true, reason); }
};

export function handleMissingPrecondition(reason: string, seam: EffectSeam = defaultEffectSeam) {
  const disp = missingPreconditionDisposition(process.env[REQUIRED_MODE_ENV]);
  if (disp === "fail") {
    seam.fail(reason);
  } else {
    seam.skip(reason);
  }
}

export const EXCLUDED_DIRS = new Set([
  "e2e", "generated", "node_modules", "dist", ".next",
  "fixtures", "__tests__", "test", "tests"
]);

export interface EventSourceSite {
  file: string;
  line: number;
  column: number;
}

export function scanEventSources(dir: string): EventSourceSite[] {
  const sites: EventSourceSite[] = [];
  if (!fs.existsSync(dir)) return sites;

  function walk(currentPath: string) {
    const entries = fs.readdirSync(currentPath, { withFileTypes: true });
    for (const entry of entries) {
      const fullPath = path.join(currentPath, entry.name);
      if (entry.isDirectory()) {
        if (!EXCLUDED_DIRS.has(entry.name)) {
          walk(fullPath);
        }
      } else if (entry.isFile() && (entry.name.endsWith(".ts") || entry.name.endsWith(".tsx"))) {
        if (entry.name.match(/\.(test|spec)\.tsx?$/)) continue;

        const content = fs.readFileSync(fullPath, "utf8");
        const sourceFile = ts.createSourceFile(fullPath, content, ts.ScriptTarget.Latest, true);

        function visit(node: ts.Node) {
          if (ts.isNewExpression(node) && ts.isIdentifier(node.expression) && node.expression.text === "EventSource") {
            const { line, character } = sourceFile.getLineAndCharacterOfPosition(node.getStart());
            sites.push({ file: fullPath, line: line + 1, column: character + 1 });
          }
          ts.forEachChild(node, visit);
        }

        visit(sourceFile);
      }
    }
  }

  walk(dir);
  return sites;
}

export function assertEventSourceBudget(dir: string, budget: number = EVENT_SOURCE_BUDGET): EventSourceSite[] {
  const sites = scanEventSources(dir);
  if (sites.length !== budget) {
    throw new Error(`EventSource budget mismatch. Expected ${budget}, found ${sites.length}. Sites:\n${JSON.stringify(sites, null, 2)}`);
  }
  return sites;
}

export function inspectSpecOwnership(specs: string[]): { orphans: string[]; duplicates: string[] } {
  const accountedSpecs = new Map<string, number>();
  for (const spec of specs) {
    accountedSpecs.set(spec, 0);
  }

  for (const spec of specs) {
    for (const project of PROJECTS) {
      if (project.match.test(spec)) {
        accountedSpecs.set(spec, accountedSpecs.get(spec)! + 1);
      }
    }
  }

  const orphans: string[] = [];
  const duplicates: string[] = [];
  for (const [spec, count] of accountedSpecs.entries()) {
    if (count === 0) orphans.push(spec);
    if (count > 1) duplicates.push(spec);
  }

  return { orphans, duplicates };
}

export function assertSpecOwnership(specs: string[]) {
  const { orphans, duplicates } = inspectSpecOwnership(specs);
  if (orphans.length > 0 || duplicates.length > 0) {
    throw new Error(`Spec ownership mismatch. Orphans: ${JSON.stringify(orphans)}. Duplicates: ${JSON.stringify(duplicates)}.`);
  }
}

export function countAstNodes(files: string[], matchFn: (node: ts.Node, sourceFile: ts.SourceFile) => boolean): number {
  let count = 0;
  for (const file of files) {
    if (!fs.existsSync(file)) continue;
    const content = fs.readFileSync(file, "utf8");
    const sourceFile = ts.createSourceFile(file, content, ts.ScriptTarget.Latest, true);

    function visit(node: ts.Node) {
      if (matchFn(node, sourceFile)) {
        count++;
      }
      ts.forEachChild(node, visit);
    }

    visit(sourceFile);
  }
  return count;
}
