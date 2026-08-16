"""Anti-drift net for the dashboard's TypeScript mirrors of `omniagentos.contracts`.

`dashboard/src/lib/contracts.ts` re-declares the contract StrEnums as TypeScript
string-literal unions. Nothing compared the two sides, and the mirror has silently
drifted at least twice -- both drifts are still recorded in that file as comments:

    // DRIFT FIX (TN.0): "irreversible" was missing. It is the highest-risk member
    // of contracts.ActionClass and the sole member of policy.HARD_STOP_CLASSES, so
    // the mirror was omitting precisely the value that always requires a human.

    // DRIFT FIX (TN.0): "swarm" was missing (ledger-only, never adapter-resolved).

Both were found by hand, after the fact. This test is the guard that was never
written for them.

It is deliberately DISCOVERED, not listed: every `export type X = "a" | "b";` in
contracts.ts whose name is also a StrEnum in `omniagentos.contracts` is compared,
so a mirror added tomorrow is covered the moment it exists, and adding a member on
either side breaks the build rather than waiting for someone to notice. A
hand-written list of the seventeen names known today would have the same failure
mode as the omission it exists to catch.
"""

from __future__ import annotations

import re
from enum import StrEnum
from pathlib import Path

import pytest

from omniagentos import contracts

CONTRACTS_TS = Path(__file__).resolve().parents[2] / "dashboard" / "src" / "lib" / "contracts.ts"

# The count is asserted below rather than trusted: if the regex or the file layout
# ever stops matching, this test must FAIL loudly instead of silently comparing
# nothing and reporting green. A vacuous pass is the failure mode this whole file
# exists to prevent, so it must not be this file's own failure mode.
#
# This is the OUTER vacuity net, not the primary defense: it catches a population
# change (a mirror added or removed), while test_no_enum_backed_union_is_silently_
# unreadable catches an IDENTITY change (a mirror silently going unparsed while the
# count stays the same, e.g. one union swapped for another). Set to the measured
# live count of StrEnum-backed mirrors in contracts.ts (17 as of this change), not
# to a value with tolerance below it -- a floor with slack is exactly what let two
# mirrors vanish before anything noticed (measured: dropping ActionClass entirely
# leaves 16 mirrors, which the old floor of 15 still passed).
MINIMUM_EXPECTED_MIRRORS = 17


def _strip_comments(source: str) -> str:
    """Drop block and line comments so a commented-out member never counts."""
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    return re.sub(r"//[^\n]*", "", source)


def _typescript_type_bodies(source: str) -> dict[str, str]:
    """Every `export type X = <body>;` as {name: raw body text}, unfiltered.

    This is the full set of exported type aliases regardless of whether they turn
    out to be pure string-literal unions -- callers decide what "unreadable" means.
    """
    bodies: dict[str, str] = {}
    for match in re.finditer(r"export type (\w+)\s*=\s*([^;]+);", _strip_comments(source)):
        name, body = match.group(1), match.group(2)
        bodies[name] = body
    return bodies


def _typescript_unions(source: str) -> dict[str, set[str]]:
    """Every `export type X = "a" | "b" | ...;` as {name: {members}}.

    Only pure string-literal unions are returned; a type aliasing an interface or
    a generic has no enum to mirror and is skipped rather than reported as empty.
    """
    unions: dict[str, set[str]] = {}
    for name, body in _typescript_type_bodies(source).items():
        members = set(re.findall(r'"([^"]*)"', body))
        # A pure literal union is nothing but quoted members and `|` separators.
        if members and not re.sub(r'"[^"]*"|\||\s', "", body):
            unions[name] = members
    return unions


def _unreadable_enum_backed_unions(source: str) -> dict[str, str]:
    """Type aliases whose NAME is backed by a StrEnum but whose BODY the parser
    could not read as a pure string-literal union (single quotes, an open-union
    `| (string & {})` tail, a referenced type, etc.).

    `_typescript_unions` silently drops any type it cannot parse as a pure literal
    union -- that is correct for a type with no enum to mirror (an interface, a
    generic), but wrong for a type whose name IS a StrEnum: there the residue means
    "I could not read this", not "there is nothing here to compare", and dropping
    it silently would shrink the guard's coverage without anyone noticing. This
    returns {name: raw_body} for exactly that case so callers can fail loudly.
    """
    bodies = _typescript_type_bodies(source)
    unions = _typescript_unions(source)
    return {
        name: body
        for name, body in bodies.items()
        if name not in unions
        and isinstance(getattr(contracts, name, None), type)
        and issubclass(getattr(contracts, name), StrEnum)
    }


def _mirrored_names() -> list[str]:
    """Union names in contracts.ts that a StrEnum of the same name backs."""
    unions = _typescript_unions(CONTRACTS_TS.read_text(encoding="utf-8"))
    return sorted(
        name
        for name in unions
        if isinstance(getattr(contracts, name, None), type)
        and issubclass(getattr(contracts, name), StrEnum)
    )


MIRRORED_NAMES = _mirrored_names()


def test_the_mirror_discovery_itself_found_something() -> None:
    """Guard the guard: a parse that finds nothing must fail, not pass green."""
    assert CONTRACTS_TS.is_file(), f"dashboard contract mirror not found at {CONTRACTS_TS}"
    assert len(MIRRORED_NAMES) >= MINIMUM_EXPECTED_MIRRORS, (
        f"only {len(MIRRORED_NAMES)} mirrored unions discovered in {CONTRACTS_TS.name} "
        f"({MIRRORED_NAMES}) -- expected at least {MINIMUM_EXPECTED_MIRRORS}. "
        "The parser has drifted from the file, so every comparison below is vacuous."
    )


