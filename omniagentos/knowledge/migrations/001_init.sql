-- Knowledge subsystem schema — FROZEN for run 20260712-1251-synapse-h3 (Revision R1).
-- Applied by omniagentos/knowledge/migrate.py (connects as the database owner / superuser;
-- agent and admin roles never run migrations).

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE kb_meta (
  key text PRIMARY KEY,
  value text NOT NULL
);
-- R2-2: seeded AT MIGRATION TIME (privileged) — the agent role only ever reads kb_meta.
INSERT INTO kb_meta (key, value) VALUES
  ('embed_model', 'ollama:bge-m3'),
  ('embed_dim', '1024'),
  ('schema_note', 'Synapse 001 — run 20260712-1251-synapse-h3');

CREATE TABLE episodes (
  id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  source       text NOT NULL CHECK (source IN ('run','web','curator','vault','human','chat')),
  source_ref   text,
  agent_id     text,
  discipline   text,
  content      text NOT NULL,
  occurred_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX episodes_source_ref_idx ON episodes (source, source_ref);

CREATE TABLE entities (
  id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  name           text NOT NULL,
  kind           text NOT NULL,
  summary        text,
  name_embedding halfvec(1024),
  created_at     timestamptz NOT NULL DEFAULT now(),
  UNIQUE (name, kind)
);

CREATE TABLE facts (
  id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  statement     text NOT NULL,
  discipline    text,
  scope         text NOT NULL DEFAULT 'global',
  embedding     halfvec(1024),            -- nullable: EmbeddingUnavailable queues for consolidator retry
  search_tsv    tsvector GENERATED ALWAYS AS (to_tsvector('simple', statement)) STORED,
  episode_id    bigint NOT NULL REFERENCES episodes(id),
  provenance    text NOT NULL CHECK (provenance IN ('extracted','inferred','ambiguous')),
  trust         real NOT NULL DEFAULT 0.5 CHECK (trust >= 0 AND trust <= 1),
  confidence    real NOT NULL DEFAULT 0.7 CHECK (confidence >= 0 AND confidence <= 1),
  status        text NOT NULL DEFAULT 'quarantined' CHECK (status IN ('quarantined','active','superseded')),
  valid_at      timestamptz NOT NULL DEFAULT now(),
  recorded_at   timestamptz NOT NULL DEFAULT now(),
  invalid_at    timestamptz,
  superseded_by bigint REFERENCES facts(id),
  importance    real NOT NULL DEFAULT 0.5 CHECK (importance >= 0 AND importance <= 1),
  access_count  int  NOT NULL DEFAULT 0,
  last_accessed timestamptz NOT NULL DEFAULT now(),
  helped_count  int  NOT NULL DEFAULT 0
);
CREATE INDEX facts_embedding_idx ON facts USING hnsw (embedding halfvec_cosine_ops) WITH (m = 16, ef_construction = 64);
CREATE INDEX facts_tsv_idx ON facts USING gin (search_tsv);
CREATE INDEX facts_live_idx ON facts (discipline, status) WHERE invalid_at IS NULL;
CREATE INDEX facts_episode_idx ON facts (episode_id);

CREATE TABLE edges (
  id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  src_kind   text NOT NULL CHECK (src_kind IN ('fact','entity')),
  src_id     bigint NOT NULL,
  dst_kind   text NOT NULL CHECK (dst_kind IN ('fact','entity')),
  dst_id     bigint NOT NULL,
  edge_type  text NOT NULL CHECK (edge_type IN
    ('about','causes','contradicts','follows','co_occurs','refines','same_run','derived_from')),
  weight     real NOT NULL DEFAULT 0.5 CHECK (weight >= 0 AND weight <= 1),
  valid_at   timestamptz NOT NULL DEFAULT now(),
  invalid_at timestamptz,
  UNIQUE (src_kind, src_id, dst_kind, dst_id, edge_type)
);
CREATE INDEX edges_src_idx ON edges (src_kind, src_id, weight DESC) WHERE invalid_at IS NULL;
CREATE INDEX edges_dst_idx ON edges (dst_kind, dst_id, weight DESC) WHERE invalid_at IS NULL;

CREATE TABLE recall_log (
  id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  run_id       text,
  agent_id     text,
  discipline   text,
  query_digest text NOT NULL,
  fact_ids     bigint[] NOT NULL DEFAULT '{}',
  tokens       int NOT NULL DEFAULT 0,
  latency_ms   real NOT NULL DEFAULT 0,
  created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX recall_log_run_idx ON recall_log (run_id);

-- === Role separation (design-review F1 BLOCKER: the DATABASE is the promotion boundary, not Python) ===
-- Cluster-level roles, guarded for idempotency. Passwords are set by scripts/knowledge/pg-auth-setup.sh
-- (never in migrations); that script also flips pg_hba to scram-sha-256 for ALL local connections
-- (trust-auth would let any local process connect as ANY role, including the superuser) and
-- self-tests that passwordless superuser/admin connections now FAIL.
DO $$ BEGIN CREATE ROLE knowledge_agent LOGIN; EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN CREATE ROLE knowledge_admin LOGIN; EXCEPTION WHEN duplicate_object THEN NULL; END $$;

GRANT USAGE ON SCHEMA public TO knowledge_agent, knowledge_admin;
GRANT SELECT ON episodes, entities, facts, edges, recall_log, kb_meta TO knowledge_agent;
GRANT INSERT ON episodes, entities, facts, edges, recall_log TO knowledge_agent;
GRANT UPDATE (access_count, last_accessed, helped_count) ON facts TO knowledge_agent;  -- Hebbian bookkeeping only
GRANT UPDATE (weight) ON edges TO knowledge_agent;                                     -- Hebbian strengthen
-- R2-8: NO entity UPDATE for the agent role — new-entity INSERT is allowed (UNIQUE(name,kind)
-- prevents overwriting hot entities; the agent-store upsert is insert-or-ignore); entity
-- enrichment + name_embedding backfill are consolidator/admin work. schema_migrations gets NO
-- agent grant at all (default-deny).
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO knowledge_agent;
GRANT ALL ON ALL TABLES IN SCHEMA public TO knowledge_admin;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO knowledge_admin;
-- knowledge_agent gets NO DELETE anywhere, NO UPDATE on facts.status/trust/importance/embedding/
-- invalid_at/superseded_by, NO UPDATE on edges.invalid_at → promotion, supersession, invalidation and
-- embedding backfill are knowledge_admin-only (consolidator/operator), enforced by PostgreSQL itself.
-- Migration discipline: every future migration adding a table/column MUST include its GRANT stanza;
-- migrate.py refuses to apply a migration file containing CREATE TABLE without a GRANT stanza.

CREATE FUNCTION enforce_agent_insert_floor() RETURNS trigger AS $$
BEGIN
  IF current_user = 'knowledge_agent' THEN
    NEW.status     := 'quarantined';                              -- agents can NEVER insert active facts
    NEW.trust      := LEAST(COALESCE(NEW.trust, 0.5), 0.6);       -- ...nor high-trust ones
    NEW.importance := LEAST(COALESCE(NEW.importance, 0.5), 0.6);  -- R2-9: nor promotion-queue-dominating ones
    NEW.confidence := LEAST(COALESCE(NEW.confidence, 0.7), 0.7);
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;
CREATE TRIGGER facts_agent_insert_floor BEFORE INSERT ON facts
  FOR EACH ROW EXECUTE FUNCTION enforce_agent_insert_floor();
