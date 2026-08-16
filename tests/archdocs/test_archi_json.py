from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

from omniagentos.archdocs.generate import (
    _json_check_payload,
    emit_archi_json,
    regenerate_archi,
)
from omniagentos.archdocs.update import is_owned_doc
from tests.archdocs.test_archdocs import _build_fake_launchd, _build_fake_repo


def test_emit_archi_json_launchd_absent_is_null_not_empty_list(tmp_path: Path) -> None:
    """ARCHI.json must distinguish unmeasurable launchd from measured-empty.

    Defect class: three-valued scan consumed with bare truthiness so None
    silently becomes the favourable empty list.

    Counterfeit (must fail this test):
        "launchd_jobs": [list(j) for j in (scan_launchd(launchd_dir) or [])]
    That collapses absent None into [] — same JSON as a present empty dir.
    """
    _build_fake_repo(tmp_path)
    missing = tmp_path / "launchd-missing"
    empty = tmp_path / "launchd-empty"
    empty.mkdir()

    absent_data = emit_archi_json(tmp_path, launchd_dir=missing)
    empty_data = emit_archi_json(tmp_path, launchd_dir=empty)

    # Python dict: None vs [] (not bare-truthiness equivalent).
    assert absent_data["launchd_jobs"] is None
    assert empty_data["launchd_jobs"] == []
    assert absent_data["launchd_jobs"] != empty_data["launchd_jobs"]

    # On-disk JSON: null vs [] — regenerate + parse so the claim is retained
    # through the write path, not only the in-memory helper.
    archi_path = tmp_path / "ARCHI.md"
    archi_path.write_text("## Subsystems\n## Notes (human)\n", encoding="utf-8")
    regenerate_archi(tmp_path, archi_path=archi_path, launchd_dir=missing)
    written_absent = json.loads((tmp_path / "ARCHI.json").read_text(encoding="utf-8"))
    regenerate_archi(tmp_path, archi_path=archi_path, launchd_dir=empty)
    written_empty = json.loads((tmp_path / "ARCHI.json").read_text(encoding="utf-8"))

    assert written_absent["launchd_jobs"] is None
    assert written_empty["launchd_jobs"] == []
    assert written_absent["launchd_jobs"] != written_empty["launchd_jobs"]


def test_emit_archi_json_absent_archi_and_launcher_not_favourable(tmp_path: Path) -> None:
    """Absent ARCHI.md / launcher must not become empty lists or concrete ports.

    Defect class: non-result presented as a favourable measured result.
    Spot-check residual: with no ARCHI.md and no launch script,
    subsystems/env_flags must be null (not []), and ports must be null
    (not baked-in 8485/3003 facts).

    Counterfeit: ``if not launcher.exists(): return defaults`` +
    ``subsystems = []`` / ``env_flags = []`` when sources are missing.
    """
    from omniagentos.archdocs.generate import launcher_default_ports

    # Minimal measured inventory so other three-valued fields are not None.
    (tmp_path / "omniagentos" / "api" / "routes").mkdir(parents=True)
    (tmp_path / "omniagentos" / "db" / "migrations").mkdir(parents=True)
    launchd_empty = tmp_path / "launchd-empty"
    launchd_empty.mkdir()

    assert not (tmp_path / "ARCHI.md").exists()
    assert not (tmp_path / "scripts" / "launch-omniagentos.sh").exists()
    assert launcher_default_ports(tmp_path) is None

    data = emit_archi_json(tmp_path, launchd_dir=launchd_empty)
    assert data["subsystems"] is None, "absent ARCHI must not yield measured-empty subsystems"
    assert data["env_flags"] is None, "absent launcher must not yield measured-empty env_flags"
    assert data["ports"] is None, "absent launcher must not yield concrete default ports"
    assert data["ports"] != {"api": 8485, "dashboard": 3003}


