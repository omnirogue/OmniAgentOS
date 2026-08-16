# PLAN2-TASK — C2 Org-model consolidation (branch plan2/c2-org-consolidation)
Mission: two org models -> one. Fold omniagentos/company/ into orgdims; /organization UI redirects to /orgdims.
OWN: omniagentos/company/** (absorb/deprecate), omniagentos/orgdims/** (receive — but NEVER change the signature or behavior of OrgDimsService.classify_board_task: a parallel lane calls it), the org/company API routes (find with rg "company" omniagentos/api/routes/), dashboard/src/app/organization/** (redirect page), dashboard/src/features/organization/** if present, tests for the moved logic.
Method: move functions/queries into orgdims with delegating shims left in company/ (log-on-use deprecation), route /organization -> /orgdims redirect, tombstone comment on company tables (NO table drops, NO migration).
Acceptance: uv run pytest -q tests/orgdims (and any tests/company) green; npm run build clean; hitting the old /organization page lands on /orgdims.
