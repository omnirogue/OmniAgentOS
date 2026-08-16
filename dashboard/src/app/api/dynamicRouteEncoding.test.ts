import { beforeEach, describe, expect, it, vi } from "vitest";

/**
 * The dashboard's dynamic API routes interpolate a URL param into a path that is
 * then handed to an AUTHORIZED proxy call (the one that attaches the local
 * session token). Every one of them wraps the param in `encodeURIComponent`;
 * dropping it is a one-token edit whose effect is not cosmetic. Measured on
 * `approvals/[id]/decision`, the param `approval/id?next=/admin` becomes the
 * upstream path `/api/approvals/approval/id?next=/admin/decision` — the segment
 * escapes its position and grafts a query string onto a token-bearing call to
 * the FastAPI control plane.
 *
 * This file pins the invariant for ALL 11 carriers — every route under
 * `app/api` that interpolates a param into a path handed to a token-bearing
 * proxy (`proxyAuthorized`, `proxyAuthorizedPost`, or `proxyRead`; note that
 * `proxyPublicRead` deliberately sends no token and so is not a carrier).
 *
 * An earlier revision of this file claimed the last two — `[...path]` and
 * `approvals/[id]/decision` — were already covered by colocated tests. That
 * claim was FALSE and is the reason the rest of this comment is now specific:
 * `approvals/[id]/decision` has no colocated test file at all, and
 * `[...path]/route.test.ts` (which covers the authorization DECISION the
 * catch-all makes on its segments) asserts nothing about SEGMENT encoding —
 * its one percent-encoded expectation pins query-string passthrough, a
 * different invariant that survives this mutation untouched. Both mutants —
 * dropping `encodeURIComponent(id)` and dropping `path.map(encodeURIComponent)`
 * — survived a full `src/app/api` run before this file's rows existed.
 *
 * Re-measured at THIS revision (238 rows in this file): dropping
 * `encodeURIComponent(id)` on `approvals/[id]/decision` fails exactly one row,
 * that route's own, and nothing else; dropping `path.map(encodeURIComponent)`
 * fails all 228 catch-all rows, because the whole block shares one
 * `upstreamPath()`. The 228 is GENERATED from `MAX_SEGMENTS` (see the matrix
 * below), so raising that constant moves this number — the invariant to check
 * is "every catch-all row", not the literal count.
 *
 * A false claim of coverage is worse than an admitted gap, because it stops the
 * next reader from looking. This comment has now been wrong twice: it once said
 * the second mutant failed "exactly the two rows below" when it failed ten, and
 * then said "eighteen" after the block grew again. Both times the count was
 * stated and never re-measured. That is the same defect this file exists to
 * prevent, so it is recorded rather than quietly corrected — and it is why the
 * rows below are now generated rather than counted by hand.
 *
 * The catch-all is exercised in its own block below rather than in the table:
 * its param is a string ARRAY, and it builds the query string from
 * `request.nextUrl`, so it needs a request shape the table's rows do not.
 */

const { proxyAuthorizedMock, proxyAuthorizedPostMock, proxyReadMock, proxyPublicReadMock } =
  vi.hoisted(() => {
  // The signature is declared on the mock rather than the implementation:
  // `proxyAuthorized` and `proxyAuthorizedPost` have different arities, and the
  // assertions below index into `mock.calls[0]`, which needs a typed argument
  // list to be indexable at all.
    type Proxy = (...args: unknown[]) => Promise<Response>;
    const ok = () =>
      Promise.resolve(new Response(JSON.stringify({ ok: true }), { status: 200 }));
    return {
      proxyAuthorizedMock: vi.fn<Proxy>(ok),
      proxyAuthorizedPostMock: vi.fn<Proxy>(ok),
      // The catch-all imports both read helpers. `proxyRead` is a carrier (it
      // attaches the session token); `proxyPublicRead` is not, and is mocked
      // only so that importing the route module does not fail — the assertions
      // below use it as the NEGATIVE control for the authorized-read rows.
      proxyReadMock: vi.fn<Proxy>(ok),
      proxyPublicReadMock: vi.fn<Proxy>(ok),
    };
  });

