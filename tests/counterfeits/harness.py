"""Executable counterfeit-corpus runner (W4-10).

Per entry:

1. Acquire an isolated scratch tree (never mutates the operator checkout,
   never ``git stash`` — nested worktrees are unavailable in this layout so we
   copy into a temp dir instead of ``git worktree add``).
2. ``git apply`` the patch; a failed apply is a hard error, never a skip.
3. Run only the ``must_fail`` node ids against the mutated tree.
4. Assert ``returncode != 0`` **and** ``re.search(failure_re, output)``.
5. Assert collection succeeded (a collection/import error is not a catch).
6. Release the scratch tree (reset before the next entry, see below).

SCRATCH REUSE (``_stage_pristine`` / :func:`acquire_scratch`)
------------------------------------------------------------
A fresh ``rsync`` of the whole checkout per entry copied 5.1k files / ~60MB and
measured 5.9–15s **per entry** on this host — 82–85% of a cold gate for a corpus
whose mean patch touches 1.02 files. Each worker (process *and* thread: see
:func:`acquire_scratch`) therefore stages ONE read-only pristine tree, and between
entries the scratch is reset with ``rsync -a --delete <pristine>/ <scratch>/``
instead of being destroyed and re-copied.

``--delete`` makes the reset **stronger** than a fresh copy, not weaker: it
restores modified files, re-creates files the patch deleted, and removes every
file the patch or the test run added (including ``var/counterfeit-entry`` runtime
state, ``tmp/`` and stray ``__pycache__``). The reset therefore runs with **no
exclude list at all** — the excludes belong to the pristine *staging* copy only.
Excluding a path from the reset would protect it from ``--delete`` and let entry
N's residue survive into entry N+1, which is exactly the isolation property this
gate exists to have. The one known in-tree mutator (``tests/doctrine/_fixtures/``)
is handled by the gate at ``scripts/merge-gate.sh``.

Fail-safe direction: a pristine that does not carry :data:`_TREE_SENTINELS`, or a
reset that fails for any reason, is REFUSED or downgraded to a fresh full copy —
never "restored" from an empty/partial source. An empty rsync source with
``--delete`` would silently empty the scratch and turn every entry into a
collection error; a reset that half-ran would leak the previous patch. Set
``OMNIAGENTOS_CF_SCRATCH_REUSE=0`` to force the old copy-per-entry behaviour.

An entry that ends ABNORMALLY does not get the reset treatment at all: see
:func:`poison_scratch_lease`. ``rsync --delete`` restores *content*, and content
is not the only unknown after a timeout — the killed pytest's grandchildren are
not killed with it and may still be writing. Such a lease is dropped and its
tree destroyed, so the next entry on that worker pays for a genuine fresh
:func:`materialise_scratch` and any orphan writes into a path nothing reads.

MEASURED (this host, interleaved runs to control for load)
----------------------------------------------------------
* copy-vs-reset — the operation this lane replaces: 1.90s → 0.65s (**3.05x**).
* steady-state per ENTRY (acquire + apply + pytest): 3.92s → 2.20s (**1.78x**).
* **FIRST entry on each worker pays a one-time +7.45s** (11.11s vs 3.66s): that
  worker must stage its pristine tree before it can reset anything. It is
  amortised over every later entry that worker runs and is NOT a regression —
  do not read a slow first entry as one. Whole-run cost is ``workers × 7.45s``.
* An earlier "2.41x" did NOT reproduce (host contention at one instant); the
  numbers above are the re-confirmed ones. Do not quote 2.41x.
* Isolation is proven by equivalence, not assertion: a byte manifest over 5,142
  paths after a mutating entry gives ``only_fresh=0 only_reset=0 changed=0``
  against a genuine fresh copy.

DISK FOOTPRINT — reuse trades disk for time. Each worker holds **two** trees at
once (its pristine + its leased scratch) where copy-per-entry held one, so pool
width N needs roughly ``2N × tree size`` on the temp filesystem (~55MB/tree
here). Width is therefore DISK-bound, not broken: a full-corpus run at width 4
died with ``ENOSPC`` on a nearly-full disk, and the same width-4 run with ~350GB
free completed GATE GREEN in 117s against 269s at width 1, with an identical
96-entry verdict list. :data:`POOL_WORKERS_DEFAULT` stays 1 anyway — the
gate-side binding is a separate lane — and ``run_entries(workers>1)``
additionally carries the spawn-safety caller trap documented on
:func:`run_entries`. Leaked-tree check after those runs: zero
``counterfeit-scratch-*`` survivors from either width, so the ``atexit`` pool
reset does reach ProcessPoolExecutor children.

A control pass runs the union of all ``must_fail`` node ids unpatched and
asserts they are all green — otherwise a corpus entry that points at an
already-broken test is indistinguishable from a working guard.

Surviving counterfeits raise :class:`CounterfeitSurvived` and make the gate red.

CORPUS FRAGMENTS (``corpus.d/``)
--------------------------------
Every concurrent lane used to append its entries to the single ``corpus.toml``
at EOF, so every multi-lane rebase produced a textual conflict in the same file.
:func:`load_corpus` therefore loads ``corpus.toml`` **and** every
``corpus.d/*.toml`` fragment beside it.  A lane adds
``corpus.d/<lane-id>.toml`` and no two lanes ever write the same file.

Rules, all mechanical:

* ``corpus.toml`` is unchanged and still authoritative; fragments are additive.
* Fragments are loaded in filename order (``sorted``) after the base manifest,
  so a run is reproducible.
* A ``patch`` inside a fragment is resolved against the **corpus root** (the
  directory holding ``corpus.toml``), exactly like a ``corpus.toml`` entry — a
  lane writes ``patch = "patches/cf-foo.patch"`` in either place.
* A duplicate ``id`` anywhere — base or any fragment — is a hard error naming
  both files, both table indices and both original spellings.  Silent last-wins
  would let one lane delete another lane's counterfeit by accident, which is the
  whole failure mode this exists to stop.  Collision is judged on the NORMALISED
  id (:func:`normalise_id`: whitespace collapsed, case folded), because a check
  that only catches byte-exact repeats lets ``cf-foo`` and ``CF-Foo`` coexist
  and is therefore decoration.
* A ``patch`` that resolves outside the corpus tree is a hard error.  The
  manifest is data and ``patch`` becomes a filesystem read, so containment is
  checked with the repo's shared inode-ancestry primitive — never
  ``str.startswith``, which admits a ``<root>-evil`` sibling.
* A malformed fragment is a hard error, never a skip: same seriousness as a
  patch that fails to apply.
* No ``corpus.d/`` directory, or one holding no ``*.toml``, behaves exactly as
  before.
"""

from __future__ import annotations

import atexit
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from collections.abc import Sequence
from concurrent.futures import (
    Future,
    ProcessPoolExecutor,
    ThreadPoolExecutor,
    as_completed,
)
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

# Containment is decided by the repo's shared inode-ancestry primitive so the
# corpus loader cannot disagree with every other security consumer, and so a
# `<root>-evil` sibling (a string prefix of the root) is refused.
from omniagentos.path_containment import inode_relative_parts

REPO_ROOT = Path(__file__).resolve().parents[2]
CORPUS_DIR = Path(__file__).resolve().parent
DEFAULT_MANIFEST = CORPUS_DIR / "corpus.toml"
# Per-lane fragment directory: one file per lane, so two lanes never collide.
FRAGMENT_DIRNAME = "corpus.d"
DEFAULT_FRAGMENT_DIR = CORPUS_DIR / FRAGMENT_DIRNAME
SELFTEST_DIR = CORPUS_DIR / "selftest"

# Per-entry pytest budget. This is an INSTRUMENT BOUND, not an assertion about
# the product: it exists so a wedged subprocess cannot hang the corpus forever.
# The correctness bound is FIXED at the base value regardless of pool width —
# see :func:`effective_entry_timeout`. Linear scaling by workers was rejected:
# a hang that scores ``survived`` at pool=1 could score ``caught`` at pool=8
# purely from a longer window, breaking verdict determinism. Every caller must
# print the effective bound (and pool width) so the instrument is visible in
# the evidence rather than ambient.
ENTRY_TIMEOUT_DEFAULT_SECONDS = 120.0
ENTRY_TIMEOUT_ENV = "OMNIAGENTOS_CF_ENTRY_TIMEOUT"

# Bounded process-pool width for entry-vs-entry execution. Default 1 (serial) —
# pending quiet-host calibration; operators opt in via OMNIAGENTOS_CF_POOL_WORKERS.
# ``1`` means genuinely serial in-process (no ProcessPoolExecutor spun up).
# Malformed or non-positive values are REFUSED, never silently ignored — same
# discipline as ENTRY_TIMEOUT_ENV. Does NOT parallelise run_control (unpatched
# live tree). Per-entry env isolation and ``.next*`` excludes still apply at
# width=1 (copy savings and ambient scrub are independent of pooling).
POOL_WORKERS_DEFAULT = 1
POOL_WORKERS_ENV = "OMNIAGENTOS_CF_POOL_WORKERS"

# Scratch reuse (pristine tree + ``rsync --delete`` reset) is ON by default;
# ``OMNIAGENTOS_CF_SCRATCH_REUSE=0`` restores copy-per-entry. Kept as an env
# lever, not a code edit, so an operator who suspects cross-entry contamination
# can re-run the gate the slow way in one command. Malformed values are REFUSED
# (same discipline as the timeout/width envs): an operator who thought they had
# turned reuse off and had not would read the run as proof of isolation.
SCRATCH_REUSE_ENV = "OMNIAGENTOS_CF_SCRATCH_REUSE"
_TRUEY = frozenset({"1", "true", "yes", "on"})
_FALSEY = frozenset({"0", "false", "no", "off"})

# A tree missing any of these is not a checkout copy — refuse rather than use it
# as an rsync source. Guards the FAVOURABLE-ABSENCE shape: an empty pristine
# would make ``rsync -a --delete`` "succeed" by emptying the scratch.
_TREE_SENTINELS: tuple[str, ...] = (
    "pyproject.toml",
    "omniagentos/__init__.py",
    "tests/counterfeits/harness.py",
)