def test_no_enum_backed_union_is_silently_unreadable() -> None:
    """A type whose name IS a StrEnum but whose body the parser cannot read as a
    pure literal union (single quotes, a `| (string & {})` tail, a referenced
    type, ...) must fail LOUDLY here, not silently vanish from MIRRORED_NAMES.
    "This type has no enum to mirror" and "I could not read this type" must not
    be the same branch -- only the first is allowed to be silent.
    """
    unreadable = _unreadable_enum_backed_unions(CONTRACTS_TS.read_text(encoding="utf-8"))
    assert not unreadable, (
        "the following types in "
        f"{CONTRACTS_TS.name} are backed by a StrEnum of the same name but could "
        "not be parsed as a pure double-quoted string-literal union, so they were "
        f"silently dropped from the drift guard's coverage: {unreadable}. Re-quote "
        "with double quotes and remove any non-literal tail (e.g. `| (string & "
        "{})`)."
    )


@pytest.mark.parametrize("name", MIRRORED_NAMES)
def test_typescript_union_mirrors_its_python_enum(name: str) -> None:
    python_members = {member.value for member in getattr(contracts, name)}
    typescript_members = _typescript_unions(CONTRACTS_TS.read_text(encoding="utf-8"))[name]

    missing_in_typescript = python_members - typescript_members
    absent_in_python = typescript_members - python_members

    assert not missing_in_typescript, (
        f"{name}: contracts.py declares {sorted(missing_in_typescript)}, "
        f"which dashboard/src/lib/contracts.ts omits. A value the backend can emit "
        f"and the dashboard's type does not admit is exactly the TN.0 drift this "
        f"test exists to stop."
    )
    assert not absent_in_python, (
        f"{name}: dashboard/src/lib/contracts.ts declares {sorted(absent_in_python)}, "
        f"which contracts.py does not. The mirror invents a value the backend "
        f"can never send."
    )


def test_unreadable_residue_is_not_silently_dropped_single_quotes() -> None:
    """Falsifier: a `StepStatus`-shaped body re-quoted with single quotes must be
    caught as unreadable, not silently treated as "no enum to mirror here".
    `StepStatus` is a real StrEnum in `omniagentos.contracts`, so re-declaring it
    with residue the old parser could not read must be reported, not dropped.
    """
    source = (
        "export type StepStatus = "
        "'pending' | 'started' | 'completed' | 'failed' | 'compensated' | 'skipped';"
    )
    # The old, buggy behavior: the union silently vanishes from the returned dict.
    assert _typescript_unions(source) == {}
    # The fix: the vanished-but-enum-backed name is surfaced as unreadable.
    unreadable = _unreadable_enum_backed_unions(source)
    assert "StepStatus" in unreadable


def test_unreadable_residue_is_not_silently_dropped_open_union_tail() -> None:
    """Falsifier: an `| (string & {})` open-union tail on a real StrEnum-backed
    type must be caught as unreadable, not silently dropped.
    """
    source = (
        'export type ApprovalState = "pending" | "approved" | "rejected" '
        '| "expired" | (string & {});'
    )
    assert _typescript_unions(source) == {}
    unreadable = _unreadable_enum_backed_unions(source)
    assert "ApprovalState" in unreadable


def test_a_vacuous_mirror_drop_is_caught_by_the_floor() -> None:
    """Falsifier for the anti-vacuity floor itself: dropping one StrEnum-backed
    union ENTIRELY (not re-quoting it -- genuinely deleting the declaration) must
    fail MINIMUM_EXPECTED_MIRRORS, not slide through on slack in the floor.

    The retired floor of 15 tolerated exactly this: removing `ActionClass` left
    16 mirrors, which 15 still passed. The floor is now the measured live count
    (17), so a one-mirror drop must be caught here even though
    `_unreadable_enum_backed_unions` cannot catch it -- a deleted declaration has
    no residue to report as unreadable, it is just gone. The mutation is applied
    to an in-memory copy of contracts.ts; the repo file is never modified.
    """
    source = CONTRACTS_TS.read_text(encoding="utf-8")
    mutated = re.sub(r"export type ActionClass\s*=\s*[^;]+;", "", source, count=1)
    assert mutated != source, (
        "the ActionClass declaration was not found in "
        f"{CONTRACTS_TS.name}; update this test to the new declaration form"
    )

    unions = _typescript_unions(mutated)
    mirrored = sorted(
        name
        for name in unions
        if isinstance(getattr(contracts, name, None), type)
        and issubclass(getattr(contracts, name), StrEnum)
    )
    assert len(mirrored) == len(MIRRORED_NAMES) - 1
    # The retired floor of 15 would have let this vacuous 16-mirror set pass
    # silently; the measured-live floor must not.
    assert len(mirrored) < MINIMUM_EXPECTED_MIRRORS, (
        f"a vacuous drop of ActionClass left {len(mirrored)} mirrors, which the "
        f"anti-vacuity floor of {MINIMUM_EXPECTED_MIRRORS} should have refused "
        "but did not -- the floor has slack again."
    )


def test_unreadable_but_not_enum_backed_is_still_silently_skipped() -> None:
    """A type with residue whose name is NOT a StrEnum genuinely has no enum to
    mirror (an interface alias, a generic, ...), so it must stay silent -- only
    enum-backed names are loud failures.
    """
    source = "export type NotARealEnum = 'a' | 'b';"
    assert _typescript_unions(source) == {}
    assert _unreadable_enum_backed_unions(source) == {}
