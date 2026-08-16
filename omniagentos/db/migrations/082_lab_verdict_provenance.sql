-- 082: auditable lab verdict provenance (075 remains a permanent gap).
CREATE TABLE lab_verdict_provenance (
  verdict_id               TEXT PRIMARY KEY,
  experiment_id            TEXT NOT NULL UNIQUE,
  panel_composition_json   TEXT NOT NULL,
  panel_lineage_count      INTEGER NOT NULL CHECK (panel_lineage_count >= 0),
  replicate_count          INTEGER NOT NULL CHECK (replicate_count >= 0),
  effective_n              INTEGER NOT NULL CHECK (effective_n >= 0),
  agreement                REAL NOT NULL CHECK (agreement >= 0.0 AND agreement <= 1.0),
  mde                      REAL NOT NULL CHECK (mde >= 0.0),
  observed_effect          REAL NOT NULL,
  invalidation_status      TEXT NOT NULL DEFAULT 'valid'
    CHECK (invalidation_status IN ('valid', 'invalidated')),
  blind_presentation_seed  INTEGER NOT NULL,
  created_at               TEXT NOT NULL
);

CREATE INDEX idx_lab_verdict_provenance_invalidation
  ON lab_verdict_provenance(
    invalidation_status,
    panel_lineage_count,
    agreement,
    effective_n
  );