# Entry ``platforms`` values must come from this set. The pin exists for
# product code that genuinely cannot run elsewhere (workfs is Darwin-kernel
# renameatx_np), and a TYPO in a pin ("macos", "osx") would skip the entry on
# EVERY host — a silent coverage loss shaped exactly like favourable absence.
# Refusing unknown names at load time keeps the failure loud and immediate.
_KNOWN_PLATFORMS = frozenset({"darwin", "linux", "win32"})


def entry_timeout_default() -> float:
    """Base per-entry pytest timeout: ``$OMNIAGENTOS_CF_ENTRY_TIMEOUT`` or 120.0.

    A malformed or non-positive value is REFUSED rather than silently ignored:
    an operator who thought they had raised the bound and did not would read
    every subsequent timeout as a surviving counterfeit.
    """
    raw = os.environ.get(ENTRY_TIMEOUT_ENV, "").strip()
    if not raw:
        return ENTRY_TIMEOUT_DEFAULT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        raise CounterfeitError(f"{ENTRY_TIMEOUT_ENV}={raw!r} is not a number") from None
    if value <= 0:
        raise CounterfeitError(f"{ENTRY_TIMEOUT_ENV}={raw!r} must be > 0")
    return value


def entry_timeout_explicitly_set() -> bool:
    """True when the operator set ``OMNIAGENTOS_CF_ENTRY_TIMEOUT`` (any non-empty value)."""
    return bool(os.environ.get(ENTRY_TIMEOUT_ENV, "").strip())


def pool_workers_default() -> int:
    """Effective entry-pool width: ``$OMNIAGENTOS_CF_POOL_WORKERS`` or 1 (serial).

    Default is serial pending quiet-host calibration; operators opt in via
    ``OMNIAGENTOS_CF_POOL_WORKERS``. A malformed or non-positive value is
    REFUSED rather than silently ignored. ``1`` means serial in-process
    execution — no process pool is started.
    """
    raw = os.environ.get(POOL_WORKERS_ENV, "").strip()
    if not raw:
        return POOL_WORKERS_DEFAULT
    try:
        value = int(raw, 10)
    except ValueError:
        raise CounterfeitError(f"{POOL_WORKERS_ENV}={raw!r} is not an integer") from None
    if value <= 0:
        raise CounterfeitError(f"{POOL_WORKERS_ENV}={raw!r} must be > 0")
    return value


def scratch_reuse_enabled() -> bool:
    """True when entries reuse one pristine-reset scratch per worker (default).

    ``OMNIAGENTOS_CF_SCRATCH_REUSE=0`` forces the historical copy-per-entry
    path. An unrecognised value is REFUSED rather than treated as either
    default: "I turned reuse off" silently not happening is precisely the
    reading an operator would use to clear a contamination suspicion.
    """
    raw = os.environ.get(SCRATCH_REUSE_ENV, "").strip().lower()
    if not raw:
        return True
    if raw in _TRUEY:
        return True
    if raw in _FALSEY:
        return False
    raise CounterfeitError(
        f"{SCRATCH_REUSE_ENV}={raw!r} is not a boolean (use one of {sorted(_TRUEY | _FALSEY)})"
    )


def effective_entry_timeout(*, workers: int | None = None) -> float:
    """Per-entry pytest budget actually handed to each ``run_entry`` call.

    The correctness timeout is FIXED at the base value (``entry_timeout_default``
    / explicit ``OMNIAGENTOS_CF_ENTRY_TIMEOUT``) for every pool width. Pool
    width must not change what a hang scores as: linear scaling by workers
    (``base * N``) let a delayed catch flip from ``survived`` at width 1 to
    ``caught`` at width 8 — verdict nondeterminism, not contention relief.

    Contention headroom is deliberately NOT added here. If a quiet-host
    calibration later proves a small bounded allowance is needed, the hard
    floor is: returned value must stay in ``[base, 2*base]`` for any workers
    in [1, 1000] when the operator has not set the env explicitly. Today we
    take the safest plateau: always exactly ``base``.

    ``workers`` is accepted so call sites stay uniform, but it does not change
    the returned bound. Callers must still print pool width + this value so
    both are durable receipt evidence.
    """
    # workers is part of the public signature for call-site uniformity; the
    # bound deliberately ignores it so verdicts cannot drift with pool width.
    _ = workers if workers is not None else pool_workers_default()
    return entry_timeout_default()


# Heavy / irrelevant trees — never needed to exercise a unit of production code.
# ``.next*`` is Next.js build output (dashboard/.next can be hundreds of MB to
# multi-GB); excluding it drops per-entry scratch copy from ~GB/tens-of-seconds
# to tens-of-MB/few-seconds. rsync exclude patterns support ``*``; the shutil
# fallback uses ``name.startswith(".next")`` because set membership does not
# glob-match.
_RSYNC_EXCLUDES: tuple[str, ...] = (
    ".git",
    ".venv",
    "node_modules",
    "dashboard/node_modules",
    "var",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".next*",
    "**/__pycache__",
    "*.pyc",
    ".DS_Store",
    "uv.lock",
)

# Runtime-root env vars that must not leak from the parent into a concurrent
# scratch-tree pytest. Mirrors merge-gate.sh ``counterfeit_worker`` /
# ``suite_worker`` (scrub ambient, then re-root under the worker's own tree).
# Half-isolation — overriding only VAR_DIR while leaving VAR ambient — left
# fixtures reading the live operator tree (merge-gate 2026-08-05 defect); scrub
# every alias, then set them all from the same scratch root.
_RUNTIME_ROOT_ENV_VARS: tuple[str, ...] = (
    "OMNIAGENTOS_DB",
    "OMNIAGENTOS_VAR",
    "OMNIAGENTOS_VAR_DIR",
    "OMNIAGENTOS_LEDGER_DIR",
    "OMNIAGENTOS_VAULT_DIR",
    "OMNIAGENTOS_TMP",
    # launch-env.sh auto-exports this from any clean *-gate checkout; scrub so
    # concurrent entry workers cannot pin into a live gate workspace.
    "OMNIAGENTOS_GATE_WORKSPACE",
)


class CounterfeitError(Exception):
    """Base error for the counterfeit gate."""


class CounterfeitApplyError(CounterfeitError):
    """Patch failed to apply — bit-rotted entry, not a skip."""


class CounterfeitCollectionError(CounterfeitError):
    """must_fail nodes failed to collect; not a caught defect."""


class CounterfeitWrongFailure(CounterfeitError):
    """must_fail failed, but not for the recorded reason."""


class CounterfeitSurvived(CounterfeitError):
    """Mutation applied and the named tests stayed green — coverage is decoration."""


class CounterfeitControlError(CounterfeitError):
    """Unpatched must_fail set is not green — corpus points at a broken test."""


class CounterfeitDuplicateIdError(CounterfeitError):
    """Same id in two corpus files — one lane would silently shadow another."""


class CounterfeitPatchEscapeError(CounterfeitError):
    """A manifest's ``patch`` resolves outside the corpus tree.

    The manifest is data, and ``patch`` becomes a filesystem read, so this is a
    containment surface: without the check a fragment could name
    ``../../pyproject.toml`` or ``/etc/passwd`` and the runner would open it.
    """


def normalise_id(raw: str) -> str:
    """Collision key for a counterfeit id: surrounding space and case removed.

    Duplicate detection has to be at least as loose as human error, or it is
    decoration: ``cf-foo``, ``CF-Foo`` and ``cf-foo `` are one id as far as two
    lanes racing on the same defect class are concerned, and letting them
    coexist is exactly the silent shadowing this gate exists to stop. The
    original spelling is preserved on the entry for display and selection.
    """
    return " ".join(raw.split()).casefold()


@dataclass(frozen=True)
class CounterfeitEntry:
    id: str
    patch: str
    rationale: str
    must_fail: tuple[str, ...]
    failure_re: str
    # Empty = runs on every platform (the default, and the norm). Non-empty =
    # the mutated product code itself only exists on these ``sys.platform``
    # values; on any other host the entry is reported ``skipped_platform`` —
    # visibly, in the report and totals — never silently dropped. Values are
    # validated against _KNOWN_PLATFORMS at load time.
    platforms: tuple[str, ...] = ()
    source: Path = field(default=DEFAULT_MANIFEST, compare=False)
    # Directory ``patch`` is relative to. ``None`` means "beside ``source``",
    # which is what a plain manifest wants; fragments pass the corpus root so
    # ``patches/foo.patch`` means the same thing in a fragment and in
    # ``corpus.toml``.
    patch_base: Path | None = field(default=None, compare=False)

    @property
    def normalised_id(self) -> str:
        """Key used for collision detection; see :func:`normalise_id`."""
        return normalise_id(self.id)

    @property
    def runs_on_this_platform(self) -> bool:
        """True when this host may execute the entry (no pin, or pin matches)."""
        return not self.platforms or sys.platform in self.platforms

    @property
    def patch_root(self) -> Path:
        """Directory ``patch`` resolves against AND the containment boundary."""
        base = self.patch_base if self.patch_base is not None else self.source.parent
        return base.resolve()

    @property
    def patch_path(self) -> Path:
        return (self.patch_root / self.patch).resolve()

    @property
    def failure_pattern(self) -> re.Pattern[str]:
        return re.compile(self.failure_re, re.IGNORECASE | re.MULTILINE)


@dataclass
class EntryResult:
    entry: CounterfeitEntry
    status: (
        str
        # caught | survived | apply_error | collection_error | wrong_failure
        # | control_error | worker_error | skipped_platform
    )
    detail: str
    returncode: int | None = None
    output_excerpt: str = ""
    # Wall seconds for the whole entry (scratch acquire + apply + pytest), set
    # by run_entry on EVERY return path including apply_error and timeout.
    # ``None`` means "not measured" (hand-built result in a test), never 0.0 —
    # a zero would read as a free entry in the report totals.
    duration_s: float | None = None

    @property
    def ok(self) -> bool:
        # ``skipped_platform`` is OK by construction: the pin is explicit,
        # validated corpus data (the mutated product code does not exist on
        # this host), and the skip is printed in the report — the opposite of
        # the silent all-skipped case, which stays a collection_error.
        return self.status in ("caught", "skipped_platform")


