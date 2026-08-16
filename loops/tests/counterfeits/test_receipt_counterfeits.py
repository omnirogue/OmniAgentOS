"""The counterfeit gate for the receipt OUTCOME invariants (P2).

Same harness and same rules as ``test_counterfeits.py``; a separate manifest
(``corpus_receipts.toml``) so this lane adds entries without editing a line of
the corpus lane's file.

    loops/bin/loop-tests --counterfeits -k receipt_counterfeit
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from harness import check, control, load_corpus  # noqa: E402

CORPUS = load_corpus(HERE / "corpus_receipts.toml")


@pytest.fixture(scope="module")
def receipt_control_pass(tmp_path_factory):
    """Every must_fail node must be GREEN unmutated, or the corpus proves nothing."""
    result = control(tmp_path_factory.mktemp("cf-receipt-control"), CORPUS)
    assert result.returncode == 0, (
        "control pass is not green — a corpus entry points at an already-failing "
        f"test:\n{(result.stdout + result.stderr)[-4000:]}"
    )
    return result


@pytest.mark.parametrize("entry", CORPUS, ids=[entry.id for entry in CORPUS])
def test_receipt_counterfeit_is_caught(entry, receipt_control_pass, tmp_path):
    check(tmp_path, entry)
