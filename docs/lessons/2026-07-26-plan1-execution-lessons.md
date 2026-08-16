# Lessons — Plan 1 + Stage-0 execution (2026-07-26)

Seed corpus for the reflection loop: concrete failure patterns observed while 9 Gemini lanes + 1 integrator executed today's plan. Each entry: what happened → generalized lesson → where the fix belongs.

## Environment / model configuration

1. **Wrong model id dispatched** — `gemini-3.1-pro` is in `configs/modelintel.yaml` but the Gemini CLI does not serve it (`ModelNotFoundError`); every lane failed instantly until switched to `gemini-3.6-flash`. → Lesson: model registry entries must be liveness-checked against their actual harness before dispatch (provider-sentinel should probe `ListModels`, and the router should down-rank ids that fail probes). → Fix target: `configs/modelintel.yaml` (mark 3.1-pro CLI-unavailable), provider-sentinel probe, modelintel refresh job.
2. **Shell alias duplication** — the interactive alias `gemini` already injects `--approval-mode=yolo`; adding the flag explicitly produced `Invalid approval mode: yolo,yolo`. → Lesson: dispatch through `command <binary>` (alias-proof) in all subprocess adapters and orchestration scripts. → Fix target: adapters/gemini.py already uses argv (immune); document in AGENTS.md; any shell-based dispatcher must use `command`.
3. **Workspace floor rejected the repo itself** — first real swarm run 403'd (`workspace outside approved roots`); the floor's default set still contained only the DELETED `~/OmniAgentOS`. → Lesson: policy defaults referencing dead paths fail silently until first use; approved-roots need a startup sanity check that warns on nonexistent roots. → Fixed today via `configs/mounts.yaml` + `OMNIAGENTOS_PROJECT_BASES`; add the warning.

## Orchestration patterns (multi-lane execution)

4. **2 of 9 lanes finished without committing** (agent-context, m4) — reports claimed "committed cleanly", trees were dirty, branches empty. → Lesson: "committed" claims must be machine-verified; a lane is DONE only when `git log main..branch` is non-empty and `git status` is clean. Integrator (or a lane post-check) should auto-commit verified work rather than trusting reports. → Fix target: lane brief template (explicit final `git log` proof), integrator checklist.
5. **Build-tool churn polluted diffs** — 2 of 3 UI lanes committed Next.js's automatic `tsconfig.json` rewrite (formatting + `.next-build/types` include). → Lesson: known build-tool churn files should be auto-reverted before commit unless intentionally changed; add a per-repo churn list (tsconfig.json) to the brief template.
6. **Shared-path work contracts collided at merge** — every lane's `PLAN2-TASK.md` at worktree root → add/add conflicts on main. → Lesson: per-lane files must live at lane-scoped paths from birth (`docs/plan2-briefs/<lane>.md`), not shared roots.
7. **A literal-minded agent archived live data** — C3 moved ~150 files out of `vault/` (live knowledge store) because the never-touch list didn't name it. → Lesson: destructive/move operations need (a) an explicit allowlist of what MAY move, not just a denylist, and (b) a human gate; inventory-then-approve beats move-then-revert.
8. **Deliberate config changes broke test pins** (worktrees default-off pin, sync swarm-create contract, brief byte-pin). → Lesson: every intentional default flip must search for pin tests in the same change (`rg` the flag/contract name through tests/); the integrator gate catches these, but lanes should own their pins.
9. **Verification suites were lane-scoped** — M3 ran tests/intake+tests/swarm and missed breaking tests/api. → Lesson: lane briefs must name the FULL blast-radius suite (grep for imports of touched modules), and the integrator always re-runs the union gate before merge.

## Process wins to preserve (positive lessons)

10. **Update-the-branch-then-merge** (integration ← main first) made a 66-commit landing conflict-free.
11. **Disjoint file-ownership lanes** made 9 parallel coders mergeable with exactly one conflict.
12. **Cross-lineage review** (Gemini implements → Claude reviews) caught every one of the defects above before main.
13. **Worktree isolation + companion contracts (TASK.md/WORKBOOK.md)** worked first try in production (`swr_2bdd821f…`).

