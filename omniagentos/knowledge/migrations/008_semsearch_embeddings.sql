-- 008: unified semantic index for skills, tools, and grantable capabilities.
--
-- This table shares the knowledge Postgres and frozen bge-m3 dimension, but it
-- remains separate from the facts/episodes graph because these rows describe
-- executable system inventory rather than learned knowledge.

CREATE TABLE semsearch_embeddings (
    id bigserial PRIMARY KEY,
    kind text NOT NULL CHECK (kind IN ('skill', 'tool', 'capability')),
    ref_id text NOT NULL,
    text text NOT NULL,
    content_hash text NOT NULL,
    embedding halfvec(1024) NOT NULL,
    model_id text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (kind, ref_id)
);

CREATE INDEX semsearch_embeddings_vec_ix
    ON semsearch_embeddings USING hnsw (embedding halfvec_cosine_ops);
CREATE INDEX semsearch_embeddings_kind_ix ON semsearch_embeddings (kind);

GRANT SELECT, INSERT, UPDATE, DELETE ON semsearch_embeddings TO knowledge_agent;
GRANT USAGE, SELECT ON SEQUENCE semsearch_embeddings_id_seq TO knowledge_agent;
GRANT ALL ON semsearch_embeddings TO knowledge_admin;
GRANT ALL ON SEQUENCE semsearch_embeddings_id_seq TO knowledge_admin;
