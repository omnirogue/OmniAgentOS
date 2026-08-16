"""Unit tests for ``scripts/openapi_drift_check.py`` (FIX 2, 2026-08-04).

``merge-gate.sh``'s ``openapi-drift`` step refuses any candidate that edits
``omniagentos/api/*.py`` without also touching ``contracts/openapi.json`` —
a path-implication floor that is UNSATISFIABLE for a real, schema-neutral
edit (regenerating comes back byte-identical, so git has no diff entry to
show). This module is the bounded, fail-closed "actually regenerate and
byte-compare" second opinion the gate pays for on that one rare combination.

Testing the bash step directly would need a REAL ``omniagentos.api.main`` /
FastAPI app inside a throwaway git worktree merge — expensive and, per the
brief, unnecessary: these tests exercise the EXTRACTED helper directly
against tiny synthetic FastAPI trees (never the real, large repo tree), which
is faster and pins the exact behaviour this helper promises:

  * a schema-neutral edit (no route change) verifies IDENTICAL
  * a route-adding edit verifies DIFFERENT (drift)
  * anything that stops verification from completing — missing contract,
    an import that fails, the bounded timeout — reports ``ok=False`` and
    NEVER ``identical=True``: an unverifiable regen is not a pass
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.openapi_drift_check import DriftVerdict, main, verify_schema_neutral

_MAIN_PY_TEMPLATE = """
{sleep_prelude}from fastapi import FastAPI

app = FastAPI(title="drift-check-fixture", version="0.0.1")


@app.get("/health")
def health() -> dict[str, str]:
    return {{"status": "ok"}}


{extra_body}
"""

_GENERATE_OPENAPI_PY = """
from __future__ import annotations

import json
from pathlib import Path

from fastapi.openapi.utils import get_openapi

from omniagentos.api.main import app


def generate_openapi_artifact(repo_root: Path) -> Path:
    schema = get_openapi(
        title=app.title,
        version=app.version,
        openapi_version=app.openapi_version,
        routes=app.routes,
    )
    contract_path = repo_root / "contracts" / "openapi.json"
    contract_path.parent.mkdir(parents=True, exist_ok=True)
    contract_path.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\\n", encoding="utf-8")
    return contract_path
"""

_SEED_CONTRACT_SNIPPET = """
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root))

from scripts.generate_openapi import generate_openapi_artifact

generate_openapi_artifact(root)
"""


def _write_main_py(tree: Path, *, extra_body: str = "", sleep_seconds: float | None = None) -> None:
    sleep_prelude = f"import time\ntime.sleep({sleep_seconds!r})\n\n" if sleep_seconds else ""
    (tree / "omniagentos" / "api" / "main.py").write_text(
        _MAIN_PY_TEMPLATE.format(sleep_prelude=sleep_prelude, extra_body=extra_body),
        encoding="utf-8",
    )


def _build_tree(tmp_path: Path, *, extra_body: str = "") -> Path:
    """A minimal, real, importable FastAPI tree — never the actual (large) repo."""
    tree = tmp_path / "tree"
    (tree / "omniagentos" / "api").mkdir(parents=True)
    (tree / "omniagentos" / "__init__.py").write_text("", encoding="utf-8")
    (tree / "omniagentos" / "api" / "__init__.py").write_text("", encoding="utf-8")
    _write_main_py(tree, extra_body=extra_body)
    (tree / "scripts").mkdir(parents=True)
    (tree / "scripts" / "__init__.py").write_text("", encoding="utf-8")
    (tree / "scripts" / "generate_openapi.py").write_text(_GENERATE_OPENAPI_PY, encoding="utf-8")
    return tree


def _seed_committed_contract(tree: Path) -> None:
    """Write tree/contracts/openapi.json for real — a fresh subprocess per call.

    Deliberately NOT done via an in-process import: two tests importing
    `omniagentos.api.main` from two DIFFERENT tmp_path trees in the same
    pytest process would hit Python's module cache on the second import and
    silently grade the wrong tree. A subprocess per seed has no such cache.
    """
    subprocess.run(
        [sys.executable, "-c", _SEED_CONTRACT_SNIPPET, str(tree)],
        cwd=tree,
        env={**os.environ, "PYTHONPATH": str(tree)},
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert (tree / "contracts" / "openapi.json").is_file()


def _python() -> Path:
    return Path(sys.executable)


# ---------------------------------------------------------------------------
# verify_schema_neutral: the decisive property
# ---------------------------------------------------------------------------


def test_schema_neutral_edit_verifies_identical(tmp_path: Path) -> None:
    """An edit that touches main.py but adds no route regenerates byte-identical."""
    tree = _build_tree(tmp_path)
    _seed_committed_contract(tree)
    committed = (tree / "contracts" / "openapi.json").read_bytes()

    # The "candidate edit": an internal helper, not a route. Must not move the schema.
    _write_main_py(tree, extra_body="def _internal_helper() -> None:\n    return None\n")

    verdict = verify_schema_neutral(tree, python=_python())
    assert verdict == DriftVerdict(
        ok=True, identical=True, detail="identical to committed contract"
    )
    # And the committed artifact itself was never touched by verification.
    assert (tree / "contracts" / "openapi.json").read_bytes() == committed


def test_route_adding_edit_verifies_as_drift(tmp_path: Path) -> None:
    """An edit that adds a route must NOT be reported as schema-neutral."""
    tree = _build_tree(tmp_path)
    _seed_committed_contract(tree)

    _write_main_py(
        tree,
        extra_body=(
            '@app.get("/new-route")\n'
            "def new_route() -> dict[str, str]:\n"
            '    return {"status": "new"}\n'
        ),
    )

    verdict = verify_schema_neutral(tree, python=_python())
    assert verdict.ok is True
    assert verdict.identical is False
    assert "differs" in verdict.detail


def test_verification_never_writes_the_tracked_contract(tmp_path: Path) -> None:
    """The regen is staged in a throwaway temp dir — the tree's own file is read-only here."""
    tree = _build_tree(tmp_path)
    _seed_committed_contract(tree)
    before = (tree / "contracts" / "openapi.json").read_bytes()
    mtime_before = (tree / "contracts" / "openapi.json").stat().st_mtime_ns

    _write_main_py(
        tree,
        extra_body=(
            '@app.get("/new-route")\n'
            "def new_route() -> dict[str, str]:\n"
            '    return {"status": "new"}\n'
        ),
    )
    verify_schema_neutral(tree, python=_python())

    after = (tree / "contracts" / "openapi.json").read_bytes()
    assert after == before
    assert (tree / "contracts" / "openapi.json").stat().st_mtime_ns == mtime_before


