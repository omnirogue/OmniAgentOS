"""End-to-end test of merge-gate.sh's ``openapi-drift`` step (FIX 2, 2026-08-04).

``tests/scripts/test_openapi_drift_check.py`` already pins the PYTHON regen
logic (``scripts/openapi_drift_check.py``) against tiny, real FastAPI trees.
This file is the other half: proving the BASH ORCHESTRATION around it —
materialize a throwaway merge worktree, invoke the checker, interpret its
exit code, refuse or pass — is wired correctly in the real
``scripts/merge-gate.sh``. The checker itself is stubbed here (its own
correctness is not this file's job), controlled per-run by
``MERGE_GATE_TEST_OPENAPI_RC``, exactly the way the existing M8 suite stubs
the counterfeit harness with ``MERGE_GATE_TEST_CF_RC``.

Three behaviours pinned:

  * API touched + contract touched (a well-behaved candidate that already
    regenerated) — the FAST PATH — passes WITHOUT ever invoking the checker.
  * API touched + contract untouched + checker verifies identical — passes,
    with a note that the regen was verified.
  * API touched + contract untouched + checker verifies different — refuses
    with the ORIGINAL path-implication message.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from pathlib import Path

import pytest

from omniagentos.scheduler.gate_evidence import GateEvidenceStore
from tests.scripts.test_merge_gate_m8_refusals import (
    MERGE_GATE,
    REAL_PYTHON,
    REPO_ROOT,
    FixtureBranch,
    M8Repo,
    _commit_file,
    _git,
    _output,
    _receipt,
    fake_python_for,
    run_contained,
)


def _install_stub_python(repo: Path) -> None:
    """Same shape as the M8 fixture's `_install_fake_python`, plus one branch.

    Intercepts `<repo>/scripts/openapi_drift_check.py <tree>` — the exact
    invocation shape merge-gate.sh uses — and answers with the exit code the
    test controls via MERGE_GATE_TEST_OPENAPI_RC, so this file exercises the
    BASH side only. Every other branch matches `_install_fake_python`
    verbatim, because the rest of the gate (trial-merge, ladder, ruff,
    reachability) still has to run for real to reach "MERGE GATE: PASS".
    """
    python = repo / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True, exist_ok=True)
    source_root = shlex.quote(str(REPO_ROOT))
    real_python = shlex.quote(str(REAL_PYTHON))
    python.write_text(
        f"""#!/bin/sh
if [ "$1" = "-m" ] && [ "$2" = "omniagentos.scheduler.gate_evidence" ]; then
  PYTHONPATH={source_root} exec {real_python} "$@"
fi
case "$1" in
  */openapi_drift_check.py)
    if [ -n "${{MERGE_GATE_TEST_ENV_MARKER_DIR:-}}" ]; then
      mkdir -p "$MERGE_GATE_TEST_ENV_MARKER_DIR"
      {{
        printf 'OMNIAGENTOS_GATE_WORKSPACE=%s\\n' "${{OMNIAGENTOS_GATE_WORKSPACE:-<absent>}}"
        printf 'OMNIAGENTOS_DB=%s\\n' "${{OMNIAGENTOS_DB:-<absent>}}"
        printf 'OMNIAGENTOS_VAR_DIR=%s\\n' "${{OMNIAGENTOS_VAR_DIR:-<absent>}}"
        printf 'OMNIAGENTOS_LEDGER_DIR=%s\\n' "${{OMNIAGENTOS_LEDGER_DIR:-<absent>}}"
      }} >"$MERGE_GATE_TEST_ENV_MARKER_DIR/openapi-drift-check.env"
    fi
    rc="${{MERGE_GATE_TEST_OPENAPI_RC:-0}}"
    case "$rc" in
      0) printf 'SCHEMA-NEUTRAL: identical to committed contract (stub)\\n' ;;
      1) printf 'DRIFT: differs from committed contract (stub)\\n' >&2 ;;
      *) printf 'UNVERIFIED: stub could not verify (stub)\\n' >&2 ;;
    esac
    exit "$rc"
    ;;
