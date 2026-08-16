"""Deterministic migration-authority rail.

Proves the real shell phase:
1. Compares committed baseline (HEAD commit) to the git index only (must stage).
2. Allows unchanged trees or exactly one staged next version (baseline_head + 1).
3. Rejects duplicate N+1, gap N+2, stale/lower numbers, multi-add, delete/renumber,
   historical modification, nested paths, C-quoted special names, and malformed
   names for arbitrary heads N.
4. Path listing is independent of user core.quotePath.
5. Does not depend on core.hooksPath; works from nested cwd.
6. Explicit MIGRATION_AUTHORITY_BASE=HEAD^ contract for clean committed tips.
7. Fresh migrate and partial-then-upgrade paths produce equivalent normalized schemas.

Never mutates the product repository's omniagentos/db/migrations/ directory.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import sqlite3
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from omniagentos.db.migrate import _migration_files, migrate

ROOT = Path(__file__).resolve().parents[2]
PHASE_SCRIPT = ROOT / "scripts" / "git-hooks" / "check-migration-versions.sh"
LATEST_VERSION = max(version for version, _ in _migration_files())
MIG_DIR = Path("omniagentos") / "db" / "migrations"
_MIGRATION_NAME = re.compile(r"^(\d{3})_.*\.sql$")
REPAIR_096 = ROOT / "omniagentos" / "db" / "migrations" / "096_dag_moe_gating.sql"
REPAIR_RECORD = (
    ROOT / "omniagentos" / "db" / "migration_repairs" / "096_dag_moe_gating.json"
)
BROKEN_096 = ROOT / "tests" / "db" / "fixtures" / "096_dag_moe_gating_broken.sql"


def _run_phase(
    *args: str,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Invoke the explicit phase command (bash + script path; no hooksPath)."""
    cmd = ["bash", str(PHASE_SCRIPT), *args]
    return subprocess.run(
        cmd,
        cwd=str(cwd or ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=check,
    )


def _pad(n: int) -> str:
    return f"{n:03d}"


def _isolated_repo(tmp_path: Path, baseline_head: int, *, name: str = "repo") -> Path:
    """Throwaway git work tree with synthetic committed head N (and 001 if N!=1).

    Configurable baseline — never hard-codes the product tree's current head.
    """
    if baseline_head < 1:
        raise ValueError("baseline_head must be >= 1")

    repo = tmp_path / f"{name}-N{_pad(baseline_head)}"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "migration-authority@test.local")
    _git(repo, "config", "user.name", "Migration Authority Test")
    _git(repo, "config", "--unset-all", "core.hooksPath", check=False)
    assert _git(repo, "config", "--get", "core.hooksPath", check=False).returncode != 0

    migrations = repo / MIG_DIR
    migrations.mkdir(parents=True)
    if baseline_head != 1:
        (migrations / "001_init.sql").write_text("-- init\n", encoding="utf-8")
    (migrations / f"{_pad(baseline_head)}_head.sql").write_text(
        f"-- synthetic head {_pad(baseline_head)}\n",
        encoding="utf-8",
    )
    # Historical gap below head remains legal history when present.
    if baseline_head >= 10:
        gap = baseline_head // 2
        if gap not in {1, baseline_head}:
            (migrations / f"{_pad(gap)}_historical_gap.sql").write_text(
                "-- legal historical gap file\n",
                encoding="utf-8",
            )

    _git(repo, "add", str(MIG_DIR))
    _git(repo, "commit", "-m", f"seed baseline head {_pad(baseline_head)}")
    return repo


def _stage_new(repo: Path, version: int, slug: str = "claim") -> Path:
    path = repo / MIG_DIR / f"{_pad(version)}_{slug}.sql"
    path.write_text(f"-- staged {_pad(version)} {slug}\n", encoding="utf-8")
    _git(repo, "add", str(path.relative_to(repo)))
    return path


def _stage_relative(repo: Path, rel: str, body: str = "-- counterfeit\n") -> Path:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    _git(repo, "add", "-A", "--", str(MIG_DIR))
    return path


