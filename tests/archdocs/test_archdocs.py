"""Tests for the living architecture docs (§8, package W9).

Covers the acceptance criteria this package is responsible for (V2-DESIGN.md §13.7):
regeneration preserves human sections, staleness flips after a synthetic stamp
change, and `load_arch_context` returns a relevant section for a sample focus term.
All fixtures build a SYNTHETIC tmp_path repo skeleton — never the real repo — so
these tests are stable regardless of what other concurrently-built packages change.
"""

from __future__ import annotations

import json
import re
import subprocess
import textwrap
from pathlib import Path

import pytest

from omniagentos.archdocs.context import load_arch_context
from omniagentos.archdocs.generate import (
    _ROUTE_DECORATOR_RE,
    regenerate_archi,
    render_launchd_block,
    render_migrations_block,
    render_packages_block,
    render_routes_block,
    replace_generated_blocks,
    scan_launchd,
    scan_migrations,
    scan_packages,
    scan_routes,
)
from omniagentos.archdocs.staleness import (
    compute_stamp,
    is_stale,
    parse_stamp_comment,
    render_stamp_comment,
    stamp_archi,
)
from omniagentos.archdocs.update import ArchdocsGuardError, apply_update, is_owned_doc

# ---------------------------------------------------------------------------
# Fixture: a small synthetic repo skeleton with routes/migrations/packages
# ---------------------------------------------------------------------------


def _build_fake_repo(root: Path, n_migrations: int = 2) -> None:
    (root / "omniagentos" / "api" / "routes").mkdir(parents=True, exist_ok=True)
    (root / "omniagentos" / "db" / "migrations").mkdir(parents=True, exist_ok=True)
    (root / "omniagentos" / "alpha").mkdir(parents=True, exist_ok=True)
    (root / "omniagentos" / "alpha" / "__init__.py").write_text("", encoding="utf-8")
    (root / "omniagentos" / "beta").mkdir(parents=True, exist_ok=True)
    (root / "omniagentos" / "beta" / "__init__.py").write_text("", encoding="utf-8")

    (root / "omniagentos" / "api" / "routes" / "widgets.py").write_text(
        textwrap.dedent(
            """
            from fastapi import APIRouter
            router = APIRouter(prefix="/api/widgets")

            @router.get("/list")
            def list_widgets():
                ...

            @router.post("/create")
            def create_widget():
                ...
            """
        ),
        encoding="utf-8",
    )

    names = ["001_init", "002_widgets", "003_gizmos", "004_gadgets"]
    for i in range(n_migrations):
        (root / "omniagentos" / "db" / "migrations" / f"{names[i]}.sql").write_text(
            "-- test migration\nCREATE TABLE x(id INTEGER);\n", encoding="utf-8"
        )


