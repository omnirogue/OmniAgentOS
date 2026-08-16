import type { NextRequest } from "next/server";
import { proxyAuthorized, proxyPublicRead, proxyRead } from "@/lib/serverProxy";

/**
 * AC-policy fix7: same-origin catch-all proxy for the token-gated FastAPI
 * control plane. Every mutating request (POST/PUT/PATCH/DELETE) the dashboard
 * sends same-origin to `/api/**` is forwarded here to the FastAPI server with
 * the local session token attached server-side — so the browser never holds the
 * token, and an agent that cannot read the 0600 token file cannot mutate the
 * control plane by loopback-POSTing it.
 *
 * More-specific route handlers (e.g. `api/pause/route.ts`,
 * `api/tasks/route.ts`) take precedence over this catch-all for their exact
 * paths; this covers every other gated mutation (projects, provision, routines,
 * suggestions, intake, collab, knowledge, skills, lab, …). Project file reads
 * are also routed here because FastAPI protects them with the session token;
 * all other reads remain public and are relayed without that token.
 *
 * Token-gated reads and mutations also forward the principal resolved from the
 * signed browser credential; FastAPI applies route-class authorization.
 *
 * Conditional reads (GET /api/board's ETag) pass through unchanged in both
 * directions: `lib/serverProxy` forwards the browser's `If-None-Match` upstream
 * and relays the upstream `ETag`, and a 304 is relayed body-less. The proxy is
 * therefore transparent to caching — it neither invents nor swallows a 304.
 */
function upstreamPath(request: NextRequest, path: string[]): string {
  const encoded = path.map(encodeURIComponent).join("/");
  return `/api/${encoded}${request.nextUrl.search}`;
}

type Ctx = { params: Promise<{ path: string[] }> };

const AUTHORIZED_READ_PREFIXES = new Set([
  "reliability",
  "improvements",
  "org",
  "judges",
  "autonomy",
  "scorecards",
  // WP6a: GET /api/swarm, /api/swarm/{id}, /api/swarm/{id}/activity — every
  // swarm route is session-token-gated (see omniagentos/api/routes/swarm.py).
  // POST /api/swarm/{id}/cancel is already reachable through this catch-all's
  // proxyAuthorized POST handler below; a DEDICATED mutation route for it
  // (the `app/api/sessions/[id]/kill/route.ts` pattern) is noted as future
  // work in contracts/swarm-api.md, not built in this slice.
  "swarm",
  // System transparency reads (map/agents/skills/improvers/agent-activity) are
  // token-gated server-side (each GET carries `_authorized`) because they expose
  // agent/curator system prompts and config — see omniagentos/api/routes/system.py.
  // The PATCH /api/system/agents/{name} mutation has its own dedicated route at
  // app/api/system/agents/[name]/route.ts (kill-route pattern).
  "system",
  // Machine-wide file search (GET /api/filesearch, /api/filesearch/semantic,
  // /api/filesearch/stats) is session-token-gated because it reveals file
  // paths + content excerpts — see `_require_token` in
  // omniagentos/api/routes/filesearch.py. POST /api/filesearch/reveal is a
  // mutation carried by the proxyAuthorized POST handler below (and has a
  // dedicated route at app/api/filesearch/reveal/route.ts).
  "filesearch",
  // GET /api/semsearch (unified semantic search over skills/tools/capabilities)
  // is session-token-gated server-side (Depends(_authorized) in
  // omniagentos/api/routes/semsearch.py) because it exposes skill/tool/
  // capability content, so the browser read must attach the token via
  // proxyRead rather than principal-less proxyPublicRead.
  "semsearch",
  // GET /api/accounts, /api/accounts/usage expose account emails, config_dir
  // paths and auth status, so they became session-token-gated server-side (see
  // the gated-GET predicates in omniagentos/api/main.py). Browser reads must go
  // through proxyRead, which attaches the token, or the Accounts page 401s.
  "accounts",
  // GET /api/provision/{project_id} exposes the project's filesystem grants
  // and connector capabilities, so the entire read namespace is gated beside
  // accounts on FastAPI's cross-principal deny list.
  "provision",
  // GET /api/workfs/tree lists the operator's Work-folder tree (absolute paths
  // on the machine), so it is session-token-gated server-side. Without the
  // token the chat ctx-row's work-folder select just 401'd.
  "workfs",
  // Employee transcript metadata exposes private corpus filenames, hashes,
  // and storage paths; FastAPI gates the collection and every child route.
  "employee-transcripts",
  // GET /api/engine/* (capabilities, runs, run snapshot) is session-token-gated
  // by the router-level `_authorized` dependency in
  // omniagentos/api/routes/engine.py, so every engine read 401s unless it goes
  // through proxyRead. The whole namespace is read-only — it has no mutations.
  "engine",
  // GET /api/team/* (board/tree/evidence/events/unattributed/scoreboard) is a
  // wholly gated read namespace server-side — evidence and verification
  // history are internal work records (`_GATED_READ_NAMESPACES` in
  // omniagentos/api/main.py groups it with accounts/provision). Added by the
  // P5 dashboard-UI package: no prior package wired this proxy-side, so
  // every `/team` read 401'd without it — see the note in
  // features/team/client.ts.
  "team",
  // GET /api/decisions[...] is the Executive Decision Center's owner-scoped
  // read surface — every row is private to the request principal, so FastAPI
  // gates the whole namespace (owner resolved from the signed session, never a
  // request param). This entry is the FE half of that privacy seam: without it
  // the browser GET goes principal-less through proxyPublicRead and the BE gate
  // 403s it — FAIL-CLOSED either way, which is correct while the omniagentos/edc
  // routes land in a parallel lane. POSTs (/decide) already ride proxyAuthorized.
  "decisions",
  // GET /api/ops/boot-receipt is the API process's boot composition receipt
  // (dsh-audit C-21): which lifespan subsystems came up, and the exception type
  // + bounded message for the ones that were swallowed. Exception text can name
  // filesystem paths, so it carries its own `Depends(_authorized)` in
  // omniagentos/api/main.py — this entry is the proxy half of that gate.
  "ops",
  // GET /api/testobs/* (overview/series/weakspots) reports failing check ids,
  // failure messages and test node ids off var/ test evidence — internal work
  // records, so every handler carries `_authorized`
  // (omniagentos/api/routes/testobs.py). Without this entry the /testing page's
  // browser reads go through proxyPublicRead with no token and 401.
  "testobs",
  // GET /api/compute/estate reports compute-pool machine identities/labels
  // and GH Actions runner names off live estate infrastructure — internal
  // work records, so the handler carries `_authorized`
  // (omniagentos/api/routes/compute.py). Without this entry the browser read
  // goes through proxyPublicRead with no token and 401.
  "compute",
]);

