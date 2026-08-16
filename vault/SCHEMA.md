---
id: schema
type: source
discipline: null
created: 2026-07-22T18:30:00Z
source_run: null
confidence: high
status: active
supersedes: null
---

# Vault Schema — Agent-Maintained Knowledge Conventions

[[Home]]

This document defines the frozen contract for agent-maintained markdown notes in the vault. All note-generation code, templates, and LLM wiki-update prompts use these conventions.

## Frontmatter Contract

Every note MUST begin with an 8-field YAML frontmatter block (see `omniagentos/vault/frontmatter.py` for the frozen contract):

```yaml
---
id: <kebab-case-id>
type: <NoteType enum value>
discipline: <kebab-case discipline slug or null>
created: <ISO 8601 Z-format timestamp>
source_run: <run ID or null>
confidence: <"low" | "medium" | "high" | null>
status: <"active" | "superseded" | "draft">
supersedes: <id of superseded note or null>
---
```

### Field Definitions

- **id**: Unique kebab-case identifier per note type + directory. No spaces, no special characters except hyphens. Examples: `home`, `run-run_abc123`, `webinars_voice_elevenlabs`.
- **type**: One of the frozen `NoteType` enum values: `run`, `experiment`, `learning`, `failure`, `decision`, `benchmark`, `discipline`, `source`, `tournament`, `leaderboard`, `playbook`, `prompt`, `briefing`.
- **discipline**: Kebab-case slug linking to a discipline hub (e.g. `webinars`, `research-briefs`, `general`), or `null` if not applicable.
- **created**: ISO 8601 timestamp with Z (UTC) suffix, e.g. `2026-07-22T18:30:00Z`. Hand-written frontmatter may omit quotes; pyyaml's implicit datetime resolver is coerced back to canonical string form.
- **source_run**: The run ID that generated this note (e.g. `run_abc123def456`), or `null` for hand-authored/external notes.
- **confidence**: Trust level (`low`, `medium`, `high`) or `null` if not ranked. Used by LLM and curator workflows to filter recall.
- **status**: `active` = current knowledge, `superseded` = replaced by another note (link via `supersedes`), `draft` = not yet published/decided. All new notes default to `active` unless authored as draft.
- **supersedes**: The `id` of the note this one replaces, or `null`. Both notes remain in the vault; the old one's status becomes `superseded`.

## Directory Taxonomy

Each directory has a defined purpose and naming convention for contained notes:

