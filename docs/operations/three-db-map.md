# Three-database map

Physical consolidation is **out of scope**. This document maps the three
logical databases an operator will encounter when launching OmniAgentOS so
tools resolve the correct file.

Canonical env resolution for the control-plane DB is
`scripts/launch-env.sh` (source it; never hardcode paths in new entry points).

| # | Database | Default path | Env var | Owning subsystem | Readers / writers | Default for tools? |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | **Control-plane SQLite** | `$OMNIAGENTOS_VAR_DIR/state.sqlite3` (dev fallback: `var/omniagentos.db` via `default_db_path()`) | `OMNIAGENTOS_DB` | API, runner, sessions, collab board, swarm, notifications, skills index | `make api` / `make runner`, supervisors, board sweep, session bridge | **Yes** — default for almost every tool and CLI |
| 2 | **Knowledge (Synapse) Postgres** | `postgresql://…@localhost/omniagentos_knowledge` | `OMNIAGENTOS_KNOWLEDGE_PG_DSN` (agent) / `OMNIAGENTOS_KNOWLEDGE_ADMIN_DSN` (admin) | Knowledge recall + consolidator | Runner recall hooks, curator, knowledge admin scripts | No — only when `OMNIAGENTOS_KNOWLEDGE=1` |
| 3 | **Lab / measurement SQLite** | under lab runtime / `var/` lab paths (campaign/scorecard stores) | lab-specific env (see `omniagentos/lab`) | Lab campaigns, scorecards, champion rows | Lab eval, curation loops | No — lab tooling only |

## Resolution rules

1. If `OMNIAGENTOS_DB` is already set in the environment, **keep it**.
   `scripts/launch-env.sh` never clobbers a preset.
2. Source `scripts/launch-env.sh` as the first action of shell entry points
   (Grok launchers, Makefile recipes, supervisor wrappers).
3. Never point Grok processes at `~/OmniAgentOS` — that tree belongs to the
   separate product (see `SEPARATE-PRODUCT.md`).
4. Knowledge and lab DBs are selected by their own env vars; they are not
   substitutes for `OMNIAGENTOS_DB`.

## Entry points that source `launch-env.sh`

- `scripts/launch-omniagentos.sh`
- `scripts/certify-omniagentos.sh`
- `scripts/test-comprehensive.sh`
- `Makefile` (`api`, `runner` recipes)
- `scripts/process_supervisor.py` (reads `OMNIAGENTOS_DB` after shell env is prepared)

## Out of scope

- Moving or merging data between the three databases
- Editing `~/OmniAgentOS`
- Changing `configs/accounts.yaml` or vault connection bundles