def test_emit_archi_json_malformed_subsystems_not_measured_empty(tmp_path: Path) -> None:
    """Present malformed ARCHI must not publish the same subsystems as measured-empty.

    Defect class: non-result presented as a favourable measured result. A
    present ARCHI.md with no parseable ``## Subsystems`` section is
    unmeasurable — it must not collapse to the same ``[]`` published for a
    valid empty Subsystems section.

    Counterfeit (must fail this test): always publish
    ``parsed_subsystems`` (defaults to ``[]``) whenever the ARCHI file is
    present, so missing/malformed section ≡ measured-empty section.
    """
    (tmp_path / "omniagentos" / "api" / "routes").mkdir(parents=True)
    (tmp_path / "omniagentos" / "db" / "migrations").mkdir(parents=True)
    launchd_empty = tmp_path / "launchd-empty"
    launchd_empty.mkdir()

    # Measured empty: section present, no bullets, closed by a following heading.
    measured_empty = "## Subsystems\n\n## Notes (human)\n"
    # Malformed: present document, other headings, no Subsystems section at all.
    malformed = "# ARCHI.md\n\n## Overview\n\nProse only.\n\n## Notes (human)\n"
    # Garbage: present document with no structural headings.
    garbage = "# ARCHI.md\njust prose, no subsystem section\n"

    empty_data = emit_archi_json(tmp_path, archi_content=measured_empty, launchd_dir=launchd_empty)
    malformed_data = emit_archi_json(tmp_path, archi_content=malformed, launchd_dir=launchd_empty)
    garbage_data = emit_archi_json(tmp_path, archi_content=garbage, launchd_dir=launchd_empty)

    assert empty_data["subsystems"] == [], (
        "valid empty Subsystems section must be measured-empty []"
    )
    assert malformed_data["subsystems"] is None, (
        "present ARCHI without parseable Subsystems section must be null, not []"
    )
    assert garbage_data["subsystems"] is None, (
        "present unstructured ARCHI must be null, not measured-empty []"
    )
    assert malformed_data["subsystems"] != empty_data["subsystems"]
    assert garbage_data["subsystems"] != empty_data["subsystems"]

    # Write path: null vs [] retained on disk for the residual cases.
    archi_path = tmp_path / "ARCHI.md"
    archi_path.write_text(malformed, encoding="utf-8")
    regenerate_archi(tmp_path, archi_path=archi_path, launchd_dir=launchd_empty)
    written_malformed = json.loads((tmp_path / "ARCHI.json").read_text(encoding="utf-8"))
    archi_path.write_text(measured_empty, encoding="utf-8")
    regenerate_archi(tmp_path, archi_path=archi_path, launchd_dir=launchd_empty)
    written_empty = json.loads((tmp_path / "ARCHI.json").read_text(encoding="utf-8"))

    assert written_malformed["subsystems"] is None
    assert written_empty["subsystems"] == []
    assert written_malformed["subsystems"] != written_empty["subsystems"]