def load_manifest(path: Path, *, patch_base: Path | None = None) -> list[CounterfeitEntry]:
    """Load ``[[counterfeit]]`` entries from a TOML manifest.

    ``patch_base`` overrides the directory each ``patch`` is resolved against;
    fragments pass the corpus root so their patch paths read identically to
    ``corpus.toml``'s. A malformed manifest is a hard error — the same
    seriousness the runner gives a patch that fails to apply.
    """
    try:
        raw = path.read_bytes()
    except OSError as exc:
        # Missing/unreadable path must become CounterfeitError (not a bare
        # FileNotFoundError) so main()'s typed handlers — and the outer
        # receipt catch-all — see a clean refusal rather than a traceback.
        raise CounterfeitError(f"{path}: unreadable manifest: {exc}") from exc
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise CounterfeitError(f"{path}: unreadable manifest: {exc}") from exc
    rows = data.get("counterfeit")
    if rows is None:
        raise CounterfeitError(f"{path}: no [[counterfeit]] tables")
    if not isinstance(rows, list) or not rows:
        raise CounterfeitError(f"{path}: [[counterfeit]] must be a non-empty array")

    entries: list[CounterfeitEntry] = []
    # normalised id -> (table index, original spelling), so a collision names
    # BOTH locations and BOTH spellings; "duplicate id somewhere in this file"
    # is not actionable when N lanes contribute.
    seen: dict[str, tuple[int, str]] = {}
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            raise CounterfeitError(f"{path}: counterfeit[{i}] is not a table")
        entry = _parse_entry(row, path=path, index=i, patch_base=patch_base)
        previous = seen.get(entry.normalised_id)
        if previous is not None:
            first_index, first_id = previous
            raise CounterfeitDuplicateIdError(
                f"{path}: duplicate counterfeit id: counterfeit[{first_index}] "
                f"{first_id!r} and counterfeit[{i}] {entry.id!r} both normalise to "
                f"{entry.normalised_id!r}"
            )
        seen[entry.normalised_id] = (i, entry.id)
        _require_patch_contained(entry, path=path, index=i)
        if not entry.patch_path.is_file():
            raise CounterfeitError(
                f"{path}: counterfeit {entry.id!r} patch missing: {entry.patch_path}"
            )
        if not entry.must_fail:
            raise CounterfeitError(f"{path}: counterfeit {entry.id!r} has empty must_fail")
        try:
            _ = entry.failure_pattern
        except re.error as exc:
            raise CounterfeitError(
                f"{path}: counterfeit {entry.id!r} bad failure_re: {exc}"
            ) from exc
        entries.append(entry)
    return entries


def _require_patch_contained(entry: CounterfeitEntry, *, path: Path, index: int) -> None:
    """Refuse a ``patch`` that resolves outside the corpus tree.

    The manifest is DATA and ``patch`` becomes a filesystem read, so a fragment
    could otherwise name ``../../pyproject.toml`` or ``/etc/passwd``.
    Containment is decided by the repo's shared inode-ancestry primitive, never
    by ``str.startswith`` — a sibling directory called ``<root>-evil`` has the
    root as a string prefix and must still be refused.
    """
    if not entry.patch.strip():
        raise CounterfeitPatchEscapeError(
            f"{path}: counterfeit[{index}] {entry.id!r} has a blank patch path"
        )
    root = entry.patch_root
    parts = inode_relative_parts(entry.patch_path, root)
    if not parts:
        # ``None`` = containment unprovable; ``()`` = the root itself, which is a
        # directory and never a patch. Both fail closed.
        raise CounterfeitPatchEscapeError(
            f"{path}: counterfeit[{index}] {entry.id!r} patch {entry.patch!r} "
            f"resolves to {entry.patch_path} which is outside the corpus tree {root}"
        )


def _parse_entry(
    row: dict[str, Any],
    *,
    path: Path,
    index: int,
    patch_base: Path | None = None,
) -> CounterfeitEntry:
    def req(key: str) -> Any:
        if key not in row:
            raise CounterfeitError(f"{path}: counterfeit[{index}] missing {key!r}")
        return row[key]

    must_fail = req("must_fail")
    if isinstance(must_fail, str):
        must_fail_t = (must_fail,)
    elif isinstance(must_fail, list) and all(isinstance(x, str) for x in must_fail):
        must_fail_t = tuple(must_fail)
    else:
        raise CounterfeitError(
            f"{path}: counterfeit[{index}].must_fail must be string or list of strings"
        )

    raw_id = str(req("id"))
    if not normalise_id(raw_id):
        raise CounterfeitError(f"{path}: counterfeit[{index}].id is blank")

    platforms_raw = row.get("platforms")
    if platforms_raw is None:
        platforms_t: tuple[str, ...] = ()
    else:
        if isinstance(platforms_raw, str):
            platforms_list = [platforms_raw]
        elif isinstance(platforms_raw, list) and all(isinstance(x, str) for x in platforms_raw):
            platforms_list = list(platforms_raw)
        else:
            raise CounterfeitError(
                f"{path}: counterfeit[{index}].platforms must be a string or list of strings"
            )
        if not platforms_list:
            # An empty LIST is refused rather than read as "everywhere": the
            # author wrote a restriction and restricted to nothing, which
            # would pin the entry off every host — loud beats silent.
            raise CounterfeitError(
                f"{path}: counterfeit[{index}].platforms is an empty list — omit the "
                f"key to run everywhere"
            )
        unknown = sorted(set(platforms_list) - _KNOWN_PLATFORMS)
        if unknown:
            raise CounterfeitError(
                f"{path}: counterfeit[{index}].platforms has unknown values {unknown} "
                f"(known: {sorted(_KNOWN_PLATFORMS)}); a typo here would silently skip "
                f"the entry on every host"
            )
        platforms_t = tuple(platforms_list)

    return CounterfeitEntry(
        id=raw_id,
        patch=str(req("patch")),
        rationale=str(req("rationale")),
        must_fail=must_fail_t,
        failure_re=str(req("failure_re")),
        platforms=platforms_t,
        source=path,
        patch_base=patch_base,
    )


def discover_fragments(fragment_dir: Path) -> list[Path]:
    """``corpus.d/*.toml`` in filename order. A missing directory yields none.

    Non-``.toml`` files (``.gitkeep``, ``README.md``) are ignored on purpose so
    the directory can exist in git without carrying a manifest.
    """
    if not fragment_dir.is_dir():
        return []
    return sorted(
        (p for p in fragment_dir.iterdir() if p.is_file() and p.suffix == ".toml"),
        key=lambda p: p.name,
    )


def load_corpus(
    manifest: Path | None = None,
    *,
    fragment_dir: Path | None = None,
) -> list[CounterfeitEntry]:
    """Base manifest + every ``corpus.d/*.toml`` fragment, as one corpus.

    Ordering is base-then-fragments-by-filename so a run is reproducible. A
    duplicate id across any two files is a hard error naming both files and both
    spellings: without it a lane could silently shadow another lane's
    counterfeit and the gate would report green while testing one fewer defect
    class. Collision is judged on the NORMALISED id (see :func:`normalise_id`),
    so ``cf-foo`` / ``CF-Foo`` / ``cf-foo `` cannot coexist — a duplicate check
    that only catches byte-exact repeats is decoration.
    """
    base = manifest or DEFAULT_MANIFEST
    corpus_root = base.parent
    fragments_at = fragment_dir if fragment_dir is not None else corpus_root / FRAGMENT_DIRNAME

    entries = load_manifest(base)
    # normalised id -> (file, table index within that file, original spelling).
    # The index is carried so the cross-file message can point at the entry the
    # same way the within-file one does; "somewhere in that file" makes a reader
    # grep, and with N contributing lanes that is the expensive part.
    defined_in: dict[str, tuple[Path, int, str]] = {
        entry.normalised_id: (base, index, entry.id) for index, entry in enumerate(entries)
    }

    for fragment in discover_fragments(fragments_at):
        fragment_entries = load_manifest(fragment, patch_base=corpus_root)
        for index, entry in enumerate(fragment_entries):
            previous = defined_in.get(entry.normalised_id)
            if previous is not None:
                first_source, first_index, first_id = previous
                raise CounterfeitDuplicateIdError(
                    f"duplicate counterfeit id {entry.normalised_id!r}: defined in "
                    f"{first_source} counterfeit[{first_index}] as {first_id!r} and in "
                    f"{fragment} counterfeit[{index}] as {entry.id!r} "
                    f"— ids must be unique, case- and whitespace-insensitively, across "
                    f"corpus.toml and every {FRAGMENT_DIRNAME}/ fragment"
                )
            defined_in[entry.normalised_id] = (fragment, index, entry.id)
        entries.extend(fragment_entries)
    return entries


def materialise_scratch(repo_root: Path | None = None) -> Path:
    """Copy the repo into a disposable tree (no nested git worktree required)."""
    root = (repo_root or REPO_ROOT).resolve()
    scratch = Path(tempfile.mkdtemp(prefix="counterfeit-scratch-"))
    cmd = ["rsync", "-a"]
    for ex in _RSYNC_EXCLUDES:
        cmd.extend(["--exclude", ex])
    cmd.extend([f"{root}/", f"{scratch}/"])
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        # rsync missing or failed — fall back to shutil with the same ignores.
        shutil.rmtree(scratch, ignore_errors=True)
        scratch = Path(tempfile.mkdtemp(prefix="counterfeit-scratch-"))
        _copytree_filtered(root, scratch)
    return scratch


def _copytree_filtered(src: Path, dst: Path) -> None:
    skip_names = {
        ".git",
        ".venv",
        "node_modules",
        "var",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "__pycache__",
        "uv.lock",
        ".DS_Store",
    }

    def _ignore(directory: str, names: list[str]) -> set[str]:
        ignored: set[str] = set()
        for name in names:
            # ``.next*`` must be an explicit prefix check: set membership does
            # not glob-match, and Next build dirs are named ``.next`` (or
            # ``.next-...`` variants) rather than a fixed skip-list token.
            if name in skip_names or name.endswith(".pyc") or name.startswith(".next"):
                ignored.add(name)
        return ignored

    shutil.copytree(src, dst, ignore=_ignore, dirs_exist_ok=True)


