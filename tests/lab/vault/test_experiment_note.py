"""render_experiment_note (contracts/lab-interfaces.md §L08-labvault).

Acceptance (task brief): the note renders the frozen frontmatter + resolving
wikilinks + the actual content — "an experiment note shows scorecard incl.
audit_flags, NO held-out expected"."""

from __future__ import annotations

from pathlib import Path

from omniagentos.contracts import NoteType
from omniagentos.lab.vault import render_experiment_note
from omniagentos.vault import parse_frontmatter, write_note

from .helpers import sample_eval_results, sample_experiment, sample_scorecard


def test_frontmatter_is_the_frozen_eight_field_set() -> None:
    relpath, content = render_experiment_note(
        sample_experiment(), sample_eval_results(), sample_scorecard()
    )
    fm = parse_frontmatter(content)  # raises if extra/missing fields
    assert fm.id == "exp_test0000000000000001"
    assert fm.type == NoteType.EXPERIMENT
    assert fm.discipline == "coding"
    assert fm.source_run is None
    assert fm.status == "active"
    assert relpath == "experiments/exp_test0000000000000001.md"


def test_wikilinks_to_surfaces_discipline_and_home() -> None:
    _relpath, content = render_experiment_note(
        sample_experiment(), sample_eval_results(), sample_scorecard()
    )
    assert "[[srf_champion0000000001]]" in content
    assert "[[srf_challenger000001]]" in content
    assert "[[coding]]" in content
    assert "[[Home]]" in content


def test_hypothesis_is_the_human_title_not_a_wikilink() -> None:
    _relpath, content = render_experiment_note(
        sample_experiment(), sample_eval_results(), sample_scorecard()
    )
    assert "# experiment: Adding a regression-test requirement improves bugfix correctness" in (
        content
    )


def test_scorecard_section_shows_metrics_delta_and_audit_flags() -> None:
    _relpath, content = render_experiment_note(
        sample_experiment(),
        sample_eval_results(),
        sample_scorecard(audit_flags=["metric_jump:pass_rate"]),
    )
    assert "## Scorecard" in content
    assert "pass_rate=0.72" in content  # champion
    assert "pass_rate=0.81" in content  # challenger
    assert "0.0900" in content  # primary_delta
    assert "`metric_jump:pass_rate`" in content
    assert "forces HUMAN_REVIEW" in content


def test_scorecard_audit_flags_shown_as_none_when_empty() -> None:
    _relpath, content = render_experiment_note(
        sample_experiment(), sample_eval_results(), sample_scorecard(audit_flags=[])
    )
    assert "**Audit flags:** _none_" in content


def test_missing_scorecard_renders_without_crashing() -> None:
    relpath, content = render_experiment_note(sample_experiment(), [], {})
    assert relpath
    assert "_No scorecard yet" in content
    assert "**Audit flags:** _none_" in content


def test_eval_results_table_shows_arm_split_and_metrics() -> None:
    _relpath, content = render_experiment_note(
        sample_experiment(), sample_eval_results(), sample_scorecard()
    )
    assert "| champion | dev | 0 | 1 | pass | cost_usd=0.1, pass_rate=0.72 | 2 |" in content
    assert "| challenger | dev | 0 | 1 | pass | cost_usd=0.11, pass_rate=0.81 | 2 |" in content


def test_never_renders_held_out_expected_even_if_smuggled_in() -> None:
    malicious_scorecard = sample_scorecard()
    malicious_scorecard["expected"] = {"c1": "SECRET_HELD_OUT_ANSWER"}
    malicious_scorecard["champion"] = {"pass_rate": 0.72, "expected": "SECRET_CHAMPION_LEAK"}
    malicious_results = sample_eval_results()
    malicious_results[0] = dict(malicious_results[0])
    malicious_results[0]["metrics_json"] = (
        '{"pass_rate": 0.72, "expected": "SECRET_RESULT_LEAK"}'
    )
    malicious_results[0]["per_case_json"] = (
        '{"c1": {"score": 1.0, "expected": "SECRET_PER_CASE_LEAK"}}'
    )

    _relpath, content = render_experiment_note(
        sample_experiment(), malicious_results, malicious_scorecard
    )

    assert "SECRET_HELD_OUT_ANSWER" not in content
    assert "SECRET_CHAMPION_LEAK" not in content
    assert "SECRET_RESULT_LEAK" not in content
    assert "SECRET_PER_CASE_LEAK" not in content
    assert "expected" not in content.lower()


def test_partial_dict_does_not_crash() -> None:
    relpath, content = render_experiment_note({"id": "exp_bare"}, [], {})
    assert relpath == "experiments/exp_bare.md"
    assert "# experiment: exp_bare" in content
    assert "[[Home]]" in content


def test_tolerates_pre_decoded_pydantic_style_dicts() -> None:
    """`results` may also be Experiment/EvalResult.model_dump()s (nested
    dicts already decoded) rather than raw Store rows with *_json strings."""
    results = [
        {
            "arm": "champion",
            "split": "dev",
            "replicate": 0,
            "suite_version": 1,
            "deterministic_passed": True,
            "metrics": {"pass_rate": 0.5},
            "per_case": {"c1": {"pass_rate": 0.5}},
            "created_at": "2026-07-11T11:00:00Z",
        }
    ]
    _relpath, content = render_experiment_note(sample_experiment(), results, sample_scorecard())
    assert "pass_rate=0.5" in content


def test_full_round_trip_confined_and_resolving(vault_dir: Path) -> None:
    relpath, content = render_experiment_note(
        sample_experiment(), sample_eval_results(), sample_scorecard()
    )
    abs_path = write_note(str(vault_dir), relpath, content, autocommit=False)
    assert Path(abs_path).is_relative_to(vault_dir)
    written = Path(abs_path).read_text(encoding="utf-8")
    parse_frontmatter(written)  # round-trips cleanly
    assert (vault_dir / "Home.md").is_file()
    assert "[[Home]]" in written


def test_malicious_experiment_id_stays_confined_to_the_vault(vault_dir: Path) -> None:
    relpath, content = render_experiment_note(
        sample_experiment(id="../../etc/passwd"), [], {}
    )
    abs_path = write_note(str(vault_dir), relpath, content, autocommit=False)
    assert Path(abs_path).is_relative_to(vault_dir)
    assert Path(abs_path).is_file()
