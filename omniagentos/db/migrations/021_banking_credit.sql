-- Adds the CREDIT sub-ledger to bank_facts: available credit (the
-- spendable reserve) and the current card balance owed.
--
-- Context: a typical Slash setup sweeps incoming cash to auto-pay a charge
-- card daily, so the `cash` sub-ledger balance is genuinely $0.00 on a normal
-- day while the spendable reserve is the `credit` sub-ledger's `available`
-- figure. GET /account/{id}/balance already
-- returns both sub-ledgers (see banking/collect.py); this migration is additive
-- storage for the credit side so the report/dashboard can surface it honestly
-- instead of only showing a misleading $0.00 cash balance.
--
-- Purely additive: two new NULLable columns on the existing bank_facts table.
-- NULL (not 0) means "not captured for this row" (e.g. an account with no
-- credit sub-ledger, or a collection that predates this migration) so a report
-- summing these never confuses "no credit line" with "zero credit available".
--
-- Numbered 021 to follow 020_banking.sql.

ALTER TABLE bank_facts ADD COLUMN available_credit_usd REAL;  -- credit sub-ledger `available.amountCents`
ALTER TABLE bank_facts ADD COLUMN card_balance_usd REAL;      -- credit sub-ledger `posted.amountCents` (owed)
