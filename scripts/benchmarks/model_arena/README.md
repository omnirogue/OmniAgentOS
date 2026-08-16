# Model arena — qwen35 vs grok-4.5 vs gemini-3.6-flash

Two independent measurements against the same three endpoints:

1. **Stress ramp** — how much parallelism and aggregate throughput each endpoint sustains.
2. **Quality suite** — 20 machine-graded tests, plus the speed each model took to answer them.

No LLM judging is involved in the headline numbers. Every score comes from executing
code, validating a schema, or matching an exact value, so a re-run reproduces it.

## Together.ai cheap-model slate

`run_arena quality --together` benchmarks a price ladder of Together models instead
of the three above, and adds cost telemetry (`$` per capability, per full suite)
sourced live from Together's own catalogue.

Two traps that cost real time to find:

- **Most of Together's cheap catalogue is not serverless.** `Llama-3.2-3B`,
  `trinity-mini`, `Nemotron-Nano-9B`, `Ministral-3-14B` and `Llama-4-Scout` all
  advertise a per-token price but reject requests with *"Unable to access
  non-serverless model"* — using them means paying hourly for a dedicated endpoint,
  which is not cheap. The catalogue's `running` flag does **not** identify these; it
  reads false even for models that answer fine. Only an actual request tells you,
  which is what `preflight()` does.
- **Probe with a real token budget.** A 64-token probe reported "empty completion"
  for `Qwen3.5-9B`, which spends ~186 hidden thinking tokens before its first
  visible one. `preflight()` uses 512.

## Contestants

| name | endpoint | notes |
|---|---|---|
| `qwen35` | `$QWEN35_BASE_URL` (RunPod H200, llama.cpp) | Qwen3.5-122B-A10B Q4_K_M, GGUF-only, served by `llama-server` |
| `grok-4.5` | `https://api.x.ai/v1` | reasoning model; reasoning tokens are counted in throughput |
| `gemini-3.6-flash` | `.../v1beta/openai` | Google's OpenAI-compatible surface |

Credentials come from `~/.config/omni/connections.env` (parsed, not executed —
inline `# comments` are stripped). Env vars of the same name override it.

The pod is toggled with `ninja on` / `ninja off` / `ninja status`, which also rewrites
`QWEN35_BASE_URL`. If the pod was created by hand instead, `llama-server` is not
running and that URL is stale — start the server and fix the URL before running.

## Usage

```bash
# prove the graders have teeth first — reference answers must score 1.0
.venv/bin/python -m scripts.benchmarks.model_arena.verify_graders

.venv/bin/python -m scripts.benchmarks.model_arena.run_arena stress
.venv/bin/python -m scripts.benchmarks.model_arena.run_arena quality
.venv/bin/python -m scripts.benchmarks.model_arena.run_arena all --run-id my-run
```

Useful flags: `--only qwen35`, `--levels 1,2,4,8,16,32,64`, `--cloud-max-n 16`
(caps the ramp for hosted APIs), `--qwen-base-url`.

## What the stress ramp measures

At each concurrency level N, N requests are released together by a thread barrier
and the batch is timed:

- **`system_tps`** — aggregate generated tokens/sec across all in-flight requests.
  This is the headline: peak `system_tps` marks the saturation knee.
- **`per_req_tps`** — what one caller feels. It falls as N rises once batching kicks in.
- **ttft p50/p95** — queueing shows up here first.
- **errors / 429s** — for a hosted API, this *is* the parallelism ceiling.

Prompts are salted per request, because a server with a prefix cache would
otherwise serve them from cache and report throughput no real workload can hit.
The ramp stops on its own when errors exceed 25% or throughput stops improving.

