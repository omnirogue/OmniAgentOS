"""The mutation mechanism: copy the tree, break ONE thing, and demand a red run.

Ported in mechanism from the system this package was extracted from, where the
same harness ran 87 mutation patches against a live control plane. The rules
below are the part that survived four adversarial reviews without a substantive
challenge, so they are reproduced here deliberately rather than reinvented.

Per entry, in this order:

1. **Materialise.** ``selfloop/``, ``tests/`` and ``pyproject.toml`` are copied
   into a throwaway root. Nothing is patched in place, so a crashed run cannot
   leave a weakened package behind — which happened once, on the predecessor,
   and was found by a developer wondering why the T2 floor had stopped working
   on their branch.
2. **Prove the copy is the thing under test.** A one-line subprocess imports
   ``selfloop`` with the mutant's environment and reports where it came from. An
   installed copy shadowing the mutant would silently turn the entire corpus
   into a no-op: every mutation would leave the real package untouched, the
   control would pass, and each entry would report SURVIVED for a guard that is
   in fact intact. Absence of isolation is not permission to run.
3. **Patch.** One literal find/replace, and **the anchor must match exactly
   once**. Zero matches or two are both a STALE corpus, not a failed mutation:
   a refactor that moves a guard must break the corpus loudly rather than
   quietly disarm the entry that was watching it.
4. **Run only the named node ids** in a subprocess, and require RED **and** a
   match on ``failure_re`` **and** no collection error.

And once per session, before any of that: a **control** pass running every
``must_fail`` node id against the UNMUTATED tree, requiring all of them green.
Without it, an entry pointing at an already-broken test is indistinguishable
from a working guard — the run is red either way, and the corpus reports a catch
it did not make.

Three refusals are spelled as distinct exception types because they need
distinct responses from whoever reads the failure, and a single ``AssertionError``
trains people to read all three as "the mutation test is flaky":

* :class:`CounterfeitSurvived` — a safety property can be deleted and the suite
  stays green. Write the missing test.
* :class:`CounterfeitStale` — the anchor moved. Re-anchor the entry, but only
  after confirming the same mutation still makes the same tests red for the same
  reason.
* :class:`HarnessNotIsolated` — the harness is not testing what it thinks it is.
  Nothing about the package has been demonstrated either way.
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

#: ``tests/counterfeits``
HERE = Path(__file__).resolve().parent
#: ``tests``
TESTS_DIR = HERE.parent
#: The project root: the directory holding ``selfloop/`` and ``pyproject.toml``.
PROJECT_ROOT = TESTS_DIR.parent
MANIFEST = HERE / "corpus.toml"

#: What a mutant root contains. Deliberately not the whole project: ``examples/``
#: and any ``var/`` a developer has lying around are not under test, and copying
#: them costs a tenth of a second per entry for nothing. ``pyproject.toml``
#: travels because it carries ``xfail_strict``, and a mutant that silently lost
#: that setting would let an ``xfail`` absorb a mutation.
COPIED: tuple[str, ...] = ("selfloop", "tests", "pyproject.toml")

IGNORED = shutil.ignore_patterns(
    "__pycache__", "*.pyc", "*.pyo", ".pytest_cache", ".git", "*.db", "*.db-wal", "*.db-shm"
)

#: Seconds one node-id run may take. Generous, because a mutant run starts a
#: fresh interpreter and some entries pull the whole learning loop in; bounded,
#: because a mutation that makes a test hang must fail the gate rather than hold
#: CI open until somebody notices.
DEFAULT_TIMEOUT_S = 600

#: Families a corpus entry may declare. ``learning`` is separated from ``safety``
#: for one reason, and it is the reason this corpus is bigger than its
#: predecessor: the source system's 87 mutation patches touched ZERO learning
#: code, and the learning loop is precisely the part that starved undetected for
#: months. ``tests/test_counterfeits.py`` asserts the learning family is
#: non-empty and stays that way.
FAMILIES = frozenset({"safety", "learning"})


class CounterfeitSurvived(AssertionError):
    """A mutation that removes a safety property was not caught by any test."""


class CounterfeitStale(AssertionError):
    """A patch anchor did not match exactly once. The corpus, not the code, is wrong."""


class HarnessNotIsolated(AssertionError):
    """The mutant tree is not what the subprocess imports. Nothing has been proven."""


@dataclass(frozen=True)
class Counterfeit:
    """One entry: a property, the line that implements it, and its witnesses."""

    id: str
    #: ``safety`` or ``learning``. See :data:`FAMILIES`.
    family: str
    #: The template whose safety property this entry primarily attacks (empty for
    #: a cross-cutting property that belongs to no single template). Every shipped
    #: template must be the primary of at least one entry — that is the plan's
    #: "one counterfeit per template" requirement, made mechanical in
    #: ``tests/test_counterfeits.py``.
    primary_template: str
    #: Path relative to the project root, e.g. ``selfloop/policy.py``.
    file: str
    find: str
    replace: str
    #: What the removed line was FOR, in a sentence an operator can act on. It is
    #: the text printed when the entry survives, so it has to answer "and what
    #: should I do about it?" rather than merely restating the diff.
    rationale: str
    must_fail: tuple[str, ...]
    #: Regex the mutated run's combined output must match. Without it, a mutation
    #: that happens to break an unrelated import counts as a catch, and the corpus
    #: slowly degrades into a very slow smoke test.
    failure_re: str
    #: Non-empty when NOTHING in the suite currently defends this property, and
    #: it names the test that is owed and the file that owes it.
    #:
    #: The entry stays in the corpus and stays running. What changes is that
    #: ``tests/test_counterfeits.py`` marks it ``xfail(strict=True)``, so the
    #: hole is visible in every test report as a named XFAIL rather than being
    #: quietly dropped — and the moment somebody writes the missing test the
    #: entry XPASSes, which under this project's ``xfail_strict`` is a hard
    #: failure demanding that this field be deleted. A debt with an alarm on it,
    #: not an exemption.
    undefended_by: str = ""


def load_corpus(manifest: Path = MANIFEST) -> list[Counterfeit]:
    """Parse ``corpus.toml``, refusing anything that would make a run meaningless.

    Every refusal here is about a corpus that would *appear* to work. A duplicate
    id makes two entries share a report line; an entry with no ``must_fail`` node
    can never fail; an empty ``failure_re`` accepts any red at all; and an empty
    corpus passes the whole gate in eight milliseconds. All four look like a
    passing counterfeit suite from the outside, which is why they are checked
    here rather than left to a reviewer.
    """
    raw: dict[str, Any] = tomllib.loads(manifest.read_text(encoding="utf-8"))
    items = raw.get("counterfeit", [])
    if not items:
        raise CounterfeitStale(f"the counterfeit corpus at {manifest} is empty")

    entries: list[Counterfeit] = []
    for index, item in enumerate(items):
        entry = Counterfeit(
            id=str(item["id"]),
            family=str(item["family"]),
            primary_template=str(item.get("primary_template", "")),
            file=str(item["file"]),
            find=str(item["find"]),
            replace=str(item["replace"]),
            rationale=str(item["rationale"]),
            must_fail=tuple(str(node) for node in item["must_fail"]),
            failure_re=str(item["failure_re"]),
            undefended_by=str(item.get("undefended_by", "")),
        )
        where = f"entry #{index} ({entry.id!r})"
        if entry.family not in FAMILIES:
            raise CounterfeitStale(
                f"{where}: family {entry.family!r} is not one of {sorted(FAMILIES)}"
            )
        if not entry.must_fail:
            raise CounterfeitStale(
                f"{where}: names no must_fail node — an entry with no witness can never "
                "catch anything, and it counts as a passing entry in the summary"
            )
        if not entry.failure_re.strip():
            raise CounterfeitStale(
                f"{where}: has an empty failure_re, which accepts ANY red run — including "
                "a collection error caused by the patch itself"
            )
        if not entry.find.strip():
            raise CounterfeitStale(f"{where}: has an empty find anchor")
        if entry.find == entry.replace:
            raise CounterfeitStale(f"{where}: find and replace are identical; it mutates nothing")
        entries.append(entry)

    ids = [entry.id for entry in entries]
    duplicates = sorted({name for name in ids if ids.count(name) > 1})
    if duplicates:
        raise CounterfeitStale(f"duplicate counterfeit ids: {duplicates}")
    return entries


def materialise(root: Path) -> Path:
    """Copy the project into *root* and prove the copy is what a subprocess imports.

    The isolation check is not ceremony. ``selfloop`` may well be installed in
    the interpreter running this suite — editable, or from a wheel — and if that
    copy wins the import, every mutation is applied to a tree nobody loads. The
    control passes, each entry reports SURVIVED, and a morning is spent looking
    for missing tests that are not missing. One extra interpreter start per
    mutant is a cheap price for never having that morning.
    """
    root.mkdir(parents=True, exist_ok=True)
    for name in COPIED:
        source = PROJECT_ROOT / name
        target = root / name
        if source.is_dir():
            shutil.copytree(source, target, ignore=IGNORED)
        elif source.is_file():
            shutil.copy2(source, target)
        else:
            raise CounterfeitStale(
                f"cannot materialise a mutant: {source} does not exist, so the harness is "
                "pointed at something that is not this project"
            )
    _assert_isolated(root)
    return root


def _env_for(root: Path) -> dict[str, str]:
    """The environment a mutant runs under: its own tree first, user site off.

    ``PYTHONPATH`` is set to the mutant root ALONE rather than prepended to an
    inherited one, because an inherited entry pointing at the real project would
    reintroduce exactly the shadowing that :func:`_assert_isolated` exists to
    catch. ``PYTHONNOUSERSITE`` closes the other route to the same problem.
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = str(root)
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _assert_isolated(root: Path) -> None:
    """Raise unless ``import selfloop`` inside *root* resolves inside *root*."""
    probe = subprocess.run(
        [sys.executable, "-c", "import selfloop, sys; sys.stdout.write(selfloop.__file__)"],
        capture_output=True,
        text=True,
        env=_env_for(root),
        cwd=str(root),
        timeout=120,
        check=False,
    )
    resolved = probe.stdout.strip()
    if probe.returncode != 0 or not resolved:
        raise HarnessNotIsolated(
            f"the mutant tree at {root} cannot import selfloop at all "
            f"(exit {probe.returncode}): {(probe.stderr or '').strip()[-2000:]}"
        )
    if not Path(resolved).resolve().is_relative_to(root.resolve()):
        raise HarnessNotIsolated(
            f"a subprocess run inside the mutant tree imported selfloop from {resolved}, "
            f"which is outside {root}. Every mutation would be applied to a tree nothing "
            "loads, so the corpus would prove nothing in either direction. The usual cause "
            "is an installed or editable selfloop earlier on sys.path."
        )


