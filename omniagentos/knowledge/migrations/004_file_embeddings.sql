-- 004: deep semantic FILE index for omniagentos/filesearch.
--
-- Its OWN table in the knowledge Postgres — deliberately NOT mixed into the
-- knowledge facts/episodes graph: rows here are chunk embeddings of the user's
-- files (privacy: embedded by the LOCAL Ollama bge-m3, stored locally only),
-- keyed by (path, chunk_ix) and refreshed incrementally by (path, mtime) from
-- the filesearch catalog. Written/read by the knowledge_agent role (the
-- filesearch index job and the API run with the agent DSN); full DML on this
-- one table is safe — it carries no trust/promotion semantics.

CREATE TABLE file_embeddings (
    path text NOT NULL,
    root text NOT NULL,          -- desktop|icloud|gdrive|repo|other-mount
    category text NOT NULL,      -- documents|spreadsheets|presentations|images|video|audio|code|archives|other
    mtime double precision NOT NULL,
    chunk_ix int NOT NULL,
    embedding vector(1024) NOT NULL,   -- bge-m3, same frozen 1024-d as the knowledge base
    excerpt text NOT NULL DEFAULT '',
    indexed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (path, chunk_ix)
);

CREATE INDEX file_embeddings_root_ix ON file_embeddings (root);
CREATE INDEX file_embeddings_category_ix ON file_embeddings (category);
CREATE INDEX file_embeddings_vec_ix ON file_embeddings USING hnsw (embedding vector_cosine_ops);

GRANT SELECT, INSERT, UPDATE, DELETE ON file_embeddings TO knowledge_agent;
GRANT ALL ON file_embeddings TO knowledge_admin;