esac
if [ "$1" = "-m" ] && [ "$2" = "tests.counterfeits.harness" ]; then
  printf 'COUNTERFEIT CORPUS REPORT\\n'
  printf 'CAUGHT    cf-fixture\\n'
  printf -- '------------------------------------------------------------\\n'
  printf 'total=1  caught=1  survived=0  skipped_platform=0  other=0\\n'
  exit 0
fi
if [ "$1" = "-c" ]; then
  if [ -n "${{MERGE_GATE_TEST_ENV_MARKER_DIR:-}}" ]; then
    mkdir -p "$MERGE_GATE_TEST_ENV_MARKER_DIR"
    {{
      printf 'OMNIAGENTOS_GATE_WORKSPACE=%s\\n' "${{OMNIAGENTOS_GATE_WORKSPACE:-<absent>}}"
      printf 'OMNIAGENTOS_DB=%s\\n' "${{OMNIAGENTOS_DB:-<absent>}}"
      printf 'OMNIAGENTOS_VAR_DIR=%s\\n' "${{OMNIAGENTOS_VAR_DIR:-<absent>}}"
      printf 'OMNIAGENTOS_LEDGER_DIR=%s\\n' "${{OMNIAGENTOS_LEDGER_DIR:-<absent>}}"
    }} >"$MERGE_GATE_TEST_ENV_MARKER_DIR/tests-own-tree.env"
  fi
  printf '%s/omniagentos/__init__.py' "$PWD"
  exit 0
fi
if [ "$1" = "-m" ] && [ "$2" = "pytest" ]; then
  printf '1 passed in 0.01s\\n'
  exit 0
fi
if [ "$1" = "-m" ] && [ "$2" = "ruff" ]; then
  exit 0
