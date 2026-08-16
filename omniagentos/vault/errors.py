"""Exceptions raised by omniagentos.vault."""

from __future__ import annotations


class VaultError(Exception):
    """Base class for all omniagentos.vault errors."""


class VaultGitGuardError(VaultError):
    """Raised when the auto-commit hard guard detects a staged path outside the
    vault directory (contracts/vault-frontmatter.md: "Commits touch vault/ paths
    ONLY — anything else staged is a bug (guard in code)"). The commit is refused;
    nothing is committed when this is raised."""