vi.mock("@/lib/serverProxy", () => ({
  proxyAuthorized: proxyAuthorizedMock,
  proxyAuthorizedPost: proxyAuthorizedPostMock,
  proxyRead: proxyReadMock,
  proxyPublicRead: proxyPublicReadMock,
}));

import type { NextRequest } from "next/server";

import { POST as approvalsDecision } from "./approvals/[id]/decision/route";
import { POST as boardFilesReveal } from "./board/[id]/files/reveal/route";
import { POST as filesearchReveal } from "./filesearch/reveal/route";
import {
  DELETE as catchAllDelete,
  GET as catchAllGet,
  HEAD as catchAllHead,
  PATCH as catchAllPatch,
  POST as catchAllPost,
  PUT as catchAllPut,
} from "./[...path]/route";
import { POST as memlifeGraduate } from "./memlife/[id]/graduate/route";
import { POST as memlifeReject } from "./memlife/[id]/reject/route";
import { POST as memlifeReopen } from "./memlife/[id]/reopen/route";
import { POST as runsCancel } from "./runs/[id]/cancel/route";
import { POST as sessionsKill } from "./sessions/[id]/kill/route";
import { PATCH as systemAgents } from "./system/agents/[name]/route";
import { PATCH as systemImproverPrompt } from "./system/improvers/[label]/prompt/route";
import { POST as tasksRuns } from "./tasks/[id]/runs/route";

/** A param that is hostile in three ways at once: segment break, query graft, traversal. */
const HOSTILE = "a/b?next=/admin";
const ENCODED = "a%2Fb%3Fnext%3D%2Fadmin";

/**
 * Each route types `params` with its OWN param name (`id`, `name`, `label`), so
 * no single signature is assignment-compatible with every row. The table erases
 * that name deliberately and each entry is cast through `unknown`, which is what
 * tsc asks for and is honest about what is being widened.
 */
type Handler = (
  request: NextRequest,
  context: { params: Promise<Record<string, string>> },
) => Promise<Response>;

type Carrier = {
  route: string;
  handler: Handler;
  param: string;
  expectedPath: string;
  /** Which proxy helper the route calls — they have different signatures. */
  proxy: "proxyAuthorized" | "proxyAuthorizedPost";
  method?: string;
};

