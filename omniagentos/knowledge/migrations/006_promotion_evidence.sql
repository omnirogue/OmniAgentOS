-- Migration 006 — system-authored promotion evidence (H-10 / B07 knowledge integrity)
-- Model-authored markers such as fact_id=<N> in run output are NEVER verification.
-- Only rows in this table, written by knowledge_admin/system paths, may satisfy the
-- system-verified half of the promotion predicate. Agent role has SELECT only.

CREATE TABLE promotion_evidence (
  id                   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  fact_id              bigint NOT NULL REFERENCES facts(id),
  provenance           text NOT NULL,
  source_identity      text NOT NULL,
  verifier_outcome     text NOT NULL CHECK (verifier_outcome IN ('pass', 'fail', 'inconclusive')),
  promotion_generation bigint NOT NULL CHECK (promotion_generation >= 1),
  status               text NOT NULL DEFAULT 'active'
                         CHECK (status IN ('active', 'revoked', 'superseded')),
  recorded_at          timestamptz NOT NULL DEFAULT now(),
  revoked_at           timestamptz,
  revoke_reason        text,
  superseded_by        bigint REFERENCES promotion_evidence(id),
  author_role          text NOT NULL DEFAULT current_user,
  CONSTRAINT promotion_evidence_system_provenance CHECK (
    provenance IN (
      'system_run_verifier',
      'system_operator',
      'system_consolidator',
      'system_lab_eval'
    )
  ),
  CONSTRAINT promotion_evidence_no_agent_author CHECK (author_role <> 'knowledge_agent')
);

CREATE INDEX promotion_evidence_fact_idx
  ON promotion_evidence (fact_id, status, promotion_generation DESC);

CREATE UNIQUE INDEX promotion_evidence_idempotent_idx
  ON promotion_evidence (fact_id, source_identity, promotion_generation, verifier_outcome)
  WHERE status = 'active';

-- Force author_role to the connecting role (un-spoofable).
CREATE FUNCTION promotion_evidence_set_author() RETURNS trigger AS $$
BEGIN
  NEW.author_role := current_user;
  IF current_user = 'knowledge_agent' THEN
    RAISE EXCEPTION 'knowledge_agent cannot author promotion evidence'
      USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER promotion_evidence_set_author
  BEFORE INSERT ON promotion_evidence
  FOR EACH ROW EXECUTE FUNCTION promotion_evidence_set_author();

-- Agent may read evidence for transparency; only admin may write/update lineage.
GRANT SELECT ON promotion_evidence TO knowledge_agent;
GRANT SELECT, INSERT, UPDATE ON promotion_evidence TO knowledge_admin;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO knowledge_admin;
