#!/usr/bin/env python3
"""Absence matrix for Integration's risky-diff build-review guard.

Runs the REAL bridge.integration.validate() over a matrix that varies ONLY the
`verdicts` field over a real risky diff. Every other field is held identical and
valid, so the sole independent variable is the shape of review disclosure.

Probe discipline (this host has bitten six lanes): the module path is
hard-asserted against this worktree before anything is measured, because
.zshenv prepends the serving checkout to PYTHONPATH and the serving venv
carries an editable .pth pin to it.

Usage:  python3 tests/verdict_absence_matrix.py
Exit 0 always — this prints a matrix, it does not judge.  The judging lives in
tests/test_crosslineage_guard.py.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from bridge import integration as I  # noqa: E402
from bridge.canonical import content_id  # noqa: E402

# --- probe discipline: refuse to measure the wrong tree ---------------------
_actual = Path(I.__file__).resolve()
_want = (HERE / "bridge" / "integration.py").resolve()
assert _actual == _want, f"imported the WRONG integration.py: {_actual} != {_want}"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=True).stdout.strip()


def scratch_repo(tmp: Path) -> tuple[Path, str, str]:
    """A real risky two-commit candidate; third return is its immutable tip."""
    repo = tmp / "repo"
    repo.mkdir()
    _git(repo.parent, "init", "-q", "-b", "main", str(repo))
    _git(repo, "config", "user.email", "probe@example.invalid")
    _git(repo, "config", "user.name", "probe")
    target = repo / "auth" / "permissions.py"
    target.parent.mkdir(parents=True)
    target.write_text("base\n")
    _git(repo, "add", "auth/permissions.py")
    _git(repo, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "-b", "lane/probe")
    target.write_text("changed\n")
    _git(repo, "commit", "-qam", "change")
    tip = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "main")
    return repo, base, tip


def envelope(base: str, branch: str, verdicts, *, producer_extra=None) -> dict:
    payload = {"fixes": "probe", "note": "verdict-shape matrix"}
    producer = {"role": "implementer", "actor": "implementer-loop@probe",
                "model": "probe-1", "lineage": "anthropic"}
    if producer_extra:
        producer.update(producer_extra)
        # `None` in the override means "drop this key entirely" — an omitted
        # field and a null field are different absence shapes and both matter.
        producer = {k: v for k, v in producer.items() if v is not None}
    art = {
        "contract": "v1.1",
        "kind": "candidate",
        "title": "verdict-shape probe",
        "id": content_id(payload),
        "created_at": "2026-08-08T00:00:00Z",
        "producer": producer,
        "base_sha": base,
        "head_sha": branch,
        "branch": branch,
        "paths": ["auth/permissions.py"],
        "evidence": [{"claim": "probe", "verified_by": "execution",
                      "command": "true", "exit_code": 0}],
        "payload": payload,
    }
    if verdicts is not ABSENT:
        if isinstance(verdicts, list):
            verdicts = [
                ({**v, "reviewed_sha": v.get("reviewed_sha", branch)}
                 if isinstance(v, dict) else v)
                for v in verdicts
            ]
        art["verdicts"] = verdicts
    return art


class _Absent:
    def __repr__(self) -> str:
        return "<no verdicts key>"


ABSENT = _Absent()

# The matrix.  Each row is a way a candidate can arrive at the gate having had
# NO cross-lineage review, plus the two controls at the bottom.
SHAPES: list[tuple[str, object, dict | None]] = [
    ("no `verdicts` key at all",              ABSENT, None),
    ("verdicts: null",                        None, None),
    ("verdicts: [] (empty list)",             [], None),
    ("verdicts: {} (object, not a list)",     {}, None),
    ('verdicts: "approved" (string)',         "approved", None),
    ("verdicts: [{}] (entry with no lineage)", [{}], None),
    ("verdicts: [<non-dict>]",                ["approve"], None),
    ("same-lineage verdict, `lineage` omitted",
     [{"reviewer": "opus", "verdict": "approve"}], None),
    ("same-lineage + a lineage-less second entry",
     [{"reviewer": "opus", "lineage": "anthropic", "verdict": "approve"},
      {"reviewer": "mystery", "verdict": "approve"}], None),
    # producer-side absence: the guard is gated on `producer_lineage` being
    # truthy, so a house producer that simply omits its OWN lineage skips the
    # whole check.  Same favourable-absence class, opposite side of the compare.
    ("producer.lineage omitted, same-lineage review",
     [{"reviewer": "opus", "lineage": "anthropic", "verdict": "approve"}],
     {"lineage": None}),
    ("producer.lineage omitted, no verdicts at all", ABSENT, {"lineage": None}),
    # --- controls -------------------------------------------------------
    ("CONTROL honest same-lineage (must REFUSE)",
     [{"reviewer": "opus", "lineage": "anthropic", "verdict": "approve"}], None),
    ("CONTROL proper cross-lineage (must ADMIT)",
     [{"reviewer": "gemini", "lineage": "google", "verdict": "approve"}], None),
    ("CONTROL declared third-party, no verdicts (must REFUSE for risky work)",
     ABSENT, {"third_party": True}),
]

_N_CONTROLS = 3


def run_candidate(repo: Path, root: Path, schema_dir: Path, art: dict):
    """Run the real validate() and hand back the whole Candidate.

    Callers that only need the decision use run_shape(); callers that need to
    assert on `warnings` (the third-party exemption must never be silent) need
    the object itself.
    """
    cand = I.Candidate(ident=art["id"], path=Path("/dev/null"), art=art)
    ledger = I.LedgerView(events=[], torn_tail=False)
    I.validate(cand, repo, root, schema_dir, ledger, {}, set())
    return cand


def run_shape(repo: Path, root: Path, schema_dir: Path, art: dict) -> tuple[str, str]:
    cand = run_candidate(repo, root, schema_dir, art)
    if cand.refusal is None:
        return "ADMITTED", ""
    return "REFUSED", cand.refusal.reason


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        repo, base, branch = scratch_repo(tmp)
        root = tmp / "root"
        (root / "var" / "loopqueue").mkdir(parents=True)
        schema_dir = HERE / "schema"

        # An absent validator silently skips half the admission path, which is
        # how the first run of this matrix produced a favourable reading.
        try:
            import jsonschema  # noqa: F401
            print("jsonschema: available — full envelope validation IS running")
        except ImportError:
            print("jsonschema: MISSING — envelope validation is SKIPPED; this "
                  "matrix is measuring less than the real gate does. Re-run on "
                  "an interpreter that has it before believing any row.")
        print(f"module under test: {_actual}")
        print(f"{'shape':45} {'result':10} reason")
        print("-" * 110)
        results = {}
        for label, verdicts, extra in SHAPES:
            art = envelope(base, branch, verdicts, producer_extra=extra)
            outcome, reason = run_shape(repo, root, schema_dir, art)
            results[label] = outcome
            print(f"{label:45} {outcome:10} {reason[:60]}")

        bypasses = [k for k, v in results.items()
                    if v == "ADMITTED" and not k.startswith("CONTROL")]
        print("-" * 110)
        print(f"absence shapes admitted without any cross-lineage review: "
              f"{len(bypasses)} of {len(SHAPES) - _N_CONTROLS}")
        for b in bypasses:
            print(f"  BYPASS: {b}")
        print(json.dumps(results, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