function isAuthorizedReadPath(path: string[]): boolean {
  if (path.length === 0) return false;
  if (AUTHORIZED_READ_PREFIXES.has(path[0]!)) return true;
  return (
    (path.length >= 3 && path[0] === "projects" && path[2] === "files") ||
    (path.length >= 3 && path[0] === "board" && path[2] === "files") ||
    path[0] === "sessions" ||
    // P2 mounts browse API (list/dir/file) is token-gated exactly like
    // project-file reads — see `_is_mounts_request` in omniagentos/api/main.py.
    path[0] === "mounts" ||
    // GET /api/access/servers exposes the live server fleet (hosts/users/
    // ports/status/SSH-key paths) and is token-gated exactly like the mounts
    // browse API — see `_is_server_inventory_request` in
    // omniagentos/api/main.py.
    //
    // GET /api/access/tool-search is gated too, but in its OWN handler rather
    // than in main.py (`verify_token` at the top of `tool_search` in
    // omniagentos/api/routes/access.py — "GATED, unlike its neighbours": a
    // ranked search over tool descriptions and parameter names discloses
    // catalogue shape). That is why it was missing here: this list was
    // transcribed from main.py's predicates, which cannot see a gate that
    // lives inside a route body. tests/api/test_dashboard_read_gate_mirror.py
    // now DISCOVERS the gated set by driving the app instead of reading
    // main.py, so the next one lands as a red test rather than a 401 page.
    //
    // Still only these two exact paths: /api/access/capabilities, /agents,
    // /log and /calls are deliberately public catalogue and roster reads and
    // must keep reaching proxyPublicRead unchanged.
    (path.length === 2 &&
      path[0] === "access" &&
      (path[1] === "servers" || path[1] === "tool-search"))
    || (path.length === 3 && path[0] === "board" &&
      (path[2] === "conversation" || path[2] === "longhaul" || path[2] === "sessions"))
    || (path.length === 1 && path[0] === "categories")
    // GET /api/ledger/claims + GET /api/ledger/tail (session-ledger
    // integration brief, 2026-08-04) relay cross-project claim/session
    // history and are session-token-gated server-side (see
    // `_is_session_ledger_read` in omniagentos/api/main.py). A LITERAL
    // two-path match, not the whole "ledger" prefix: the pre-existing
    // GET /api/ledger (cognitive-flow manifest read) stays public and must
    // keep going through proxyPublicRead unchanged.
    || (path.length === 2 && path[0] === "ledger" && (path[1] === "claims" || path[1] === "tail"))
  );
}

/** Human decision routes that require X-Autonomy-Token in addition to session. */
const AUTONOMY_ACTIONS = new Set(["approve", "reject", "apply", "rollback", "pull"]);

function needsAutonomyToken(path: string[], method: string): boolean {
  if (method === "PUT" && path[0] === "autonomy") return true;
  if (
    method === "POST" &&
    path[0] === "improvements" &&
    path.length === 3 &&
    AUTONOMY_ACTIONS.has(path[2]!)
  ) {
    return true;
  }
  return false;
}

async function read(request: NextRequest, ctx: Ctx) {
  const { path } = await ctx.params;
  const apiPath = upstreamPath(request, path);
  return isAuthorizedReadPath(path)
    ? proxyRead(apiPath, request)
    : proxyPublicRead(apiPath, request);
}

export async function GET(request: NextRequest, ctx: Ctx) {
  return read(request, ctx);
}

export async function HEAD(request: NextRequest, ctx: Ctx) {
  return read(request, ctx);
}

export async function POST(request: NextRequest, ctx: Ctx) {
  const { path } = await ctx.params;
  return proxyAuthorized(upstreamPath(request, path), request, "POST", {
    autonomy: needsAutonomyToken(path, "POST"),
  });
}

export async function PUT(request: NextRequest, ctx: Ctx) {
  const { path } = await ctx.params;
  return proxyAuthorized(upstreamPath(request, path), request, "PUT", {
    autonomy: needsAutonomyToken(path, "PUT"),
  });
}

export async function PATCH(request: NextRequest, ctx: Ctx) {
  const { path } = await ctx.params;
  return proxyAuthorized(upstreamPath(request, path), request, "PATCH");
}

export async function DELETE(request: NextRequest, ctx: Ctx) {
  const { path } = await ctx.params;
  return proxyAuthorized(upstreamPath(request, path), request, "DELETE");
}
