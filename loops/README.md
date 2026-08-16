# `loops/` — the LangGraph loop runtime (Phase 1)

Option G thin hybrid, per `devtasks/langgraph-comparison/MIGRATION_ARCHITECTURE.md`.
**Additive, isolated, reversible.** No existing lane is migrated; LangGraph enters only as
the runtime for *new* loop workers, behind seams the control plane already owns.

Division of labour, in one sentence: **LangGraph owns loop-body durability; the control
plane owns everything that can hurt you.** Policy, approvals, events, spend and
exactly-once external effects all stay where they already are.

## Install (once)

```sh
uv venv --python 3.12 var/loops/venv
uv pip install --python var/loops/venv/bin/python -r loops/requirements.txt
```

`pyproject.toml` and `uv.lock` are untouched — the production venv has no LangGraph and
never will unless that is separately approved. The loops venv does **not** install
`omniagentos`: the worker puts its own checkout root on `sys.path`, so the bridge runs the
same control-plane code the API and runner do.

Rollback: `launchctl bootout` the loop jobs, disable the routine rows, `rm -rf var/loops`.
Business effects remain visible in `approvals` / `events` / `idempotency` as always.

## Run

```sh
loops/bin/loop-worker --template draft_approve_send --instance cs_replies \
    --instance-module omniagentos_loops.instances.cs_replies --params '{}'
loops/bin/loop-tests                # the suite (loops venv only)
loops/bin/loop-tests --counterfeits  # + the counterfeit corpus
```

One invocation == one **tick** of one instance, in one short-lived process. Ticks are
fired by the existing routines scheduler (below); nothing here is resident and nothing
here opens a port.

## Layout

| path | what it owns |
|---|---|
| `omniagentos_loops/paths.py` | `var/loops/` layout, safe-name rules |
| `omniagentos_loops/contracts.py` | `RiskTier`, `LoopState`, statuses, error classes |
| `omniagentos_loops/tools.py` | `LoopTool` + `execute_effect` — **the one execution seam** |
| `omniagentos_loops/policy_gate.py` | calls `omniagentos.policy.evaluate_action`; T2+ floor |
| `omniagentos_loops/approvals.py` | `interrupt` ⇄ `approvals` row, expiry, Slack deep link |
| `omniagentos_loops/receipts.py` | the `idempotency` table's first real producer (P7) |
| `omniagentos_loops/observability.py` | node transitions → `events` rows (SSE for free) |
| `omniagentos_loops/models.py` | model calls, only via `llm/` short-call ($10/day cap) |
| `omniagentos_loops/retry.py` | `RetryPolicy` + the transient-fault allowlist (P5) |
| `omniagentos_loops/registry.py` | routine-row shape for `kind='loop'` |
| `omniagentos_loops/runtime.py` | `LoopContext`, checkpointing, the tick driver |
| `omniagentos_loops/templates/` | the five reusable graph shapes |
| `omniagentos_loops/instances/` | per-workflow tool registrations (W1..W5 land here) |

## The five rules a loop obeys

1. **Every effect passes `policy_gate` first.** `add_effect()` adds the gate node and the
   effect node together, and the only edge into an effect comes from its gate.
2. **T2 and above always park for a human.** Not configurable. A loop is unattended, so it
   takes a stricter floor than an interactive session gets from AUTO mode.
3. **The durable row is the authority, not the graph.** `execute_effect` re-reads the
   `approvals` row itself, so a missing/forged/expired/bot-decided token executes nothing.
4. **Expiry aborts.** A request nobody answered is never an approval.
5. **Every effect is receipted.** Claim before acting, complete after; a
   claimed-but-uncompleted receipt fails closed rather than risking a duplicate.

## Registering a loop (what W2/W3 need)

A registered workflow is a **routine row**, not a new registry:

```python
from omniagentos_loops.registry import loop_routine_row

row = loop_routine_row(
    name="loop-inbox-triage",
    template="poll_classify_act_verify",
    instance_id="w2_inbox_triage",
    instance_module="omniagentos_loops.instances.w2_inbox_triage",  # REQUIRED
    cron="*/15 * * * *",
    params={"mailbox": "support"},
)
# -> RoutinesStore.create_routine(row); enable/disable, revision CAS, acceptance
#    floor and last_fired all already work.
```

`instance_module` is **required and never derived**. It is the module whose
`register(ctx)` supplies this instance's tools, it reaches the worker as
`--instance-module`, and the runtime ships no tools of its own — so a row without
it fails every tick on `instance is missing required tools: [...]`. It is not
inferred from `instance_id` because the two differ in practice (instance
`w3_health_monitor` registers from `...instances.health_monitor`; instance
`w2_inbox` from `...instances.w2_inbox_triage`), and one module may serve several
instances. Both `loop_routine_row` and `loop_spec` refuse anything that is not a
valid module path under `omniagentos_loops.instances.` — the same prefix rule the
worker enforces before `importlib.import_module`, because a routine row is data.

The row's `task_template.input.module` is `omniagentos.loops`, which
`omniagentos/scheduler/loop_jobs.py` (the one production hook) turns into a
`loops/bin/loop-worker` subprocess on the next due tick.

Then add `omniagentos_loops/instances/<instance>.py`:

```python
def register(ctx) -> None:
    ctx.tools.register(LoopTool(
        name="poll", tier=RiskTier.T0,
        idempotency_key=lambda args: "poll",
        call=read_unseen_messages,                # existing callable
    ))
    ctx.tools.register(LoopTool(
        name="act", tier=RiskTier.T2,             # -> always parks
        idempotency_key=lambda args: args["item"]["id"],
        call=send_reply,                          # broker / notify.py
        description="reply to a customer message",
    ))
```

Tool rules: the idempotency key is the **business identity** of the effect (message id,
recipient + content digest, incident id) — never a timestamp, never a random id, never the
tick number. Secrets are resolved by the callable through
`omniagentos.connectors.broker`; nothing in this package reads a credential from the
environment, and a test enforces that.

## Templates

| template | shape | required tools |
|---|---|---|
| `poll_classify_act_verify` | one item per tick | `poll`, `classify`, `act`, `verify` |
| `monitor_diagnose_repair_verify` | allowlisted auto-remedy, else escalate | `monitor`, `diagnose`, `repair`, `escalate`, `verify` |
| `draft_approve_send` | every send parks | `draft`, `send` |
| `generate_evaluate_improve` | bounded by `max_rounds` | `generate`, `evaluate`, `publish` |
| `dispatch_await_summarize` | wraps the swarm, awaits across ticks | `dispatch`, `poll_card`, `summarize` |

## Operator surface

* **Approvals inbox** — loop approvals are ordinary rows with `risk='loop_approval'`;
  `params_json` carries instance/template/node/tool/tier/business key.
* **Events / SSE** — `loop.node`, `loop.effect`, `loop.approval`, `loop.model_call`,
  `loop.status` ride the existing `events` table and `GET /api/events`.
* **Checkpoints** — `var/loops/<family>.ckpt.sqlite3`, one per family, `durability="sync"`.
  Deleting one loses in-flight loop position only; business effects are receipted.
