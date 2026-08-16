-- U-S2 / S3-W3: the name-only secret catalog and its rotation state machine.
--
-- ORDINAL: renumbered 109 -> 111 at Phase-2 integration, which is exactly the
-- renumber-at-merge this file was written expecting (PLAN.md §4 trap rule 3).
-- Three lanes claimed 109; 109 and 110 turned out to be already APPLIED on the
-- live database by lane/cap-skills, so they are spent history and this file
-- takes the first genuinely free ordinal above them.
--
-- `schema_migrations` reconciliation for this renumber is a NO-OP, and that is
-- a verified fact rather than an assumption: no database anywhere has a row for
-- this file's content. The live database has no `secret_catalog`,
-- `secret_versions`, `secret_rotations` or `secret_rotation_events` table, and
-- its 109/110 rows checksum to lane/cap-skills' two files, not to this one. A
-- renumber only owes a row rewrite when the OLD ordinal was applied; this one
-- never was, so there is nothing to match, nothing to re-checksum, and no
-- reason to touch a live row. 105-110 are applied live and are never edited.
--
-- Had it been applied somewhere, the owed reconciliation would be, in order:
-- back the database up; confirm the recorded checksum for the old ordinal
-- equals sha256 of this file's bytes (matching content is what makes a
-- renumber a rename rather than a rewrite); `UPDATE schema_migrations SET
-- version = 111 WHERE version = 109`; leave the checksum alone, because the
-- bytes did not change. Never a DELETE, and never a re-apply -- the DDL is not
-- idempotent past `CREATE TABLE IF NOT EXISTS`.
--
-- NO COLUMN HERE HOLDS SECRET MATERIAL. `env_name` is the NAME of an
-- environment variable and nothing else. There is deliberately no value
-- column, no ciphertext column, and no fingerprint/hash-of-value column
-- anywhere in these four tables, so a catalog dump can never become a
-- credential dump and no future writer can be tempted by an obvious slot.

CREATE TABLE IF NOT EXISTS secret_catalog (
    credential_id       TEXT PRIMARY KEY,
    env_name            TEXT NOT NULL UNIQUE,
    provider_family     TEXT NOT NULL DEFAULT '',
    risk_domain         TEXT NOT NULL DEFAULT '',
    store_shard         TEXT NOT NULL DEFAULT '',
    active_version_id   TEXT NOT NULL DEFAULT '',
    capability_refs     TEXT NOT NULL DEFAULT '',
    owner               TEXT NOT NULL DEFAULT '',
    effect_class        TEXT NOT NULL DEFAULT 'unknown'
        CHECK (effect_class IN ('read', 'write', 'read_write', 'unknown')),
    -- The six states of D-33 / S3-W3. `missing` is a MARK, never an invention:
    -- a declared name with no value gets a row and no version, and provisioning
    -- it is a separate named [OPERATOR] decision.
    state               TEXT NOT NULL
        CHECK (state IN ('missing', 'quarantined', 'active', 'rotating', 'retired', 'revoked')),
    created_at          TEXT NOT NULL DEFAULT '',
    rotated_at          TEXT NOT NULL DEFAULT '',
    rotation_due_at     TEXT NOT NULL DEFAULT '',
    -- Populated ONLY from the U-A1 broker audit spine (`broker_calls`). Empty
    -- means "no broker evidence of use", which is NOT the same claim as
    -- "never used" -- so nothing may retire a credential on this column alone.
    last_used_at        TEXT NOT NULL DEFAULT '',
    recovery_dependency TEXT NOT NULL DEFAULT '',
    -- Operator assertion that this credential is shared with a system outside
    -- this repo. Repo absence is not proof of provider-side death, so a marked
    -- row can never be auto-revoked or auto-deleted by a sync pass.
    shared_owner_marked INTEGER NOT NULL DEFAULT 0,
    disposition_note    TEXT NOT NULL DEFAULT '',
    updated_at          TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_secret_catalog_state ON secret_catalog(state, env_name);

-- One row per key version. The active pointer in `secret_catalog` references a
-- version id here, so "which version was live when that call was made" and
-- "which version has been revoked at the provider" are separate, durable facts.
CREATE TABLE IF NOT EXISTS secret_versions (
    version_id        TEXT PRIMARY KEY,
    credential_id     TEXT NOT NULL REFERENCES secret_catalog(credential_id),
    state             TEXT NOT NULL
        CHECK (state IN ('staged', 'canary', 'active', 'retired', 'revoked')),
    created_at        TEXT NOT NULL DEFAULT '',
    activated_at      TEXT NOT NULL DEFAULT '',
    retired_at        TEXT NOT NULL DEFAULT '',
    revoked_at        TEXT NOT NULL DEFAULT '',
    -- 1 only after a provider-side revocation actually executed. The step is
    -- [OPERATOR]-gated (D-05) and OFF by default, so this column stays 0 until a
    -- human arms it.
    provider_revoked  INTEGER NOT NULL DEFAULT 0,
    backup_expires_at TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_secret_versions_credential
    ON secret_versions(credential_id, state);

-- One row per rotation ceremony. `outcome` stays 'open' until the receipt is
-- closed, so an interrupted ceremony is visible rather than inferred.
CREATE TABLE IF NOT EXISTS secret_rotations (
    rotation_id           TEXT PRIMARY KEY,
    credential_id         TEXT NOT NULL REFERENCES secret_catalog(credential_id),
    from_version_id       TEXT NOT NULL DEFAULT '',
    to_version_id         TEXT NOT NULL DEFAULT '',
    step                  TEXT NOT NULL DEFAULT 'create_new',
    outcome               TEXT NOT NULL DEFAULT 'open'
        CHECK (outcome IN ('open', 'succeeded', 'rolled_back', 'rolled_back_no_active', 'failed')),
    provider_revoke_state TEXT NOT NULL DEFAULT 'not_attempted'
        CHECK (provider_revoke_state IN
            ('not_attempted', 'skipped_flag_off', 'completed', 'failed')),
    operator              TEXT NOT NULL DEFAULT '',
    opened_at             TEXT NOT NULL DEFAULT '',
    closed_at             TEXT NOT NULL DEFAULT '',
    receipt_digest        TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_secret_rotations_credential
    ON secret_rotations(credential_id, outcome);

-- Append-only step log. The receipt digest is computed over these rows, so a
-- ceremony that skipped a step cannot later claim it ran one.
CREATE TABLE IF NOT EXISTS secret_rotation_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    rotation_id TEXT NOT NULL REFERENCES secret_rotations(rotation_id),
    seq         INTEGER NOT NULL,
    step        TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('ok', 'refused', 'skipped')),
    detail      TEXT NOT NULL DEFAULT '',
    ts          TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_secret_rotation_events_rotation
    ON secret_rotation_events(rotation_id, seq);
