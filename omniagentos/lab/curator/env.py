"""Environment scrubbing for the curator agent (BINDING, contracts/lab-interfaces.md
§L06-curator / design review HD-001).

Any subprocess that runs a challenger/judge/curator agent must be launched with
an environment that does NOT contain the L02 protected-store pointer. The
curator itself only ever reads the shared, sanitized `LabStore` (never
`var/eval_protected.db`), and when it drives a live Sonnet-high narrative pass
(`omniagentos.lab.curator.agent`) that pass must not be able to reach the
protected store via the environment either -- `omniagentos/adapters/common.py`
spawns CLI subprocesses with `subprocess.Popen(..., )` and no explicit `env=`,
so they inherit the CALLING process's environment verbatim. This module is the
single place that knows which keys are protected and how to strip them.
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager

# The env var the L02 protected grader reads (contracts/lab-interfaces.md
# HD-001/HD-ALT/HD-009): "any subprocess that runs a challenger/judge/curator
# agent is launched with an env that DOES NOT contain OMNIAGENTOS_EVAL_PROTECTED".
PROTECTED_ENV_KEY = "OMNIAGENTOS_EVAL_PROTECTED"


def is_protected_key(key: str) -> bool:
    """True if *key* names an env var a curator/challenger/judge process must
    never see. Matches the documented key exactly (case-insensitively) plus,
    as defense-in-depth, any key whose name contains "PROTECTED" -- so a future
    protected-store env var this module does not yet know the exact name of is
    still stripped rather than silently leaking."""
    upper = key.upper()
    return upper == PROTECTED_ENV_KEY or "PROTECTED" in upper


def scrubbed_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return a copy of *base* (default ``os.environ``) with protected keys removed."""
    source: Mapping[str, str] = os.environ if base is None else base
    return {key: value for key, value in source.items() if not is_protected_key(key)}


@contextmanager
def scrubbed_environ() -> Iterator[None]:
    """Temporarily scrub protected keys out of ``os.environ`` for the duration
    of the block, restoring them on exit (including on an exception).

    Use this around any curator code path that invokes an ``AgentAdapter``,
    since adapters spawn subprocesses that inherit the current process
    environment verbatim -- there is no ``env=`` seam to pass a scrubbed
    mapping through instead.
    """
    removed = {key: os.environ.pop(key) for key in list(os.environ) if is_protected_key(key)}
    try:
        yield
    finally:
        os.environ.update(removed)