def destroy_scratch(scratch: Path) -> None:
    shutil.rmtree(scratch, ignore_errors=True)


# --- pristine staging + scratch leases -------------------------------------
#
# State is per PROCESS (module globals) and per THREAD (lease key carries
# ``threading.get_ident()``), because run_entries falls back to a
# ThreadPoolExecutor when SemLock is unavailable — a process-wide singleton
# scratch would then be shared by concurrent entries and destroy the isolation
# this gate is for. The pristine tree is written once and only ever read after
# that, so it IS safe to share across threads of one process.
_pristine_lock = threading.Lock()
_pristine_trees: dict[str, Path] = {}
# Newest mtime anywhere in each pristine tree — the input to the quick-check
# soundness test in :func:`acquire_scratch`. See _RESET_MTIME_SLACK_SECONDS.
_pristine_newest_mtime: dict[str, float] = {}
_lease_lock = threading.Lock()
_scratch_leases: dict[tuple[str, int], Path] = {}
# Wall time each lease was last handed to an entry: the earliest moment anything
# could have written into that scratch.
_lease_acquired_at: dict[tuple[str, int], float] = {}
_cleanup_registered = False

# macOS ships **openrsync** ("rsync version 2.6.9 compatible"), whose quick
# check compares mtimes at WHOLE-SECOND granularity. Verified on this host: a
# same-size edit whose mtime differs from the source by 11ms is NOT transferred
# by ``rsync -a --delete`` — the reset silently leaves the mutation in place.
# Same size + same integer second is a real (if narrow) way for entry N's patch
# to survive into entry N+1, and a same-size patch is ordinary (``!=`` -> ``==``).
#
# The quick check is sound whenever every write into the scratch happened in a
# strictly later second than every pristine mtime, which is the normal case:
# pristine mtimes are checkout-old and staging alone costs seconds. So instead
# of paying ``--checksum`` on every reset (measured 1.24s vs 0.69s here — it
# would drop the lane's 3.05x to 1.70x), :func:`acquire_scratch` PROVES the
# precondition per reset and falls back to ``--checksum`` when it cannot.
# Direction of failure is correctness: unproven means slow, never dirty.
_RESET_MTIME_SLACK_SECONDS = 2.0


def _assert_tree_populated(tree: Path, *, what: str) -> None:
    """Refuse a tree that is not recognisably a checkout copy.

    Without this, an empty or half-built pristine is a *favourable* input to
    ``rsync -a --delete``: the reset "succeeds" and empties the scratch, so
    every entry then fails as a collection error rather than as the loud
    instrument failure it actually is.
    """
    missing = [s for s in _TREE_SENTINELS if not (tree / s).exists()]
    if missing:
        raise CounterfeitError(
            f"{what} at {tree} is missing {missing} — refusing to use it as a "
            f"scratch source (an empty source with rsync --delete would empty "
            f"the scratch and read as a corpus verdict)"
        )


def _register_scratch_cleanup() -> None:
    global _cleanup_registered
    if not _cleanup_registered:
        atexit.register(reset_scratch_pool)
        _cleanup_registered = True


def _newest_mtime(tree: Path) -> float:
    """Newest mtime anywhere under ``tree`` (one lstat walk, ~30ms for 5.1k paths)."""
    newest = 0.0
    for path in tree.rglob("*"):
        try:
            mtime = path.lstat().st_mtime
        except OSError:  # vanished mid-walk: cannot be a quick-check hazard
            continue
        if mtime > newest:
            newest = mtime
    return newest


def _stage_pristine(repo_root: Path) -> Path:
    """One read-only pristine copy of ``repo_root`` per process. Never patched."""
    key = str(repo_root)
    with _pristine_lock:
        existing = _pristine_trees.get(key)
        if existing is not None and existing.is_dir():
            return existing
        tree = materialise_scratch(repo_root)
        try:
            _assert_tree_populated(tree, what="pristine tree")
        except CounterfeitError:
            destroy_scratch(tree)
            raise
        _pristine_trees[key] = tree
        # Recorded BEFORE any entry runs, and never recomputed: the pristine is
        # read-only, so this bound only ever gets safer as wall time advances.
        _pristine_newest_mtime[key] = _newest_mtime(tree)
        _register_scratch_cleanup()
        return tree


