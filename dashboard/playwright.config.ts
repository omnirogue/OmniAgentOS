import { defineConfig, devices } from "@playwright/test";
import path from "node:path";
import { B06_E2E_OUT } from "./e2e/runtimePaths";
import {
  E2E_PORT,
  E2E_BASE_URL,
  E2E_PRODUCTION_BASE_URL,
  SKIP_DEV_SERVER_ENV,
  PROJECTS,
} from "./e2e/rail.authority";

export default defineConfig({
  testDir: "./e2e",
  testMatch: /.*\.spec\.ts$/,
  timeout: 45_000,
  fullyParallel: false,
  retries: 0,
  workers: 1,
  reporter: [
    ["list"],
    ["json", { outputFile: path.join(B06_E2E_OUT, "playwright-report.json") }],
  ],
  outputDir: path.join(B06_E2E_OUT, "test-results"),
  // Undefined only when SKIP_DEV_SERVER_ENV is explicitly set — an isolated
  // `--project=production` run has no use for the :3002 dev server it would
  // otherwise cold-start. Unset (every existing lane, including this rail
  // check) reproduces the object below exactly, so default behavior is
  // byte-for-byte unchanged.
  webServer:
    process.env[SKIP_DEV_SERVER_ENV] === "1"
      ? undefined
      : {
          command: `npx --no-install next dev -H 127.0.0.1 -p ${E2E_PORT}`,
          port: E2E_PORT,
          reuseExistingServer: false,
        },
  use: {
    baseURL: E2E_BASE_URL,
    headless: true,
    trace: "off",
    video: "off",
    screenshot: "off",
    launchOptions: {
      chromiumSandbox: false,
      args: [
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--single-process",
        "--no-zygote",
        "--disable-gpu",
        "--disable-dev-shm-usage",
      ],
    },
  },
  projects: [
    {
      name: "chromium",
      testMatch: PROJECTS.find(p => p.name === "chromium")!.match,
      use: {
        ...devices["Desktop Chrome"],
        browserName: "chromium",
        launchOptions: {
          chromiumSandbox: false,
          args: [
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--single-process",
            "--no-zygote",
            "--disable-gpu",
            "--disable-dev-shm-usage",
          ],
        },
      },
    },
    {
      name: "node-harness",
      testMatch: PROJECTS.find(p => p.name === "node-harness")!.match,
      // No browser required — Node experimental EventSource only.
    },
    {
      name: "product-qa",
      testMatch: PROJECTS.find(p => p.name === "product-qa")!.match,
      use: {
        ...devices["Desktop Chrome"],
        browserName: "chromium",
        launchOptions: {
          chromiumSandbox: false,
          args: [
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--single-process",
            "--no-zygote",
            "--disable-gpu",
            "--disable-dev-shm-usage",
          ],
        },
      },
    },
    {
      name: "@visual",
      testMatch: PROJECTS.find(p => p.name === "@visual")!.match,
      use: {
        ...devices["Desktop Chrome"],
        browserName: "chromium",
        launchOptions: {
          chromiumSandbox: false,
          args: [
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--single-process",
            "--no-zygote",
            "--disable-gpu",
            "--disable-dev-shm-usage",
          ],
        },
      },
    },
    {
      name: "rail",
      testMatch: PROJECTS.find(p => p.name === "rail")!.match,
    },
    {
      // Read-only production smoke checks against the LOCAL live dashboard
      // (:3003, already running — see E2E_PRODUCTION_BASE_URL). Never selected
      // by a bare `npx playwright test`; run only via `--project=production`
      // from the feature-health tier3 lane. No webServer of its own — Playwright
      // webServer is config-global (see SKIP_DEV_SERVER_ENV above), so an
      // isolated production run should also set SKIP_DEV_SERVER_ENV=1 to avoid
      // cold-starting the unrelated :3002 dev server.
      name: "production",
      testMatch: PROJECTS.find(p => p.name === "production")!.match,
      use: {
        ...devices["Desktop Chrome"],
        browserName: "chromium",
        baseURL: E2E_PRODUCTION_BASE_URL,
        launchOptions: {
          chromiumSandbox: false,
          args: [
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--single-process",
            "--no-zygote",
            "--disable-gpu",
            "--disable-dev-shm-usage",
          ],
        },
      },
    },
  ],
});
