# PLAN2-TASK — E2E product QA (branch plan2/e2e-product-qa)
Mission: browser-level e2e specs for the new product surfaces so every later merge has a walkthrough gate.
OWN: the e2e test tree only (find the existing e2e setup: Makefile `e2e` target + rg -l playwright tests/ dashboard/ — follow its conventions; if the harness lives in dashboard/, stay inside its e2e dir), plus docs/TESTING-E2E-NOTES.md.
Specs to write: (1) /chats renders the seeded folder tree (Globex, Initech, AcmeUni with nested AgentProAcademy), open a project conversation, post a message, see it render with the queued chip; (2) /board shows the category filter (5 seeded categories) and kanban columns; (3) /activity task detail renders checklist section for a known task id (mock/skip-if-empty pattern); (4) sessions view opens a transcript modal. Run the dashboard yourself with PORT=3002 npm run dev against API 127.0.0.1:8485 (auth is proxied server-side; if an e2e needs the token it reads var/secrets/sessions-token and sends X-Session-Token).
Keep specs resilient to empty data (skip-with-reason, never false-green).
Acceptance: the new specs pass locally against the live stack; `make e2e` still passes end to end.
