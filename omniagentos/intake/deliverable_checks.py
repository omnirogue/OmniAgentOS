"""Fail-closed, dependency-free deliverable checker for the intake real-harness path.

Invocation-agnostic: this module is inert on its own — nothing calls it yet.
Lane 2 (a separate change) materializes it into an armed run's workspace and
appends a `validate` plan step that invokes it. It is written to be runnable
BOTH ways:

    python3 deliverable_checks.py --spec <path/to/spec.json>
    from omniagentos.intake.deliverable_checks import main

It reads a JSON "deliverable spec" describing a list of deterministic checks,
resolves every path in the spec against the current working directory (which
the caller is responsible for setting to the run workspace), and runs each
check with NO network access and NO LLM calls.

FAIL-CLOSED IS THE WHOLE POINT: a missing spec file, an unparseable spec, an
empty `checks` list, or a check of an unknown kind must NEVER read as a pass.
Absence must never read favourably (this mirrors the existing fail-closed
idiom in omniagentos/swarm/scheduler.py's `default_verifier`, which refuses a
vacuous pass rather than treating "nothing to check" as success).

Spec shape (JSON object):
    {
      "checks": [
        {"kind": "produced_output"},
        {"kind": "file_exists", "path": "report.md"},
        {"kind": "must_include", "path": "report.md", "text": "Summary"},
        {"kind": "must_not_include", "path": "report.md", "text": "TODO"}
      ]
    }

Check kinds (v1):
  - produced_output: at least one file under the workspace (excluding the
    `.omniagentos/` control dir) that is non-empty and has an mtime at or
    after the spec file's own mtime. This is the floor check.
  - file_exists: `{path}` exists and is a non-empty file.
  - must_include: a literal substring (NOT a regex) is present in the named
    file's text.
  - must_not_include: a literal substring is absent from the named file's
    text.

Every path in a check is confined to the workspace by INODE ancestry, using
the estate's shared primitive (`omniagentos.path_containment`), never by
comparing resolved path spellings: the components the primitive proves to be
below the workspace are re-joined onto the workspace's own canonical root, so
the path a check finally opens is the proved one rather than the spelling the
spec supplied. Absolute paths, `..` traversal, symlink escapes, and any
undecidable answer are all refused. Because containment is delegated rather
than reimplemented here, materializing this checker into a run workspace must
copy `omniagentos/path_containment.py` beside it (it is itself stdlib-only);
without it the checker refuses to start rather than falling back to a lexical
compare. Free text
from acceptance_criteria never reaches argv — it only ever flows through the
JSON spec file this module reads, so there is no shell-injection surface.

On success (all checks pass) this prints one JSON receipt to stdout and
exits 0. On any failure — spec-level or check-level — it still prints a JSON
receipt (so callers always get a parseable result) and exits 1. The receipt
always enumerates every check that was evaluated, by kind and target, with
an explicit pass/fail — never a bare "pass" — plus this checker's own
sha256, so tampering with the copied checker is detectable after the fact.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

# Containment is decided by the shared inode primitive, never by a string
# compare of resolved spellings.  When this file is materialized into a run
# workspace the package is not importable, so path_containment.py is loaded
# from beside it; if it is absent the import fails loudly.  There is
# deliberately NO lexical fallback — a second copy of the primitive here is a
# second implementation to keep correct, and a silent downgrade is exactly the
# favourable-absence shape this checker exists to close.
try:
    from omniagentos.path_containment import inode_paths_equal, inode_relative_parts
except ImportError:  # pragma: no cover - standalone materialized copy
    from path_containment import (  # type: ignore[import-not-found, no-redef]
        inode_paths_equal,
        inode_relative_parts,
    )

KNOWN_CHECK_KINDS = frozenset(
    {"produced_output", "file_exists", "must_include", "must_not_include"}
)

CONTROL_DIR_NAME = ".omniagentos"


def _checker_sha256() -> str:
    """Sha256 of this module's own source, so tampering is detectable."""
    try:
        data = Path(__file__).read_bytes()
    except OSError:
        return ""
    return hashlib.sha256(data).hexdigest()


