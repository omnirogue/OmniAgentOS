# PLAN2-TASK — C3 Doc diet (branch plan2/c3-doc-diet)
Full spec: Desktop plan C3. Mission: 627 markdown files -> <150 living.
OWN: docs/archive/** (destination), a classification script scripts/doc-diet/classify.py (read-only scan: living/stale/generated with reasons -> docs/consolidation/C3-DOC-INVENTORY.md), then `git mv` stale docs into docs/archive/ preserving paths. NEVER touch: ARCHI.md, ARCHI.json, AGENTS.md, ARCHITECTURE.md, DECISIONS.md, TESTING.md, CLAUDE.md, README*, docs/adr/, docs/architecture/, contracts/, HANDOFF/ (mark HANDOFF candidates in the inventory for human sign-off instead of moving).
Acceptance: inventory doc lists every md with verdict+reason; moves are git mv only; make test unaffected.
