"""The counterfeit gate: proof that this suite would notice if the code stopped being safe.

Every other test in this package was written by the same process, at the same
sitting, as the code it checks. That is not nothing, but it is close: the thing a
green suite most reliably demonstrates is that the author's model of the system
is internally consistent, and an author who misunderstood a guard will write a
test that passes whether the guard is there or not. The corpus is the one signal
here that cannot be produced that way, because it is not a claim about the code —
it is a claim about what the *tests* do when a named safety property is deleted.

How to read a failure
---------------------

``CounterfeitSurvived``
    A property in ``corpus.toml`` can be removed and this suite stays green. The
    fix is a test, never a smaller corpus. The failure prints the exact mutation
    and the rationale for the line it removed.
``CounterfeitStale``
    An anchor stopped matching exactly once — the guard moved, was renamed, or
    was duplicated. Re-anchor the entry, but only after confirming the same
    mutation still makes the same tests red for the same reason. An anchor that
    is silently re-pointed at whatever is nearby is a corpus disarming itself
    during exactly the refactor you most wanted it watching.
``HarnessNotIsolated``
    A mutant subprocess imported ``selfloop`` from somewhere other than the
    mutant tree, usually an installed or editable copy earlier on ``sys.path``.
    Nothing has been demonstrated in either direction; fix the environment
    before believing anything this file says.
XFAIL
    An entry carrying ``undefended_by``: the property is real, nothing defends
    it, and the field names the test that is owed. It is ``strict``, so the day
    somebody writes that test the entry XPASSes and this suite goes red until
    the field is deleted. A tracked debt with an alarm on it.

Cost
----

Each entry copies the project, starts a fresh interpreter to prove the copy is
what gets imported, and runs its witnesses in another. The whole file is on the
order of two minutes. That is the price of the only unforgeable evidence in the
suite, and it is paid once per run rather than once per edit.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

try:  # pytest's rootdir insertion differs depending on whether tests/ is a package
    from counterfeits import harness
except ImportError:  # pragma: no cover - taken only under the other layout
    from tests.counterfeits import harness

from selfloop.templates import TEMPLATES

CORPUS = harness.load_corpus()

#: How many learning entries the corpus must carry before this gate will vouch
#: for anything. The number is a floor rather than a count so that adding
#: coverage never breaks the suite, and it exists because the corpus this one
#: descends from ran 87 mutation patches that touched ZERO learning code — while
#: the learning loop was the part that had silently starved for months. Trimming
#: the safety half under deadline pressure is a decision somebody can defend;
#: trimming this half is the failure repeating itself.
MIN_LEARNING_ENTRIES = 8

#: ``tests/test_x.py::test_name`` — the only node-id shape the corpus may use.
#: Parametrised ids are deliberately excluded: an entry that names
#: ``...::test_x[sqlite]`` pins a fixture parametrisation that is not the
#: property under test, and it goes stale the day somebody adds a third backend.
_NODE_RE = re.compile(r"^(?P<file>tests/[\w/]+\.py)::(?P<name>test_[\w]+)$")


def _param(entry: harness.Counterfeit) -> object:
    """One parametrisation, marked ``xfail(strict=True)`` when nothing defends it."""
    if entry.undefended_by:
        return pytest.param(
            entry,
            id=entry.id,
            marks=pytest.mark.xfail(strict=True, reason=entry.undefended_by),
        )
    return pytest.param(entry, id=entry.id)


@pytest.fixture(scope="module")
def control(tmp_path_factory: pytest.TempPathFactory) -> None:
    """Run every witness in the corpus UNMUTATED and require all of them green.

    This is the half of a mutation gate that people leave out, and leaving it out
    turns the whole exercise into theatre. Each entry claims "these tests go red
    when the guard is removed"; if one of those tests is *already* red, the entry
    passes for exactly the wrong reason — and it keeps passing forever, because
    nobody re-reads a green gate.

    Module-scoped, so the cost is one run per session however many entries there
    are, and a failure here fails every entry rather than being buried inside one
    of them.
    """
    root = tmp_path_factory.mktemp("counterfeit-control")
    result = harness.control(root / "tree", CORPUS)
    if result.returncode != 0:
        nodes = harness.control_nodes(CORPUS)
        raise AssertionError(
            "the counterfeit CONTROL pass is red, so every entry below would be "
            "meaningless: a witness that already fails is indistinguishable from a "
            "witness catching a mutation. Fix these before reading any counterfeit "
            f"result.\nnodes: {list(nodes)}\n"
            f"{(result.stdout or '')[-4000:]}\n{(result.stderr or '')[-2000:]}"
        )


@pytest.mark.parametrize("entry", [_param(item) for item in CORPUS])
def test_the_counterfeit_is_caught(
    entry: harness.Counterfeit, control: None, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Remove one safety property; require the tests that name it to go red.

    ``control`` is requested rather than used: it is what guarantees the
    witnesses below were green before the mutation, and taking it as an argument
    is how that ordering is stated to pytest instead of hoped for.
    """
    del control
    root = tmp_path_factory.mktemp(f"cf-{entry.id[:24]}")
    harness.check(root / "tree", entry)


# ---------------------------------------------------------------------------
# Corpus consistency. All of these are pure file reads and run in milliseconds,
# so a stale or self-defeating corpus is reported long before two minutes of
# subprocesses have been spent proving it.
# ---------------------------------------------------------------------------


