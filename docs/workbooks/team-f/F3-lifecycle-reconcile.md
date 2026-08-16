# F3 — Lifecycle reconcile + notification hygiene

## Built
- `omniagentos/intake/run_card_reconcile.py` — completed-run unblock + dead-session flip (≤60s budget, injected clock)
- `omniagentos/intake/board_sweep.py` — invokes reconcile when flag on; preserves stale-card/approval-hang logic; return shape unchanged when flag off
- `omniagentos/notifications/cap_retention.py` — per-event cap + batched retention
- `omniagentos/notifications/service.py` — emission cap check (fail-open)
- Flag: `OMNIAGENTOS_LIFECYCLE_RECONCILE_MODE` (default off)

## Demonstrations
- Zero blocked cards after completed-run reconcile (enforce)
- Dead-session flip with age ≤ 60s under injected clock
- Cap suppression counter observable; shadow no writes

## owned_paths
- omniagentos/intake/run_card_reconcile.py
- omniagentos/intake/board_sweep.py
- omniagentos/notifications/
- tests/intake/test_run_card_reconcile.py
- tests/notifications/test_cap_retention.py
- docs/workbooks/team-f/F3-lifecycle-reconcile.md

## est_minutes
50
## depends_on
[F2]
## verify_command
`uv run pytest -q tests/intake/test_run_card_reconcile.py tests/notifications/test_cap_retention.py tests/intake/test_board_sweep.py && uv run ruff check omniagentos/intake/run_card_reconcile.py omniagentos/intake/board_sweep.py omniagentos/notifications/cap_retention.py && uv run mypy omniagentos/intake/run_card_reconcile.py omniagentos/intake/board_sweep.py omniagentos/notifications/cap_retention.py`
