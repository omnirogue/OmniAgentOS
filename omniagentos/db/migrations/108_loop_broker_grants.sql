-- Seed broker-side standing grants for the live loop callers migrated by U-R10.
--
-- INSTANCE_CAPABILITIES remains the parent-side source floor. These rows are a
-- separate broker-side fact: deleting one immediately denies the external call
-- even while the in-source floor still lists the capability.
--
-- ORDINAL: deliberately LAST of the phase-1 trio. 106 establishes the grant
-- shape these rows are written into, and 107 gives the broker somewhere to
-- record the calls these grants enable. Seeding the grants before either would
-- mean a halted migration run leaves loops authorized to make calls the broker
-- cannot yet audit -- which fails closed, but as a self-inflicted outage.

INSERT OR IGNORE INTO agents (
    id, name, lineage, model, expertise_json, trust_level, status, created_at, updated_at
) VALUES (
    'loop:render_probe',
    'loop:render_probe',
    'scheduler.loop_effects',
    NULL,
    '[]',
    'T1',
    'idle',
    CURRENT_TIMESTAMP,
    CURRENT_TIMESTAMP
);

INSERT INTO capability_grant_log (
    agent_id, capability_id, action, action_class, actor, note, ts
)
SELECT
    'loop:render_probe',
    'replicate.generate',
    'grant',
    'sandboxed_creation',
    'migration:108',
    'U-R10 broker-side loop seed',
    CURRENT_TIMESTAMP
WHERE NOT EXISTS (
    SELECT 1
    FROM agent_capabilities
    WHERE agent_id = 'loop:render_probe'
      AND capability_id = 'replicate.generate'
);

INSERT OR IGNORE INTO agent_capabilities (
    agent_id, capability_id, granted_at, granted_by, note
) VALUES (
    'loop:render_probe',
    'replicate.generate',
    CURRENT_TIMESTAMP,
    'migration:108',
    'U-R10 broker-side loop seed'
);

INSERT OR IGNORE INTO agents (
    id, name, lineage, model, expertise_json, trust_level, status, created_at, updated_at
) VALUES (
    'loop:flowers_collection',
    'loop:flowers_collection',
    'scheduler.loop_effects',
    NULL,
    '[]',
    'T1',
    'idle',
    CURRENT_TIMESTAMP,
    CURRENT_TIMESTAMP
);

INSERT INTO capability_grant_log (
    agent_id, capability_id, action, action_class, actor, note, ts
)
SELECT
    'loop:flowers_collection',
    'replicate.generate',
    'grant',
    'sandboxed_creation',
    'migration:108',
    'U-R10 broker-side loop seed',
    CURRENT_TIMESTAMP
WHERE NOT EXISTS (
    SELECT 1
    FROM agent_capabilities
    WHERE agent_id = 'loop:flowers_collection'
      AND capability_id = 'replicate.generate'
);

INSERT OR IGNORE INTO agent_capabilities (
    agent_id, capability_id, granted_at, granted_by, note
) VALUES (
    'loop:flowers_collection',
    'replicate.generate',
    CURRENT_TIMESTAMP,
    'migration:108',
    'U-R10 broker-side loop seed'
);
