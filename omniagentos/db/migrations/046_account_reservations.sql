-- 046_account_reservations.sql
-- WP2 (routing/limit_state): atomic spawn-slot reservations against provider accounts.
--
-- reserve_account() must make "count inflight + claim a slot" ONE atomic step
-- across processes: two slot workers each observing N < limit concurrently must
-- not both spawn against the same account. A reservation row is a short-TTL
-- (~120s, configs/swarm.yaml limits.reservation_ttl_seconds) claim on one
-- concurrent-session slot for an account. It converts to real inflight when the
-- supervisor persists sessions.account_id at launch (the reservation row is then
-- deleted), and is reclaimed by TTL if the spawn never happens (crash between
-- reserve and launch). Expired rows are ignored by every count and lazily reaped.
--
-- Additive only; no existing rows touched.

CREATE TABLE account_reservations (
    id          TEXT PRIMARY KEY,     -- new_id('rsv')
    account_id  TEXT NOT NULL,        -- claude_accounts.id
    provider    TEXT NOT NULL,        -- denormalized for provider-pressure queries
    session_id  TEXT,                 -- ses_* once known (informational)
    created_at  TEXT NOT NULL,        -- UTC ISO
    expires_at  TEXT NOT NULL         -- UTC ISO; expired rows are ignored + lazily reaped
);

CREATE INDEX idx_account_reservations_account
    ON account_reservations(account_id, expires_at);
