"""Multiple Claude accounts for OmniAgentOS.

An account is primarily a CLI config dir (``CLAUDE_CONFIG_DIR``); the registry stores
only the path, never a credential. See :mod:`omniagentos.accounts.service`.
"""

from omniagentos.accounts.service import (
    SpawnAccount,
    add_account,
    detect_config_dirs,
    list_accounts,
    next_account_for_spawn,
    remove_account,
    set_default,
    set_enabled,
)

__all__ = [
    "SpawnAccount",
    "add_account",
    "detect_config_dirs",
    "list_accounts",
    "next_account_for_spawn",
    "remove_account",
    "set_default",
    "set_enabled",
]
