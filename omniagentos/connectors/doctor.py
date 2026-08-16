"""Report declared connector credential names missing from the environment.

This module deliberately compares *names* only.  It never reads secret values,
loads a vault, or includes credentials in its output.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping

from omniagentos.connectors import load_registry

_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")


def _declared_env_names() -> list[str]:
    """Return the unique declared environment-variable names, sorted."""
    registry = load_registry()
    names = {name for connector in registry.connectors.values() for name in connector.env}
    invalid = [name for name in names if not _ENV_NAME.fullmatch(name)]
    if invalid:
        # Do not reproduce a malformed registry entry: it could be value-shaped.
        raise ValueError("Connector registry contains an invalid environment-variable name.")
    return sorted(names)


def _check_secrets(environ: Mapping[str, object] | None = None) -> list[str]:
    """Return declared connector credential names absent from ``environ``.

    Presence is intentionally distinct from truthiness: an empty value is still
    present for this diagnostic.  The check does not access environment values.

    Module-internal: this module's operator surface is :func:`main`
    (``make secrets-doctor`` -> ``python -m omniagentos.connectors.doctor``), and
    nothing outside this file computes the report.
    """
    env = os.environ if environ is None else environ
    return [name for name in _declared_env_names() if name not in env]


#: Header for the second report section. A literal marker so a caller parsing
#: this output can never mistake a shadowed name for a missing one.
EMPTY_BUT_VAULT_BACKED_HEADER = "# empty-but-vault-backed (blanking no longer disables):"


def _check_empty_but_vault_backed(
    environ: Mapping[str, object] | None = None,
    *,
    secrets_dir: str | None = None,
    root: str | None = None,
) -> list[str]:
    """Return declared names that are present-but-EMPTY and refillable from the vault.

    This category exists because the loader and this diagnostic disagree about
    what an empty value means, and both are right about their own job:

    * ``_check_secrets`` treats presence as presence — an empty value is still a
      declared name the operator has acknowledged;
    * ``secrets_env.emit_export_lines`` treats an empty value as unset and
      REFILLS it from the vault, because an empty preset used to shadow the
      vault and silently un-configure a connector.

    The consequence is that blanking a variable no longer disables anything: the
    next launch restores it. An operator who cannot see that will believe a
    connector is off while it is live. These names are exactly the set where
    that misunderstanding is possible. Names only — the vault is consulted
    through ``vault_backed_names``, which never returns a value.
    """
    from omniagentos.connectors.secrets_env import vault_backed_names

    env = os.environ if environ is None else environ
    backed = vault_backed_names(secrets_dir, root=root)
    return [
        name
        for name in _declared_env_names()
        if name in env and not str(env[name] or "") and name in backed
    ]


def main(environ: Mapping[str, object] | None = None) -> int:
    """Print missing names, one per line, and return a process exit status.

    A second, clearly-headed section lists names that are present-but-empty and
    still supplied by the vault. It is advisory, so it never changes the exit
    status: those names are configured, just not in the way the operator may
    think.
    """
    missing = _check_secrets(environ)
    if missing:
        print("\n".join(missing))

    shadowed = _check_empty_but_vault_backed(environ)
    if shadowed:
        print(EMPTY_BUT_VAULT_BACKED_HEADER)
        print("\n".join(shadowed))

    return 1 if missing else 0


if __name__ == "__main__":  # pragma: no cover - exercised by module invocation
    raise SystemExit(main())

