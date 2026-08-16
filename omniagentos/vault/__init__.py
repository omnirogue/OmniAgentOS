"""omniagentos.vault — the human-readable half of the system of record.

Public API (contracts/interfaces.md §p05):
    render_run_note(run, steps, manifest_path, receipts, *, artifacts_list,
                    task_title, task_input) -> (relpath, content)
    write_note(vault_dir, relpath, content, *, autocommit=None) -> abs_path
    render_benchmark_note(bench_id, manifests) -> (relpath, content)
    update_home(vault_dir, stats) -> None
    parse_frontmatter(content) -> VaultFrontmatter

No other package writes vault files directly — they call these functions.
See contracts/vault-frontmatter.md for the frontmatter/body/git contract.
"""

from __future__ import annotations

from omniagentos.vault.benchmark_note import render_benchmark_note
from omniagentos.vault.errors import VaultError, VaultGitGuardError
from omniagentos.vault.frontmatter import parse_frontmatter, render_frontmatter
from omniagentos.vault.home import update_home
from omniagentos.vault.run_note import render_run_note
from omniagentos.vault.sections import NOTES_HUMAN_HEADING
from omniagentos.vault.write import write_note

__all__ = [
    "render_run_note",
    "write_note",
    "render_benchmark_note",
    "update_home",
    "parse_frontmatter",
    "render_frontmatter",
    "VaultError",
    "VaultGitGuardError",
    "NOTES_HUMAN_HEADING",
]
