"""The Phase-3 parallelism knobs: scope-lock mode, TTL, starvation window.

Semantics are lifted verbatim from ``swarm.worktrees.swarm_worktrees_enabled``
(the repo's established opt-in idiom), and the important half is the part that
is easy to get wrong: **the environment variable overrides in BOTH directions**.
A truthy value turns the feature on even when the config file says off, and a
falsy value force-DISABLES it even when the config file says on. A one-directional
override is not a kill switch, and a feature that can refuse a worker's writes
needs a kill switch that works from a shell without editing a file.

Resolution order, for every knob here:

1. the environment variable, when set to something parseable;
2. ``configs/parallelism.yaml``;
3. the hard-coded default (off / 90s / 300s).

The config file is NEW and deliberately separate from ``configs/swarm.yaml``.
Scope locking is cross-lane by construction — the runner, the session lane,
longhaul and swarm all take locks out of the same table — so putting its knobs
under the swarm config would say the opposite of what the mechanism is.

Three modes, not a boolean:

``off``
    Nothing is written and nothing is refused. The default, so a lane can wire
    :mod:`omniagentos.scope.locks` in before anyone has decided to turn it on.
``shadow``
    Lock rows ARE inserted; conflicts are reported but never refused. This is the
    measurement ramp, and the row-writing is the whole point of it — a shadow
    mode that only *evaluates* the predicate measures a hypothetical, because
    the locks it declined to write are the locks the next claimant would have
    collided with. Writing them means the contention you measure is the
    contention you would actually have had.
``enforce``
    Conflicts are refused; the claimant parks.

``OMNIAGENTOS_SCOPE_STARVATION_S`` has no consumer in T3.2 — the waiter queue
this package lands is FIFO and the anti-starvation escalation that reads this
value belongs to the scheduler work package. It is declared here anyway so that
all three knobs land in one file with one documented resolution order rather
than accreting a second config surface later.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml

__all__ = [
    "DEFAULT_SCOPE_MODE",
    "DEFAULT_STARVATION_S",
    "DEFAULT_TTL_S",
    "SCOPE_LOCKS_ENV",
    "SCOPE_MODES",
    "SCOPE_STARVATION_ENV",
    "SCOPE_TTL_ENV",
    "ScopeLockMode",
    "parallelism_config",
    "parallelism_config_path",
    "scope_locks_enabled",
    "scope_locks_enforcing",
    "scope_locks_mode",
    "scope_starvation_s",
    "scope_ttl_s",
]

ScopeLockMode = Literal["off", "shadow", "enforce"]

#: The three modes, in increasing order of consequence.
SCOPE_MODES: tuple[ScopeLockMode, ...] = ("off", "shadow", "enforce")

SCOPE_LOCKS_ENV = "OMNIAGENTOS_SCOPE_LOCKS"
SCOPE_TTL_ENV = "OMNIAGENTOS_SCOPE_TTL_S"
SCOPE_STARVATION_ENV = "OMNIAGENTOS_SCOPE_STARVATION_S"

#: Off by default: a mechanism that can refuse writes ships dark and is turned on
#: deliberately, per-host, after a shadow soak.
DEFAULT_SCOPE_MODE: ScopeLockMode = "off"

#: Lock lease, seconds. Must comfortably exceed the renew interval of the
#: slowest lane — a lock that lapses under a live worker is worse than no lock,
#: because a second worker then acquires it legitimately.
DEFAULT_TTL_S = 90.0

#: How long a parked waiter may sit before the scheduler is expected to escalate
#: (priority boost / operator-visible event). Declared here, consumed later.
DEFAULT_STARVATION_S = 300.0

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY = frozenset({"0", "false", "no", "off"})

_CONFIG_ENV = "OMNIAGENTOS_PARALLELISM_CONFIG"


def _repo_root() -> Path:
    """Repo root = parent of the ``omniagentos`` package (cwd-independent)."""
    return Path(__file__).resolve().parent.parent.parent


def parallelism_config_path() -> Path:
    """Resolve ``configs/parallelism.yaml`` cwd-independently.

    ``OMNIAGENTOS_PARALLELISM_CONFIG`` overrides, the same convention as
    ``OMNIAGENTOS_SWARM_CONFIG`` / ``OMNIAGENTOS_ACCOUNTS_CONFIG``.
    """
    override = os.environ.get(_CONFIG_ENV)
    if override:
        return Path(override)
    return _repo_root() / "configs" / "parallelism.yaml"


def parallelism_config(path: Path | None = None) -> dict[str, Any]:
    """``configs/parallelism.yaml`` as a plain dict; missing/broken file -> ``{}``.

    Best-effort by design (the ``load_swarm_config`` contract): a fresh checkout,
    a test tmpdir, or a half-edited YAML file must degrade to the hard-coded
    defaults rather than take a lane down. Since the default is "off", a broken
    config file fails CLOSED with respect to refusing anyone's writes.
    """
    try:
        raw = yaml.safe_load((path or parallelism_config_path()).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _scope_section(config: dict[str, Any] | None = None) -> dict[str, Any]:
    config = config if config is not None else parallelism_config()
    section = config.get("scope_locks")
    return section if isinstance(section, dict) else {}


def _coerce_mode(value: Any) -> ScopeLockMode | None:
    """Parse one mode spelling; ``None`` when the value says nothing.

    Accepts the three mode names, and also the boolean spellings, because
    ``OMNIAGENTOS_SCOPE_LOCKS=1`` is what a hand at a terminal actually types and
    silently ignoring it would be the worst outcome. Truthy maps to ``enforce``:
    the flag is named SCOPE_LOCKS, and a lock that never refuses is not a lock.
    The measurement ramp is one explicit word away (``=shadow``), whereas mapping
    truthy to ``shadow`` would leave an operator believing they had enabled
    protection they had not.

    ``bool`` is handled explicitly because YAML parses a bare ``off:`` value as
    ``False`` — an unquoted ``mode: off`` in the config file must mean the OFF
    MODE and not "the boolean false, which is unparseable".
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return "enforce" if value else "off"
    text = str(value).strip().lower()
    if not text:
        return None
    if text in SCOPE_MODES:
        return text  # type: ignore[return-value]
    if text in _TRUTHY:
        return "enforce"
    if text in _FALSY:
        return "off"
    return None


