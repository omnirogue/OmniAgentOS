# S14D/L14 handoff: migration 072

Ownership: S14D / consolidated L14. This is a prerequisite artifact consumed by
the L06 long-haul repair and must not be re-created under another migration
number by B04 or B07.

Merge order:

1. Consolidated L14 through migrations 068–071 (`1fc796298da77c1e66cd8d8f378166c74d45b8b3`).
2. The exact `072_longhaul_provider_harnesses.sql` object from this artifact.
3. The L06 logic commit (`a28cce435db063b6fbc2eb6b95a7176bb02f3bf7`), if it is not already present.

The migration runner scans every unapplied version, so an operator database that
temporarily saw 072 before 068–071 will still apply those missing versions.
After consolidated migration 068 is present, its checksum policy records or
backfills 072 like every other applied migration.

Frozen file identities:

- Historical `043_longhaul.sql` SHA-256:
  `6c0d5310061b35805f8fe8396419b383f478d922112c9b7a8c54f38b4ac7e0bb`
- Forward `072_longhaul_provider_harnesses.sql` SHA-256:
  `573cc39d8b4631228aab02881a6ee2b813b92fb447d8f886e60265db6708e8f2`

Migration 043 remains byte-identical. Migration 072 rebuilds only
`task_sessions`, preserving all eleven columns, the `(board_task_id, seq)`
unique constraint, the end-reason constraint, and all three explicit indexes.
The sole schema expansion is the `harness` CHECK for `cli-grok`, `cli-gemini`,
and `cli-kimi`.

Acceptance evidence is in
`tests/longhaul/test_provider_migration_072.py`: exact file checksums,
preexisting-row/column/index/constraint preservation, repeat-safe migration,
pre-072 fail-closed dispatch, and post-072 public dispatch through all three
real adapters for both provider-specific usage and authentication outcomes.

Do not modify 043, renumber 072, or create a divergent provider-harness
migration in B04/B07. Update the consolidated migrations README to claim
060–072 (next version 073) when this artifact is integrated onto the L14 tip.
