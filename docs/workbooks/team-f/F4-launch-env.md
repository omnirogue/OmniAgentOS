# F4 — Launch env include + three-DB map

## Built
- `scripts/launch-env.sh` — idempotent sourceable include; never clobbers preset `OMNIAGENTOS_DB`
- Sourced by: `scripts/launch-supervised.sh`, `scripts/certify-omniagentos.sh`, `scripts/test-comprehensive.sh`, `Makefile` api/runner
- `docs/operations/three-db-map.md` — control-plane SQLite / knowledge Postgres / lab SQLite

## Entry point enumeration
| Entry | Sources launch-env? |
| --- | --- |
| scripts/launch-supervised.sh | yes |
| scripts/certify-omniagentos.sh | yes |
| scripts/test-comprehensive.sh | yes |
| Makefile api/runner | yes |
| scripts/launch-omniagentos.sh | no (separate-product launcher at ~/OmniAgentOS path; out of scope) |

## archdocs update needed
Yes — launch-env + three-DB map should be noted in architecture ops surface via archdocs update API.

## owned_paths
- scripts/launch-env.sh
- scripts/launch-supervised.sh
- scripts/certify-omniagentos.sh
- scripts/test-comprehensive.sh
- Makefile
- docs/operations/three-db-map.md
- tests/scripts/test_launch_env.py
- docs/workbooks/team-f/F4-launch-env.md

## est_minutes
40
## depends_on
[F3]
## verify_command
`uv run pytest -q tests/scripts/test_launch_env.py && test -f scripts/launch-env.sh && test -f docs/operations/three-db-map.md && bash -n scripts/launch-env.sh && uv run ruff check tests/scripts/test_launch_env.py`