def _build_fake_launchd(root: Path) -> Path:
    launchd_dir = root / "LaunchAgents"
    launchd_dir.mkdir(parents=True, exist_ok=True)
    (launchd_dir / "com.omniagentos.widgets.plist").write_bytes(
        b"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.omniagentos.widgets</string>
    <key>StartInterval</key><integer>300</integer>
</dict>
</plist>
"""
    )
    return launchd_dir


# ---------------------------------------------------------------------------
# Scanners
# ---------------------------------------------------------------------------


def test_scan_routes_finds_decorated_endpoints(tmp_path: Path) -> None:
    _build_fake_repo(tmp_path)
    routes = scan_routes(tmp_path)
    assert ("GET", "/list", "widgets") in routes
    assert ("POST", "/create", "widgets") in routes


def test_scan_migrations_sorted_by_number(tmp_path: Path) -> None:
    _build_fake_repo(tmp_path, n_migrations=3)
    migs = scan_migrations(tmp_path)
    assert [m[0] for m in migs] == [1, 2, 3]


def test_scan_packages_lists_only_real_packages(tmp_path: Path) -> None:
    _build_fake_repo(tmp_path)
    pkgs = scan_packages(tmp_path)
    assert pkgs is not None
    assert "alpha" in pkgs
    assert "beta" in pkgs
    # "api" and "db" have no __init__.py in this synthetic fixture (only nested
    # subpackages do) — the scanner requires __init__.py at the top level, so
    # they must NOT show up as top-level packages.
    assert "api" not in pkgs
    assert "db" not in pkgs


def test_scan_routes_missing_dir_is_not_favourable_empty(tmp_path: Path) -> None:
    """Absent routes source must not render as measured zero routes.

    Defect class: a missing source reported identically to a genuinely empty one.
    Counterfeit: ``if not routes_dir.is_dir(): return []`` so both paths emit
    ``**0 routes across 0 modules.**``.
    """
    missing = tmp_path  # no omniagentos/api/routes
    empty = tmp_path / "empty_routes_repo"
    (empty / "omniagentos" / "api" / "routes").mkdir(parents=True)

    empty_routes = scan_routes(empty)
    missing_routes = scan_routes(missing)
    assert empty_routes == []
    assert missing_routes is None
    assert missing_routes != empty_routes

    empty_block = render_routes_block(empty)
    missing_block = render_routes_block(missing)
    assert "**0 routes across 0 modules.**" in empty_block
    assert "**0 routes" not in missing_block
    assert "_(none found)_" not in missing_block
    assert "unavailable" in missing_block.lower()


def test_scan_migrations_unreadable_dir_not_measured_empty(tmp_path: Path) -> None:
    """Unreadable migrations directory must not render as version 0.

    Defect class: missing/unreadable source reported identically to a genuinely
    empty one. ``Path.glob`` under a chmod-000 directory can yield [] silently,
    so renderers emit ``**0 migrations, current version 0.**``.

    Counterfeit: iterate ``mig_dir.glob("*.sql")`` without an enumeration
    probe; unreadable dir and empty dir both become [].
    """
    import os
    import stat

    empty = tmp_path / "empty_migs_repo"
    (empty / "omniagentos" / "db" / "migrations").mkdir(parents=True)

    unreadable = tmp_path / "unreadable_migs_repo"
    mig_dir = unreadable / "omniagentos" / "db" / "migrations"
    mig_dir.mkdir(parents=True)
    (mig_dir / "001_init.sql").write_text(
        "-- test migration\nCREATE TABLE x(id INTEGER);\n", encoding="utf-8"
    )
    os.chmod(mig_dir, 0)

    try:
        empty_migs = scan_migrations(empty)
        unreadable_migs = scan_migrations(unreadable)
        assert empty_migs == []
        assert unreadable_migs is None, (
            "unreadable migrations directory must be None (unmeasurable), not [] "
            f"(got {unreadable_migs!r})"
        )
        assert unreadable_migs != empty_migs

        empty_block = render_migrations_block(empty)
        unreadable_block = render_migrations_block(unreadable)
        assert "**0 migrations, current version 0.**" in empty_block
        assert "**0 migrations" not in unreadable_block
        assert "current version 0" not in unreadable_block
        assert "unavailable" in unreadable_block.lower()
    finally:
        os.chmod(mig_dir, stat.S_IRWXU)


def test_scan_migrations_missing_dir_is_not_favourable_empty(tmp_path: Path) -> None:
    """Absent migrations source must not render as measured zero / version 0.

    Defect class: a missing source reported identically to a genuinely empty one.
    Counterfeit: ``if not mig_dir.is_dir(): return []`` so both paths emit
    ``**0 migrations, current version 0.**``.
    """
    missing = tmp_path  # no omniagentos/db/migrations
    empty = tmp_path / "empty_mig_repo"
    (empty / "omniagentos" / "db" / "migrations").mkdir(parents=True)

    empty_migs = scan_migrations(empty)
    missing_migs = scan_migrations(missing)
    assert empty_migs == []
    assert missing_migs is None
    assert missing_migs != empty_migs

    empty_block = render_migrations_block(empty)
    missing_block = render_migrations_block(missing)
    assert "**0 migrations, current version 0.**" in empty_block
    assert "**0 migrations" not in missing_block
    assert "current version 0" not in missing_block
    assert "unavailable" in missing_block.lower()


def test_scan_packages_missing_dir_is_not_favourable_empty(tmp_path: Path) -> None:
    """Absent package source must not render as measured ``_(none found)_``.

    Defect class: a missing source reported identically to a genuinely empty one.
    Counterfeit: ``if not base.is_dir(): return []`` so both paths emit
    ``_(none found)_``.
    """
    missing = tmp_path  # no omniagentos/
    empty = tmp_path / "empty_pkg_repo"
    (empty / "omniagentos").mkdir(parents=True)

    empty_pkgs = scan_packages(empty)
    missing_pkgs = scan_packages(missing)
    assert empty_pkgs == []
    assert missing_pkgs is None
    assert missing_pkgs != empty_pkgs

    empty_block = render_packages_block(empty)
    missing_block = render_packages_block(missing)
    assert "_(none found)_" in empty_block
    assert "_(none found)_" not in missing_block
    assert "unavailable" in missing_block.lower()


def test_scan_launchd_parses_label_and_interval(tmp_path: Path) -> None:
    launchd_dir = _build_fake_launchd(tmp_path)
    jobs = scan_launchd(launchd_dir)
    assert jobs == [("com.omniagentos.widgets", "every 300s")]


def test_scan_launchd_missing_dir_is_not_favourable_empty(tmp_path: Path) -> None:
    """Absent launchd directory must not render/check as a present empty scan.

    Defect class: a missing source reported identically to a genuinely empty one.
    Counterfeit: ``if not launchd_dir.is_dir(): return []`` so both paths emit
    ``_(none found)_``.
    """
    missing = tmp_path / "does-not-exist"
    empty = tmp_path / "empty_launchd"
    empty.mkdir()

    missing_jobs = scan_launchd(missing)
    empty_jobs = scan_launchd(empty)
    assert empty_jobs == []
    # Three-valued: absent is None, not the favourable empty list.
    assert missing_jobs is None
    assert missing_jobs != empty_jobs

    missing_block = render_launchd_block(missing)
    empty_block = render_launchd_block(empty)
    assert "_(none found)_" in empty_block
    assert "_(none found)_" not in missing_block
    assert "unavailable" in missing_block.lower()


def test_scan_launchd_unreadable_dir_not_measured_empty(tmp_path: Path) -> None:
    """Unreadable launchd directory must not render as measured-empty inventory.

    Defect class: missing/unreadable source reported identically to a genuinely
    empty one. ``Path.glob`` under a chmod-000 directory can yield [] silently,
    so renderers emit ``_(none found)_``.

    Counterfeit: iterate ``launchd_dir.glob(...)`` without an enumeration
    probe; unreadable dir and empty dir both become [].
    """
    import os
    import stat

    empty = tmp_path / "empty_launchd"
    empty.mkdir()

    unreadable = tmp_path / "unreadable_launchd"
    unreadable.mkdir()
    (unreadable / "com.omniagentos.widgets.plist").write_bytes(
        b'<?xml version="1.0"?><plist version="1.0"><dict>'
        b"<key>Label</key><string>com.omniagentos.widgets</string>"
        b"<key>StartInterval</key><integer>300</integer>"
        b"</dict></plist>"
    )
    os.chmod(unreadable, 0)

    try:
        empty_jobs = scan_launchd(empty)
        unreadable_jobs = scan_launchd(unreadable)
        assert empty_jobs == []
        assert unreadable_jobs is None, (
            "unreadable launchd directory must be None (unmeasurable), not [] "
            f"(got {unreadable_jobs!r})"
        )
        assert unreadable_jobs != empty_jobs

        empty_block = render_launchd_block(empty)
        unreadable_block = render_launchd_block(unreadable)
        assert "_(none found)_" in empty_block
        assert "_(none found)_" not in unreadable_block
        assert "unavailable" in unreadable_block.lower()
    finally:
        os.chmod(unreadable, stat.S_IRWXU)


def test_unparseable_launchd_plist_does_not_render_as_none_found(tmp_path: Path) -> None:
    """Unparseable plists must not look like a genuinely empty launchd scan.

    Defect class: a missing/unreadable source reported identically to a
    genuinely empty one. Counterfeit: ``except Exception: continue`` so the
    scan returns [] and the block still renders ``_(none found)_``.
    """
    empty_dir = tmp_path / "empty_launchd"
    empty_dir.mkdir()
    broken_dir = tmp_path / "broken_launchd"
    broken_dir.mkdir()
    (broken_dir / "com.omniagentos.broken.plist").write_bytes(b"not-a-valid-plist")

    empty_jobs = scan_launchd(empty_dir)
    broken_jobs = scan_launchd(broken_dir)
    assert empty_jobs == []
    assert broken_jobs is not None
    # Unparseable must surface — never collapse to the same empty scan.
    assert broken_jobs != empty_jobs
    assert any(label == "com.omniagentos.broken" for label, _ in broken_jobs)
    assert any("unparseable" in schedule.lower() for _, schedule in broken_jobs)

    empty_block = render_launchd_block(empty_dir)
    broken_block = render_launchd_block(broken_dir)
    assert "_(none found)_" in empty_block
    # Governing filter: unparseable must never render as good / empty.
    assert "_(none found)_" not in broken_block
    assert "com.omniagentos.broken" in broken_block
    assert "unparseable" in broken_block.lower()


def test_unparseable_route_source_does_not_render_as_measured_zero(tmp_path: Path) -> None:
    """Invalid/undecodable route modules must not look like measured inventory.

    Defect class: unparseable source reported identically to a genuinely empty
    inventory *or* counted as a real route. Counterfeits this test must catch:

    1. Scanner: ``read_text(..., errors=\"replace\")`` + silent skip so a dir
       with only ``broken.py`` (invalid UTF-8) yields ``[]`` and
       ``**0 routes across 0 modules.**``.
    2. Renderer: generic ``len(routes)`` counting that treats the
       ``UNPARSEABLE`` sentinel as a real route
       (``UNPARSEABLE broken.py`` + ``**1 routes across 1 modules.**``).
    3. Syntax-only match: UTF-8-decodable but syntactically invalid Python that
       still contains ``@router.get('/x')`` must not be counted as a real route.
    """
    empty = tmp_path / "empty_routes_repo"
    (empty / "omniagentos" / "api" / "routes").mkdir(parents=True)

    broken = tmp_path / "broken_routes_repo"
    routes_dir = broken / "omniagentos" / "api" / "routes"
    routes_dir.mkdir(parents=True)
    # Invalid UTF-8 bytes — must not be silently replaced into an empty scan.
    (routes_dir / "broken.py").write_bytes(b"\xff\xfe not valid utf-8 @router.get('/x')")

    empty_routes = scan_routes(empty)
    broken_routes = scan_routes(broken)
    assert empty_routes == []
    assert broken_routes is not None
    assert broken_routes != empty_routes
    assert any(method == "UNPARSEABLE" for method, _, _ in broken_routes)
    assert any(module == "broken" for _, _, module in broken_routes)
    # Sentinel is not a real HTTP method/path inventory entry.
    assert not any(method == "GET" for method, _, _ in broken_routes)

    empty_block = render_routes_block(empty)
    broken_block = render_routes_block(broken)
    assert "**0 routes across 0 modules.**" in empty_block
    # Governing filter: unparseable must never render as measured zero OR as a
    # measured positive count of the sentinel itself.
    assert "**0 routes" not in broken_block
    assert "**1 routes" not in broken_block
    assert "UNPARSEABLE broken.py" not in broken_block
    assert "unparseable" in broken_block.lower()
    assert "broken.py" in broken_block
    assert "inventory incomplete" in broken_block.lower()

    # Residual: syntactically invalid but UTF-8-decodable source with a
    # decorator string must not be counted as a real route.
    syntax_bad = tmp_path / "syntax_bad_routes_repo"
    syntax_dir = syntax_bad / "omniagentos" / "api" / "routes"
    syntax_dir.mkdir(parents=True)
    (syntax_dir / "broken.py").write_text(
        "def oops(\n@router.get('/x')\ndef x():\n    ...\n",
        encoding="utf-8",
    )
    syntax_routes = scan_routes(syntax_bad)
    assert syntax_routes is not None
    assert any(method == "UNPARSEABLE" for method, _, _ in syntax_routes)
    assert not any(method == "GET" and path == "/x" for method, path, _ in syntax_routes)
    syntax_block = render_routes_block(syntax_bad)
    assert "GET /x" not in syntax_block
    assert "**1 routes" not in syntax_block
    assert "unparseable" in syntax_block.lower()


# ---------------------------------------------------------------------------
# Acceptance: regeneration preserves human sections (§13.7)
# ---------------------------------------------------------------------------


def test_regenerate_bootstraps_when_missing(tmp_path: Path) -> None:
    _build_fake_repo(tmp_path)
    launchd_dir = _build_fake_launchd(tmp_path)
    archi_path = tmp_path / "ARCHI.md"
    assert not archi_path.exists()

    content = regenerate_archi(tmp_path, archi_path=archi_path, launchd_dir=launchd_dir)

    assert archi_path.exists()
    assert "<!-- generated:begin:migrations -->" in content
    assert "<!-- generated:end:migrations -->" in content
    assert "## Notes (human)" in content
    assert "com.omniagentos.widgets" in content


def test_regenerate_preserves_human_section_and_narrative(tmp_path: Path) -> None:
    _build_fake_repo(tmp_path, n_migrations=2)
    launchd_dir = _build_fake_launchd(tmp_path)
    archi_path = tmp_path / "ARCHI.md"

    regenerate_archi(tmp_path, archi_path=archi_path, launchd_dir=launchd_dir)

    # A human hand-edits: adds custom narrative AND a Notes (human) section.
    existing = archi_path.read_text(encoding="utf-8")
    human_note = "## Notes (human)\n\nthe operator says: widgets are load-bearing, do not remove.\n"
    hand_edited = existing.split("## Notes (human)")[0] + human_note
    custom_marker = "\n<!-- CUSTOM NARRATIVE MARKER, MUST SURVIVE REGEN -->\n"
    hand_edited = hand_edited.replace("## Subsystems\n", "## Subsystems\n" + custom_marker, 1)
    archi_path.write_text(hand_edited, encoding="utf-8")

    # Now bump migrations (simulating new code landing) and regenerate again.
    _build_fake_repo(tmp_path, n_migrations=4)
    regenerated = regenerate_archi(tmp_path, archi_path=archi_path, launchd_dir=launchd_dir)

    # Human section preserved byte-for-byte.
    assert "the operator says: widgets are load-bearing, do not remove." in regenerated
    # Custom narrative outside any generated block preserved byte-for-byte.
    assert custom_marker.strip() in regenerated
    # But the generated migrations block DID update to reflect the new migration.
    assert "004_gadgets.sql" in regenerated
    assert "**4 migrations, current version 4.**" in regenerated


def test_replace_generated_blocks_ignores_unknown_ids() -> None:
    original = (
        "before\n"
        "<!-- generated:begin:migrations -->\nold\n<!-- generated:end:migrations -->\n"
        "<!-- generated:begin:mystery -->\nkeep me\n<!-- generated:end:mystery -->\n"
        "after\n"
    )
    updated = replace_generated_blocks(
        original,
        {
            "migrations": "<!-- generated:begin:migrations -->\nnew\n<!-- generated:end:migrations -->\n"
        },
    )
    assert "new" in updated
    assert "old" not in updated
    assert "keep me" in updated  # untouched — not in the replacement dict


# ---------------------------------------------------------------------------
# Acceptance: staleness flips after a synthetic stamp change (§13.7)
# ---------------------------------------------------------------------------


def test_stamp_roundtrip() -> None:
    stamp = compute_stamp(Path("/nonexistent/repo"))
    comment = render_stamp_comment(stamp)
    parsed = parse_stamp_comment(f"# doc\n{comment}\nmore text\n")
    assert parsed == stamp


def test_missing_doc_is_stale(tmp_path: Path) -> None:
    assert is_stale(tmp_path, tmp_path / "ARCHI.md") is True


def test_missing_stamp_is_stale(tmp_path: Path) -> None:
    archi = tmp_path / "ARCHI.md"
    archi.write_text("# ARCHI.md\nno stamp here\n", encoding="utf-8")
    assert is_stale(tmp_path, archi) is True


def _init_git(root: Path) -> None:
    """Give a tmp_path a measurable HEAD so is_stale is not forced by unknown git."""
    subprocess.run(["git", "init", "-q"], cwd=str(root), check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(root), check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(root), check=True)
    seed = root / ".gitkeep"
    seed.write_text("", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=str(root), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=str(root), check=True)


def _commit(root: Path, message: str, *paths: str) -> str:
    subprocess.run(["git", "add", "--", *paths], cwd=str(root), check=True)
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=str(root), check=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(root),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_committed_owned_refresh_is_fresh_then_code_commit_is_stale(tmp_path: Path) -> None:
    """Only the direct, oracle-only successor of the stamped source is fresh."""
    _build_fake_repo(tmp_path)
    _init_git(tmp_path)
    archi = tmp_path / "ARCHI.md"
    archi.write_text("# ARCHI.md\n", encoding="utf-8")

    source = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    stamp_archi(tmp_path, archi)
    refresh = _commit(tmp_path, "archdocs refresh", "ARCHI.md")

    stored = parse_stamp_comment(archi.read_text(encoding="utf-8"))
    assert stored is not None
    assert stored.git_head == source
    assert refresh != source
    assert is_stale(tmp_path, archi) is False, (
        "one direct commit changing only generator-owned oracles must remain fresh"
    )

    (tmp_path / "omniagentos" / "alpha" / "feature.py").write_text(
        "ENABLED = True\n", encoding="utf-8"
    )
    _commit(tmp_path, "code after refresh", "omniagentos/alpha/feature.py")
    assert is_stale(tmp_path, archi) is False, (
        "a MAP-NEUTRAL commit (no oracle, no architecture surface, no package "
        "add/delete) must not invalidate the stamp — the one-successor rule "
        "reddened main after nearly every landing while the stamped quantities "
        "were still compared live (2026-08-15 redesign)"
    )

    (tmp_path / "omniagentos" / "alpha" / "more.py").write_text(
        "MORE = True\n", encoding="utf-8"
    )
    _commit(tmp_path, "second neutral commit", "omniagentos/alpha/more.py")
    assert is_stale(tmp_path, archi) is False, (
        "chain length alone is not staleness — every commit here is map-neutral"
    )


def test_architecture_surface_commit_after_refresh_is_stale(tmp_path: Path) -> None:
    """The redesign's boundary: a successor touching what the map DESCRIBES
    still invalidates the stamp even when the stamped quantities are unchanged."""
    _build_fake_repo(tmp_path)
    _init_git(tmp_path)
    archi = tmp_path / "ARCHI.md"
    archi.write_text("# ARCHI.md\n", encoding="utf-8")
    stamp_archi(tmp_path, archi)
    _commit(tmp_path, "archdocs refresh", "ARCHI.md")
    assert is_stale(tmp_path, archi) is False

    # Same route COUNT, different route PATH: the live numeric gates cannot see
    # it, so the path-class rule must.
    widgets = tmp_path / "omniagentos" / "api" / "routes" / "widgets.py"
    widgets.write_text(
        widgets.read_text(encoding="utf-8").replace('"/list"', '"/listing"'),
        encoding="utf-8",
    )
    _commit(tmp_path, "rename route path", "omniagentos/api/routes/widgets.py")
    assert is_stale(tmp_path, archi) is True, (
        "a route-surface commit must invalidate the stamp even at equal count"
    )


def test_launcher_commit_after_refresh_is_stale(tmp_path: Path) -> None:
    """The generator parses the launcher (ports, env flags) into the map — a
    launcher-only commit is an architecture-surface commit, not a neutral one
    (grok second-lens finding on the chain redesign)."""
    _build_fake_repo(tmp_path)
    (tmp_path / "scripts").mkdir(exist_ok=True)
    launcher = tmp_path / "scripts" / "launch-omniagentos.sh"
    launcher.write_text("API_PORT=8788\n", encoding="utf-8")
    _init_git(tmp_path)
    archi = tmp_path / "ARCHI.md"
    archi.write_text("# ARCHI.md\n", encoding="utf-8")
    stamp_archi(tmp_path, archi)
    _commit(tmp_path, "archdocs refresh", "ARCHI.md")

    launcher.write_text("API_PORT=9999\n", encoding="utf-8")
    _commit(tmp_path, "move the api port", "scripts/launch-omniagentos.sh")
    assert is_stale(tmp_path, archi) is True, (
        "a launcher commit must invalidate the stamp — the map renders its ports"
    )


def test_new_package_after_refresh_is_stale(tmp_path: Path) -> None:
    """Adding a top-level package changes the packages inventory block."""
    _build_fake_repo(tmp_path)
    _init_git(tmp_path)
    archi = tmp_path / "ARCHI.md"
    archi.write_text("# ARCHI.md\n", encoding="utf-8")
    stamp_archi(tmp_path, archi)
    _commit(tmp_path, "archdocs refresh", "ARCHI.md")

    (tmp_path / "omniagentos" / "gamma").mkdir()
    (tmp_path / "omniagentos" / "gamma" / "__init__.py").write_text("", encoding="utf-8")
    _commit(tmp_path, "new package", "omniagentos/gamma/__init__.py")
    assert is_stale(tmp_path, archi) is True, (
        "a new top-level package must invalidate the stamp"
    )


def test_oracle_hand_edit_after_neutral_chain_is_stale(tmp_path: Path) -> None:
    """Only the chain's FIRST commit may touch the oracles, and only in the
    generator's own shape — a later ARCHI.md edit never inherits freshness."""
    _build_fake_repo(tmp_path)
    _init_git(tmp_path)
    archi = tmp_path / "ARCHI.md"
    archi.write_text("# ARCHI.md\n", encoding="utf-8")
    stamp_archi(tmp_path, archi)
    _commit(tmp_path, "archdocs refresh", "ARCHI.md")

    (tmp_path / "omniagentos" / "alpha" / "feature.py").write_text(
        "ENABLED = True\n", encoding="utf-8"
    )
    _commit(tmp_path, "neutral code", "omniagentos/alpha/feature.py")
    assert is_stale(tmp_path, archi) is False

    archi.write_text(
        archi.read_text(encoding="utf-8") + "\nhand-edited narrative\n", encoding="utf-8"
    )
    _commit(tmp_path, "hand edit", "ARCHI.md")
    assert is_stale(tmp_path, archi) is True, (
        "an oracle edit later in the chain must not read fresh"
    )


def _merge_branch(root: Path, branch: str) -> str:
    """No-ff merge ``branch`` into the current branch; return the merge sha."""
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=test@example.com",
            "-c",
            "user.name=Test",
            "merge",
            "--no-ff",
            "-q",
            "-m",
            f"trial of {branch}",
            branch,
        ],
        cwd=str(root),
        check=True,
    )
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(root),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_trial_checkout_of_fresh_mainline_is_fresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CI's synthetic two-parent HEAD stays fresh when the oracles came
    unchanged from a mainline commit the stamp accepts — the shape every
    gate-escalated candidate run has, which the plain head equality can
    never satisfy."""
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    _build_fake_repo(tmp_path)
    _init_git(tmp_path)
    archi = tmp_path / "ARCHI.md"
    archi.write_text("# ARCHI.md\n", encoding="utf-8")
    stamp_archi(tmp_path, archi)
    _commit(tmp_path, "archdocs refresh", "ARCHI.md")

    subprocess.run(["git", "checkout", "-q", "-b", "candidate"], cwd=str(tmp_path), check=True)
    (tmp_path / "omniagentos" / "alpha" / "feature.py").write_text(
        "ENABLED = True\n", encoding="utf-8"
    )
    _commit(tmp_path, "candidate code", "omniagentos/alpha/feature.py")
    subprocess.run(["git", "checkout", "-q", "-"], cwd=str(tmp_path), check=True)
    _merge_branch(tmp_path, "candidate")

    assert is_stale(tmp_path, archi) is False, (
        "a two-parent trial HEAD whose oracles are byte-identical to the fresh "
        "mainline parent must not read stale — the numeric drift gates still "
        "run against the merged tree"
    )


def test_local_two_parent_commit_is_stale_outside_ci(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Parent order is the only thing separating CI's trial shape from an
    operator's local ``pull.rebase=false`` result, and git alone cannot tell
    them apart — so outside GITHUB_ACTIONS the exact commit topology that is
    fresh in CI keeps the strict pre-existing answer: stale (review F3)."""
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    _build_fake_repo(tmp_path)
    _init_git(tmp_path)
    archi = tmp_path / "ARCHI.md"
    archi.write_text("# ARCHI.md\n", encoding="utf-8")
    stamp_archi(tmp_path, archi)
    _commit(tmp_path, "archdocs refresh", "ARCHI.md")

    subprocess.run(["git", "checkout", "-q", "-b", "local"], cwd=str(tmp_path), check=True)
    (tmp_path / "omniagentos" / "alpha" / "feature.py").write_text(
        "ENABLED = True\n", encoding="utf-8"
    )
    _commit(tmp_path, "local code", "omniagentos/alpha/feature.py")
    subprocess.run(["git", "checkout", "-q", "-"], cwd=str(tmp_path), check=True)
    _merge_branch(tmp_path, "local")

    assert is_stale(tmp_path, archi) is True


