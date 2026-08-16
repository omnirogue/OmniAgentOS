import path from "node:path";
import type { NextConfig } from "next";

/**
 * H-32 — pin the Next workspace root and isolate build output from live/dev.
 *
 * Without an explicit root, Next walks ancestors for a lockfile and can select
 * `$HOME` (or another monorepo root). That exhausts file watchers (EMFILE),
 * restarts the dev server on phantom changes, and 404s the live dashboard.
 * Sharing `.next` between `next dev` and `next build` also corrupts the live
 * server's build state during validation.
 */

/** Absolute path of this dashboard package — never an ancestor directory. */
export function resolveDashboardTracingRoot(
  configDir: string = __dirname,
): string {
  return path.resolve(configDir);
}

/**
 * Resolve the Next `distDir`.
 *
 * - `OMNIAGENTOS_NEXT_DIST_DIR` wins (lane-isolated validation runtimes).
 * - production (`next build` / `next start`) → `.next-build` so it never
 *   clobbers a concurrent `next dev` writing to `.next`.
 * - development → `.next`, or `.next-dev-<port>` when PORT is set to a
 *   non-default port so concurrent dev servers do not share build output.
 */
export function resolveDashboardDistDir(
  env: NodeJS.ProcessEnv = process.env,
): string {
  const override = env.OMNIAGENTOS_NEXT_DIST_DIR?.trim();
  if (override) return override;
  if (env.NODE_ENV === "production") return ".next-build";
  const port = env.PORT?.trim();
  if (port && port !== "3000") return `.next-dev-${port}`;
  return ".next";
}

const dashboardRoot = resolveDashboardTracingRoot();

const nextConfig: NextConfig = {
  reactStrictMode: true,
  outputFileTracingRoot: dashboardRoot,
  distDir: resolveDashboardDistDir(),
};

export default nextConfig;
