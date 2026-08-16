"""scripts/golden-suite/run_golden.py::load_policy -- prompt.md's fenced
```yaml policy: block is parsed at runtime so editing the file (by hand, or
via the dashboard's file editor) genuinely changes behavior; a malformed or
missing block must fall back to code defaults and say so, never crash.

Imported via `importlib.import_module` for the same reason
tests/golden/test_history_stats.py is: `scripts/golden-suite` is a
hyphenated directory name, never a valid dotted import path.
"""

from __future__ import annotations

import importlib
from pathlib import Path

run_golden = importlib.import_module("scripts.golden-suite.run_golden")


def test_policy_override_from_prompt_md_is_honored(tmp_path: Path) -> None:
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text(
        "# some narrative text a human reads\n\n"
        "```yaml\n"
        "policy:\n"
        "  regression_threshold_pct: 40\n"
        "  consecutive_nights: 3\n"
        "  benchmarks_file: other-benchmarks.yaml\n"
        "```\n"
    )
    policy, warnings = run_golden.load_policy(prompt_path)
    assert warnings == []
    assert policy.regression_threshold_pct == 40.0
    assert policy.consecutive_nights == 3
    assert policy.benchmarks_file == "other-benchmarks.yaml"
    # Keys the block didn't mention keep their code defaults.
    assert policy.rolling_window == run_golden.Policy().rolling_window
    assert policy.default_timeout_minutes == run_golden.Policy().default_timeout_minutes


def test_policy_malformed_yaml_falls_back_to_defaults_and_logs(tmp_path: Path) -> None:
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("```yaml\npolicy:\n  regression_threshold_pct: [not, closed\n```\n")
    policy, warnings = run_golden.load_policy(prompt_path)
    assert policy == run_golden.Policy()
    assert len(warnings) == 1
    assert "malformed YAML" in warnings[0]


def test_policy_missing_fenced_block_falls_back_to_defaults_and_logs(tmp_path: Path) -> None:
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("just some narrative text, no policy block at all\n")
    policy, warnings = run_golden.load_policy(prompt_path)
    assert policy == run_golden.Policy()
    assert len(warnings) == 1
    assert "no fenced" in warnings[0]


def test_policy_missing_file_falls_back_to_defaults_and_logs(tmp_path: Path) -> None:
    policy, warnings = run_golden.load_policy(tmp_path / "does-not-exist.md")
    assert policy == run_golden.Policy()
    assert len(warnings) == 1
    assert "unreadable" in warnings[0]


def test_policy_invalid_key_type_falls_back_to_default_for_that_key_only(tmp_path: Path) -> None:
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text(
        "```yaml\npolicy:\n  regression_threshold_pct: not-a-number\n  consecutive_nights: 4\n```\n"
    )
    policy, warnings = run_golden.load_policy(prompt_path)
    assert policy.regression_threshold_pct == run_golden.Policy().regression_threshold_pct
    assert policy.consecutive_nights == 4
    assert len(warnings) == 1
    assert "policy.regression_threshold_pct" in warnings[0]


def test_real_prompt_md_parses_with_no_warnings() -> None:
    """The shipped prompt.md itself must parse cleanly (regression guard for
    a future hand-edit that breaks its own fenced block)."""
    real_prompt = Path("scripts/golden-suite/prompt.md")
    policy, warnings = run_golden.load_policy(real_prompt)
    assert warnings == []
    assert policy.benchmarks_file == "benchmarks.yaml"
