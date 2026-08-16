-- 065_formation_selections.sql
-- Phase B6 telemetry: task → formation/arm → roles/models → outcome + ETAR.
-- Seeded by L5 calibration; filled by production routing later.

CREATE TABLE IF NOT EXISTS formation_selections (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    task_fingerprint TEXT,
    goal TEXT NOT NULL DEFAULT '',
    arm TEXT NOT NULL,                  -- formation | opus_solo
    formation_id TEXT,
    confidence REAL,
    low_confidence INTEGER NOT NULL DEFAULT 0,
    topology TEXT,
    implementers_json TEXT NOT NULL DEFAULT '[]',
    reviewer TEXT,
    planner TEXT,
    mechanical_gate INTEGER NOT NULL DEFAULT 0,
    models_json TEXT NOT NULL DEFAULT '{}',
    predicted_etar_s REAL,
    etar_components_json TEXT NOT NULL DEFAULT '{}',
    outcome TEXT,                       -- predicted | accepted | rejected | aborted
    repair_loops INTEGER NOT NULL DEFAULT 0,
    escalations INTEGER NOT NULL DEFAULT 0,
    first_pass_accept INTEGER,
    wall_clock_s REAL,
    source TEXT NOT NULL DEFAULT 'calibration',  -- calibration | production
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_formation_selections_formation
    ON formation_selections(formation_id);
CREATE INDEX IF NOT EXISTS idx_formation_selections_arm
    ON formation_selections(arm);
CREATE INDEX IF NOT EXISTS idx_formation_selections_created
    ON formation_selections(created_at);