def apply(root: Path, entry: Counterfeit) -> None:
    """Apply *entry*'s single find/replace to the mutant at *root*.

    **The anchor must match exactly once.** Zero matches means the guard moved or
    was renamed; two means the anchor is no longer specific enough to say which
    line it was watching. Both are the corpus being wrong about the code, and
    both must stop the run — a corpus that silently skips a moved anchor is a
    corpus that disarms itself during exactly the refactor you most want it
    watching.
    """
    target = root / entry.file
    if not target.is_file():
        raise CounterfeitStale(
            f"{entry.id}: {entry.file} does not exist in the mutant tree; the corpus names "
            "a file this project no longer has"
        )
    text = target.read_text(encoding="utf-8")
    occurrences = text.count(entry.find)
    if occurrences != 1:
        raise CounterfeitStale(
            f"{entry.id}: the patch anchor must match exactly once in {entry.file}, found "
            f"{occurrences}. The corpus is STALE — the guard it watches has moved, been "
            f"renamed, or been duplicated. Re-anchor it only after confirming the same "
            f"mutation still makes {list(entry.must_fail)} red for the same reason.\n"
            f"--- anchor ---\n{entry.find}"
        )
    target.write_text(text.replace(entry.find, entry.replace), encoding="utf-8")


def run_nodes(
    root: Path, node_ids: tuple[str, ...], *, timeout_s: int = DEFAULT_TIMEOUT_S
) -> subprocess.CompletedProcess[str]:
    """Run exactly *node_ids* in a fresh interpreter rooted at the mutant.

    A subprocess and not an in-process ``pytest.main``: the mutated modules must
    be imported from scratch, and this interpreter already has the real ones in
    ``sys.modules``. ``-p no:cacheprovider`` keeps the mutant from writing a
    cache directory back into a tree that is about to be deleted.

    ``-rf`` is not cosmetic. It guarantees a line reading
    ``FAILED <node id>`` for every failure, which is the stable surface an
    entry's ``failure_re`` matches against; without it the only occurrence of a
    test's name is inside a traceback whose format changes between pytest
    releases, and a corpus whose judging rule depends on a traceback layout
    starts silently accepting the wrong reds after an upgrade.
    """
    argv = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "--no-header",
        "-rf",
        "-p",
        "no:cacheprovider",
        *node_ids,
    ]
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        env=_env_for(root),
        cwd=str(root),
        timeout=timeout_s,
        check=False,
    )


