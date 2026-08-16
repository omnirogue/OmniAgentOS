-- Dimensional revenue / ad-spend facts (vertical + source + campaign).
--
-- metric_snapshots holds ONE scalar per (goal, source, metric, day): it cannot
-- answer "AcmeUni vs Initech" or "which campaign". This additive table is the
-- dimensional store the P&L dashboard reads from. It never replaces
-- metric_snapshots; the hourly goal collector keeps writing there unchanged.
--
-- One row = one (ET calendar day, vertical/brand, source, campaign) fact. Stripe
-- rows carry campaign_id = NULL (brand-level revenue); Meta rows carry a
-- campaign_id/name (per-campaign spend + attribution). revenue_usd is REAL
-- COLLECTED money (Stripe only); Meta attribution is NOT summed into it — it
-- lives in meta_json.revenue_attributed_usd — so totals never conflate real
-- revenue with platform-attributed estimates.
CREATE TABLE revenue_facts (
    day              TEXT NOT NULL,          -- ET calendar day, YYYY-MM-DD
    vertical         TEXT NOT NULL,          -- brand: 'AcmeUni' | 'Initech' | ...
    source           TEXT NOT NULL,          -- 'stripe' | 'meta' | ...
    campaign_id      TEXT,                   -- NULL for brand-level (Stripe) rows
    campaign_name    TEXT,
    revenue_usd      REAL DEFAULT 0,         -- real collected revenue (net of refunds)
    ad_spend_usd     REAL DEFAULT 0,
    roas             REAL,                   -- NULL when not applicable / unknown
    payment_failures INTEGER DEFAULT 0,
    refunds_usd      REAL DEFAULT 0,
    currency         TEXT DEFAULT 'usd',
    captured_at      TEXT,
    meta_json        TEXT DEFAULT '{}',
    UNIQUE(day, vertical, source, campaign_id)
);

-- The table-level UNIQUE above does NOT constrain rows whose campaign_id is NULL,
-- because SQLite treats every NULL as distinct — two brand-level Stripe rows for
-- the same day would both be admitted, defeating the hourly re-collect UPSERT.
-- This COALESCE index closes that gap: it makes (day, vertical, source) unique for
-- the NULL-campaign (brand-level) rows too, so an hourly re-collect refreshes the
-- one row in place as late refunds/settlements land.
CREATE UNIQUE INDEX idx_revenue_facts_key
    ON revenue_facts(day, vertical, source, COALESCE(campaign_id, ''));

CREATE INDEX idx_revenue_facts_day ON revenue_facts(day);
