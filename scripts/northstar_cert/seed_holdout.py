#!/usr/bin/env python3
"""Create the runtime-only North Star protected holdout store.

This intentionally seeds no repository database.  Expected values are handed
to :class:`ProtectedGrader`, the existing encrypted protected-store path.

The store is EPHEMERAL and OUT-OF-CHECKOUT by production design
(``omniagentos/lab/eval/paths.py``): a per-process key means seeded rows are
only decryptable inside the seeding process, and
``tests/lab/eval/test_pentest_leak.py`` pins ``var/eval_protected.db`` as a
must-not-exist leak location.  Seeding INTO the checkout recreates the exact
leak the pentest suite exists to catch (found live 2026-08-09, R1-012) — so
an in-checkout path is refused, exit 2, do not retry unchanged.

"THE checkout" is not one path.  The installed ``omniagentos`` package and the
copy of this script being executed can live in DIFFERENT checkouts (a worktree
run under the cadence interpreter is the live case), so every checkout this
process can name is guarded — see :func:`_protected_roots`.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from omniagentos.lab.eval.grader import ProtectedGrader
from omniagentos.lab.eval.paths import default_protected_db_path

DEFAULT_CASES: dict[str, dict[str, object]] = {
    "nsc-holdout-sales-01": {"outcome": "park_for_human", "reason": "discount_outside_policy"},
    "nsc-holdout-content-01": {"outcome": "pass", "channel": "editorial_calendar"},
    "nsc-holdout-finance-01": {"outcome": "unknown", "reason": "conflicting_source_facts"},
    "nsc-holdout-cs-01": {"outcome": "escalate", "reason": "account_takeover_signal"},
}


def _repo_root() -> Path:
    """The checkout the IMPORTED ``omniagentos`` package was loaded from.

    Under the cadence interpreter this is the installed checkout, which is not
    necessarily the checkout this script is being executed from — see
    :func:`_protected_roots`.
    """
    import omniagentos

    return Path(os.path.dirname(os.path.dirname(os.path.abspath(omniagentos.__file__))))


def _script_root() -> Path:
    """The checkout THIS FILE lives in — ``scripts/northstar_cert/`` up two."""
    return Path(__file__).resolve().parents[2]


def _protected_roots() -> tuple[Path, ...]:
    """Every checkout the protected store must not be seeded into.

    Deriving the guarded root from ``omniagentos.__file__`` ALONE was the R1-012
    repair's own defect: under the cadence interpreter the package resolves to
    the installed checkout (``~/OmniAgentOS``) while the script runs out of a
    worktree, so the guard refused the checkout nobody was writing to and
    admitted the one that was. Both roots are guarded, and they are computed
    independently: a run where they coincide is the common case, not the
    assumption.

    A missing ``omniagentos`` narrows the guard to the script's own checkout
    rather than disabling it — the refusal path must stay reachable when the
    runtime is broken.
    """
    roots = [_script_root().resolve()]
    try:
        roots.append(_repo_root().resolve())
    except ImportError:  # pragma: no cover - omniagentos is a hard dep of the seeder
        pass
    return tuple(dict.fromkeys(roots))


def seed_holdout(path: Path) -> Path:
    """Seed *path* through the protected-store writer and return its resolved path.

    Refuses any path inside ANY repository checkout this process can name — the
    protected store must never be reachable at a guessable in-repo location.
    """
    resolved = path.resolve()
    for root in _protected_roots():
        if resolved.is_relative_to(root):
            raise ValueError(
                f"refusing to seed the protected store inside the checkout {root} ({resolved}); "
                "set OMNIAGENTOS_EVAL_PROTECTED or pass an out-of-repo --path "
                "(pentest invariant: var/eval_protected.db must not exist)"
            )
    with ProtectedGrader(str(resolved)) as grader:
        grader.put_expected_many(DEFAULT_CASES)
    return resolved


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--path",
        type=Path,
        default=None,
        help=(
            "runtime protected-store path (default: OMNIAGENTOS_EVAL_PROTECTED, "
            "else a per-process out-of-checkout ephemeral path)"
        ),
    )
    args = parser.parse_args()
    path = args.path if args.path is not None else Path(default_protected_db_path())
    try:
        print(seed_holdout(path))
    except ValueError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
