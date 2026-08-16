"""sentinel.load_policy -- runtime policy parsed from prompt.md's fenced
```yaml policy:``` block. Addendum requirement: override honored; malformed
(missing file, missing block, bad yaml, missing key, wrong-typed field) ->
defaults, logged."""

from __future__ import annotations

import logging
from pathlib import Path
from types import ModuleType

import pytest

VALID_BLOCK = """# provider-sentinel

Some narrative text above the policy block, exactly like the real file.

```yaml
policy:
  session_remaining_alert_pct: 25
  consecutive_fail_nights: 3
  disable_on_auth_failure: false
```
"""


def test_defaults_when_file_missing(sentinel: ModuleType, tmp_path: Path) -> None:
    policy = sentinel.load_policy(tmp_path / "does-not-exist.md")
    assert policy == sentinel.DEFAULT_POLICY


def test_override_honored(sentinel: ModuleType, tmp_path: Path) -> None:
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text(VALID_BLOCK, encoding="utf-8")

    policy = sentinel.load_policy(prompt_path)

    assert policy == {
        "session_remaining_alert_pct": 25.0,
        "consecutive_fail_nights": 3,
        "disable_on_auth_failure": False,
    }
    # And the override genuinely differs from the shipped defaults -- this
    # is the "dashboard edits change behavior" contract, not a no-op parse.
    assert policy != sentinel.DEFAULT_POLICY


def test_malformed_yaml_falls_back_to_defaults(
    sentinel: ModuleType, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text(
        "```yaml\npolicy:\n  session_remaining_alert_pct: [unterminated\n```\n",
        encoding="utf-8",
    )
    with caplog.at_level(logging.WARNING):
        policy = sentinel.load_policy(prompt_path)
    assert policy == sentinel.DEFAULT_POLICY
    assert any("yaml parse error" in message for message in caplog.messages)


def test_missing_yaml_block_falls_back_to_defaults(
    sentinel: ModuleType, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("# provider-sentinel\n\nNo policy block here.\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        policy = sentinel.load_policy(prompt_path)
    assert policy == sentinel.DEFAULT_POLICY
    assert any("no fenced yaml block" in message for message in caplog.messages)


def test_missing_policy_key_falls_back_to_defaults(sentinel: ModuleType, tmp_path: Path) -> None:
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("```yaml\nnot_policy:\n  foo: 1\n```\n", encoding="utf-8")
    assert sentinel.load_policy(prompt_path) == sentinel.DEFAULT_POLICY


def test_per_field_fallback_keeps_valid_fields(
    sentinel: ModuleType, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A single bad field degrades to ITS default only -- the rest of a
    partially-valid block still applies."""
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text(
        "```yaml\n"
        "policy:\n"
        "  session_remaining_alert_pct: not-a-number\n"
        "  consecutive_fail_nights: 5\n"
        "  disable_on_auth_failure: false\n"
        "```\n",
        encoding="utf-8",
    )
    with caplog.at_level(logging.WARNING):
        policy = sentinel.load_policy(prompt_path)
    assert (
        policy["session_remaining_alert_pct"]
        == sentinel.DEFAULT_POLICY["session_remaining_alert_pct"]
    )
    assert policy["consecutive_fail_nights"] == 5
    assert policy["disable_on_auth_failure"] is False
    assert any("invalid session_remaining_alert_pct" in message for message in caplog.messages)


def test_out_of_range_percent_falls_back(sentinel: ModuleType, tmp_path: Path) -> None:
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text(
        "```yaml\npolicy:\n  session_remaining_alert_pct: 150\n```\n", encoding="utf-8"
    )
    policy = sentinel.load_policy(prompt_path)
    assert (
        policy["session_remaining_alert_pct"]
        == sentinel.DEFAULT_POLICY["session_remaining_alert_pct"]
    )


def test_real_prompt_md_parses_to_documented_defaults(sentinel: ModuleType) -> None:
    """The shipped prompt.md's own policy block matches DEFAULT_POLICY (the
    doc and the code must never silently disagree)."""
    policy = sentinel.load_policy(sentinel.DEFAULT_PROMPT_PATH)
    assert policy == sentinel.DEFAULT_POLICY
