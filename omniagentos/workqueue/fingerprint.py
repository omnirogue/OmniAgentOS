"""The input fingerprint — SPEC-shared-queue §4.1.

``input_key`` is the whole anti-retry contract in one function: a gate that
refused an input must compute the SAME key for that input on every machine in
the pool, or the shared refusal ledger silently evaporates into a per-machine
one (which is exactly the AccurateGate limitation this port exists to remove).

Ported from ``~/.omniagentos/ops/AccurateGate/accurate-gate.py:input_key()`` (kinds
``tree`` / ``self`` / ``file``), with the two kinds SPEC §4.1 adds — ``gate``
(entry 1) and ``cmd`` (entry 5) — and three hardening changes:

1. **Kind-tagged and length-prefixed.** The original concatenated raw bytes, so
   two adjacent entries could alias into one (a file whose bytes end in what the
   next entry begins with produces the same digest as a different split). Every
   entry is now fed as ``<kind>\\x1f<len>\\x1f<payload>`` — two fields can never
   alias, which is what SPEC §4.1 means by "length-prefixed with its kind tag".

2. **No host identity, and no host PATHS.** SPEC §4.1 names what must never be
   hashed: ``machine_id``, ``worker_id``, ``attempt``, timestamps, load, or a
   nonce. Paths are the silent member of that family — ``/Users/owner/wq/work/x``
   on a Mac and ``/home/omniworker/wq/work/x`` on a Vultr box are the same input
   — so this module hashes what a path CONTAINS, never the path itself. The one
   exception is an ABSENT file, where there are no bytes to hash: the basename
   stands in, because it is the same on every machine. Positional order plus the
   length prefix keep two absent files distinct anyway.

3. **The dirty tree is hashed by CONTENT** (see :func:`_tree_payload`), not by the
   list of dirty paths. The original's ``status --porcelain`` component names the
   paths only, which gave two genuinely different agent attempts one key.

Consequence, stated because it is the point: two machines that check out the
same ``base_sha``, run the same ``acceptance_cmd`` through the same gate script
and gate config compute the same key, so the second one refuses in ~0.2 s
instead of spending another ten-minute gate run.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

__all__ = ["InputKeyError", "input_key", "input_key_from_specs", "pinned_tree_payload"]

#: Field separator inside one hashed entry. 0x1f (US) cannot appear in a kind
#: tag or a decimal length, so the framing is unambiguous.
_SEP = b"\x1f"

_KNOWN_KINDS = ("gate", "tree", "self", "file", "cmd")


class InputKeyError(RuntimeError):
    """The fingerprint could not be computed — an instrument fact, never a verdict.

    Raised for an unreadable git worktree or an unknown spec kind. Callers must
    translate it into ``instrument-error``: a key that cannot be computed says
    nothing at all about the candidate's code.
    """


def _feed(h: Any, kind: str, payload: bytes) -> None:
    h.update(kind.encode() + _SEP + str(len(payload)).encode() + _SEP + payload)


def _git(
    args: Sequence[str], env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    # errors="replace": git argv/paths can carry bytes that are not valid UTF-8.
    # The instrument must degrade a byte, never crash mid-fingerprint.
    return subprocess.run(
        list(args), capture_output=True, text=True, errors="replace", check=False, env=env
    )


def _tree_payload(worktree: str) -> bytes:
    """A CONTENT hash of the tree the gate will grade, plus the porcelain status.

    The original hashed ``rev-parse HEAD^{tree}`` + ``status --porcelain``. That is
    not enough here, and the gap is not theoretical: porcelain status names the
    dirty PATHS but says nothing about their CONTENT, so an agent that rewrote the
    same file with a different fix produced a byte-identical key and its retry was
    refused as ``unchanged-retry`` before it ran. Found by driving a two-attempt
    agent unit through the real loop, 2026-08-11 — and it would have silently
    disabled §4.6's lineage swap for every agent unit in the pool.

    So the tree id comes from a DISPOSABLE INDEX (``GIT_INDEX_FILE`` copy →
    ``add -A`` → ``write-tree``), the same technique
    ``omniagentos/execution/verify.py`` uses to observe a working tree without
    touching the real one. It covers tracked edits, untracked files and deletions
    by content, and it respects .gitignore — an ignored build artefact must not
    re-admit a refused input.

    Equivalence with the pinned pre-clone form is preserved exactly: on a clean
    checkout, ``add -A`` + ``write-tree`` reproduces the commit's own tree id and
    the status is empty (asserted in tests/workqueue_gate).
    """
    fd, temp_name = tempfile.mkstemp(prefix="wq-fingerprint-index-")
    os.close(fd)
    temp_index = Path(temp_name)
    try:
        temp_index.unlink(missing_ok=True)  # git reads a zero-byte index as corrupt
        real_index = _git(["git", "-C", worktree, "rev-parse", "--git-path", "index"])
        if real_index.returncode != 0:
            raise InputKeyError(
                f"input_key tree {worktree}: {real_index.stderr.strip() or 'not a git worktree'}"
            )
        source = Path(worktree) / real_index.stdout.strip()
        if source.exists():
            shutil.copy2(source, temp_index)
        env = os.environ | {"GIT_INDEX_FILE": str(temp_index)}
        staged = _git(["git", "-C", worktree, "add", "-A"], env=env)
        if staged.returncode != 0:
            raise InputKeyError(f"input_key stage {worktree}: {staged.stderr.strip()}")
        tree = _git(["git", "-C", worktree, "write-tree"], env=env)
        if tree.returncode != 0:
            raise InputKeyError(f"input_key write-tree {worktree}: {tree.stderr.strip()}")
    finally:
        temp_index.unlink(missing_ok=True)
        Path(f"{temp_index}.lock").unlink(missing_ok=True)
    status = _git(["git", "-C", worktree, "status", "--porcelain"])
    if status.returncode != 0:
        raise InputKeyError(f"input_key status {worktree}: {status.stderr.strip() or 'unreadable'}")
    return tree.stdout.strip().encode() + b"\n" + status.stdout.encode()


def _bytes_payload(path: str) -> bytes:
    p = Path(path)
    try:
        return p.read_bytes()
    except OSError:
        # ABSENT stands in for "there were no bytes". The BASENAME is host-stable;
        # the full path is not, and hashing it would break pool-wide agreement.
        return b"ABSENT:" + p.name.encode()


def pinned_tree_payload(repo_dir: str, base_sha: str) -> bytes:
    """The tree payload a PRISTINE checkout at ``base_sha`` would produce.

    This is what lets the worker consult the refusal ledger BEFORE it clones —
    the §4 promise is a refusal in ~0.2 s, and a clone is not free. It is exactly
    equivalent, not an approximation: a fresh ``clone --no-checkout`` +
    ``checkout -b <branch> <base_sha>`` has ``HEAD^{tree}`` equal to the pinned
    commit's tree and an EMPTY ``status --porcelain``, which is byte-for-byte the
    payload built here. ``tests/workqueue_gate/test_input_key_is_host_invariant.py``
    asserts that equality against a real clone so the two can never drift.

    Works against a BARE mirror, where ``git status`` is not runnable at all.
    """
    rev = _git(["git", "-C", repo_dir, "rev-parse", f"{base_sha}^{{tree}}"])
    if rev.returncode != 0:
        raise InputKeyError(
            f"input_key pin {base_sha[:12]} in {repo_dir}: {rev.stderr.strip() or 'unreadable'}"
        )
    return rev.stdout.strip().encode() + b"\n"


def input_key_from_specs(specs: Iterable[str], *, tree_payload: bytes | None = None) -> str:
    """Hash an ordered list of ``<kind>:<value>`` declarations.

    Kinds: ``gate`` (name), ``tree`` (git worktree), ``self`` (the gate script's
    bytes — a gate UPGRADE legitimately re-admits every previously refused
    input), ``file`` (a config the gate reads), ``cmd`` (the exact acceptance
    command string). An unknown kind raises rather than being skipped: silently
    dropping a declared input would make two different inputs share one key.

    ``tree_payload`` substitutes a precomputed payload (see
    :func:`pinned_tree_payload`) for every ``tree:`` entry, so the key can be
    computed before the worktree exists.
    """
    h = hashlib.sha256()
    for spec in specs:
        kind, _, value = spec.partition(":")
        if kind not in _KNOWN_KINDS:
            raise InputKeyError(f"unknown input_key kind {kind!r} in {spec!r}")
        if kind == "tree":
            _feed(h, kind, tree_payload if tree_payload is not None else _tree_payload(value))
        elif kind in ("self", "file"):
            _feed(h, kind, _bytes_payload(value))
        else:  # gate, cmd — literal strings, hashed as given
            _feed(h, kind, value.encode())
    return h.hexdigest()


def input_key(
    gate: str,
    worktree: str,
    gate_script: str | None = None,
    config_files: Sequence[str] = (),
    acceptance_cmd: str | None = None,
    *,
    pinned_at: tuple[str, str] | None = None,
) -> str:
    """SPEC §4.1, in its exact order: gate, tree, self, file × N, cmd.

    The worker calls this before consulting the refusal ledger; ``scripts/
    accurate-gate.py`` builds the same ordered spec list from its ``input_key:``
    block. Both routes go through :func:`input_key_from_specs`, so there is one
    implementation of the key and no way for the two to drift.

    ``pinned_at=(repo_dir, base_sha)`` computes the tree entry from a pinned
    commit in a (possibly bare) repository instead of from ``worktree`` — the
    pre-clone path described in :func:`pinned_tree_payload`. ``worktree`` is
    then unused and may be ``""``.
    """
    specs: list[str] = [f"gate:{gate}", f"tree:{worktree}"]
    if gate_script:
        specs.append(f"self:{gate_script}")
    specs.extend(f"file:{path}" for path in config_files)
    if acceptance_cmd is not None:
        specs.append(f"cmd:{acceptance_cmd}")
    payload = pinned_tree_payload(*pinned_at) if pinned_at else None
    return input_key_from_specs(specs, tree_payload=payload)