def _resolve_confined(cwd: Path, rel_path: Any) -> tuple[Path | None, str | None]:
    """Resolve ``rel_path`` against ``cwd`` and refuse anything outside it.

    Returns (resolved_path, None) on success, or (None, reason) on refusal.
    Absolute paths, ``..`` traversal, symlink escapes, and an undecidable
    answer are refused uniformly: containment is proved by inode ancestry
    (``inode_relative_parts``), which returns ``None`` for all four.

    The returned path is the workspace's canonical root re-joined with the
    components the primitive *proved* to be below it — never the caller's
    spelling.  That distinction matters: a spelling like ``link/../x`` whose
    ``..`` the kernel would apply only after following ``link`` cannot be
    handed onward, because what this function returns is the contained path,
    not the requested one.
    """
    if not isinstance(rel_path, str) or not rel_path:
        return None, "missing or invalid 'path' field"
    try:
        cwd_resolved = cwd.resolve()
    except OSError as exc:
        return None, f"could not resolve workspace: {exc}"
    # The proof is guarded here rather than trusting the primitive's own
    # internal error handling: this module's contract is that a caller ALWAYS
    # gets a parseable receipt, so a filesystem error must become a refusal,
    # never an uncaught exception (and never an admission).
    try:
        parts = inode_relative_parts(cwd_resolved / rel_path, cwd_resolved)
    except (OSError, ValueError) as exc:
        return None, f"could not prove containment for {rel_path!r}: {exc}"
    if parts is None:
        return None, f"path escapes workspace: {rel_path!r}"
    return cwd_resolved.joinpath(*parts), None


def _check_produced_output(cwd: Path, spec_path: Path) -> tuple[bool, str]:
    try:
        spec_mtime = spec_path.stat().st_mtime
    except OSError as exc:
        return False, f"could not stat spec file: {exc}"

    cwd_resolved = cwd.resolve()

    for root, dirs, files in os.walk(cwd_resolved):
        dirs[:] = [d for d in dirs if d != CONTROL_DIR_NAME]
        root_path = Path(root)
        for name in files:
            candidate = root_path / name
            try:
                st = candidate.stat()
            except OSError:
                continue
            if st.st_size <= 0 or st.st_mtime < spec_mtime:
                continue
            # The spec file is never its own produced output.  Identity is
            # decided by inode, and an undecidable answer skips the candidate:
            # skipping can only withhold a pass, never grant one.
            #
            # os.walk does not follow symlinked directories, but a symlinked
            # FILE inside the workspace still appears here and stat() follows
            # it.  Requiring proved containment stops outside content from
            # being credited as this run's output.
            #
            # A filesystem error in either proof skips the candidate for the
            # same reason: unprovable is not produced, and the walk continues
            # so the caller still receives a receipt.
            try:
                if inode_paths_equal(candidate, spec_path) is not False:
                    continue
                parts = inode_relative_parts(candidate, cwd_resolved)
            except (OSError, ValueError):
                continue
            if parts is None:
                continue
            return True, f"found produced output: {Path(*parts)}"

    return False, "no non-empty file at or after the spec's mtime was found in the workspace"


def _check_file_exists(cwd: Path, target: Any) -> tuple[bool, str]:
    resolved, err = _resolve_confined(cwd, target)
    if err is not None or resolved is None:
        return False, err or f"could not resolve: {target}"
    if not resolved.exists() or not resolved.is_file():
        return False, f"file does not exist: {target}"
    try:
        size = resolved.stat().st_size
    except OSError as exc:
        return False, f"could not stat file {target}: {exc}"
    if size <= 0:
        return False, f"file is empty: {target}"
    return True, f"file exists and is non-empty: {target}"


def _read_text(resolved: Path) -> tuple[str | None, str | None]:
    try:
        return resolved.read_text(errors="replace"), None
    except OSError as exc:
        return None, f"could not read file: {exc}"