def test_emit_archi_json_malformed_subsystem_bullets_not_measured_empty(
    tmp_path: Path,
) -> None:
    """Present ``## Subsystems`` with malformed bullets must not publish ``[]``.

    Defect class: non-result as favourable measured result. A present section
    whose bullets fail the ``**name**`` shape is unparseable inventory — not
    the same as a valid empty section (no bullets → ``[]``). Skipping
    unparseable bullets into ``parsed_subsystems=[]`` is the counterfeit the
    missing-section binder does not catch.

    Counterfeit (must fail this test): keep publishing ``parsed_subsystems``
    (defaults to ``[]``) whenever the section heading is present, ignoring
    that collected bullets failed to parse.
    """
    (tmp_path / "omniagentos" / "api" / "routes").mkdir(parents=True)
    (tmp_path / "omniagentos" / "db" / "migrations").mkdir(parents=True)
    launchd_empty = tmp_path / "launchd-empty"
    launchd_empty.mkdir()

    measured_empty = "## Subsystems\n\n## Notes (human)\n"
    # Section present, closed by next heading, but bullets are not **name**.
    malformed_bullets = (
        "# ARCHI.md\n\n"
        "## Subsystems\n\n"
        "- not a bold name item\n"
        "- also broken without bold\n\n"
        "## Notes (human)\n"
    )
    # Mixed: one valid + one malformed must not silently drop the bad item
    # and publish a partial favourable list either.
    mixed_bullets = (
        "# ARCHI.md\n\n"
        "## Subsystems\n\n"
        "- **Execution** — State machine\n"
        "- bare text without bold name\n\n"
        "## Notes (human)\n"
    )
    valid_bullets = (
        "# ARCHI.md\n\n## Subsystems\n\n- **Execution** — State machine\n\n## Notes (human)\n"
    )

    empty_data = emit_archi_json(tmp_path, archi_content=measured_empty, launchd_dir=launchd_empty)
    bad_data = emit_archi_json(tmp_path, archi_content=malformed_bullets, launchd_dir=launchd_empty)
    mixed_data = emit_archi_json(tmp_path, archi_content=mixed_bullets, launchd_dir=launchd_empty)
    good_data = emit_archi_json(tmp_path, archi_content=valid_bullets, launchd_dir=launchd_empty)

    assert empty_data["subsystems"] == [], (
        "valid empty Subsystems section must be measured-empty []"
    )
    assert bad_data["subsystems"] is None, (
        "present Subsystems section with only malformed bullets must be null, "
        "not the same favourable [] as measured-empty"
    )
    assert mixed_data["subsystems"] is None, (
        "present Subsystems section with any malformed bullet must be null, "
        "not a partial list that drops unparseable items"
    )
    assert good_data["subsystems"] == [{"name": "Execution", "description": "State machine"}]
    assert bad_data["subsystems"] != empty_data["subsystems"]
    assert mixed_data["subsystems"] != empty_data["subsystems"]

    archi_path = tmp_path / "ARCHI.md"
    archi_path.write_text(malformed_bullets, encoding="utf-8")
    regenerate_archi(tmp_path, archi_path=archi_path, launchd_dir=launchd_empty)
    written_bad = json.loads((tmp_path / "ARCHI.json").read_text(encoding="utf-8"))
    archi_path.write_text(measured_empty, encoding="utf-8")
    regenerate_archi(tmp_path, archi_path=archi_path, launchd_dir=launchd_empty)
    written_empty = json.loads((tmp_path / "ARCHI.json").read_text(encoding="utf-8"))

    assert written_bad["subsystems"] is None
    assert written_empty["subsystems"] == []
    assert written_bad["subsystems"] != written_empty["subsystems"]


