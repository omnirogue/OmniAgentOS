-- OmniAgentOS H2 self-improvement lab schema (additive migration 003).
-- Never edits 001_init.sql / 002_events_target.sql. Applied by the existing
-- migrate_connection() runner after 001/002. All timestamps UTC ISO-8601.
-- Reward-hacking invariant: eval_cases.expected_json for split='held_out' is
-- served ONLY through the eval package's protected path — never via the lab API.

CREATE TABLE surfaces (
    id             TEXT PRIMARY KEY,        -- new_id('srf'); for genomes this IS the config_id
    kind           TEXT NOT NULL,           -- lab.contracts.SurfaceKind
    discipline     TEXT NOT NULL,
    version        INTEGER NOT NULL DEFAULT 1,
    path           TEXT NOT NULL,
    content_hash   TEXT NOT NULL,
    parent_version INTEGER,
    status         TEXT NOT NULL DEFAULT 'draft',
    label          TEXT NOT NULL DEFAULT '',
    safety_relevant INTEGER NOT NULL DEFAULT 0,  -- promotion needs a human (HD-003)
    created_at     TEXT NOT NULL,
    UNIQUE(kind, discipline, version, path)
);
CREATE INDEX idx_surfaces_disc ON surfaces(discipline, kind, status);

CREATE TABLE champions (
    discipline               TEXT NOT NULL,
    surface_kind             TEXT NOT NULL,
    surface_id               TEXT NOT NULL,
    surface_version          INTEGER NOT NULL,
    promoted_from_experiment TEXT,
    rollback_to_surface_id   TEXT,          -- NULL only for seed champion #0 (HD-011)
    cas_version              INTEGER NOT NULL DEFAULT 0,  -- compare-and-swap guard (HD-006)
    promoted_at              TEXT NOT NULL,
    PRIMARY KEY (discipline, surface_kind)
);
CREATE TABLE champion_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    discipline   TEXT NOT NULL,
    surface_kind TEXT NOT NULL,
    surface_id   TEXT NOT NULL,
    event        TEXT NOT NULL,             -- 'promoted' | 'rolled_back'
    experiment   TEXT,
    at           TEXT NOT NULL
);

CREATE TABLE eval_suites (
    id           TEXT PRIMARY KEY,          -- new_id('evs')
    discipline   TEXT NOT NULL,
    version      INTEGER NOT NULL DEFAULT 1,
    metrics_json TEXT NOT NULL DEFAULT '[]',
    protected    INTEGER NOT NULL DEFAULT 1,
    dataset_hash TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    UNIQUE(discipline, version)
);

-- eval_cases (shared DB): held_out rows carry input/rubric ONLY. Their expected
-- answers are NEVER stored here (design review HD-001/HD-ALT) — a challenger/judge/
-- curator subprocess can read this DB file. Held-out expected lives solely in the L02
-- protected store var/eval_protected.db (separate file, opened only by the grader
-- process). expected_json here is populated ONLY for split='dev' (candidate-visible).
CREATE TABLE eval_cases (
    id            TEXT PRIMARY KEY,         -- new_id('evc')
    suite_id      TEXT NOT NULL REFERENCES eval_suites(id),
    split         TEXT NOT NULL,            -- 'dev' | 'held_out'
    input_json    TEXT NOT NULL,
    expected_json TEXT,                     -- DEV ONLY; NULL for held_out (protected store holds it)
    rubric        TEXT NOT NULL DEFAULT '',
    weight        REAL NOT NULL DEFAULT 1.0
);
CREATE INDEX idx_eval_cases_suite ON eval_cases(suite_id, split);

CREATE TABLE eval_results (
    id                  TEXT PRIMARY KEY,   -- new_id('evr')
    experiment_id       TEXT NOT NULL,
    arm                 TEXT NOT NULL,      -- 'champion' | 'challenger'
    suite_id            TEXT NOT NULL,
    suite_version       INTEGER NOT NULL,
    split               TEXT NOT NULL,
    metrics_json        TEXT NOT NULL DEFAULT '{}',
    per_case_json       TEXT NOT NULL DEFAULT '{}',
    replicate           INTEGER NOT NULL DEFAULT 0,
    deterministic_passed INTEGER NOT NULL DEFAULT 1,
    created_at          TEXT NOT NULL
);
CREATE INDEX idx_eval_results_exp ON eval_results(experiment_id, arm, split);

CREATE TABLE judge_records (
    id              TEXT PRIMARY KEY,       -- new_id('jdg')
    case_id         TEXT NOT NULL,
    experiment_id   TEXT,
    tournament_id   TEXT,
    arm             TEXT NOT NULL,          -- recorded AFTER judging
    judge_lineage   TEXT NOT NULL,
    blind_token     TEXT NOT NULL,          -- what the judge actually saw
    score           REAL NOT NULL,
    notes           TEXT NOT NULL DEFAULT '',
    rubric_dimension TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL
);

