"""The counterfeit gate for the loop runtime.

Excluded from the default lane (it spawns a pytest subprocess per entry). Run:

    loops/bin/loop-tests --counterfeits -k counterfeit
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import check, control, load_corpus  # noqa: E402

CORPUS = load_corpus()


@pytest.fixture(scope="module")
def control_pass(tmp_path_factory):
    """Every must_fail node must be GREEN unmutated, or the corpus proves nothing."""
    result = control(tmp_path_factory.mktemp("cf-control"), CORPUS)
    assert result.returncode == 0, (
        "control pass is not green — a corpus entry points at an already-failing "
        f"test:\n{(result.stdout + result.stderr)[-4000:]}"
    )
    return result


def test_every_template_is_the_primary_target_of_a_counterfeit():
    """Every graph template is covered; standalone drivers are explicit."""
    from omniagentos_loops.templates import TEMPLATE_NAMES

    primaries = {entry.primary_template for entry in CORPUS if entry.primary_template}
    assert primaries <= TEMPLATE_NAMES, primaries - TEMPLATE_NAMES
    missing = TEMPLATE_NAMES - primaries
    assert not missing, f"no counterfeit targets: {sorted(missing)}"
    modules = {
        path.stem
        for path in (Path(__file__).resolve().parents[2] / "omniagentos_loops" / "templates").glob(
            "*.py"
        )
        if path.stem not in {"__init__", "common"}
    }
    # measure_gap_act is a cadence-driven dry-run controller, not a LoopTemplate
    # graph. Naming it here prevents it from silently dodging this inventory.
    standalone_non_templates = {"measure_gap_act"}
    assert modules == TEMPLATE_NAMES | standalone_non_templates


@pytest.mark.parametrize("entry", CORPUS, ids=[entry.id for entry in CORPUS])
def test_counterfeit_is_caught(entry, control_pass, tmp_path):
    check(tmp_path, entry)