def test_trial_checkout_branch_side_oracle_edit_is_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A branch that edited ARCHI.md never inherits mainline freshness."""
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    _build_fake_repo(tmp_path)
    _init_git(tmp_path)
    archi = tmp_path / "ARCHI.md"
    archi.write_text("# ARCHI.md\n", encoding="utf-8")
    stamp_archi(tmp_path, archi)
    _commit(tmp_path, "archdocs refresh", "ARCHI.md")

    subprocess.run(["git", "checkout", "-q", "-b", "tamper"], cwd=str(tmp_path), check=True)
    archi.write_text(archi.read_text(encoding="utf-8") + "\nbranch edit\n", encoding="utf-8")
    _commit(tmp_path, "branch oracle edit", "ARCHI.md")
    subprocess.run(["git", "checkout", "-q", "-"], cwd=str(tmp_path), check=True)
    _merge_branch(tmp_path, "tamper")

    assert is_stale(tmp_path, archi) is True


def test_trial_checkout_of_stale_mainline_is_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mainline drift is not laundered by the merge: when the mainline parent
    carries ARCHITECTURE-SURFACE drift past the stamp (here: a route path
    rename the numeric gates cannot see), the trial HEAD is stale. (Map-neutral
    mainline successors are accepted since the 2026-08-15 chain redesign —
    that acceptance is pinned by test_trial_checkout_of_fresh_mainline_is_fresh.)"""
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    _build_fake_repo(tmp_path)
    _init_git(tmp_path)
    archi = tmp_path / "ARCHI.md"
    archi.write_text("# ARCHI.md\n", encoding="utf-8")
    stamp_archi(tmp_path, archi)
    _commit(tmp_path, "archdocs refresh", "ARCHI.md")
    widgets = tmp_path / "omniagentos" / "api" / "routes" / "widgets.py"
    widgets.write_text(
        widgets.read_text(encoding="utf-8").replace('"/list"', '"/listing"'),
        encoding="utf-8",
    )
    _commit(tmp_path, "mainline route drift after refresh", "omniagentos/api/routes/widgets.py")

    subprocess.run(["git", "checkout", "-q", "-b", "cand2"], cwd=str(tmp_path), check=True)
    (tmp_path / "omniagentos" / "alpha" / "feature.py").write_text(
        "ENABLED = True\n", encoding="utf-8"
    )
    _commit(tmp_path, "candidate code", "omniagentos/alpha/feature.py")
    subprocess.run(["git", "checkout", "-q", "-"], cwd=str(tmp_path), check=True)
    _merge_branch(tmp_path, "cand2")

    assert is_stale(tmp_path, archi) is True


