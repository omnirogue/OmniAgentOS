-- 034_session_todos.sql
-- Live progress for a bridged Claude Code session.
--
-- The supervisor captures the session's own TodoWrite checklist (todos_json) and
-- the set of files it wrote/edited (files_json) straight from the stream, so the
-- board's task detail can show a real checklist + progress bar + "files involved"
-- instead of an opaque spinner. Both are additive + nullable: every existing
-- session simply has no captured todos/files yet, and SELECT * carries the new
-- columns through the session serializer with no query changes.
ALTER TABLE sessions ADD COLUMN todos_json TEXT;
ALTER TABLE sessions ADD COLUMN files_json TEXT;
