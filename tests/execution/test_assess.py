from __future__ import annotations

from pathlib import Path

from omniagentos.execution.assess import AssessmentContext, assess, fix_scope_after_fail


def test_rung_zero_fails_for_missing_expected_file(tmp_path: Path) -> None:
    result = assess(working_dir=tmp_path, expected_files=["missing.txt"])
    assert result["verdict"] == "fail"
    assert result["rung"] == 0
    assert result["mechanical"]["missing"] == ["missing.txt"]


def test_rung_zero_passes_for_contentful_expected_file(tmp_path: Path) -> None:
    (tmp_path / "output.txt").write_text("done\n")
    result = assess(working_dir=tmp_path, expected_files=["output.txt"])
    assert result["verdict"] == "pass"
    assert result["rung"] == 0


def test_injectable_assessor_at_rung_one(tmp_path: Path) -> None:
    result = assess(
        working_dir=tmp_path,
        assessor=lambda context: {"verdict": "fail", "reasons": ["no"]},
        max_rung=1,
    )
    assert result["verdict"] == "fail"
    assert result["rung"] == 1
    assert result["assessor"]["attempts"][0]["verdict"] == "fail"


def test_rung_two_retries_with_more_budget(tmp_path: Path) -> None:
    calls: list[bool] = []

    def assessor(context: AssessmentContext) -> str:
        more_budget = context.more_budget
        calls.append(more_budget)
        return "pass" if more_budget else "needs_review"

    result = assess(working_dir=tmp_path, assessor=assessor, max_rung=2)
    assert calls == [False, True]
    assert result["rung"] == 2
    assert result["verdict"] == "pass"


def test_fix_scope_after_fail_never_widens() -> None:
    fixed = fix_scope_after_fail(["a.py", "b.py", "c.py"], ["b.py", "d.py"])
    assert set(fixed) == {"b.py"}


def test_missing_judges_do_not_break_assessment(tmp_path: Path) -> None:
    result = assess(working_dir=tmp_path, judges=None, execution_level=4)
    assert result["verdict"] == "pass"
    assert result["judges"] is None


def test_judges_run_for_level_four(tmp_path: Path) -> None:
    class Judges:
        def review(self, context: object) -> dict[str, object]:
            return {"verdict": "pass", "reasons": ["panel agrees"]}

    result = assess(working_dir=tmp_path, judges=Judges(), execution_level=4)
    assert result["rung"] == 3
    assert result["verdict"] == "pass"
