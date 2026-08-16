-- U-C12 verify-at-read at birth: record the content digest at approval time.
--
-- Lane: lane/cap-skills (U-C12). Ordinal 110 claimed by this lane; 109 is this
-- same lane's U-16 migration.
--
-- Skill bodies are about to be INLINED into worker briefs instead of rendered
-- as opaque `name@version` labels. That makes `skill_versions.content_snapshot`
-- an injection surface, so the resolver has to be able to tell "this is the
-- body that was approved" from "this is whatever is in the row now". It
-- recomputes contracts.digest(content_snapshot) at read time and compares it
-- against this column, which every sanctioned write path (upsert_skill,
-- _insert_version, and therefore the vault re-index, the curator and the API)
-- fills in as it writes the body.
--
-- The column is deliberately NULLABLE and deliberately NOT backfilled.
-- SQLite has no sha256, so a backfill would have to be computed in Python from
-- the rows themselves — which certifies nothing: it would stamp whatever the
-- content currently is as "what was approved", which is exactly the vacuous
-- self-certification this check exists to prevent. A NULL therefore means "no
-- approval record", and the resolver DROPS such a version loudly rather than
-- inlining it. Vault-backed skills mint a real digest on the next
-- index_vault_playbook() run, which the API performs at startup by default.

ALTER TABLE skill_versions ADD COLUMN content_digest TEXT;

CREATE INDEX IF NOT EXISTS idx_skill_versions_selected
    ON skill_versions(skill_id, version);