def test_mixed_oracle_and_code_commit_is_stale(tmp_path: Path) -> None:
    """An archdocs-looking commit cannot smuggle a non-oracle path."""
    _build_fake_repo(tmp_path)
    _init_git(tmp_path)
    archi = tmp_path / "ARCHI.md"
    archi.write_text("# ARCHI.md\n", encoding="utf-8")
    stamp_archi(tmp_path, archi)
    (tmp_path / "README-refresh-test.md").write_text("not an oracle\n", encoding="utf-8")
    _commit(tmp_path, "mixed refresh", "ARCHI.md", "README-refresh-test.md")

    assert is_stale(tmp_path, archi) is True


def test_owned_commit_without_stamp_update_is_stale(tmp_path: Path) -> None:
    """An owned-path commit is not a refresh unless it commits the stamped doc."""
    _build_fake_repo(tmp_path)
    _init_git(tmp_path)
    archi = tmp_path / "ARCHI.md"
    archi.write_text("# ARCHI.md\n", encoding="utf-8")
    stamp_archi(tmp_path, archi)
    _commit(tmp_path, "stamp source", "ARCHI.md")

    system_map = tmp_path / "docs" / "architecture" / "system-map.md"
    system_map.parent.mkdir(parents=True)
    system_map.write_text("# map\n", encoding="utf-8")
    _commit(tmp_path, "map only", "docs/architecture/system-map.md")

    assert is_stale(tmp_path, archi) is True


def test_regenerate_recomputes_present_but_stale_stamp(tmp_path: Path) -> None:
    """THE FIX (generate.py:621-623 and render_archi): a present-but-STALE stamp
    must be recomputed, not carried forward unchanged.

    Before the fix, both `render_archi()`'s ARCHI.md header and
    `emit_archi_json()`'s ARCHI.json stamp object only recomputed when the
    embedded stamp was ABSENT — a present stamp naming a stale git HEAD (here:
    two commits behind, NOT the legitimate one-commit owned-refresh-successor)
    was reused verbatim. The sanctioned regeneration path
    (`python -m omniagentos.archdocs.generate`, exercised here via
    `regenerate_archi()`, its library entry point) must leave the doc no
    longer stale.
    """
    _build_fake_repo(tmp_path, n_migrations=2)
    _init_git(tmp_path)
    archi = tmp_path / "ARCHI.md"
    launchd_dir = _build_fake_launchd(tmp_path)
    archi.write_text("# ARCHI.md\n## Notes (human)\n", encoding="utf-8")
    stamp_archi(tmp_path, archi)
    _commit(tmp_path, "stamp source", "ARCHI.md")

    # A non-oracle code commit lands afterward — this is NOT the legitimate
    # one-commit-owned-successor exception, so the stamp is genuinely stale.
    (tmp_path / "omniagentos" / "gamma").mkdir()
    (tmp_path / "omniagentos" / "gamma" / "__init__.py").write_text("", encoding="utf-8")
    _commit(tmp_path, "code lands", "omniagentos/gamma/__init__.py")
    assert is_stale(tmp_path, archi) is True, "precondition: stamp must be stale before regen"
    stale_stamp = parse_stamp_comment(archi.read_text(encoding="utf-8"))
    assert stale_stamp is not None

    regenerated = regenerate_archi(tmp_path, archi_path=archi, launchd_dir=launchd_dir)
    fresh_stamp = parse_stamp_comment(regenerated)

    assert fresh_stamp is not None
    assert fresh_stamp.git_head != stale_stamp.git_head, (
        "regeneration must recompute a present-but-stale stamp, not carry it forward"
    )
    assert is_stale(tmp_path, archi) is False, (
        "the sanctioned regeneration path must leave the doc fresh, not silently still stale"
    )


def test_regenerate_computes_stamp_when_absent_unchanged_behaviour(tmp_path: Path) -> None:
    """Regression guard: the pre-existing absent-stamp -> compute path must be
    unchanged by the stale-stamp fix. Bootstrapping ARCHI.md from nothing (no
    prior stamp at all) must still produce a fresh, non-stale, computed stamp.
    """
    _build_fake_repo(tmp_path, n_migrations=2)
    _init_git(tmp_path)
    archi = tmp_path / "ARCHI.md"
    launchd_dir = _build_fake_launchd(tmp_path)
    assert not archi.exists()

    regenerated = regenerate_archi(tmp_path, archi_path=archi, launchd_dir=launchd_dir)

    stamp = parse_stamp_comment(regenerated)
    assert stamp is not None, "absent stamp must still result in a computed stamp"
    live = compute_stamp(tmp_path)
    assert stamp.git_head == live.git_head
    assert stamp.max_migration == live.max_migration
    assert stamp.route_count == live.route_count
    assert is_stale(tmp_path, archi) is False