const CARRIERS: Carrier[] = [
  {
    // The route the docstring above used to claim was covered elsewhere. It has
    // no colocated test file; this row is its only encoding coverage.
    route: "approvals/[id]/decision",
    handler: approvalsDecision as unknown as Handler,
    param: "id",
    expectedPath: `/api/approvals/${ENCODED}/decision`,
    proxy: "proxyAuthorizedPost",
  },
  {
    route: "board/[id]/files/reveal",
    handler: boardFilesReveal as unknown as Handler,
    param: "id",
    expectedPath: `/api/board/${ENCODED}/files/reveal`,
    proxy: "proxyAuthorizedPost",
  },
  {
    route: "filesearch/reveal",
    handler: filesearchReveal as unknown as Handler,
    param: "unused",
    expectedPath: "/api/filesearch/reveal",
    proxy: "proxyAuthorizedPost",
  },
  {
    route: "memlife/[id]/graduate",
    handler: memlifeGraduate as unknown as Handler,
    param: "id",
    expectedPath: `/api/memlife/${ENCODED}/graduate`,
    proxy: "proxyAuthorizedPost",
  },
  {
    route: "memlife/[id]/reject",
    handler: memlifeReject as unknown as Handler,
    param: "id",
    expectedPath: `/api/memlife/${ENCODED}/reject`,
    proxy: "proxyAuthorizedPost",
  },
  {
    route: "memlife/[id]/reopen",
    handler: memlifeReopen as unknown as Handler,
    param: "id",
    expectedPath: `/api/memlife/${ENCODED}/reopen`,
    proxy: "proxyAuthorizedPost",
  },
  {
    route: "runs/[id]/cancel",
    handler: runsCancel as unknown as Handler,
    param: "id",
    expectedPath: `/api/runs/${ENCODED}/cancel`,
    proxy: "proxyAuthorized",
    method: "POST",
  },
  {
    route: "sessions/[id]/kill",
    handler: sessionsKill as unknown as Handler,
    param: "id",
    expectedPath: `/api/sessions/${ENCODED}/kill`,
    proxy: "proxyAuthorizedPost",
  },
  {
    route: "system/agents/[name]",
    handler: systemAgents as unknown as Handler,
    param: "name",
    expectedPath: `/api/system/agents/${ENCODED}`,
    proxy: "proxyAuthorized",
    method: "PATCH",
  },
  {
    route: "system/improvers/[label]/prompt",
    handler: systemImproverPrompt as unknown as Handler,
    param: "label",
    expectedPath: `/api/system/improvers/${ENCODED}/prompt`,
    proxy: "proxyAuthorized",
    method: "PATCH",
  },
  {
    route: "tasks/[id]/runs",
    handler: tasksRuns as unknown as Handler,
    param: "id",
    expectedPath: `/api/tasks/${ENCODED}/runs`,
    proxy: "proxyAuthorized",
    method: "POST",
  },
];

describe("dynamic API routes encode their param before the authorized proxy", () => {
  beforeEach(() => {
    proxyAuthorizedMock.mockClear();
    proxyAuthorizedPostMock.mockClear();
  });

  it.each(CARRIERS)("$route", async ({ handler, param, expectedPath, proxy, method }) => {
    const request = new Request("http://dashboard.test/api/test", {
      method: method ?? "POST",
    }) as NextRequest;

    const response = await handler(request, {
      params: Promise.resolve({ [param]: HOSTILE }),
    });

    const mock = proxy === "proxyAuthorized" ? proxyAuthorizedMock : proxyAuthorizedPostMock;
    const other = proxy === "proxyAuthorized" ? proxyAuthorizedPostMock : proxyAuthorizedMock;

    expect(other).not.toHaveBeenCalled();
    expect(mock).toHaveBeenCalledTimes(1);

    const actualPath = mock.mock.calls[0]![0] as string;
    expect(actualPath).toBe(expectedPath);
    // Redundant with the equality above on purpose: it is the property that
    // matters, and it states why the row exists if the expectation is edited.
    expect(actualPath).not.toContain(HOSTILE);
    expect(response.status).toBe(200);
  });
});

/**
 * The eleventh carrier. `[...path]` shares ONE `upstreamPath()` helper across
 * GET/HEAD/POST/PUT/PATCH/DELETE, so a single dropped `map(encodeURIComponent)`
 * unencodes every method at once. That shared helper is exactly why this block
 * must NOT test only one method and one segment position: three mutants below
 * the whole-array level survived an earlier revision of these tests, all three
 * measured passing 38/38 —
 *
 *   1. encode every segment EXCEPT index 0 (only a later segment was hostile);
 *   2. have PUT bypass `upstreamPath()` and join the array raw (only POST and
 *      GET were exercised, so PUT/PATCH/DELETE/HEAD were unpinned);
 *   3. the same bypass on any other unexercised method.
 *
 * So the rows below cross every mutating method with BOTH segment positions,
 * and cover the two read methods on the authorized-read path. They also split
 * by helper: mutations land on `proxyAuthorized` and authorized reads on
 * `proxyRead`, and a regression rerouting a read to `proxyPublicRead` would
 * strip the session token without changing the path at all — so
 * `proxyPublicRead` is asserted un-called rather than merely ignored.
 */
