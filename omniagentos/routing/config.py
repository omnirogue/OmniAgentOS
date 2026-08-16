"""Account-pool configuration -- typed load of ``configs/accounts.yaml``.

Pure config loading and filesystem discovery; no process/thread state lives
here (that's ``omniagentos.routing.account_pool.AccountPool``). Kept separate
so the pool itself can be constructed from either a loaded config or an
in-memory one built by a test, without touching disk.

Never resolves or reads credentials: `config_dir` is always treated as an
opaque filesystem path, passed straight through to the CLI as
``CLAUDE_CONFIG_DIR`` (see adapters/claude.py). This module never opens it.
"""

from __future__ import annotations

import glob
import os
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

DEFAULT_COOLDOWN_SECONDS = 60


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def config_path() -> Path:
    """Resolve configs/accounts.yaml cwd-independently.

    ``OMNIAGENTOS_ACCOUNTS_CONFIG`` overrides (same convention as
    ``OMNIAGENTOS_POLICY`` / ``OMNIAGENTOS_MODELINTEL_CONFIG``); otherwise
    anchored to the repo root so callers resolve the same file no matter what
    directory the process was launched from.
    """
    override = os.environ.get("OMNIAGENTOS_ACCOUNTS_CONFIG")
    if override:
        return Path(override)
    return _repo_root() / "configs" / "accounts.yaml"


class Account(BaseModel):
    """One subscription account for a provider.

    ``config_dir`` is a filesystem PATH only -- never a credential or token.
    The actual auth material lives inside that directory (e.g.
    ``~/.claude/.credentials.json``) and this model never reads it.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    config_dir: str
    enabled: bool = True
    priority: int = 0
    max_inflight: int | None = None
    cooldown_seconds: int | None = None  # None => provider default

    def resolved_config_dir(self) -> str:
        """``config_dir`` with ``~`` expanded, as a plain string path."""
        return str(Path(self.config_dir).expanduser())


class ProviderPool(BaseModel):
    """One provider's account list plus its pooling defaults."""

    model_config = ConfigDict(extra="forbid")

    cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS
    accounts: list[Account] = Field(default_factory=list)
    discover_glob: str | None = None


class AccountPoolConfig(BaseModel):
    """Validated representation of ``configs/accounts.yaml``."""

    model_config = ConfigDict(extra="forbid")

    providers: dict[str, ProviderPool] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _unique_static_ids(self) -> AccountPoolConfig:
        """Fail loudly on a duplicate id declared in the static config.

        Ids must be unique across the WHOLE file (not just within one
        provider) because ``AccountPool.report(account_id, outcome)`` looks an
        account up by id alone, with no provider argument -- and
        ``AccountPool`` keys its internal state flatly by id too, so a
        same-provider duplicate is just as unsafe as a cross-provider one.
        Discovery-time ids (derived from a directory basename) are
        de-duplicated separately in ``load_accounts_config`` and never raise
        -- a glob match is best-effort, not a config authoring mistake.
        """
        seen: set[str] = set()
        for pool in self.providers.values():
            for account in pool.accounts:
                if account.id in seen:
                    raise ValueError(
                        f"account id {account.id!r} is declared more than once; "
                        "account ids must be unique across the whole pool"
                    )
                seen.add(account.id)
        return self


def _discover_accounts(pattern: str) -> list[Account]:
    """Glob-discover extra accounts from directories matching ``pattern``.

    Discovered ids are derived from the directory's basename (leading dot
    stripped), e.g. ``~/.claude-account-3`` -> id ``claude-account-3``. That
    naming scheme can never collide with a hand-picked static id like
    ``account-1`` unless someone deliberately names a static account after a
    literal directory basename -- ``load_accounts_config`` de-duplicates by
    both resolved path and id regardless, so a collision is silently skipped
    rather than raised (discovery is best-effort).
    """
    discovered: list[Account] = []
    for index, match in enumerate(sorted(glob.glob(os.path.expanduser(pattern)))):
        path = Path(match)
        try:
            is_directory = path.is_dir()
        except OSError:
            # Account discovery is advisory. Sandboxed workers and locked-down
            # operator homes may allow globbing a name but deny stat(2) on it;
            # one unreadable candidate must not prevent static accounts (or
            # other providers) from loading.
            continue
        if not is_directory:
            continue
        account_id = path.name.lstrip(".") or path.name
        discovered.append(
            Account(
                id=account_id,
                config_dir=str(path),
                enabled=True,
                # Discovered accounts sort after every statically declared one
                # by default, so a hand-authored priority ordering always wins.
                priority=100 + index,
            )
        )
    return discovered


def load_accounts_config(path: Path | None = None) -> AccountPoolConfig:
    """Load ``configs/accounts.yaml`` and expand each provider's
    ``discover_glob`` (if set) into extra ``Account`` entries.

    Discovered accounts are de-duplicated against everything already in the
    pool (static entries first, then earlier discoveries) by BOTH resolved
    ``config_dir`` path and id, so re-declaring an account statically and
    having it also match the glob never produces a duplicate pool entry.
    """
    raw = yaml.safe_load((path or config_path()).read_text(encoding="utf-8")) or {}
    config = AccountPoolConfig.model_validate(raw)

    seen_ids: set[str] = set()
    seen_dirs: set[str] = set()
    for pool in config.providers.values():
        for account in pool.accounts:
            seen_ids.add(account.id)
            seen_dirs.add(account.resolved_config_dir())

    for pool in config.providers.values():
        if not pool.discover_glob:
            continue
        for account in _discover_accounts(pool.discover_glob):
            resolved = account.resolved_config_dir()
            if resolved in seen_dirs or account.id in seen_ids:
                continue
            seen_dirs.add(resolved)
            seen_ids.add(account.id)
            pool.accounts.append(account)

    return config


__all__ = [
    "DEFAULT_COOLDOWN_SECONDS",
    "Account",
    "AccountPoolConfig",
    "ProviderPool",
    "config_path",
    "load_accounts_config",
]
