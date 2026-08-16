# Phase-2 runbook — operating the five dark upgrade tracks

Five upgrade tracks merged behind three-rung flags. This is the operator's guide to
running them: what state they are in, how to advance one rung, how to measure whether
that rung earned the next one, and how to put it back.

The governing rule for every track below: **`off` means byte-identical to the feature not
existing, `shadow` computes and records but applies nothing, only `enforce` changes
behaviour.** A flag is a rollout control, not an authorization — grant, policy and
human-approval requirements still apply at every rung.

---

## 1. Current state

Every track ships dark. Nothing on this list is enforcing anywhere by default.

| Track | Config file / key | Default in-repo | Env kill-switch | Telemetry sink |
| --- | --- | --- | --- | --- |
| Task-shape router | `configs/routing.yaml` → `task_shape_router.mode` | `"off"` | `OMNIAGENTOS_TASK_SHAPE_ROUTER` | `task_shape_decisions` (migration 079) |
| Tool catalog + search | `configs/toolplane.yaml` → `tool_catalog.mode` | `"off"` | `OMNIAGENTOS_TOOL_CATALOG` | `<ledger>/toolplane-observations/*.json` |
| Tool scheduler | `configs/toolplane.yaml` → `tool_scheduler.mode` | `"off"` | `OMNIAGENTOS_TOOL_SCHEDULER` | `<ledger>/toolplane-observations/*.json` |
| Autonomy lease | `configs/lease.yaml` → `autonomy_lease.mode` | `off` | `OMNIAGENTOS_AUTONOMY_LEASE` | `<ledger>/leases-YYYYMM.jsonl` |
| Gate evidence | *(no flag — additive telemetry)* | always on | *(n/a)* | `gate_evidence` (migration 080) |

Resolution order for all four flagged tracks is identical and mirrors
`omniagentos.scope.config`: **env beats config file beats `off`.** The env override is
bidirectional — `OMNIAGENTOS_AUTONOMY_LEASE=off` disables the lease on a host whose
`configs/lease.yaml` enables it, which is what makes it a kill switch rather than just an
enable switch.

Config-path overrides (useful for canaries — see §5): `OMNIAGENTOS_LEASE_CONFIG`,
`OMNIAGENTOS_PARALLELISM_CONFIG`, `OMNIAGENTOS_METACOG_CONFIG`.

### What "the lease off" does and does not mean

Turning the lease off removes **only the lease layer**. The pre-existing OS sandbox
(`omniagentos/runner/sandbox.py`), the env scrub, the credential broker and the
fail-closed unattended-launch floor are all unchanged and still enforced with the lease
off. `mode: off` restores exactly today's behaviour and never weakens it.

---

## 2. Rollback — one line per track

Rollback is always the same move: set the env flag to `off` on the affected host and
restart the process that reads it. No migration is reverted, no config file is edited,
and the telemetry already collected stays queryable.

```bash
export OMNIAGENTOS_TASK_SHAPE_ROUTER=off   # task-shape router
export OMNIAGENTOS_TOOL_CATALOG=off        # deferred tool catalog + search
export OMNIAGENTOS_TOOL_SCHEDULER=off      # resource-aware tool scheduling
export OMNIAGENTOS_AUTONOMY_LEASE=off      # autonomy lease (FS roots, ceilings, TTL, egress)
```

Gate evidence has no kill switch because it applies nothing — it is an additive
append-only record of gate runs.

---

## 3. The canary ladder

Four rungs. **A rung is never climbed on intuition; it is climbed on the evidence
`promotion_report.py` grades in §4.** A rung whose thresholds read `INSUFFICIENT
EVIDENCE` has not been earned — that verdict means "keep accruing", not "close enough".

**Rung 1 — shadow evidence accrual.** `mode: shadow` on a working host. The feature
computes its decision and writes it to its sink; nothing about execution changes. Stay
here until the evidence count clears the report's `--min-samples` and the coverage window
spans real, varied work — not one afternoon of one task class.

