# F2 — PreToolUse experience capture (no migration)

## Built
- `omniagentos/sessions/lifecycle_capture.py` — versioned envelope, redaction, fail-open writer via ConversationStore
- `omniagentos/sessions/hook_client.py` — PreToolUse capture call (fail-open)
- `docs/schemas/lifecycle-capture-v1.md` — schema + Phase-2 consumer query notes
- Flag: `OMNIAGENTOS_SESSION_MEMORY_CAPTURE_MODE` (default off)

## No migration
Phase-2 query schema deferred. Writes go through existing `conversations` table (meta.kind=`lifecycle_capture`).

## archdocs update needed
Yes — session experience-memory capture path should be recorded via archdocs update API.

## owned_paths
- omniagentos/sessions/lifecycle_capture.py
- omniagentos/sessions/hook_client.py
- docs/schemas/lifecycle-capture-v1.md
- tests/sessions/test_lifecycle_capture.py
- docs/workbooks/team-f/F2-session-memory-capture.md

## est_minutes
55
## depends_on
[F1]
## verify_command
`uv run pytest -q tests/sessions/test_lifecycle_capture.py && test -f docs/schemas/lifecycle-capture-v1.md && uv run ruff check omniagentos/sessions/lifecycle_capture.py omniagentos/sessions/hook_client.py && uv run mypy omniagentos/sessions/lifecycle_capture.py omniagentos/sessions/hook_client.py`
