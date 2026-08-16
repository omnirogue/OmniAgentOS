#!/usr/bin/env python3
"""Verify a merged tree's contracts/openapi.json is schema-neutral (FIX 2, 2026-08-04).

``merge-gate.sh``'s ``openapi-drift`` step refuses, on the DIFF alone, any
candidate that edits ``omniagentos/api/*.py`` without also touching
``contracts/openapi.json``. That path-implication floor is deliberately the
fast path (milliseconds) but it is UNSATISFIABLE for a real, schema-neutral
edit: regenerating the contract from such a candidate comes back
BYTE-IDENTICAL, so git has no diff entry to show for the contract at all, and
the floor refuses a candidate that changed nothing observable
(fix/migrate-startup-gate-0804 wiring a startup-migration check into
``api/main.py`` is the live case that hit this).

This script is the EXPENSIVE second opinion the gate pays for only on that
rare combination: given an already-materialized tree (the candidate merged
onto ``HEAD`` — materializing that tree is ``merge-gate.sh``'s job, not
this script's), regenerate ``contracts/openapi.json`` from it and byte-compare
against what is already committed there.

Interpreter discipline
-----------------------
The regeneration runs the SAME interpreter that is running this script
(``sys.executable`` by default, or ``--python`` to override) as its OWN
subprocess, with ``PYTHONPATH`` pinned to the tree under test and nothing
else — so a ``sys.modules`` entry left over from THIS process's own imports
(e.g. an editable install of a different ``omniagentos`` on the caller's
``PYTHONPATH``) can never silently answer for the tree being checked. The
generated schema is written into a PARENT-allocated output file (never the
child's own choice, never the tree's tracked ``contracts/openapi.json``) and
read back as raw bytes — never through the child's stdout, where an
import-time ``print()`` or a non-UTF-8 locale would silently corrupt the
artifact and produce a spurious DRIFT refusal on a candidate that changed
nothing (review 2026-08-04, F3).

Candidate-code isolation (review 2026-08-04, F1)
--------------------------------------------------
The regen subprocess IMPORTS the CANDIDATE's own ``omniagentos/api/main.py``
— untrusted merged-tree content, executing inside the gate. The child's
environment is therefore never a bare copy of this process's own: the live
``OMNIAGENTOS_GATE_WORKSPACE`` pin is scrubbed and ``OMNIAGENTOS_DB``/
``_VAR_DIR``/``_LEDGER_DIR`` are pointed at scratch paths under the tree
itself, so candidate import-time code can see no live gate coordination
state. This is independent of, and does not trust, whatever
``merge-gate.sh``'s own shell invocation of this script does — the same
scrub applied at both layers is the point.

Bounded, fail-closed
---------------------
The subprocess call is bounded by ``--timeout`` seconds (default 45s — the
whole point is staying far cheaper than the ~12-minute ladder this step runs
ahead of). Anything that stops verification from completing — a missing
contract, an import that resolves outside the tree, a non-zero regen, or the
timeout — is reported as UNVERIFIED, never as "identical". A regen that
failed specifically because the interpreter is missing a dependency
(``ModuleNotFoundError``) is reported as an ENVIRONMENT problem rather than
folded into the generic failure text, so a reviewer does not mistake "this
venv lacks fastapi" for "this candidate's API changed". Either way the
caller (``merge-gate.sh``) treats UNVERIFIED exactly like a confirmed
difference: refuse. An unverifiable regen is not a pass.

    openapi_drift_check.py <merged-tree-path> [--timeout SECONDS] [--python PATH]

Exit codes: 0 schema-neutral (verified identical) · 1 verified DIFFERENT ·
2 could not verify (fail closed).
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TIMEOUT_SECONDS = 45.0

# Executed as `python -c <this>` inside the SUBPROCESS, with PYTHONPATH (and,
# belt-and-suspenders, sys.path[0]) pinned to the tree under test — so the
# import authority is decided entirely by argv[1], never by whatever the
# calling process happened to already have loaded. Mirrors the same shadow
# guard scripts/refresh-contracts.sh applies to the live checkout. argv[2] is
# a PARENT-allocated output path: this snippet only ever writes BYTES into
# it, never chooses its own location or format (F3).
_GENERATE_SNIPPET = """
import sys
import tempfile
from pathlib import Path

root = Path(sys.argv[1]).resolve()
output_path = Path(sys.argv[2])
sys.path.insert(0, str(root))

import omniagentos  # noqa: E402

resolved = Path(omniagentos.__file__).resolve()
if root not in resolved.parents:
    print(f"omniagentos resolves to {resolved}, not under {root}", file=sys.stderr)
    raise SystemExit(3)

from scripts.generate_openapi import generate_openapi_artifact  # noqa: E402

