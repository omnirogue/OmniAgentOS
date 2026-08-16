import path from "node:path";
import { describe, expect, it } from "vitest";
import nextConfig, {
  resolveDashboardDistDir,
  resolveDashboardTracingRoot,
} from "./next.config";

describe("H-32 next.config isolation", () => {
  it("pins outputFileTracingRoot to the dashboard package directory", () => {
    const root = resolveDashboardTracingRoot();
    expect(path.basename(root)).toBe("dashboard");
    expect(root).toBe(path.resolve(__dirname));
    expect(nextConfig.outputFileTracingRoot).toBe(root);
    // Must never resolve to the user home or an ancestor monorepo root alone.
    expect(root).not.toBe(path.resolve(root, ".."));
    expect(root).not.toBe(path.resolve(root, "../.."));
  });

  it("uses .next for development and .next-build for production", () => {
    expect(resolveDashboardDistDir({ NODE_ENV: "development" })).toBe(".next");
    expect(resolveDashboardDistDir({ NODE_ENV: "production" })).toBe(
      ".next-build",
    );
    expect(resolveDashboardDistDir({} as NodeJS.ProcessEnv)).toBe(".next");
  });

  it("lets OMNIAGENTOS_NEXT_DIST_DIR isolate validation output", () => {
    expect(
      resolveDashboardDistDir({
        NODE_ENV: "production",
        OMNIAGENTOS_NEXT_DIST_DIR: ".next-l19-validate",
      }),
    ).toBe(".next-l19-validate");
    expect(
      resolveDashboardDistDir({
        NODE_ENV: "development",
        OMNIAGENTOS_NEXT_DIST_DIR: "  /tmp/lane-next  ",
      }),
    ).toBe("/tmp/lane-next");
  });

  it("exports a distDir that is not the live-dev default when building", () => {
    // When this suite runs under vitest, NODE_ENV is typically "test".
    // Assert the production build path is isolated from ".next".
    const buildDir = resolveDashboardDistDir({ NODE_ENV: "production" });
    expect(buildDir).not.toBe(".next");
    expect(buildDir).toBe(".next-build");
  });

  it("exported nextConfig has production-safe distDir and root settings", () => {
    // Verify the exported config object has the isolation settings applied
    expect(nextConfig.reactStrictMode).toBe(true);
    expect(nextConfig.outputFileTracingRoot).toBeDefined();
    expect(nextConfig.distDir).toBeDefined();
    // distDir should not be empty
    expect(nextConfig.distDir).not.toBe("");
    // outputFileTracingRoot should be an absolute path containing 'dashboard'
    expect(nextConfig.outputFileTracingRoot).toContain("dashboard");
  });

  it("tracing root is absolute and does not escape to ancestor", () => {
    const root = resolveDashboardTracingRoot();
    // Must be absolute path (starts with /)
    expect(path.isAbsolute(root)).toBe(true);
    // Must end with 'dashboard' specifically
    expect(path.basename(root)).toBe("dashboard");
    // Must not be the parent or grandparent
    const parent = path.dirname(root);
    const grandparent = path.dirname(parent);
    expect(root).not.toBe(parent);
    expect(root).not.toBe(grandparent);
  });
});
