"""Ruling #4 (operator, 2026-08-09): exit-code 2 = COULD NOT RUN, estate-wide.

Mechanically pins the ratified semantics so a later edit cannot silently drift
them back to the pre-ruling scheme (where exit 2 read as "do not retry this
input" and could-not-run hid at 3). The three concepts are DISTINCT:

    1  candidate defect   — the code/input is wrong; fix it
    2  could not run      — the instrument/gate could not evaluate this input
                            (missing dep, dirty/moved workspace, unreadable
                            input); fix the mechanics, then re-run the SAME input
    3  do not retry       — a producer writer's genuine dead end (the id is
                            already filed / carries a live rejection or park)

These assertions are the vocabulary check the ruling asked for: constants,
cross-tool agreement, and the prose in the files that DEFINE the convention.
The behavioural exit codes themselves are exercised in test_file_proposal.py,
test_contract_lens.py and test_interpreter_remedy.py.
"""
from __future__ import annotations

import sys
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PKG / "bridge"))

import file_inquiry  # noqa: E402
import file_proposal  # noqa: E402

# --------------------------------------------------------------------------
# Constants — the carrier the ruling ratifies.
# --------------------------------------------------------------------------

def test_could_not_run_is_2():
    assert file_proposal.EXIT_COULD_NOT_RUN == 2


def test_do_not_retry_is_3():
    assert file_proposal.EXIT_REFUSED_DO_NOT_RETRY == 3


def test_the_three_codes_are_distinct():
    codes = {
        file_proposal.EXIT_WRITTEN,
        file_proposal.EXIT_REFUSED_FIXABLE,
        file_proposal.EXIT_COULD_NOT_RUN,
        file_proposal.EXIT_REFUSED_DO_NOT_RETRY,
    }
    assert codes == {0, 1, 2, 3}
    # 2 is neither the candidate-defect code nor the do-not-retry code.
    assert file_proposal.EXIT_COULD_NOT_RUN != file_proposal.EXIT_REFUSED_FIXABLE
    assert file_proposal.EXIT_COULD_NOT_RUN != file_proposal.EXIT_REFUSED_DO_NOT_RETRY


def test_file_inquiry_tracks_the_same_constants():
    # file_inquiry imports the constants from file_proposal, so the two writers
    # can never disagree on what 2 and 3 mean.
    assert file_inquiry.EXIT_COULD_NOT_RUN == 2
    assert file_inquiry.EXIT_REFUSED_DO_NOT_RETRY == 3
    assert file_inquiry.EXIT_COULD_NOT_RUN is file_proposal.EXIT_COULD_NOT_RUN
    assert file_inquiry.EXIT_REFUSED_DO_NOT_RETRY is file_proposal.EXIT_REFUSED_DO_NOT_RETRY


# --------------------------------------------------------------------------
# Prose — the files that DEFINE the convention must say 2 = could not run and
# must not still assert the retired "exit 2 = do not retry".
# --------------------------------------------------------------------------

CONTRACT = (PKG / "CONTRACT.md").read_text(encoding="utf-8")
FILE_PROPOSAL_SRC = (PKG / "bridge" / "file_proposal.py").read_text(encoding="utf-8")
FILE_INQUIRY_SRC = (PKG / "bridge" / "file_inquiry.py").read_text(encoding="utf-8")
VALIDATE_SRC = (PKG / "bridge" / "validate_envelope.py").read_text(encoding="utf-8")


def _norm(text: str) -> str:
    return text.lower().replace("-", " ").replace("`", "").replace("*", "")


def test_contract_table_row_2_is_could_not_run():
    body = _norm(CONTRACT)
    # The §8(a) table row for code 2.
    assert "| 2 | could not run" in body or "|2| could not run" in body


def test_contract_carries_the_ruling4_ratification():
    body = _norm(CONTRACT)
    assert "ruling #4" in body
    assert "exit 2 = could not run" in body or "exit 2 = could not run." in body


def test_contract_does_not_define_exit_2_as_do_not_retry():
    # The retired assertion: the §8(a) table must not map 2 -> "do not retry".
    body = _norm(CONTRACT)
    assert "| 2 | do not retry" not in body
    assert "|2| do not retry" not in body


def test_producer_tool_docstrings_state_2_could_not_run_3_do_not_retry():
    for src in (FILE_PROPOSAL_SRC, FILE_INQUIRY_SRC):
        body = _norm(src)
        assert "2 could not run" in body or "2   could not run" in body
        assert "3 refused, do not retry" in body or "3 refused do not retry" in body \
            or "3 do not retry" in body or "3   refused, do not retry" in body


def test_validate_envelope_still_2_could_not_run():
    body = _norm(VALIDATE_SRC)
    assert "exit 2 if jsonschema is not importable" in body
    assert "exit 2 = could not run" in body