with tempfile.TemporaryDirectory() as stage:
    artifact = generate_openapi_artifact(Path(stage))
    output_path.write_bytes(artifact.read_bytes())
"""


@dataclass(frozen=True)
class DriftVerdict:
    """``ok=False`` means verification itself did not complete.

    Callers MUST fail closed on ``ok=False`` — ``identical`` is meaningless
    in that case, never treat it as a pass.
    """

    ok: bool
    identical: bool
    detail: str


def verify_schema_neutral(
    tree: Path,
    *,
    python: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> DriftVerdict:
    """Regenerate ``contracts/openapi.json`` from ``tree`` and byte-compare it.

    Never writes into ``tree``'s own ``contracts/openapi.json``: the schema
    is generated into a throwaway temp directory inside the bounded
    subprocess, copied into a PARENT-allocated output file, and read back as
    raw bytes (F3) — the child's stdout is diagnostic-only.
    """
    interpreter = Path(python) if python is not None else Path(sys.executable)
    contract_path = tree / "contracts" / "openapi.json"
    if not contract_path.is_file():
        return DriftVerdict(ok=False, identical=False, detail=f"no contract at {contract_path}")
    try:
        committed = contract_path.read_bytes()
    except OSError as exc:
        return DriftVerdict(
            ok=False, identical=False, detail=f"cannot read committed contract: {exc}"
        )

    # F1: the regen subprocess imports the CANDIDATE's own merged-tree code.
    # Scrub the live gate-workspace pin and point DB/VAR_DIR/LEDGER at scratch
    # paths under `tree` itself — independent of, and not trusting, whatever
    # merge-gate.sh's OWN invocation of this script already scrubbed.
    scratch_var = tree / "var" / "openapi-drift-check"
    child_env = dict(os.environ)
    child_env.pop("OMNIAGENTOS_GATE_WORKSPACE", None)
    child_env["PYTHONPATH"] = str(tree)
    child_env["OMNIAGENTOS_DB"] = str(scratch_var / "state.sqlite3")
    child_env["OMNIAGENTOS_VAR_DIR"] = str(scratch_var)
    child_env["OMNIAGENTOS_LEDGER_DIR"] = str(scratch_var / "ledger")

    with tempfile.TemporaryDirectory() as parent_stage:
        # F3: allocated HERE, in the parent, before the child ever runs — the
        # child receives a path to write bytes into, never a format or a
        # location it gets to choose.
        output_file = Path(parent_stage) / "generated-openapi.json"
        try:
            proc = subprocess.run(
                [str(interpreter), "-c", _GENERATE_SNIPPET, str(tree), str(output_file)],
                cwd=tree,
                env=child_env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return DriftVerdict(
                ok=False, identical=False, detail=f"regen timed out after {timeout:g}s"
            )
        except OSError as exc:
            return DriftVerdict(
                ok=False, identical=False, detail=f"could not launch interpreter: {exc}"
            )

        if proc.returncode != 0:
            stderr_tail = " ".join(proc.stderr.split())[:400]
            # F7: a missing dependency in the interpreter is an ENVIRONMENT
            # problem, not evidence the candidate's API drifted — naming it
            # separately stops a reviewer from chasing a schema change that
            # was never there.
            if "ModuleNotFoundError" in proc.stderr:
                return DriftVerdict(
                    ok=False,
                    identical=False,
                    detail=f"openapi-regen environment problem: {stderr_tail}",
                )
            return DriftVerdict(
                ok=False,
                identical=False,
                detail=f"regen failed (rc={proc.returncode}): {stderr_tail}",
            )

        if not output_file.is_file():
            return DriftVerdict(
                ok=False, identical=False, detail="regen exited 0 but wrote no artifact"
            )
        generated = output_file.read_bytes()

    if generated == committed:
        return DriftVerdict(ok=True, identical=True, detail="identical to committed contract")
    return DriftVerdict(ok=True, identical=False, detail="differs from committed contract")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("tree", help="path to the already-materialized (merged) candidate tree")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument(
        "--python", default=None, help="interpreter to regenerate with (default: this one)"
    )
    args = parser.parse_args(argv)

    tree = Path(args.tree).resolve()
    if not tree.is_dir():
        print(
            f"UNVERIFIED: not a directory: {tree} — refusing rather than assuming schema-neutral",
            file=sys.stderr,
        )
        return 2

    verdict = verify_schema_neutral(tree, python=args.python, timeout=args.timeout)
    if not verdict.ok:
        print(
            f"UNVERIFIED: {verdict.detail} — refusing rather than assuming schema-neutral",
            file=sys.stderr,
        )
        return 2
    if verdict.identical:
        print(f"SCHEMA-NEUTRAL: {verdict.detail}")
        return 0
    print(f"DRIFT: {verdict.detail}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
