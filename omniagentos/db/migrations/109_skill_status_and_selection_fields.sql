-- U-16 corpus hygiene: one status vocabulary + real selection metadata.
--
-- Lane: lane/cap-skills (U-16). Ordinal 109 claimed by this lane; main was at
-- 108 when it was written.
--
-- TWO defects are closed here, both of which made the skill selector lie.
--
-- 1. STATUS VOCABULARY SPLIT BRAIN.
--    `skills.status` permitted only ('active','archived') while
--    omniagentos/skills/select.py allowlisted approved/canary/deprecated/
--    experimental. Half the selector's status logic could never fire, and
--    there was no representable state for "this row is machine-quarantined
--    evidence, not a usable skill". The DB vocabulary wins (it is the only
--    side with a producer) and is widened to the set the selector actually
--    needs:
--        active        selectable
--        deprecated    selectable at half score (authorable via a vault note's
--                      `status:` frontmatter or PUT /api/skills)
--        experimental  selectable at half score (same producers)
--        archived      operator retired it -- never selected
--        quarantined   machine judgement: this row is retained as evidence and
--                      is never selected, never a filler, never inlined
--    approved/canary are DROPPED from the selector in the same diff: nothing
--    can produce them, so they were dead branches.
--
-- 2. UNPOPULATABLE SELECTION SIGNALS.
--    list_skills() hardcoded `risk_classes: []` and `artifacts: []` because
--    the schema had nowhere to put them, and packed the whole preferred_method
--    prose sentence into `tools`. Three of select_skills()'s four scoring
--    signals therefore could not fire, and the fourth (tool overlap) compared
--    an allowed-tool name against an English sentence. The three JSON columns
--    added here give those signals a real, declarable source: a vault note's
--    `<!-- skill-library -->` block may declare `tools:`, `artifacts:` and
--    `risk_classes:` lists, and list_skills() reads them verbatim.
--
-- The quarantine of the 34 auto-captured mock-run rows happens at the bottom.
-- It is REVERSIBLE by design: nothing is deleted, the prior status is implied
-- (every matching row is 'active'), and `quarantine_reason`/`quarantined_at`
-- record the machine judgement so the curator post-mortem still has its
-- evidence.
--
-- SQLite cannot widen an inline column CHECK in place, so this is the standard
-- table rebuild. skill_versions, update_proposals and execution_evidence all
-- carry ON DELETE CASCADE foreign keys into skills, so `DROP TABLE skills`
-- with foreign_keys=ON (which db/store.py sets) would fire an implicit DELETE
-- and CASCADE every version row out of existence. The children are therefore
-- rebuilt alongside the parent and dropped in reverse-dependency order, the
-- same shape migration 089 used for sessions/session_messages. Column lists
-- are spelled out explicitly on both sides of every INSERT ... SELECT.

CREATE TABLE skills_109 (
    id               TEXT PRIMARY KEY,
    slug             TEXT NOT NULL UNIQUE,
    category         TEXT NOT NULL,
    subcategory      TEXT NOT NULL,
    title            TEXT NOT NULL,
    summary          TEXT NOT NULL DEFAULT '',
    preferred_method TEXT,
    fallback_method  TEXT,
    vault_note_path  TEXT,
    status           TEXT NOT NULL DEFAULT 'active'
                     CHECK (status IN (
                         'active', 'archived', 'quarantined',
                         'deprecated', 'experimental'
                     )),
    current_version  INTEGER NOT NULL DEFAULT 1,
    created_at       TIMESTAMP NOT NULL,
    updated_at       TIMESTAMP NOT NULL,
    -- Declared selection metadata (JSON arrays of strings). '[]' is an honest
    -- "this skill declares none", not a placeholder for "never populated".
    tools_json       TEXT NOT NULL DEFAULT '[]',
    artifacts_json   TEXT NOT NULL DEFAULT '[]',
    risk_classes_json TEXT NOT NULL DEFAULT '[]',
    -- Why a row is quarantined, and when. NULL for every non-quarantined row.
    quarantine_reason TEXT,
    quarantined_at    TIMESTAMP
);

INSERT INTO skills_109
    (id, slug, category, subcategory, title, summary, preferred_method,
     fallback_method, vault_note_path, status, current_version,
     created_at, updated_at,
     tools_json, artifacts_json, risk_classes_json,
     quarantine_reason, quarantined_at)
SELECT
    id, slug, category, subcategory, title, summary, preferred_method,
    fallback_method, vault_note_path, status, current_version,
    created_at, updated_at,
    '[]', '[]', '[]',
    NULL, NULL
FROM skills;

CREATE TABLE skill_versions_109 (
    id               TEXT PRIMARY KEY,
    skill_id         TEXT NOT NULL REFERENCES skills_109(id) ON DELETE CASCADE,
    version          INTEGER NOT NULL,
    content_snapshot TEXT NOT NULL,
    preferred_method TEXT,
    fallback_method  TEXT,
    change_reason    TEXT NOT NULL,
    evidence_json    TEXT NOT NULL DEFAULT '{}',
    author           TEXT NOT NULL,
    status           TEXT NOT NULL CHECK (status IN ('active', 'superseded')),
    created_at       TIMESTAMP NOT NULL,
    UNIQUE (skill_id, version)
);

