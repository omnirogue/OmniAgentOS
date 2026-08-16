"""Property fuzzer for ``_chain_is_map_neutral`` (gemini review of #504).

The chain rule is the load-bearing relaxation of the ARCHI stamp gate: it may
return fresh ONLY for a linear descendant chain of the stamped source in which
at most the first commit is the generator's own oracle refresh and every other
commit is map-neutral. Reviewing #504 took ~20 minutes of hand-built git
topologies to probe ONE candidate laundering path; this fuzzer builds those
topologies mechanically so every future edit to the predicate is checked
against the whole class in seconds.

Deterministic on purpose: seeds are parametrized, ``random.Random(seed)`` only
— no wall-clock, no global random state, immune to ``pytest-randomly``.

The oracle here is an independent MODEL of the rule, not a re-implementation
of its git plumbing: while building each repo we record ground truth
(linearity, per-commit neutrality, oracle placement) and assert the predicate
agrees. A predicate mutant that accepts a poisoned chain disagrees with the
model on at least one seed; a model bug that disagrees with a correct
predicate fails loudly the same way — either way a human looks.
"""

from __future__ import annotations

import random
import subprocess
from pathlib import Path

import pytest

from omniagentos.archdocs.staleness import (
    _chain_is_map_neutral,
    parse_stamp_comment,
    stamp_archi,
)

from .test_archdocs import _build_fake_repo, _commit, _init_git

#: Neutral edits: files no scanner reads and no surface prefix covers.
_NEUTRAL_EDITS = (
    ("omniagentos/alpha/feature.py", "ENABLED = {n}\n"),
    ("omniagentos/beta/logic.py", "STEP = {n}\n"),
    ("tests/test_something.py", "EXPECTED = {n}\n"),
    ("dashboard/page.tsx", "// rev {n}\n"),
    ("vault/notes.md", "note {n}\n"),
)

#: Poison edits: each MUST make the chain non-neutral (surface, package
#: inventory, launcher, or a non-refresh oracle touch).
_POISON_EDITS = (
    ("omniagentos/api/routes/extra.py", "# route module rev {n}\n"),
    ("omniagentos/db/migrations/900_fuzz.sql", "-- rev {n}\n"),
    ("omniagentos/archdocs/tables.py", "NODES = {n}\n"),
    ("docs/architecture/notes.md", "narrative {n}\n"),
    ("scripts/launch-omniagentos.sh", "API_PORT={n}\n"),
    ("omniagentos/gamma/__init__.py", ""),  # new top-level package
    ("ARCHI.md", "\nhand-edited narrative {n}\n"),  # oracle outside a refresh
)


def _write(root: Path, rel: str, content: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if rel == "ARCHI.md":
        path.write_text(path.read_text(encoding="utf-8") + content, encoding="utf-8")
    else:
        path.write_text(content, encoding="utf-8")


def _stamped_repo(tmp_path: Path, *, with_refresh: bool) -> str:
    """Fake repo with a stamped ARCHI.md; returns the stamped head (short)."""
    _build_fake_repo(tmp_path)
    (tmp_path / "scripts").mkdir(exist_ok=True)
    (tmp_path / "scripts" / "launch-omniagentos.sh").write_text(
        "API_PORT=8788\n", encoding="utf-8"
    )
    _init_git(tmp_path)
    archi = tmp_path / "ARCHI.md"
    archi.write_text("# ARCHI.md\n", encoding="utf-8")
    stamp_archi(tmp_path, archi)
    if with_refresh:
        _commit(tmp_path, "archdocs refresh", "ARCHI.md")
    stored = parse_stamp_comment(archi.read_text(encoding="utf-8"))
    assert stored is not None
    return stored.git_head


@pytest.mark.parametrize("seed", range(8))
def test_random_neutral_linear_chains_are_accepted(tmp_path: Path, seed: int) -> None:
    """Property: a linear chain of purely neutral commits (optionally opened by
    the generator's own refresh) is always accepted."""
    rng = random.Random(seed)
    stamped = _stamped_repo(tmp_path, with_refresh=rng.random() < 0.5)
    for n in range(rng.randint(1, 5)):
        rel, template = rng.choice(_NEUTRAL_EDITS)
        _write(tmp_path, rel, template.format(n=n))
        _commit(tmp_path, f"neutral {n}", rel)
    assert _chain_is_map_neutral(tmp_path, stamped) is True, (
        f"seed {seed}: a purely neutral linear chain must be accepted"
    )


@pytest.mark.parametrize("seed", range(8))
def test_any_poison_commit_anywhere_in_the_chain_is_rejected(
    tmp_path: Path, seed: int
) -> None:
    """Property: one poison commit at a random position rejects the chain,
    whatever neutral commits surround it — with ONE documented exception the
    fuzzer itself surfaced (seed 6): an ARCHI.md-only commit that is the
    DIRECT successor of the stamped source is provenance-indistinguishable
    from the generator's own refresh, and both the old one-successor rule and
    the chain rule accept that shape by design (content lies are still caught
    by the live quantity comparison in ``is_stamp_stale`` and by
    ``test_launcher_hygiene.py::test_archi_stamp_gate_and_stale_route_mutation``'s
    hostile mutation). Every other placement of an oracle touch, and every
    other poison class at any position, must reject."""
    rng = random.Random(seed)
    with_refresh = rng.random() < 0.5
    stamped = _stamped_repo(tmp_path, with_refresh=with_refresh)
    length = rng.randint(1, 5)
    poison_at = rng.randrange(length)
    poison_rel, poison_template = rng.choice(_POISON_EDITS)
    for n in range(length):
        if n == poison_at:
            rel, template = poison_rel, poison_template
        else:
            rel, template = rng.choice(_NEUTRAL_EDITS)
        _write(tmp_path, rel, template.format(n=n))
        _commit(tmp_path, f"commit {n}", rel)

    refresh_shaped = poison_rel == "ARCHI.md" and poison_at == 0 and not with_refresh
    expected = refresh_shaped  # accepted by design; everything else rejects
    assert _chain_is_map_neutral(tmp_path, stamped) is expected, (
        f"seed {seed}: poison {poison_rel!r} at position {poison_at} of "
        f"{length} (with_refresh={with_refresh}) expected "
        f"{'acceptance (refresh-shaped by design)' if expected else 'rejection'}"
    )


def test_package_deletion_is_rejected(tmp_path: Path) -> None:
    """The package-inventory rule's D branch (gemini review, finding 1): the
    random loop only ever ADDS files, so deletion gets its own case — removing
    a top-level package __init__.py changes the packages block and must
    reject."""
    stamped = _stamped_repo(tmp_path, with_refresh=True)
    subprocess.run(
        ["git", "rm", "-q", "omniagentos/beta/__init__.py"], cwd=str(tmp_path), check=True
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "drop package beta"], cwd=str(tmp_path), check=True
    )
    assert _chain_is_map_neutral(tmp_path, stamped) is False, (
        "deleting a top-level package __init__.py must reject the chain"
    )


