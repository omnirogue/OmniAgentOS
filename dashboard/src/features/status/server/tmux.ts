import { execFile } from "node:child_process";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);

const TMUX_BIN = "/opt/homebrew/bin/tmux";
const TMUX_TIMEOUT_MS = 3_000;

/**
 * `tmux has-session -t <name>` exits 0 when the session exists and exits
 * non-zero (no useful stderr) when it doesn't — that clean non-zero exit
 * is a normal "not alive" answer, not a fetch failure, so it resolves
 * `false` rather than throwing. Anything else (tmux binary missing, the
 * 3s timeout firing, a permission error) rethrows for the caller's
 * try/catch to turn into an explicit section error — never silently
 * reported as "not alive".
 *
 * Server-only (uses `node:child_process`).
 */
export async function tmuxHasSession(sessionName: string): Promise<boolean> {
  try {
    await execFileAsync(TMUX_BIN, ["has-session", "-t", sessionName], {
      timeout: TMUX_TIMEOUT_MS,
    });
    return true;
  } catch (reason) {
    const code = (reason as NodeJS.ErrnoException & { code?: unknown })?.code;
    if (typeof code === "number") return false;
    throw reason;
  }
}
