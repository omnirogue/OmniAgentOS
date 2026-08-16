# prompt-ab — A/B replay harness for system-prompt changes

Repeatable, mechanical evidence for "does this prompt change actually help?".
Built 2026-08-08 from the 48h mechanization audit (the operator directive: replay real
failures, A/B old-vs-new prompt, promote only confirmed winners).

## What it does

Each **scenario** in `scenarios/` replays a REAL measured failure (its
`failure_ref` points at the fingerprint/ledger evidence) against two prompt
arms — `control` (current prompt) and `candidate` (proposed prompt) — N trials
each, on the lineage and **production effort tier** of the role under test.
Grading is 100% mechanical (JSON keys, enum fields, must/forbid regexes).
No LLM judges, ever.

**Promotion rule (strict):** candidate must strictly beat control AND pass
every trial. Ties or partial wins keep the control. A promotion commits the
new prompt text through the normal channel for that role (`system-prompts/`
registry conventions, `~/.claude/agents/*.md`, or the ThreeLoops `PROMPT-*.md`
files) citing the ledger line as evidence.

## Run it

```bash
cd ~/OmniAgentOS
python3 scripts/prompt-ab/run_ab.py               # all scenarios
python3 scripts/prompt-ab/run_ab.py <scenario-id> # one
```

Outputs:
- `var/prompt-ab/runs/<UTC-stamp>/results.jsonl` + `summary.json` — full trial detail
- `var/prompt-ab/ledger-YYYYMM.jsonl` — append-only verdict ledger with sha256
  digests of BOTH arms (every promotion is traceable to the exact texts compared)

## Add a scenario

Copy any file in `scenarios/`, keep the schema (documented in `run_ab.py`'s
docstring), and make sure:
1. `failure_ref` cites real evidence (a fingerprint id, a loopqueue ledger row,
   an audit artifact path) — scenarios without provenance get deleted.
2. `effort` matches the role's PRODUCTION tier (the operator ladder 2026-08-08: questions/
   mechanical=low, coding=medium, review/architecture/security=high/xhigh).
3. Grading criteria would have FAILED on the original bad behavior — check by
   running the control arm first; a scenario the control passes 3/3 teaches nothing.

## Caveats

- `claude -p` runs on the invoking account's profile effort; codex takes the
  scenario's `effort` directly.
- Single-turn text probes only — this tests decision/output behavior, not
  multi-turn tool execution. Multi-turn replay belongs to the audit plan's
  D-4 replay corpus (see Unified Mechanization plan §9).
- Small-N: the strict promotion rule trades sensitivity for zero-false-promotion.
  Raise `trials` for close calls.
