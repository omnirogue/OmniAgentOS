-- Migration 130: Executive Decision Center (EDC) substrate.
--
-- One owner-scoped Decision object plus its append-only audit trail and the
-- per-owner structured rules table, and the owner axis on the email substrate
-- (comms_messages). Email is source adapter #1; the pipeline is source-agnostic
-- (synthesis §1/§2/§3).
--
-- Every statement is additive: comms_messages gains a nullable column + index,
-- and three new tables are created IF NOT EXISTS. No existing row is rewritten
-- and no existing writer changes shape (the steward allowlist gains the new
-- comms column in the SAME commit — _checked() raises on unknown keys otherwise).
--
-- Append-only (M-06): once applied, this file's checksum is frozen. Never edit
-- it — ship a forward migration instead.
--
-- Two RESOLUTIONS-round-1 overrides are folded in below and marked inline:
--   F1  edc_source_cursor — durable per-source triage watermark so the adapter
--       selects only messages beyond the cursor (O(new), not O(history)).
--   F2  decisions.escalated_for_deadline — one-shot deadline-escalation flag so
--       an item due within 24h does not re-ping every 5-min sweep.

-- --------------------------------------------------------------------------
-- Owner axis on the email substrate (migration-123 owner_employee_id pattern).
-- Nullable + no default: a message from an UNMAPPED source stays owner NULL and
-- EDC skips it loudly rather than guessing (synthesis §8.1).
-- --------------------------------------------------------------------------
ALTER TABLE comms_messages ADD COLUMN owner_employee_id TEXT REFERENCES employees(id);
CREATE INDEX IF NOT EXISTS idx_comms_owner ON comms_messages(owner_employee_id, sent_at);