def test_owned_refresh_successor_stamp_is_not_rewritten_by_regenerate(tmp_path: Path) -> None:
    """CRITICAL CONSTRAINT regression guard: recomputing on stale must not make a
    legitimately-fresh parent-naming stamp (the archi-morning owned-refresh-
    successor exception `is_stale()`/`is_stamp_stale()` already encode) read as
    stale and get needlessly rewritten by `render_archi()`.
    """
    _build_fake_repo(tmp_path, n_migrations=2)
    _init_git(tmp_path)
    archi = tmp_path / "ARCHI.md"
    launchd_dir = _build_fake_launchd(tmp_path)
    archi.write_text("# ARCHI.md\n## Notes (human)\n", encoding="utf-8")
    source = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    stamp_archi(tmp_path, archi)
    stored_before = parse_stamp_comment(archi.read_text(encoding="utf-8"))
    assert stored_before is not None
    assert stored_before.git_head == source
    _commit(tmp_path, "archdocs refresh", "ARCHI.md")

    assert is_stale(tmp_path, archi) is False, (
        "precondition: one direct owned-only successor commit must be fresh"
    )

    regenerate_archi(tmp_path, archi_path=archi, launchd_dir=launchd_dir)
    after_stamp = parse_stamp_comment(archi.read_text(encoding="utf-8"))

    assert after_stamp == stored_before, (
        "a legitimately-fresh parent-naming stamp must not be recomputed/rewritten"
    )


def test_regenerate_archi_md_and_json_stamps_agree(tmp_path: Path) -> None:
    """ARCHI.md and ARCHI.json must not drift apart: regeneration must derive
    ARCHI.json's stamp from the SAME freshly-spliced stamp written into
    ARCHI.md (one canonical `compute_stamp()` call per regeneration), not
    compute a second, independently-timestamped one.
    """
    _build_fake_repo(tmp_path, n_migrations=2)
    _init_git(tmp_path)
    archi = tmp_path / "ARCHI.md"
    launchd_dir = _build_fake_launchd(tmp_path)
    archi.write_text("# ARCHI.md\n## Notes (human)\n", encoding="utf-8")
    stamp_archi(tmp_path, archi)
    _commit(tmp_path, "stamp source", "ARCHI.md")

    # Land a code commit so the stamp goes genuinely stale (not the owned-
    # refresh-successor exception).
    (tmp_path / "omniagentos" / "delta").mkdir()
    (tmp_path / "omniagentos" / "delta" / "__init__.py").write_text("", encoding="utf-8")
    _commit(tmp_path, "code lands", "omniagentos/delta/__init__.py")
    assert is_stale(tmp_path, archi) is True

    regenerate_archi(tmp_path, archi_path=archi, launchd_dir=launchd_dir)

    md_stamp = parse_stamp_comment(archi.read_text(encoding="utf-8"))
    json_data = json.loads((tmp_path / "ARCHI.json").read_text(encoding="utf-8"))
    json_stamp = json_data["stamp"]

    assert md_stamp is not None
    assert md_stamp.git_head == json_stamp["git_head"]
    assert md_stamp.max_migration == json_stamp["max_migration"]
    assert md_stamp.route_count == json_stamp["route_count"]
    assert md_stamp.generated_at == json_stamp["generated_at"], (
        "ARCHI.md and ARCHI.json must share exactly one recomputed stamp, "
        "not two independently-timestamped ones"
    )
    assert is_stale(tmp_path, archi) is False


def test_owned_refresh_tamper_and_inventory_drift_fail_closed(tmp_path: Path) -> None:
    """Tampering plus route or migration drift stays stale after a valid refresh."""
    _build_fake_repo(tmp_path, n_migrations=2)
    _init_git(tmp_path)
    archi = tmp_path / "ARCHI.md"
    archi.write_text("# ARCHI.md\n", encoding="utf-8")
    stamp_archi(tmp_path, archi)
    _commit(tmp_path, "archdocs refresh", "ARCHI.md")
    committed_content = archi.read_text(encoding="utf-8")
    assert is_stale(tmp_path, archi) is False

    archi.write_text(committed_content + "\ntampered\n", encoding="utf-8")
    assert is_stale(tmp_path, archi) is True, "dirty owned oracle must invalidate freshness"
    archi.write_text(committed_content, encoding="utf-8")
    assert is_stale(tmp_path, archi) is False

    routes_file = tmp_path / "omniagentos" / "api" / "routes" / "widgets.py"
    routes_original = routes_file.read_text(encoding="utf-8")
    routes_file.write_text(
        routes_original + '\n@router.delete("/drift")\ndef drift(): ...\n',
        encoding="utf-8",
    )
    assert is_stale(tmp_path, archi) is True, "route drift must still fail closed"
    routes_file.write_text(routes_original, encoding="utf-8")
    assert is_stale(tmp_path, archi) is False

    migration = tmp_path / "omniagentos" / "db" / "migrations" / "003_drift.sql"
    migration.write_text("-- drift\n", encoding="utf-8")
    assert is_stale(tmp_path, archi) is True, "migration drift must still fail closed"


def test_staleness_flips_after_synthetic_migration_bump(tmp_path: Path) -> None:
    _build_fake_repo(tmp_path, n_migrations=2)
    _init_git(tmp_path)
    archi = tmp_path / "ARCHI.md"
    archi.write_text("# ARCHI.md\n\n## Notes (human)\n", encoding="utf-8")

    stamp_archi(tmp_path, archi)
    assert is_stale(tmp_path, archi) is False, "freshly stamped doc must not be stale"

    # Synthetic change: a new migration lands (max_migration increases) without
    # the doc being restamped.
    _build_fake_repo(tmp_path, n_migrations=4)
    assert is_stale(tmp_path, archi) is True, "stale after live migration count diverges from stamp"

    # Re-stamping brings it current again.
    stamp_archi(tmp_path, archi)
    assert is_stale(tmp_path, archi) is False


def test_staleness_flips_after_route_count_change(tmp_path: Path) -> None:
    _build_fake_repo(tmp_path)
    _init_git(tmp_path)
    archi = tmp_path / "ARCHI.md"
    archi.write_text("# ARCHI.md\n\n## Notes (human)\n", encoding="utf-8")
    stamp_archi(tmp_path, archi)
    assert is_stale(tmp_path, archi) is False

    # Add a new route — route_count diverges from the stamped value.
    routes_file = tmp_path / "omniagentos" / "api" / "routes" / "widgets.py"
    routes_file.write_text(
        routes_file.read_text(encoding="utf-8")
        + '\n@router.delete("/gone")\ndef delete_widget():\n    ...\n',
        encoding="utf-8",
    )
    assert is_stale(tmp_path, archi) is True


def test_unmeasurable_git_head_is_stale_not_fresh(tmp_path: Path) -> None:
    """unknown git HEAD must not report 'fresh' — cannot measure ⇒ stale.

    Counterfeit: treating stored==live when both are the sentinel ``unknown``
    as a successful match (zero-work / unmeasurable run scored healthy), OR
    relying on a different fail-close (absent inventory) to mask a disabled
    git-HEAD guard.

    Binding requirement: inventory sources MUST be present and measured-empty
    so that ``if False and (stored.git_head == "unknown" or ...)`` alone would
    make this test fail (live stamp matches stored zeros without the HEAD
    guard). This is independent of the absent-inventory fail-close.
    """
    # Measured-empty inventory present — isolates the git-HEAD fail-close.
    (tmp_path / "omniagentos" / "api" / "routes").mkdir(parents=True)
    (tmp_path / "omniagentos" / "db" / "migrations").mkdir(parents=True)
    archi = tmp_path / "ARCHI.md"
    # No .git directory: compute_stamp().git_head is the sentinel "unknown".
    stamp = compute_stamp(tmp_path)
    assert stamp.git_head == "unknown"
    assert stamp.max_migration == 0
    assert stamp.route_count == 0
    assert scan_routes(tmp_path) == []
    assert scan_migrations(tmp_path) == []
    archi.write_text(render_stamp_comment(stamp) + "\n# ARCHI.md\n", encoding="utf-8")
    assert is_stale(tmp_path, archi) is True, (
        "unknown git HEAD must fail-close as stale even when inventory is "
        "measured-empty and stamp numerics match (cannot measure ⇒ not fresh)"
    )


def test_is_stale_fail_closes_when_stamp_numerics_unknown(tmp_path: Path) -> None:
    """Matching unmeasurable stamp numerics must be stale, not fresh.

    Defect class: non-result presented as favourable result. When both stored
    and live stamp numerics are ``None`` (stamp field ``unknown`` / absent
    inventory), the final ``!=`` comparison is ``None != None`` → False and
    would report *fresh* without an explicit fail-close.

    Binding requirement: inventory sources MUST be absent (so live numerics
    are None) and the embedded stamp MUST already say ``unknown`` for both
    numeric fields, with a measurable matching git HEAD. Then:
      ``if False and (stored.max_migration is None or live.max_migration ...)``
    alone makes this test fail (final comparison matches unknowns as equal).

    Counterfeit: disable the nullable-numeric guard; matching unknowns look
    fresh. A redundant second ``scan_* is None`` rescan is not required and
    must not be the only binding — this test isolates the stamp-numeric gate.
    """
    from omniagentos.archdocs.generate import git_head

    # No routes/migrations dirs → compute_stamp numerics are None (unmeasurable).
    # Measurable git HEAD so the unknown-HEAD guard cannot mask this path.
    _init_git(tmp_path)
    head = git_head(tmp_path)
    assert head != "unknown"
    assert scan_routes(tmp_path) is None
    assert scan_migrations(tmp_path) is None
    live = compute_stamp(tmp_path)
    assert live.max_migration is None
    assert live.route_count is None
    assert live.git_head == head

    archi = tmp_path / "ARCHI.md"
    # Stored stamp: same HEAD, unknown numerics — without fail-close this matches live.
    archi.write_text(
        f"<!-- archdocs:stamp git_head={head} max_migration=unknown "
        f"route_count=unknown generated_at=2026-07-25T00:00:00Z -->\n"
        "# ARCHI.md\n",
        encoding="utf-8",
    )
    assert is_stale(tmp_path, archi) is True, (
        "matching unknown stamp numerics must fail-close as stale "
        "(None==None must not report fresh)"
    )


