/**
 * Supplemental Node W3C EventSource harness (not the B06 browser requirement).
 *
 * Uses Node's experimental EventSource client against the same-origin isolated
 * SSE fixture. Useful for fast CI-without-browser smoke checks.
 *
 * Truthful claims only:
 * - This is Node's experimental EventSource, NOT Chromium.
 * - Same-origin supplemental proof only; product-origin cross-origin + CORS
 *   is covered by eventsource.chromium.spec.ts in a real browser.
 */
import { mkdirSync, writeFileSync } from "node:fs";
import path from "node:path";
import { spawn } from "node:child_process";
import { test, expect } from "@playwright/test";
import { B06_E2E_OUT } from "./runtimePaths";
import { startSseFixture, stopSseFixture } from "./sseHarness";

type NodeProbeResult = {
  opened: boolean;
  receivedType: string | null;
  errorSeen: boolean;
};

/** Node experimental EventSource client — explicitly NOT a browser. */
function runNodeEventSourceClient(baseUrl: string): Promise<NodeProbeResult> {
  return new Promise((resolve, reject) => {
    const child = spawn(
      process.execPath,
      [
        "--experimental-eventsource",
        "--input-type=module",
        "-e",
        `
const baseUrl = ${JSON.stringify(baseUrl)};
const source = new EventSource(baseUrl + "/api/events?after_id=0");
let opened = false;
let receivedType = null;
let errorSeen = false;
const finish = () => {
  source.close();
  process.stdout.write(JSON.stringify({ opened, receivedType, errorSeen }));
  process.exit(0);
};
source.onopen = () => { opened = true; };
source.addEventListener("run.updated", (ev) => {
  try {
    const row = JSON.parse(ev.data);
    receivedType = row.type ?? "run.updated";
  } catch {
    receivedType = "parse-error";
  }
  source.close();
  const dead = new EventSource(baseUrl + "/api/missing");
  dead.onerror = () => {
    errorSeen = true;
    dead.close();
    finish();
  };
  setTimeout(() => {
    if (!errorSeen) {
      errorSeen = dead.readyState === 2;
      dead.close();
      finish();
    }
  }, 1500);
});
source.onerror = () => {
  if (!opened && !receivedType) {
    errorSeen = true;
    finish();
  }
};
setTimeout(() => finish(), 5000);
`,
      ],
      { stdio: ["ignore", "pipe", "pipe"] },
    );
    let out = "";
    let err = "";
    child.stdout.on("data", (chunk) => {
      out += String(chunk);
    });
    child.stderr.on("data", (chunk) => {
      err += String(chunk);
    });
    child.on("error", reject);
    child.on("close", (code) => {
      try {
        resolve(JSON.parse(out.trim()) as NodeProbeResult);
      } catch (reason) {
        reject(
          new Error(
            `Node EventSource client failed (code=${code}): ${err || out || String(reason)}`,
          ),
        );
      }
    });
  });
}

test.describe("Node W3C EventSource harness (supplemental, not browser)", () => {
  test("connects, receives run.updated, and surfaces onerror", async () => {
    mkdirSync(B06_E2E_OUT, { recursive: true });
    const fixture = await startSseFixture();
    try {
      const result = await runNodeEventSourceClient(fixture.baseUrl);
      writeFileSync(
        path.join(B06_E2E_OUT, "eventsource-node-result.json"),
        JSON.stringify(
          {
            harness: "node-experimental-eventsource",
            note: "Supplemental only — B06 browser requirement is Chromium",
            baseUrl: fixture.baseUrl,
            port: fixture.port,
            result,
            at: new Date().toISOString(),
          },
          null,
          2,
        ),
      );
      expect(result.opened).toBe(true);
      expect(result.receivedType).toBe("run.updated");
      expect(result.errorSeen).toBe(true);
    } finally {
      await stopSseFixture(fixture);
    }
  });
});
