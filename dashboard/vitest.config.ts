import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import path from "node:path";
import os from "node:os";

/**
 * Minimal Vitest setup — dashboard had zero test infra before this. Scoped
 * to unblock coverage for dashboard/src/features/voice (W3-voice-F1); other
 * features can adopt the same config as they add tests.
 */
export default defineConfig({
  plugins: [react()],
  cacheDir:
    process.env.OMNIAGENTOS_VITE_CACHE_DIR ??
    path.join(os.tmpdir(), "omniagentos-b06-vite", String(process.pid)),
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./vitest.setup.ts"],
    globals: true,
    include: ["src/**/*.test.{ts,tsx}", "next.config.test.ts"],
  },
});