def _combined(result: subprocess.CompletedProcess[str]) -> str:
    return f"{result.stdout or ''}\n{result.stderr or ''}"


def _collection_error(output: str) -> bool:
    """Did the mutant fail to COLLECT? An import error is never a catch.

    A patch that produces a syntax error, or that deletes a name another module
    imports, makes the run red without demonstrating anything about the guard.
    Counting that as a catch is how a corpus keeps its score while losing its
    meaning.
    """
    lowered = output.lower()
    return (
        "errors during collection" in lowered
        or "error collecting" in lowered
        or "internalerror" in lowered
    )


def control_nodes(entries: list[Counterfeit]) -> tuple[str, ...]:
    """Every node id any entry relies on, de-duplicated, in first-seen order."""
    return tuple(dict.fromkeys(node for entry in entries for node in entry.must_fail))


def control(
    root: Path, entries: list[Counterfeit], *, timeout_s: int = DEFAULT_TIMEOUT_S
) -> subprocess.CompletedProcess[str]:
    """Run every ``must_fail`` node UNMUTATED. All green, or the corpus proves nothing.

    This is the half of the mechanism that people leave out, and leaving it out
    is what turns a mutation corpus into theatre. Each entry asserts "these tests
    go red when the guard is removed"; without a control, an entry pointing at a
    test that is *already* red passes for exactly the wrong reason, and it passes
    forever, because nobody re-reads a green gate.

    Module-scoped in ``tests/test_counterfeits.py``: one control run per session,
    covering every node the corpus depends on.
    """
    return run_nodes(materialise(root), control_nodes(entries), timeout_s=timeout_s)