# ---------------------------------------------------------------------------
# fail-closed: ok=False must never be read as a pass
# ---------------------------------------------------------------------------


def test_missing_committed_contract_fails_closed(tmp_path: Path) -> None:
    tree = _build_tree(tmp_path)
    # No contracts/openapi.json at all — never seeded.
    verdict = verify_schema_neutral(tree, python=_python())
    assert verdict.ok is False
    assert verdict.identical is False
    assert "no contract at" in verdict.detail


def test_broken_tree_fails_closed_rather_than_passing(tmp_path: Path) -> None:
    """A tree whose regen cannot even import must refuse, not silently pass."""
    tree = _build_tree(tmp_path)
    _seed_committed_contract(tree)
    # Corrupt the app module the generator imports.
    (tree / "omniagentos" / "api" / "main.py").write_text(
        "this is not ( valid python", encoding="utf-8"
    )

    verdict = verify_schema_neutral(tree, python=_python())
    assert verdict.ok is False
    assert verdict.identical is False
    assert "regen failed" in verdict.detail


def test_bounded_timeout_fails_closed(tmp_path: Path) -> None:
    """A pathologically slow regen must be bounded, not hang the gate."""
    tree = _build_tree(tmp_path)
    _seed_committed_contract(tree)
    _write_main_py(tree, sleep_seconds=5.0)

    verdict = verify_schema_neutral(tree, python=_python(), timeout=0.5)
    assert verdict.ok is False
    assert verdict.identical is False
    assert "timed out" in verdict.detail


def _build_shadow_tree(tmp_path: Path) -> Path:
    """A second, fully independent, real, importable FastAPI tree with a
    DIFFERENT schema — the package an ambient PYTHONPATH could resolve to if
    import resolution were not pinned to the tree under test. Never imported
    by the real checker; used only to prove the fixture below is decisive
    (review 2026-08-04, F2).
    """
    shadow = tmp_path / "shadow"
    (shadow / "omniagentos" / "api").mkdir(parents=True)
    (shadow / "omniagentos" / "__init__.py").write_text("", encoding="utf-8")
    (shadow / "omniagentos" / "api" / "__init__.py").write_text("", encoding="utf-8")
    (shadow / "omniagentos" / "api" / "main.py").write_text(
        "from fastapi import FastAPI\n\n"
        'app = FastAPI(title="SHADOW-PACKAGE-MUST-NEVER-BE-SEEN", version="9.9.9")\n\n\n'
        '@app.get("/from-shadow-only")\n'
        "def from_shadow_only() -> dict[str, str]:\n"
        '    return {"status": "shadow"}\n',
        encoding="utf-8",
    )
    (shadow / "scripts").mkdir(parents=True)
    (shadow / "scripts" / "__init__.py").write_text("", encoding="utf-8")
    (shadow / "scripts" / "generate_openapi.py").write_text(_GENERATE_OPENAPI_PY, encoding="utf-8")
    return shadow


