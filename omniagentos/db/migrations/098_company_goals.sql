-- Migration 098: company goals spine (Migration B / JG2-BE).
--
-- RE-DERIVED SQL. The FINAL-plan text for Migration B was lost; this file is
-- re-authored from the surviving constraint set (JG2 brief) plus the repo's own
-- idioms: 061's prefixed-id / UNIQUE-slug shape for org_companies children, and
-- 092's partial unique index for COALESCE-style uniqueness over a nullable key.
-- Binding contract: tests/db/test_migration_company_goals.py.
--
-- Append-only (M-06): once applied, this file's checksum is frozen. Never edit
-- it — ship a forward migration instead.

-- The people whose transcripts drive the pipeline. jira_account_id stays NULL
-- until an operator maps each person to a live Jira account (JG2-E13).
CREATE TABLE IF NOT EXISTS employees (
    id              TEXT PRIMARY KEY,          -- emp_...
    name            TEXT NOT NULL UNIQUE,
    role            TEXT,
    jira_account_id TEXT NULL,
    status          TEXT NOT NULL DEFAULT 'active',
    created_at      TEXT NOT NULL
);

-- Company goals form a forest: long_term roots, short_term children that MUST
-- carry a parent. `horizon` is deliberately CHECK-free — the vocabulary
-- (long_term|short_term) is validated app-side in omniagentos/company_goals,
-- matching the routines scope/purpose idiom (Migration F) so the vocabulary can
-- grow without a table rebuild.
CREATE TABLE IF NOT EXISTS company_goals (
    id              TEXT PRIMARY KEY,          -- cgl_...
    org_company_id  TEXT NOT NULL REFERENCES org_companies(id),
    title           TEXT NOT NULL,
    horizon         TEXT NOT NULL,             -- long_term | short_term (app-side)
    parent_goal_id  TEXT NULL REFERENCES company_goals(id),
    status          TEXT NOT NULL DEFAULT 'active',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_company_goals_org_company
    ON company_goals(org_company_id);

-- Goal <-> Jira mapping. A NULL jira_issue_key means the link is project-level.
CREATE TABLE IF NOT EXISTS company_goal_jira_links (
    id               TEXT PRIMARY KEY,         -- cgj_...
    goal_id          TEXT NOT NULL REFERENCES company_goals(id),
    jira_project_key TEXT NOT NULL,
    jira_issue_key   TEXT NULL,
    link_kind        TEXT NOT NULL,
    created_at       TEXT NOT NULL
);

-- COALESCE-style uniqueness, expressed as 092's pair of partial indexes rather
-- than a UNIQUE(...) over an expression: NULL issue keys never collide with each
-- other under a plain UNIQUE constraint, which is exactly the hole these close.
--
--   * at most ONE project-level link per (goal, project);
--   * at most ONE link per (goal, issue) — keyed on the ISSUE alone, not on
--     (goal, project, issue). A Jira issue key already names its project
--     ("HOO-42" belongs to HOO), so a (goal, project, issue) index would
--     happily admit both (HOO, HOO-42) and (ACM, HOO-42): the same issue
--     linked twice to the same goal under a lying project key. This index is
--     strictly stronger and subsumes the (goal, project, issue) form.
CREATE UNIQUE INDEX IF NOT EXISTS idx_company_goal_jira_links_project
    ON company_goal_jira_links(goal_id, jira_project_key)
    WHERE jira_issue_key IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_company_goal_jira_links_issue
    ON company_goal_jira_links(goal_id, jira_issue_key)
    WHERE jira_issue_key IS NOT NULL;

-- Neither partial index covers every row, so listing a goal's links needs its
-- own total index.
CREATE INDEX IF NOT EXISTS idx_company_goal_jira_links_goal
    ON company_goal_jira_links(goal_id);