**Rung 2 — internal reversible canary.** `mode: enforce` for internal/self-hosted work
only, on work whose failure costs nothing but a re-run. Requires: rung-1 thresholds not
`NOT MET`, and every safety count at zero. Watch the sinks daily. This is the first rung
where the feature can actually break something, so it is also the first rung that needs a
named owner watching it.

**Rung 3 — task-class canary.** `enforce` for one named task class (one discipline, one
topology, one lane) chosen because rung-2 evidence shows the feature helps *that* class.
Do not widen to a second class on the first class's evidence.

**Rung 4 — targeted default.** Flip the config-file default for the proven classes only;
leave the env kill switch in place indefinitely. "Targeted" is load-bearing — a global
default is not on this ladder.

At every rung, a regression drops the feature straight back to `off`, not back one rung.

---

## 4. Promotion thresholds

These are the approved gates. A feature promotes only when its thresholds are `MET`;
**any safety count greater than zero rejects outright, regardless of every other number.**

| Track | Thresholds | Safety count (non-zero ⇒ REJECT) |
| --- | --- | --- |
| Task-shape routing | ≥ **+5pp** accepted-rate **or** ≥ **20% faster** in-class | sequential decisions provisioning > 1 worker |
| Tool disclosure | ≥ **70%** fewer initial schema tokens **and** selection within **2pp** **and** zero unauthorized disclosure | unauthorized disclosures |
| Resource-aware execution | ≥ **20%** wall **or** billed-token reduction **and** serial-equivalence | *(none defined; equivalence failure is a NOT MET)* |
| Autonomy lease | ≥ **50%** fewer permission prompts **and** zero escapes | lease escapes (enforce-mode records unsigned or not enforced) |

"or" thresholds are graded as an `any_of` group — one `MET` carries the group. "and"
thresholds are `all_of` — every member must be `MET`.

### Running the report

```bash
# Markdown to stdout against the live sinks
uv run python -m scripts.benchmarks.promotion_report

# JSON for a dashboard or a diff between two windows
uv run python -m scripts.benchmarks.promotion_report --format json --out var/promotion.json

# Point it at a specific host's evidence
uv run python -m scripts.benchmarks.promotion_report \
    --db /path/to/omniagentos.db \
    --ledger-dir /path/to/ledger \
    --observations-dir /path/to/ledger/toolplane-observations \
    --min-samples 50
```

Defaults: `--db` → `default_db_path()` (`$OMNIAGENTOS_DB` or `var/omniagentos.db`),
`--ledger-dir` → `default_ledger_dir()` (`$OMNIAGENTOS_LEDGER_DIR` or `ledger/`),
`--observations-dir` → `<ledger-dir>/toolplane-observations`. `--now` pins the report
timestamp so two runs over the same evidence are byte-identical.

The report is deterministic and fully offline: no network, no LLM, no subprocess. A
missing DB, a missing ledger or a corrupt line is counted and reported as missing
evidence — never as a clean pass.

**Read the `Missing evidence` section, not just the verdict.** Several thresholds are
currently ungradeable by construction, and the report says exactly which field would have
to be recorded to grade them (e.g. the toolplane observation schema is metadata-only by
design and carries no token counts, so the 70%-token-reduction threshold cannot be
computed from it — it needs a per-session measurement from
`omniagentos/toolplane/exposure.py` under catalog `off` vs `shadow`).

---

## 5. Monitoring pointers

| What | Where | Useful query |
| --- | --- | --- |
| Lease decisions | `<ledger>/leases-YYYYMM.jsonl` | `jq -r '.event + " " + .mode' leases-*.jsonl \| sort \| uniq -c` |
| Lease escapes | same | records with `mode=="enforce"` and (`signed==false` or `enforced==false`) |
| Route decisions | `task_shape_decisions` (079) | `SELECT route, topology, worker_count, applied, COUNT(*) FROM task_shape_decisions GROUP BY 1,2,3,4;` |
| Route outcomes | `formation_selections` (065/078) | join `task_shape_decisions.board_task_id = formation_selections.task_id` |
| Gate evidence | `gate_evidence` (080) | `SELECT gate_name, exit_code, COUNT(*) FROM gate_evidence GROUP BY 1,2;` |
| Tool calls | `<ledger>/toolplane-observations/*.json` | one JSON object per call: `tool`, `status`, `error`, `duration_ms` |
| Stuck observations | `<ledger>/toolplane-observations-pending/` | non-empty ⇒ the sink is failing to write; drain with `toolplane.observe.drain_spool()` |
| Lost observations | `<ledger>/toolplane-observations-failed/` | **non-empty is an alert** — records that exhausted their retries |

