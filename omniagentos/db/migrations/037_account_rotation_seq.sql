-- 037_account_rotation_seq.sql
-- A monotonic rotation cursor for account selection. last_used_at is second-granular
-- (utc_now_iso), so when many sessions spawn in the SAME second -- exactly the
-- concurrent-load case multiple accounts exist to serve -- ordering by it degenerates
-- and everything picks one account. last_used_seq is bumped to MAX+1 on each pick, so
-- the just-used account always goes to the back of the queue regardless of the clock.
ALTER TABLE claude_accounts ADD COLUMN last_used_seq INTEGER;
