#!/usr/bin/env python3
"""Offline validator for loop artifact envelopes (schema/envelope.schema.json).

    python3 bridge/validate_envelope.py <file.json> [more.json ...]

One line per file: `OK <path>` or `FAIL <path>: <location>: <message>`.
Exit 0 if every file validated, exit 1 if any file failed or could not be
read, exit 2 if jsonschema is not importable (validation could not run at
all — a distinct exit code so this is never mistaken for "all valid"; if
this happens, the stderr message names a conforming interpreter to re-run
under, discovered from $VIRTUAL_ENV / $LOOP_WORKDIR, if one is found).

This tool's `exit 2 = could not run` is the ratified estate convention
(operator Ruling #4, 2026-08-09): exit 2 means the instrument/gate COULD NOT
RUN this input — distinct from a candidate defect (1) and from do-not-retry.
file_proposal.py / file_inquiry.py were realigned to it (they previously put
could-not-run at 3); see CONTRACT.md §8.

The schema's `$id` is the absolute URL
`https://omniagentos.local/schema/loops/envelope-v1.1.json` (referenced by
profile/omniagentos.schema.json via `$ref`). This tool is OFFLINE by design —
it must never make a network request to resolve that URL. It builds a local
`referencing.Registry` that maps the `$id` straight to the schema file on
disk, so any `$ref` to it (now or from a schema added later) resolves
locally. If the `referencing` package is not importable (older jsonschema),
this falls back to plain `jsonschema.validate()`, which is still correct for
schema/envelope.schema.json today because that schema does not itself
`$ref` anything external — the fallback only loses forward-compatibility
with a future schema that does.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schema"
ENVELOPE_SCHEMA_PATH = SCHEMA_DIR / "envelope.schema.json"


# THE SINGLE shared implementation of interpreter discovery. bridge/
# file_proposal.py and bridge/integration.py both import these two functions
# from here rather than keeping their own copies -- three duplicated copies
# of this probe is exactly this codebase's #1 recurring defect pattern (one
# bug becomes 2-13 bugs across clone families), and it already bit this
# plan once: subprocess.run(..., timeout=10) here originally caught only
# OSError, so a genuinely hanging candidate interpreter raised
# subprocess.TimeoutExpired UNCAUGHT (it is not an OSError subclass) and
# turned the documented could-not-run contract — exit 2 in validate_envelope.py
# and file_proposal.py (Ruling #4 realigned file_proposal from its old exit 3),
# 'unavailable' in integration._schema_validate — into a raw traceback,
# identically in all three copies. Fixed here, once.
def _discover_conforming_interpreter() -> tuple[str | None, list[str]]:
    """Search for a Python interpreter under which ``import jsonschema`` works.

    Order: ``$VIRTUAL_ENV/bin/python``, then ``$LOOP_WORKDIR/.venv/bin/python``.
    Never a hardcoded absolute path — ThreeLoops must not couple to one
    project repo's venv; it has none of its own. Returns
    ``(path or None, candidates probed)`` so a caller can report either a
    remedy or exactly what it looked for. A candidate that cannot even be
    invoked (``OSError``) or that hangs (``subprocess.TimeoutExpired``) is
    treated the same way — skipped, not fatal to the probe as a whole.
    """
    candidates: list[Path] = []
    venv = os.environ.get("VIRTUAL_ENV")
    if venv:
        candidates.append(Path(venv) / "bin" / "python")
    workdir = os.environ.get("LOOP_WORKDIR")
    if workdir:
        candidates.append(Path(workdir) / ".venv" / "bin" / "python")
    probed = [str(c) for c in candidates]
    for cand in candidates:
        if not cand.exists():
            continue
        try:
            result = subprocess.run(
                [str(cand), "-c", "import jsonschema"],
                capture_output=True, timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if result.returncode == 0:
            return str(cand), probed
    return None, probed


def _no_conforming_interpreter_hint(script: Path, argv: list[str] | None = None) -> str:
    """`script` is the caller's OWN __file__ (not necessarily this module's) —
    the re-run command must name the tool that actually failed, not this
    helper module."""
    found, probed = _discover_conforming_interpreter()
    if found:
        rerun_argv = argv or []
        rerun = " ".join([found, str(script), *rerun_argv])
        return f"re-run under a conforming interpreter: {rerun}"
    if probed:
        return f"no conforming interpreter found; candidates probed: {', '.join(probed)}"
    return ("no conforming interpreter found; $VIRTUAL_ENV and $LOOP_WORKDIR are both unset, "
            "so nothing was probed")


def _build_validator():
    """Returns (validate_fn, note) where validate_fn(instance) raises
    jsonschema.ValidationError on failure, or None on success. `note` is a
    string describing which resolution path was used (for --help/debugging;
    not printed in normal per-file output)."""
    try:
        import jsonschema
    except ImportError as exc:
        print(f"jsonschema is not importable — cannot validate ({exc}). "
              f"{_no_conforming_interpreter_hint(Path(__file__).resolve(), sys.argv[1:])}",
              file=sys.stderr)
        sys.exit(2)

    schema = json.loads(ENVELOPE_SCHEMA_PATH.read_text())

    try:
        from referencing import Registry, Resource
        from referencing.jsonschema import DRAFT202012

        # Local registry: the schema's own $id resolves to itself on disk, so
        # any $ref to https://omniagentos.local/schema/loops/envelope-v1.1.json
        # (from this file or a schema that imports it) never hits the network.
        resource = Resource.from_contents(schema, default_specification=DRAFT202012)
        registry = Registry().with_resource(schema["$id"], resource)
        validator_cls = jsonschema.Draft202012Validator
        validator = validator_cls(schema, registry=registry)
        return validator.validate, "referencing.Registry (local $id resolution)"
    except ImportError:
        # Older jsonschema without `referencing`. Safe fallback ONLY because
        # envelope.schema.json does not itself $ref anything external today —
        # see module docstring.
        def _validate(instance):
            jsonschema.validate(instance, schema)
        return _validate, "jsonschema.validate() fallback (no referencing package)"


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__.strip().splitlines()[2].strip(), file=sys.stderr)
        return 2

    validate, _note = _build_validator()  # exits 2 (with a remedy) if jsonschema is unimportable

    import jsonschema  # now confirmed importable by _build_validator

    any_fail = False
    for path_str in argv[1:]:
        path = Path(path_str)
        try:
            instance = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            print(f"FAIL {path}: <read>: {exc}")
            any_fail = True
            continue
        try:
            validate(instance)
        except jsonschema.ValidationError as exc:
            loc = "/".join(str(p) for p in exc.absolute_path) or "<root>"
            print(f"FAIL {path}: {loc}: {exc.message}")
            any_fail = True
        else:
            print(f"OK {path}")

    return 1 if any_fail else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