def test_is_stale_fail_closes_when_inventory_source_absent(tmp_path: Path) -> None:
    """Absent routes/migrations after a measured stamp must report stale.

    Stamp with *measured-empty* sources (dirs present, counts 0), then remove a
    source directory. Live stamp numerics become ``None`` (unmeasurable); the
    nullable-numeric fail-close (and/or ``0 != None``) must report stale.

    Note: after compute_stamp became three-valued, ``0 != None`` alone also
    catches this path — the binding isolation for the nullable-numeric guard
    itself is ``test_is_stale_fail_closes_when_stamp_numerics_unknown``.
    """
    import shutil

    # Measured-empty inventory: dirs exist, no routes / no migrations.
    (tmp_path / "omniagentos" / "api" / "routes").mkdir(parents=True)
    (tmp_path / "omniagentos" / "db" / "migrations").mkdir(parents=True)
    _init_git(tmp_path)
    archi = tmp_path / "ARCHI.md"
    archi.write_text("# ARCHI.md\n\n## Notes (human)\n", encoding="utf-8")

    stamp_archi(tmp_path, archi)
    assert is_stale(tmp_path, archi) is False, "measured-empty inventory must stamp fresh"

    # Withhold the routes source after stamping — live numeric becomes None.
    shutil.rmtree(tmp_path / "omniagentos" / "api" / "routes")
    assert scan_routes(tmp_path) is None
    assert compute_stamp(tmp_path).route_count is None
    assert is_stale(tmp_path, archi) is True, (
        "absent routes source must fail-close as stale, not match stamp zeros as fresh"
    )

    # Restore routes (empty), remove migrations — same class on the other inventory.
    (tmp_path / "omniagentos" / "api" / "routes").mkdir(parents=True)
    assert is_stale(tmp_path, archi) is False
    shutil.rmtree(tmp_path / "omniagentos" / "db" / "migrations")
    assert scan_migrations(tmp_path) is None
    assert compute_stamp(tmp_path).max_migration is None
    assert is_stale(tmp_path, archi) is True, (
        "absent migrations source must fail-close as stale, not match stamp zeros as fresh"
    )


def test_compute_stamp_absent_migrations_not_measured_zero(tmp_path: Path) -> None:
    """Absent migrations must stamp max_migration=None, not favourable 0.

    Defect class: three-valued scan consumed with bare truthiness so None and
    measured-empty [] both become stamp max_migration 0 — the value
    ``emit_archi_json`` publishes. A later ``is_stale`` guard cannot bind that
    published stamp field.

    Counterfeit (must fail this test):
        max_migration = migrations[-1][0] if migrations else 0
    That collapses absent ``None`` into the same 0 as measured-empty ``[]``.
    """
    from omniagentos.archdocs.generate import emit_archi_json

    # Measured-empty migrations + present empty routes.
    empty = tmp_path / "empty_migs"
    (empty / "omniagentos" / "api" / "routes").mkdir(parents=True)
    (empty / "omniagentos" / "db" / "migrations").mkdir(parents=True)

    # Absent migrations; routes present-empty so only migration field is unbound.
    absent = tmp_path / "absent_migs"
    (absent / "omniagentos" / "api" / "routes").mkdir(parents=True)
    # no omniagentos/db/migrations

    assert scan_migrations(empty) == []
    assert scan_migrations(absent) is None

    stamp_empty = compute_stamp(empty)
    stamp_absent = compute_stamp(absent)
    assert stamp_empty.max_migration == 0, "measured-empty migrations stamp as 0"
    assert stamp_absent.max_migration is None, (
        "absent migrations must stamp max_migration=None, not favourable 0 "
        "(bare-truthiness counterfeit collapses None and [] to 0)"
    )
    assert stamp_absent.max_migration != stamp_empty.max_migration

    # emit_archi_json publishes the stamp — bind the published value, not only is_stale.
    launchd = tmp_path / "launchd-empty"
    launchd.mkdir()
    json_absent = emit_archi_json(absent, launchd_dir=launchd)
    json_empty = emit_archi_json(empty, launchd_dir=launchd)
    assert json_empty["stamp"]["max_migration"] == 0
    assert json_absent["stamp"]["max_migration"] is None, (
        "ARCHI.json stamp.max_migration must be null when migrations unmeasurable"
    )
    assert json_absent["stamp"]["max_migration"] != json_empty["stamp"]["max_migration"]


def test_compute_stamp_absent_routes_not_measured_zero(tmp_path: Path) -> None:
    """Absent routes must stamp route_count=None, not favourable 0.

    Defect class: three-valued scan consumed with bare truthiness so None and
    measured-empty [] both become stamp route_count 0 — the value
    ``emit_archi_json`` publishes. A later ``is_stale`` guard cannot bind that
    published stamp field.

    Counterfeit (must fail this test):
        route_count = sum(1 for method, _, _ in (routes or []) if method != "UNPARSEABLE")
    That collapses absent ``None`` into the same 0 as measured-empty ``[]``.
    """
    from omniagentos.archdocs.generate import emit_archi_json

    empty = tmp_path / "empty_routes"
    (empty / "omniagentos" / "api" / "routes").mkdir(parents=True)
    (empty / "omniagentos" / "db" / "migrations").mkdir(parents=True)

    absent = tmp_path / "absent_routes"
    (absent / "omniagentos" / "db" / "migrations").mkdir(parents=True)
    # no omniagentos/api/routes

    assert scan_routes(empty) == []
    assert scan_routes(absent) is None

    stamp_empty = compute_stamp(empty)
    stamp_absent = compute_stamp(absent)
    assert stamp_empty.route_count == 0, "measured-empty routes stamp as 0"
    assert stamp_absent.route_count is None, (
        "absent routes must stamp route_count=None, not favourable 0 "
        "(bare-truthiness / (routes or []) counterfeit collapses None and [] to 0)"
    )
    assert stamp_absent.route_count != stamp_empty.route_count

    launchd = tmp_path / "launchd-empty"
    launchd.mkdir()
    json_absent = emit_archi_json(absent, launchd_dir=launchd)
    json_empty = emit_archi_json(empty, launchd_dir=launchd)
    assert json_empty["stamp"]["route_count"] == 0
    assert json_absent["stamp"]["route_count"] is None, (
        "ARCHI.json stamp.route_count must be null when routes unmeasurable"
    )
    assert json_absent["stamp"]["route_count"] != json_empty["stamp"]["route_count"]


def test_scan_routes_unreadable_dir_not_measured_empty(tmp_path: Path) -> None:
    """Unreadable routes directory must not render as measured zero routes.

    Defect class: missing/unreadable source reported identically to a genuinely
    empty one. ``Path.glob`` under a chmod-000 directory can yield [] silently,
    so renderers emit ``**0 routes across 0 modules.**``.

    Counterfeit: iterate ``routes_dir.glob("*.py")`` without an enumeration
    probe; unreadable dir and empty dir both become [].
    """
    import os
    import stat

    empty = tmp_path / "empty_routes_repo"
    (empty / "omniagentos" / "api" / "routes").mkdir(parents=True)

    unreadable = tmp_path / "unreadable_routes_repo"
    routes_dir = unreadable / "omniagentos" / "api" / "routes"
    routes_dir.mkdir(parents=True)
    (routes_dir / "widgets.py").write_text(
        "from fastapi import APIRouter\nrouter = APIRouter()\n",
        encoding="utf-8",
    )
    # Drop all permissions so enumeration fails (not measured-empty).
    os.chmod(routes_dir, 0)

    try:
        empty_routes = scan_routes(empty)
        unreadable_routes = scan_routes(unreadable)
        assert empty_routes == []
        assert unreadable_routes is None, (
            "unreadable routes directory must be None (unmeasurable), not [] "
            f"(got {unreadable_routes!r})"
        )
        assert unreadable_routes != empty_routes

        empty_block = render_routes_block(empty)
        unreadable_block = render_routes_block(unreadable)
        assert "**0 routes across 0 modules.**" in empty_block
        assert "**0 routes" not in unreadable_block
        assert "unavailable" in unreadable_block.lower()
    finally:
        os.chmod(routes_dir, stat.S_IRWXU)


