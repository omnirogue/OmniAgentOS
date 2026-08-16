-- Covering-order index for SessionsDal.list_sessions
-- (omniagentos/sessions/dal.py): SELECT <cols> FROM sessions
-- [WHERE state = ?] ORDER BY created_at DESC, id DESC LIMIT ?. The sessions
-- table had indexes on state, source and (account_id, state) but nothing on
-- created_at, so every list -- the board poll, the SSE session.updated tick,
-- and the supervisor's limit=10_000 reconcilers -- did a full SCAN plus a
-- TEMP B-TREE sort of the entire table just to return the newest N rows.
-- The (created_at DESC, id DESC) index matches the ORDER BY exactly, so
-- SQLite walks the index and stops after LIMIT rows: no scan, no sort.
CREATE INDEX IF NOT EXISTS idx_sessions_created ON sessions(created_at DESC, id DESC);
