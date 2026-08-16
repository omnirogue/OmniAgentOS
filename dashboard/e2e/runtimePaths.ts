import path from "node:path";
import os from "node:os";

/**
 * B06 assembly: browser evidence must never depend on a developer's worktree.
 * Callers may provide an isolated output root; the fallback is per-process
 * temporary storage outside the checkout.
 */
export const B06_E2E_OUT = path.resolve(
  process.env.OMNIAGENTOS_E2E_OUT ??
    path.join(os.tmpdir(), "omniagentos-b06-e2e", String(process.pid)),
);
