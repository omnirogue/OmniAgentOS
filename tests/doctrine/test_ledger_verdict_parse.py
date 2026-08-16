"""The ledger must read the VERDICT LINE, never a substring of the prose.

Written after the ledger misreported in both directions at once: 21 sol approvals
counted as zero, and 19 anthropic approvals counted as rejections — because both
parsers asked `"REJECT" in text` and reviewers routinely write "no reason to reject".
A measurement that turns approvals into refusals is worse than no measurement, because
it is acted on.
"""

import importlib.util
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "fleet_ledger", Path(__file__).resolve().parents[2] / "scripts" / "fleet-ledger.py"
)
fleet_ledger = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fleet_ledger)


APPROVAL_WITH_REJECT_IN_PROSE = """# lane review

Reviewer: claude-opus-5.

I looked for a reason to reject this change and found none. Nothing here would be
rejected by the merge gate.

VERDICT: APPROVE
"""


def test_prose_containing_reject_does_not_flip_an_approval():
    assert fleet_ledger._verdict_word(APPROVAL_WITH_REJECT_IN_PROSE) == "APPROVE"


def test_a_real_rejection_is_still_a_rejection():
    assert fleet_ledger._verdict_word("blah\nVERDICT: REJECT — unwitnessed claim\n") == "REJECT"
    assert fleet_ledger._verdict_word("VERDICT: FAILED (no usable verdict)") == "FAIL"


def test_absent_verdict_line_is_none_not_approval():
    """An absent witness is a refusal, never a pass."""
    assert fleet_ledger._verdict_word("the tests all passed, looks good to me") is None


@pytest.mark.parametrize(
    "line", ["**VERDICT:** APPROVE", "VERDICT = APPROVE", "verdict: approved", "VERDICT - PASS"]
)
def test_formatting_variants_reviewers_actually_emit(line):
    assert fleet_ledger._verdict_word(line) == "APPROVE"


def test_the_repos_own_verdict_corpus_parses():
    """Far-side check: run the parser against every real verdict on disk. Any file with a
    VERDICT line that parses to UNCLEAR means the corpus has drifted past the parser."""
    repo = Path(__file__).resolve().parents[2]
    corpus = list((repo / "var" / "swarm" / "verdicts").glob("*.md")) + list(
        (repo / "var" / "swarm" / "sol-verdicts").glob("*.md")
    )
    if not corpus:
        pytest.skip("no verdict corpus on this checkout")
    unclear = [f.name for f in corpus if fleet_ledger._verdict_word(f.read_text(errors="ignore")) == "UNCLEAR"]
    assert not unclear, f"verdict lines the parser cannot classify: {unclear}"