def scope_locks_mode() -> ScopeLockMode:
    """The effective scope-lock mode: env override, else config, else ``off``.

    The env override is bidirectional: ``OMNIAGENTOS_SCOPE_LOCKS=off`` disables
    the mechanism on a host whose config file enables it, which is the emergency
    lever. An UNPARSEABLE env value (a typo like ``=enfroce``) is ignored rather
    than treated as on — a typo must never silently start refusing writes.
    """
    from_env = _coerce_mode(os.environ.get(SCOPE_LOCKS_ENV))
    if from_env is not None:
        return from_env
    from_config = _coerce_mode(_scope_section().get("mode"))
    if from_config is not None:
        return from_config
    return DEFAULT_SCOPE_MODE


def scope_locks_enabled() -> bool:
    """True when lock rows are written at all (``shadow`` or ``enforce``)."""
    return scope_locks_mode() != "off"


def scope_locks_enforcing() -> bool:
    """True only in ``enforce`` — i.e. when a conflict actually refuses a claim."""
    return scope_locks_mode() == "enforce"


def _positive_float(value: Any) -> float | None:
    """A usable positive duration, or ``None`` for anything else.

    Non-positive is rejected rather than clamped: a TTL of 0 would expire every
    lock the instant it was taken, which reads as "locking is broken" rather than
    "locking is off", and the operator who typed it meant neither.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed <= 0 or parsed != parsed:  # NaN != NaN
        return None
    return parsed


def _duration(env_name: str, config_key: str, fallback: float) -> float:
    from_env = _positive_float(os.environ.get(env_name))
    if from_env is not None:
        return from_env
    from_config = _positive_float(_scope_section().get(config_key))
    if from_config is not None:
        return from_config
    return fallback


def scope_ttl_s() -> float:
    """Lock lease length in seconds (env, then config, then 90)."""
    return _duration(SCOPE_TTL_ENV, "ttl_seconds", DEFAULT_TTL_S)


def scope_starvation_s() -> float:
    """Waiter age at which the scheduler should escalate (env, config, then 300)."""
    return _duration(SCOPE_STARVATION_ENV, "starvation_seconds", DEFAULT_STARVATION_S)