def test_compute_stamp_excludes_unparseable_from_route_count(tmp_path: Path) -> None:
    """UNPARSEABLE route sentinels must not be counted as measured routes.

    Defect class: non-result presented as a favourable measured count.
    - UNPARSEABLE-only must stamp ``route_count=None`` (unmeasurable), never
      the same measured ``0`` as an empty routes directory.
    - Mixed inventory excludes UNPARSEABLE from the numeric count (not
      ``len(routes)``).

    Counterfeit A: ``route_count = len(routes)`` so UNPARSEABLE-only stamps 1
    and mixed over-counts.
    Counterfeit B: ``sum(... if method != "UNPARSEABLE")`` with no
    UNPARSEABLE-only → None branch so UNPARSEABLE-only stamps 0 ≡ empty.
    """
    from omniagentos.archdocs.generate import emit_archi_json

    routes_dir = tmp_path / "omniagentos" / "api" / "routes"
    routes_dir.mkdir(parents=True)
    (tmp_path / "omniagentos" / "db" / "migrations").mkdir(parents=True)
    _init_git(tmp_path)

    # Measured-empty control: empty routes dir stamps 0.
    stamp_empty = compute_stamp(tmp_path)
    assert stamp_empty.route_count == 0, "measured-empty routes stamp as 0"

    # Only unparseable — must not collapse to the same 0 as measured-empty.
    (routes_dir / "broken.py").write_bytes(b"\xff\xfe not valid utf-8")
    only_broken = scan_routes(tmp_path)
    assert only_broken is not None
    assert any(method == "UNPARSEABLE" for method, _, _ in only_broken)
    stamp_broken = compute_stamp(tmp_path)
    assert stamp_broken.route_count is None, (
        "UNPARSEABLE-only inventory must stamp route_count=None (unmeasurable), "
        "not favourable measured 0 like an empty routes directory"
    )
    assert stamp_broken.route_count != stamp_empty.route_count

    launchd = tmp_path / "launchd-empty"
    launchd.mkdir()
    json_broken = emit_archi_json(tmp_path, launchd_dir=launchd)
    assert json_broken["stamp"]["route_count"] is None, (
        "ARCHI.json stamp.route_count must be null for UNPARSEABLE-only inventory"
    )

    # Mixed: one real GET + one UNPARSEABLE — len(routes) would stamp 2.
    (routes_dir / "widgets.py").write_text(
        textwrap.dedent(
            """
            from fastapi import APIRouter
            router = APIRouter()

            @router.get("/list")
            def list_widgets():
                ...
            """
        ),
        encoding="utf-8",
    )
    mixed = scan_routes(tmp_path)
    assert mixed is not None
    assert sum(1 for m, _, _ in mixed if m != "UNPARSEABLE") == 1
    assert sum(1 for m, _, _ in mixed if m == "UNPARSEABLE") == 1
    stamp_mixed = compute_stamp(tmp_path)
    assert stamp_mixed.route_count == 1, (
        "UNPARSEABLE sentinel must be excluded from stamp route_count "
        f"(got {stamp_mixed.route_count}; len(routes) counterfeit would be {len(mixed)})"
    )


def test_is_stale_fail_closes_when_routes_unparseable(tmp_path: Path) -> None:
    """Unparseable route inventory must be stale, not fresh-via-zero-count.

    UNPARSEABLE-only stamps ``route_count=None`` (unmeasurable). Mixed
    inventory still has a numeric measurable count, so ``is_stale`` must also
    fail closed on any UNPARSEABLE sentinel — otherwise a partial count can
    match and report fresh.

    Counterfeit: fail-close only on ``scan_routes(...) is None`` (not on
    UNPARSEABLE entries), and/or stamp UNPARSEABLE-only as measured 0 so a
    doc stamped empty matches live "zero" after a module becomes unparseable.
    """
    routes_dir = tmp_path / "omniagentos" / "api" / "routes"
    routes_dir.mkdir(parents=True)
    (tmp_path / "omniagentos" / "db" / "migrations").mkdir(parents=True)
    _init_git(tmp_path)
    archi = tmp_path / "ARCHI.md"

    # Stamp while inventory is measured-empty (real zero), then break a module.
    archi.write_text("# ARCHI.md\n\n## Notes (human)\n", encoding="utf-8")
    stamp_archi(tmp_path, archi)
    assert is_stale(tmp_path, archi) is False
    assert compute_stamp(tmp_path).route_count == 0

    (routes_dir / "broken.py").write_bytes(b"\xff\xfe not valid utf-8")
    live_routes = scan_routes(tmp_path)
    assert live_routes is not None  # present dir — not the None fail-close path
    assert any(method == "UNPARSEABLE" for method, _, _ in live_routes)
    # UNPARSEABLE-only is unmeasurable, not a matching measured zero.
    assert compute_stamp(tmp_path).route_count is None
    assert is_stale(tmp_path, archi) is True, (
        "UNPARSEABLE route inventory must fail-close as stale "
        "(stamp route_count must not remain a counterfeit measured 0)"
    )

    # Also: stamped *while* unparseable must not report fresh on re-check.
    stamp_archi(tmp_path, archi)
    assert compute_stamp(tmp_path).route_count is None
    assert is_stale(tmp_path, archi) is True, (
        "doc stamped during unparseable inventory must not report fresh"
    )


def test_check_staleness_is_production_entry_for_reliability_audit(tmp_path: Path) -> None:
    """``check_staleness`` is the symbol reliability.audit imports.

    Counterfeit: export only ``is_stale`` (tests green) while production
    ``from omniagentos.archdocs.staleness import check_staleness`` fails and the
    audit stage reports ``{"skipped": "no_staleness_fn"}`` — built/tested but
    never wired. Return shape must be ``{"stale": bool}``; reliability calls
    with ZERO args (``staleness_fn()``), so the entry must accept that.
    """
    import importlib

    # Exact production import path used by reliability.audit._staleness_check
    mod = importlib.import_module("omniagentos.archdocs.staleness")
    assert hasattr(mod, "check_staleness"), (
        "reliability.audit imports check_staleness; missing symbol ⇒ "
        '{"skipped": "no_staleness_fn"} (built/tested, never wired)'
    )
    prod = mod.check_staleness

    # Missing ARCHI.md ⇒ stale (fail-closed); shape must be {"stale": bool}
    report = prod(repo_root=tmp_path)
    assert report == {"stale": True}

    # Zero-arg call — this is how audit invokes the production entry
    zero_arg = prod()
    assert set(zero_arg.keys()) == {"stale"}
    assert isinstance(zero_arg["stale"], bool)

    _build_fake_repo(tmp_path)
    _init_git(tmp_path)
    archi = tmp_path / "ARCHI.md"
    archi.write_text("# ARCHI.md\n\n## Notes (human)\n", encoding="utf-8")
    stamp_archi(tmp_path, archi)
    fresh = prod(repo_root=tmp_path, archi_path=archi)
    assert fresh == {"stale": False}

    # Material drift still surfaces
    _build_fake_repo(tmp_path, n_migrations=4)
    assert prod(repo_root=tmp_path, archi_path=archi) == {"stale": True}


# ---------------------------------------------------------------------------
# Acceptance: load_arch_context returns a relevant section for 'judge quorum' (§13.7)
# ---------------------------------------------------------------------------


def _build_fake_docs(root: Path) -> None:
    (root / "docs" / "architecture").mkdir(parents=True, exist_ok=True)
    (root / "ARCHI.md").write_text(
        "# ARCHI.md\n\n## Subsystems\n\nGeneral overview, nothing about judging here.\n"
        "\n## Notes (human)\n",
        encoding="utf-8",
    )
    (root / "docs" / "architecture" / "reliability.md").write_text(
        "# Reliability\n\n"
        "## Judge panel\n\n"
        "The judge panel requires a quorum of 3 distinct model families before an "
        "improvement can leave the judging stage. Quorum failures block the panel.\n\n"
        "## Sandbox\n\n"
        "The sandbox runs pytest against the proposal diff, unrelated to judging.\n\n"
        "## Notes (human)\n",
        encoding="utf-8",
    )
    (root / "docs" / "architecture" / "ui.md").write_text(
        "# UI\n\n## Dashboard\n\nRenders kanban cards and approval buttons.\n\n## Notes (human)\n",
        encoding="utf-8",
    )


def test_load_arch_context_ranks_relevant_section_first(tmp_path: Path) -> None:
    _build_fake_docs(tmp_path)
    context = load_arch_context(["judge", "quorum"], max_tokens=400, repo_root=str(tmp_path))
    assert context != ""
    assert "quorum" in context.lower()
    # The relevant section should be the FIRST included section (highest-ranked).
    first_heading_pos = context.index("Judge panel")
    dashboard_pos = context.find("Dashboard")
    assert dashboard_pos == -1 or first_heading_pos < dashboard_pos


def test_load_arch_context_no_match_returns_empty(tmp_path: Path) -> None:
    _build_fake_docs(tmp_path)
    context = load_arch_context(
        ["xylophone_nonsense_term"], max_tokens=400, repo_root=str(tmp_path)
    )
    assert context == ""


def test_load_arch_context_no_focus_terms_returns_overview(tmp_path: Path) -> None:
    _build_fake_docs(tmp_path)
    context = load_arch_context([], max_tokens=400, repo_root=str(tmp_path))
    assert context != ""


def test_load_arch_context_no_docs_returns_empty(tmp_path: Path) -> None:
    context = load_arch_context(["judge"], max_tokens=400, repo_root=str(tmp_path))
    assert context == ""


