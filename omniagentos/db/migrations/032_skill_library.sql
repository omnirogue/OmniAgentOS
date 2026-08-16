-- Wave 4 S-A: structured index, immutable versions, update proposals, and
-- execution evidence for the vault-backed skills library.

CREATE TABLE IF NOT EXISTS skills (
    id               TEXT PRIMARY KEY,
    slug             TEXT NOT NULL UNIQUE,
    category         TEXT NOT NULL,
    subcategory      TEXT NOT NULL,
    title            TEXT NOT NULL,
    summary          TEXT NOT NULL DEFAULT '',
    preferred_method TEXT,
    fallback_method  TEXT,
    vault_note_path  TEXT,
    status           TEXT NOT NULL DEFAULT 'active'
                     CHECK (status IN ('active', 'archived')),
    current_version  INTEGER NOT NULL DEFAULT 1,
    created_at       TIMESTAMP NOT NULL,
    updated_at       TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS skill_versions (
    id               TEXT PRIMARY KEY,
    skill_id         TEXT NOT NULL REFERENCES skills(id) ON DELETE CASCADE,
    version          INTEGER NOT NULL,
    content_snapshot TEXT NOT NULL,
    preferred_method TEXT,
    fallback_method  TEXT,
    change_reason    TEXT NOT NULL,
    evidence_json    TEXT NOT NULL DEFAULT '{}',
    author           TEXT NOT NULL,
    status           TEXT NOT NULL CHECK (status IN ('active', 'superseded')),
    created_at       TIMESTAMP NOT NULL,
    UNIQUE (skill_id, version)
);

CREATE TABLE IF NOT EXISTS update_proposals (
    id                       TEXT PRIMARY KEY,
    skill_id                 TEXT NOT NULL REFERENCES skills(id) ON DELETE CASCADE,
    proposed_diff            TEXT,
    proposed_content         TEXT,
    evidence_files_json      TEXT NOT NULL DEFAULT '[]',
    linked_execution_id      TEXT,
    risk                     TEXT NOT NULL CHECK (risk IN ('low', 'model', 'major')),
    state                    TEXT NOT NULL CHECK (state IN ('pending', 'approved', 'rejected')),
    created_by               TEXT NOT NULL,
    created_at               TIMESTAMP NOT NULL,
    decided_by               TEXT,
    decided_at               TIMESTAMP,
    decision_note            TEXT
);

CREATE TABLE IF NOT EXISTS execution_evidence (
    id                TEXT PRIMARY KEY,
    proposal_id       TEXT REFERENCES update_proposals(id) ON DELETE SET NULL,
    skill_id          TEXT NOT NULL REFERENCES skills(id) ON DELETE CASCADE,
    run_id            TEXT,
    skill_version     INTEGER,
    validation_status TEXT,
    metrics_json      TEXT NOT NULL DEFAULT '{}',
    linked_episode_id TEXT,
    created_at        TIMESTAMP NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_skills_tree
    ON skills(category, subcategory, title);
CREATE INDEX IF NOT EXISTS idx_skill_versions_skill
    ON skill_versions(skill_id, version DESC);
CREATE INDEX IF NOT EXISTS idx_update_proposals_state
    ON update_proposals(state, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_update_proposals_skill
    ON update_proposals(skill_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_execution_evidence_skill
    ON execution_evidence(skill_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_execution_evidence_proposal
    ON execution_evidence(proposal_id);

-- The six proof-of-concept records are seeded here so a migrated live database
-- is immediately useful. index_vault_playbook() refreshes their structured
-- fields and snapshots from the checked-in notes without creating duplicates.
INSERT OR IGNORE INTO skills
    (id, slug, category, subcategory, title, summary, preferred_method,
     fallback_method, vault_note_path, status, current_version, created_at, updated_at)
VALUES
    ('webinars_voice_qwen9b', 'webinars_voice_qwen9b', 'Webinars', 'Voice',
     'Qwen 9B on RunPod', 'Run Qwen 9B voice model on RunPod for webinar narration',
     'Use RunPod inference endpoint with HF quantized model',
     'ElevenLabs API fallback (higher cost)',
     'vault/playbook/skill-webinars_voice_qwen9b.md', 'active', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP),
    ('webinars_voice_elevenlabs', 'webinars_voice_elevenlabs', 'Webinars', 'Voice',
     'ElevenLabs', 'Generate webinar narration with the ElevenLabs API',
     'Use ElevenLabs API', 'Qwen 9B on RunPod',
     'vault/playbook/skill-webinars_voice_elevenlabs.md', 'active', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP),
    ('webinars_video', 'webinars_video', 'Webinars', 'Video',
     'Record and Edit Webinar Video', 'Record and edit webinar video',
     'Record modular scenes and assemble with a reproducible edit script',
     'Record and edit manually in the approved desktop editor',
     'vault/playbook/skill-webinars_video.md', 'active', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP),
    ('webinars_slides', 'webinars_slides', 'Webinars', 'Slides',
     'Build Webinar Slides', 'Design and produce webinar presentation slides',
     'Generate a structured deck from the approved script and brand system',
     'Build the deck manually from the standard webinar template',
     'vault/playbook/skill-webinars_slides.md', 'active', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP),
    ('webinars_script', 'webinars_script', 'Webinars', 'Script',
     'Write Webinar Script', 'Write and validate a conversion-focused webinar script',
     'Draft from the approved offer brief, then validate timing and claims',
     'Adapt the most recent validated webinar script',
     'vault/playbook/skill-webinars_script.md', 'active', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP),
    ('infrastructure_runpod_deploy', 'infrastructure_runpod_deploy', 'Infrastructure', 'RunPod',
     'Deploy Model, Mount Volume, Start Endpoint',
     'Deploy ML model on RunPod with persistent storage + inference endpoint',
     'Deploy a pinned container with a persistent network volume and serverless endpoint',
     'Launch a secured GPU pod and expose the model through a private API service',
     'vault/playbook/skill-infrastructure_runpod_deploy.md', 'active', 1,
     CURRENT_TIMESTAMP, CURRENT_TIMESTAMP);

INSERT OR IGNORE INTO skill_versions
    (id, skill_id, version, content_snapshot, preferred_method, fallback_method,
     change_reason, evidence_json, author, status, created_at)
SELECT
    'skv_seed_' || id,
    id,
    1,
    '# Skill: ' || title || char(10) || char(10) ||
    '## Purpose' || char(10) || summary || char(10) || char(10) ||
    '## When to Use' || char(10) || 'Use when this workflow matches the approved task.' || char(10) || char(10) ||
    '## Preferred Method' || char(10) || '### Steps' || char(10) || '1. Validate inputs.' || char(10) || '2. Run and verify the workflow.' || char(10) || char(10) ||
    '### Prompts / Scripts / Configs' || char(10) || '[Validated assets are linked from the vault note.]' || char(10) || char(10) ||
    '### Inputs / Outputs' || char(10) || '- Inputs: [task inputs]' || char(10) || '- Outputs: [validated artifact]' || char(10) || '- Cost: [measure per run]' || char(10) || '- Time: [measure per run]' || char(10) || '- Hardware: [workflow dependent]' || char(10) || char(10) ||
    '## Fallback Method' || char(10) || fallback_method || char(10) || char(10) ||
    '## Known Failures' || char(10) || '[Add verified failure modes here.]' || char(10) || char(10) ||
    '## Last Validated' || char(10) || '- Version: 1' || char(10) || '- Date: 2026-07-21' || char(10) || '- Changelog: Initial seed',
    preferred_method,
    fallback_method,
    'Initial seed',
    '{}',
    'system:seed',
    'active',
    CURRENT_TIMESTAMP
FROM skills
WHERE id IN (
    'webinars_voice_qwen9b', 'webinars_voice_elevenlabs', 'webinars_video',
    'webinars_slides', 'webinars_script', 'infrastructure_runpod_deploy'
);