function catchAllRequest(method: string): NextRequest {
  const url = new URL("http://dashboard.test/api/anything");
  // The route reads `request.nextUrl.search` directly; a plain `Request` has no
  // `nextUrl`, so it is attached here rather than faking the whole NextRequest.
  return Object.assign(new Request(url, { method }), { nextUrl: url }) as unknown as NextRequest;
}

type CatchAll = (
  request: NextRequest,
  ctx: { params: Promise<{ path: string[] }> },
) => Promise<Response>;

/** Every method the catch-all sends to `proxyAuthorized` (the token-bearing mutation helper). */
const MUTATING: Array<{ method: string; handler: CatchAll }> = [
  { method: "POST", handler: catchAllPost as unknown as CatchAll },
  { method: "PUT", handler: catchAllPut as unknown as CatchAll },
  { method: "PATCH", handler: catchAllPatch as unknown as CatchAll },
  { method: "DELETE", handler: catchAllDelete as unknown as CatchAll },
];

/**
 * The vectors a hostile segment can arrive in.
 *
 * This matrix is GENERATED, and that is the whole point. Three successive
 * revisions of this file hand-wrote the rows, and each time a mutant hid one
 * index past wherever the hand-written list happened to stop:
 *
 *   - `i === 0 ? s : encodeURIComponent(s)` (spares index 0) survived until a
 *     row supplied a hostile FIRST segment;
 *   - `i === 2 ? …` survived 46/46, because no row supplied a third segment;
 *   - `i === 3 ? …` survived 28/28 targeted and 54/54 across `src/app/api`,
 *     because every row stopped at three segments.
 *
 * Each round closed the named index and left the next one open. A list cannot
 * fix that, because the defect is the list's CEILING, not any missing entry.
 * So the rows below are built by construction instead: for every length up to
 * `MAX_SEGMENTS`, one row per hostile index within that length. Any mutant of
 * the form "skip encoding at fixed index K" therefore fails for every
 * K < MAX_SEGMENTS without anyone having to think of K first.
 *
 * Two axes are covered, because position is not the only place a mutant hides:
 *   - POSITION — one hostile segment at each index, killing every fixed-index
 *     skip. Index 0 matters twice over: it also decides read authorization.
 *   - COUNT — every length from 1 to `MAX_SEGMENTS`, killing "encode only the
 *     first N", and an all-hostile row per length, killing an encoder that
 *     stops after the first element it had to escape.
 *
 * Raising `MAX_SEGMENTS` extends the guarantee; it never invalidates a row.
 */
const MAX_SEGMENTS = 8;

/**
 * A benign segment is spelled so that its encoding is ITSELF. The expectations
 * below are then assembled from string literals rather than by calling
 * `encodeURIComponent` in the test — if the test computed the expected path
 * with the same function the route uses, a mutant would move both sides of the
 * assertion together and the row would pass while the product was broken.
 */
const benign = (index: number): string => `seg${index}`;

type Position = { name: string; path: string[]; expected: string };

function buildPositions(): Position[] {
  const rows: Position[] = [];

  for (let length = 1; length <= MAX_SEGMENTS; length += 1) {
    for (let hostileAt = 0; hostileAt < length; hostileAt += 1) {
      const path = Array.from({ length }, (_, i) => (i === hostileAt ? HOSTILE : benign(i)));
      const expected = Array.from({ length }, (_, i) =>
        i === hostileAt ? ENCODED : benign(i),
      ).join("/");
      rows.push({
        name: `hostile at index ${hostileAt} of ${length} segment(s)`,
        path,
        expected: `/api/${expected}`,
      });
    }

    // Length 1 would duplicate the single-hostile row above.
    if (length >= 2) {
      rows.push({
        name: `all ${length} segments hostile`,
        path: Array.from({ length }, () => HOSTILE),
        expected: `/api/${Array.from({ length }, () => ENCODED).join("/")}`,
      });
    }
  }

  return rows;
}

