import { defineConfig } from "@playwright/test";
import os from "node:os";
import path from "node:path";

export default defineConfig({
  testDir: "./",
  testMatch: /rail\.spec\.ts$/,
  workers: 1,
  retries: 0,
  outputDir: path.join(os.tmpdir(), `rail-check-${process.pid}`),
  projects: [
    {
      name: "rail-check",
      testMatch: /rail\.spec\.ts$/,
    },
  ],
});
