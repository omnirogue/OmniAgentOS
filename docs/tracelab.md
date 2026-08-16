# Tracelab — continuous trace mining into the improvement lane

Tracelab (`omniagentos/tracelab/`) turns heterogeneous agent traces — downloaded
public corpora AND OmniAgentOS's own telemetry — into evidence-backed,
threshold-gated improvement hypotheses that feed the `configtest_hypotheses`
lane (migration 083). It replaces the ad-hoc scripts in
`~/initech/AI-Traces/` (keyword-grep "failure analysis", stubbed model
calls, canned-fallback reports), which should be treated as superseded.

## The loop

1. **Ingest** — `make tracelab-refresh` searches HuggingFace for new
   agent-trace datasets and downloads bounded samples, each with a
   `MANIFEST.json` (what, when, selection rule, bytes) plus an append-only
   `refresh-log.jsonl` in the corpus root.
2. **Mine** — `make tracelab` (or `python -m omniagentos.tracelab scan …`)
   streams every recognized source through format adapters into one unified
   event model, computes deterministic per-trace metrics, and contrasts
   successful vs failed traces where ground-truth labels exist. Own telemetry
   (`ledger/runs-*.jsonl`, recent `~/.claude*/projects` transcripts) is
   included via `--own`.
3. **Propose** — statistically supported contrasts become `proposed` rows for
   `configtest_hypotheses` (`--emit-db`), each carrying its numeric evidence
   and exemplar trace pointers. Confidence is stamped honestly:
   `correlational` (labeled contrast) or `observational` (prevalence only).
4. **Test** — the improvement lane's judged config tests promote or disprove
   hypotheses (`proposed → testing → … → replicated | disproved`). Tracelab
   never advances lifecycle state; re-runs only refresh evidence.

## Adapters

| adapter | format | outcome labels |
|---|---|---|
| `claude-code` | Claude Code session JSONL (HF corpora + our own `~/.claude*/projects`) | session-limit truncation only |
| `swe-agent` | nebius SWE-agent parquet | `target` bool |
| `openhands` | SWE-rebench trajectories parquet | `resolved` int |
| `conversations` | AgentTrove terminus-2 + hermes ShareGPT parquet | AgentTrove harness failures only |
| `pi-session` | pi-coding-agent session JSONL (0xKobolds) | none |
| `own-runs` | `ledger/runs-*.jsonl` escalation ladders | run `state` |

Not yet adapted (documented in the profiling notes): CooperBench
team-trajectories tarballs (multi-agent coordination — high value, complex
format), Yunjue base64 text logs, LangGraph checkpoints, SWE-agent `.traj`.

## Design rules

- **No keyword-counting on prose.** Taxonomy classification runs only over
  tool results actually flagged as errors; an agent *talking about* errors is
  not a failure. Taxons extend `omniagentos.reflection.taxonomy` — one
  vocabulary across reflection and tracelab.
- **Per-dataset contrasts guard pooled stats.** Corpora with different base
  rates dilute each other (Simpson); the hypothesis rules consult
  `pattern_lift_by_dataset` before declaring or discarding signal.
- **Every claim is auditable.** Reports cite trace IDs + source paths;
  hypothesis evidence embeds the numbers it fired on.
- **Streaming everywhere.** 2 GB parquet shards are read in small batches;
  excerpts are capped at 400 chars; full blobs never enter memory.

## Scheduling

Wire a nightly run the same way as other maintenance jobs (see the
`install-steward.sh` idiom): a launchd plist running
`make tracelab-refresh && make tracelab` off-peak, or register a routine via
the routines engine. Reports land in `var/tracelab/report-<stamp>.md`;
`mining-summary.json` + `hypotheses.json` are stable paths for downstream
consumers.
