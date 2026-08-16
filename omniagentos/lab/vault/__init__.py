"""omniagentos.lab.vault — the human-readable half of the H2 self-improvement
lab (contracts/lab-interfaces.md §L08-labvault, contracts/vault-frontmatter.md).

Public API:
    render_experiment_note(exp, results, scorecard) -> (relpath, content)
    render_tournament_note(tnm, matches, elo) -> (relpath, content)
    render_leaderboard_note(subject, rows) -> (relpath, content)      # the log-book
    render_playbook_note(discipline, entries) -> (relpath, content)
    render_prompt_note(surface, content) -> (relpath, content)

Every generator returns a vault_dir-relative path plus the FULL note content
(frontmatter + body); callers write it with H1's `omniagentos.vault.write_note`
(confined + 8-field frontmatter + human-section-preserving) exactly like the
H1 run/benchmark note generators — this package never writes to disk itself.
Notes wikilink experiments<->tournaments<->surfaces<->leaderboard<->playbook so
the vault stays a navigable graph (D-011); see each module's docstring for its
exact wikilink set. No note ever renders a held-out `expected` value (Section
11.7) — see `omniagentos.lab.vault.util.scrub_held_out`.
"""

from __future__ import annotations

from omniagentos.lab.vault.experiment_note import render_experiment_note
from omniagentos.lab.vault.leaderboard_note import render_leaderboard_note
from omniagentos.lab.vault.playbook_note import render_playbook_note
from omniagentos.lab.vault.prompt_note import render_prompt_note
from omniagentos.lab.vault.tournament_note import render_tournament_note

__all__ = [
    "render_experiment_note",
    "render_tournament_note",
    "render_leaderboard_note",
    "render_playbook_note",
    "render_prompt_note",
]
