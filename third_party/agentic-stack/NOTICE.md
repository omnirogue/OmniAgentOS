# agentic-stack — attribution

Source: https://github.com/codejunkie99/agentic-stack (Apache License 2.0, © 2026 Avid).
A copy of the licence is in `LICENSE` beside this file.

**What we took:** architecture and state machines — the memory lifecycle
(episodic → cluster → candidate → human-gated graduation → rendered lessons, with
salience decay and a nightly dream cycle), the salience scoring shape, and the loop
contract / checker verdict grammar.

**What we did NOT take:** their implementations of the empty and error paths. A review of
that codebase found eleven instances of the defect class this repo exists to eliminate — a
non-result presented as a favourable result. Examples: `auto_dream.py` silently drops
unparseable JSONL lines on rewrite; `review_state.mark_graduated` reports success without
writing the lesson; `validate.py`'s `passed = not reasons` returns "no duplicates" when the
lessons file is missing; `pre_tool_call.check_tool_call` is invoked by nothing, so a
documented permission layer never executes; `context_budget` adds to `used` without ever
checking `budget`. Every ported path is rewritten fail-loud against our null-discipline
invariant (W4-03: a rate over an empty set reports unknown, never a favourable number).

Files in `omniagentos/memlife/` derived from their design carry a header naming the
upstream module and stating that behaviour was modified.

Not vendored under any circumstance: `docs/demo/` (Remotion, source-available/paid).
