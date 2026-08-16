import { describe, expect, it } from "vitest";
import { looksLikeNlAssign } from "./nlAssignGrammar";

/**
 * Parity fixtures (Sol@high cross-review round 2, 2026-08-14) — the
 * `NL_ASSIGN_ACCEPTED`/`NL_ASSIGN_REJECTED`/`NL_ASSIGN_UNKNOWN_NAME` tuples
 * below are copied VERBATIM from `tests/team/test_nl_assign.py` (read
 * directly; do not hand-edit these three without re-copying from there).
 *
 * The recognizer is a documented SUPERSET of the server grammar (see
 * `nlAssignGrammar.ts`'s docstring for the full reasoning): the only
 * dangerous direction is client-rejects-but-server-accepts (an assignment
 * silently becomes an LLM turn), so:
 *   - every `NL_ASSIGN_ACCEPTED` string MUST be client-accepted;
 *   - every `NL_ASSIGN_UNKNOWN_NAME` string (shape-valid, roster-invalid —
 *     the server 400s on "no active teammate called ...") MUST be
 *     client-accepted too, for the same reason: the client only judges
 *     SHAPE, never the roster;
 *   - `NL_ASSIGN_REJECTED` strings that are plain chatter (no assignment
 *     verb, or the right verb with no title at all) MUST be
 *     client-rejected — accepting an ordinary sentence would swallow chat;
 *   - the THREE "near-miss" `NL_ASSIGN_REJECTED` strings (right shape,
 *     wrong verb/prefix — the server file's own third fixture group) are
 *     intentionally NOT asserted either way: the server rejects them and
 *     the superset rule makes either client outcome benign (a false
 *     accept there just shows the server's 400 inline).
 *
 * PROPOSE half (automation backlog, 2026-08-14): `NL_PROPOSE_ACCEPTED` and
 * the propose-shaped entries in `NL_ASSIGN_REJECTED` are copied verbatim the
 * same way. Unlike the assign near-misses, none of the propose near-misses
 * are ambiguous the way "assign to <name>" was (there is no keyword the
 * grammar could accidentally absorb as a title) — the recognizer rejects all
 * five deterministically, so they are asserted as must-reject rather than
 * parked unasserted.
 */
const NL_ASSIGN_ACCEPTED: readonly string[] = [
  // (a) the Slack /task spelling, with and without the @, any case
  "/task assign @bob fix the login page",
  "/task assign bob fix the login page",
  "/TASK ASSIGN @Bob fix the login page",
  // (b) "give <name> a task to ..." and its colon form
  "give bob a task to fix the login page",
  "give @Bob a task to fix the login page",
  "give bob a task: fix the login page",
  "GIVE BOB A TASK TO fix the login page",
  // (c) the terse form
  "assign bob fix the login page",
  "assign @bob fix the login page",
  "Assign Bob fix the login page",
];

// Shape-ACCEPTED, roster-REJECTED server-side — the client only judges
// shape, so these must ALSO be client-accepted (the server names the
// unresolvable teammate in its 400).
const NL_ASSIGN_UNKNOWN_NAME: readonly string[] = [
  "assign fix the login page",
  "give bartholomew a task to fix the login page",
];

// "not an assignment at all" + "right verb, no title" — plain chatter or an
// unmistakably incomplete command; MUST stay client-rejected.
const NL_ASSIGN_REJECTED_MUST_REJECT: readonly string[] = [
  "what is bob working on?",
  "hello there",
  "bob should fix the login page",
  "can you assign this to bob?",
  "assign bob",
  "give bob a task to",
  "give bob a task:",
  "/task assign @bob",
];

// "right shape, wrong verb/prefix" — the server's own third fixture group.
// Deliberately UNASSERTED (see the module docstring above): the superset
// rule makes either client outcome acceptable for these.
const NL_ASSIGN_REJECTED_NEAR_MISS: readonly string[] = [
  "task assign bob fix the login page",
  "give bob a job to fix the login page",
  "/task claim GH-7",
];

// PROPOSE half — must be client-accepted (copied verbatim from
// `NL_PROPOSE_ACCEPTED`, `tests/team/test_nl_assign.py`).
const NL_PROPOSE_ACCEPTED: readonly string[] = [
  "propose an automation to draft the weekly digest",
  "propose a automation to draft the weekly digest",
  "propose an automation: draft the weekly digest",
  "propose automation: draft the weekly digest",
  "PROPOSE AN AUTOMATION TO draft the weekly digest",
  "propose an automation to draft the weekly digest #email",
  "propose an automation to draft the weekly digest for ai",
  "propose an automation to draft the weekly digest #email for bob",
];

// The five "propose near-misses" from `NL_ASSIGN_REJECTED` — the verb
// without the noun, the noun without a title, an unanchored prefix, and the
// wrong noun. Deterministically rejected (see the module docstring above),
// so asserted as must-reject rather than parked as near-miss.
const NL_PROPOSE_REJECTED_MUST_REJECT: readonly string[] = [
  "propose an automation",
  "propose automation:",
  "propose an automation to",
  "we should propose an automation to draft the digest",
  "propose a meeting to discuss automation",
];

describe("looksLikeNlAssign — composer intercept grammar parity", () => {
  it.each(NL_ASSIGN_ACCEPTED)("accepts server-accepted shape %j", (text) => {
    expect(looksLikeNlAssign(text)).toBe(true);
  });

  it.each(NL_ASSIGN_UNKNOWN_NAME)("accepts shape-valid, roster-unknown %j", (text) => {
    expect(looksLikeNlAssign(text)).toBe(true);
  });

  it.each(NL_ASSIGN_REJECTED_MUST_REJECT)("rejects plain chatter / titleless %j", (text) => {
    expect(looksLikeNlAssign(text)).toBe(false);
  });

  it("does not require a verdict on the near-miss set (superset rule — informational only)", () => {
    // No assertion on direction; this just documents the set exists and is
    // exercised elsewhere (nothing in this file depends on its outcome).
    expect(NL_ASSIGN_REJECTED_NEAR_MISS.length).toBeGreaterThan(0);
  });

  it.each(NL_PROPOSE_ACCEPTED)("accepts server-accepted proposal shape %j", (text) => {
    expect(looksLikeNlAssign(text)).toBe(true);
  });

  it.each(NL_PROPOSE_REJECTED_MUST_REJECT)("rejects proposal near-miss %j", (text) => {
    expect(looksLikeNlAssign(text)).toBe(false);
  });

  it("accepts a unicode name — JS \\w has no Unicode support the way Python's does", () => {
    expect(looksLikeNlAssign("give josé a task to fix login")).toBe(true);
  });

  it("still rejects genuinely unrelated chat starting with an unrelated word", () => {
    expect(looksLikeNlAssign("please summarize the last run")).toBe(false);
    expect(looksLikeNlAssign("")).toBe(false);
    expect(looksLikeNlAssign("   ")).toBe(false);
  });
});