def test_load_arch_context_respects_token_budget(tmp_path: Path) -> None:
    _build_fake_docs(tmp_path)
    small = load_arch_context(["judge", "quorum"], max_tokens=10, repo_root=str(tmp_path))
    large = load_arch_context(["judge", "quorum"], max_tokens=2000, repo_root=str(tmp_path))
    assert len(small) <= len(large)


# ---------------------------------------------------------------------------
# update.py: Tier S safe write path
# ---------------------------------------------------------------------------


def test_is_owned_doc(tmp_path: Path) -> None:
    (tmp_path / "docs" / "architecture").mkdir(parents=True)
    assert is_owned_doc(tmp_path, tmp_path / "ARCHI.md") is True
    assert is_owned_doc(tmp_path, tmp_path / "docs" / "architecture" / "reliability.md") is True
    assert is_owned_doc(tmp_path, tmp_path / "omniagentos" / "contracts.py") is False
    assert is_owned_doc(tmp_path, tmp_path / "docs" / "architecture" / "notes.txt") is False


def test_is_owned_doc_rejects_archi_symlink_landing_outside_repo(tmp_path: Path) -> None:
    """Path security: a symlinked ARCHI.md must not admit writes outside the repo.

    Counterfeit: ``Path.resolve()`` equality (or ``str.startswith`` on roots) that
    treats "resolves to the same place as ARCHI.md" as owned — so a symlink
    ARCHI.md → /tmp/secret makes both the link and the secret path look owned.
    Real check: ``omniagentos.path_containment.inode_relative_parts``.
    """
    outside = tmp_path / "outside_secret.md"
    outside.write_text("keep me\n", encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "ARCHI.md").symlink_to(outside)

    assert is_owned_doc(repo, repo / "ARCHI.md") is False
    assert is_owned_doc(repo, outside) is False
    with pytest.raises(ArchdocsGuardError):
        apply_update(repo, repo / "ARCHI.md", "PWNED\n\n## Notes (human)\n")
    assert outside.read_text(encoding="utf-8") == "keep me\n"


def test_is_owned_doc_rejects_architecture_doc_symlink_outside(tmp_path: Path) -> None:
    """docs/architecture/ as a directory symlink must not count as owned.

    Binding counterfeit (Path.resolve under owned root): when
    ``docs/architecture`` itself is a symlink to an outside directory,
    ``(repo/docs/architecture).resolve()`` *is* that outside dir, so a
    ``target.resolve().relative_to(owned_root)`` check falsely admits
    ``outside/owned.md``. Leaf-file symlinks are rejected by both styles and
    do not fail-on-revert. Real check: ``inode_relative_parts`` against repo root.
    """
    outside_dir = tmp_path / "outside_arch"
    outside_dir.mkdir()
    outside_file = outside_dir / "owned.md"
    outside_file.write_text("orig\n", encoding="utf-8")

    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    (repo / "docs" / "architecture").symlink_to(outside_dir)
    target = repo / "docs" / "architecture" / "owned.md"

    assert is_owned_doc(repo, target) is False
    with pytest.raises(ArchdocsGuardError):
        apply_update(repo, target, "PWNED\n\n## Notes (human)\n")
    assert outside_file.read_text(encoding="utf-8") == "orig\n"


def test_apply_update_refuses_out_of_scope_path(tmp_path: Path) -> None:
    with pytest.raises(ArchdocsGuardError):
        apply_update(tmp_path, tmp_path / "omniagentos" / "contracts.py", "malicious content")


def test_apply_update_preserves_human_section_even_if_caller_tries_to_overwrite(
    tmp_path: Path,
) -> None:
    target = tmp_path / "docs" / "architecture" / "reliability.md"
    target.parent.mkdir(parents=True)
    target.write_text(
        "# Reliability\n\nOld narrative.\n\n## Notes (human)\n\nthe operator's private note.\n",
        encoding="utf-8",
    )

    result = apply_update(
        tmp_path,
        target,
        "# Reliability\n\nNEW narrative from the pipeline.\n\n"
        "## Notes (human)\n\npipeline trying to overwrite human notes!\n",
        actor="pipeline",
    )

    final = target.read_text(encoding="utf-8")
    assert "NEW narrative from the pipeline." in final
    assert "the operator's private note." in final
    assert "pipeline trying to overwrite human notes!" not in final
    assert result["human_preserved"] is True
    assert result["changed"] is True
    assert result["committed"] is False  # autocommit off by default


def test_apply_update_no_write_when_content_unchanged(tmp_path: Path) -> None:
    target = tmp_path / "ARCHI.md"
    target.write_text("body\n\n## Notes (human)\n", encoding="utf-8")
    result = apply_update(tmp_path, target, "body", actor="pipeline")
    assert result["changed"] is False


def test_apply_update_autocommit_flag_gated_and_bot_identity(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=str(tmp_path), check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=str(tmp_path), check=True
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(tmp_path), check=True)

    target = tmp_path / "ARCHI.md"
    target.write_text("initial\n\n## Notes (human)\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=str(tmp_path), check=True)

    result = apply_update(tmp_path, target, "updated body", actor="pipeline", autocommit=True)
    assert result["committed"] is True

    log = subprocess.run(
        ["git", "log", "-1", "--format=%an <%ae>%n%s"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        check=True,
    )
    assert "archdocs-bot <4580856+omniagentos-bot[bot]@users.noreply.github.com>" in log.stdout
    assert "archdocs: update ARCHI.md" in log.stdout


def test_apply_update_autocommit_off_by_default_leaves_index_untouched(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=str(tmp_path), check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=str(tmp_path), check=True
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(tmp_path), check=True)

    target = tmp_path / "ARCHI.md"
    target.write_text("initial\n\n## Notes (human)\n", encoding="utf-8")

    result = apply_update(tmp_path, target, "updated body", actor="pipeline")
    assert result["committed"] is False

    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        check=True,
    )
    assert status.stdout.strip() != ""  # file written, but nothing staged/committed


def test_scan_routes_empty_paths_recognized(tmp_path: Path) -> None:
    """Empty-path routes must be recognized to avoid modules dropping out of the inventory entirely.

    Modules like autonomy, banking, connections, models, revenue, and system_jobs
    were completely invisible (measured 0 routes) because their route decorators
    used empty paths, effectively making `scan_routes` report 271 routes across
    37 modules instead of the syntax-aware truth of 296 routes across 43 modules.

    Defect class: a negative mutation/counterfeit of the prior regex/parser behavior
    (`+` quantifier instead of `*` for the path) drops empty paths `""` and `''`.
    """
    _build_fake_repo(tmp_path)
    routes_dir = tmp_path / "omniagentos" / "api" / "routes"

    # Add modules matching the real issue with empty-path routes.
    (routes_dir / "autonomy.py").write_text(
        "@router.post('')\ndef auto(): ...\n@router.get('')\ndef g_auto(): ...\n",
        encoding="utf-8",
    )
    (routes_dir / "banking.py").write_text(
        '@router.get("")\ndef bank(): ...\n',
        encoding="utf-8",
    )
    (routes_dir / "connections.py").write_text(
        "@router.delete('')\ndef conn(): ...\n",
        encoding="utf-8",
    )
    (routes_dir / "models.py").write_text(
        '@router.patch("")\ndef mod(): ...\n',
        encoding="utf-8",
    )
    (routes_dir / "revenue.py").write_text(
        "@router.put('')\ndef rev(): ...\n",
        encoding="utf-8",
    )
    (routes_dir / "system_jobs.py").write_text(
        '@router.get("")\ndef sys(): ...\n',
        encoding="utf-8",
    )

    routes = scan_routes(tmp_path)
    assert routes is not None

    # Prove empty-path routes are recognized
    assert ("POST", "", "autonomy") in routes
    assert ("GET", "", "autonomy") in routes
    assert ("GET", "", "banking") in routes
    assert ("DELETE", "", "connections") in routes
    assert ("PATCH", "", "models") in routes
    assert ("PUT", "", "revenue") in routes
    assert ("GET", "", "system_jobs") in routes

    # Prove all six real modules become visible
    modules_found = {module for _, _, module in routes}
    assert "autonomy" in modules_found
    assert "banking" in modules_found
    assert "connections" in modules_found
    assert "models" in modules_found
    assert "revenue" in modules_found
    assert "system_jobs" in modules_found

    block = render_routes_block(tmp_path)
    # The scan_routes implementation sorts routes by (module, path, method).
    # Since path is "", the order for autonomy is GET "", POST "".
    assert '- `autonomy.py`: GET "", POST ""' in block
    assert "banking.py" in block
    assert not any(line.endswith(" ") for line in block.splitlines())

    # Negative mutation test: ensuring `+` counterfeit fails
    prior_regex = re.compile(
        r'@router\.(get|post|put|patch|delete)\(\s*["\']([^"\']+)["\']', re.MULTILINE
    )
    # The prior regex should NOT find empty routes
    text = (routes_dir / "autonomy.py").read_text(encoding="utf-8")
    assert not prior_regex.findall(text), "Counterfeit prior regex should fail to match empty paths"
    # But the real one does
    assert _ROUTE_DECORATOR_RE.findall(text)