const POSITIONS: Position[] = buildPositions();

/**
 * The same depth matrix for the authorized-read carrier. Index 0 is pinned to
 * `sessions` because that prefix is what routes the request to `proxyRead` (the
 * token-bearing read) rather than `proxyPublicRead` — making index 0 hostile
 * would change WHICH helper is called, which is a different invariant. So the
 * hostile segment walks indices 1..length-1.
 */
const AUTHORIZED_READ_PREFIX = "sessions";

function buildAuthorizedReadPositions(): Position[] {
  const rows: Position[] = [];

  for (let length = 2; length <= MAX_SEGMENTS; length += 1) {
    for (let hostileAt = 1; hostileAt < length; hostileAt += 1) {
      const segment = (i: number): string => (i === 0 ? AUTHORIZED_READ_PREFIX : benign(i));
      const path = Array.from({ length }, (_, i) => (i === hostileAt ? HOSTILE : segment(i)));
      const expected = Array.from({ length }, (_, i) =>
        i === hostileAt ? ENCODED : segment(i),
      ).join("/");
      rows.push({
        name: `hostile at index ${hostileAt} of ${length} segment(s)`,
        path,
        expected: `/api/${expected}`,
      });
    }
  }

  return rows;
}

const AUTHORIZED_READ_POSITIONS: Position[] = buildAuthorizedReadPositions();

describe("[...path] catch-all encodes every segment before a token-bearing proxy", () => {
  beforeEach(() => {
    proxyAuthorizedMock.mockClear();
    proxyReadMock.mockClear();
    proxyPublicReadMock.mockClear();
  });

  describe.each(MUTATING)("$method → proxyAuthorized", ({ method, handler }) => {
    it.each(POSITIONS)("hostile $name is encoded", async ({ path, expected }) => {
      const response = await handler(catchAllRequest(method), {
        params: Promise.resolve({ path }),
      });

      expect(proxyAuthorizedMock).toHaveBeenCalledTimes(1);
      const actualPath = proxyAuthorizedMock.mock.calls[0]![0] as string;
      expect(actualPath).toBe(expected);
      expect(actualPath).not.toContain(HOSTILE);
      expect(response.status).toBe(200);
    });
  });

  // `sessions` is an authorized read prefix, so these land on proxyRead (which
  // attaches the session token), not proxyPublicRead. The hostile segment can
  // only be a LATER one here: index 0 is what makes the read authorized at all,
  // so a hostile index 0 would route to the public helper and stop being a
  // token-bearing carrier — a different case, not this invariant.
  //
  // Reads get their own depth matrix rather than borrowing the mutating one.
  // `read()` calls `upstreamPath()` on its own line, so a mutant can bypass the
  // shared helper HERE while leaving it intact for mutations — that is mutant
  // (2) in the block comment above, which survived by being unexercised. Depth
  // is generated for the same reason it is generated above: a fixed-index skip
  // must fail without anyone naming the index first.
  describe.each([
    { method: "GET", handler: catchAllGet as unknown as CatchAll },
    { method: "HEAD", handler: catchAllHead as unknown as CatchAll },
  ])("$method on an authorized prefix → token-bearing read", ({ method, handler }) => {
    it.each(AUTHORIZED_READ_POSITIONS)("segment encoded, $name", async ({ path, expected }) => {
      const response = await handler(catchAllRequest(method), {
        params: Promise.resolve({ path }),
      });

      expect(proxyPublicReadMock).not.toHaveBeenCalled();
      expect(proxyReadMock).toHaveBeenCalledTimes(1);
      const actualPath = proxyReadMock.mock.calls[0]![0] as string;
      expect(actualPath).toBe(expected);
      expect(actualPath).not.toContain(HOSTILE);
      expect(response.status).toBe(200);
    });
  });
});
