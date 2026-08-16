# Knowledge — skills, Synapse graph, memory, vaultgraph, repomap, filesearch

Six subsystems that together give agents durable, versioned instructions; a fact graph
with role-separated promotion; conversation-scoped recall; and two complementary code/
file discovery tools. All are read-mostly context sources for the execution layer and,
in V2, for the reliability analyzer/department/CTO passes (`archdocs.load_arch_context`
+ `repomap.repo_map_for_task` are both designed to prepend compact, relevant blocks to
agent prompts).

## Skills library (`omniagentos/skills/`, migration `032_skill_library.sql`)

Vault-backed, versioned instruction storage: `skills` (slug, category, title,
`vault_note_path`, `current_version`) → `skill_versions` (content snapshot,
`change_reason`, `evidence_json`, `status`) → `update_proposals` (risk `low|model|
major`; `low` auto-approves via `decide_proposal()`, `model`/`major` need an explicit
call). The vault note (`vault/playbook/skill-*.md`) is the SOURCE OF TRUTH —
`index_vault_playbook()` scans and upserts; editing the `skills` table directly without
updating the note breaks the contract. V2's "confirmed fix → reusable skill" writeback
(`reliability/memory.py`, design §3) routes through this same `propose_update()` path,
extending `evidence_json` with `{proposal_id, audit_run_id, validation_results}`.

## Synapse / knowledge graph (`omniagentos/knowledge/`, PostgreSQL + pgvector)

Facts live in Postgres (`OMNIAGENTOS_KNOWLEDGE=1`, migration-031+ required; a no-op
everywhere if disabled or schema absent — recall never fails a run). Role separation
IS the promotion boundary (ADR-005): `knowledge_agent` (read-only, `~/.pgpass`) vs.
`knowledge_admin` (write, `var/secrets/knowledge-pg.env`, 0600, never agent-accessible)
— `PromotionGate` in Python is defense-in-depth only; the real gate is the DB grant.
Agent-ingested facts land quarantined (`trust <= 0.6`), forcing consolidator review
(001_init.sql floor trigger). Embedding dimension is FROZEN at 1024-d (`bge-m3`);
changing models needs a re-embed migration, no runtime fallback.

## Memory layer (`omniagentos/memory/`, migration `031_project_hierarchy.sql`+)

Assembles context from the frozen `conversations` table (role/meta/turns) +
recalled facts. `OMNIAGENTOS_MEMORY=1` gates conversation-history injection; pre-031
or disabled = no-op. Recall budget defaults to 800 tokens, HARD capped — facts drop
from the end of the recalled list past `budget_tokens * 3.5` chars (no graceful
overflow). Recall happens ONCE per run.

## VaultGraph (`omniagentos/vaultgraph/`)

Parses the Obsidian vault into a queryable graph (entity/concept nodes, edges) for
fact classification and contradiction detection, stored separately in
`vaultgraph.db` (not `omniagentos.db`). `classify_fact()` is the extension point for
new edge types (e.g. V2's planned "contradiction → arbitration panel" hook).

## Repomap (`omniagentos/repomap/`)

Aider-style repo map: `tags.py` extracts per-file definitions/references (Python/JS/TS
extractors; adding a language means a new `extract_<lang>()` following the same
`FileTags` shape), `ranking.py` runs personalized PageRank over the dependency graph
(converges in 60 iterations at 1e-8 tolerance — very large/deep repos may need more),
`service.py::build_repo_map(repo_dir, focus_files, focus_terms, max_tokens)` renders a
budget-fit signature map (`_CHARS_PER_TOKEN = 4` heuristic), `context.py::
repo_map_for_task(working_dir, task_text, max_tokens)` wraps it for prompt injection.
Dependency-free, CLI via `python -m omniagentos.repomap <repo> [--focus] [--terms]
[--tokens]`.

## Filesearch (`omniagentos/filesearch/`)

Federated file search: local+iCloud via Spotlight/`mdfind` (default, always on) plus
opt-in Google Drive/Dropbox walks. `curated_roots()` is opt-in BY PATH (missing a
folder means it's not searchable). Default cap `OMNI_FILESEARCH_MAX_FILES` (100k; media
rows are metadata-only, and personal + cloud roots walk BEFORE code checkouts so they
win under the cap); first full index runs ~45 docs/sec.
`com.omniagentos.filesearch-index` (every 7200s) keeps the catalog current;
`docs/architecture/` is an indexable root via the repo scope (no extra config
needed for these living docs to be searchable).

Every catalog row carries mtime, size, a derived `category`
(documents|spreadsheets|presentations|images|video|audio|code|archives|other — ONE
mapping table, `catalog._CATEGORY_BY_EXT`) and a `root` label
(desktop|icloud|gdrive|repo|other-mount). HTTP surface (all token-gated, error
envelope `{"error":{code,message,detail}}`):

- `GET /api/filesearch?q=` — live federated search (unchanged legacy shape, `hits`
  plus the uniform `rows`); adding any of `root=`/`category=`/`sort=recency|name`
  switches to a metadata-filtered catalog listing (`rows` only, `mode:"catalog"`).
- `GET /api/filesearch/semantic?q=&root=&category=&limit=` — DEEP chunk-level
  semantic search: `file_embeddings` (pgvector, knowledge Postgres, migration 004 —
  its own table, never mixed into facts), ~1500-char chunks, incremental by
  (path, mtime), embedded by the LOCAL Ollama bge-m3 (1024-d, same as Synapse;
  nothing leaves the machine). Rows: `{path, root, category, mtime, score, excerpt}`.
  The 2h index job runs the deep pass under a ~10-min budget with a resume cursor
  (`var/filesearch/semantic_cursor.json`).
- `POST /api/filesearch/reveal` `{path, root?, app:"finder"}` — Finder reveal with an
  INDEX-MEMBERSHIP floor (client paths must be byte-for-byte catalog rows; kill
  switch `OMNIAGENTOS_DISABLE_LOCAL_REVEAL`).
- `POST /api/filesearch/reindex`, `GET /api/filesearch/stats` — unchanged (stats now
  breaks down by root/category).

Extraction coverage for the deep layer: direct-read text/markdown/csv; `textutil` for
rtf/doc/docx/html/odt; PDFs only when `pdftotext` is installed (else name-only via the
catalog). Dataless cloud files are never read (no forced downloads).

## Living architecture docs (`omniagentos/archdocs/`, this file's own package — §8)

`generate.py` scans routes/migrations/launchd/packages into delimited
`<!-- generated:begin/end -->` blocks inside `ARCHI.md`; `context.py::
load_arch_context(focus_terms, max_tokens)` ranks `ARCHI.md` + `docs/architecture/*.md`
sections by term overlap (same budget-fit idea as repomap, applied to prose);
`staleness.py` embeds a `git HEAD / max migration / route count` stamp and flags
drift; `update.py` is the ONLY agent-facing write path, and it is Tier S (see
`governance.md`) — it always preserves the on-disk `## Notes (human)` section
verbatim and is only ever called by the pipeline applying an ALREADY-APPROVED docs
improvement, never as a direct agent action.

## How discovery composes

A reliability analyzer or CTO-review prompt (see `reliability.md`, `organization.md`)
is expected to call BOTH `repo_map_for_task()` (code structure) and
`load_arch_context()` (subsystem narrative) before drafting a root-cause / proposal —
faster RCA, less hallucinated architecture. Neither call executes code or touches the
DB; both are pure functions over the filesystem.

## Notes (human)
