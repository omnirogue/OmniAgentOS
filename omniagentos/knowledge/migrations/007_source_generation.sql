-- Migration 007 — authoritative per-SOURCE generation registry (M-26 / M-27)
--
-- A promotion generation is a property of a SOURCE, never of a fact. Migration 006 stored
-- the generation on each evidence row, and the promotion predicate took the maximum
-- generation *within a single fact*. That is fact-local and wrong: a source that advanced
-- (re-ingest, re-run, new artifact revision) without re-verifying a given fact kept its
-- stale PASS counting for that fact.
--
-- The authoritative value is
--
--   current_generation(S) = GREATEST(
--       registry.current_generation,                       -- explicitly advanced here
--       MAX(promotion_evidence.promotion_generation)       -- implied, across ALL facts
--         FILTER (source_identity = S AND status = 'active')
--   )
--
-- and evidence counts only when promotion_generation = current_generation(S). Staleness
-- fails closed: a source that advanced without re-verifying this fact blocks promotion
-- exactly like a FAIL.
--
-- Role/trigger controls mirror 006 and are NOT relaxed: knowledge_agent gets SELECT only,
-- author_role is forced to current_user by a trigger, and knowledge_agent is rejected
-- outright by both a CHECK constraint and the trigger.

CREATE TABLE knowledge_source_generation (
  source_identity    text PRIMARY KEY,
  current_generation bigint NOT NULL CHECK (current_generation >= 1),
  updated_at         timestamptz NOT NULL DEFAULT now(),
  author_role        text NOT NULL DEFAULT current_user,
  CONSTRAINT knowledge_source_generation_no_agent_author CHECK (author_role <> 'knowledge_agent')
);

-- Force author_role to the connecting role (un-spoofable), same shape as 006.
CREATE FUNCTION knowledge_source_generation_set_author() RETURNS trigger AS $$
BEGIN
  NEW.author_role := current_user;
  IF current_user = 'knowledge_agent' THEN
    RAISE EXCEPTION 'knowledge_agent cannot advance a source generation'
      USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER knowledge_source_generation_set_author
  BEFORE INSERT OR UPDATE ON knowledge_source_generation
  FOR EACH ROW EXECUTE FUNCTION knowledge_source_generation_set_author();

-- Generations are monotonic: never let an UPDATE walk a source backwards, which would
-- resurrect stale PASS evidence.
CREATE FUNCTION knowledge_source_generation_monotonic() RETURNS trigger AS $$
BEGIN
  IF NEW.current_generation < OLD.current_generation THEN
    RAISE EXCEPTION 'source generation is monotonic: % -> % for %',
      OLD.current_generation, NEW.current_generation, OLD.source_identity
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER knowledge_source_generation_monotonic
  BEFORE UPDATE ON knowledge_source_generation
  FOR EACH ROW EXECUTE FUNCTION knowledge_source_generation_monotonic();

-- The predicate now takes MAX(promotion_generation) per source across ALL facts; 006's
-- index is (fact_id, status, promotion_generation) and cannot serve that scan.
CREATE INDEX promotion_evidence_source_gen_idx
  ON promotion_evidence (source_identity, status, promotion_generation DESC);

-- Agent may read the registry for transparency; only admin may write it.
GRANT SELECT ON knowledge_source_generation TO knowledge_agent;
GRANT SELECT, INSERT, UPDATE ON knowledge_source_generation TO knowledge_admin;

-- Backfill: every source that already has evidence starts at its highest recorded
-- generation, so applying this migration cannot invalidate evidence that was valid before.
INSERT INTO knowledge_source_generation (source_identity, current_generation)
SELECT source_identity, MAX(promotion_generation)
FROM promotion_evidence
WHERE status = 'active'
GROUP BY source_identity
ON CONFLICT (source_identity) DO NOTHING;
