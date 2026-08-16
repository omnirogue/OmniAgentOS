-- 041_session_prompt.sql
-- Persist the full launch prompt for accurate respawns after auth failures.
--
-- When a session is spawned, the prompt parameter passed to spawn() contains the
-- full task instructions (goal + project scaffold + TODO items) — only the title
-- lands on the session row. On auth failure retry, we must re-launch with the full
-- prompt, not just the degraded title. Additive, nullable: existing sessions have no
-- prompt, and SELECT * carries the column through with no query changes.
ALTER TABLE sessions ADD COLUMN prompt TEXT;