def test_every_shipped_template_is_the_primary_of_at_least_one_entry() -> None:
    """The plan's "one counterfeit per template" rule, made mechanical.

    A template is where the gate/receipt/park spine is actually wired, and it is
    the layer most likely to lose a guard to an ordinary-looking refactor — the
    package's own modules get read carefully, a graph-building function gets
    tidied. Registering a new template without an entry means the safety of
    everything built on it rests on somebody having remembered.
    """
    claimed = {entry.primary_template for entry in CORPUS if entry.primary_template}
    unknown = sorted(claimed - set(TEMPLATES))
    assert not unknown, f"corpus entries name templates that are not registered: {unknown}"

    uncovered = sorted(set(TEMPLATES) - claimed)
    assert not uncovered, (
        f"these shipped templates are the primary target of no counterfeit: {uncovered}. "
        "Add one that removes a guard the template itself is responsible for, and name a "
        "test that must go red."
    )


def test_the_learning_loop_is_covered() -> None:
    """Coverage of the learning loop is the one thing in this corpus that is not cuttable.

    See :data:`MIN_LEARNING_ENTRIES`. The predecessor's corpus was large, careful
    and entirely about the safety spine, which is why the learning loop could be
    fully wired, fully green and 100% starved without anything noticing.
    """
    learning = [entry.id for entry in CORPUS if entry.family == "learning"]
    assert len(learning) >= MIN_LEARNING_ENTRIES, (
        f"only {len(learning)} learning entries ({learning}); at least "
        f"{MIN_LEARNING_ENTRIES} are required. The safety half of this corpus is the "
        "half that gets written by default — this is the half that gets cut under "
        "deadline, and cutting it is the original failure repeating itself."
    )


def test_every_anchor_matches_exactly_once_in_the_real_tree() -> None:
    """Staleness, detected in milliseconds instead of after two minutes of subprocesses.

    :func:`harness.apply` enforces this per entry against a mutant copy anyway.
    Checking it here as well means a refactor that moved a guard is reported as
    one legible list rather than as the first entry that happens to run.
    """
    stale: list[str] = []
    for entry in CORPUS:
        target = harness.PROJECT_ROOT / entry.file
        if not target.is_file():
            stale.append(f"{entry.id}: {entry.file} does not exist")
            continue
        occurrences = target.read_text(encoding="utf-8").count(entry.find)
        if occurrences != 1:
            stale.append(f"{entry.id}: anchor matches {occurrences} times in {entry.file}")
    assert not stale, "the corpus is STALE:\n" + "\n".join(stale)


def test_every_must_fail_node_id_names_a_test_that_exists() -> None:
    """A witness that cannot be collected proves nothing and fails as a collection error.

    Checked structurally — the file exists and defines a function of that name —
    rather than by collecting pytest, because this must stay fast enough to run
    on every invocation and the failure mode it catches is a rename.
    """
    missing: list[str] = []
    for entry in CORPUS:
        for node in entry.must_fail:
            match = _NODE_RE.match(node)
            if match is None:
                missing.append(f"{entry.id}: {node!r} is not a `tests/<file>.py::test_<name>` id")
                continue
            path = harness.PROJECT_ROOT / match.group("file")
            if not path.is_file():
                missing.append(f"{entry.id}: {match.group('file')} does not exist")
                continue
            if f"def {match.group('name')}(" not in path.read_text(encoding="utf-8"):
                missing.append(f"{entry.id}: {node} names a test that is not defined there")
    assert not missing, "corpus witnesses that cannot be run:\n" + "\n".join(missing)


def test_no_entry_targets_the_counterfeit_suite_itself() -> None:
    """A corpus that mutates or runs itself is a corpus grading its own homework."""
    own = Path(__file__).name
    offenders = [
        entry.id
        for entry in CORPUS
        if entry.file.startswith("tests/counterfeits/")
        or any(own in node or "tests/counterfeits/" in node for node in entry.must_fail)
    ]
    assert not offenders, (
        f"these entries point the corpus at itself: {offenders}. A mutation run that "
        "re-enters this file recurses, and one that patches the harness makes the "
        "gate's own verdict a thing the mutation can decide."
    )


def test_every_undefended_entry_names_the_file_that_owes_the_test() -> None:
    """An acknowledged hole must say who fills it, or it is just an excuse.

    ``undefended_by`` is the only way an entry may be green while surviving, so
    the bar for writing one is that it reads as a work item: which module is
    unprotected, what the missing test must assert, and where it should live.
    """
    vague: list[str] = []
    for entry in CORPUS:
        if not entry.undefended_by:
            continue
        text = entry.undefended_by
        if "tests/" not in text:
            vague.append(f"{entry.id}: names no file that should hold the missing test")
        elif len(text) < 120:
            vague.append(f"{entry.id}: {text!r} does not say what the missing test must assert")
    assert not vague, "undefended entries that are not actionable:\n" + "\n".join(vague)


def test_the_corpus_is_not_quietly_all_xfail() -> None:
    """The gate must be mostly load-bearing, or it is a list of intentions.

    ``undefended_by`` exists so a real hole is tracked rather than dropped. It
    stops being that the moment it becomes the cheap way to green a red entry, so
    the majority of the corpus has to be entries that genuinely catch something.
    """
    undefended = [entry.id for entry in CORPUS if entry.undefended_by]
    assert len(undefended) * 2 < len(CORPUS), (
        f"{len(undefended)} of {len(CORPUS)} entries are marked undefended ({undefended}). "
        "At that ratio this file is documenting a suite rather than testing one."
    )
