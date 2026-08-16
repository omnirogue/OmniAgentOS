"""One resolver for runtime state roots (``var`` / ``secrets`` / ``ledger`` / ``vault``).

Every runtime state path used to be re-derived from ``__file__`` at its call
site, which is exactly how a simulation process ended up writing into the
operator checkout. This module is the single place that answers "where does
runtime state live for THIS process?".

Precedence is **caller-controlled** (``env_keys``) on purpose. The pre-existing
call sites do not agree on it and never did:

* ``reliability/memory.py`` reads ``OMNIAGENTOS_VAR`` then ``OMNIAGENTOS_VAR_DIR``
  (the canonical order, exported here as :data:`CANONICAL_VAR_ENV_KEYS`);
* ``sessions/token.py`` and ``simgate`` read ``OMNIAGENTOS_VAR_DIR`` first;
* ``sessions/chain_read.py`` historically honoured ``OMNIAGENTOS_VAR`` *only*.

Imposing one order here would silently relocate live production state (the
launchers always export both variables), so each converted caller passes the
order that keeps its own production path byte-identical and, where it wants a
new variable admitted, appends it *after* the one it already honoured.

``reliability/memory.py``'s extra ``OMNIAGENTOS_HOME`` step is intentionally NOT
generalised here: it is that resolver's own reflexion-storage convention, no
converted caller has it, and adding it would silently admit a fourth spelling of
"where var lives" to every call site.

The environment value is used verbatim (``Path(raw.strip())``) rather than
``expanduser().resolve()``. Normalising would change the resolved path for
relative, ``~``-prefixed and symlinked roots — the precise byte-identity the
conversions must preserve. ``simgate`` already refuses a non-absolute var root
under simulation, so nothing is lost.

Under an effective simulation context the package-anchored fallback is
**refused**: a campaign process with no runtime-root variable must fail loudly
naming the fix, never quietly resolve into the operator checkout. An
incoherent simulation environment (:class:`~omniagentos.simgate.SimGateError`)
is likewise a refusal and propagates to the caller — resolving runtime state
from an environment we cannot validate is the defect, not the error.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from omniagentos import simgate

#: The canonical order, generalised from ``reliability/memory.py``'s resolver.
CANONICAL_VAR_ENV_KEYS: tuple[str, ...] = ("OMNIAGENTOS_VAR", "OMNIAGENTOS_VAR_DIR")

#: ``sessions/token.py`` / ``simgate`` order: ``OMNIAGENTOS_VAR_DIR`` wins.
TOKEN_VAR_ENV_KEYS: tuple[str, ...] = ("OMNIAGENTOS_VAR_DIR", "OMNIAGENTOS_VAR")

#: Package anchor: ``<repo>/var``. Identical to the ``parents[2] / "var"`` that
#: the converted call sites (one package level deeper) computed for themselves.
_PACKAGE_VAR_ROOT = Path(__file__).resolve().parents[1] / "var"


class RuntimePathError(RuntimeError):
    """A runtime state root cannot be resolved safely."""


def resolve_sim_context_or_none(
    environ: Mapping[str, str] | None = None,
) -> simgate.SimContext | None:
    """The validated simulation context, or ``None`` when not simulating.

    Raises :class:`~omniagentos.simgate.SimGateError` when the simulation
    environment is incoherent (fail closed — never "assume production").
    """
    context = simgate.resolve_sim_context(environ)
    return context if context.sim_mode else None


def resolve_var_root(
    env_keys: tuple[str, ...] = CANONICAL_VAR_ENV_KEYS,
    leaf: tuple[str, ...] = (),
    *,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Resolve a runtime state root and append ``leaf``.

    Args:
        env_keys: the caller's own precedence, highest first. Keep the key the
            call site already honoured in front of any key you are adding, or
            production state moves.
        leaf: path components appended to the resolved root.
        environ: environment mapping (defaults to ``os.environ``).

    Returns:
        ``<first set env_keys value>/<leaf>``, else ``<repo>/var/<leaf>``.

    Raises:
        RuntimePathError: an effective simulation context is active but none of
            ``env_keys`` is set, so only the package anchor remains.
        omniagentos.simgate.SimGateError: the simulation environment is
            incoherent.
    """
    values = os.environ if environ is None else environ
    # Resolved FIRST so an incoherent simulation refuses even when a var root
    # happens to be set.
    context = resolve_sim_context_or_none(values)

    for key in env_keys:
        raw = values.get(key)
        if raw and raw.strip():
            return Path(raw.strip()).joinpath(*leaf)

    if context is not None:
        names = " or ".join(env_keys) or "a runtime-root environment variable"
        raise RuntimePathError(
            f"OMNIAGENTOS_SIM_MODE=1 (campaign {context.campaign!r}) requires {names} "
            "to be set to an absolute campaign directory; refusing to fall back to "
            f"the package-anchored production root {_PACKAGE_VAR_ROOT}"
        )

    return _PACKAGE_VAR_ROOT.joinpath(*leaf)


__all__ = [
    "CANONICAL_VAR_ENV_KEYS",
    "TOKEN_VAR_ENV_KEYS",
    "RuntimePathError",
    "resolve_sim_context_or_none",
    "resolve_var_root",
]