-- --------------------------------------------------------------------------
-- decisions — the generic, owner-scoped Decision object.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS decisions (
    id                  TEXT PRIMARY KEY,            -- new_id('dcn')
    number              INTEGER NOT NULL UNIQUE,     -- monotonic human ref: card ref EDC-<n>
    owner_employee_id   TEXT NOT NULL REFERENCES employees(id),
    company_slug        TEXT NOT NULL DEFAULT '',    -- org_companies.slug via the account map (§8)
    -- source adapter boundary
    source              TEXT NOT NULL,               -- 'email' | 'rule_proposal' (v1); later 'slack','billing','agent'
    source_ref          TEXT NOT NULL,               -- comms_messages.id | decision_rules.id
    source_account      TEXT NOT NULL DEFAULT '',    -- 'gmail_ownera', ...
    occurred_at         TEXT,
    -- what happened / why it matters
    title               TEXT NOT NULL,
    context             TEXT NOT NULL DEFAULT '',    -- concise what/why (redacted, quote_untrusted'd)
    counterparty        TEXT NOT NULL DEFAULT '',
    -- classification (derived from consequence+deadline+likelihood, never wording)
    classification      TEXT NOT NULL CHECK (classification IN ('urgent','needs_owner','maybe','ignore')),
    consequence         TEXT NOT NULL DEFAULT '',
    deadline_at         TEXT,
    likelihood          REAL,
    confidence          REAL NOT NULL DEFAULT 0.0,
    reason              TEXT NOT NULL DEFAULT '',
    classifier          TEXT NOT NULL DEFAULT 'deterministic'
                        CHECK (classifier IN ('deterministic','rule','llm','llm_unavailable')),
    rule_matches_json   TEXT NOT NULL DEFAULT '[]',
    -- REQUIRED recommended action (invariant 1): {kind, params, human_line}
    recommended_json    TEXT NOT NULL,
    available_actions_json TEXT NOT NULL DEFAULT '[]',
    -- lifecycle (reconciled vocabulary — supersedes both FE plans' status lists).
    -- Recovery states (review F03, crash-safety): an action transitions
    -- open→in_progress BEFORE the external effect, then to done_unverified on
    -- success, 'failed_retryable' on a transient error (safe to re-drive), or
    -- 'reconcile_required' when the effect's outcome is AMBIGUOUS (a send/execute
    -- that may or may not have landed — never silently retried, never marked
    -- done). 'failed' is the terminal, non-retryable failure.
    status              TEXT NOT NULL DEFAULT 'open' CHECK (status IN (
                          'suppressed','open','snoozed','draft_pending','awaiting_approval',
                          'in_progress','done_unverified','done_verified','dismissed','denied',
                          'expired','failed','failed_retryable','reconcile_required')),
    surfaced            INTEGER NOT NULL DEFAULT 0,
    -- F2: one-shot deadline-escalation flag. Set true when the deadline-URGENT
    -- DM fires; the sweep escalates only when it is false, so a snoozed item due
    -- within 24h cannot re-ping every tick (no DM-spam loop).
    escalated_for_deadline INTEGER NOT NULL DEFAULT 0,
    resolution          TEXT CHECK (resolution IN ('execute','delegate','defer','approve','deny',
                                                   'reply','snooze','dismiss') OR resolution IS NULL),
    decided_by          TEXT,
    decided_at          TEXT,
    notes               TEXT NOT NULL DEFAULT '',
    tags_json           TEXT NOT NULL DEFAULT '[]',
    -- linkage (Decision <-> work)
    board_task_id       TEXT REFERENCES board_tasks(id),
    board_task_ref      TEXT NOT NULL DEFAULT '',    -- 'EDC-<number>' (directive: link via ref)
    wq_unit_id          TEXT,
    assignee_employee_id TEXT REFERENCES employees(id),
    -- reply/execute machinery
    draft_json          TEXT NOT NULL DEFAULT '{}',  -- {to,subject,body,sha256,approved_sha256,approved_at}
    execution_json      TEXT NOT NULL DEFAULT '{}',  -- executor kind, preview shown, broker call ids, results
    verification_json   TEXT NOT NULL DEFAULT '{}',  -- probe spec + result
    -- snooze / slack transport
    snooze_until        TEXT,
    slack_number        INTEGER,                     -- team-decisions number while a numbered DM is pending
    dm_channel          TEXT NOT NULL DEFAULT '',
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    UNIQUE (source, source_ref, owner_employee_id)   -- adapter idempotency = the cursor (D3)
);
CREATE INDEX IF NOT EXISTS idx_decisions_owner_status ON decisions(owner_employee_id, status);
CREATE INDEX IF NOT EXISTS idx_decisions_owner_class  ON decisions(owner_employee_id, classification, created_at);
CREATE INDEX IF NOT EXISTS idx_decisions_deadline     ON decisions(deadline_at) WHERE deadline_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_decisions_board_task   ON decisions(board_task_id) WHERE board_task_id IS NOT NULL;

-- --------------------------------------------------------------------------
-- decision_events — append-only audit trail (task_events pattern, migration 123).
-- ON DELETE CASCADE: an event has no meaning without its decision.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS decision_events (
    id          TEXT PRIMARY KEY,                    -- new_id('dce')
    decision_id TEXT NOT NULL REFERENCES decisions(id) ON DELETE CASCADE,
    actor       TEXT NOT NULL,                       -- employee id | 'system' | 'system:rule:<id>'
    event       TEXT NOT NULL CHECK (event IN (
                  'create','classify','surface','decide','edit','draft','approve','deny',
                  'send','delegate','defer','execute','snooze','dismiss','escalate',
                  'complete','verify_outcome','rule_match','expire')),
    from_status TEXT,
    to_status   TEXT,
    note        TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_decision_events ON decision_events(decision_id, created_at);

-- --------------------------------------------------------------------------
-- decision_rules — per-owner structured rules. Learning kinds vs automation
-- kinds are DISJOINT by CHECK + writer (synthesis §9). Learning code can never
-- write the automation kinds; those arrive only via explicit promotion.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS decision_rules (
    id                 TEXT PRIMARY KEY,             -- new_id('dcr')
    owner_employee_id  TEXT NOT NULL REFERENCES employees(id),
    kind               TEXT NOT NULL CHECK (kind IN (
                         'suppress','surface','classify_hint',   -- visibility preference
                         'snooze_default',                        -- decision preference
                         'delegate',                              -- delegation preference (pre-fill only)
                         'auto_delegate','auto_send')),           -- AUTOMATION AUTHORITY (promotion only)
    category           TEXT NOT NULL DEFAULT '',
    matcher_json       TEXT NOT NULL,                -- structured: {sender|sender_domain|subject_re|category|source}
    action_json        TEXT NOT NULL DEFAULT '{}',
    state              TEXT NOT NULL DEFAULT 'proposed'
                       CHECK (state IN ('proposed','active','disabled','declined')),
    created_from       TEXT NOT NULL DEFAULT '',     -- 'learned:<ids>' | 'nl:<decision id>' | 'manual'
    approved_by        TEXT,
    approved_at        TEXT,
    hit_count          INTEGER NOT NULL DEFAULT 0,
    last_hit_at        TEXT,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_decision_rules_owner ON decision_rules(owner_employee_id, state, kind);

-- --------------------------------------------------------------------------
-- F1: edc_source_cursor — durable per-(source, owner) triage watermark.
-- The email adapter advances this to the highest comms_messages.id it has
-- turned into a Decision, and selects only rows beyond it — triage stays
-- O(new messages), never a LEFT JOIN scan of the growing comms history. The
-- UNIQUE(source, source_ref, owner) on decisions remains the idempotency
-- backstop; this cursor is the fast path (mirrors the imap poller's uid_cursor
-- idea).
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS edc_source_cursor (
    source            TEXT NOT NULL,
    owner_employee_id TEXT NOT NULL REFERENCES employees(id),
    last_message_id   TEXT NOT NULL DEFAULT '',      -- comms_messages.id watermark (highest triaged)
    last_triaged_at   TEXT,
    PRIMARY KEY (source, owner_employee_id)
);
