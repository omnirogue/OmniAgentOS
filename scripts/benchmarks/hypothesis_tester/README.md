# hypothesis_tester — does past-run data change agent behavior?

Repeatable, logged experiments testing the core claim of the emergent-neural
multi-agent theory (an operator-supplied hypothesis document):
**memory of past runs must measurably change future behavior** — and which
representation (wins/losses, transcripts, consolidated lessons, activation-
selected experience) carries the effect. Cheap pinned OpenRouter models, three
seeded synthetic worlds, deterministic verifiers, no LLM judges.

Read `DESIGN.md` first — it is the pre-registration (arms, matrix, metrics,
frozen decision thresholds). Changing thresholds after data exists requires a
new experiment id and a note there.

## Run

```bash
cd ~/OmniAgentOS  # or any worktree; stdlib-only, no venv needed
python3 -m scripts.benchmarks.hypothesis_tester.experiment estimate
python3 -m scripts.benchmarks.hypothesis_tester.experiment run --exp-id pilot-$(date +%m%d) \
    --families trapcli --arms none,transcripts --models nemo --seeds 0 --episodes 8
python3 -m scripts.benchmarks.hypothesis_tester.experiment run --exp-id 20260812-full
python3 -m scripts.benchmarks.hypothesis_tester.experiment analyze --exp-id 20260812-full
```

- Key: `OMNIAGENTOS_OPENROUTER_API_KEY` (or `OPENROUTER_API_KEY`) from env or
  `~/.config/omni/connections.env`. Never logged.
- Outputs under `var/hypothesis_tester/runs/<exp-id>/`: `config.json` (frozen,
  sha-identified), `episodes.jsonl` (full audit trail incl. memory text),
  `results.jsonl` + `summary.json`, `ANALYSIS.md` + `analysis.json`.
- Verdict ledger: `var/hypothesis_tester/ledger-YYYYMM.jsonl` (append-only).
- Re-running the same `--exp-id` resumes (skips logged episodes). A changed
  matrix under the same id is refused.
- `--dry-run` = offline scripted agent, zero spend (used by the tests).
- exp ids prefixed `pilot-` are excluded from confirmatory claims.

## Using it as the effectiveness gate for new system developments

Model a new development as a **memory/context arm** (subclass or function in
`memory.py`), then run the frozen matrix with `--arms none,<new-arm>,placebo`:
the paired delta vs `none` measures the effect; the delta vs `placebo` separates
content learning from context priming. Real estate data (ledger wins/losses,
session transcripts) can be plugged in by generating `EpisodeRecord`s from a
corpus adapter — the interface only needs task/action/verdict/feedback text.

## Relationship to `scripts/prompt-ab`

Complementary, deliberately not merged (assessed 2026-08-12): prompt-ab replays
real single-turn failures to A/B *prompt texts* with a strict all-trials
promotion rule; hypothesis_tester measures *learning behavior* over
multi-episode runs with paired bootstrap CIs. Both share the same evidence
doctrine: mechanical grading only, `results.jsonl`/`summary.json`, append-only
monthly sha-digested verdict ledgers under `var/`.
