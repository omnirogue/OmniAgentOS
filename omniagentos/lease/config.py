"""The Autonomy Lease configuration manager.

This module resolves and manages the configuration parameters for the sandboxed
autonomy lease subsystem. It supports multiple runtime modes ("off", "shadow", "enforce")
governing process execution limits, network policies, domain filters, proxying, and
associated task execution tracking.

Semantics and design principles are lifted verbatim from the established patterns in
``omniagentos.scope.config``:
- Environment variable overrides work in BOTH directions.
- A truthy value in the OMNIAGENTOS_AUTONOMY_LEASE environment variable acts as a forced override
  to enable the subsystem ("enforce").
- A falsy value in the OMNIAGENTOS_AUTONOMY_LEASE environment variable acts as a forced disablement
  to disable the subsystem ("off"), regardless of config file settings. This works as an emergency
  kill switch.
- Unparseable or invalid values in environment variable overrides are ignored (failing closed to
  safeguard the process tree).
- Missing, broken, or unparseable configuration files fail closed (reverting to the default
  disabled mode of "off").

Resolution Order:
1. Environment overrides (e.g. OMNIAGENTOS_AUTONOMY_LEASE, OMNIAGENTOS_LEASE_TTL_S) if valid.
2. The config file configs/lease.yaml (overrideable via OMNIAGENTOS_LEASE_CONFIG).
3. Hardcoded defaults (off, 3600.0, etc.).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml

__all__ = [
    "DEFAULT_LEASE_MODE",
    "DEFAULT_NET_POLICY",
    "DEFAULT_TTL_S",
    "LEASE_ENV",
    "LEASE_MODES",
    "LEASE_TTL_ENV",
    "LeaseMode",
    "NET_POLICIES",
    "lease_allowed_domains",
    "lease_ceilings",
    "lease_config",
    "lease_config_path",
    "lease_enabled",
    "lease_enforcing",
    "lease_ledger_dir",
    "lease_mode",
    "lease_net_policy",
    "lease_proxy_ports",
    "lease_ttl_s",
]

LeaseMode = Literal["off", "shadow", "enforce"]

#: The three lease modes, in increasing order of consequence.
LEASE_MODES: tuple[LeaseMode, ...] = ("off", "shadow", "enforce")

LEASE_ENV = "OMNIAGENTOS_AUTONOMY_LEASE"
LEASE_TTL_ENV = "OMNIAGENTOS_LEASE_TTL_S"
_CONFIG_ENV = "OMNIAGENTOS_LEASE_CONFIG"

#: Off by default: a mechanism that restricts agent execution and processes
#: ships dark and is turned on deliberately, per-host, after a shadow soak.
DEFAULT_LEASE_MODE: LeaseMode = "off"

#: Default time-to-live (lease TTL) in seconds.
DEFAULT_TTL_S = 3600.0

#: Default network policy when no policy is configured or resolved.
DEFAULT_NET_POLICY = "open"

#: Available network policies under leased execution.
NET_POLICIES: tuple[str, ...] = ("open", "deny", "proxy")

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY = frozenset({"0", "false", "no", "off"})


def _repo_root() -> Path:
    """Repo root = parent of the ``omniagentos`` package (cwd-independent)."""
    return Path(__file__).resolve().parent.parent.parent


def lease_config_path() -> Path:
    """Resolve ``configs/lease.yaml`` cwd-independently.

    ``OMNIAGENTOS_LEASE_CONFIG`` overrides, the same convention as
    ``OMNIAGENTOS_SWARM_CONFIG`` / ``OMNIAGENTOS_ACCOUNTS_CONFIG``.
    """
    override = os.environ.get(_CONFIG_ENV)
    if override:
        return Path(override)
    return _repo_root() / "configs" / "lease.yaml"


def lease_config(path: Path | None = None) -> dict[str, Any]:
    """``configs/lease.yaml`` as a plain dict; missing/broken file -> ``{}``.

    Best-effort by design (the ``load_swarm_config`` contract): a fresh checkout,
    a test tmpdir, or a half-edited YAML file must degrade to the hard-coded
    defaults rather than take a lane down. Since the default is "off", a broken
    config file fails CLOSED with respect to refusing agent writes or execution.

    TOTALITY MATTERS HERE, not just tolerance. This runs on EVERY sub-CLI launch,
    including with the feature off, so any exception it lets escape would turn a
    launch that succeeds on main into a failure -- which is precisely the additive
    guarantee this lane is built on. ``UnicodeDecodeError`` is the one that is easy
    to miss: it is a ``ValueError``, NOT an ``OSError``, and a config path pointed
    at a binary file raises it out of ``read_text``. The except clause is therefore
    broad on purpose (security review finding #12).
    """
    try:
        raw = yaml.safe_load((path or lease_config_path()).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - a config read must NEVER break a launch
        return {}
    return raw if isinstance(raw, dict) else {}


def _lease_section(config: dict[str, Any] | None = None) -> dict[str, Any]:
    config = config if config is not None else lease_config()
    section = config.get("autonomy_lease")
    return section if isinstance(section, dict) else {}


def _coerce_mode(value: Any) -> LeaseMode | None:
    """Parse one mode spelling; ``None`` when the value says nothing.

    Accepts the three mode names, and also the boolean spellings, because
    ``OMNIAGENTOS_AUTONOMY_LEASE=1`` is what a hand at a terminal actually types and
    silently ignoring it would be the worst outcome. Truthy maps to ``enforce``:
    the flag is named OMNIAGENTOS_AUTONOMY_LEASE, and a lease that never enforces is
    not a lease. The measurement ramp is one explicit word away (``=shadow``).

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
    if text in LEASE_MODES:
        return text  # type: ignore[return-value]
    if text in _TRUTHY:
        return "enforce"
    if text in _FALSY:
        return "off"
    return None


def lease_mode() -> LeaseMode:
    """The effective autonomy-lease mode: env override, else config, else ``off``.

    The env override is bidirectional: ``OMNIAGENTOS_AUTONOMY_LEASE=off`` disables
    the mechanism on a host whose config file enables it, which is the emergency
    lever. An UNPARSEABLE env value (a typo like ``=enfroce``) is ignored rather
    than treated as on — a typo must never silently start enforcing limits.
    """
    from_env = _coerce_mode(os.environ.get(LEASE_ENV))
    if from_env is not None:
        return from_env
    from_config = _coerce_mode(_lease_section().get("mode"))
    if from_config is not None:
        return from_config
    return DEFAULT_LEASE_MODE


def lease_enabled() -> bool:
    """True when the autonomy lease is not ``off`` (i.e. ``shadow`` or ``enforce``)."""
    return lease_mode() != "off"


def lease_enforcing() -> bool:
    """True only in ``enforce`` — i.e. when lease violation actually triggers enforcement."""
    return lease_mode() == "enforce"


def _positive_float(value: Any) -> float | None:
    """A usable positive duration or limit, or ``None`` for anything else.

    Non-positive is rejected rather than clamped.
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


def lease_ttl_s() -> float:
    """Autonomy lease duration / TTL in seconds (env, then config, then 3600.0)."""
    from_env = _positive_float(os.environ.get(LEASE_TTL_ENV))
    if from_env is not None:
        return from_env
    from_config = _positive_float(_lease_section().get("ttl_seconds"))
    if from_config is not None:
        return from_config
    return DEFAULT_TTL_S


def lease_ceilings() -> dict[str, float]:
    """The hard resource limits applied to the process tree under lease.

    Returns a dict with exactly the keys ``cpu_s``, ``rss_mb``, ``max_procs``, ``wall_s``.
    A missing / non-positive / garbage value becomes ``0.0``, which means UNSET (no limit).
    Never raises.
    """
    ceilings = _lease_section().get("ceilings")
    if not isinstance(ceilings, dict):
        ceilings = {}

    res: dict[str, float] = {}
    for key in ("cpu_s", "rss_mb", "max_procs", "wall_s"):
        val = ceilings.get(key)
        parsed = _positive_float(val)
        res[key] = parsed if parsed is not None else 0.0
    return res


def lease_net_policy() -> str:
    """The default network safety policy applied under leased execution.

    Resolution: ``autonomy_lease.net.default_policy``, lowercased+stripped;
    anything not in ``NET_POLICIES`` degrades to ``DEFAULT_NET_POLICY`` ("open").
    """
    net = _lease_section().get("net")
    if not isinstance(net, dict):
        return DEFAULT_NET_POLICY
    val = net.get("default_policy")
    if not isinstance(val, (str, bytes)):
        return DEFAULT_NET_POLICY
    try:
        policy = str(val).strip().lower()
    except Exception:
        return DEFAULT_NET_POLICY
    if policy in NET_POLICIES:
        return policy
    return DEFAULT_NET_POLICY


def lease_allowed_domains() -> list[str]:
    """List of allowed outbound domains under restricted network policies.

    Resolution: ``autonomy_lease.net.allow_domains``, a list of non-empty strings,
    lowercased+stripped, de-duplicated preserving order; anything else -> ``[]``.
    """
    net = _lease_section().get("net")
    if not isinstance(net, dict):
        return []
    domains = net.get("allow_domains")
    if not isinstance(domains, list):
        return []

    seen: set[str] = set()
    result: list[str] = []
    for item in domains:
        if isinstance(item, (str, bytes)):
            try:
                cleaned = str(item).strip().lower()
                if cleaned and cleaned not in seen:
                    seen.add(cleaned)
                    result.append(cleaned)
            except Exception:
                continue
    return result


def lease_proxy_ports() -> tuple[int, ...]:
    """Tuple of allowed ports for outbound traffic under lease.

    Resolution: ``autonomy_lease.net.allow_ports``, a tuple of ints in 1..65535,
    de-duplicated preserving order; default ``(443,)`` when absent/garbage.
    """
    net = _lease_section().get("net")
    if not isinstance(net, dict):
        return (443,)
    ports = net.get("allow_ports")
    if not isinstance(ports, list):
        return (443,)

    seen: set[int] = set()
    result: list[int] = []
    for item in ports:
        try:
            val = int(item)
            if 1 <= val <= 65535 and val not in seen:
                seen.add(val)
                result.append(val)
        except (TypeError, ValueError):
            continue

    return tuple(result) if result else (443,)


def lease_ledger_dir() -> str:
    """The effective ledger directory for writing lease transactions.

    Resolution: ``autonomy_lease.ledger_dir`` when it is a non-empty string,
    else ``omniagentos.contracts.default_ledger_dir()``.
    """
    val = _lease_section().get("ledger_dir")
    if isinstance(val, (str, bytes)):
        cleaned = str(val).strip()
        if cleaned:
            return cleaned

    from omniagentos.contracts import default_ledger_dir

    return default_ledger_dir()
