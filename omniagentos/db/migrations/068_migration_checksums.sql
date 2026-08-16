-- 068_migration_checksums.sql
-- H-09 / M-06: record SHA-256 checksums of applied migration files so
-- post-application edits are detectable.
--
-- Append-only: this migration ADDS bookkeeping; it must never rewrite
-- already-applied migration SQL (migration 064 stays frozen as applied).
-- Existing operator DBs that applied an earlier 064 text keep their schema;
-- only future drift against the recorded checksum fails closed.

ALTER TABLE schema_migrations ADD COLUMN checksum TEXT;