## From Kimi's transcripts (~/.kimi-code — 47 workspaces, 113 sessions, ~5,094 logged responses)

14. **Kimi fan-out multiplies latency, not throughput** — main-agent TTFT median 5.0s; sub-agents under 8-way fan-out: median 43.6s, p90 181s, max 868s. The Moonshot account serializes concurrent requests. → Cap kimi concurrency at 2-3 per run; add a provider-concurrency capability table to routing config; buy parallelism from providers with real concurrent capacity.
15. **Global `effort=max` on every call** — `~/.kimi-code/config.toml` pins default_effort=max + always_thinking; the registered highspeed model (`kimi-k2.7-code-highspeed`) is never used; the operator had to steer effort down mid-session. → Effort is a per-task-tier decision; default low, opt into high/max.
16. **Hour-long hangs believed complete** — one session hung ~1h on an API request that never returned; killed + resumed. → TTFT watchdog (~120s hard deadline → abort + fresh retry); every resume brief must open with "re-verify disk state; do not trust your last message."
17. **Verdicts die with the process** — a reviewer finished items 1-9, was killed before emitting PASS/FAIL; the verdict existed only in the unsent final message. → Brief template: create `<lane>-verdict.md` skeleton FIRST, append evidence incrementally, flip the verdict line last.
18. **On harness timeout, narrow scope — never retry the same prompt** (B09: broad prompt hit the 300s timeout; scoped retry completed cleanly). → retry policy `on_timeout: narrow_scope_and_retry`.
19. **Shared worktrees are single-writer** — another agent switched the shared integration worktree's branch mid-cycle; two of Kimi's merges silently landed on `warning-debt` instead of `integration`. Precondition was checked once per cycle, not per mutation. → Exclusive worktree leases; re-assert `git rev-parse --abbrev-ref HEAD` immediately before AND after every merge.
20. **Zero-conflict merges are not semantically clean** — wave-4 needed a repair commit for textually-clean losses (dropped import, lost assignment, degraded mocks). → Post-merge focused test run is mandatory with no exemption for clean merges.
21. **`git add -A` during conflict resolution pollutes branches** (swept an untracked ledger into history). → Merge agents stage enumerated paths only.
22. **Half the MCP fleet dies at startup, silently** — 7 servers unavailable every session (handshake mismatches + `uvx ENOENT` off-PATH), and the agent never reports having fewer tools. → Loud MCP preflight; put `uv/uvx` on the CLI PATH; surface missing-tool state into agent context.
23. **The operator's six speed directives are orchestrator policy, not steering** (eager dependency-ordered launch; 25-min worker deadline then kill+respawn; kill >90%-CPU-for-10-min spinners; continuous merge to integration, never batched behind the slowest lane; no periodic self-check spawns; reviews via self-contained file briefs to a standing critic fleet). → Bake into the orchestrator system prompt + scheduler defaults.
24. **Cron loops coalesce silently** — the 37-min merge cron fell ~2.5h behind (coalescedCount=4) and replayed stale work; its brief shape is otherwise the canonical template (verbatim paths, SHA verification, never-do list, STOP-and-report clause). → Keep the brief; add "if coalesced, execute only the newest fire."
25. **The ledger can't answer "how is Kimi doing"** — kimi rows carry model:null, tokens:null, cost:0, and are simultaneously `killed_by:supervisor-shutdown` AND `final_state:completed`. → Populate extproc usage in the ledger; the harvester joins ledger rows to `~/.kimi-code` on (time window × workDir); fix the state contradiction.
26. **Durable records lag task completion** — Kimi's B09 re-verification COMPLETED with `VERDICT: APPROVE` (artifact on disk) but `KIMI_MERGE_LEDGER.md` still said "stopped mid-flight": completion arrived as a steer event and the record only updates on the next reasoning pass. → Task-completion hooks must write the durable record directly, not wait for the agent to notice.

