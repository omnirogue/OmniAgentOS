# Vault contract (FROZEN, Wave 0)

The vault is the human-readable half of the system of record (blueprint §10): a
git-versioned Obsidian folder generated from the same events as the database.
G1 criterion B7 checks the EXACT frontmatter field set below.

## Layout

```
vault/
  Home.md                        # dashboard note (lead-owned skeleton; p05 updater refreshes stats block)
  disciplines/<slug>.md          # one per discipline
  runs/<yyyy>/<mm>/<run-id>.md   # one per run (generator: p05, called by runner/bench/wrapper)
  experiments/                   # H4 — folder exists, empty in H1
  learnings/  failures/  playbook/  prompts/  decisions/  benchmarks/  sources/
  templates/                     # p05-owned Jinja/py templates for note bodies
```

## Frontmatter (exact set, no extras, no omissions)

```yaml
---
id: run_ab12cd            # note id == entity id
type: run                 # contracts.NoteType
discipline: code-changes  # or null
created: 2026-07-11T15:30:00Z
source_run: run_ab12cd    # the run that produced this note; null for hand-written
confidence: null          # low|medium|high|null
status: active            # active|superseded|draft
supersedes: null          # id of the note this replaces, or null
---
```

Rendered/parsed by `omniagentos/vault` using contracts.VaultFrontmatter — no other
package writes vault files directly; they call p05's generator API.

## Body conventions

- H1 note title: `# <type>: <human title>`.
- Wikilinks connect entities (D-011): a run note MUST link `[[<discipline-slug>]]`
  (p05 generates a stub `disciplines/<slug>.md` on first reference so the link
  always resolves) and `[[Home]]`; the task title appears as plain text (no tasks/
  folder in H1). Benchmark notes link every run note they aggregate; decision notes
  link affected notes. No orphans: every generated note contains ≥1 resolving
  wikilink.
- Run note body sections (template): Summary (state, harness+arm, model, timings),
  Usage (tokens/cost with `estimated` flags SHOWN), Steps table, Artifacts list,
  Provenance (task id, trace id, manifest path).
- Machine-readable duplication is forbidden: the note POINTS to the ledger line
  (manifest_path) rather than embedding full JSON.

## Git behavior (p05)

- Auto-commit is flag-gated: env `OMNIAGENTOS_VAULT_AUTOCOMMIT=1` (default OFF, and
  OFF in tests). When ON: `git add vault/<changed paths> && git commit` with author
  `omniagentos-bot <00000000+omniagentos-bot[bot]@users.noreply.github.com>`, message `vault: <type> <id>`.
  Commits touch vault/ paths ONLY — anything else staged is a bug (guard in code).
- Never push. Never touch non-vault paths. Repo dirty state outside vault/ is
  ignored, not an error.

## Human-edit rule (blueprint §10)

Humans hand-edit only `learnings/` and `decisions/` (author recorded in git);
regenerating a system note overwrites system sections only — generator must
preserve a `## Notes (human)` section verbatim if present.
