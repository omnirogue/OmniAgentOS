-- Replace second-resolution updated_at lifecycle tokens with an integer
-- revision. updated_at remains display/audit metadata only.
ALTER TABLE routines ADD COLUMN revision INTEGER NOT NULL DEFAULT 0;