**Kimi harvest map for the loop (Stage A adapter):** entry point `~/.kimi-code/session_index.jsonl` ({sessionId, sessionDir, workDir}) → per-session `state.json` (timestamps, title, lastPrompt, agents{} incl. verbatim sub-agent briefs in `swarmItem`) → `agents/<id>/wire.jsonl` (the transcript: turn/tool/think/usage events, protocol 1.4) → `agents/main/tasks/<id>/output.log` (detached-task outputs — where verdicts land) → `logs/kimi-code.log` (cheap TTFT/decode/token line metrics). No cost rollup exists — `usage.record` events are the only usage source.

**Provider snapshot (evidence-based):** Kimi = best process/merge/adjudication agent (meticulous conflict resolution, correct fail-closed holds, honest self-reporting; 70/75 lane sessions produced explicit verdicts) but slowest and least parallelizable. Codex/sol = fast, only provider reporting real token usage, but returned `review_denied` on 4 consecutive swarm attempts. Claude/opus = crashed mid-swarm on 3 of 4 bridge sessions (investigate). Gemini 3.6-flash = fast implementer, weak on commit discipline (2/9 lanes) and literal-minded on briefs.

## From Gemini's transcripts (~/.gemini/tmp/<slug>/chats/*.jsonl — full fidelity: toolCalls+args+results, per-turn tokens+model)

27. **Silent model substitution** — all 9 lanes launched `-m gemini-3.6-flash`; every one of 4,544 recorded turns says `"model":"gemini-3.5-flash"`. The flag was accepted and ignored. → Requested ≠ served; only the transcript's per-turn `model` field is ground truth. Post-run assertion in the dispatcher; liveness column in modelintel.
28. **The rejected model had a working alias in the registry** — `gemini-3.1-pro` 404'd while `gemini-3.1-pro-preview-customtools` (modelintel.yaml:333) served fine. → Order aliases by probed liveness; try alias-fallback before failing a wave.
29. **Transient 5xx/429 retry storms are invisible in reports** — per-lane counts up to 77 hidden retries; the CLI retries with backoff silently. → Harvest retry counts per lane; feed provider routing weights.
30. **Workspace jail blocked the referenced spec** — m1's brief cited a Desktop path; the CLI denied it (`Path not in workspace`) and the lane proceeded on partial context. → Briefs must inline or copy specs INTO the worktree; never cite paths outside the workspace root.
31. **Malformed tool-regex burns turns silently** (26 `Invalid regular expression` recoveries across lanes). → Tools should auto-escape/fall back literal; error counts are a lane-quality metric.
32. **Cost/quality is measurable per lane** — 5.9M (c3) to 32M (m3) tokens; the cheapest lane was the wrongest. → Track tokens.total vs review outcome; don't reward cheap-and-wrong.
33. Harvest note: `~/.gemini/projects.json` is the entry map; `history/` contains only stubs (skip); scratchpad stdout captures are a lossy 0.2% — never the corpus.

## From Grok + Claude transcripts