INSERT INTO skill_versions_109
    (id, skill_id, version, content_snapshot, preferred_method, fallback_method,
     change_reason, evidence_json, author, status, created_at)
SELECT
    id, skill_id, version, content_snapshot, preferred_method, fallback_method,
    change_reason, evidence_json, author, status, created_at
FROM skill_versions;

CREATE TABLE update_proposals_109 (
    id                       TEXT PRIMARY KEY,
    skill_id                 TEXT NOT NULL REFERENCES skills_109(id) ON DELETE CASCADE,
    proposed_diff            TEXT,
    proposed_content         TEXT,
    evidence_files_json      TEXT NOT NULL DEFAULT '[]',
    linked_execution_id      TEXT,
    risk                     TEXT NOT NULL CHECK (risk IN ('low', 'model', 'major')),
    state                    TEXT NOT NULL CHECK (state IN ('pending', 'approved', 'rejected')),
    created_by               TEXT NOT NULL,
    created_at               TIMESTAMP NOT NULL,
    decided_by               TEXT,
    decided_at               TIMESTAMP,
    decision_note            TEXT
);

INSERT INTO update_proposals_109
    (id, skill_id, proposed_diff, proposed_content, evidence_files_json,
     linked_execution_id, risk, state, created_by, created_at,
     decided_by, decided_at, decision_note)
SELECT
    id, skill_id, proposed_diff, proposed_content, evidence_files_json,
    linked_execution_id, risk, state, created_by, created_at,
    decided_by, decided_at, decision_note
FROM update_proposals;

CREATE TABLE execution_evidence_109 (
    id                TEXT PRIMARY KEY,
    proposal_id       TEXT REFERENCES update_proposals_109(id) ON DELETE SET NULL,
    skill_id          TEXT NOT NULL REFERENCES skills_109(id) ON DELETE CASCADE,
    run_id            TEXT,
    skill_version     INTEGER,
    validation_status TEXT,
    metrics_json      TEXT NOT NULL DEFAULT '{}',
    linked_episode_id TEXT,
    created_at        TIMESTAMP NOT NULL
);

INSERT INTO execution_evidence_109
    (id, proposal_id, skill_id, run_id, skill_version, validation_status,
     metrics_json, linked_episode_id, created_at)
SELECT
    id, proposal_id, skill_id, run_id, skill_version, validation_status,
    metrics_json, linked_episode_id, created_at
FROM execution_evidence;

-- Reverse dependency order: execution_evidence has no inbound FK, then
-- update_proposals (its only referrer is already gone), then skill_versions,
-- then skills. Every child of the *new* tables points at the *_109 names, so
-- no cascade can reach the copied rows.
DROP TABLE execution_evidence;
DROP TABLE update_proposals;
DROP TABLE skill_versions;
DROP TABLE skills;

ALTER TABLE skills_109 RENAME TO skills;
ALTER TABLE skill_versions_109 RENAME TO skill_versions;
ALTER TABLE update_proposals_109 RENAME TO update_proposals;
ALTER TABLE execution_evidence_109 RENAME TO execution_evidence;

CREATE INDEX IF NOT EXISTS idx_skills_tree
    ON skills(category, subcategory, title);
CREATE INDEX IF NOT EXISTS idx_skill_versions_skill
    ON skill_versions(skill_id, version DESC);
CREATE INDEX IF NOT EXISTS idx_update_proposals_state
    ON update_proposals(state, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_update_proposals_skill
    ON update_proposals(skill_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_execution_evidence_skill
    ON execution_evidence(skill_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_execution_evidence_proposal
    ON execution_evidence(proposal_id);
CREATE INDEX IF NOT EXISTS idx_skills_status
    ON skills(status);

-- One-time quarantine of the auto-captured corpus.
--
-- The predicate is the curator's own two tells, read off the stored body:
--   * "No step-level detail was recorded in the ledger manifest" is the literal
--     string omniagentos/selfimprove/curator.py substitutes when a manifest
--     yields ZERO step names -- i.e. the note records no reusable procedure at
--     all, which is the whole point of a skill.
--   * "harness=mock" is a run that never touched a real agent, so it is not
--     evidence of anything reusable.
-- On the live corpus these two select exactly the 34 auto-captures and none of
-- the 7 authored/seeded skills. Only 'active' rows are touched, so an operator
-- decision (archived) is never overwritten.
UPDATE skills
   SET status = 'quarantined',
       quarantine_reason = 'auto-capture:no-step-evidence-or-mock-harness',
       quarantined_at = CURRENT_TIMESTAMP,
       updated_at = CURRENT_TIMESTAMP
 WHERE status = 'active'
   AND id IN (
       SELECT s.id
         FROM skills s
         JOIN skill_versions v
           ON v.skill_id = s.id AND v.version = s.current_version
        WHERE v.content_snapshot LIKE
              '%No step-level detail was recorded in the ledger manifest%'
           OR v.content_snapshot LIKE '%harness=mock%'
   );
