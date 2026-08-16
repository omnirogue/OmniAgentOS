# F5 — GitHub comms adapter + correlation

## Built
- `omniagentos/comms/normalize.py` — `normalize_github` / `process_github_event` (frozen message shape preserved)
- `omniagentos/comms/correlation.py` — repo/PR/commit/check → session attribution precedence
- `omniagentos/comms/github_mode.py` — `OMNIAGENTOS_GITHUB_COMMS_MODE` (default off)
- Fixtures under `tests/comms/fixtures/github/`

## Attribution precedence
1. Embedded `ses_` / trailer markers
2. Swarm/run markers
3. Else unattributed (never guess)

## owned_paths
- omniagentos/comms/normalize.py
- omniagentos/comms/correlation.py
- omniagentos/comms/github_mode.py
- tests/comms/test_github_adapter.py
- tests/comms/fixtures/github/
- docs/workbooks/team-f/F5-github-comms.md

## est_minutes
45
## depends_on
[F4]
## verify_command
`uv run pytest -q tests/comms/test_github_adapter.py && uv run ruff check omniagentos/comms/normalize.py omniagentos/comms/correlation.py omniagentos/comms/github_mode.py tests/comms/test_github_adapter.py && uv run mypy omniagentos/comms/normalize.py omniagentos/comms/correlation.py omniagentos/comms/github_mode.py`