| Directory | Type | Purpose | ID Pattern |
|-----------|------|---------|-----------|
| `runs/` | run | One note per completed run, organized by run ID (flat or by date). | `run_<run_id>` (backfilled into run notes' `id` field) |
| `learnings/` | learning | Durable insights or patterns from runs, experiments, or external sources. | Kebab-case slug, e.g. `adapter-context-limits`. |
| `failures/` | failure | Root-cause post-mortems, error patterns, gotchas. | Kebab-case, e.g. `sandbox-traversal-escape`. |
| `decisions/` | decision | Architecture decisions (ADR-style), trade-offs, policy decisions, gate evidence. | Kebab-case, e.g. `vault-frontmatter-frozen-contract`. |
| `playbook/` | playbook | Validated strategies, reusable workflows, skill library entries. | Skill ID or pattern slug. |
| `capabilities/` | source | Orchestration capabilities, tool/model features discovered/validated. | Kebab-case descriptor, e.g. `claude-web-browsing`. |
| `models/` | source | Model metadata: pricing, latency, capabilities, benchmark evidence. | Model name (e.g. `claude-opus-4-1`). |
| `benchmarks/` | benchmark | Eval suite results, metric diffs, scorecard snapshots. | Benchmark name or ID. |
| `disciplines/` | discipline | Hub/index notes for a vertical (research, webinars, infra, etc.). | Discipline slug. |
| `orchestration/` | source | System design, runner behavior, council/orchestrator patterns. | Kebab-case topic. |
| `servers/` | source | Server inventory, SSH recipes, deployment targets, firewall rules. | Server name or cluster slug. |
| `sources/` | source | External knowledge (docs, research papers, vendor info). | Source name or URL slug. |
| `briefings/` | briefing | Daily/periodic briefings composed by the steward system. | Date-based slug (e.g. `2026-07-22`). |
| `experiments/` | experiment | Lab experiments, controlled A/B tests, ablations. | Experiment UUID or slug. |
| `conversations/` | source | Chat history, dialogue trees, example exchanges with agents. | Kebab-case topic or participant ID. |
| `artifacts/` | source | Generated outputs: blueprints, configs, runbooks, code snippets. | Artifact name or project+target. |
| `leaderboard/` | leaderboard | Ranked log of top orchestrations (tournaments, best runs, winning configs). | Leaderboard name or tournament ID. |
| `prompts/` | prompt | Versioned system prompts, instruction refinements, prompt experiments. | Prompt name + version. |
| `templates/` | (not notes) | Jinja2 templates (.j2 files) used to render notes. Not versioned in Git. | Template name (e.g. `run_note.md.j2`). |

## Linking Rules

### Wikilinks

Use double-bracket syntax: `[[id-slug]]` or `[[id-slug\|Display Text]]` to reference other notes.

- **Every new page must be reachable from [[Home]] or a discipline hub** (listed in Home.md's Map section or the discipline's hierarchy).
- **Link intent**: Use links to create a graph of related concepts, not to list every mention. Link to foundational/reference notes, gate evidence, and prerequisite learnings.
- **Cross-discipline links**: Link between disciplines sparingly; group related concepts at the discipline level or in `orchestration/`.
- **Supersession**: A superseded note's `supersedes: <id>` field creates an implicit backlink to the new note. Always add a forward reference from old→new in the body if human-authored.

### Duplication vs. Update

- **Update existing notes** over creating near-duplicates. If a page is 95% the same as an existing one, edit the existing one.
- **Use `supersedes`** when a note is fundamentally replaced (e.g., a decision is overturned, a learning is corrected). Both remain in vault; old one's status → `superseded`.
- **Create a new note** only when the topic is genuinely distinct or the existing note covers a different scope/time period.

## Update Rules

### Status Accuracy

Keep status in sync with note state:
- `active`: Currently trusted, used in recall/orchestration.
- `superseded`: Replaced by another note; retained for history/audit.
- `draft`: Not yet finalized (decision not decided, learning unvalidated). Draft notes are not returned by recall queries.

### One Concept Per Page

Each note should represent one durable concept (one decision, one pattern, one gotcha, one model capability). If a run touches multiple independent learnings, create multiple learning notes and link them from the run note.

### Backfilling Fields

- **source_run**: Set by run-note generation code; null for hand-authored notes or external sources.
- **created**: Set at note creation time; never updated. Reflects when the concept was first captured.
- **discipline**: Set based on the directory and/or task metadata; null if cross-cutting.

## Run Notes

Run notes are auto-generated by `omniagentos/vault/run_note.py` and follow the template in `vault/templates/run_note.md.j2`:

- **ID**: Derived from run ID (e.g., `run_abc123`).
- **Type**: Always `run`.
- **Frontmatter**: Populated from run dict, task metadata, and receipts.
- **Sections**: Summary (state, harness, model, timing), Output, Usage, Input, Steps, Artifacts, Provenance.
- **Extracted (auto)**: Machine-owned section containing back-links to concept notes extracted from this run (added by post-run wiki-update step if enabled).
- **Notes (human)**: Operator-authored annotations — never auto-populated.

After a run note is written, the post-run wiki-update step may create/link durable knowledge notes (learnings, failures, decisions) if the run produced insights. Back-links to extracted concepts are added to the "## Extracted (auto)" section, keeping "## Notes (human)" as pure operator territory.

## Artifact and Config## Artifact and Config Files

Template files (`.j2`) are NOT version-controlled as notes — they are rendered at code time. The Jinja2 source is the authority; generated notes carry the rendered output.

Configuration and contracts files (`contracts/vault-frontmatter.md`, `omniagentos/contracts.py`) define the frozen enums and schemas. Changes to frontmatter fields MUST be reviewed council-style (G1 criterion B7 — frozen contract).

## Notes (human)

Last updated by schema-definition run.