def _stage_filename(repo: Path, filename: str, body: str = "-- counterfeit\n") -> Path:
    path = repo / MIG_DIR / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    _git(repo, "add", "-A", "--", str(MIG_DIR))
    return path


def _repair_repo(
    tmp_path: Path,
    *,
    version: int = 96,
    original: Path = BROKEN_096,
    name: str = "repair",
) -> Path:
    """Throwaway authority repo whose historical migration can be repaired."""
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "migration-authority@test.local")
    _git(repo, "config", "user.name", "Migration Authority Test")
    migrations = repo / MIG_DIR
    migrations.mkdir(parents=True)
    shutil.copyfile(original, migrations / f"{version:03d}_dag_moe_gating.sql")
    _git(repo, "add", str(MIG_DIR))
    _git(repo, "commit", "-m", "seed historical migration")
    return repo


def _stage_exact_repair(repo: Path) -> None:
    target = repo / MIG_DIR / REPAIR_096.name
    record = repo / REPAIR_RECORD.relative_to(ROOT)
    record.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(REPAIR_096, target)
    shutil.copyfile(REPAIR_RECORD, record)
    _git(repo, "add", str(target.relative_to(repo)), str(record.relative_to(repo)))


def _assert_blocked(
    result: subprocess.CompletedProcess[str],
    *,
    baseline_head: int,
    needle: str | None = None,
) -> None:
    assert result.returncode == 1, (
        f"expected reject, got {result.returncode}\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    assert "BLOCKED:" in result.stderr
    assert f"baseline_head={baseline_head}" in result.stderr
    assert f"expected_next={_pad(baseline_head + 1)}" in result.stderr
    if needle is not None:
        assert needle in result.stderr


def _normalized_schema(db_path: Path) -> tuple[list[tuple], list[tuple]]:
    """Schema fingerprint excluding intentional temporal metadata (applied_at)."""
    connection = sqlite3.connect(str(db_path))
    try:
        master = connection.execute(
            """
            SELECT type, name, tbl_name, sql
            FROM sqlite_master
            WHERE name NOT LIKE 'sqlite_%'
            ORDER BY type, name, tbl_name
            """
        ).fetchall()
        versions = connection.execute(
            """
            SELECT version, checksum
            FROM schema_migrations
            ORDER BY version
            """
        ).fetchall()
        return master, versions
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# Phase command plumbing (hooksPath / cwd / product tree smoke)
# ---------------------------------------------------------------------------


def test_phase_command_exists_and_passes_on_clean_product_tree() -> None:
    assert PHASE_SCRIPT.is_file()
    result = _run_phase()
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""


def test_phase_command_from_nested_cwd_without_hooks_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline = 44
    repo = _isolated_repo(tmp_path, baseline, name="nested-cwd")
    nested = repo / "deep" / "cwd"
    nested.mkdir(parents=True)

    env = os.environ.copy()
    env.pop("GIT_CONFIG_COUNT", None)
    env.pop("GIT_CONFIG_PARAMETERS", None)
    env.pop("MIGRATION_AUTHORITY_BASE", None)
    monkeypatch.delenv("GIT_DIR", raising=False)
    monkeypatch.delenv("GIT_WORK_TREE", raising=False)

    result = _run_phase(cwd=nested, env=env)
    assert result.returncode == 0, result.stderr

    result2 = _run_phase(str(repo), cwd=tmp_path, env=env)
    assert result2.returncode == 0, result2.stderr

    hooks = _git(repo, "config", "--get", "core.hooksPath", check=False)
    assert hooks.returncode != 0


# ---------------------------------------------------------------------------
# Dynamic synthetic authority matrix (two distinct baseline heads)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("baseline_head", [17, 44])
def test_unchanged_baseline_passes(tmp_path: Path, baseline_head: int) -> None:
    repo = _isolated_repo(tmp_path, baseline_head, name="unchanged")
    result = _run_phase(str(repo), cwd=tmp_path)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("baseline_head", [17, 44])
def test_exactly_one_staged_next_passes(tmp_path: Path, baseline_head: int) -> None:
    repo = _isolated_repo(tmp_path, baseline_head, name="next-ok")
    _stage_new(repo, baseline_head + 1, "only_claim")
    result = _run_phase(str(repo), cwd=tmp_path)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("baseline_head", [17, 44])
def test_duplicate_next_is_rejected(tmp_path: Path, baseline_head: int) -> None:
    repo = _isolated_repo(tmp_path, baseline_head, name="dup-next")
    nxt = baseline_head + 1
    _stage_new(repo, nxt, "first")
    _stage_new(repo, nxt, "counterfeit")
    result = _run_phase(str(repo), cwd=tmp_path)
    _assert_blocked(result, baseline_head=baseline_head, needle="duplicate")
    assert str(nxt) in result.stderr


@pytest.mark.parametrize("baseline_head", [17, 44])
def test_gap_n_plus_2_is_rejected(tmp_path: Path, baseline_head: int) -> None:
    repo = _isolated_repo(tmp_path, baseline_head, name="gap")
    _stage_new(repo, baseline_head + 2, "skipped")
    result = _run_phase(str(repo), cwd=tmp_path)
    _assert_blocked(result, baseline_head=baseline_head, needle="not the next allocation")
    assert f"candidate={_pad(baseline_head + 2)}" in result.stderr


@pytest.mark.parametrize("baseline_head", [17, 44])
def test_stale_lower_unused_number_is_rejected(tmp_path: Path, baseline_head: int) -> None:
    repo = _isolated_repo(tmp_path, baseline_head, name="stale")
    committed = {int(p.name[:3]) for p in (repo / MIG_DIR).glob("*.sql")}
    stale = next(v for v in range(2, baseline_head) if v not in committed)
    _stage_new(repo, stale, "stale_lower")
    result = _run_phase(str(repo), cwd=tmp_path)
    _assert_blocked(result, baseline_head=baseline_head, needle="not the next allocation")
    assert f"candidate={_pad(stale)}" in result.stderr


@pytest.mark.parametrize("baseline_head", [17, 44])
def test_contiguous_new_sequential_files_accepted(tmp_path: Path, baseline_head: int) -> None:
    """Multiple contiguous new migrations are now accepted."""
    repo = _isolated_repo(tmp_path, baseline_head, name="multi")
    _stage_new(repo, baseline_head + 1, "a")
    _stage_new(repo, baseline_head + 2, "b")
    _stage_new(repo, baseline_head + 3, "c")
    result = _run_phase(str(repo), cwd=tmp_path)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("baseline_head", [17, 44])
def test_non_contiguous_new_files_rejected(tmp_path: Path, baseline_head: int) -> None:
    """Non-contiguous new migrations must still be rejected (gaps)."""
    repo = _isolated_repo(tmp_path, baseline_head, name="noncontiguous")
    _stage_new(repo, baseline_head + 1, "a")
    _stage_new(repo, baseline_head + 3, "c")  # Skip baseline_head + 2
    result = _run_phase(str(repo), cwd=tmp_path)
    _assert_blocked(result, baseline_head=baseline_head, needle="non-contiguous")


@pytest.mark.parametrize("baseline_head", [17, 44])
def test_committed_deletion_is_rejected(tmp_path: Path, baseline_head: int) -> None:
    repo = _isolated_repo(tmp_path, baseline_head, name="delete")
    head_path = repo / MIG_DIR / f"{_pad(baseline_head)}_head.sql"
    head_path.unlink()
    _git(repo, "add", "-u", str(MIG_DIR))
    result = _run_phase(str(repo), cwd=tmp_path)
    _assert_blocked(result, baseline_head=baseline_head, needle="deleted or renumbered")


@pytest.mark.parametrize("baseline_head", [17, 44])
def test_renumber_is_rejected(tmp_path: Path, baseline_head: int) -> None:
    repo = _isolated_repo(tmp_path, baseline_head, name="renumber")
    migrations = repo / MIG_DIR
    old = migrations / f"{_pad(baseline_head)}_head.sql"
    new = migrations / f"{_pad(baseline_head + 1)}_head.sql"
    old.rename(new)
    _git(repo, "add", "-A", str(MIG_DIR))
    result = _run_phase(str(repo), cwd=tmp_path)
    _assert_blocked(result, baseline_head=baseline_head, needle="deleted or renumbered")


@pytest.mark.parametrize("baseline_head", [17, 44])
def test_historical_modification_is_rejected(tmp_path: Path, baseline_head: int) -> None:
    repo = _isolated_repo(tmp_path, baseline_head, name="modify")
    head_path = repo / MIG_DIR / f"{_pad(baseline_head)}_head.sql"
    head_path.write_text(head_path.read_text(encoding="utf-8") + "-- edit\n", encoding="utf-8")
    _git(repo, "add", str(head_path.relative_to(repo)))
    result = _run_phase(str(repo), cwd=tmp_path)
    _assert_blocked(result, baseline_head=baseline_head, needle="historical migration modified")


def test_exact_migration_096_repair_is_the_only_allowed_historical_edit(
    tmp_path: Path,
) -> None:
    repo = _repair_repo(tmp_path, name="exact-repair")
    _stage_exact_repair(repo)

    result = _run_phase(str(repo), cwd=tmp_path)

    assert result.returncode == 0, result.stderr


def test_migration_096_repair_rejects_invalid_authority_record(
    tmp_path: Path,
) -> None:
    repo = _repair_repo(tmp_path, name="invalid-repair-record")
    _stage_exact_repair(repo)
    record = repo / REPAIR_RECORD.relative_to(ROOT)
    record.write_text(
        record.read_text(encoding="utf-8").replace(
            "6752fac84728d5ef31030f0755a39bc8cdb2bb5c87f5fb79ec2ae8df3ae94e8a",
            "f" * 64,
        ),
        encoding="utf-8",
    )
    _git(repo, "add", str(record.relative_to(repo)))

    result = _run_phase(str(repo), cwd=tmp_path)

    _assert_blocked(result, baseline_head=96, needle="repair authority record")


def test_migration_096_repair_rejects_wrong_old_hash(tmp_path: Path) -> None:
    wrong_old = tmp_path / "wrong-old.sql"
    wrong_old.write_text(BROKEN_096.read_text(encoding="utf-8") + "-- changed old\n")
    repo = _repair_repo(tmp_path, original=wrong_old, name="wrong-old-repair")
    target = repo / MIG_DIR / REPAIR_096.name
    shutil.copyfile(REPAIR_096, target)
    _git(repo, "add", str(target.relative_to(repo)))

    result = _run_phase(str(repo), cwd=tmp_path)

    _assert_blocked(result, baseline_head=96, needle="historical migration modified")


def test_migration_096_repair_rejects_wrong_new_hash(tmp_path: Path) -> None:
    repo = _repair_repo(tmp_path, name="wrong-new-repair")
    target = repo / MIG_DIR / REPAIR_096.name
    target.write_bytes(REPAIR_096.read_bytes() + b"\n-- changed new\n")
    _git(repo, "add", str(target.relative_to(repo)))

    result = _run_phase(str(repo), cwd=tmp_path)

    _assert_blocked(result, baseline_head=96, needle="historical migration modified")


def test_migration_096_repair_rejects_wrong_version(tmp_path: Path) -> None:
    repo = _repair_repo(tmp_path, version=95, name="wrong-version-repair")
    target = repo / MIG_DIR / "095_dag_moe_gating.sql"
    shutil.copyfile(REPAIR_096, target)
    _git(repo, "add", str(target.relative_to(repo)))

    result = _run_phase(str(repo), cwd=tmp_path)

    _assert_blocked(result, baseline_head=95, needle="historical migration modified")


def test_migration_096_repair_does_not_mask_second_historical_edit(
    tmp_path: Path,
) -> None:
    repo = _repair_repo(tmp_path, name="repair-plus-second-edit")
    other = repo / MIG_DIR / "095_other.sql"
    other.write_text("-- historical 095\n", encoding="utf-8")
    _git(repo, "add", str(other.relative_to(repo)))
    _git(repo, "commit", "-m", "add second historical migration")

    repaired = repo / MIG_DIR / REPAIR_096.name
    shutil.copyfile(REPAIR_096, repaired)
    other.write_text("-- illegal second edit\n", encoding="utf-8")
    _git(repo, "add", str(MIG_DIR))

    result = _run_phase(str(repo), cwd=tmp_path)

    _assert_blocked(result, baseline_head=96, needle="historical migration modified")
    assert "095_other.sql" in result.stderr


@pytest.mark.parametrize("baseline_head", [17, 44])
@pytest.mark.parametrize(
    ("rel_template", "kind"),
    [
        ("omniagentos/db/migrations/not_a_migration.sql", "nonnumeric"),
        ("omniagentos/db/migrations/18_short_numeric.sql", "short_numeric"),
        ("omniagentos/db/migrations/{next}_.sql", "empty_slug"),
        (
            "omniagentos/db/migrations/subdir/{next}_nested_valid_looking.sql",
            "nested",
        ),
    ],
    ids=["nonnumeric", "short_numeric", "empty_slug", "nested"],
)
def test_malformed_counterfeits_are_rejected(
    tmp_path: Path,
    baseline_head: int,
    rel_template: str,
    kind: str,
) -> None:
    """Permanent counterfeits that must not false-green under _version_of."""
    repo = _isolated_repo(tmp_path, baseline_head, name=f"malformed-{kind}")
    nxt = baseline_head + 1
    rel = rel_template.format(next=_pad(nxt))
    _stage_relative(repo, rel)
    result = _run_phase(str(repo), cwd=tmp_path)
    _assert_blocked(result, baseline_head=baseline_head, needle="malformed migration name")
    assert rel in result.stderr
    assert "candidate=<malformed>" in result.stderr
    assert "direct child" in result.stderr


@pytest.mark.parametrize("baseline_head", [17, 44])
@pytest.mark.parametrize("quote_path", ["true", "false"])
@pytest.mark.parametrize(
    ("kind", "filename_factory"),
    [
        ("quote", lambda n: f'{_pad(n + 2)}_bad"quote.sql'),
        ("backslash", lambda n: f"{_pad(n + 2)}_bad\\slash.sql"),
        ("non_ascii", lambda n: f"{_pad(n + 2)}_café.sql"),
        ("newline", lambda n: f"{_pad(n + 2)}_bad\nline.sql"),
        ("ascii_control_tab", lambda n: f"{_pad(n + 2)}_bad\tcontrol.sql"),
        ("normal_ascii_n_plus_2", lambda n: f"{_pad(n + 2)}_normal_ascii_gap.sql"),
    ],
    ids=[
        "quote",
        "backslash",
        "non_ascii",
        "newline",
        "ascii_control_tab",
        "normal_ascii_n_plus_2",
    ],
)
def test_special_n_plus_2_counterfeits_rejected_independent_of_quote_path(
    tmp_path: Path,
    baseline_head: int,
    quote_path: str,
    kind: str,
    filename_factory: Callable[[int], str],
) -> None:
    """Git C-quote / special-slug N+2 paths must never false-green.

    migrate.py accepts quote/backslash/non-ASCII names via Path.glob + regex;
    the phase must still evaluate or fail closed regardless of core.quotePath.
    """
    repo = _isolated_repo(tmp_path, baseline_head, name=f"special-{kind}-qp{quote_path}")
    _git(repo, "config", "core.quotePath", quote_path)
    filename = filename_factory(baseline_head)
    # Product engine would accept these names if they reached disk as direct children.
    assert _MIGRATION_NAME.fullmatch(filename) is not None or "\n" in filename or "\t" in filename
    _stage_filename(repo, filename)
    result = _run_phase(str(repo), cwd=tmp_path)

    # Phase uses command-local `git -c core.quotePath=false`, which overrides any
    # repository core.quotePath. Non-ASCII is therefore always emitted unquoted and
    # evaluated as an N+2 allocation miss. Quote/backslash/newline/tab remain
    # C-quoted even with quotePath=false and fail closed before allocation.
    if kind in {"quote", "backslash", "newline", "ascii_control_tab"}:
        expect_needle = "C-quoted path"
    else:
        # non_ascii and normal_ascii_n_plus_2 — evaluated, rejected as N+2.
        expect_needle = "not the next allocation"
    _assert_blocked(result, baseline_head=baseline_head, needle=expect_needle)


def test_untracked_next_is_ignored_until_staged(tmp_path: Path) -> None:
    """Index-only: untracked candidates are outside authority (must stage)."""
    baseline = 17
    repo = _isolated_repo(tmp_path, baseline, name="untracked")
    path = repo / MIG_DIR / f"{_pad(baseline + 2)}_untracked_gap.sql"
    path.write_text("-- not staged\n", encoding="utf-8")
    result = _run_phase(str(repo), cwd=tmp_path)
    assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# Explicit MIGRATION_AUTHORITY_BASE contract for clean committed tips
# ---------------------------------------------------------------------------


def test_explicit_base_head_parent_clean_tip_n_plus_1_and_n_plus_2(
    tmp_path: Path,
) -> None:
    """Clean tip needs MIGRATION_AUTHORITY_BASE=HEAD^; N+1 passes, N+2 rejects."""
    baseline = 23

    # Valid allocation committed as tip.
    ok_repo = _isolated_repo(tmp_path, baseline, name="base-ok")
    _stage_new(ok_repo, baseline + 1, "committed_next")
    _git(ok_repo, "commit", "-m", "allocate n+1")
    # Default HEAD on a clean tip is an unchanged self-baseline → pass.
    clean = _run_phase(str(ok_repo), cwd=tmp_path)
    assert clean.returncode == 0, clean.stderr
    env = os.environ.copy()
    env["MIGRATION_AUTHORITY_BASE"] = "HEAD^"
    ok = _run_phase(str(ok_repo), cwd=tmp_path, env=env)
    assert ok.returncode == 0, ok.stderr

    # Invalid N+2 committed as tip — must reject when base is parent.
    bad_repo = _isolated_repo(tmp_path, baseline, name="base-bad")
    _stage_new(bad_repo, baseline + 2, "committed_gap")
    _git(bad_repo, "commit", "-m", "illegal n+2")
    env_bad = os.environ.copy()
    env_bad["MIGRATION_AUTHORITY_BASE"] = "HEAD^"
    bad = _run_phase(str(bad_repo), cwd=tmp_path, env=env_bad)
    _assert_blocked(bad, baseline_head=baseline, needle="not the next allocation")
    assert f"candidate={_pad(baseline + 2)}" in bad.stderr


# ---------------------------------------------------------------------------
# Normalized fresh vs upgraded schema equivalence (real migrate engine)
# ---------------------------------------------------------------------------


def test_fresh_and_upgraded_schemas_are_equivalent_after_normalization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fresh empty→latest equals partial-apply→upgrade after metadata normalize."""
    packaged = _migration_files()
    assert LATEST_VERSION >= 2

    versions = [version for version, _ in packaged]
    mid = versions[len(versions) // 2]

    fresh_db = tmp_path / "fresh.db"
    upgraded_db = tmp_path / "upgraded.db"

    assert migrate(str(fresh_db)) == LATEST_VERSION

    partial = [(version, path) for version, path in packaged if version <= mid]
    monkeypatch.setattr("omniagentos.db.migrate._migration_files", lambda: partial)
    assert migrate(str(upgraded_db)) == mid

    monkeypatch.setattr("omniagentos.db.migrate._migration_files", lambda: packaged)
    assert migrate(str(upgraded_db)) == LATEST_VERSION

    fresh_master, fresh_versions = _normalized_schema(fresh_db)
    upgraded_master, upgraded_versions = _normalized_schema(upgraded_db)

    assert fresh_master == upgraded_master
    assert fresh_versions == upgraded_versions
    assert fresh_versions[-1][0] == LATEST_VERSION
    by_version = {int(version): checksum for version, checksum in fresh_versions}
    for version, path in packaged:
        assert by_version[version] == hashlib.sha256(path.read_bytes()).hexdigest()
