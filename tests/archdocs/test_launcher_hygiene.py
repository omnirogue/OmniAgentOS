from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from omniagentos.api.main import allowed_cors_origins
from omniagentos.archdocs.generate import launcher_default_ports
from omniagentos.archdocs.staleness import (
    compute_stamp,
    is_stale,
    parse_stamp_comment,
    stamp_archi,
    stamp_archi_and_json,
)

ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = ROOT / "scripts" / "launch-supervised.sh"


def _current_and_next_migration() -> tuple[int, int]:
    versions = sorted(
        int(path.name.split("_", 1)[0])
        for path in (ROOT / "omniagentos" / "db" / "migrations").glob("*.sql")
        if path.name.split("_", 1)[0].isdigit()
    )
    assert versions, "at least one numbered migration must exist"
    return versions[-1], versions[-1] + 1


def _python_constant(path: Path, name: str) -> object:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if any(isinstance(target, ast.Name) and target.id == name for target in targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} is not a module-level literal in {path}")


def _dashboard_function(source: str) -> str:
    match = re.search(
        r"(?ms)^_dashboard\(\) \{\n.*?^\}\n(?=\n_supervisor_args=)",
        source,
    )
    assert match is not None, "launcher _dashboard function is not statically measurable"
    return match.group(0)


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def test_five_named_dashboard_authority_sites_are_exactly_3003() -> None:
    launcher_source = LAUNCHER.read_text(encoding="utf-8")
    launcher_match = re.search(
        r'^export OMNIAGENTOS_DASH_PORT="\$\{OMNIAGENTOS_DASH_PORT:-(\d+)\}"$',
        launcher_source,
        re.MULTILINE,
    )
    assert launcher_match is not None

    package = json.loads((ROOT / "dashboard" / "package.json").read_text(encoding="utf-8"))
    authorities = {
        "launcher_default": int(launcher_match.group(1)),
        "dashboard_production_start": package["scripts"]["start"],
        "contracts_dashboard_origin": _python_constant(
            ROOT / "omniagentos" / "contracts.py",
            "DASHBOARD_ORIGIN",
        ),
        "api_default_cors_port": _python_constant(
            ROOT / "omniagentos" / "api" / "main.py",
            "_DEFAULT_DASHBOARD_PORT",
        ),
        "archdocs_default_dashboard_port": _python_constant(
            ROOT / "omniagentos" / "archdocs" / "generate.py",
            "_PORT_DEFAULT_DASH",
        ),
    }

    assert authorities == {
        "launcher_default": 3003,
        "dashboard_production_start": "next start -H 127.0.0.1 -p ${PORT:-3003}",
        "contracts_dashboard_origin": "http://127.0.0.1:3003",
        "api_default_cors_port": 3003,
        "archdocs_default_dashboard_port": "3003",
    }
    assert package["scripts"]["dev"] == "next dev -H 127.0.0.1 -p ${PORT:-3003}"


