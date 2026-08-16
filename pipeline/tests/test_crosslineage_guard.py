"""Risk-tiered build-review admission tests.

Routine work goes straight to the mandatory mechanical gate.  A risky real
Git diff must carry a named, final approval from a known different lineage,
bound to the immutable candidate SHA.  Producer assertions and declared paths
do not decide the tier.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from verdict_absence_matrix import (  # noqa: E402
    ABSENT,
    envelope,
    run_candidate,
    run_shape,
    scratch_repo,
)

ROOT = HERE.parent


def _fixture_parts(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("xlineage")
    repo, base, tip = scratch_repo(tmp)
    root = tmp / "root"
    (root / "var" / "loopqueue").mkdir(parents=True)
    return repo, root, ROOT / "schema", base, tip


@pytest.fixture(scope="module")
def gate(tmp_path_factory):
    repo, root, schema_dir, base, tip = _fixture_parts(tmp_path_factory)

    def _run(verdicts, producer_extra=None):
        art = envelope(base, tip, verdicts, producer_extra=producer_extra)
        return run_shape(repo, root, schema_dir, art)

    return _run


@pytest.fixture(scope="module")
def gate_candidate(tmp_path_factory):
    repo, root, schema_dir, base, tip = _fixture_parts(tmp_path_factory)

    def _run(verdicts, producer_extra=None):
        art = envelope(base, tip, verdicts, producer_extra=producer_extra)
        return run_candidate(repo, root, schema_dir, art)

    return _run


ABSENCE_SHAPES = [
    pytest.param(ABSENT, None, id="no-verdicts-key"),
    pytest.param(None, None, id="verdicts-null"),
    pytest.param([], None, id="verdicts-empty-list"),
    pytest.param({}, None, id="verdicts-object-not-list"),
    pytest.param("approved", None, id="verdicts-string"),
    pytest.param([{}], None, id="entry-with-no-lineage"),
    pytest.param(["approve"], None, id="entry-not-a-dict"),
    pytest.param(
        [{"reviewer": "opus", "verdict": "approve"}],
        None,
        id="review-lineage-omitted",
    ),
    pytest.param(
        [
            {"reviewer": "opus", "lineage": "anthropic", "verdict": "approve"},
            {"reviewer": "mystery", "verdict": "approve"},
        ],
        None,
        id="same-lineage-plus-lineageless-entry",
    ),
    pytest.param(
        [{"reviewer": "opus", "lineage": "anthropic", "verdict": "approve"}],
        {"lineage": None},
        id="producer-lineage-omitted",
    ),
]


@pytest.mark.parametrize("verdicts,producer_extra", ABSENCE_SHAPES)
def test_risky_diff_without_exact_cross_lineage_approval_is_refused(
    gate, verdicts, producer_extra
):
    outcome, reason = gate(verdicts, producer_extra)
    assert outcome == "REFUSED"
    assert "risky real diff" in reason
    assert "reviewed_sha=" in reason


def test_honest_same_lineage_is_refused(gate):
    outcome, reason = gate(
        [{"reviewer": "opus", "lineage": "anthropic", "verdict": "approve"}]
    )
    assert outcome == "REFUSED"
    assert "lineage" in reason


def test_proper_exact_cross_lineage_is_admitted(gate):
    outcome, reason = gate(
        [{"reviewer": "gemini", "lineage": "google", "verdict": "approve"}]
    )
    assert outcome == "ADMITTED", reason


def test_one_valid_cross_lineage_verdict_is_enough(gate):
    outcome, reason = gate(
        [
            {"reviewer": "opus", "lineage": "anthropic", "verdict": "approve"},
            {"reviewer": "sol", "lineage": "openai", "verdict": "approve"},
        ]
    )
    assert outcome == "ADMITTED", reason


@pytest.mark.parametrize("decision", ["request-changes", "rework", "approve-with-fix"])
def test_non_final_or_conditional_decision_is_refused(gate, decision):
    outcome, _ = gate(
        [{"reviewer": "sol", "lineage": "openai", "verdict": decision}]
    )
    assert outcome == "REFUSED"


def test_approval_for_a_different_sha_is_refused(gate_candidate):
    cand = gate_candidate(
        [
            {
                "reviewer": "sol",
                "lineage": "openai",
                "reviewed_sha": "f" * 40,
                "verdict": "approve",
            }
        ]
    )
    assert cand.refusal is not None
    assert "reviewed_sha=" in cand.refusal.reason


@pytest.mark.parametrize("spoof", ["Anthropic", "ANTHROPIC", " anthropic", "anthropic "])
def test_case_and_whitespace_do_not_create_a_second_lineage(gate, spoof):
    outcome, _ = gate(
        [{"reviewer": "r", "lineage": spoof, "verdict": "approve"}]
    )
    assert outcome == "REFUSED"


def test_self_declared_third_party_never_bypasses_risky_review(gate):
    without, _ = gate(ABSENT)
    with_flag, _ = gate(ABSENT, {"third_party": True})
    assert (without, with_flag) == ("REFUSED", "REFUSED")


def test_routine_real_diff_needs_no_separate_verdict(tmp_path):
    from verdict_absence_matrix import _git

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo.parent, "init", "-q", "-b", "main", str(repo))
    _git(repo, "config", "user.email", "probe@example.invalid")
    _git(repo, "config", "user.name", "probe")
    routine = repo / "widgets" / "render.py"
    routine.parent.mkdir(parents=True)
    routine.write_text("base\n")
    _git(repo, "add", "widgets/render.py")
    _git(repo, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "-b", "lane/routine")
    routine.write_text("changed\n")
    _git(repo, "commit", "-qam", "routine")
    tip = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "main")
    root = tmp_path / "root"
    (root / "var" / "loopqueue").mkdir(parents=True)
    art = envelope(base, tip, ABSENT)
    art["paths"] = ["widgets/render.py"]
    outcome, reason = run_shape(repo, root, ROOT / "schema", art)
    assert outcome == "ADMITTED", reason
