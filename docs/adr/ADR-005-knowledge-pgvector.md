# ADR-005: Knowledge subsystem on Postgres+pgvector; DB-enforced promotion boundary

**Status:** accepted · 2026-07-12 · run `20260712-1251-synapse-h3` · Blueprint Horizon 3

## Decision
The knowledge subsystem ("Synapse" — `omniagentos/knowledge/`) is the first component
to cross the line ADR-001 drew: it is backed by **Postgres + pgvector**, a second
database alongside H1's SQLite. SQLite remains the runtime/durable-execution store
(runs, tasks, events, ledger); Postgres holds only the knowledge graph (`episodes`,
`entities`, `facts`, `edges`, `recall_log`, `kb_meta` — schema frozen in
`omniagentos/knowledge/migrations/001_init.sql`). Two Postgres roles — `knowledge_agent`
and `knowledge_admin` — separate what a running agent can do from what only the offline
consolidator/operator can do, enforced by PostgreSQL grants and a trigger, not by
Python convention.

## Why (the ADR-001 trigger firing)
ADR-001 named its own upgrade trigger: *"Adopt Postgres/pgvector when concurrent
writers exceed WAL comfort or governed semantic retrieval lands (Horizon 3)."*
`docs/research/knowledge-subsystem-horizon3.md` is that research: hybrid vector +
full-text + graph recall needs (a) a real ANN index (HNSW over `halfvec`), (b) a
`tsvector` full-text leg, and (c) recursive-CTE 2-hop spreading activation — all in
one round trip, fused with Reciprocal Rank Fusion. SQLite has none of pgvector's ANN
indexing and no native full-text ranking competitive with Postgres GIN/tsvector;
bolting an ANN library onto SQLite for one subsystem is more surface area than
running a second, purpose-built database for exactly this workload. The knowledge DB
stays logically separate from the H1 runtime DB deliberately — a slow/locked
knowledge query must never be able to stall run durability, and the two have
completely different consistency needs (knowledge is append-mostly + eventually
consolidated; runs need immediate durable checkpoints).

## The role-separation security model (the REAL boundary is the grant set)
Design review (F1, pre-fan-out) flagged the obvious failure mode: an agent that can
read its own knowledge base and also write to it can promote its own claims to
"trusted," closing a self-reinforcing hallucination loop with no external check. The
fix is **PostgreSQL role separation**, not a Python-level check an agent's own code
path could route around:

