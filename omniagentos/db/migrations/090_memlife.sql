-- 090_memlife.sql
-- Memory-lifecycle review queue: candidates awaiting human judgement,
-- append-only decision history, and graduated lessons.
--
-- Status / action enums match omniagentos.memlife.contracts
-- (CandidateStatus, DecisionAction, LessonStatus). Salience is nullable:
-- NULL means not computable, never a flattering 0.0 stand-in.

CREATE TABLE IF NOT EXISTS memlife_candidates (
    id TEXT PRIMARY KEY,
    key TEXT NOT NULL,
    claim TEXT NOT NULL,
    conditions TEXT NOT NULL DEFAULT '',
    evidence_ids_json TEXT NOT NULL DEFAULT '[]',
    cluster_size INTEGER NOT NULL CHECK (cluster_size >= 1),
    status TEXT NOT NULL CHECK (status IN (
        'staged', 'graduated', 'rejected', 'reopened', 'quarantined'
    )),
    rejection_count INTEGER NOT NULL DEFAULT 0 CHECK (rejection_count >= 0),
    salience REAL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memlife_candidates_status
    ON memlife_candidates(status);
CREATE INDEX IF NOT EXISTS idx_memlife_candidates_key
    ON memlife_candidates(key);

CREATE TABLE IF NOT EXISTS memlife_decisions (
    id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL REFERENCES memlife_candidates(id) ON DELETE CASCADE,
    action TEXT NOT NULL CHECK (action IN (
        'stage', 'graduate', 'reject', 'reopen', 'quarantine'
    )),
    at TEXT NOT NULL,
    actor TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_memlife_decisions_candidate
    ON memlife_decisions(candidate_id, at);

CREATE TABLE IF NOT EXISTS memlife_lessons (
    id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL REFERENCES memlife_candidates(id),
    claim TEXT NOT NULL,
    conditions TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK (status IN (
        'provisional', 'accepted', 'retracted'
    )),
    graduated_at TEXT NOT NULL,
    graduated_by TEXT NOT NULL,
    evidence_ids_json TEXT NOT NULL DEFAULT '[]',
    provenance_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memlife_lessons_candidate
    ON memlife_lessons(candidate_id);
CREATE INDEX IF NOT EXISTS idx_memlife_lessons_status
    ON memlife_lessons(status);

-- Append-only decision log: history is how we detect a reported graduation
-- that never wrote the lesson. Mutations after insert are refused.
CREATE TRIGGER IF NOT EXISTS memlife_decisions_no_update
BEFORE UPDATE ON memlife_decisions
BEGIN
    SELECT RAISE(ABORT, 'memlife_decisions is append-only');
END;

CREATE TRIGGER IF NOT EXISTS memlife_decisions_no_delete
BEFORE DELETE ON memlife_decisions
BEGIN
    SELECT RAISE(ABORT, 'memlife_decisions is append-only');
END;
