-- Alert-once state for reliability events (fixes the critical-alert storm).
--
-- audit._critical_alerts fired one notification for EVERY open critical event on
-- EVERY watch cycle (StartInterval 600 s) with dedupe disabled. Recovery only
-- allow-lists rate_limit/timeout, so a session_error / account_disabled event
-- never leaves 'open' -- 43 stale events from one debugging session produced
-- 246 notifications/hour indefinitely (3,838 rows by 2026-07-24 before this).
--
-- alerted_at records when an event was last alerted, so the watch alerts ONCE
-- per event (plus an optional re-alert cadence, OMNIAGENTOS_RELIABILITY_REALERT_HOURS).
-- Ongoing open-event volume is already reported by the twice-daily grouped digest.
ALTER TABLE reliability_events ADD COLUMN alerted_at TEXT;

CREATE INDEX idx_relev_alerted ON reliability_events(severity, status, alerted_at);

-- Backfill: every currently-open critical event has already been alerted (many
-- hundreds of times). Stamp them so the storm stops the moment this lands,
-- rather than firing one final round per event. No-op on a fresh database.
UPDATE reliability_events
   SET alerted_at = detected_at
 WHERE severity = 'critical'
   AND status = 'open'
   AND alerted_at IS NULL;