The observation sink is metadata-only by design: it never records tool arguments, output
content, paths to user data, or credentials. Do not add them to make a metric easier to
compute; add the metric's own counter instead.

---

## 6. Benchmark control baseline

Any enforce-mode canary is compared against the frozen control capture, not against
memory or a previous week's impression. See
[`benchmarks/PHASE2-CONTROL-BASELINE.md`](benchmarks/PHASE2-CONTROL-BASELINE.md) for the
numbers, the capture ids, and the rule.

```bash
# Re-capture an arm under a canary flag and compare to the control
OMNIAGENTOS_AUTONOMY_LEASE=enforce \
  uv run python -m scripts.benchmarks.capture_baseline --arm grok --replicates 3 \
    --label canary-lease-enforce
```

The corpus digest is checked before anything runs: if the fixtures have drifted from
`tests/benchmarks/frozen_digests.json`, the capture refuses, because results from a
changed corpus are not comparable to earlier baselines.

---

## 7. Per-CLI proxy compatibility

**Status: proxy egress mode is not viable for the grok lane.** This is a live finding from
2026-07-27, not a theoretical concern.

### What was tested

`configs/lease.yaml` → `autonomy_lease.net.default_policy` accepts `open`, `deny` and
`proxy`. Under `proxy`, direct egress is denied by the sandbox profile and exactly one
ephemeral port is re-opened for a parent-side allowlisting CONNECT proxy. That only works
if the sub-CLI actually routes its traffic through the proxy — which it can only do by
honouring the `HTTP_PROXY` / `HTTPS_PROXY` environment variables it is handed.

Two independent observations on the grok CLI (0.2.112):

1. **Under lease `enforce` + net `proxy`: timed out.** Never reached `api.x.ai`; hung
   until the lease TTL.
2. **Isolated probe** (`scripts/benchmarks/proxy_compat.py`, direct egress *allowed*,
   `HTTPS_PROXY` pointed at a closed local port): **hung for the full 90s budget.** The
   control — the identical call with no proxy variables set — succeeded in **5.9s**.

Observation 2 is the informative one, and it **corrects the obvious first reading**. If
grok simply ignored `HTTP(S)_PROXY`, the probe would have dialled `api.x.ai` directly and
returned in ~6s like the control. It did not. So grok *is* affected by the proxy
variables — it does not blindly bypass them — but it does not degrade gracefully when the
proxy is unreachable: **it hangs instead of failing fast.**

Why it then failed under real lease proxy mode (where a working parent-side proxy *was*
listening) is **not yet determined**. Do not guess in this document; the candidates are a
CONNECT-handshake incompatibility, a partial honouring that covers some requests and not
others, or a port/allowlist mismatch. Someone should isolate it before proxy mode is
reconsidered for this lane.

| Provider CLI | Under lease `enforce` + net `proxy` | Isolated proxy probe | Status |
| --- | --- | --- | --- |
| `grok` (0.2.112) | timed out to lease TTL | hangs 90s (control: 5.9s) | **FAILS** |
| `claude` | not yet run | not yet run | UNTESTED |
| `codex` | not yet run | not yet run | UNTESTED |
| `gemini` | not yet run | not yet run | UNTESTED |
| `kimi` | not yet run | not yet run | UNTESTED |

The operational conclusion is unchanged and is what matters: **proxy egress is not viable
for the grok lane.** The failure mode is the worst kind — a silent hang to TTL rather than
a fast, diagnosable error — so a lane left on it burns its entire budget producing nothing.