def _naive_unpinned_schema_title(pythonpath: Path, *, python: Path) -> str:
    """What resolves with NO cwd pin to any tree and NO explicit PYTHONPATH
    override beyond the ambient one — the counterfactual this fixture must
    prove is reachable, so the protected assertion below is decisive rather
    than vacuous (F2: the prior version of this test pointed PYTHONPATH at an
    empty, non-package directory, so there was nothing there to shadow with).
    """
    result = subprocess.run(
        [str(python), "-c", "import omniagentos.api.main as m; print(m.app.title)"],
        cwd=pythonpath.parent,  # deliberately NOT the shadow and NOT any real tree
        env={**os.environ, "PYTHONPATH": str(pythonpath)},
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    return result.stdout.strip()


def test_ambient_pythonpath_shadow_package_never_wins_over_the_tree(tmp_path: Path) -> None:
    """F2 rebuild: a REAL, reachable, different-schema shadow package must
    lose to `tree`'s own copy, even when the CALLING process's ambient
    PYTHONPATH points straight at it.
    """
    tree = _build_tree(tmp_path)
    _seed_committed_contract(tree)
    committed = (tree / "contracts" / "openapi.json").read_bytes()
    shadow = _build_shadow_tree(tmp_path)

    # Mutation-check baseline: prove the shadow is REAL and reachable by a
    # resolution that is not pinned to `tree` — this is what "deleting the
    # pin" looks like. It MUST resolve the shadow, or this fixture is
    # decorative before the protected assertion even runs.
    naive_title = _naive_unpinned_schema_title(shadow, python=_python())
    assert naive_title == "SHADOW-PACKAGE-MUST-NEVER-BE-SEEN", (
        "the shadow fixture is not actually reachable — rebuilding this test "
        "would still be decorative"
    )

    previous = os.environ.get("PYTHONPATH")
    os.environ["PYTHONPATH"] = str(shadow)
    try:
        verdict = verify_schema_neutral(tree, python=_python())
    finally:
        if previous is None:
            os.environ.pop("PYTHONPATH", None)
        else:
            os.environ["PYTHONPATH"] = previous

    assert verdict == DriftVerdict(
        ok=True, identical=True, detail="identical to committed contract"
    ), verdict
    assert b"SHADOW-PACKAGE-MUST-NEVER-BE-SEEN" not in committed


# ---------------------------------------------------------------------------
# F1: candidate code executing during regen must never see live gate state
# ---------------------------------------------------------------------------

_PROBE_MAIN_PY = """
import os

marker = os.environ.get("PROBE_MARKER_PATH")
if marker:
    with open(marker, "w", encoding="utf-8") as fh:
        for key in (
            "OMNIAGENTOS_GATE_WORKSPACE",
            "OMNIAGENTOS_DB",
            "OMNIAGENTOS_VAR_DIR",
            "OMNIAGENTOS_LEDGER_DIR",
        ):
            fh.write(f"{key}={os.environ.get(key, '<absent>')}\\n")
raise RuntimeError("F1 probe: recorded ambient env, refusing to define an app")
"""


def test_regen_subprocess_never_sees_live_gate_env_even_when_ambient(tmp_path: Path) -> None:
    """F1 red-first: the CANDIDATE's own main.py — untrusted merged-tree
    content — executes during regen. It must observe scratch/absent state,
    never the live gate workspace or its DB/VAR_DIR/LEDGER paths, even when
    the CALLING process (simulating merge-gate.sh's own ambient env, e.g. a
    launch-env-sourced shell) has all four set to live-looking values.
    """
    tree = _build_tree(tmp_path)
    _seed_committed_contract(tree)
    (tree / "omniagentos" / "api" / "main.py").write_text(_PROBE_MAIN_PY, encoding="utf-8")

    marker = tmp_path / "probe-marker.txt"
    live_workspace = tmp_path / "would-be-live-gate-workspace"
    live_workspace.mkdir()
    live_db = tmp_path / "would-be-live-state.sqlite3"
    live_var = tmp_path / "would-be-live-var"
    live_ledger = tmp_path / "would-be-live-ledger"

    env_keys = (
        "OMNIAGENTOS_GATE_WORKSPACE",
        "OMNIAGENTOS_DB",
        "OMNIAGENTOS_VAR_DIR",
        "OMNIAGENTOS_LEDGER_DIR",
        "PROBE_MARKER_PATH",
    )
    previous = {key: os.environ.get(key) for key in env_keys}
    os.environ["OMNIAGENTOS_GATE_WORKSPACE"] = str(live_workspace)
    os.environ["OMNIAGENTOS_DB"] = str(live_db)
    os.environ["OMNIAGENTOS_VAR_DIR"] = str(live_var)
    os.environ["OMNIAGENTOS_LEDGER_DIR"] = str(live_ledger)
    os.environ["PROBE_MARKER_PATH"] = str(marker)
    try:
        verdict = verify_schema_neutral(tree, python=_python())
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    # The probe's own RuntimeError fails the regen — fail-closed either way,
    # but the marker file is what proves WHAT the candidate code saw.
    assert verdict.ok is False, verdict
    assert marker.is_file(), "candidate code never ran — cannot prove isolation"
    observed = marker.read_text(encoding="utf-8")
    assert "OMNIAGENTOS_GATE_WORKSPACE=<absent>" in observed, observed
    assert str(live_db) not in observed, observed
    assert str(live_var) not in observed, observed
    assert str(live_ledger) not in observed, observed
    # And the scratch trio it DID see landed under the tree's own root.
    assert str(tree / "var" / "openapi-drift-check") in observed, observed


# ---------------------------------------------------------------------------
# F3: the artifact travels through a parent-allocated file, never child stdout
# ---------------------------------------------------------------------------


def test_stray_stdout_during_import_does_not_corrupt_the_artifact(tmp_path: Path) -> None:
    """A print() at import time (e.g. a stray deprecation notice) must not
    leak into the compared bytes now that the artifact travels through a
    file, not the child's stdout.
    """
    tree = _build_tree(tmp_path)
    _seed_committed_contract(tree)
    committed = (tree / "contracts" / "openapi.json").read_bytes()

    _write_main_py(tree, extra_body="print('a stray deprecation notice')\n")

    verdict = verify_schema_neutral(tree, python=_python())
    assert verdict == DriftVerdict(
        ok=True, identical=True, detail="identical to committed contract"
    ), verdict
    assert (tree / "contracts" / "openapi.json").read_bytes() == committed


# ---------------------------------------------------------------------------
# F7: a missing dependency is an environment problem, not drift
# ---------------------------------------------------------------------------


def test_missing_dependency_reports_environment_problem_not_drift(tmp_path: Path) -> None:
    """A regen that fails because the INTERPRETER lacks a dependency must say
    so distinctly — a reviewer reading the refusal should not go hunting for
    an API change that was never there.
    """
    tree = _build_tree(tmp_path)
    _seed_committed_contract(tree)
    _write_main_py(
        tree,
        extra_body="import this_module_does_not_exist_anywhere_at_all  # noqa: F401\n",
    )

    verdict = verify_schema_neutral(tree, python=_python())
    assert verdict.ok is False
    assert verdict.identical is False
    assert "openapi-regen environment problem" in verdict.detail, verdict


# ---------------------------------------------------------------------------
# CLI wrapper: exit codes merge-gate.sh depends on
# ---------------------------------------------------------------------------


def test_cli_exits_zero_and_prints_schema_neutral(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tree = _build_tree(tmp_path)
    _seed_committed_contract(tree)

    rc = main([str(tree), "--python", str(_python())])
    out = capsys.readouterr()
    assert rc == 0
    assert "SCHEMA-NEUTRAL" in out.out


def test_cli_exits_one_and_prints_drift_to_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tree = _build_tree(tmp_path)
    _seed_committed_contract(tree)
    _write_main_py(
        tree,
        extra_body=(
            '@app.get("/new-route")\n'
            "def new_route() -> dict[str, str]:\n"
            '    return {"status": "new"}\n'
        ),
    )

    rc = main([str(tree), "--python", str(_python())])
    out = capsys.readouterr()
    assert rc == 1
    assert "DRIFT" in out.err


def test_cli_exits_two_for_a_missing_tree(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main([str(tmp_path / "does-not-exist")])
    out = capsys.readouterr()
    assert rc == 2
    assert "UNVERIFIED" in out.err


def test_cli_exits_two_and_never_claims_verified_when_unverifiable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tree = _build_tree(tmp_path)
    # No committed contract seeded — verification cannot complete.
    rc = main([str(tree), "--python", str(_python())])
    out = capsys.readouterr()
    assert rc == 2
    assert "UNVERIFIED" in out.err
    assert "SCHEMA-NEUTRAL" not in out.out
