# ADR-003: Vault lives in-repo, system-written, git-versioned

**Status:** accepted · 2026-07-11 · Blueprint §10

## Decision
The Obsidian vault is `vault/` inside this repository (not a separate repo): one
git history covers code, config, ledger, and the human-readable record. The system
writes the vault through ONE module (omniagentos/vault); frontmatter is the exact
8-field contract in contracts/vault-frontmatter.md (G1 criterion B7). The JSONL
ledger (`ledger/`) is tracked in git as well — manifests are append-only and diff
cleanly.

## Rules
- Auto-commit is flag-gated (`OMNIAGENTOS_VAULT_AUTOCOMMIT=1`, default OFF, OFF in
  tests): bot-author commits touching vault/ paths ONLY; never push.
- Humans hand-edit only `learnings/` and `decisions/`; generators preserve a
  `## Notes (human)` section verbatim.
- The vault is a projection of DB/ledger events — no data exists ONLY in the vault
  except human notes in the two hand-edit folders.

## Rejected alternative
Separate vault repo: cleaner commit history, but breaks the one-checkout operator
story and complicates wikilinks to run artifacts; revisit if vault churn ever
drowns code history (trigger: vault commits >80% of repo commits over a month).