def _reset_scratch_from_pristine(pristine: Path, scratch: Path, *, quick_ok: bool) -> bool:
    """``rsync -a --delete pristine/ scratch/``. True only on a clean reset.

    Deliberately passes NO ``--exclude``: excluded paths are protected from
    ``--delete`` on the receiver, so any exclude here would let the previous
    entry's patch output or runtime residue survive into the next entry. The
    exclude list belongs to :func:`materialise_scratch` (what gets copied out of
    the live checkout), not to the reset.

    ``quick_ok=False`` adds ``--checksum``, which compares content instead of
    (size, whole-second mtime). The caller decides; see
    :data:`_RESET_MTIME_SLACK_SECONDS` for why the default quick check can miss
    a same-size edit and when it provably cannot.
    """
    cmd = ["rsync", "-a", "--delete"]
    if not quick_ok:
        cmd.append("--checksum")
    proc = subprocess.run(
        [*cmd, f"{pristine}/", f"{scratch}/"],
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0


def acquire_scratch(repo_root: Path | None = None) -> Path:
    """Scratch tree for one entry: reset-from-pristine, else a fresh full copy.

    Reuse is per (repo_root, process, thread). The returned tree is always
    equivalent to a fresh :func:`materialise_scratch` copy — modified files
    restored, deleted files re-created, added files and runtime residue removed.
    Any doubt (bad pristine, non-zero rsync, vanished scratch) falls back to a
    genuine fresh copy: slower, never dirtier.

    "Equivalent" is enforced, not hoped for. ``rsync``'s default quick check
    skips a file whose size and whole-second mtime match the source, so it can
    miss a same-size edit made within a second of the pristine's mtime. This
    function only uses the quick check when it can PROVE that window is closed —
    the previous entry got this tree more than
    :data:`_RESET_MTIME_SLACK_SECONDS` after the newest mtime in the pristine,
    so every write it could have made is in a strictly later second. When it
    cannot prove that (a freshly-written source tree, a clock that moved), the
    reset runs with ``--checksum`` instead: ~2x the reset cost, still cheaper
    than a full copy, and content-exact.
    """
    root = (repo_root or REPO_ROOT).resolve()
    if not scratch_reuse_enabled():
        return materialise_scratch(root)

    pristine = _stage_pristine(root)
    key = (str(root), threading.get_ident())
    with _lease_lock:
        scratch = _scratch_leases.get(key)
        fresh_lease = scratch is None or not scratch.is_dir()
        if fresh_lease:
            scratch = Path(tempfile.mkdtemp(prefix="counterfeit-scratch-"))
            _scratch_leases[key] = scratch
        # Earliest instant anything could have written into this scratch: when
        # the previous entry was handed the tree.
        wrote_after = _lease_acquired_at.get(key)
        _register_scratch_cleanup()

    # Quick check is sound only if every possible write into the scratch landed
    # in a strictly later second than every pristine mtime — otherwise a
    # same-size edit is invisible to openrsync (see _RESET_MTIME_SLACK_SECONDS).
    # Note the direction on MISSING bookkeeping: an absent acquire time or an
    # absent pristine mtime means "we do not know when this tree was written",
    # which must resolve to --checksum. Reading absence as soundness is the
    # favourable-absence shape — the unknown case is the one most likely to be
    # dirty, and it is also the cheapest to be wrong about.
    newest = _pristine_newest_mtime.get(str(root))
    if fresh_lease:
        quick_ok = True  # brand new empty dir: nothing present to be skipped
    else:
        quick_ok = (
            wrote_after is not None
            and newest is not None
            and wrote_after > newest + _RESET_MTIME_SLACK_SECONDS
        )

    if not _reset_scratch_from_pristine(pristine, scratch, quick_ok=quick_ok):
        # Half-reset trees are the dangerous state: drop the lease entirely and
        # pay for a fresh copy rather than run an entry on an unknown tree.
        with _lease_lock:
            if _scratch_leases.get(key) == scratch:
                del _scratch_leases[key]
            _lease_acquired_at.pop(key, None)
        destroy_scratch(scratch)
        return materialise_scratch(root)
    with _lease_lock:
        _lease_acquired_at[key] = time.time()

    _assert_tree_populated(scratch, what="reset scratch tree")
    return scratch


def release_scratch(scratch: Path, *, repo_root: Path | None = None) -> None:
    """Counterpart to :func:`acquire_scratch`; destroys only non-leased trees.

    A leased tree is left dirty on purpose — the next :func:`acquire_scratch`
    resets it from the pristine, so a crashed run never leaves work half-done
    that a later run would trust.
    """
    root = (repo_root or REPO_ROOT).resolve()
    key = (str(root), threading.get_ident())
    with _lease_lock:
        leased = _scratch_leases.get(key)
    if leased is not None and leased == scratch:
        return
    destroy_scratch(scratch)


def poison_scratch_lease(scratch: Path | None, *, repo_root: Path | None = None) -> None:
    """Mark this worker's leased scratch UNUSABLE and destroy it.

    Called instead of :func:`release_scratch` when an entry ends abnormally
    (see :func:`_run_entry_body`). The reset path is deliberately NOT trusted
    here, because after a timeout the tree's *contents* are not the only
    unknown:

    * ``subprocess.run(timeout=...)`` kills the pytest process it started, but
      not that process's children. Anything the entry spawned can still be
      writing into the tree after :func:`run_entry` has returned.
    * ``rsync -a --delete`` restores content; it cannot evict a live writer, and
      a write that lands *after* the reset is exactly the silent contamination
      of entry N+1 this gate exists to prevent.

    So the lease is dropped and the directory removed: the next
    :func:`acquire_scratch` on this worker mints a brand new temp dir and
    populates it from the pristine in full, and any orphan keeps writing into a
    path nothing will ever read. Cost is one full copy, on abnormal exits only.

    Idempotent, and safe for a tree that was never leased (reuse disabled, or
    the fresh-copy fallback): that degenerates to :func:`destroy_scratch`.
    ``scratch=None`` drops whatever this worker holds without naming it.
    """
    root = (repo_root or REPO_ROOT).resolve()
    key = (str(root), threading.get_ident())
    with _lease_lock:
        leased = _scratch_leases.get(key)
        if leased is not None and (scratch is None or leased == scratch):
            del _scratch_leases[key]
            _lease_acquired_at.pop(key, None)
    if scratch is not None:
        destroy_scratch(scratch)
    elif leased is not None:
        destroy_scratch(leased)


def reset_scratch_pool() -> None:
    """Drop and delete every pristine/leased tree owned by this process."""
    with _lease_lock:
        leases = list(_scratch_leases.values())
        _scratch_leases.clear()
        _lease_acquired_at.clear()
    with _pristine_lock:
        pristines = list(_pristine_trees.values())
        _pristine_trees.clear()
        _pristine_newest_mtime.clear()
    for tree in leases + pristines:
        destroy_scratch(tree)


def apply_patch(scratch: Path, patch_path: Path) -> None:
    """Apply a unified diff inside ``scratch``. Failed apply is a hard error."""
    proc = subprocess.run(
        ["git", "apply", "--verbose", "--whitespace=nowarn", str(patch_path)],
        cwd=scratch,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        # Retry with plain patch(1) for environments where git apply is picky.
        proc2 = subprocess.run(
            ["patch", "-p1", "--forward", "--batch", "-i", str(patch_path)],
            cwd=scratch,
            capture_output=True,
            text=True,
        )
        if proc2.returncode != 0:
            raise CounterfeitApplyError(
                f"patch {patch_path.name} failed to apply\n"
                f"git apply: rc={proc.returncode}\n{proc.stdout}\n{proc.stderr}\n"
                f"patch: rc={proc2.returncode}\n{proc2.stdout}\n{proc2.stderr}"
            )


def _python_bin(repo_root: Path) -> str:
    override = (os.environ.get("OMNIAGENTOS_PYTHON") or "").strip()
    if override and os.path.isfile(override) and os.access(override, os.X_OK):
        return override
    venv = repo_root / ".venv" / "bin" / "python"
    if venv.is_file() and os.access(venv, os.X_OK):
        return str(venv)
    # Worktree checkouts often share the product venv; honour it when present.
    git_common = subprocess.run(
        ["git", "rev-parse", "--git-common-dir"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if git_common.returncode == 0:
        common = Path(git_common.stdout.strip())
        if not common.is_absolute():
            common = (repo_root / common).resolve()
        # common-dir is <product>/.git → product root is parent
        product_venv = common.parent / ".venv" / "bin" / "python"
        if product_venv.is_file() and os.access(product_venv, os.X_OK):
            return str(product_venv)
    return sys.executable


def _apply_isolated_runtime_env(env: dict[str, str], tree: Path) -> None:
    """Scrub ambient runtime roots and re-root them under ``tree``.

    Concurrent entry workers each own a scratch tree; inheriting the parent's
    ``OMNIAGENTOS_DB`` / ``VAR`` / ``LEDGER_DIR`` / ``VAULT_DIR`` would make
    every worker write the same sqlite/ledger/vault paths. Scrub-then-set (not
    just set): an ambient alias that survived while only one name was overridden
    is the half-isolation defect merge-gate already paid for once.
    """
    for key in _RUNTIME_ROOT_ENV_VARS:
        env.pop(key, None)
    # TMPDIR is not Omni-namespaced but is equally racy under concurrent
    # pytest tempfiles; pin it under the scratch so workers cannot collide
    # on the operator's ambient temp dir either.
    env.pop("TMPDIR", None)
    var_root = tree / "var" / "counterfeit-entry"
    tmp_root = tree / "tmp"
    # Create roots BEFORE the pytest subprocess: Python's tempfile rejects a
    # nonexistent TMPDIR candidate and silently falls back to the shared host
    # temp dir, which would make the claimed isolation a no-op. ledger/vault
    # parents must exist for the same reason when code opens files under them.
    var_root.mkdir(parents=True, exist_ok=True)
    (var_root / "ledger").mkdir(parents=True, exist_ok=True)
    (var_root / "vault").mkdir(parents=True, exist_ok=True)
    tmp_root.mkdir(parents=True, exist_ok=True)
    env["OMNIAGENTOS_DB"] = str(var_root / "state.sqlite3")
    env["OMNIAGENTOS_VAR"] = str(var_root)
    env["OMNIAGENTOS_VAR_DIR"] = str(var_root)
    env["OMNIAGENTOS_LEDGER_DIR"] = str(var_root / "ledger")
    env["OMNIAGENTOS_VAULT_DIR"] = str(var_root / "vault")
    env["OMNIAGENTOS_TMP"] = str(tmp_root)
    env["TMPDIR"] = str(tmp_root)


def run_pytest_nodes(
    *,
    tree: Path,
    nodes: Sequence[str],
    repo_root: Path | None = None,
    timeout: float = 120.0,
    isolate_runtime: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run specific pytest node ids against ``tree`` (PYTHONPATH=tree).

    ``isolate_runtime=True`` scrubs ambient OMNIAGENTOS_* runtime roots and
    re-roots them under ``tree`` — required for concurrent ``run_entry``
    workers so two scratches never share one sqlite/ledger/vault. ``run_control``
    leaves this False: it runs alone against the unpatched live tree and
    should keep ambient-env behaviour.
    """
    root = (repo_root or REPO_ROOT).resolve()
    python = _python_bin(root)
    env = os.environ.copy()
    # Mutated tree must win over any editable install of the main checkout.
    env["PYTHONPATH"] = str(tree) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    env["PYTHONSAFEPATH"] = "1"
    # Keep the gate free of network / live / wall-clock suites. "Not simulating"
    # is spelled by ABSENCE: omniagentos.simgate refuses any OMNIAGENTOS_SIM_MODE
    # that is set to anything but "1", so the old "0" placeholder now makes every
    # gate subprocess fail closed. Popping also stops an ambient sim env in the
    # operator's shell from leaking into the gate.
    env.pop("OMNIAGENTOS_SIM_MODE", None)
    # THE INTERPRETER IS A TEST PREMISE (2026-08-08). Corpus entries name
    # tests/scripts/test_merge_gate_*.py, and those meta-tests spawn nested
    # `bash merge-gate.sh` runs whose whole design is that the fixture's fake
    # python answers instantly. MERGE_GATE_PY is the FIRST candidate in that
    # script's interpreter resolution, so an ambient value (the production gate
    # exports one) overrode the stub, the nested gate ran the REAL corpus, and
    # each generation spawned more — caught live at 13+ generations. Measured on
    # one node: 2.14s scrubbed vs 180.44s inherited, an 84x blow-up, and the
    # 180s is pytest-timeout, not the work finishing.
    #
    # merge-gate.sh's own workers scrub this too; this is the second, independent
    # layer, because the harness is also reachable from `make counterfeit-gate`
    # and from a bare operator shell that never passed through the gate.
    env.pop("MERGE_GATE_PY", None)
    env.pop("MERGE_GATE_PINNED", None)
    if isolate_runtime:
        _apply_isolated_runtime_env(env, tree)
    # pytest cache under the scratch would be fine; disable for speed/noise.
    cmd = [
        python,
        "-m",
        "pytest",
        *nodes,
        "-q",
        "--tb=short",
        "-p",
        "no:cacheprovider",
        "--no-header",
    ]
    return subprocess.run(
        cmd,
        cwd=tree,
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
    )


_COLLECTION_MARKERS = (
    "error collecting",
    "errors during collection",
    "import error",
    "importerror",
    "not found",
    "no tests collected",
    "usageerror",
    "module not found",
)


def _is_collection_failure(proc: subprocess.CompletedProcess[str]) -> bool:
    if proc.returncode in {4, 5}:
        return True
    blob = f"{proc.stdout}\n{proc.stderr}".lower()
    if proc.returncode != 0 and any(m in blob for m in _COLLECTION_MARKERS):
        # Distinguish "test failed with assertion mentioning 'not found'" from
        # real collection problems: collection failures almost always print
        # ERROR/ERROR collecting before any test phase.
        if "error collecting" in blob or "errors during collection" in blob:
            return True
        if "no tests collected" in blob:
            return True
        if re.search(r"not found:\s*\S+", blob) and "FAILED" not in proc.stdout:
            return True
        if "importerror" in blob or "module not found" in blob:
            # Only treat as collection if no tests ran.
            if " passed" not in proc.stdout and " failed" not in proc.stdout:
                return True
    return False


def _executed_no_tests(proc: subprocess.CompletedProcess[str]) -> bool:
    """Return whether a rc==0 run executed zero tests because all were skipped.

    pytest exits 0 both when every named node passed and when every named node
    was *skipped*, so ``returncode`` alone cannot tell "the guard held" from
    "the guard was never asked". A ``must_fail`` set behind a module-level
    ``skipif`` (platform, optional dependency, missing service) therefore reads
    as green to :func:`run_control` and as ``survived`` to
    :func:`evaluate_mutated` — the same verdicts a real regression produces.

    Judged on the summary line rather than on the ``s`` progress characters,
    which also appear for a partially-skipped run that did execute tests.
    """
    if proc.returncode != 0:
        return False
    blob = f"{proc.stdout}\n{proc.stderr}"
    if re.search(r"\b\d+ (?:passed|failed|error|errors|xpassed|xfailed)\b", blob):
        return False
    return bool(re.search(r"\b\d+ (?:skipped|deselected)\b", blob))


def _excerpt(proc: subprocess.CompletedProcess[str], limit: int = 2500) -> str:
    blob = (proc.stdout or "") + "\n" + (proc.stderr or "")
    blob = blob.strip()
    if len(blob) > limit:
        return blob[: limit // 2] + "\n…\n" + blob[-limit // 2 :]
    return blob


def evaluate_mutated(
    entry: CounterfeitEntry,
    proc: subprocess.CompletedProcess[str],
) -> EntryResult:
    """Interpret a mutated-tree pytest result for one corpus entry."""
    excerpt = _excerpt(proc)
    if _is_collection_failure(proc):
        return EntryResult(
            entry=entry,
            status="collection_error",
            detail=(
                f"must_fail nodes failed to collect under mutation "
                f"(rc={proc.returncode}); collection errors are not catches"
            ),
            returncode=proc.returncode,
            output_excerpt=excerpt,
        )

    if _executed_no_tests(proc):
        # Not a survival: the named coverage was never asked. Reported as a
        # collection error because it is the same family — no test executed —
        # and `assert_entry_caught` already maps that to a hard failure.
        return EntryResult(
            entry=entry,
            status="collection_error",
            detail=(
                "must_fail nodes executed ZERO tests under mutation (all skipped/deselected, "
                f"rc={proc.returncode}); a guard that was never asked is an instrument error, "
                "not a verdict about the code"
            ),
            returncode=proc.returncode,
            output_excerpt=excerpt,
        )

    if proc.returncode == 0:
        return EntryResult(
            entry=entry,
            status="survived",
            detail=(
                "counterfeit SURVIVED: must_fail nodes stayed green after mutation "
                "— the named coverage is decoration"
            ),
            returncode=0,
            output_excerpt=excerpt,
        )

    blob = f"{proc.stdout}\n{proc.stderr}"
    if not entry.failure_pattern.search(blob):
        return EntryResult(
            entry=entry,
            status="wrong_failure",
            detail=(
                f"must_fail failed (rc={proc.returncode}) but output did not match "
                f"failure_re={entry.failure_re!r}; unrelated failures do not count"
            ),
            returncode=proc.returncode,
            output_excerpt=excerpt,
        )

    return EntryResult(
        entry=entry,
        status="caught",
        detail=f"caught: must_fail went red for the recorded reason (rc={proc.returncode})",
        returncode=proc.returncode,
        output_excerpt=excerpt,
    )


def run_entry(
    entry: CounterfeitEntry,
    *,
    repo_root: Path | None = None,
    timeout: float | None = None,
) -> EntryResult:
    """Apply mutation → run must_fail → assert red for the recorded reason.

    ``timeout=None`` resolves through :func:`entry_timeout_default`, so the
    hardware-dependent instrument bound has ONE definition instead of one per
    call site. Top-level (not a closure) so :class:`ProcessPoolExecutor` can
    pickle it as a worker target.

    Wraps :func:`_run_entry_body` purely to stamp ``duration_s`` on EVERY
    returned result — apply_error and timeout included. Timing the body from
    inside would need one ``perf_counter`` diff per return statement, and the
    one that got missed would report as ``None`` (unmeasured) forever.
    """
    started = time.perf_counter()
    result = _run_entry_body(entry, repo_root=repo_root, timeout=timeout)
    return replace(result, duration_s=time.perf_counter() - started)


def _run_entry_body(
    entry: CounterfeitEntry,
    *,
    repo_root: Path | None = None,
    timeout: float | None = None,
) -> EntryResult:
    """Untimed body of :func:`run_entry` — see it for contract.

    A ``subprocess.TimeoutExpired`` is NOT a catch: it scores as an
    uncaught/survived signal so the gate goes red rather than treating a hung
    subprocess as coverage.

    SCRATCH DISPOSAL depends on how the entry ended, and the three cases are
    enumerated here rather than left to one blanket ``release``:

    * normal return (caught / survived / wrong_failure / collection_error) —
      :func:`release_scratch`; the lease survives and the next entry resets it.
    * ``apply_error`` — also :func:`release_scratch`. ``git apply`` is atomic and
      the ``patch(1)`` fallback is not, so the tree may be half-patched, but
      that is a pure CONTENT difference and ``rsync -a --delete`` provably
      restores content (5,142-path byte manifest). No process is left running,
      so paying for a fresh copy here would buy nothing.
    * ``TimeoutExpired``, or ANY exception escaping this body — the tree's state
      is unknown in a way the reset cannot fix (a killed pytest's children are
      not killed with it and can still be writing), so the lease is POISONED:
      :func:`poison_scratch_lease` drops it and the next entry on this worker
      materialises a fresh tree.
    """
    root = (repo_root or REPO_ROOT).resolve()
    if timeout is None:
        timeout = entry_timeout_default()
    if not entry.runs_on_this_platform:
        # Before any scratch work: the mutated product code does not exist on
        # this host (explicit, load-validated pin), so running the entry could
        # only produce the all-skipped instrument error. Reported, never
        # silent — format_report prints it and totals count it.
        return EntryResult(
            entry=entry,
            status="skipped_platform",
            detail=(
                f"entry is pinned to platforms {list(entry.platforms)} and this host "
                f"is {sys.platform!r}; the mutated product code cannot run here — "
                f"skipped by explicit corpus pin, verified on a pinned-platform host"
            ),
        )
    scratch: Path | None = None
    poisoned = False
    try:
        scratch = acquire_scratch(root)
        try:
            apply_patch(scratch, entry.patch_path)
        except CounterfeitApplyError as exc:
            return EntryResult(
                entry=entry,
                status="apply_error",
                detail=str(exc),
            )
        try:
            proc = run_pytest_nodes(
                tree=scratch,
                nodes=entry.must_fail,
                repo_root=root,
                timeout=timeout,
                isolate_runtime=True,
            )
        except subprocess.TimeoutExpired as exc:
            # Instrument-bound exhaustion, not a product catch. Status is
            # ``survived`` so it fails the gate the same way an uncaught
            # counterfeit does (and so ``--allow-survived`` report-only mode
            # is the only soft-pass path — a deliberate hang is never "caught").
            #
            # The lease dies with the entry: handing this tree to the next entry
            # would hand it a directory that a surviving grandchild of the timed
            # out pytest may still be writing to. See poison_scratch_lease.
            poisoned = True
            stdout = exc.stdout or ""
            stderr = exc.stderr or ""
            if isinstance(stdout, bytes):
                stdout = stdout.decode("utf-8", errors="replace")
            if isinstance(stderr, bytes):
                stderr = stderr.decode("utf-8", errors="replace")
            blob = f"{stdout}\n{stderr}".strip()
            excerpt = blob if len(blob) <= 2500 else blob[:1250] + "\n…\n" + blob[-1250:]
            return EntryResult(
                entry=entry,
                status="survived",
                detail=(
                    f"entry timed out after {timeout:.1f}s; instrument bound "
                    f"exhausted — timeout is not a catch (treated as "
                    f"uncaught/survived signal)"
                ),
                returncode=None,
                output_excerpt=excerpt,
            )
        return evaluate_mutated(entry, proc)
    except BaseException:
        # SIBLING of the timeout path, same reasoning: an OSError spawning
        # pytest, a MemoryError, a KeyboardInterrupt or a bug in
        # evaluate_mutated all leave this tree in a state nobody has inspected,
        # and _run_entry_job turns the exception into a ``worker_error`` and
        # keeps the run going on this same worker/thread. Fixing only the
        # TimeoutExpired branch would leave that hole open.
        poisoned = True
        raise
    finally:
        if scratch is not None:
            if poisoned:
                poison_scratch_lease(scratch, repo_root=root)
            else:
                release_scratch(scratch, repo_root=root)


def run_control(
    entries: Sequence[CounterfeitEntry],
    *,
    repo_root: Path | None = None,
    timeout: float = 300.0,
) -> None:
    """Unpatched union of runnable entries' must_fail node ids must be green.

    Platform-pinned entries that cannot run on this host are excluded from the
    union — their nodes would only skip here, and an all-skipped node in the
    control proves nothing about the entries that DO run. If every entry is
    pinned away the control refuses loudly: a corpus with nothing runnable is
    an instrument error, not a green.
    """
    root = (repo_root or REPO_ROOT).resolve()
    nodes: list[str] = []
    seen: set[str] = set()
    pinned_away = 0
    for entry in entries:
        if not entry.runs_on_this_platform:
            pinned_away += 1
            continue
        for node in entry.must_fail:
            if node not in seen:
                seen.add(node)
                nodes.append(node)
    if not nodes:
        if pinned_away:
            raise CounterfeitControlError(
                f"no runnable must_fail nodes on this host ({sys.platform!r}): all "
                f"{pinned_away} selected entries are platform-pinned elsewhere — the "
                f"corpus cannot be verified here at all, which is an instrument "
                f"error, not a green control"
            )
        raise CounterfeitControlError("no must_fail nodes in corpus")

    # Run against the live tree so control reflects HEAD, not a stale copy.
    try:
        proc = run_pytest_nodes(tree=root, nodes=nodes, repo_root=root, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        # A hung control is an instrument failure, not a bare traceback: wrap
        # so main()'s CounterfeitControlError handler (and the outer receipt
        # catch-all) still emit the durable receipt line.
        raise CounterfeitControlError(
            f"control (unpatched) timed out after {timeout:.1f}s — instrument "
            f"bound exhausted, not a corpus verdict\n{exc}"
        ) from exc
    if proc.returncode != 0:
        raise CounterfeitControlError(
            "control (unpatched) must_fail set is not green — corpus points at "
            f"broken or missing tests (rc={proc.returncode})\n{_excerpt(proc, 4000)}"
        )
    if _executed_no_tests(proc):
        # `returncode == 0` is the same integer for "all passed" and "all
        # skipped", so without this the control reports green for a corpus
        # whose must_fail nodes cannot run here at all — exactly the
        # already-broken-test case the control exists to catch.
        raise CounterfeitControlError(
            "control (unpatched) must_fail set executed ZERO tests (all "
            "skipped/deselected) — the corpus cannot be verified on this host, "
            f"which is an instrument error, not a green control\n{_excerpt(proc, 4000)}"
        )


def _run_entry_job(
    entry: CounterfeitEntry,
    repo_root: str | None,
    timeout: float,
) -> EntryResult:
    """Process-pool worker target for one corpus entry.

    Must stay a top-level function (picklable under spawn). Converts unexpected
    exceptions into a hard per-entry failure so a dead worker never disappears
    from the result list silently — the parent also re-checks for empty slots.
    """
    root = Path(repo_root) if repo_root is not None else None
    try:
        return run_entry(entry, repo_root=root, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 — worker boundary: never silent-skip
        return EntryResult(
            entry=entry,
            status="worker_error",
            detail=(
                f"entry worker raised {type(exc).__name__}: {exc} "
                f"— worker produced no usable status (hard failure, not a catch)"
            ),
        )


def run_entries(
    entries: Sequence[CounterfeitEntry],
    *,
    repo_root: Path | None = None,
    entry_timeout: float | None = None,
    workers: int | None = None,
) -> list[EntryResult]:
    """Run corpus entries with a bounded process pool; preserve input order.

    ``workers=1`` is genuinely serial in-process (no pool). ``workers>1`` uses
    :class:`ProcessPoolExecutor`. Results are reassembled in ``entries`` order
    regardless of completion order. A worker that raises, is killed, or returns
    nothing yields a hard per-entry failure — never a silent skip. Final result
    count always equals ``len(entries)``.

    Does NOT run :func:`run_control`; callers that need the unpatched green
    pass must call it first, serially, on the live tree.

    CALLER TRAP — ``workers > 1`` is spawn-unsafe without a main guard. macOS
    defaults multiprocessing to ``spawn``, so every child re-imports the calling
    module; a script that calls this at import scope re-enters itself in each
    child and the pool dies with ``BrokenProcessPool`` before any entry runs.
    Callers of the API must put their call under ``if __name__ == "__main__":``
    (this module's own CLI already does). ``workers == 1`` is unaffected — it is
    genuinely serial and starts no pool.

    Each worker keeps its own reused scratch (see :func:`acquire_scratch`);
    leases are per (repo_root, process, thread), so the ThreadPoolExecutor
    fallback below does not collapse N entries onto one tree.
    """
    if workers is None:
        workers = pool_workers_default()
    if workers <= 0:
        raise CounterfeitError(f"pool workers must be > 0, got {workers}")
    if entry_timeout is None:
        entry_timeout = effective_entry_timeout(workers=workers)
    if not entries:
        return []

    # Serial path: no ProcessPoolExecutor, no pickle, deterministic and cheap
    # for operators who set OMNIAGENTOS_CF_POOL_WORKERS=1.
    if workers == 1:
        return [run_entry(entry, repo_root=repo_root, timeout=entry_timeout) for entry in entries]

    root_s = str((repo_root or REPO_ROOT).resolve())
    results: list[EntryResult | None] = [None] * len(entries)

    # Prefer ProcessPoolExecutor (true process isolation). Some restricted
    # environments refuse SemLock creation (PermissionError); fall back to a
    # thread pool, which is equivalent HERE because each entry already owns a
    # scratch tree and runs pytest in a subprocess with a scrubbed env dict —
    # the concurrency race is I/O and subprocess env, not shared Python heap.
    executor: ProcessPoolExecutor | ThreadPoolExecutor
    try:
        executor = ProcessPoolExecutor(max_workers=workers)
    except PermissionError:
        print(
            "counterfeit gate: ProcessPoolExecutor unavailable "
            "(SemLock PermissionError); falling back to ThreadPoolExecutor "
            f"(workers={workers}) — scratch/env isolation still per-entry",
            file=sys.stderr,
        )
        executor = ThreadPoolExecutor(max_workers=workers)

    try:
        with executor:
            future_to_idx: dict[Future[EntryResult], int] = {
                executor.submit(_run_entry_job, entry, root_s, entry_timeout): idx
                for idx, entry in enumerate(entries)
            }
            for fut in as_completed(future_to_idx):
                idx = future_to_idx[fut]
                try:
                    results[idx] = fut.result()
                except BrokenProcessPool:
                    # Must propagate: the outer handler raises a hard
                    # CounterfeitError for the WHOLE pool. Catching this under
                    # ``except Exception`` would silently downgrade a dead pool
                    # into a single-entry worker_error and hide the failure mode.
                    raise
                except Exception as exc:  # noqa: BLE001 — surface, never drop
                    results[idx] = EntryResult(
                        entry=entries[idx],
                        status="worker_error",
                        detail=(f"entry worker produced no result: {type(exc).__name__}: {exc}"),
                    )
    except BrokenProcessPool as exc:
        # Whole process pool unusable — hard refusal, not a partial silent report.
        raise CounterfeitError(
            f"counterfeit process pool broke before all entries finished: {exc}"
        ) from exc

    # Hard refusal slots: a missing status is never a silent skip.
    out: list[EntryResult] = []
    for idx, result in enumerate(results):
        if result is None:
            out.append(
                EntryResult(
                    entry=entries[idx],
                    status="worker_error",
                    detail="entry worker produced no status/result at all",
                )
            )
        else:
            out.append(result)
    if len(out) != len(entries):
        raise CounterfeitError(
            f"entry result count {len(out)} != entries {len(entries)} — pool assembly bug, refusing"
        )
    return out


def run_corpus(
    manifest: Path | None = None,
    *,
    repo_root: Path | None = None,
    run_control_first: bool = True,
    entry_timeout: float | None = None,
    workers: int | None = None,
) -> list[EntryResult]:
    """Run every corpus entry (base manifest + fragments). Survival is the caller's call.

    ``run_control`` (unpatched live tree) stays serial and runs first when
    ``run_control_first`` is True. Entry-vs-entry execution goes through
    :func:`run_entries` (bounded process pool). Ladder-vs-gate ordering in
    merge-gate.sh is out of scope here.
    """
    if workers is None:
        workers = pool_workers_default()
    if entry_timeout is None:
        entry_timeout = effective_entry_timeout(workers=workers)
    entries = load_corpus(manifest)
    if run_control_first:
        run_control(entries, repo_root=repo_root)
    return run_entries(
        entries,
        repo_root=repo_root,
        entry_timeout=entry_timeout,
        workers=workers,
    )


def assert_entry_caught(result: EntryResult) -> None:
    """Raise the appropriate error if the entry did not catch its counterfeit."""
    if result.ok:
        return
    mapping = {
        "survived": CounterfeitSurvived,
        "apply_error": CounterfeitApplyError,
        "collection_error": CounterfeitCollectionError,
        "wrong_failure": CounterfeitWrongFailure,
        "control_error": CounterfeitControlError,
    }
    exc_cls = mapping.get(result.status, CounterfeitError)
    raise exc_cls(f"[{result.entry.id}] {result.detail}\n{result.output_excerpt}")


# Sentinel for receipt fields that could not be resolved (config parse failed
# before workers/timeout were known). Must never look like a real integer or
# second count — a blank/omitted line is also forbidden on terminal paths.
RECEIPT_UNRESOLVED = "unresolved"


def format_receipt_line(
    workers: int | str | None = None,
    entry_timeout: float | str | None = None,
) -> str:
    """Pretty receipt line for protected/happy paths (format_report, typed refuse).

    NOT used by the outer emergency handler in :func:`main` — that path must not
    depend on this function (or on ``:.1f`` float formatting). See
    :func:`format_emergency_receipt_line` / :func:`emit_emergency_receipt`.
    """
    if workers is None:
        w_s = RECEIPT_UNRESOLVED
    elif isinstance(workers, str):
        w_s = workers
    else:
        w_s = str(workers)
    if entry_timeout is None:
        t_s = RECEIPT_UNRESOLVED
    elif isinstance(entry_timeout, str):
        # Explicit sentinel / preformatted string — do not append "s".
        t_s = entry_timeout
    else:
        t_s = f"{entry_timeout:.1f}s"
    return f"pool_workers={w_s}  entry_timeout={t_s}"


def format_emergency_receipt_line(
    workers: object = RECEIPT_UNRESOLVED,
    entry_timeout: object = RECEIPT_UNRESOLVED,
) -> str:
    """Structurally fail-safe receipt for main()'s outer ``except Exception`` only.

    A fallback that depends on the thing that just failed is not a fallback:
    this must NOT call :func:`format_receipt_line` / :func:`format_report` and
    must not use ``:.1f`` (a non-float binding would raise). Only ``repr()`` on
    the current bindings plus string concatenation — neither raises on int,
    float, str, or the ``unresolved`` sentinel.
    """
    # Concatenation + repr: no format mini-language, no helper calls.
    return "pool_workers=" + repr(workers) + "  entry_timeout=" + repr(entry_timeout)


def emit_emergency_receipt(
    workers: object = RECEIPT_UNRESOLVED,
    entry_timeout: object = RECEIPT_UNRESOLVED,
) -> None:
    """Print emergency receipt; never re-raises (outer handler must not die twice)."""
    try:
        line = format_emergency_receipt_line(workers, entry_timeout)
    except Exception:
        # Even repr is theoretically failure-proof for our types; belt.
        line = "pool_workers=unresolved  entry_timeout=unresolved"
    try:
        print(line)
    except Exception:
        pass


def emit_receipt(
    workers: int | str | None = None,
    entry_timeout: float | str | None = None,
) -> None:
    """Print the pretty receipt on stdout (typed refusal / happy paths).

    Falls back to :func:`emit_emergency_receipt` if the pretty formatter itself
    raises, so a typed-handler path cannot lose the receipt either.
    """
    try:
        print(format_receipt_line(workers, entry_timeout))
    except Exception:
        emit_emergency_receipt(
            RECEIPT_UNRESOLVED if workers is None else workers,
            RECEIPT_UNRESOLVED if entry_timeout is None else entry_timeout,
        )


def format_report(
    results: Sequence[EntryResult],
    *,
    workers: int | str | None = None,
    entry_timeout: float | str | None = None,
    wall_s: float | None = None,
) -> str:
    """Human report body — also durable gate evidence (merge-gate captures this).

    Pool width and the effective per-entry timeout are recorded next to the
    totals line so a receipt retains them even if the startup print is truncated
    or the scratch log is deleted. Do not put these only in a preamble print.
    Refusal paths that never build a results list call :func:`emit_receipt`
    instead so the same line still appears.

    Timing (``duration_s`` per entry, ``entry_seconds_*`` and
    ``entries_wall_seconds`` in the footer) is durable evidence too: the pool
    width default carried "pending quiet-host calibration" for as long as the
    harness collected no timing at all, so there was nothing to calibrate
    against. ``wall_s`` is the caller's measured wall clock for the whole
    entries phase — the only field that stays honest above width 1.
    """
    lines = ["COUNTERFEIT CORPUS REPORT", "=" * 72]
    caught = survived = skipped_platform = other = 0
    for r in results:
        mark = {
            "caught": "CAUGHT  ",
            "survived": "SURVIVED",
            "apply_error": "APPLY_ERR",
            "collection_error": "COLLECT ",
            "wrong_failure": "WRONG_FAIL",
            "worker_error": "WORKER_ERR",
            "skipped_platform": "SKIP_PLAT",
        }.get(r.status, r.status.upper())
        if r.status == "caught":
            caught += 1
        elif r.status == "survived":
            survived += 1
        elif r.status == "skipped_platform":
            skipped_platform += 1
        else:
            other += 1
        lines.append(f"{mark}  {r.entry.id}")
        lines.append(f"         rationale: {r.entry.rationale}")
        lines.append(f"         must_fail: {', '.join(r.entry.must_fail)}")
        lines.append(f"         detail:    {r.detail}")
        if r.duration_s is not None:
            lines.append(f"         seconds:   {r.duration_s:.2f}")
    lines.append("-" * 72)
    lines.append(
        f"total={len(results)}  caught={caught}  survived={survived}  "
        f"skipped_platform={skipped_platform}  other={other}"
    )
    # Timing totals. Printed only over the MEASURED subset, and the measured
    # count is printed with them: a mean silently taken over "everything that
    # happened to carry a number" is how a half-instrumented run reads as fast.
    timed = [r.duration_s for r in results if r.duration_s is not None]
    if timed:
        # ``entry_seconds_sum``, never "total": at pool_workers>1 the per-entry
        # wall times overlap, so their sum EXCEEDS the wall clock. A field named
        # "total" would be read as the gate's duration and would understate the
        # cost of raising the width — the exact calibration mistake this
        # instrumentation exists to prevent. ``wall_s`` below is the real one.
        lines.append(
            f"entry_seconds_sum={sum(timed):.2f} (summed per-entry wall; "
            f"= wall clock only at pool_workers=1)  "
            f"entry_seconds_mean={sum(timed) / len(timed):.2f}  "
            f"entry_seconds_max={max(timed):.2f}  "
            f"timed_entries={len(timed)}/{len(results)}"
        )
    if wall_s is not None:
        lines.append(f"entries_wall_seconds={wall_s:.2f}")
    # Durable instrument receipt next to totals — same string as emit_receipt.
    if workers is not None or entry_timeout is not None:
        lines.append(format_receipt_line(workers, entry_timeout))
    if survived:
        lines.append(
            "FINDING: surviving counterfeits mean the named suite does not bind "
            "to the defect class — coverage is decoration."
        )
    return "\n".join(lines)


def _new_entries_list() -> list[CounterfeitEntry]:
    """Empty entry list for main()'s working set.

    Module-level (not a bare ``[]`` literal in main) so tests can force an
    allocation failure and prove the outer receipt catch-all still fires.
    """
    return []


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry for ``make counterfeit-gate``.

    VALUE protected: a durable receipt line on stdout before every process-
    terminating return from main (``pool_workers=…  entry_timeout=…``).

    Known stages that must not escape without a receipt include argv
    normalisation, argparse construction/parse (SystemExit for --help/bad-flag
    still propagates), instrument resolution, corpus load, --entry filtering,
    run_control, run_entries, format_report, and the GATE RED/GREEN finish.
    That list is illustrative, not exhaustive: stages such as ``import argparse``,
    timeout-note rendering, and the startup print are also inside the protected
    body. The outer ``except Exception`` is the backstop for any stage not
    named here (and for stages added later), so this docstring cannot become a
    favourable-absence by omitting a real step.

    Receipt emission on the emergency path uses emit_emergency_receipt only
    (never format_receipt_line), so a pretty-formatter failure cannot double-
    fault. Sentinels are seeded first so the emergency line has defined
    bindings. ``SystemExit`` / ``KeyboardInterrupt`` are BaseException and
    still propagate (argparse --help unchanged).
    """
    # Seed sentinels BEFORE the outer try with LOAD_CONST string literals only.
    # Writing RECEIPT_UNRESOLVED here would be LOAD_GLOBAL — if that name is
    # missing (or broken) we NameError before any handler exists and emit no
    # receipt (Sol bytecode probe). The string "unresolved" matches the constant
    # value; every use INSIDE the try still references RECEIPT_UNRESOLVED so a
    # NameError there is caught by except Exception. Frame creation failing
    # before any bytecode runs is out of scope. The entries list is allocated
    # inside the try via _new_entries_list so resource failure there still
    # receipts.
    workers: object = "unresolved"
    effective_timeout: object = "unresolved"
    base_timeout: float | None = None

    try:
        entries: list[CounterfeitEntry] = _new_entries_list()

        import argparse

        # argv normalisation is inside the try: list() on a bad iterable must
        # not escape without a receipt.
        argv_list = list(argv) if argv is not None else None

        parser = argparse.ArgumentParser(description="W4-10 counterfeit corpus gate")
        parser.add_argument(
            "--manifest",
            type=Path,
            default=DEFAULT_MANIFEST,
            help="corpus.toml path (its sibling corpus.d/*.toml fragments load too)",
        )
        parser.add_argument(
            "--fragment-dir",
            type=Path,
            default=None,
            help="override the corpus.d/ fragment directory (default: beside --manifest)",
        )
        parser.add_argument(
            "--skip-control",
            action="store_true",
            help="skip the unpatched green control pass",
        )
        parser.add_argument(
            "--entry",
            action="append",
            default=[],
            help="run only this counterfeit id (repeatable)",
        )
        parser.add_argument(
            "--allow-survived",
            action="store_true",
            help="exit 0 even when counterfeits survive (report-only; NOT for release)",
        )
        # SystemExit from --help / usage errors propagates (not Exception).
        # A genuine ValueError/other Exception from parse_args is caught below.
        args = parser.parse_args(argv_list)

        try:
            # Resolved and PRINTED before anything runs: pool width and the
            # effective per-entry bound decide whether a slow entry under
            # contention reads as "survived" or as "the instrument ran out of
            # time", so both belong in the report a human reads, not only in
            # the env.
            workers = pool_workers_default()
            effective_timeout = effective_entry_timeout(workers=workers)
            base_timeout = entry_timeout_default()
            entries = load_corpus(args.manifest, fragment_dir=args.fragment_dir)
        except CounterfeitError as exc:
            print(f"COUNTERFEIT GATE REFUSED: {exc}", file=sys.stderr)
            # Receipt always present — real values if resolved, else "unresolved".
            emit_receipt(workers, effective_timeout)  # type: ignore[arg-type]
            return 2

        if entry_timeout_explicitly_set() and isinstance(base_timeout, float):
            timeout_note = f"explicit {ENTRY_TIMEOUT_ENV}={base_timeout:.1f}s"
        elif isinstance(effective_timeout, float):
            # Bound is fixed at base for every pool width (verdict determinism).
            timeout_note = f"fixed base {effective_timeout:.1f}s (independent of pool width)"
        else:
            timeout_note = f"base {effective_timeout!r}"
        if isinstance(effective_timeout, float):
            t_disp = f"{effective_timeout:.1f}s"
        else:
            t_disp = f"{effective_timeout!r}"
        print(
            f"counterfeit gate: pool workers {workers}; per-entry timeout {t_disp} ({timeout_note})"
        )

        if args.entry:
            # Match on the normalised id: ids are unique under that key, so this
            # can never mis-select, and `--entry CF-Foo` for `cf-foo` now works
            # instead of silently reporting an unknown id.
            wanted = {normalise_id(e) for e in args.entry}
            entries = [e for e in entries if e.normalised_id in wanted]
            missing = wanted - {e.normalised_id for e in entries}
            if missing:
                print(
                    f"COUNTERFEIT GATE REFUSED: unknown ids {sorted(missing)}",
                    file=sys.stderr,
                )
                emit_receipt(workers, effective_timeout)  # type: ignore[arg-type]
                return 2

        try:
            if not args.skip_control:
                # Unpatched live tree — serial, before the pool starts. Do not
                # parallelise: control must reflect HEAD, not a contended scratch.
                run_control(entries)
        except CounterfeitControlError as exc:
            print(f"COUNTERFEIT GATE CONTROL FAILED:\n{exc}", file=sys.stderr)
            emit_receipt(workers, effective_timeout)  # type: ignore[arg-type]
            return 1

        try:
            # workers/timeout are ints/floats after successful resolve above.
            assert isinstance(workers, int)
            assert isinstance(effective_timeout, float)
            entries_started = time.perf_counter()
            results = run_entries(
                entries,
                entry_timeout=effective_timeout,
                workers=workers,
            )
            entries_wall = time.perf_counter() - entries_started
        except CounterfeitError as exc:
            print(f"COUNTERFEIT GATE REFUSED: {exc}", file=sys.stderr)
            emit_receipt(workers, effective_timeout)  # type: ignore[arg-type]
            return 2
        # Report body carries pool width + effective timeout as durable evidence
        # (merge-gate captures this stdout; startup print alone is not enough).
        print(
            format_report(
                results,
                workers=workers,
                entry_timeout=effective_timeout,
                wall_s=entries_wall,
            )
        )

        bad = [r for r in results if not r.ok]
        if not bad:
            skipped = sum(1 for r in results if r.status == "skipped_platform")
            if skipped:
                print(
                    f"GATE GREEN: every runnable counterfeit was caught for its "
                    f"recorded reason ({skipped} platform-pinned elsewhere — see "
                    f"SKIP_PLAT lines above; those entries are verified on their "
                    f"pinned host)."
                )
            else:
                print("GATE GREEN: every counterfeit was caught for its recorded reason.")
            return 0

        print("GATE RED:", file=sys.stderr)
        for r in bad:
            print(f"  - {r.entry.id}: {r.status}: {r.detail}", file=sys.stderr)

        if args.allow_survived and all(r.status == "survived" for r in bad):
            # Explicit report-only mode — never the default release path.
            return 0
        return 1
    except Exception as exc:
        # Outer catch-all for the WHOLE body (argv → final return). Must not
        # call format_receipt_line — that is what may have just failed (Sol
        # double-fault probe). emit_emergency_receipt is structurally fail-safe.
        # Catch Exception, not BaseException — Ctrl-C / SystemExit intact.
        try:
            print(
                f"COUNTERFEIT GATE REFUSED: unexpected {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
        except Exception:
            pass
        emit_emergency_receipt(workers, effective_timeout)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
