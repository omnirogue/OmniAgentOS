-- Migration 099: employee transcript uploads (Migration C / JG3-BE).
--
-- RE-DERIVED SQL. The FINAL-plan text for Migration C was lost; this file is
-- re-authored from the surviving JG3 constraint set. Binding contract:
-- tests/db/test_migration_transcript_uploads.py.
--
-- Migration execution is serialized by omniagentos.db.migrate and every
-- application connection enables PRAGMA foreign_keys=ON. This migration is
-- idempotent and append-only: once applied, change schema with a new migration.

CREATE TABLE IF NOT EXISTS transcript_uploads (
    id           TEXT PRIMARY KEY,                 -- tu_...
    employee_id  TEXT NOT NULL REFERENCES employees(id),
    filename     TEXT NOT NULL,                    -- metadata only; never a disk path
    content_hash TEXT NOT NULL,                    -- SHA-256 hex
    size_bytes   INTEGER NOT NULL,
    storage_path TEXT NOT NULL,                    -- contained path relative to root
    source       TEXT NOT NULL,                    -- manual|chat|dev_session|slack (app-side)
    status       TEXT NOT NULL DEFAULT 'uploaded', -- uploaded|analyzed|error (app-side)
    uploaded_at  TEXT NOT NULL,
    analyzed_at  TEXT NULL,
    meta_json    TEXT NULL
);

CREATE INDEX IF NOT EXISTS idx_transcript_uploads_employee
    ON transcript_uploads(employee_id);
CREATE INDEX IF NOT EXISTS idx_transcript_uploads_status
    ON transcript_uploads(status);