def test_emit_archi_json_inventory_absent_is_null_not_empty_list(tmp_path: Path) -> None:
    """ARCHI.json must distinguish absent routes/migrations/packages from empty.

    Defect class: missing inventory sources collapsed to [] — same as measured
    empty, so unknown renders as a valid zero/none result.

    Counterfeit: ``[list(r) for r in scan_routes(...)]`` / bare ``scan_packages``
    with scanners that return [] when the source directory is missing.
    """
    launchd_empty = tmp_path / "launchd-empty"
    launchd_empty.mkdir()

    # Missing: no omniagentos tree at all → routes/migrations/packages unmeasurable.
    missing_root = tmp_path / "missing_inventory"
    missing_root.mkdir()
    (missing_root / "ARCHI.md").write_text("## Subsystems\n## Notes (human)\n", encoding="utf-8")

    # Empty: sources present but inventory is measured-empty.
    empty_root = tmp_path / "empty_inventory"
    (empty_root / "omniagentos" / "api" / "routes").mkdir(parents=True)
    (empty_root / "omniagentos" / "db" / "migrations").mkdir(parents=True)
    (empty_root / "omniagentos").mkdir(parents=True, exist_ok=True)
    (empty_root / "ARCHI.md").write_text("## Subsystems\n## Notes (human)\n", encoding="utf-8")

    absent_data = emit_archi_json(missing_root, launchd_dir=launchd_empty)
    empty_data = emit_archi_json(empty_root, launchd_dir=launchd_empty)

    for key in ("routes", "migrations", "packages"):
        assert empty_data[key] == [], f"measured-empty {key} must be []"
        assert absent_data[key] is None, f"absent {key} must be null/None"
        assert absent_data[key] != empty_data[key]

    # Write path: null vs [] retained on disk.
    regenerate_archi(missing_root, archi_path=missing_root / "ARCHI.md", launchd_dir=launchd_empty)
    written_absent = json.loads((missing_root / "ARCHI.json").read_text(encoding="utf-8"))
    regenerate_archi(empty_root, archi_path=empty_root / "ARCHI.md", launchd_dir=launchd_empty)
    written_empty = json.loads((empty_root / "ARCHI.json").read_text(encoding="utf-8"))

    for key in ("routes", "migrations", "packages"):
        assert written_empty[key] == []
        assert written_absent[key] is None
        assert written_absent[key] != written_empty[key]


def test_archi_json_emitted_and_parsed(tmp_path: Path) -> None:
    _build_fake_repo(tmp_path)
    launchd_dir = _build_fake_launchd(tmp_path)

    # Create launch script to test env flags parsing
    (tmp_path / "scripts").mkdir(parents=True, exist_ok=True)
    (tmp_path / "scripts" / "launch-omniagentos.sh").write_text(
        "export OMNIAGENTOS_SWARM_EXECUTE=1\n"
        "export OMNIAGENTOS_FAST_DISPATCH=1\n"
        # Not `OMNIAGENTOS_`-prefixed on purpose: env-flag scanning matches by
        # prefix only, so this line is the negative control proving a
        # non-namespaced export (e.g. a plain port var) is never swept into
        # env_flags.
        "export PORT=8485\n",
        encoding="utf-8",
    )

    # Create ARCHI.md with subsystems
    archi_path = tmp_path / "ARCHI.md"
    archi_path.write_text(
        "<!-- archdocs:stamp git_head=abc max_migration=2 route_count=2 generated_at=2026-07-25T09:30:05Z -->\n"
        "## Subsystems\n"
        "- **Execution** — State machine\n"
        "- **Governance** — Approvals\n"
        "## Notes (human)\n",
        encoding="utf-8",
    )

    # Regenerate ARCHI
    regenerate_archi(tmp_path, archi_path=archi_path, launchd_dir=launchd_dir)

    # Verify ARCHI.json is created
    json_path = tmp_path / "ARCHI.json"
    assert json_path.exists()

    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert data["schema_version"] == 1
    assert data["stamp"]["max_migration"] == 2
    assert len(data["subsystems"]) == 2
    assert data["subsystems"][0]["name"] == "Execution"
    assert data["subsystems"][0]["description"] == "State machine"
    assert data["subsystems"][1]["name"] == "Governance"
    assert data["subsystems"][1]["description"] == "Approvals"
    assert "packages" in data
    assert "routes" in data
    assert "migrations" in data
    assert "launchd_jobs" in data
    assert "ports" in data
    assert len(data["env_flags"]) == 2
    assert data["env_flags"][0]["name"] == "OMNIAGENTOS_SWARM_EXECUTE"
    assert data["env_flags"][0]["value"] == "1"


