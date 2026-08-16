-- Migration 003 — un-forgeable authorship (adversarial re-review: 3rd self-promotion primitive)
-- The promotion predicate must key trust on WHO authored a fact, not on the source LABEL of
-- the episode it references — because facts.episode_id is agent-settable and unclamped, an
-- agent could point a fabricated fact at an existing human/vault/curator episode and borrow
-- its "trusted" source class (proven live: fabricated content promoted to active). The only
-- thing an agent cannot spoof is current_user, so we stamp author_role on every row.
-- No CREATE TABLE (ALTER only) — the migrate CREATE-without-GRANT guard does not apply; the
-- new columns inherit the tables' existing role grants, which is correct (agent may write its
-- own facts/episodes; the trigger forces author_role = current_user so it cannot lie about it).

ALTER TABLE facts    ADD COLUMN author_role text NOT NULL DEFAULT 'system';
ALTER TABLE episodes ADD COLUMN author_role text NOT NULL DEFAULT 'system';

-- Backfill existing rows: they predate authorship; treat them as system/admin-authored
-- (they were only ever written by privileged paths before this migration).
UPDATE facts    SET author_role = 'knowledge_admin' WHERE author_role = 'system';
UPDATE episodes SET author_role = 'knowledge_admin' WHERE author_role = 'system';

CREATE FUNCTION set_author_role() RETURNS trigger AS $$
BEGIN
  NEW.author_role := current_user;  -- un-spoofable: the connecting role, not a client value
  RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER facts_set_author    BEFORE INSERT ON facts    FOR EACH ROW EXECUTE FUNCTION set_author_role();
CREATE TRIGGER episodes_set_author BEFORE INSERT ON episodes FOR EACH ROW EXECUTE FUNCTION set_author_role();

-- Belt-and-braces (re-review recommendation): structurally forbid an agent fact from
-- referencing an episode it did not author, killing the "episode borrow" in Postgres, not
-- just in the Python predicate. Legit agent ingest always creates its own episode then the
-- fact in the same path, so this never breaks the real flow.
CREATE FUNCTION forbid_agent_episode_borrow() RETURNS trigger AS $$
DECLARE ep_author text;
BEGIN
  IF current_user = 'knowledge_agent' THEN
    SELECT author_role INTO ep_author FROM episodes WHERE id = NEW.episode_id;
    IF ep_author IS NOT NULL AND ep_author <> 'knowledge_agent' THEN
      RAISE EXCEPTION 'knowledge_agent fact may not reference an episode authored by %', ep_author
        USING ERRCODE = 'insufficient_privilege';
    END IF;
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;
CREATE TRIGGER facts_agent_no_episode_borrow BEFORE INSERT ON facts
  FOR EACH ROW EXECUTE FUNCTION forbid_agent_episode_borrow();
