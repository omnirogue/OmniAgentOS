-- Migration 002 — agent-write hardening (adversarial council: security-abuse seat)
-- Tightens what the knowledge_agent role can WRITE, closing three poisoning primitives
-- the frozen 001 trigger/grants left open. All enforced by PostgreSQL, not Python.
-- GRANT stanza present (migrate.py refuses CREATE-without-GRANT); this migration only
-- alters triggers/functions/grants, adds no new table.

-- (1) Episode-source forgery: the promotion predicate keys on DISTINCT episode.source
-- types, but the agent role could INSERT episodes with source='human'/'vault'/'curator'
-- (the "trusted" labels). Clamp any agent-role episode insert to run/chat so an agent can
-- never fabricate a trusted-source corroboration for its own claim.
CREATE FUNCTION enforce_agent_episode_source() RETURNS trigger AS $$
BEGIN
  IF current_user = 'knowledge_agent' AND NEW.source NOT IN ('run', 'chat', 'web') THEN
    NEW.source := 'run';
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;
CREATE TRIGGER episodes_agent_source_floor BEFORE INSERT ON episodes
  FOR EACH ROW EXECUTE FUNCTION enforce_agent_episode_source();

-- (2) Fact-column poisoning: 001's trigger clamps status/trust/importance/confidence but
-- left embedding/superseded_by/invalid_at/access_count/helped_count agent-settable on
-- INSERT (pre-positioning an embedding near a target query; forged supersession/counters).
-- Force them to safe defaults for agent-role inserts.
CREATE OR REPLACE FUNCTION enforce_agent_insert_floor() RETURNS trigger AS $$
BEGIN
  IF current_user = 'knowledge_agent' THEN
    NEW.status        := 'quarantined';
    NEW.trust         := LEAST(COALESCE(NEW.trust, 0.5), 0.6);
    NEW.importance    := LEAST(COALESCE(NEW.importance, 0.5), 0.6);
    NEW.confidence    := LEAST(COALESCE(NEW.confidence, 0.7), 0.7);
    -- embedding is NOT clamped: the legitimate ingest path stores the real Ollama
    -- embedding of the statement on agent facts (needed for vector recall). A crafted
    -- embedding can't affect ACTIVE recall (quarantined facts are never injected), and
    -- the consolidator RE-EMBEDS from the statement at promotion (implemented in consolidate()), neutralizing any
    -- agent-chosen vector before the fact becomes active (council rated this LOW).
    NEW.superseded_by := NULL;   -- no legitimate agent use; forged supersession blocked
    NEW.invalid_at    := NULL;   -- no legitimate agent use; forged invalidation blocked
    NEW.access_count  := 0;
    NEW.helped_count  := 0;
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;
-- trigger facts_agent_insert_floor from 001 already binds this function name.

-- (3) Edge-weight steering: 001 let the agent UPDATE(weight) on ANY edge (pump a chosen
-- edge to 1.0 or zero a legitimate one → amplify/suppress OTHER agents' 2-hop recall) and
-- INSERT edges at any weight (forge a max-weight steering edge). The agent still LEGITIMATELY
-- needs edge INSERT during ingest (about/contradicts/co_occurs), so: keep INSERT but clamp
-- agent-inserted weight to a low ceiling, and REVOKE UPDATE(weight) entirely. Hebbian
-- strengthening (which must UPDATE weight) goes through an admin-owned SECURITY DEFINER fn.
REVOKE UPDATE (weight) ON edges FROM knowledge_agent;

CREATE FUNCTION enforce_agent_edge_weight() RETURNS trigger AS $$
BEGIN
  -- current_user is the agent for direct inserts; inside strengthen_co_occurrence
  -- (SECURITY DEFINER) it is the function owner, so the bounded +delta path is not clamped.
  IF current_user = 'knowledge_agent' THEN
    NEW.weight := LEAST(COALESCE(NEW.weight, 0.5), 0.5);
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;
CREATE TRIGGER edges_agent_weight_floor BEFORE INSERT ON edges
  FOR EACH ROW EXECUTE FUNCTION enforce_agent_edge_weight();

CREATE FUNCTION strengthen_co_occurrence(a bigint, b bigint, delta real DEFAULT 0.05, cap real DEFAULT 1.0)
  RETURNS void AS $$
BEGIN
  INSERT INTO edges (src_kind, src_id, dst_kind, dst_id, edge_type, weight)
  VALUES ('fact', LEAST(a, b), 'fact', GREATEST(a, b), 'co_occurs', LEAST(GREATEST(delta, 0.0), cap))
  ON CONFLICT (src_kind, src_id, dst_kind, dst_id, edge_type)
  DO UPDATE SET weight = LEAST(edges.weight + LEAST(GREATEST(delta, 0.0), cap), cap);
END $$ LANGUAGE plpgsql SECURITY DEFINER;
REVOKE ALL ON FUNCTION strengthen_co_occurrence(bigint, bigint, real, real) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION strengthen_co_occurrence(bigint, bigint, real, real) TO knowledge_agent, knowledge_admin;
