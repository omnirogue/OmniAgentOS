import { NextResponse } from "next/server";
import { getStatus } from "@/features/status/server/assembleStatus";

/**
 * Server route for the Status homepage (`/`) — assembles a JSON status
 * object straight from GROUND TRUTH on this box (tmux, loop/gate/recycler
 * logs, `queue.json` top-level keys, `git log origin/main`, ALERTS.md).
 * READ-ONLY: the only child processes it runs are `tmux has-session` and
 * `git log`, both with timeouts; every source is independently try/caught
 * in `assembleStatus`, so a dead source renders as an explicit
 * `{ ok: false, error }` for its section, never a fake-healthy default.
 *
 * `runtime = "nodejs"` because this needs `node:fs`/`node:child_process`
 * (not available on the edge runtime). `dynamic = "force-dynamic"` because
 * the whole point is a fresh read every poll — Next must never cache this
 * route itself (the module-level 15s cache inside `getStatus` is the only
 * caching layer, and it's intentionally short).
 */
export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET() {
  const data = await getStatus();
  return NextResponse.json(data);
}