> **llama.cpp concurrency (measured on this pod, 2026-07-29).**
>
> | config | log line | peak aggregate | knee | ctx/slot |
> |---|---|--:|--:|--:|
> | default (no `--parallel`) | `n_slots = 4, n_ctx_slot = 262144, kv_unified = true` | 184.7 tok/s | N=4 | 262,144 |
> | `--parallel 16` | `n_slots = 16, n_ctx_slot = 16384, kv_unified = false` | 253.5 tok/s | N=16 | 16,384 |
>
> Raising `--parallel` is worth **+37% peak throughput** and roughly **3× better TTFT
> under load** (at N=16: 13.06s → 4.11s). Past 16 slots it regresses (228–233 tok/s at
> N=32/48), so 16 is the sweet spot for this model on an H200.
>
> **But `--parallel` also flipped `kv_unified` to false and divided context 16 ways**
> (262K → 16K per slot). The two are coupled: the pleasant default is unified KV with
> only 4 slots. Whether `--parallel 16 --kv-unified` gives both is UNTESTED — verify
> with `grep -a "load_model: initializing" /workspace/logs/server.log` after starting.
>
> Scaling is sublinear because this is a 256-expert MoE with ~9 experts active per
> token: a wider batch touches more experts, so weight traffic grows with batch size.
>
> The server also does prefix caching (`selected slot by LCP similarity`), which is
> exactly why the ramp salts every prompt.
>
> Two operational traps: `ninja on` is what actually starts `llama-server` and
> rewrites `QWEN35_BASE_URL` — a pod created in the RunPod console has neither. And
> never use `pkill -f llama-server` over ssh: the pattern matches the `bash -c`
> wrapper of your own command and kills it before it starts the server. Use `pkill -x`.

## The 20 tests

**Tier A — 12 coding tasks** reused from `var/e2e-bench/coding-arena`. The model sees
only `spec.md`; its file is run against a hidden pytest suite and scored binary
PASS/FAIL from the pytest exit code. Each suite is pre-verified: the reference
implementation passes and a plausible-but-wrong one fails.

**Tier B — 8 reasoning/handling tests**, fractional credit:

| id | measures | grading |
|---|---|---|
| `needle_long_context` | recall of one fact in a ~50k-token log | exact match on the buried city |
| `json_schema_strict` | instruction following under hard constraints | jsonschema validation, `additionalProperties: false` |
| `messy_extraction` | pulling current facts out of a contradictory thread | 7 fields, superseded values rejected |
| `math_multistep` | multi-step arithmetic with a compounding final step | exact answer (1137); 1128 scores partial |
| `sql_synthesis` | SQL correctness | query executed against a seeded SQLite fixture, result set compared |
| `regex_synthesis` | precision on a spec | positive recall × rule coverage over 8 rule groups |
| `bug_hunt_review` | code review recall | 5 planted defects, each matched by pattern |
| `hallucination_guard` | calibration — the API asked about does not exist | rewards denial, penalises invented signatures |

## Output

```
var/model-arena/<run-id>/raw.jsonl     every completion in full, with timings
var/model-arena/<run-id>/stress.json   per-level ramp data
var/model-arena/<run-id>/quality.json  per-test rows + aggregates
vault/benchmarks/model-arena-<run>.md  the report
```

`raw.jsonl` holds the complete text of every answer, so re-grading or a later
judging pass never has to re-spend contestant tokens.

## Fairness notes

- Providers run in parallel **with each other** (separate endpoints, no shared queue)
  and strictly sequentially **within** a provider, so one model's latency never
  inflates another's.
- `temperature=0` for the quality suite; `0.7` in the stress ramp to defeat caching.
- Two first-token numbers are recorded: `ttft_s` (first delta of any kind) and
  `ttfvt_s` (first *visible* token). For a reasoning model the gap is thinking time
  the caller genuinely waits through.
- Throughput counts reasoning tokens as well as visible ones — that is work the GPU did.
  Gemini never reports a `reasoning_tokens` field, so its thinking is recovered from
  the `total_tokens` gap; without that its throughput would be understated ~20×.
- **Buffered streams.** Gemini's OpenAI surface buffers and flushes, so its generation
  window can be 3% of total latency. Dividing tokens by that window reports a fictional
  four-figure tok/s, so a reply only counts as streamed at `content_chunks >= 3` and a
  window ≥20% of total; otherwise throughput is end-to-end and flagged `tps_end_to_end`.
- **Token headroom.** Every task gets at least 4096 (8000 for coding) output tokens.
  Both hosted models spend hidden thinking from the same budget — 241 thinking vs 11
  visible tokens was measured under a 256 cap — so a tight cap would score a model 0
  for running out of room rather than for being wrong.
- qwen35 is served `--reasoning off`. A client can still opt in with
  `chat_template_kwargs: {"enable_thinking": true}`, which the vault notes say flips
  some multi-step answers from wrong to right. The default run measures it
  **as deployed**; treat a thinking-on run as a separate contestant.