def check(
    root: Path, entry: Counterfeit, *, timeout_s: int = DEFAULT_TIMEOUT_S
) -> subprocess.CompletedProcess[str]:
    """Materialise, patch, run, judge. Raises :class:`CounterfeitSurvived` on a pass.

    *root* is this entry's own directory and must not be shared: a mutant is a
    weakened copy of the package, and two of them in one tree is a mutation
    nobody wrote.

    Three ways a red run is refused as a catch, and each of them has happened:
    the run did not collect (the patch broke an import, not a guard); the run
    exited zero (the guard is not tested); and the run was red for a reason the
    entry did not predict (something unrelated is broken, and the entry is now
    reporting somebody else's failure as its own success).
    """
    mutant = materialise(root)
    apply(mutant, entry)
    result = run_nodes(mutant, entry.must_fail, timeout_s=timeout_s)
    output = _combined(result)

    if _collection_error(output):
        raise CounterfeitSurvived(
            f"{entry.id}: the mutated tree failed to COLLECT, so no guard was exercised. "
            f"An import error is not a catch — narrow the patch until it removes the "
            f"property and nothing else.\nnodes: {list(entry.must_fail)}\n{output[-3000:]}"
        )
    if result.returncode == 0:
        raise CounterfeitSurvived(
            f"{entry.id} SURVIVED: {entry.rationale}\n"
            f"The mutation below is applied to {entry.file} and "
            f"{list(entry.must_fail)} still pass. Nothing in this suite defends that "
            f"property.\n--- removed ---\n{entry.find}\n--- replaced with ---\n"
            f"{entry.replace}\n{output[-3000:]}"
        )
    if not re.search(entry.failure_re, output):
        raise CounterfeitSurvived(
            f"{entry.id}: the mutated tree is red, but not for the expected reason — "
            f"/{entry.failure_re}/ does not appear in its output. Either the guard is "
            f"intact and something else is broken, or the entry's failure_re no longer "
            f"describes how this property fails.\n{output[-3000:]}"
        )
    return result


__all__ = [
    "COPIED",
    "DEFAULT_TIMEOUT_S",
    "FAMILIES",
    "HERE",
    "MANIFEST",
    "PROJECT_ROOT",
    "TESTS_DIR",
    "Counterfeit",
    "CounterfeitStale",
    "CounterfeitSurvived",
    "HarnessNotIsolated",
    "apply",
    "check",
    "control",
    "control_nodes",
    "load_corpus",
    "materialise",
    "run_nodes",
]
