-- Cash / banking facts: bank BALANCES, MONEY DEPOSITED, and TRUE EXPENSES
-- (actual debits out of the bank accounts) as a cash-flow complement to the
-- revenue / P&L store (revenue_facts, migration 009).
--
-- This is REAL money. The load-bearing honesty rule of this schema: `expenses_usd`
-- is actual cash OUT of a bank account (debits), NOT accrued cost, COGS, or ad
-- spend — those live in revenue_facts and are a different question. `balance_usd`
-- is a latest-available snapshot captured with the day's collection, not a
-- reconstructed end-of-day close.
--
-- Numbered 020 (not 010-015) to avoid colliding with a parallel deploy branch.

-- One row per bank account we can see, across every provider. Identity is the
-- provider's own account id (namespaced by provider in the collector), so an
-- account re-appears in place across collections rather than duplicating.
CREATE TABLE bank_accounts (
    id          TEXT PRIMARY KEY,          -- '<provider>:<external account id>'
    provider    TEXT NOT NULL,             -- 'slash' | 'teller' | ...
    brand       TEXT,                      -- owning brand/label: 'AcmeUni' | 'Initech' | 'primary' | ...
    name        TEXT,                      -- human account name from the provider
    mask        TEXT,                      -- last 4 (never the full number)
    currency    TEXT DEFAULT 'usd',
    kind        TEXT,                      -- 'checking' | 'savings' | 'card' | ...
    created_at  TEXT
);

-- One row = one (ET calendar day, account) cash fact. UPSERTed hourly: as late
-- transactions post and balances move, the day's row is refreshed in place.
--   balance_usd  : latest-available balance snapshot at captured_at (a card
--                  account's balance is its outstanding balance; meta.balance_kind
--                  records which so the sum is never silently mixed).
--   deposits_usd : money IN — credits that posted within the ET day (>= 0).
--   expenses_usd : money OUT — TRUE cash expenses, i.e. debits that posted within
--                  the ET day, reported as a positive magnitude (>= 0).
--   net_flow_usd : deposits_usd - expenses_usd (may be negative).
CREATE TABLE bank_facts (
    day          TEXT NOT NULL,            -- ET calendar day, YYYY-MM-DD
    account_id   TEXT NOT NULL,
    provider     TEXT NOT NULL,
    balance_usd  REAL DEFAULT 0,
    deposits_usd REAL DEFAULT 0,
    expenses_usd REAL DEFAULT 0,
    net_flow_usd REAL DEFAULT 0,
    txn_count    INTEGER DEFAULT 0,
    captured_at  TEXT,
    meta_json    TEXT DEFAULT '{}',
    UNIQUE(day, account_id)
);

CREATE INDEX idx_bank_facts_day ON bank_facts(day);

-- Individual transactions, kept for the "largest transactions" detail table.
-- amount_usd is SIGNED: + for a deposit (credit), − for an expense (debit), so a
-- single ORDER BY ABS(amount_usd) surfaces the biggest movements either way.
-- external_id is the provider's stable id (synthesized deterministically when the
-- provider gives none) so re-collection UPSERTs a transaction rather than
-- double-counting it.
CREATE TABLE bank_transactions (
    day         TEXT NOT NULL,             -- ET calendar day the txn posted, YYYY-MM-DD
    account_id  TEXT NOT NULL,
    posted_at   TEXT,                      -- provider-reported post timestamp
    amount_usd  REAL NOT NULL,             -- signed: + deposit / − expense
    description TEXT,
    category    TEXT,
    external_id TEXT NOT NULL UNIQUE
);

CREATE INDEX idx_bank_transactions_day ON bank_transactions(day);