34. **Claude's bridge crashes were nested Seatbelt, not capability** — `_INNER_SANDBOX_DISABLE` never listed claude; Read worked, every mutating tool died as opaque `api-error` (`toolDenialKind: permission-rule`), 3/3 bridge sessions failed, $4 burned on misdiagnosis. FIXED 2026-07-26 (`adapters/common.py`: `--settings '{"sandbox":{"enabled":false}}'` under the outer wrap). → Sandbox-disable exemption lists are correctness contracts; fix per-class, assert no double-confinement at launch; audit gemini/kimi.
35. **Opaque error text costs whole sessions** — the denial reached the model as literally `api-error`; two Opus workers probed ~12× each and concluded "API outage". → Propagate `toolDenialKind` into tool_result text.
36. **Terminal states need machine-readable causes** — ledger recorded `failed`/`crashed` with `killed_by:null` and no error; costs present but tokens null. → Ledger gains first_error/tool_failure_count; usage persists on error paths.
37. **Grok's own telemetry is richer than our ledger** — `~/.grok/sessions/<enc-cwd>/<uuid>/signals.json` carries errorCount, toolFailureCount, contextWindowUsage, doomLoopRecoveryAttempts, latency p50/p99; the adapter uses `estimated_usage` instead. → Harvest signals.json; replace estimates with CLI truth. (`session_search.sqlite` is a prebuilt cross-session index.)
38. **Headless grok fails the github MCP server on every launch** (auth_required, non-interactive) — pollutes toolFailureCount. → Pre-provision non-interactive creds or disable the server for headless runs.
39. **Workers who can't write must still hand off** — WORKBOOK/subtask writes died with the sandbox, so state lived only in the final message. → Parse the final assistant message as a fallback handoff.
40. **Grok-as-reviewer is strong**: 3/3 exact-SHA APPROVE verdicts with reproduced gates and counterfactuals at 9-15% context in <5min. Claude-as-worker: reasoning excellent (found a real CSS cascade bug + the coordinator's verify_command gap), execution was 100% infrastructure-blocked.

## From Codex transcripts (~/.codex/sessions/<Y>/<M>/<D>/rollout-*.jsonl)

41. **`review_denied` ×14 was our own unsatisfiable gate** — planner emitted `verify_command: ""` with the mechanical gate ON; `_detect_mechanical_suite` (scheduler.py:828) only recognizes pytest or npm-with-typecheck layouts and never disclosed accepted paths; codex shipped verify.sh + npm test + a passing 5-test suite and was still denied. Control: identical solo benchmark arms went 3/3 exit-0. → A fail-closed gate must be provably satisfiable by the agent it judges; the accepted-command list belongs IN the worker brief; planner must drop the gate for formations with no detectable suite.
42. **Identical retry feedback means environmental failure** — attempt 4 carried three copies of the same denial string. → Dedupe feedback digests; after 2 identical denials escalate provider or mutate the plan, never replay.
43. **Codex's tool router rejects whole compound commands on one destructive token** (`rm -rf __pycache__ && test && rg …` → all rejected). → Never bundle cleanup with verification for codex; deletes as isolated calls.
44. **Provider tool-arg floors are silent turn-killers** (`timeout_ms must be at least 10000`). → Encode known floors in adapters; clamp before dispatch.
45. **For codex, cwd IS the write boundary** — `apply_patch` refused WORKBOOK writes outside cwd regardless of approval_policy. → provider_exec must put `var/swarm/<run>/<task>/` in workspace_roots for codex spawns.
46. **Ledger effort/tokens are requests and estimates, not reality** — ledger says xhigh, every turn_context says high (CBM downgraded); token counts ~20x inflated by cached_input; cost 0 on subscription. → Record effective params from rollouts; budget subscription providers on `rate_limits.primary.used_percent`, store cached tokens separately.
47. **Rollout files are not sessions** — 29 files can replay one conversation (forks/subagents rewrite full rollouts). → Dedupe on payload.id; build the tree from parent_thread_id; attribute usage to leaf rollouts only. `session_index.jsonl` is STALE (unlike Kimi's) — enumerate the date tree.
48. **Repeated worker observations are coordinator alerts** — five sessions independently reported PLAN.md sha mismatch vs the assigned plan hash (planner projection bug). → Promote N-independent-reports to an alert; fix planner hashing.
49. **Codex parallelizes cleanly** (13 concurrent sessions, TTFT median 8.9s) but nothing bounded a 4.6-hour turn. → Raise codex lane concurrency; add a turn-duration watchdog.
50. **Codex quality verdict: strongest, most disciplined agent in the fleet** — 13/13 workbook discipline, zero git violations, self-verified with headless-Chromium smokes, caught a real coordinator bug; its 0/4 swarm record was entirely the gate's fault. Promote as implementer AND reviewer.

## Session-level telemetry worth mining daily

- Lane wall-clock vs scope size (m1 largest, finished mid-pack; c3 fastest and wrongest — speed/care tradeoff is measurable).
- Which briefs produced zero-deviation implementations vs deviations (brief quality signal).
- Formation outcome rows (`formation_selections.source='production'`) now accumulate per dispatch — first real corpus for the selector.
- Router shadow log (`var/modelintel/router_shadow.jsonl`) accumulates incumbent-vs-LLM routing disagreements with latencies.
