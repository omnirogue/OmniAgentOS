"""The architecture refresh must never mutate a non-main branch."""

from __future__ import annotations

import hashlib
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ARCHI_MORNING = REPO_ROOT / "scripts" / "archi-morning" / "archi-morning.sh"
OWNED = (
    "ARCHI.md",
    "ARCHI.json",
    "docs/architecture/system-map.mmd",
    "docs/architecture/system-map.md",
)


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _hashes(repo: Path) -> dict[str, str]:
    return {
        relative: hashlib.sha256((repo / relative).read_bytes()).hexdigest() for relative in OWNED
    }


@pytest.fixture
def non_main_archi_repo(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Archi Morning Test")
    _git(repo, "config", "user.email", "archi-morning@example.com")

    for relative in OWNED:
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"baseline {relative}\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "baseline architecture")
    _git(repo, "checkout", "-b", "scratch")

    script = repo / "scripts" / "archi-morning" / "archi-morning.sh"
    script.parent.mkdir(parents=True)
    shutil.copy2(ARCHI_MORNING, script)

    python = repo / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text(
        """#!/bin/sh
printf 'invoked\\n' >> var/fake-python.invoked
if [ "$1" = "-m" ] && [ "$2" = "omniagentos.archdocs.generate" ]; then
  printf 'mutated inventory\\n' > ARCHI.md
  printf '{"mutated": true}\\n' > ARCHI.json
elif [ "$1" = "-" ]; then
  cat >/dev/null
  printf 'mutated stamp\\n' >> ARCHI.md
  printf 'stamped\\n'
elif [ "$1" = "-m" ] && [ "$2" = "omniagentos.archdocs.diagram" ]; then
  printf 'mutated diagram\\n' > docs/architecture/system-map.mmd
  printf 'mutated diagram\\n' > docs/architecture/system-map.md
fi
""",
        encoding="utf-8",
    )
    python.chmod(0o755)
    return repo, script


def test_archi_morning_refuses_non_main_without_mutation_or_commit(
    non_main_archi_repo: tuple[Path, Path],
) -> None:
    repo, script = non_main_archi_repo
    before_hashes = _hashes(repo)
    before_head = _git(repo, "rev-parse", "HEAD")

    completed = subprocess.run(
        ["sh", str(script)],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )

    log = (repo / "var" / "log" / "archi-morning.log").read_text(encoding="utf-8")
    assert completed.returncode == 0, completed.stdout + completed.stderr + log
    assert "REFUSED: HEAD is scratch, not main" in log
    assert _hashes(repo) == before_hashes
    assert _git(repo, "rev-parse", "HEAD") == before_head
    assert not (repo / "var" / "fake-python.invoked").exists()


def test_archi_morning_commits_one_owned_refresh_that_verifies_fresh(tmp_path: Path) -> None:
    """The supported main-only script must finish with a committed fresh tree."""
    from omniagentos.archdocs.staleness import is_stale, parse_stamp_comment

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Archi Morning Test")
    _git(repo, "config", "user.email", "archi-morning@example.com")

    routes = repo / "omniagentos" / "api" / "routes"
    migrations = repo / "omniagentos" / "db" / "migrations"
    routes.mkdir(parents=True)
    migrations.mkdir(parents=True)
    (routes / "widgets.py").write_text(
        '@router.get("/widgets")\ndef widgets(): ...\n', encoding="utf-8"
    )
    (migrations / "001_init.sql").write_text("-- init\n", encoding="utf-8")

    script = repo / "scripts" / "archi-morning" / "archi-morning.sh"
    script.parent.mkdir(parents=True)
    shutil.copy2(ARCHI_MORNING, script)
    (repo / ".gitignore").write_text(".venv/\nvar/\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "source code")
    source = _git(repo, "rev-parse", "--short", "HEAD")

    python = repo / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text(
        f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n',
        encoding="utf-8",
    )
    python.chmod(0o755)
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT)

    completed = subprocess.run(
        ["sh", str(script)],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    log = (repo / "var" / "log" / "archi-morning.log").read_text(encoding="utf-8")

    assert completed.returncode == 0, completed.stdout + completed.stderr + log
    assert "verified committed architecture refresh is fresh" in log
    assert _git(repo, "rev-parse", "--short", "HEAD") != source
    changed = set(_git(repo, "show", "--format=", "--name-only", "HEAD").splitlines())
    assert changed
    assert changed.issubset(set(OWNED))
    stored = parse_stamp_comment((repo / "ARCHI.md").read_text(encoding="utf-8"))
    assert stored is not None
    assert stored.git_head == source
    assert is_stale(repo) is False
