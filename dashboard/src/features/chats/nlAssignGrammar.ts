/**
 * Composer intercept (M6, spec §2 gap) — recognizes sentences that should be
 * routed to `POST /api/team/nl-assign` instead of the chat/LLM turn.
 *
 * SUPERSET RULE (Sol@high cross-review round 2, 2026-08-14): the two
 * directions of client/server disagreement are NOT symmetric. Client-rejects-
 * but-server-accepts is the dangerous one — the sentence silently becomes an
 * LLM turn instead of an assignment, and the operator has no idea it slipped
 * through. Client-accepts-but-server-rejects is benign — the composer shows
 * the server's own helpful 400 inline (draft preserved, nothing sent to the
 * LLM), which is exactly the same experience as a genuine typo in a name. So
 * this recognizer is a deliberate, documented SUPERSET of the server's
 * `_NL_GRAMMAR` (`omniagentos/api/routes/team.py`), not an exact mirror: the
 * name token uses `[^\s@]+` (any run of non-whitespace, non-`@` characters)
 * rather than trying to reproduce the server's `[A-Za-z][\w.-]*` — Python's
 * `\w` is Unicode-aware by default (`re` module, no flag needed), so a name
 * like "josé" is a valid server-side name; JS's bare `\w` is ASCII-only and
 * has no cheap equivalent without `\p{...}` + the `u` flag support matrix
 * this codebase does not otherwise depend on. Widening to "any token" trades
 * a small amount of over-acceptance (this recognizer will also route a
 * shape-matching sentence whose "name" the roster does not recognize — the
 * SAME shape-accepted/roster-rejected split `NL_ASSIGN_UNKNOWN_NAME` already
 * exercises server-side) for the guarantee that it can never UNDER-accept
 * relative to the server.
 *
 * Five shapes total (case-insensitive; a trailing deadline, `#company`/
 * `#category`, `for <who>`, or `| ac: <criteria>` suffix is just more of
 * <title> as far as recognition goes — the server owns splitting it out):
 * three ASSIGN shapes, then two PROPOSE shapes (automation backlog,
 * 2026-08-14 — mirrors `_NL_PROPOSE_GRAMMAR`, checked server-side BEFORE the
 * assign grammar so "propose an automation to X" can never be misread as an
 * assignment; order does not matter here since this function is a plain OR).
 *   (a) "/task assign @name <title>" (@ optional)
 *   (b) "give <name> a task to <title>" / "give <name> a task: <title>"
 *       ("a" is required and literal — "an task" / a missing article are
 *       near-misses the server rejects; recognizing them anyway is benign
 *       per the superset rule above, but the regex does not go out of its
 *       way to widen the "a"/"task" keywords themselves)
 *   (c) "assign <name> <title>" — this also matches "assign to <name>
 *       <title>" by treating "to" as the (roster-invalid) name, exactly like
 *       the server's own regex does; both sides then 400 on "no active
 *       teammate called 'to'".
 *   (d) "propose an automation to <title>" / "propose a automation to
 *       <title>" / "propose an automation: <title>" — a PROPOSAL, not an
 *       assignment: it names no owner and lands in `awaiting_approval` for
 *       the operator. `ChatSurface` renders a different confirmation for it (see the
 *       `kind: "automation_proposal"` discriminant on `TeamNlAssignResult`).
 *   (e) "propose automation: <title>" — the no-article colon form.
 *
 * Deliberately NOT recognized: a "please " prefix, or "propose" without the
 * literal word "automation" (e.g. "propose a meeting to..."). The server's
 * anchors require the verb/noun at position 0 with no leniency for either,
 * and both are plain chatter that happens to start with a related word —
 * treating them as superset candidates anyway would intercept far too
 * eagerly (see `nlAssignGrammar.test.ts`'s near-miss fixtures, copied
 * verbatim from `tests/team/test_nl_assign.py`'s `NL_ASSIGN_REJECTED`).
 */

const NAME = "[^\\s@]+";

const NL_ASSIGN_SLASH_TASK_RE = new RegExp(`^/task\\s+assign\\s+@?${NAME}\\s+\\S`, "i");
const NL_ASSIGN_GIVE_RE = new RegExp(`^give\\s+@?${NAME}\\s+a\\s+task\\s*(?:to\\s+|:\\s*)\\S`, "i");
const NL_ASSIGN_ASSIGN_RE = new RegExp(`^assign\\s+@?${NAME}\\s+\\S`, "i");

// PROPOSE — mirrors `_NL_PROPOSE_GRAMMAR` (team.py) exactly; no name token
// to widen for Unicode here, so no superset gap to document beyond the
// "please "/wrong-noun exclusions already covered above.
const NL_PROPOSE_ARTICLE_RE = /^propose\s+an?\s+automation\s*(?:to\s+|:\s*)\S/i;
const NL_PROPOSE_COLON_RE = /^propose\s+automation\s*:\s*\S/i;

export function looksLikeNlAssign(text: string): boolean {
  const trimmed = text.trim();
  return (
    NL_ASSIGN_SLASH_TASK_RE.test(trimmed) ||
    NL_ASSIGN_GIVE_RE.test(trimmed) ||
    NL_ASSIGN_ASSIGN_RE.test(trimmed) ||
    NL_PROPOSE_ARTICLE_RE.test(trimmed) ||
    NL_PROPOSE_COLON_RE.test(trimmed)
  );
}