- `knowledge_agent` (the runner's role, used for recall + reflection writes) has
  `SELECT`/`INSERT` on `episodes`/`entities`/`facts`/`edges`/`recall_log`, and
  **column-scoped** `UPDATE` limited to `facts.(access_count, last_accessed,
  helped_count)` and `edges.weight` — pure Hebbian bookkeeping. It has **no** grant to
  update `facts.status`/`trust`/`importance`/`embedding`/`invalid_at`/
  `superseded_by`, no grant to update `entities`, and no `DELETE` anywhere.
- A `BEFORE INSERT` trigger (`facts_agent_insert_floor`, keyed on
  `current_user = 'knowledge_agent'`) additionally clamps every agent-authored
  fact to `status='quarantined'`, `trust<=0.6`, `importance<=0.6`,
  `confidence<=0.7` — an agent literally cannot construct an INSERT that lands
  active or high-trust, regardless of what its own code claims.
- `knowledge_admin` (the offline consolidator + the operator-token-checked API
  route) has full table grants and is the only role that can execute
  `UPDATE facts SET status='active' ...` (promotion) or set an embedding directly.
- The Python `PromotionGate` (`contracts.py`) is **defense-in-depth above** this,
  not the boundary: it is constructible only via `consolidate.gate()` or the
  operator-token API route (a private sentinel token), so nothing on an
  agent-reachable code path can even construct one. But the enforced boundary —
  proven by `tests/knowledge/test_promotion_gate.py` attacking a real
  `knowledge_agent` psycopg connection directly, forged gate in hand — is the
  database grant set. If the Python object were ever bypassed entirely, the SQL
  `UPDATE` still fails.

This means the promotion predicate (R2-7: >=2 distinct source types, or one
system-verified run outcome, and zero unresolved contradictions) only ever executes
inside the offline consolidator running as `knowledge_admin` — never inline in an
agent's own session. The consolidator's promotion/merge/decay/adjudication/backfill
passes are **implemented and tested** (`consolidate()`; `tests/knowledge/test_consolidate.py`
drives real seeded facts and a rogue-consolidator mutation fails a test). The predicate is
forgery-resistant because a trust CLASS is keyed on WHO AUTHORED a fact (`facts.author_role`, forced to `current_user`
by migration 003's trigger — an agent cannot spoof the connecting role), NOT on the source
label of the episode it references. An AGENT-authored fact contributes ZERO trust classes;
only NON-agent (admin/operator/system) facts count, each by its episode source. Promotion
requires >=2 DISTINCT NON-AGENT source classes, so an agent can never supply even the tipping
class — neither by self-corroborating (run/chat/web all agent-authored → 0 classes) nor by
echoing a single trusted statement (a single operator source + an agent echo stays
quarantined; only TWO independent admin/operator sources promote). A promoted fact is
RE-EMBEDDED from its statement at promotion, FAIL-CLOSED: both `consolidate()` and the
operator promote route re-embed with the recall embedder and DEFER/refuse promotion when it
is unavailable rather than promote an unverified vector — so no agent-crafted embedding
reaches active-recall ranking (the scheduled consolidator uses the same Ollama embedder recall
queries in, never a fake one). NOTE: an agent's verbatim echo of a doubly-non-agent-corroborated
statement CAN itself promote (its content is genuinely corroborated and re-embedded) — the
guarantee is that no FABRICATED (uncorroborated) content and no agent-chosen embedding ever
reaches active, not that `author_role='knowledge_agent'` never appears in an active row. This closed three distinct
self-promotion primitives found across re-review rounds, each with a live regression test:
(1) an agent self-corroborating across its own run+chat+web episodes — one class
(`test_agent_run_chat_web_cannot_self_promote`); (2) an agent-forged co_occurs/refines edge
to a trusted fact — edges are NOT counted for promotion at all, only for recall graph-spread
(`test_agent_forged_edge_to_trusted_fact_cannot_promote`); (3) an agent pointing a fabricated
fact at a trusted human/vault/curator EPISODE to borrow its label — blocked at the DB by
migration 003's belt-and-braces trigger (an agent fact may not reference a non-agent episode)
AND neutralized by author-keying (`test_agent_borrowed_trusted_episode_cannot_promote`).
Because `_has_system_verified_outcome` is still a stub, the only live auto-promotion path is
genuine admin/operator corroboration — agent-only knowledge stays quarantined until an
operator promotes or corroborates it. Consequence: with the
"system-verified run outcome" branch still scaffolded (`_has_system_verified_outcome`
returns False pending the H1 runs-table read), the ONLY live auto-promotion path today is
genuine admin/operator corroboration (a `human`/`vault`/`curator` episode) — agent-only
knowledge stays quarantined until an operator promotes it or corroborates it. That is the
intended fail-safe posture, not a gap.

## The honesty boundary (what this does NOT protect against)
This is a **same-uid, no-container host** (Horizon 1-3; the container gate lands at
H4+ per the blueprint). The role separation stops an agent from promoting its own
facts *through the knowledge subsystem's own API surface* — it does not stop an
agent holding an APPROVED raw-shell grant from reading `var/secrets/knowledge-pg.env`
(the file carrying the `knowledge_admin` password) or another process's environment,
and then connecting to Postgres directly as `knowledge_admin`. `~/.pgpass` on this
host carries **only** the `knowledge_agent` credential precisely to shrink that
residual; the admin/superuser secrets live only in `var/secrets/knowledge-pg.env`
(mode 0600), and the knowledge database logs all mutating statements
(`log_statement='mod'`) so credential-theft promotion is at least auditable after
the fact. No "provably inaccessible" claim is made here — this is a documented,
accepted residual until the H4+ container gate gives agents a real security boundary
(separate uid/namespace) instead of a database-grant one. `contracts.py`'s own
header comment carries this same note so it travels with the frozen contract, not
just this ADR.

## The 1024-d-only embedding decision
`kb_meta.embed_dim` is seeded at migration time to `1024` (`ollama:bge-m3`,
`omniagentos/knowledge/embeddings.py:OllamaEmbedding`) and `KnowledgeStore.__init__`
raises `KnowledgeError` on any provider whose `.dim` disagrees — there is **no
runtime fallback or auto-detect**. Facts table `embedding halfvec(1024)` is a fixed
column type, not a generic vector; mixing dimensions in one HNSW index is either
impossible or silently wrong depending on pgvector version, and a system that
silently truncated/padded vectors would produce recall scores nobody could reason
about. Switching embedding models (dimension OR semantics) is therefore always an
explicit, versioned re-embedding migration: bump `kb_meta.embed_model`/`embed_dim`,
re-embed every fact via the consolidator's backfill pass
(`store.facts_missing_embedding` + `set_embedding`, admin-only), never a config flag
flipped underneath live data.

## Backup/restore runbook
Postgres roles are **cluster-level**, not part of any single database, so a plain
`pg_dump omniagentos_knowledge` backs up schema+data but **not** the
`knowledge_agent`/`knowledge_admin` roles or their passwords.

- **Routine backup:** `pg_dump omniagentos_knowledge > knowledge.sql` (schema+data)
  **plus** `pg_dumpall --roles-only > roles.sql` (captures `CREATE ROLE ... LOGIN`
  and the `ALTER ROLE ... WITH PASSWORD` set by `pg-auth-setup.sh`). Keep both;
  restoring only the first loses the passwords.
- **Restore, roles-only dump available:** restore `roles.sql` first (repopulates
  both roles with their prior passwords and the scram entries already match
  `pg_hba.conf`), then restore `knowledge.sql` normally. No further action needed.
- **Restore, roles-only dump NOT available (data-only restore):** `001_init.sql`'s
  `CREATE ROLE ... LOGIN` statements are idempotent (`EXCEPTION WHEN
  duplicate_object THEN NULL`) and run again the next time `migrate.py` applies —
  but a freshly `CREATE ROLE`'d role has **no password**, so it reverts to
  passwordless (trust-auth-only) even on a cluster whose `pg_hba.conf` still says
  `scram-sha-256`, meaning it simply **cannot authenticate** until a password is
  set. The fix is exactly the ordered sequence the security hardening already
  documents: re-run `scripts/knowledge/pg-auth-setup.sh`, which is idempotent,
  regenerates fresh passwords for `knowledge_agent`/`knowledge_admin`/the
  superuser, rewrites `var/secrets/knowledge-pg.env` (0600), and re-verifies the
  `pg_hba.conf` scram flip is in place (it no-ops if already flipped). Do this
  **before** pointing the runner/API back at the restored database — an unflipped
  or passwordless window is the same local-trust-auth exposure ADR's design review
  already flagged.
- Migration order is unaffected by restore: `migrate.py` always runs first (schema
  + roles + grants, privileged/superuser), `pg-auth-setup.sh` always runs after
  (passwords + the trust→scram flip) — restoring mid-sequence just means re-running
  from wherever the restored state actually landed.

## Consequences
A second running database is now part of the deploy story (H1's "one operator, one
machine, SQLite-only" story from ADR-001 no longer covers everything). The
per-worktree isolated test DB (`tests/knowledge/conftest.py`) and `OMNIAGENTOS_
REQUIRE_PG` make the suite fail loudly instead of silently skipping when Postgres
is down, so this dependency cannot regress unnoticed (`tests/knowledge/
test_e2e_real.py` additionally proves the REAL chain end-to-end with a live Ollama
embedder — the H2 anti-fake safeguard for this subsystem). Operationally: Postgres
must be running and migrated (`migrate.py` before `pg-auth-setup.sh`, see README)
before `OMNIAGENTOS_KNOWLEDGE=1` is turned on; with the flag off, every knowledge
call degrades to a no-op (`safe_recall_block`/`safe_record_helped` never raise) so
existing H1/H2 behavior is unaffected by default.

## Known residuals — H3.1 hardening backlog (adversarial council, honest scope)
The core subsystem is secure (F1/R2 boundary + migration 002 hardening, verified live) and
functional (recall, ingestion, the promotion loop, the wired dashboard, the real-components
e2e — all green). The following are **known, documented** production-at-scale / hardening
items deferred to H3.1, none of which is a dev-exploitable defect at single-operator scale
with the default flag-off posture:
- **Recall perf at scale**: the `perf` gate measures `lean` mode + a fake embedder; production
  default is `full` mode + real Ollama (measured ~145 ms embed + ~170 ms full-retrieve on a
  300k-edge graph ≈ ~315 ms, under the 800 ms budget but with less headroom than the gate
  implies). There is no edge/importance pruning yet, so the graph grows via Hebbian
  accumulation. H3.1: add a full-mode+dense-graph perf case and an edge-weight-floor prune.
- **recall_log retention**: `record_recall` inserts per run with no retention; `stats()`
  full-scans the table. H3.1: a 30-day retention job + a time-bounded stats aggregate.
- **Hebbian write batching**: co-occurrence strengthening does N separate commits (capped at
  28/run); a single batched call is ~3x faster. H3.1: a batch `strengthen_pairs`.
- **Post-scram test harness**: `conftest.py` bootstraps via passwordless-superuser
  (`postgresql://localhost/postgres`); once `pg-auth-setup.sh` flips to scram it must source
  `var/secrets`. H3.1: have conftest read `migrate_dsn()`/`admin_dsn()` when present.
- **Admin-cred file exposure**: on this same-uid host `var/secrets/knowledge-pg.env` (admin +
  superuser) is readable by a shell-capable agent — the accepted same-uid residual above; the
  refinement is to move the admin/superuser secrets outside the repo tree (root-owned or a
  distinct service account) so they are not repo-relative. Superseded by the H4+ container gate.
