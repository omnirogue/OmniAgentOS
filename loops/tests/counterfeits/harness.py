"""Executable counterfeit corpus for the loop runtime (house doctrine, W4-10).

Each entry is a mutation that makes a safety property LOOK implemented while
removing it. The corpus is only meaningful if two things hold:

1. a **control** pass runs every ``must_fail`` node id against the UNMUTATED
   tree and they are all green — otherwise an entry pointing at an
   already-broken test is indistinguishable from a working guard; and
2. the mutated run is RED **and** the failure text matches ``failure_re`` —
   otherwise a collection error or an unrelated failure counts as a catch.

The loops suite cannot use ``tests/counterfeits/harness.py``: that one shells
out to the production venv, which has no LangGraph. This one mirrors its rules
in ~120 lines against an isolated copy of ``loops/``, with ``omniagentos`` and
``configs`` symlinked in so the copy runs against the REAL control plane.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
LOOPS_DIR = HERE.parents[1]
REPO_ROOT = LOOPS_DIR.parent
MANIFEST = HERE / "corpus.toml"
LINKED_FROM_REPO = ("omniagentos", "configs")
IGNORED = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache")


class CounterfeitSurvived(AssertionError):
    """A mutation that removes a safety property was not caught by any test."""


@dataclass(frozen=True)
class Counterfeit:
    id: str
    #: The template whose safety property this entry primarily attacks
    #: (empty for a cross-cutting bridge property). Every template must be the
    #: primary of at least one entry — that is the plan's "one counterfeit per
    #: template" requirement, made mechanical.
    primary_template: str
    file: str
    find: str
    replace: str
    rationale: str
    must_fail: tuple[str, ...]
    failure_re: str


def load_corpus(manifest: Path = MANIFEST) -> list[Counterfeit]:
    raw: dict[str, Any] = tomllib.loads(manifest.read_text(encoding="utf-8"))
    entries = [
        Counterfeit(
            id=item["id"],
            primary_template=item.get("primary_template", ""),
            file=item["file"],
            find=item["find"],
            replace=item["replace"],
            rationale=item["rationale"],
            must_fail=tuple(item["must_fail"]),
            failure_re=item["failure_re"],
        )
        for item in raw.get("counterfeit", [])
    ]
    ids = [entry.id for entry in entries]
    duplicates = {name for name in ids if ids.count(name) > 1}
    if duplicates:
        raise ValueError(f"duplicate counterfeit ids: {sorted(duplicates)}")
    if not entries:
        raise ValueError("counterfeit corpus is empty")
    return entries


def materialise(root: Path) -> Path:
    """Copy ``loops/`` into an isolated fake repo root and link the control plane."""
    fake_repo = root / "repo"
    fake_repo.mkdir(parents=True, exist_ok=True)
    shutil.copytree(LOOPS_DIR, fake_repo / "loops", ignore=IGNORED)
    for name in LINKED_FROM_REPO:
        os.symlink(REPO_ROOT / name, fake_repo / name)
    (fake_repo / "var").mkdir(exist_ok=True)
    return fake_repo


def apply(fake_repo: Path, entry: Counterfeit) -> None:
    target = fake_repo / "loops" / entry.file
    if not target.is_file():
        raise FileNotFoundError(f"{entry.id}: {entry.file} does not exist")
    text = target.read_text(encoding="utf-8")
    occurrences = text.count(entry.find)
    if occurrences != 1:
        raise ValueError(
            f"{entry.id}: patch anchor must match exactly once in {entry.file}, "
            f"found {occurrences} — the corpus is stale"
        )
    target.write_text(text.replace(entry.find, entry.replace), encoding="utf-8")


def run_nodes(fake_repo: Path, node_ids: tuple[str, ...]) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = f"{fake_repo}:{fake_repo / 'loops'}"
    env.pop("OMNIAGENTOS_LOOPS_ROOT", None)
    argv = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        *[str(fake_repo / "loops" / node) for node in node_ids],
    ]
    return subprocess.run(
        argv, capture_output=True, text=True, env=env, cwd=str(fake_repo), timeout=900
    )


def control(root: Path, entries: list[Counterfeit]) -> subprocess.CompletedProcess:
    """Every must_fail node, unmutated. All green, or the corpus proves nothing."""
    nodes = tuple(dict.fromkeys(node for entry in entries for node in entry.must_fail))
    return run_nodes(materialise(root / "control"), nodes)


def check(root: Path, entry: Counterfeit) -> subprocess.CompletedProcess:
    fake_repo = materialise(root / entry.id)
    apply(fake_repo, entry)
    result = run_nodes(fake_repo, entry.must_fail)
    output = result.stdout + result.stderr
    if "error" in output.lower() and "errors during collection" in output.lower():
        raise CounterfeitSurvived(
            f"{entry.id}: the mutated tree failed to COLLECT — an import error is "
            f"not a catch.\n{output[-3000:]}"
        )
    if result.returncode == 0:
        raise CounterfeitSurvived(
            f"{entry.id} SURVIVED: {entry.rationale}\nnodes: {entry.must_fail}\n{output[-3000:]}"
        )
    if not re.search(entry.failure_re, output):
        raise CounterfeitSurvived(
            f"{entry.id}: red, but not for the expected reason "
            f"(/{entry.failure_re}/ not found).\n{output[-3000:]}"
        )
    return result


__all__ = [
    "Counterfeit",
    "CounterfeitSurvived",
    "apply",
    "check",
    "control",
    "load_corpus",
    "materialise",
    "run_nodes",
]