**Enforce mode itself is fine — it is only the `proxy` egress policy that fails.** Lease
`enforce` with net `open` (FS read/write roots + rlimits + TTL) works for grok today:
`fx_001_greenfield_palindrome` passed under it and the ledger recorded `enforced: true`.
So the lease's filesystem and resource half can be canaried on the grok lane now; the
egress half cannot.

Do not read `proxy` as an exfiltration boundary even where it works. SBPL enforces only
the *port* half of the filter, so that one port is reachable on any host and a sub-CLI
that *wants* to evade the allowlist can. `proxy` controls accidental and incidental
egress. The real defence is unchanged and does not rely on hostname filtering: secret
dirs are read-denied, the env is an allowlist, and the broker holds the real keys.

### How to test one CLI's proxy compatibility

**Step 1 — the cheap isolated probe.** Run this first; it needs no lease, no sandbox and
no proxy server, and it takes one trivial API call per CLI.

```bash
uv run python -m scripts.benchmarks.proxy_compat --clis claude        # one CLI
uv run python -m scripts.benchmarks.proxy_compat --all --format json  # the whole matrix
```

It points `HTTPS_PROXY` at a closed local port and makes one trivial call. Read the
verdict with the inversion in mind:

| Verdict | Meaning | Implication for proxy egress |
| --- | --- | --- |
| `HONORS` | failed fast with a proxy/connection error | routes through the proxy — **viable** |
| `IGNORES` | **succeeded** despite a dead proxy | bypassed it entirely — **not viable** |
| `INCONCLUSIVE` | hung, or failed for an unrelated reason | needs step 2; this is what grok returns |

A *successful* call is the bad outcome — it proves the CLI dialled the provider directly.
Always compare against a control run of the same command with no proxy variables set, or
a slow network reads as a hang.

**Step 2 — the real lease.** Do not flip the global config. Point the lease at a scratch
config with `OMNIAGENTOS_LEASE_CONFIG` and run exactly one fixture, so a hang costs one
fixture's timeout rather than a whole capture.

```bash
# 1. Copy the real config and switch only the egress policy.
cp configs/lease.yaml /tmp/lease-proxy.yaml
#    then edit /tmp/lease-proxy.yaml:
#       autonomy_lease.mode: enforce
#       autonomy_lease.net.default_policy: proxy
#    (leave allow_ports/allow_domains alone — the provider host must already be listed)

# 2. Run ONE fixture under it.
OMNIAGENTOS_LEASE_CONFIG=/tmp/lease-proxy.yaml \
OMNIAGENTOS_AUTONOMY_LEASE=enforce \
  uv run python -m scripts.benchmarks.capture_baseline \
    --arm <arm> --replicates 1 \
    --only fx_001_greenfield_palindrome \
    --label proxy-compat-<cli>

# 3. Read the verdict off two places, not one:
#    - the capture line: PASS in normal wall time = honours the proxy;
#      a timeout at the lease TTL = ignores it.
#    - the ledger: the lease record must show net_policy proxy and enforced: true,
#      confirming the mode was actually applied and not silently downgraded.
jq -r 'select(.mode=="enforce") | [.event, (.net_policy|tostring), .enforced] | @tsv' \
  ledger/leases-$(date -u +%Y%m).jsonl | tail -5
```

A CLI that times out here is **not** a lease bug — it is a missing proxy-env capability in
that vendor's CLI. Record it in the table above and leave that lane on net `open` until
the vendor ships proxy support.

---

## 8. Where the runtime data lives

None of it is committed; all of it is under gitignored `var/` or `ledger/`.

| Data | Path |
| --- | --- |
| Benchmark records (raw) | `var/benchmarks/results.jsonl` |
| Benchmark store (queryable) | `var/benchmarks/baseline.db` |
| Benchmark workspaces | `var/benchmarks/workspaces/<capture_id>/` |
| Control-plane DB | `$OMNIAGENTOS_DB` or `var/omniagentos.db` |
| Ledger (leases + observations) | `$OMNIAGENTOS_LEDGER_DIR` or `ledger/` |