def test_package_init_modification_is_neutral(tmp_path: Path) -> None:
    """False-positive guard (gemini review, finding 2): the inventory rule
    trips on A/D only — MODIFYING an existing package __init__.py does not
    change the packages block and must stay neutral. An over-tightened gate
    that rejects any __init__.py touch fails here."""
    stamped = _stamped_repo(tmp_path, with_refresh=True)
    (tmp_path / "omniagentos" / "beta" / "__init__.py").write_text(
        '"""beta package."""\n', encoding="utf-8"
    )
    _commit(tmp_path, "document beta", "omniagentos/beta/__init__.py")
    assert _chain_is_map_neutral(tmp_path, stamped) is True, (
        "modifying an existing package __init__.py is map-neutral"
    )


@pytest.mark.parametrize("seed", range(4))
def test_merge_topologies_are_rejected(tmp_path: Path, seed: int) -> None:
    """Property: any chain containing a merge commit fails closed, however
    neutral both sides are."""
    rng = random.Random(seed)
    stamped = _stamped_repo(tmp_path, with_refresh=False)
    _write(tmp_path, "omniagentos/alpha/base.py", "BASE = 1\n")
    _commit(tmp_path, "mainline neutral", "omniagentos/alpha/base.py")

    subprocess.run(
        ["git", "checkout", "-q", "-b", f"side-{seed}"], cwd=str(tmp_path), check=True
    )
    rel, template = rng.choice(_NEUTRAL_EDITS)
    _write(tmp_path, rel, template.format(n=99))
    _commit(tmp_path, "side neutral", rel)
    subprocess.run(["git", "checkout", "-q", "-"], cwd=str(tmp_path), check=True)
    subprocess.run(
        ["git", "merge", "--no-ff", "-q", "-m", "merge side", f"side-{seed}"],
        cwd=str(tmp_path),
        check=True,
    )
    assert _chain_is_map_neutral(tmp_path, stamped) is False, (
        f"seed {seed}: a merge commit in the chain must fail closed"
    )


def test_worktree_oracle_tamper_is_rejected(tmp_path: Path) -> None:
    """Property: an uncommitted oracle edit rejects an otherwise neutral chain."""
    stamped = _stamped_repo(tmp_path, with_refresh=True)
    _write(tmp_path, "omniagentos/alpha/feature.py", "ENABLED = True\n")
    _commit(tmp_path, "neutral", "omniagentos/alpha/feature.py")
    assert _chain_is_map_neutral(tmp_path, stamped) is True

    archi = tmp_path / "ARCHI.md"
    archi.write_text(archi.read_text(encoding="utf-8") + "\ntampered\n", encoding="utf-8")
    assert _chain_is_map_neutral(tmp_path, stamped) is False, (
        "a dirty oracle in the worktree must reject the chain"
    )


def test_unresolvable_stamp_is_rejected(tmp_path: Path) -> None:
    """Property: a stamp naming a sha this repo does not contain fails closed."""
    _stamped_repo(tmp_path, with_refresh=False)
    assert _chain_is_map_neutral(tmp_path, "deadbeefdead") is False


def test_empty_chain_is_not_accepted_by_the_walk(tmp_path: Path) -> None:
    """Property: head == stamp source is the callers' equality fast-path, not
    this walk's — an empty chain here fails closed rather than vacuously
    passing (a mutant dropping the emptiness guard would accept it)."""
    stamped = _stamped_repo(tmp_path, with_refresh=False)
    assert _chain_is_map_neutral(tmp_path, stamped) is False
