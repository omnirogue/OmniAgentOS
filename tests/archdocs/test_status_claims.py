from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from omniagentos.archdocs.generate import (
    launcher_default_ports,
    render_archi,
    scan_evidence,
    update_archi_facts,
    update_status_file,
)


def _git_repo(root: Path) -> str:
    (root / ".gitignore").write_text("evidence.json\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    (root / "seed").write_text("immutable\n", encoding="utf-8")
    subprocess.run(["git", "add", "--all"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=root, check=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _manifest(sha: str, **claim_statuses: bool) -> dict[str, object]:
    evidence_paths = {
        "Decision Center": "tests/decision_center",
        "TaskContract": "tests/taskcontract",
        "Toolplane": "tests/toolplane",
        "fan-in": "tests/fanin",
        "isolation": "tests/scope/test_isolation_drill.py",
        "P12": "tests/p12",
    }
    claims = {
        claim: {
            "status": "passed" if passed else "not_certified",
            "evidence": [evidence_paths[claim]] if passed else [],
        }
        for claim, passed in claim_statuses.items()
    }
    executed_paths = [evidence_paths[claim] for claim, passed in claim_statuses.items() if passed]
    expected_counts = {path: 1 for path in executed_paths}
    return {
        "schema": "omniagentos.certification-evidence.v1",
        "source": "scripts/certify-omniagentos.sh",
        "repository_sha": sha,
        "clean_tree": True,
        "pytest_exit_code": 0,
        "certified": True,
        "suite_complete": True,
        "expected_test_counts": expected_counts,
        "actual_test_counts": dict(expected_counts),
        "inventory_errors": [],
        "test_counts": {
            "tests": len(executed_paths),
            "passed": len(executed_paths),
            "skipped": 0,
            "failed": 0,
        },
        "executed_paths": executed_paths,
        "claims": claims,
    }


def _write_toolplane_test(root: Path) -> None:
    test_dir = root / "tests" / "toolplane"
    test_dir.mkdir(parents=True, exist_ok=True)
    (test_dir / "test_contract.py").write_text(
        "def test_contract():\n    assert True\n",
        encoding="utf-8",
    )


def test_directory_presence_is_not_certification_evidence(tmp_path: Path) -> None:
    _git_repo(tmp_path)
    (tmp_path / "tests" / "toolplane").mkdir(parents=True)
    (tmp_path / "tests" / "p12").mkdir(parents=True)

    evidence = scan_evidence(tmp_path)

    assert evidence["Toolplane"] is False
    assert evidence["P12"] is False


def test_manifest_must_match_full_current_sha(tmp_path: Path) -> None:
    _write_toolplane_test(tmp_path)
    sha = _git_repo(tmp_path)
    evidence_path = tmp_path / "evidence.json"
    evidence_path.write_text(
        json.dumps(_manifest("0" * 40, Toolplane=True)),
        encoding="utf-8",
    )

    assert scan_evidence(tmp_path, evidence_path)["Toolplane"] is False

    evidence_path.write_text(json.dumps(_manifest(sha, Toolplane=True)), encoding="utf-8")
    assert scan_evidence(tmp_path, evidence_path)["Toolplane"] is True

    (tmp_path / "untracked-source.py").write_text("dirty = True\n", encoding="utf-8")
    assert scan_evidence(tmp_path, evidence_path)["Toolplane"] is False


def test_mixed_skipped_manifest_cannot_certify(tmp_path: Path) -> None:
    _write_toolplane_test(tmp_path)
    sha = _git_repo(tmp_path)
    evidence_path = tmp_path / "evidence.json"
    manifest = _manifest(sha, Toolplane=True)
    manifest["expected_test_counts"] = {"tests/toolplane": 2}
    manifest["actual_test_counts"] = {"tests/toolplane": 2}
    manifest["test_counts"] = {"tests": 2, "passed": 1, "skipped": 1, "failed": 0}
    evidence_path.write_text(json.dumps(manifest), encoding="utf-8")

    evidence = scan_evidence(tmp_path, evidence_path)

    assert evidence["Certification"] is False
    assert evidence["Toolplane"] is False


def test_claim_evidence_must_be_claim_specific_and_actually_executed(tmp_path: Path) -> None:
    _write_toolplane_test(tmp_path)
    unrelated = tmp_path / "tests" / "unrelated"
    unrelated.mkdir(parents=True)
    (unrelated / "test_unrelated.py").write_text("def test_ok(): pass\n", encoding="utf-8")
    sha = _git_repo(tmp_path)
    evidence_path = tmp_path / "evidence.json"
    manifest = _manifest(sha, Toolplane=True)
    claims = manifest["claims"]
    assert isinstance(claims, dict)
    claims["Toolplane"]["evidence"] = ["tests/unrelated"]
    manifest["executed_paths"] = ["tests/unrelated"]
    evidence_path.write_text(json.dumps(manifest), encoding="utf-8")

    assert scan_evidence(tmp_path, evidence_path)["Toolplane"] is False


def test_status_requires_every_claim_named_on_a_row(tmp_path: Path) -> None:
    _write_toolplane_test(tmp_path)
    status_path = tmp_path / "STATUS.md"
    status_path.write_text(
        "| Toolplane | **done** |\n"
        "| Fan-in + TaskContract + revise | **done** |\n"
        "| Scope shadow default + isolation drill | **done** |\n",
        encoding="utf-8",
    )
    sha = _git_repo(tmp_path)
    evidence_path = tmp_path / "evidence.json"
    evidence_path.write_text(
        json.dumps(
            _manifest(
                sha,
                Toolplane=True,
                **{"fan-in": True, "TaskContract": False, "isolation": False},
            )
        ),
        encoding="utf-8",
    )

    new_content = update_status_file(
        tmp_path,
        status_path=status_path,
        evidence_path=evidence_path,
    )

    assert "| Toolplane | **done** |" in new_content
    assert "| Fan-in + TaskContract + revise | **pending certified evidence** |" in new_content
    assert (
        "| Scope shadow default + isolation drill | **pending certified evidence** |" in new_content
    )


def test_check_plus_update_status_never_writes(tmp_path: Path) -> None:
    _git_repo(tmp_path)
    archi_path = tmp_path / "ARCHI.md"
    status_path = tmp_path / "STATUS.md"
    archi_path.write_text("# Human-only architecture notes\n", encoding="utf-8")
    status_path.write_text("| Toolplane | **done** |\n", encoding="utf-8")
    archi_before = archi_path.read_bytes()
    status_before = status_path.read_bytes()

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "omniagentos.archdocs.generate",
            "--repo-root",
            str(tmp_path),
            "--archi-path",
            str(archi_path),
            "--status-path",
            str(status_path),
            "--check",
            "--update-status",
        ],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert archi_path.read_bytes() == archi_before
    assert status_path.read_bytes() == status_before


def test_status_ports_and_migration_ceiling_come_from_repository(tmp_path: Path) -> None:
    launcher = tmp_path / "scripts" / "launch-omniagentos.sh"
    launcher.parent.mkdir(parents=True)
    launcher.write_text(
        'export OMNIAGENTOS_API_PORT="${OMNIAGENTOS_API_PORT:-18120}"\n'
        'export OMNIAGENTOS_DASH_PORT="${OMNIAGENTOS_DASH_PORT:-19120}"\n',
        encoding="utf-8",
    )
    migration = tmp_path / "omniagentos" / "db" / "migrations" / "071_truth.sql"
    migration.parent.mkdir(parents=True)
    migration.write_text("-- evidence\n", encoding="utf-8")
    status_path = tmp_path / "STATUS.md"
    status_path.write_text(
        "| Launch on :8485 / :3001 | **done** |\n"
        "| Migration claim 060–069 Grok-exclusive | **done** |\n",
        encoding="utf-8",
    )
    _git_repo(tmp_path)

    content = update_status_file(tmp_path, status_path=status_path)

    assert "Launch on :18120 / :19120" in content
    assert "Migration claim 060–071 Grok-exclusive" in content

    archi_path = tmp_path / "ARCHI.md"
    archi_path.write_text(
        "- API: `omniagentos.api:app` (FastAPI), bound `127.0.0.1:8484`.\n"
        "- Dashboard: Next.js app under `dashboard/`, `npm run dev`/`npm run start`, "
        "port 3000.\n"
        "- API: `127.0.0.1:8484` (loopback only).\n"
        "- Dashboard: `127.0.0.1:3000`, same-origin proxy.\n",
        encoding="utf-8",
    )
    archi = render_archi(
        tmp_path,
        archi_path=archi_path,
        launchd_dir=tmp_path / "LaunchAgents",
    )
    assert archi.count("127.0.0.1:18120") == 2
    assert archi.count("19120") == 2


def test_update_status_absent_migrations_not_favourable(tmp_path: Path) -> None:
    """Absent migrations source must not claim version 0 or leave **done**.

    Defect class: non-result presented as favourable. When
    ``omniagentos/db/migrations`` is missing:

    - Must not rewrite the ceiling to ``060–000`` (unknown-as-zero).
    - Must not leave an existing ``**done**`` status on the migration row.

    Counterfeit A: ``if True:`` / drop the ``migs is not None`` guard so absent
    scans rewrite to version 0 while keeping **done**.
    Counterfeit B: skip the ceiling rewrite but leave ``**done**`` intact.
    """
    # No omniagentos/db/migrations directory at all.
    status_path = tmp_path / "STATUS.md"
    status_path.write_text(
        "| Migration claim 060–069 Grok-exclusive | **done** |\n",
        encoding="utf-8",
    )
    _git_repo(tmp_path)

    content = update_status_file(tmp_path, status_path=status_path)

    # Unknown inventory must not be rewritten to a measured-zero ceiling.
    assert "060–000" not in content
    assert "Migration claim 060–000" not in content
    # Favourable status must not survive when the source is unmeasurable.
    assert "**done**" not in content
    assert "source unavailable" in content.lower()
    # Ceiling range text is left alone (still 060–069) when unmeasured.
    assert "Migration claim 060–069 Grok-exclusive" in content


def test_update_status_absent_launcher_does_not_rewrite_ports(tmp_path: Path) -> None:
    """Missing launcher must not rewrite Launch-on claim to baked defaults.

    Defect class: non-result presented as favourable concrete facts.
    The JSON path already proves ``ports: null`` when the launcher is absent;
    this binds the STATUS updater itself.

    Counterfeit: restore
    ``if port_pair is None: port_pair = (_PORT_DEFAULT_API, _PORT_DEFAULT_DASH)``
    so unmeasurable launcher rewrites a non-default Launch-on claim to 8485/3003.

    Binding requirement: existing ``:9999 / :8888`` must survive; baked
    ``:8485 / :3003`` must not appear. Using non-default ports is required —
    if the claim already said 8485/3003, the counterfeit rewrite would be invisible.
    """
    # No scripts/launch-omniagentos.sh
    assert not (tmp_path / "scripts" / "launch-omniagentos.sh").exists()
    assert launcher_default_ports(tmp_path) is None

    status_path = tmp_path / "STATUS.md"
    status_path.write_text(
        "| Launch on :9999 / :8888 | **done** |\n",
        encoding="utf-8",
    )
    _git_repo(tmp_path)

    content = update_status_file(tmp_path, status_path=status_path)

    assert "Launch on :9999 / :8888" in content, (
        "absent launcher must leave existing Launch-on claim untouched"
    )
    assert "Launch on :8485 / :3003" not in content, (
        "absent launcher must not rewrite Launch-on to baked _PORT_DEFAULT_* ports"
    )
    assert ":8485" not in content
    assert ":3003" not in content


def test_update_archi_facts_absent_launcher_leaves_ports_untouched(tmp_path: Path) -> None:
    """Missing launcher must leave existing ARCHI port facts untouched.

    Defect class: non-result presented as favourable concrete facts.
    Counterfeit: restore
    ``if port_pair is None: port_pair = (_PORT_DEFAULT_API, _PORT_DEFAULT_DASH)``
    in ``update_archi_facts`` so unmeasurable launcher rewrites port prose to
    baked 8485/3003.

    Binding: non-default ports (9999/8888) must survive byte-stable; baked
    defaults must not appear. Direct call (not only via render_archi) so the
    production helper itself is proved.
    """
    assert not (tmp_path / "scripts" / "launch-omniagentos.sh").exists()
    assert launcher_default_ports(tmp_path) is None

    content = (
        "- API: `omniagentos.api:app` (FastAPI), bound `127.0.0.1:9999`.\n"
        "- Dashboard: Next.js app under `dashboard/`, `npm run dev`/`npm run start`, "
        "port 8888.\n"
        "- API: `127.0.0.1:9999` (loopback only).\n"
        "- Dashboard: `127.0.0.1:8888`, same-origin proxy.\n"
    )
    out = update_archi_facts(tmp_path, content)

    assert out == content, "absent launcher must leave archi port facts byte-untouched"
    assert "8485" not in out
    assert "3003" not in out
    assert "9999" in out
    assert "8888" in out


def test_launcher_unreadable_not_default_ports(tmp_path: Path) -> None:
    """Unreadable/undecodable launcher must return None, not baked defaults.

    Defect class: non-result presented as favourable concrete facts.
    ``launcher_default_ports`` documents unreadable input as ``None``. Missing
    and present-empty launchers take other branches; this binds the
    ``except (OSError, UnicodeDecodeError)`` path specifically.

    Counterfeit: restore
    ``except (OSError, UnicodeDecodeError): return _PORT_DEFAULT_API, _PORT_DEFAULT_DASH``
    so unreadable input invents 8485/3003. That mutation must fail this test
    while missing/empty-launcher tests still pass.
    """
    launcher = tmp_path / "scripts" / "launch-omniagentos.sh"
    launcher.parent.mkdir(parents=True)
    # Invalid UTF-8 hits UnicodeDecodeError on read_text(encoding="utf-8").
    # (chmod-000 is platform-dependent for the owner process; binary garbage
    # is the portable OSError/UnicodeDecodeError binder.)
    launcher.write_bytes(b"\xff\xfe not valid utf-8 OMNIAGENTOS_API_PORT=8485")

    assert launcher.exists()
    ports = launcher_default_ports(tmp_path)
    assert ports is None, (
        f"unreadable/undecodable launcher must return None, not baked defaults (got {ports!r})"
    )
    assert ports != ("8485", "3003")

    # Also OSError path: present file that cannot be read.
    import os
    import stat

    launcher.write_text(
        'OMNIAGENTOS_API_PORT="${OMNIAGENTOS_API_PORT:-8485}"\nOMNIAGENTOS_DASH_PORT="${OMNIAGENTOS_DASH_PORT:-3001}"\n',
        encoding="utf-8",
    )
    os.chmod(launcher, 0)
    try:
        ports_os = launcher_default_ports(tmp_path)
        assert ports_os is None, (
            f"OSError-unreadable launcher must return None, not baked defaults (got {ports_os!r})"
        )
        assert ports_os != ("8485", "3001")
    finally:
        os.chmod(launcher, stat.S_IRWXU)


def test_launcher_present_unparseable_ports_not_defaults(tmp_path: Path) -> None:
    """Present launcher with no parseable port decls must not invent defaults.

    Defect class: non-result presented as favourable concrete facts. A present
    empty (or otherwise unparseable) launcher previously returned
    ``('8485', '3003')`` and JSON ports ``{api: 8485, dashboard: 3003}`` —
    unknown facts rendered as measured defaults.

    Counterfeit: initialize ``api_port, dash_port = _PORT_DEFAULT_API,
    _PORT_DEFAULT_DASH`` and return them when no ``_PORT_RE`` match is found.
    """
    from omniagentos.archdocs.generate import emit_archi_json

    launcher = tmp_path / "scripts" / "launch-omniagentos.sh"
    launcher.parent.mkdir(parents=True)
    # Present but empty — no GROK_*_PORT default declarations.
    launcher.write_text("", encoding="utf-8")

    assert launcher.exists()
    ports = launcher_default_ports(tmp_path)
    assert ports is None, (
        f"empty/unparseable launcher must return None, not baked defaults (got {ports!r})"
    )
    assert ports != ("8485", "3003")

    (tmp_path / "omniagentos" / "api" / "routes").mkdir(parents=True)
    (tmp_path / "omniagentos" / "db" / "migrations").mkdir(parents=True)
    launchd = tmp_path / "launchd-empty"
    launchd.mkdir()
    data = emit_archi_json(tmp_path, launchd_dir=launchd)
    assert data["ports"] is None, (
        "unparseable launcher must publish ports:null, not concrete 8485/3003"
    )
    assert data["ports"] != {"api": 8485, "dashboard": 3003}

    # STATUS / ARCHI updaters must not rewrite claims to baked defaults either.
    status_path = tmp_path / "STATUS.md"
    status_path.write_text("| Launch on :9999 / :8888 | **done** |\n", encoding="utf-8")
    _git_repo(tmp_path)
    status_out = update_status_file(tmp_path, status_path=status_path)
    assert "Launch on :9999 / :8888" in status_out
    assert "Launch on :8485 / :3003" not in status_out

    archi_content = (
        "- API: `omniagentos.api:app` (FastAPI), bound `127.0.0.1:9999`.\n"
        "- Dashboard: Next.js app under `dashboard/`, `npm run dev`/`npm run start`, "
        "port 8888.\n"
    )
    archi_out = update_archi_facts(tmp_path, archi_content)
    assert archi_out == archi_content
    assert "8485" not in archi_out
    assert "3003" not in archi_out


def test_launcher_partial_ports_not_accepted(tmp_path: Path) -> None:
    """Either missing port declaration must fail closed — not a partial pair.

    Defect class: non-result presented as a favourable measured result. A
    launcher that declares only ``OMNIAGENTOS_API_PORT`` (or only ``OMNIAGENTOS_DASH_PORT``)
    cannot be treated as a complete measured port pair.

    The fixture that covers *neither* declaration is independent; this binds
    the *partial* case specifically.

    Counterfeit (must fail this test): change the fail-close from
    ``if api_port is None or dash_port is None`` to
    ``if api_port is None and dash_port is None`` so an API-only or
    dashboard-only launcher returns a half-measured pair instead of None.
    """
    from omniagentos.archdocs.generate import emit_archi_json

    launcher = tmp_path / "scripts" / "launch-omniagentos.sh"
    launcher.parent.mkdir(parents=True)

    (tmp_path / "omniagentos" / "api" / "routes").mkdir(parents=True)
    (tmp_path / "omniagentos" / "db" / "migrations").mkdir(parents=True)
    launchd = tmp_path / "launchd-empty"
    launchd.mkdir()

    # API-only: one measured, one unknown → must not accept.
    launcher.write_text(
        'OMNIAGENTOS_API_PORT="${OMNIAGENTOS_API_PORT:-8485}"\n',
        encoding="utf-8",
    )
    ports_api_only = launcher_default_ports(tmp_path)
    assert ports_api_only is None, (
        f"API-only launcher must return None, not a partial pair (got {ports_api_only!r})"
    )
    assert ports_api_only != ("8485", None)
    data_api = emit_archi_json(tmp_path, launchd_dir=launchd)
    assert data_api["ports"] is None, (
        "API-only launcher must publish ports:null, not a partial concrete object"
    )

    # Dashboard-only: the other half of the requirement.
    launcher.write_text(
        'OMNIAGENTOS_DASH_PORT="${OMNIAGENTOS_DASH_PORT:-3001}"\n',
        encoding="utf-8",
    )
    ports_dash_only = launcher_default_ports(tmp_path)
    assert ports_dash_only is None, (
        f"dashboard-only launcher must return None, not a partial pair (got {ports_dash_only!r})"
    )
    assert ports_dash_only != (None, "3001")
    data_dash = emit_archi_json(tmp_path, launchd_dir=launchd)
    assert data_dash["ports"] is None, (
        "dashboard-only launcher must publish ports:null, not a partial concrete object"
    )

    # Control: both declared → measured pair is accepted.
    launcher.write_text(
        'OMNIAGENTOS_API_PORT="${OMNIAGENTOS_API_PORT:-8485}"\nOMNIAGENTOS_DASH_PORT="${OMNIAGENTOS_DASH_PORT:-3001}"\n',
        encoding="utf-8",
    )
    ports_both = launcher_default_ports(tmp_path)
    assert ports_both == ("8485", "3001")
    data_both = emit_archi_json(tmp_path, launchd_dir=launchd)
    assert data_both["ports"] == {"api": 8485, "dashboard": 3001}