def _check_must_include(cwd: Path, target: Any, text: Any) -> tuple[bool, str]:
    if not isinstance(text, str) or not text:
        return False, "missing or invalid 'text' field"
    resolved, err = _resolve_confined(cwd, target)
    if err is not None or resolved is None:
        return False, err or f"could not resolve: {target}"
    if not resolved.exists() or not resolved.is_file():
        return False, f"file does not exist: {target}"
    content, err = _read_text(resolved)
    if content is None:
        return False, err or f"could not read file: {target}"
    if text in content:
        return True, f"found required substring in {target}"
    return False, f"required substring not found in {target}"


def _check_must_not_include(cwd: Path, target: Any, text: Any) -> tuple[bool, str]:
    if not isinstance(text, str) or not text:
        return False, "missing or invalid 'text' field"
    resolved, err = _resolve_confined(cwd, target)
    if err is not None or resolved is None:
        return False, err or f"could not resolve: {target}"
    if not resolved.exists() or not resolved.is_file():
        return False, f"file does not exist: {target}"
    content, err = _read_text(resolved)
    if content is None:
        return False, err or f"could not read file: {target}"
    if text not in content:
        return True, f"forbidden substring absent from {target}"
    return False, f"forbidden substring present in {target}"


def _run_check(cwd: Path, spec_path: Path, check: Any) -> dict[str, Any]:
    if not isinstance(check, dict):
        return {
            "kind": None,
            "target": None,
            "ok": False,
            "detail": "check entry is not a JSON object",
        }

    kind = check.get("kind")
    target = check.get("path")

    if kind == "produced_output":
        ok, detail = _check_produced_output(cwd, spec_path)
    elif kind == "file_exists":
        ok, detail = _check_file_exists(cwd, target)
    elif kind == "must_include":
        ok, detail = _check_must_include(cwd, target, check.get("text"))
    elif kind == "must_not_include":
        ok, detail = _check_must_not_include(cwd, target, check.get("text"))
    elif kind not in KNOWN_CHECK_KINDS:
        # An unknown check kind must NEVER be skipped-as-passed — that is
        # the exact favourable-absence shape this checker exists to close.
        ok, detail = False, f"unknown check kind: {kind!r}"
    else:  # pragma: no cover - defensive, unreachable given KNOWN_CHECK_KINDS
        ok, detail = False, f"unhandled check kind: {kind!r}"

    return {"kind": kind, "target": target, "ok": ok, "detail": detail}


def _build_receipt(
    ok: bool, reason: str | None, checks: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "ok": ok,
        "reason": reason,
        "checks": checks,
        "checker_sha256": _checker_sha256(),
    }


def _emit(receipt: dict[str, Any]) -> None:
    print(json.dumps(receipt, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="deliverable_checks",
        description=(
            "Fail-closed deterministic deliverable checker for the intake "
            "real-harness path. Reads a JSON spec of checks, runs them "
            "against the current working directory, and prints a JSON "
            "receipt enumerating every check."
        ),
    )
    parser.add_argument(
        "--spec", required=True, help="Path to the JSON deliverable spec file."
    )
    args = parser.parse_args(argv)

    cwd = Path.cwd()
    spec_path = Path(args.spec)
    if not spec_path.is_absolute():
        spec_path = cwd / spec_path

    if not spec_path.exists() or not spec_path.is_file():
        _emit(_build_receipt(False, f"spec file not found: {args.spec}", []))
        return 1

    try:
        raw = spec_path.read_text()
    except OSError as exc:
        _emit(_build_receipt(False, f"could not read spec file: {exc}", []))
        return 1

    try:
        spec = json.loads(raw)
    except json.JSONDecodeError as exc:
        _emit(_build_receipt(False, f"spec file is not valid JSON: {exc}", []))
        return 1

    if not isinstance(spec, dict):
        _emit(_build_receipt(False, "spec file must contain a JSON object", []))
        return 1

    checks_spec = spec.get("checks")
    if not isinstance(checks_spec, list) or len(checks_spec) == 0:
        _emit(
            _build_receipt(
                False, "spec 'checks' list is missing or empty — refusing a vacuous pass", []
            )
        )
        return 1

    results = [_run_check(cwd, spec_path, check) for check in checks_spec]
    overall_ok = all(r["ok"] for r in results)
    reason = None if overall_ok else "one or more deliverable checks failed"
    _emit(_build_receipt(overall_ok, reason, results))
    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(main())
