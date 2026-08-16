-- NSG-021 / NSC-C31-05: per-attempt canonical tool-set identity.
ALTER TABLE swarm_attempts ADD COLUMN tool_set_digest TEXT
  CHECK(tool_set_digest IS NULL OR length(tool_set_digest) = 64);
CREATE INDEX idx_swarm_attempts_tool_set_digest
  ON swarm_attempts(tool_set_digest) WHERE tool_set_digest IS NOT NULL;

