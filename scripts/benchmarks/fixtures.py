"""Benchmark fixtures: load, freeze-check, and materialize a hermetic workspace.

A fixture directory looks like::

    tests/benchmarks/fixtures/<id>/
        fixture.yaml   # metadata + task text + declared file set + acceptance
        seed/          # copied into the workspace BEFORE the arm runs
        accept/        # copied over the workspace AFTER the arm runs (frozen)

``accept/`` landing last is the whole trick: an arm that "fixes" a failing
check by editing the check has its edit overwritten before grading, so it
scores a failure and the edit is separately recorded as an undeclared
modification.

Fixtures are hermetic — the seed is self-contained and nothing reads the
surrounding repository at run time — so acceptance stays deterministic as the
repo moves. ``repo_rev`` in ``fixture.yaml`` is the provenance pin (which repo
state the fixture was authored against), not an execution dependency.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from omniagentos.contracts import digest

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES_DIR = REPO_ROOT / "tests" / "benchmarks" / "fixtures"
FROZEN_DIGESTS_PATH = REPO_ROOT / "tests" / "benchmarks" / "frozen_digests.json"

# Never copied into a workspace and never part of a fixture digest.
_IGNORED_NAMES = frozenset({"__pycache__", ".pytest_cache", ".DS_Store", ".ruff_cache"})


@dataclass(frozen=True)
class Canary:
    """A file an arm has no legitimate reason to open, plus its unique marker."""

    path: str
    token: str


@dataclass(frozen=True)
class Fixture:
    """One benchmark task with a frozen acceptance check."""

    id: str
    prompt: str
    discipline: str
    kind: str
    tier: str
    repo_rev: str
    declared_files: tuple[str, ...]
    canaries: tuple[Canary, ...]
    acceptance_paths: tuple[str, ...]
    acceptance_timeout_s: int
    timeout_ms: int
    max_turns: int | None
    root: Path
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def seed_dir(self) -> Path:
        return self.root / "seed"

    @property
    def accept_dir(self) -> Path:
        return self.root / "accept"

    @property
    def solution_dir(self) -> Path:
        """Reference solution overlay. Never materialized into an arm's workspace."""
        return self.root / "solution"

    def digest(self) -> str:
        """Content digest over the WHOLE fixture (metadata + seed + accept)."""
        return tree_digest(self.root)


def _load_yaml(path: Path) -> dict[str, Any]:
    import yaml

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} must be a mapping, got {type(data).__name__}")
    return data


def _iter_files(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in _IGNORED_NAMES for part in path.relative_to(root).parts):
            continue
        yield path


def tree_digest(root: Path) -> str:
    """Order-independent content digest of a directory tree."""
    parts: list[str] = []
    for path in _iter_files(root):
        rel = path.relative_to(root).as_posix()
        parts.append(f"{rel}:{digest(path.read_bytes())}")
    return digest("\n".join(parts))


def load_fixture(fixture_dir: str | Path) -> Fixture:
    """Load one fixture directory."""
    root = Path(fixture_dir).resolve()
    meta_path = root / "fixture.yaml"
    if not meta_path.is_file():
        raise FileNotFoundError(f"fixture metadata not found: {meta_path}")
    data = _load_yaml(meta_path)

    for key in ("id", "prompt", "discipline"):
        if key not in data:
            raise ValueError(f"{meta_path} missing required field {key!r}")
    if data["id"] != root.name:
        raise ValueError(f"fixture id {data['id']!r} does not match directory {root.name!r}")
    if not (root / "seed").is_dir():
        raise FileNotFoundError(f"fixture {data['id']} has no seed/ directory")
    if not (root / "accept").is_dir():
        raise FileNotFoundError(f"fixture {data['id']} has no accept/ directory")

    acceptance = data.get("acceptance") or {}
    paths = tuple(str(p) for p in (acceptance.get("paths") or []))
    if not paths:
        raise ValueError(f"fixture {data['id']} declares no acceptance.paths")
    for rel in paths:
        if not (root / "accept" / rel).is_file():
            raise FileNotFoundError(
                f"fixture {data['id']} acceptance path {rel!r} is missing from accept/"
            )

    canaries = tuple(
        Canary(path=str(c["path"]), token=str(c["token"])) for c in (data.get("canaries") or [])
    )
    for canary in canaries:
        if not (root / "seed" / canary.path).is_file():
            raise FileNotFoundError(
                f"fixture {data['id']} canary {canary.path!r} is missing from seed/"
            )

    max_turns_raw = data.get("max_turns")
    return Fixture(
        id=str(data["id"]),
        prompt=str(data["prompt"]).strip(),
        discipline=str(data["discipline"]),
        kind=str(data.get("kind") or "unspecified"),
        tier=str(data.get("tier") or "standard"),
        repo_rev=str(data.get("repo_rev") or ""),
        declared_files=tuple(str(p) for p in (data.get("declared_files") or [])),
        canaries=canaries,
        acceptance_paths=paths,
        acceptance_timeout_s=int(acceptance.get("timeout_s") or 120),
        timeout_ms=int(data.get("timeout_ms") or 300_000),
        max_turns=int(max_turns_raw) if max_turns_raw is not None else None,
        root=root,
        raw=data,
    )


def load_fixtures(
    fixtures_dir: str | Path = FIXTURES_DIR,
    *,
    only: Sequence[str] | None = None,
) -> list[Fixture]:
    """Load every fixture under *fixtures_dir*, sorted by id."""
    root = Path(fixtures_dir)
    if not root.is_dir():
        raise FileNotFoundError(f"fixtures directory not found: {root}")
    wanted = set(only or ())
    out: list[Fixture] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.name in _IGNORED_NAMES:
            continue
        if not (child / "fixture.yaml").is_file():
            continue
        if wanted and child.name not in wanted:
            continue
        out.append(load_fixture(child))
    if wanted:
        missing = wanted - {f.id for f in out}
        if missing:
            raise KeyError(f"unknown fixture ids: {sorted(missing)}")
    return out


def fixture_set_digest(fixtures: Sequence[Fixture]) -> str:
    """One digest identifying the whole corpus. Changes ⇒ baselines are not comparable."""
    return digest("\n".join(f"{f.id}:{f.digest()}" for f in sorted(fixtures, key=lambda f: f.id)))


def materialize(fixture: Fixture, workspace: str | Path) -> Path:
    """Copy ``seed/`` into a fresh *workspace* directory and return it."""
    target = Path(workspace)
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(
        fixture.seed_dir,
        target,
        ignore=shutil.ignore_patterns(*sorted(_IGNORED_NAMES)),
    )
    return target


def _overlay(source: Path, workspace: str | Path) -> list[str]:
    target = Path(workspace)
    written: list[str] = []
    for path in _iter_files(source):
        rel = path.relative_to(source)
        dest = target / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, dest)
        written.append(rel.as_posix())
    return written


def apply_solution(fixture: Fixture, workspace: str | Path) -> list[str]:
    """Overlay the reference solution. Used only by the harness's own tests, to
    prove a fixture is passable and that its acceptance check discriminates."""
    if not fixture.solution_dir.is_dir():
        raise FileNotFoundError(f"fixture {fixture.id} has no solution/ directory")
    return _overlay(fixture.solution_dir, workspace)


def apply_acceptance_files(fixture: Fixture, workspace: str | Path) -> list[str]:
    """Overlay ``accept/`` onto the workspace, overwriting. Returns the paths written.

    Called AFTER the arm has finished, so frozen checks always win over
    whatever the arm left behind.
    """
    return _overlay(fixture.accept_dir, workspace)
