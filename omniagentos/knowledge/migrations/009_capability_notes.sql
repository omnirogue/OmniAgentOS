-- 009: Plan-07 scoped, atomic capability notes.
--
-- Capability notes remain facts so they reuse the existing pgvector/FTS/graph recall
-- stack.  The dedicated columns make the tenant boundary and domain metadata
-- predicateable before ranking; frontmatter alone is never trusted for isolation.

ALTER TABLE facts ADD COLUMN capability_scope text
    CHECK (capability_scope IN ('company', 'estate'));
ALTER TABLE facts ADD COLUMN company_id text;
ALTER TABLE facts ADD COLUMN domains text[] NOT NULL DEFAULT '{}';
ALTER TABLE facts ADD COLUMN capability_kind text
    CHECK (capability_kind IN ('tool', 'technique', 'gotcha', 'vendor'));
ALTER TABLE facts ADD COLUMN capability_provenance text;
ALTER TABLE facts ADD COLUMN last_verified date;
ALTER TABLE facts ADD COLUMN promoted_from bigint REFERENCES facts(id);

ALTER TABLE facts ADD CONSTRAINT capability_note_shape CHECK (
    (capability_scope IS NULL
      AND company_id IS NULL
      AND capability_kind IS NULL
      AND capability_provenance IS NULL
      AND last_verified IS NULL)
    OR
    (capability_scope = 'company'
      AND company_id IS NOT NULL
      AND capability_kind IS NOT NULL
      AND capability_provenance IS NOT NULL
      AND last_verified IS NOT NULL
      AND cardinality(domains) > 0)
    OR
    (capability_scope = 'estate'
      AND company_id IS NULL
      AND capability_kind IS NOT NULL
      AND capability_provenance IS NOT NULL
      AND last_verified IS NOT NULL
      AND cardinality(domains) > 0)
);

-- Agent capture is always quarantined by 001.  Also force any agent-authored
-- capability into the company namespace: an uncertain or malicious capture may not
-- mint an estate-visible row.  Company identity is subsequently checked by the
-- admin promotion/capture path before activation.
CREATE OR REPLACE FUNCTION enforce_agent_insert_floor() RETURNS trigger AS $$
BEGIN
  IF current_user = 'knowledge_agent' THEN
    NEW.status     := 'quarantined';
    NEW.trust      := LEAST(COALESCE(NEW.trust, 0.5), 0.6);
    NEW.importance := LEAST(COALESCE(NEW.importance, 0.5), 0.6);
    NEW.confidence := LEAST(COALESCE(NEW.confidence, 0.7), 0.7);
    -- Preserve every poisoning clamp established by migration 002 when replacing
    -- this function. Embeddings remain legitimate ingest data and are re-embedded
    -- by the admin promotion path.
    NEW.superseded_by := NULL;
    NEW.invalid_at    := NULL;
    NEW.access_count  := 0;
    NEW.helped_count  := 0;
    IF NEW.capability_scope IS NOT NULL THEN
      NEW.capability_scope := 'company';
      IF NEW.company_id IS NULL OR btrim(NEW.company_id) = '' THEN
        RAISE EXCEPTION 'company_id is required for agent capability capture';
      END IF;
    END IF;
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE INDEX facts_capability_visibility_idx
    ON facts (capability_scope, company_id, status)
    WHERE invalid_at IS NULL AND capability_scope IS NOT NULL;
CREATE INDEX facts_capability_domains_idx ON facts USING gin (domains)
    WHERE invalid_at IS NULL AND capability_scope IS NOT NULL;

CREATE TABLE capability_promotion_log (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_fact_id bigint NOT NULL REFERENCES facts(id),
    estate_fact_id bigint NOT NULL REFERENCES facts(id),
    source_company_id text NOT NULL,
    source_statement text NOT NULL,
    estate_statement text NOT NULL,
    actor text NOT NULL,
    promoted_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX capability_promotion_source_idx
    ON capability_promotion_log (source_fact_id, promoted_at DESC);

GRANT SELECT ON capability_promotion_log TO knowledge_agent;
GRANT SELECT, INSERT ON capability_promotion_log TO knowledge_admin;
GRANT USAGE, SELECT ON SEQUENCE capability_promotion_log_id_seq TO knowledge_admin;
GRANT SELECT (capability_scope, company_id, domains, capability_kind,
              capability_provenance, last_verified, promoted_from) ON facts
    TO knowledge_agent;