CREATE TABLE experiments (
    id                   TEXT PRIMARY KEY,  -- new_id('exp')
    hypothesis           TEXT NOT NULL,
    discipline           TEXT NOT NULL,
    explore_policy       TEXT NOT NULL DEFAULT 'exploit',
    mutable_surface_kind TEXT NOT NULL,
    champion_surface_id  TEXT NOT NULL,
    challenger_surface_id TEXT NOT NULL,
    eval_suite_id        TEXT NOT NULL,
    dataset_hash         TEXT NOT NULL DEFAULT '',
    primary_metric       TEXT NOT NULL DEFAULT '',
    budgets_json         TEXT NOT NULL DEFAULT '{}',
    promotion_json       TEXT NOT NULL DEFAULT '{}',
    status               TEXT NOT NULL DEFAULT 'proposed',
    disposition          TEXT,
    scorecard_json       TEXT,
    snapshot_hash        TEXT NOT NULL DEFAULT '',
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);
CREATE INDEX idx_experiments_disc ON experiments(discipline, status);

CREATE TABLE tournaments (
    id              TEXT PRIMARY KEY,       -- new_id('tnm')
    subject         TEXT NOT NULL,
    discipline      TEXT NOT NULL,
    arena_task_hash TEXT NOT NULL,
    config_ids_json TEXT NOT NULL DEFAULT '[]',
    winner_config_id TEXT,
    status          TEXT NOT NULL DEFAULT 'running',
    created_at      TEXT NOT NULL
);

CREATE TABLE matches (
    id            TEXT PRIMARY KEY,         -- new_id('mch')
    tournament_id TEXT NOT NULL REFERENCES tournaments(id),
    config_a      TEXT NOT NULL,
    config_b      TEXT NOT NULL,
    winner        TEXT,
    score_a       REAL NOT NULL DEFAULT 0,
    score_b       REAL NOT NULL DEFAULT 0,
    judge_notes   TEXT NOT NULL DEFAULT '',
    blind         INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL
);
CREATE INDEX idx_matches_tnm ON matches(tournament_id);

CREATE TABLE elo_ratings (
    subject    TEXT NOT NULL,
    config_id  TEXT NOT NULL,
    rating     REAL NOT NULL DEFAULT 1000.0,
    matches    INTEGER NOT NULL DEFAULT 0,
    wins       INTEGER NOT NULL DEFAULT 0,
    losses     INTEGER NOT NULL DEFAULT 0,
    draws      INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (subject, config_id)
);

CREATE TABLE playbook (
    id                    TEXT PRIMARY KEY, -- new_id('pbk')
    trait                 TEXT NOT NULL,
    discipline            TEXT NOT NULL,
    evidence_experiments_json TEXT NOT NULL DEFAULT '[]',
    evidence_tournaments_json TEXT NOT NULL DEFAULT '[]',
    scope                 TEXT NOT NULL DEFAULT '',
    confidence            TEXT NOT NULL DEFAULT 'medium',
    elo_support           REAL,
    status                TEXT NOT NULL DEFAULT 'validated',
    created_at            TEXT NOT NULL
);
CREATE INDEX idx_playbook_disc ON playbook(discipline, status);

CREATE TABLE leaderboard (
    subject            TEXT NOT NULL,
    discipline         TEXT NOT NULL,
    rank               INTEGER NOT NULL,
    config_id          TEXT NOT NULL,       -- == surfaces.id (srf_) of a genome (HD-004)
    elo                REAL NOT NULL DEFAULT 1000.0,
    summary            TEXT NOT NULL DEFAULT '',
    judge_notes        TEXT NOT NULL DEFAULT '',
    source_experiments_json TEXT NOT NULL DEFAULT '[]',
    updated_by         TEXT NOT NULL DEFAULT 'curator',
    updated_at         TEXT NOT NULL,
    PRIMARY KEY (subject, rank)
);

-- Delayed-outcome tracking (HD-012; §11.6 / §4): a promotion's later signal can trigger
-- auto-rollback so a change that scored well immediately but degrades over time is caught.
CREATE TABLE delayed_outcomes (
    id            TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL,
    discipline    TEXT NOT NULL,
    metric        TEXT NOT NULL,
    value         REAL,
    baseline      REAL,
    regressed     INTEGER NOT NULL DEFAULT 0,
    observed_at   TEXT NOT NULL,
    created_at    TEXT NOT NULL
);
CREATE INDEX idx_delayed_exp ON delayed_outcomes(experiment_id);

