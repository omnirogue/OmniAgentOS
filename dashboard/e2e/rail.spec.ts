import { test, expect } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";
import os from "node:os";
import ts from "typescript";
import {
  missingPreconditionDisposition,
  handleMissingPrecondition,
  assertEventSourceBudget,
  EVENT_SOURCE_BUDGET,
  assertSpecOwnership,
  countAstNodes
} from "./rail.authority";
import config from "../playwright.config";

test.describe("Rail mechanical assertions", () => {
  test("webServer and reuseExistingServer are strictly configured", () => {
    expect(config.webServer).toBeDefined();

    // Config could theoretically be an array of WebServerConfig
    const webServer = Array.isArray(config.webServer) ? config.webServer[0] : config.webServer;

    expect(webServer?.port).toBe(3002);
    expect(webServer?.command).toBe("npx --no-install next dev -H 127.0.0.1 -p 3002");
    expect(webServer?.reuseExistingServer).toBe(false);
    expect(config.use?.baseURL).toBe("http://127.0.0.1:3002");
  });

  test("missingPreconditionDisposition pure function behavior and effect seam", () => {
    expect(missingPreconditionDisposition(undefined)).toBe("skip");
    expect(missingPreconditionDisposition("0")).toBe("skip");
    expect(missingPreconditionDisposition("false")).toBe("skip");
    expect(missingPreconditionDisposition("1")).toBe("fail");

    let skipped = false;
    const testSeam = {
      fail: (reason: string): never => { throw new Error(`EXACT SENTINEL: ${reason}`); },
      skip: (reason: string) => { void reason; skipped = true; }
    };

    const orig = process.env.PLAYWRIGHT_REQUIRED;
    try {
      process.env.PLAYWRIGHT_REQUIRED = "1";
      expect(() => handleMissingPrecondition("test fail reason", testSeam))
        .toThrowError("EXACT SENTINEL: test fail reason");
      expect(skipped).toBe(false);

      process.env.PLAYWRIGHT_REQUIRED = "0";
      handleMissingPrecondition("test skip reason", testSeam);
      expect(skipped).toBe(true);
    } finally {
      if (orig === undefined) {
        delete process.env.PLAYWRIGHT_REQUIRED;
      } else {
        process.env.PLAYWRIGHT_REQUIRED = orig;
      }
    }
  });

  test("EventSource production budget exactly 8, excluding generated/node_modules/e2e", () => {
    const srcDir = path.resolve(__dirname, "../src");
    // Should pass cleanly on real src directory
    expect(() => assertEventSourceBudget(srcDir, EVENT_SOURCE_BUDGET)).not.toThrow();

    // Negative mutation design: tmpdir fixture
    const tmpdir = fs.mkdtempSync(path.join(os.tmpdir(), "rail-fixture-"));
    try {
      const testSrc = path.join(tmpdir, "src");
      fs.mkdirSync(testSrc);

      // Create 8 production constructor files
      for (let i = 1; i <= 8; i++) {
        fs.writeFileSync(path.join(testSrc, `prod${i}.ts`), "const x = new EventSource('/dummy');\n");
      }

      const e2eDir = path.join(testSrc, "e2e");
      fs.mkdirSync(e2eDir);
      fs.writeFileSync(path.join(e2eDir, "test.ts"), "new EventSource('e2e');");

      const genDir = path.join(testSrc, "generated");
      fs.mkdirSync(genDir);
      fs.writeFileSync(path.join(genDir, "gen.ts"), "new EventSource('gen');");

      const nmDir = path.join(testSrc, "node_modules");
      fs.mkdirSync(nmDir);
      fs.writeFileSync(path.join(nmDir, "module.ts"), "new EventSource('nm');");

      // Comment and string false positives
      fs.writeFileSync(path.join(testSrc, "false_positives.ts"), `
        // new EventSource('comment');
        const s = "new EventSource('string')";
      `);

      // Prove budget 8 passes with the 8 prod files (and ignored ones)
      expect(() => assertEventSourceBudget(testSrc, 8)).not.toThrow();

      // Add 9th (RED condition)
      const ninthPath = path.join(testSrc, "ninth.ts");
      fs.writeFileSync(ninthPath, "new EventSource('ninth');");
      expect(() => assertEventSourceBudget(testSrc, 8)).toThrowError(/EventSource budget mismatch/);

      // Restore baseline 8 by removing 9th
      fs.unlinkSync(ninthPath);
      expect(() => assertEventSourceBudget(testSrc, 8)).not.toThrow();

      // Permanent production-to-test swap negative control: move baseline 8 to test
      const prod8Path = path.join(testSrc, "prod8.ts");
      const prod8TestPath = path.join(testSrc, "prod8.test.ts");
      fs.renameSync(prod8Path, prod8TestPath);
      expect(() => assertEventSourceBudget(testSrc, 8)).toThrowError(/EventSource budget mismatch.*found 7/);

      // Explicitly prove root-level *.spec.ts artifacts are excluded
      fs.writeFileSync(path.join(testSrc, "dummy.spec.ts"), "new EventSource('spec');");
      expect(() => assertEventSourceBudget(testSrc, 8)).toThrowError(/EventSource budget mismatch.*found 7/);

      // Explicitly exercise the new fixtures, __tests__, test, and tests exclusions
      const testDirs = ["fixtures", "__tests__", "test", "tests"];
      for (const tDir of testDirs) {
        const fullTDir = path.join(testSrc, tDir);
        fs.mkdirSync(fullTDir);
        fs.writeFileSync(path.join(fullTDir, "exclude_me.ts"), "new EventSource('excluded');");
      }
      expect(() => assertEventSourceBudget(testSrc, 8)).toThrowError(/EventSource budget mismatch.*found 7/);
    } finally {
      fs.rmSync(tmpdir, { recursive: true, force: true });
    }
  });

  test("Test-list mechanical accounting for every e2e spec exactly once", () => {
    const e2eDir = __dirname;
    const allSpecs = fs.readdirSync(e2eDir).filter(f => f.endsWith(".spec.ts"));

    // Prove actual e2e inventory passes
    expect(() => assertSpecOwnership(allSpecs)).not.toThrow();

    // Prove synthetic orphan throws
    expect(() => assertSpecOwnership([...allSpecs, "orphan.spec.ts"])).toThrowError(/Spec ownership mismatch.*orphan/);

    // Prove synthetic duplicate throws (by adding an existing spec again, simulating 2 matches)
    expect(() => assertSpecOwnership([...allSpecs, allSpecs[0]])).toThrowError(/Spec ownership mismatch.*Duplicates/);
  });

  test("Product and Board specs statically avoid raw test.skip usage via AST", () => {
    const files = [
      path.join(__dirname, "product.spec.ts"),
      path.join(__dirname, "board.spec.ts"),
      // Added F06 (2026-08-14): new-ia.spec.ts had 6 unaudited
      // handleMissingPrecondition call sites that skipped without first
      // checking a named ErrorState actually rendered -- this mechanical
      // rail must audit it too, not just product/board.
      path.join(__dirname, "new-ia.spec.ts")
    ];

    const testSkipMatch = (node: ts.Node) => {
      if (ts.isCallExpression(node) && ts.isPropertyAccessExpression(node.expression)) {
        if (ts.isIdentifier(node.expression.expression) && node.expression.expression.text === "test" &&
            ts.isIdentifier(node.expression.name) && node.expression.name.text === "skip") {
          return true;
        }
      }
      return false;
    };

    const handleMissingMatch = (node: ts.Node) => {
      if (ts.isCallExpression(node) && ts.isIdentifier(node.expression)) {
        if (node.expression.text === "handleMissingPrecondition") {
          return true;
        }
      }
      return false;
    };

    const skips = countAstNodes(files, testSkipMatch);
    const handles = countAstNodes(files, handleMissingMatch);

    expect(skips).toBe(0);
    // 7 -> 11 (P7, new-ia IA sweep, 2026-08-14): -1 for the deleted /chats
    // test (route removed outright, its precondition wrap went with it), +4
    // for wrapping the four assertions across these two files that this
    // box's own unconfigured trusted-hop middleware — no
    // OMNIAGENTOS_TRUSTED_HOP_SECRET / dev-escape for this `next dev`,
    // unrelated to the IA redo (see docs/runbooks/dashboard-local-auth.md) —
    // was driving into a hard failure instead of a graceful skip. Each new
    // wrap and the count above were updated together in the same change.
    //
    // 11 -> 11 (F06, 2026-08-14): new-ia.spec.ts joined the files this rail
    // audits (was product.spec.ts=8 + board.spec.ts=3 = 11), but it
    // contributes 0 handleMissingPrecondition calls, not the 6 it had before
    // this fix -- every one of its skip-without-checking-anything quarantine
    // sites was converted to a direct `realContent.or(namedErrorState)`
    // assertion (see e2e/new-ia.spec.ts's file doc comment): an explicit
    // ErrorState is now an asserted PASS, and the absence of both content and
    // ErrorState fails the test outright instead of silently skipping.
    // Arithmetic: 8 (product.spec.ts) + 3 (board.spec.ts) + 0
    // (new-ia.spec.ts) = 11, unchanged from before this file joined the audit.
    expect(handles).toBe(11);
  });
});