def test_dashboard_launcher_builds_stamped_sha_before_supplied_port_start(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log_path = tmp_path / "npm.log"
    expected_sha = "0123456789abcdef0123456789abcdef01234567"

    _write_executable(
        fake_bin / "git",
        (
            "#!/bin/sh\n"
            'test "$*" = "rev-parse --verify HEAD" || exit 91\n'
            f"printf '%s\\n' '{expected_sha}'\n"
        ),
    )
    _write_executable(
        fake_bin / "npm",
        (
            "#!/bin/sh\n"
            'printf \'%s|%s|%s\\n\' "$*" "${PORT-}" '
            '"${NEXT_PUBLIC_BUILD_SHA-}" >> "$P0_HYGIENE_NPM_LOG"\n'
        ),
    )

    harness = tmp_path / "dashboard-function.sh"
    harness.write_text(
        (
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "OMNIAGENTOS_DASH_PORT=43123\n"
            # The launcher preamble exports this before any target runs; the
            # harness extracts _dashboard alone, so under `set -u` it has to be
            # supplied here like OMNIAGENTOS_DASH_PORT above.
            "OMNIAGENTOS_CADDY_PORT=43133\n"
            "_prepare_runtime() { :; }\n"
            # Stubbed like _prepare_runtime: this test is about the build SHA
            # stamp and the port, and the real helper needs a writable secrets
            # dir. That the dashboard exports the trusted-hop secret, and does
            # so BEFORE the build, is pinned in
            # tests/scripts/test_trusted_hop_deployment.py.
            "_export_hop_env() { :; }\n"
            f"{_dashboard_function(LAUNCHER.read_text(encoding='utf-8'))}\n"
            "_dashboard\n"
        ),
        encoding="utf-8",
    )
    harness.chmod(0o755)

    env = os.environ.copy()
    env.pop("PORT", None)
    env.pop("NEXT_PUBLIC_BUILD_SHA", None)
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
    env["P0_HYGIENE_NPM_LOG"] = str(log_path)
    completed = subprocess.run(
        [str(harness)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
    assert log_path.read_text(encoding="utf-8").splitlines() == [
        f"run build||{expected_sha}",
        f"run start|43123|{expected_sha}",
    ]

    _write_executable(fake_bin / "git", "#!/bin/sh\nexit 17\n")
    log_path.unlink()
    failed = subprocess.run(
        [str(harness)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert failed.returncode != 0
    assert not log_path.exists(), "a missing verified HEAD must fail before npm build"


def test_supervisor_readiness_uses_canonical_api_health() -> None:
    source = (ROOT / "scripts" / "process_supervisor.py").read_text(encoding="utf-8")
    run_body = re.search(r"(?ms)^def _run\(args: Any\).*?(?=^def main\(\))", source)
    assert run_body is not None
    assert 'f"http://127.0.0.1:{args.api_port}/api/health"' in run_body.group(0)
    assert "/api/grok/health" not in run_body.group(0)


def test_default_cors_and_archdocs_measure_canonical_dashboard_port_3003() -> None:
    assert allowed_cors_origins(env={}) == [
        "http://127.0.0.1:3003",
        "http://localhost:3003",
    ]
    assert launcher_default_ports(ROOT) == ("8485", "3003")


def test_operator_docs_publish_current_and_next_migration() -> None:
    current, next_version = _current_and_next_migration()

    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    migrations = (ROOT / "omniagentos" / "db" / "migrations" / "README.md").read_text(
        encoding="utf-8"
    )
    assert f"currently `{current:03d}`; next `{next_version:03d}`" in agents
    assert f"**060–{current:03d}**" in migrations
    assert f"New Grok migrations start at **{next_version:03d}**." in migrations


def _sourced_launch_env_origins(
    launch_env: Path,
    *,
    extra_ports: str | None = None,
) -> list[str]:
    """Source the supported launch include in a clean shell, then probe real Python env."""
    env = {
        "HOME": os.environ.get("HOME", "/private/tmp"),
        "PATH": os.defpath,
    }
    if extra_ports is not None:
        env["OMNIAGENTOS_CORS_EXTRA_PORTS"] = extra_ports

    probe = (
        "import json, os;"
        "from omniagentos.api.main import allowed_cors_origins;"
        "print(json.dumps(allowed_cors_origins(env=os.environ)))"
    )
    completed = subprocess.run(
        [
            "/bin/bash",
            "--noprofile",
            "--norc",
            "-c",
            '. "$1"; exec "$2" -c "$3"',
            "p0-hygiene-cors-probe",
            str(launch_env),
            sys.executable,
            probe,
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )
    return json.loads(completed.stdout)


def _assert_exact_default_cors(origins: list[str]) -> None:
    assert origins == [
        "http://127.0.0.1:3003",
        "http://localhost:3003",
    ]


def test_launch_env_cors_defaults_and_explicit_override(tmp_path: Path) -> None:
    launch_env = ROOT / "scripts" / "launch-env.sh"

    # This is the real supported launch include, not an env={} unit-level shortcut.
    _assert_exact_default_cors(_sourced_launch_env_origins(launch_env))

    assert _sourced_launch_env_origins(launch_env, extra_ports="9999") == [
        "http://127.0.0.1:3003",
        "http://localhost:3003",
        "http://127.0.0.1:9999",
        "http://localhost:9999",
    ]

    # Hostile mutation: restoring a production default for E2E port 3002 must turn RED.
    mutated_launch_env = tmp_path / "launch-env.sh"
    mutated_launch_env.write_text(
        launch_env.read_text(encoding="utf-8").replace(
            "export OMNIAGENTOS_CORS_EXTRA_PORTS",
            ': "${OMNIAGENTOS_CORS_EXTRA_PORTS:=3002}"\nexport OMNIAGENTOS_CORS_EXTRA_PORTS',
            1,
        ),
        encoding="utf-8",
    )
    with pytest.raises(AssertionError):
        _assert_exact_default_cors(_sourced_launch_env_origins(mutated_launch_env))


def _assert_archi_stamp_matches_live(
    archi_markdown: str,
    archi_json: dict[str, object],
) -> None:
    stored = parse_stamp_comment(archi_markdown)
    assert stored is not None
    json_stamp = archi_json["stamp"]
    assert isinstance(json_stamp, dict)

    live = compute_stamp(ROOT)
    current, _ = _current_and_next_migration()
    assert stored.max_migration == json_stamp["max_migration"] == live.max_migration == current
    assert live.route_count > 0
    assert stored.route_count == json_stamp["route_count"] == live.route_count
    assert stored.git_head == json_stamp["git_head"]

    # The embedded SHA is source-commit provenance. Freshness accepts only the
    # source commit itself or its one direct, generator-owned oracle successor;
    # an arbitrary later descendant must remain stale.
    assert is_stale(ROOT) is False


def test_archi_stamp_gate_and_stale_route_mutation() -> None:
    archi_markdown = (ROOT / "ARCHI.md").read_text(encoding="utf-8")
    archi_json = json.loads((ROOT / "ARCHI.json").read_text(encoding="utf-8"))
    _assert_archi_stamp_matches_live(archi_markdown, archi_json)

    # Hostile mutation: even mutually consistent stale files must fail live truth.
    mutated_markdown = re.sub(
        r"route_count=\d+",
        "route_count=271",
        archi_markdown,
        count=1,
    )
    mutated_json = json.loads(json.dumps(archi_json))
    mutated_json["stamp"]["route_count"] = 271
    with pytest.raises(AssertionError):
        _assert_archi_stamp_matches_live(mutated_markdown, mutated_json)


def test_archi_stamp_desync_regression(tmp_path: Path) -> None:
    """Regression test: ARCHI.md and ARCHI.json stamps must be atomically synchronized.

    This test proves that the desync bug (where stamp_archi updates only ARCHI.md
    but not ARCHI.json) is fixed and cannot recur. After stamp_archi_and_json runs,
    both files must have matching stamps, field-for-field.
    """
    # Create temp copies of the current files
    temp_md = tmp_path / "ARCHI.md"
    temp_json = tmp_path / "ARCHI.json"
    temp_md.write_text((ROOT / "ARCHI.md").read_text(encoding="utf-8"))
    temp_json.write_text((ROOT / "ARCHI.json").read_text(encoding="utf-8"))

    # Parse the current stamps (baseline)
    original_md_stamp = parse_stamp_comment(temp_md.read_text())
    original_json_data = json.loads(temp_json.read_text())
    assert original_md_stamp is not None
    original_json_stamp = original_json_data["stamp"]

    # Verify they match before the test
    assert original_md_stamp.git_head == original_json_stamp["git_head"]
    assert original_md_stamp.max_migration == original_json_stamp["max_migration"]
    assert original_md_stamp.route_count == original_json_stamp["route_count"]

    # Mutate the files to have mismatched stamps (simulating the old bug behavior)
    mutated_md = temp_md.read_text()
    mutated_json_data = json.loads(temp_json.read_text())
    # Change JSON stamp to an old value
    mutated_json_data["stamp"]["generated_at"] = "2020-01-01T00:00:00Z"
    temp_json.write_text(json.dumps(mutated_json_data, indent=2, sort_keys=True) + "\n")
    # Change MD stamp similarly
    mutated_md = re.sub(
        r"generated_at=\S+",
        "generated_at=2020-01-01T00:00:00Z",
        mutated_md,
        count=1,
    )
    temp_md.write_text(mutated_md)

    # Verify stamps are initially out of sync
    before_md_stamp = parse_stamp_comment(temp_md.read_text())
    before_json_data = json.loads(temp_json.read_text())
    before_json_stamp = before_json_data["stamp"]
    assert before_md_stamp is not None
    assert before_md_stamp.generated_at != original_md_stamp.generated_at
    assert before_json_stamp["generated_at"] != original_json_stamp["generated_at"]

    # Call the NEW atomic stamping function
    stamp_archi_and_json(tmp_path, temp_md, temp_json)

    # Verify both files are now stamped identically with fresh values
    after_md_content = temp_md.read_text()
    after_json_data = json.loads(temp_json.read_text())

    after_md_stamp = parse_stamp_comment(after_md_content)
    after_json_stamp = after_json_data["stamp"]

    assert after_md_stamp is not None, "MD stamp should be present after stamping"
    assert after_json_stamp is not None, "JSON stamp should be present after stamping"

    # The critical test: stamps must match field-for-field
    assert (
        after_md_stamp.git_head == after_json_stamp["git_head"]
    ), "git_head should match in both files"
    assert (
        after_md_stamp.max_migration == after_json_stamp["max_migration"]
    ), "max_migration should match in both files"
    assert (
        after_md_stamp.route_count == after_json_stamp["route_count"]
    ), "route_count should match in both files"
    assert (
        after_md_stamp.generated_at == after_json_stamp["generated_at"]
    ), "generated_at should match in both files"

    # Verify the values are fresh (different from the old mutated values)
    assert after_md_stamp.generated_at != before_md_stamp.generated_at


def test_archi_stamp_old_behavior_fails(tmp_path: Path) -> None:
    """Prove the regression test fails with the old behavior (markdown-only stamping).

    This test demonstrates that calling only stamp_archi (the old way) leaves
    ARCHI.json out of sync, which our regression test would catch.
    """
    # Create temp copies of the current files
    temp_md = tmp_path / "ARCHI.md"
    temp_json = tmp_path / "ARCHI.json"
    temp_md.write_text((ROOT / "ARCHI.md").read_text(encoding="utf-8"))
    temp_json.write_text((ROOT / "ARCHI.json").read_text(encoding="utf-8"))

    # Call the OLD function that only stamps ARCHI.md
    stamp_archi(tmp_path, temp_md)

    # Verify ARCHI.md is stamped with a new value
    after_md_content = temp_md.read_text()
    after_md_stamp = parse_stamp_comment(after_md_content)
    assert after_md_stamp is not None

    # But ARCHI.json is NOT updated (old bug behavior)
    after_json_data = json.loads(temp_json.read_text())
    after_json_stamp = after_json_data["stamp"]

    # This should fail: stamps are out of sync
    # The generated_at will differ (MD has new, JSON still has old)
    assert after_md_stamp.generated_at != after_json_stamp["generated_at"], (
        "Old stamp_archi behavior leaves JSON out of sync "
        "(MD has new generated_at, JSON still has old value)"
    )
