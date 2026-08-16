# loop plan review (historical `fable-curator` slot)

The existing 23:00 launchd label and script path are retained for compatibility,
but the old single-Fable curator has been replaced by the ordered improvement
chain configured in `configs/loop_models.yaml`:

1. Kimi 2.7 Code Highspeed reads accumulated loop suggestions and creates a
   structured draft.
2. Claude Opus 5 at `xhigh` works in a confined staging directory and directly
   edits `devtasks/LOOP-IMPROVEMENT-PLAN.md`.
3. Claude Fable 5 receives the Opus-edited plan read-only and writes the final
   approval or revision review into the plan.

The chain ingests:

- `var/improvement-log.jsonl`;
- the tail of `vault/swarm/playbook.md`;
- recent curator/reflection reports and backlog digests;
- the current actionable plan, when it already exists.

Per-run evidence, raw model products, the staged Opus plan, Fable review, and
the final installed plan are retained under `var/loop-review/<run_id>/`.

## Safety boundary

Kimi and Fable are read-only. Opus has real editing capability, but only inside
an isolated staging directory. After validation, the runner copies out exactly
one configured Markdown plan target. Models cannot use this chain to edit code,
configuration, security surfaces, or unrelated plans.

## Model controls

Edit `configs/loop_models.yaml` to change the three stages. The intended policy
is:

| Stage | Harness/model | Effort | Plan writes |
|---|---|---|---|
| Primary analyst | `cli-kimi` / `moonshot-ai/kimi-k2.7-code-highspeed` | Kimi default | no |
| Plan editor | `cli-claude` / `claude-opus-5` | `xhigh` | staged target only |
| Final reviewer | `cli-claude` / `claude-fable-5` | `high` | no |

Run manually:

```sh
sh scripts/fable-curator/fable-curator.sh
```

The rendered launchd plist already points at this compatibility entrypoint, so
an installed job uses the new chain without changing its label or schedule.