fi
exec {real_python} "$@"
""",
        encoding="utf-8",
    )
    python.chmod(0o755)


def _parse_env_marker(path: Path) -> dict[str, str]:
    return dict(
        line.split("=", 1) for line in path.read_text(encoding="utf-8").splitlines() if "=" in line
    )


@pytest.fixture
def openapi_repo(tmp_path: Path) -> M8Repo:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "OpenAPI Drift Gate Test")
    _git(repo, "config", "user.email", "openapi-drift-gate@example.com")
    (repo / ".gitignore").write_text(".venv\nnode_modules\nvar\n", encoding="utf-8")
    (repo / "ARCHI.md").write_text("main architecture\n", encoding="utf-8")
    (repo / "WORKBOOK.md").write_text("shared workbook\n", encoding="utf-8")
    reachability = repo / "scripts" / "reachability-gate.py"
    reachability.parent.mkdir(parents=True, exist_ok=True)
    reachability.write_bytes((REPO_ROOT / "scripts" / "reachability-gate.py").read_bytes())
    reachability.chmod(0o755)
    # THE PINNED WORKSPACE MUST CARRY THE JUDGE (2026-08-07). These tests drive
    # MERGE_GATE_PINNED=1 with OMNIAGENTOS_GATE_WORKSPACE pointed at this
    # fixture, and the gate now refuses `unverifiable-gate-script` when it
    # cannot prove which copy of itself is running. A real gate workspace is a
    # checkout of this repository and always has this file; a fixture that
    # omits it is modelling a workspace that cannot exist. Copied byte-for-byte
    # from the script under test, so the identity check MATCHES — including
    # under the counterfeit corpus and the revert-check mutations, which read
    # the same path.
    gate_script = repo / "scripts" / "merge-gate.sh"
    gate_script.write_bytes(MERGE_GATE.read_bytes())
    gate_script.chmod(0o755)
    # Content is irrelevant below: the checker is stubbed (see
    # _install_stub_python), so this test proves the BASH wiring around it,
    # not the python regen logic (test_openapi_drift_check.py's job).
    api_file = repo / "omniagentos" / "api" / "main.py"
    api_file.parent.mkdir(parents=True, exist_ok=True)
    api_file.write_text("# placeholder api module\n", encoding="utf-8")
    contract = repo / "contracts" / "openapi.json"
    contract.parent.mkdir(parents=True, exist_ok=True)
    contract.write_text('{"openapi": "3.1.0"}\n', encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    base_sha = _git(repo, "rev-parse", "HEAD")

    _install_stub_python(repo)

    candidates = {
        "api_only": (
            "fixture/api-only-edit",
            _commit_file(repo, "fixture/api-only-edit", "omniagentos/api/main.py", "# edited\n"),
            "openapi-drift",
            "without regenerating contracts/openapi.json",
        ),
        "api_and_contract": (
            "fixture/api-and-contract",
            None,  # filled below: two files in one commit
            None,
            None,
        ),
    }

    # api_and_contract needs two files touched in the SAME commit — build it
    # by hand instead of `_commit_file` (which writes exactly one path).
    _git(repo, "checkout", "-b", "fixture/api-and-contract", "main")
    (repo / "omniagentos" / "api" / "main.py").write_text(
        "# edited with contract\n", encoding="utf-8"
    )
    (repo / "contracts" / "openapi.json").write_text(
        '{"openapi": "3.1.0", "changed": true}\n', encoding="utf-8"
    )
    _git(repo, "add", "omniagentos/api/main.py", "contracts/openapi.json")
    _git(repo, "commit", "-m", "fixture: api-and-contract")
    api_and_contract_sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "main")

    branches = {
        "api_only": FixtureBranch(
            name=candidates["api_only"][0],
            candidate_sha=candidates["api_only"][1],
            merge_base_sha=base_sha,
            refusal=candidates["api_only"][2],
            reason=candidates["api_only"][3],
        ),
        "api_and_contract": FixtureBranch(
            name="fixture/api-and-contract",
            candidate_sha=api_and_contract_sha,
            merge_base_sha=base_sha,
            refusal=None,
            reason=None,
        ),
    }

    evidence_root = tmp_path / "gate-evidence"
    store = GateEvidenceStore(evidence_root)
    for case in branches.values():
        signed = store.sign(_receipt(case, repo))
        receipt_path = evidence_root / "records" / "merge-gate" / f"{case.candidate_sha}.json"
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_text(
            json.dumps(signed.to_payload(), sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )

    return M8Repo(path=repo, evidence_root=evidence_root, branches=branches)


def _run_gate(
    fixture: M8Repo, case: FixtureBranch, *, env_extra: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    # openapi-drift lives in the HOISTED block, which only runs under
    # MERGE_GATE_PINNED=1 (the un-armed default path never reaches it — that
    # is pre-existing gating this fix does not change, see merge-gate.sh's
    # own PINNED-mode header). Arming it needs GATE_WS to resolve to the SAME
    # clean checkout as REPO, so OMNIAGENTOS_GATE_WORKSPACE is pinned at this
    # fixture's own repo.
    env = {
        **os.environ,
        "REPO": str(fixture.path),
        # STATE THE INTERPRETER, never inherit it — see fake_python_for().
        "MERGE_GATE_PY": fake_python_for(fixture.path),
        "MERGE_GATE_PINNED": "1",
        "OMNIAGENTOS_GATE_WORKSPACE": str(fixture.path),
        "MERGE_GATE_EVIDENCE_ROOT": str(fixture.evidence_root),
        "MERGE_GATE_STEP_RECEIPTS": "0",
        **env_extra,
    }
    # CONTAINED: a gate spawns pytest which spawns more gates; on timeout
    # subprocess.run kills only the direct child and the rest orphan to PPID 1.
    return run_contained(
        ["bash", str(MERGE_GATE), case.name],
        cwd=fixture.path,
        env=env,
    )


def test_fast_path_passes_without_invoking_the_checker(openapi_repo: M8Repo) -> None:
    """API + contract touched together: no throwaway worktree, no checker call.

    MERGE_GATE_TEST_OPENAPI_RC is set to a REFUSE value on purpose — if the
    fast path ever regressed into calling the checker, this candidate would
    wrongly refuse, proving the fast path is genuinely independent of it.
    """
    case = openapi_repo.branches["api_and_contract"]
    result = _run_gate(openapi_repo, case, env_extra={"MERGE_GATE_TEST_OPENAPI_RC": "1"})
    print(f"FAST-PATH rc={result.returncode}\n{_output(result)}")
    assert result.returncode == 0, _output(result)
    assert "MERGE GATE: PASS" in _output(result)


def test_schema_neutral_regen_verified_identical_passes(openapi_repo: M8Repo) -> None:
    case = openapi_repo.branches["api_only"]
    result = _run_gate(openapi_repo, case, env_extra={"MERGE_GATE_TEST_OPENAPI_RC": "0"})
    print(f"NEUTRAL rc={result.returncode}\n{_output(result)}")
    assert result.returncode == 0, _output(result)
    assert "MERGE GATE: PASS" in _output(result)
    assert re.search(r"reachability\s+ok", _output(result)), _output(result)


def test_verified_drift_refuses_with_the_original_message(openapi_repo: M8Repo) -> None:
    case = openapi_repo.branches["api_only"]
    result = _run_gate(openapi_repo, case, env_extra={"MERGE_GATE_TEST_OPENAPI_RC": "1"})
    print(f"DRIFT rc={result.returncode}\n{_output(result)}")
    assert result.returncode != 0, _output(result)
    assert "MERGE GATE: PASS" not in _output(result)
    assert case.refusal in _output(result)
    assert case.reason in _output(result)


def test_unverifiable_regen_fails_closed_and_refuses(openapi_repo: M8Repo) -> None:
    """rc=2 from the checker ('could not verify') must refuse, not pass."""
    case = openapi_repo.branches["api_only"]
    result = _run_gate(openapi_repo, case, env_extra={"MERGE_GATE_TEST_OPENAPI_RC": "2"})
    print(f"UNVERIFIABLE rc={result.returncode}\n{_output(result)}")
    assert result.returncode != 0, _output(result)
    assert "MERGE GATE: PASS" not in _output(result)
    assert case.refusal in _output(result)
    assert case.reason in _output(result)


def test_checker_and_probe_invocations_never_see_the_live_gate_env(
    openapi_repo: M8Repo, tmp_path: Path
) -> None:
    """F1 (bash layer) + F6 red-first: neither the openapi-drift checker
    invocation nor the pre-existing tests-own-tree import probe may observe
    the parent gate process's live OMNIAGENTOS_GATE_WORKSPACE (which
    `_run_gate` pins to arm PINNED mode, exactly as a real launch-env shell
    would export it) or its DB/VAR_DIR/LEDGER_DIR.
    """
    case = openapi_repo.branches["api_only"]
    marker_dir = tmp_path / "env-markers"
    live_db = tmp_path / "would-be-live-state.sqlite3"
    live_var = tmp_path / "would-be-live-var"
    live_ledger = tmp_path / "would-be-live-ledger"

    result = _run_gate(
        openapi_repo,
        case,
        env_extra={
            "MERGE_GATE_TEST_OPENAPI_RC": "0",
            "MERGE_GATE_TEST_ENV_MARKER_DIR": str(marker_dir),
            "OMNIAGENTOS_DB": str(live_db),
            "OMNIAGENTOS_VAR_DIR": str(live_var),
            "OMNIAGENTOS_LEDGER_DIR": str(live_ledger),
        },
    )
    print(f"ENV-PROBE rc={result.returncode}\n{_output(result)}")
    assert result.returncode == 0, _output(result)
    assert "MERGE GATE: PASS" in _output(result)

    for name in ("openapi-drift-check.env", "tests-own-tree.env"):
        marker = marker_dir / name
        assert marker.is_file(), f"{name} probe never ran"
        observed = _parse_env_marker(marker)
        assert observed["OMNIAGENTOS_GATE_WORKSPACE"] == "<absent>", (name, observed)
        assert observed["OMNIAGENTOS_DB"] != str(live_db), (name, observed)
        assert observed["OMNIAGENTOS_VAR_DIR"] != str(live_var), (name, observed)
        assert observed["OMNIAGENTOS_LEDGER_DIR"] != str(live_ledger), (name, observed)


def test_worktree_add_failure_surfaces_gits_reason_and_refuses(openapi_repo: M8Repo) -> None:
    """F4: an unwritable var/swarm must refuse WITH git's own reason attached,
    never a bare '2>/dev/null'-discarded refusal, and must never reach the
    checker at all.
    """
    case = openapi_repo.branches["api_only"]
    swarm_dir = openapi_repo.path / "var" / "swarm"
    swarm_dir.mkdir(parents=True, exist_ok=True)
    swarm_dir.chmod(0o555)
    try:
        result = _run_gate(openapi_repo, case, env_extra={"MERGE_GATE_TEST_OPENAPI_RC": "0"})
    finally:
        swarm_dir.chmod(0o755)

    print(f"ADD-FAILURE rc={result.returncode}\n{_output(result)}")
    assert result.returncode != 0, _output(result)
    assert "MERGE GATE: PASS" not in _output(result)
    assert case.refusal in _output(result)
    assert "no worktree to stage the merged tree" in _output(result)
    # git's OWN stderr reason made it into the refusal — not silently
    # discarded — the specific wording is git's, so match loosely.
    assert re.search(r"denied|not permitted|cannot|fatal", _output(result), re.IGNORECASE), _output(
        result
    )


def test_merge_conflict_while_staging_fails_closed_and_refuses(openapi_repo: M8Repo) -> None:
    """F7: if the throwaway merge-to-verify itself conflicts, verification
    cannot complete — refuse with the original message, never invoke the
    checker at all.
    """
    case = openapi_repo.branches["api_only"]
    # Advance main with a CONFLICTING edit to the same path the candidate
    # touched, committed AFTER the candidate already branched off base_sha.
    (openapi_repo.path / "omniagentos" / "api" / "main.py").write_text(
        "# a DIFFERENT edit landed on main after the candidate branched\n",
        encoding="utf-8",
    )
    _git(openapi_repo.path, "add", "omniagentos/api/main.py")
    _git(openapi_repo.path, "commit", "-m", "advance main with a conflicting edit")

    result = _run_gate(openapi_repo, case, env_extra={"MERGE_GATE_TEST_OPENAPI_RC": "0"})
    print(f"MERGE-CONFLICT rc={result.returncode}\n{_output(result)}")
    assert result.returncode != 0, _output(result)
    assert "MERGE GATE: PASS" not in _output(result)
    assert case.refusal in _output(result)
    assert "merge conflict while staging the tree to check" in _output(result)


def test_openapi_worktree_is_cleaned_up_after_a_refusal(openapi_repo: M8Repo) -> None:
    """F7: the on_exit trap must remove $OPENAPI_TREE even when refuse() exits
    mid-verification (reviewer-verified correct behaviour; this pins it).
    """
    case = openapi_repo.branches["api_only"]
    result = _run_gate(openapi_repo, case, env_extra={"MERGE_GATE_TEST_OPENAPI_RC": "1"})
    assert result.returncode != 0, _output(result)
    swarm_dir = openapi_repo.path / "var" / "swarm"
    leftovers = list(swarm_dir.glob("gate-openapi-*")) if swarm_dir.is_dir() else []
    assert leftovers == [], f"leftover openapi-drift worktree(s): {leftovers}"