def test_check_fails_when_stale(tmp_path: Path) -> None:
    _build_fake_repo(tmp_path)
    launchd_dir = _build_fake_launchd(tmp_path)

    archi_path = tmp_path / "ARCHI.md"
    archi_path.write_text("## Subsystems\n## Notes (human)\n", encoding="utf-8")

    # Run generate to create both md and json
    regenerate_archi(tmp_path, archi_path=archi_path, launchd_dir=launchd_dir)

    # Check command should pass (exit code 0) even after a second boundary —
    # stamp.generated_at is wall-clock noise, not material drift.
    time.sleep(1.1)
    cmd = [
        sys.executable,
        "-m",
        "omniagentos.archdocs.generate",
        "--repo-root",
        str(tmp_path),
        "--archi-path",
        str(archi_path),
        "--launchd-dir",
        str(launchd_dir),
        "--check",
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    assert res.returncode == 0, f"fresh regenerate must pass --check; stderr={res.stderr!r}"

    # Modify ARCHI.json to trigger drift/staleness
    json_path = tmp_path / "ARCHI.json"
    json_path.write_text("{}", encoding="utf-8")

    # Running check now should fail (exit code 1)
    res2 = subprocess.run(cmd, capture_output=True, text=True)
    assert res2.returncode == 1


def test_json_check_payload_ignores_generated_at_only_drift() -> None:
    """Counterfeit: byte-compare ARCHI.json so a clock tick fails --check.

    Requirement: generated_at-only differences are not material drift.
    """
    a = (
        json.dumps(
            {
                "schema_version": 1,
                "stamp": {
                    "git_head": "abc",
                    "max_migration": 2,
                    "route_count": 3,
                    "generated_at": "2026-07-29T12:00:00Z",
                },
                "packages": ["x"],
            },
            sort_keys=True,
            indent=2,
        )
        + "\n"
    )
    b = (
        json.dumps(
            {
                "schema_version": 1,
                "stamp": {
                    "git_head": "abc",
                    "max_migration": 2,
                    "route_count": 3,
                    "generated_at": "2026-07-29T12:00:01Z",
                },
                "packages": ["x"],
            },
            sort_keys=True,
            indent=2,
        )
        + "\n"
    )
    assert _json_check_payload(a) == _json_check_payload(b)

    c = (
        json.dumps(
            {
                "schema_version": 1,
                "stamp": {
                    "git_head": "abc",
                    "max_migration": 9,
                    "route_count": 3,
                    "generated_at": "2026-07-29T12:00:00Z",
                },
                "packages": ["x"],
            },
            sort_keys=True,
            indent=2,
        )
        + "\n"
    )
    assert _json_check_payload(a) != _json_check_payload(c)


def test_json_check_payload_absent_generated_at_not_equal_present() -> None:
    """Absent stamp.generated_at must not normalize equal to a present one.

    Counterfeit: delete the key from both sides before compare, so a file
    missing required stamp metadata checks as fresh against a good file.
    """
    present = (
        json.dumps(
            {
                "schema_version": 1,
                "stamp": {
                    "git_head": "abc",
                    "max_migration": 2,
                    "route_count": 3,
                    "generated_at": "2026-07-29T12:00:00Z",
                },
                "packages": ["x"],
            },
            sort_keys=True,
            indent=2,
        )
        + "\n"
    )
    absent = (
        json.dumps(
            {
                "schema_version": 1,
                "stamp": {
                    "git_head": "abc",
                    "max_migration": 2,
                    "route_count": 3,
                },
                "packages": ["x"],
            },
            sort_keys=True,
            indent=2,
        )
        + "\n"
    )
    assert _json_check_payload(present) != _json_check_payload(absent)
    # raw payloads already differ; normalization must preserve the distinction
    present_norm = json.loads(_json_check_payload(present) or "")
    absent_norm = json.loads(_json_check_payload(absent) or "")
    assert "generated_at" in present_norm["stamp"]
    assert "generated_at" not in absent_norm["stamp"]


def test_is_owned_doc_refuses_archi_json(tmp_path: Path) -> None:
    assert is_owned_doc(tmp_path, tmp_path / "ARCHI.md") is True
    assert is_owned_doc(tmp_path, tmp_path / "ARCHI.json") is False
