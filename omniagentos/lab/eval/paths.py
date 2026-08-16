"""Path + environment isolation for the PROTECTED evaluator store.

Reward-hacking invariant (blueprint Section 11.7; design review HD-001/HD-ALT/
HD-009): held-out expected values live ONLY in a SEPARATE SQLite file this
module resolves (env ``OMNIAGENTOS_EVAL_PROTECTED``, otherwise a randomized
process-local path outside the checkout). That is a DIFFERENT FILE from
``omniagentos.contracts.default_db_path()``'s ``var/omniagentos.db`` on
purpose — structural isolation, not merely a different table in the same
database a challenger/judge/curator subprocess can already open.

Encryption at rest (``omniagentos.lab.eval._crypto``) defeats the demonstrated
same-uid attack that enumerates this directory and reads the file directly.
This module does not claim OS-level sandboxing: live-process memory or worker
environment inspection is a deeper H3 filesystem/process-isolation boundary.

``scrubbed_env`` is the BINDING helper (contracts/lab-interfaces.md
§L02-eval): any subprocess that runs a challenger/judge/curator agent MUST be
launched with it so the child has no environment-based pointer to the
protected store (L04x-executor/L05/L06 own actually wiring it into their
``subprocess`` calls; this package only provides the helper).
"""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path

from omniagentos.lab.eval._crypto import EVAL_KEY_ENV

PROTECTED_DB_ENV = "OMNIAGENTOS_EVAL_PROTECTED"

# Every env var whose mere PRESENCE would let a subprocess locate OR decrypt the
# protected store. A tuple (not just the one constant) so a future
# protected-surface env var has exactly one place to be added. EVAL_KEY_ENV is
# never placed in the parent env today (it is set only on the worker's own spawn
# env), so scrubbing it is inert defense-in-depth against a future regression.
SENSITIVE_ENV_VARS: tuple[str, ...] = (PROTECTED_DB_ENV, EVAL_KEY_ENV)
_DEFAULT_PROTECTED_DB_PATH: str | None = None
_CREATED_PROTECTED_DIRS: set[str] = set()
_ATEXIT_REGISTERED = False


def _cleanup_created_protected_dirs() -> None:
    """Remove process-owned protected temp directories at normal exit."""
    for directory in list(_CREATED_PROTECTED_DIRS):
        shutil.rmtree(directory, ignore_errors=True)
    _CREATED_PROTECTED_DIRS.clear()


def _repo_root() -> str:
    """Repo root = parent of the installed ``omniagentos`` package. Mirrors
    ``omniagentos.contracts._repo_root``'s anchoring (resolve via the
    package's own file location, not the process cwd — council finding
    INT-002/OPS-005: cwd-relative defaults caused a silent split-brain)."""
    import omniagentos

    return os.path.dirname(os.path.dirname(os.path.abspath(omniagentos.__file__)))


def default_protected_db_path() -> str:
    """Resolve the protected store path: ``OMNIAGENTOS_EVAL_PROTECTED`` wins;
    otherwise use one randomized, out-of-checkout path per process.

    The store is EPHEMERAL, not persistent: held-out values are encrypted with a
    per-PROCESS in-memory key (see ``_crypto``), so even a fixed
    ``OMNIAGENTOS_EVAL_PROTECTED`` override is not durable across restarts — rows
    written by a prior boot become undecryptable and surface as a loud
    ``DecryptionError``/``MissingExpectedError`` (never a silent wrong score), so
    the protected store must be re-seeded each boot."""
    global _ATEXIT_REGISTERED, _DEFAULT_PROTECTED_DB_PATH

    override = os.environ.get(PROTECTED_DB_ENV)
    if override:
        return override
    if _DEFAULT_PROTECTED_DB_PATH is None:
        parent = Path(tempfile.mkdtemp(prefix="omniagentos-eval-"))
        _CREATED_PROTECTED_DIRS.add(str(parent))
        if not _ATEXIT_REGISTERED:
            atexit.register(_cleanup_created_protected_dirs)
            _ATEXIT_REGISTERED = True
        _DEFAULT_PROTECTED_DB_PATH = str(parent / "protected.db")
    return _DEFAULT_PROTECTED_DB_PATH


def scrubbed_env(base_env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return a copy of *base_env* (default: the current process environment)
    with every protected-store-locating variable removed.

    A candidate/challenger/judge/curator subprocess otherwise legitimately
    inherits everything else it needs (``PATH``, provider API keys, cwd via
    the ``subprocess`` ``cwd=`` kwarg, ...) — this strips ONLY the pointer(s)
    to the protected store, so the child has no env-based path to it even if
    it has full filesystem read access to everything else in its workspace.
    """
    source = os.environ if base_env is None else base_env
    return {key: value for key, value in source.items() if key not in SENSITIVE_ENV_VARS}


def assert_env_scrubbed(env: Mapping[str, str]) -> None:
    """Defense-in-depth guard a subprocess-launching caller may assert right
    before spawning: raises loudly instead of silently launching a child that
    can still find the protected store."""
    leaked = [key for key in SENSITIVE_ENV_VARS if key in env]
    if leaked:
        raise RuntimeError(
            f"env is not scrubbed for a candidate-facing subprocess; still carries: "
            f"{', '.join(sorted(leaked))}"
        )
