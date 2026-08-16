-- C11 / D-31 — bind a capability grant to a PROJECT, not just to a holder.
--
-- ORDINAL: 115, computed from the filesystem (highest prefix on disk was 114)
-- immediately before this file was written. Migrations are append-only and
-- byte-immutable: nothing above was renumbered or edited to make room.
--
-- WHY THIS EXISTS
-- D-31 requires a send grant to be bound to a HOLDER **and** a PROJECT. Until
-- now the standing-grant table named only a holder, so `lane:worker` holding
-- `gmail_acmeuni.send` for project A held it for project B, C, and every project
-- minted next week as well. Nothing in the broker could even ask the question,
-- because the fact was not recorded anywhere: cross-project reach was not
-- "allowed by policy", it was invisible.
--
-- `campaign_grants.project_id` (migration 058) already recorded the binding for
-- BOUNDED grants and nothing read it at authorization time either. So the two
-- halves of this change are: record the fact for standing grants (here), and
-- enforce it for BOTH kinds at the one chokepoint that decides a call
-- (`connectors/broker.py:authorize`).
--
-- NULLABLE, AND THAT IS NOT A LOOPHOLE
-- Every existing row gets `project_id = NULL`. NULL means "this grant proves no
-- project binding" and the broker treats it as exactly that: fail-closed, a
-- NULL-project grant does NOT authorize a call that names a project (denial
-- `grant_project_unbound`), and a project-BOUND grant does not authorize a call
-- that names no project (denial `call_project_unknown`). A backfill guess would
-- have been worse than a NULL: inventing a binding is inventing an
-- authorization, and the whole point of C11 is that a project-A grant must not
-- silently become usable by project-B work.
--
-- ONE BINDING PER (agent, capability), ON PURPOSE
-- `agent_capabilities` is keyed `(agent_id, capability_id)` and this migration
-- does not rebuild that key. So an agent that must act in two projects is
-- REBOUND (its row's project_id is overwritten by the next issuance), not
-- widened. Narrower than necessary is the correct direction for a first
-- landing: widening the key later is an additive migration, whereas shipping a
-- multi-project row set and discovering the enforcement was ambiguous is not
-- recoverable from an audit trail.
--
-- The request side gets the same column so the ASK carries the project it was
-- made for: `capability_requests.project_id` is what
-- `capability_decisions.cas_grant` copies onto the grant it writes, which makes
-- "which project was this capability granted for, and who asked?" one join
-- instead of a reconstruction.

-- Standing grants (migration 005 / 106).
ALTER TABLE agent_capabilities ADD COLUMN project_id TEXT REFERENCES projects(id);

-- The immutable request envelope (migration 113). The BEFORE UPDATE trigger on
-- that table is untouched: this column is written once, by the INSERT that
-- creates the envelope, and never rewritten.
ALTER TABLE capability_requests ADD COLUMN project_id TEXT REFERENCES projects(id);

-- "What does this project's fleet actually hold?" is the containment review
-- question, and it is the one query that has no index without this.
CREATE INDEX IF NOT EXISTS idx_agent_caps_project
    ON agent_capabilities(project_id, capability_id);

CREATE INDEX IF NOT EXISTS idx_capability_requests_project
    ON capability_requests(project_id, requested_at);
