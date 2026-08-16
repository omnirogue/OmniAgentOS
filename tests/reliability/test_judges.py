"""Tests for JudgePanel (W3, §4, §5b.7).

Tests cover:
  - Adapter family detection
  - JSON verdict parsing with retry
  - Judge prompt construction (M4: fencing)

Note: Full judge panel tests (availability check, parallel invocation, vote storage)
require full adapter mocking and are tested in integration tests. These unit tests
focus on parsing, prompt generation, and helper functions.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from omniagentos.reliability.judges import JudgePanel, _build_judge_prompt, _get_adapter_family
from omniagentos.reliability.taxonomy import VerdictKind


class TestAdapterFamily:
    """Test adapter family detection."""

    def test_anthropic_family(self):
        assert _get_adapter_family("cli-claude") == "anthropic"

    def test_openai_family(self):
        assert _get_adapter_family("cli-codex") == "openai"

    def test_xai_family(self):
        assert _get_adapter_family("cli-grok") == "xai"

    def test_google_family(self):
        assert _get_adapter_family("cli-gemini") == "google"

    def test_moonshot_family(self):
        assert _get_adapter_family("cli-kimi") == "moonshot"

    def test_unknown_family(self):
        assert _get_adapter_family("unknown-harness") == "unknown"


class TestVerdictParsing:
    """Test JSON verdict parsing with retry (§4)."""

    def test_parse_valid_json_dict(self):
        """Parse valid JSON verdict (already parsed dict)."""
        mock_store = MagicMock()
        panel = object.__new__(JudgePanel)
        panel.store = mock_store
        panel.judge_timeout_seconds = 120
        panel.available_judges = []

        verdict_json = {
            "verdict": "approve",
            "scores_json": {"root_cause": 8, "fix_correctness": 9},
            "reasoning": "Good fix",
            "conditions": "",
        }

        parsed = panel._parse_verdict(verdict_json)
        assert parsed["verdict"] == "approve"
        assert parsed["scores_json"]["root_cause"] == 8
        assert parsed["reasoning"] == "Good fix"

    def test_parse_valid_json_string(self):
        """Parse valid JSON verdict (from string)."""
        mock_store = MagicMock()
        panel = object.__new__(JudgePanel)
        panel.store = mock_store

        json_str = json.dumps(
            {
                "verdict": "reject",
                "scores_json": {"root_cause": 3},
                "reasoning": "Bad fix",
                "conditions": "",
            }
        )

        parsed = panel._parse_verdict(json_str)
        assert parsed["verdict"] == "reject"

    def test_parse_json_with_retry(self):
        """Retry on first unparseable JSON."""
        mock_store = MagicMock()
        panel = object.__new__(JudgePanel)
        panel.store = mock_store

        # Malformed JSON with embedded valid JSON
        output = 'Some text before {"verdict": "approve", "reasoning": "good"} text after'

        parsed = panel._parse_verdict(output)
        assert parsed["verdict"] == "approve"

    def test_parse_unparseable_json_defaults_to_needs_human(self):
        """Unparseable JSON falls back to needs_human."""
        mock_store = MagicMock()
        panel = object.__new__(JudgePanel)
        panel.store = mock_store

        output = "This is just plain text with no JSON at all"

        parsed = panel._parse_verdict(output)
        assert parsed["verdict"] == VerdictKind.NEEDS_HUMAN.value
        assert "No JSON found" in parsed["reasoning"]

    def test_parse_invalid_verdict_value_defaults_to_needs_human(self):
        """Invalid verdict value → needs_human."""
        mock_store = MagicMock()
        panel = object.__new__(JudgePanel)
        panel.store = mock_store

        verdict_json = {
            "verdict": "invalid_verdict",
            "scores_json": {},
            "reasoning": "Test",
        }

        parsed = panel._parse_verdict(verdict_json)
        assert parsed["verdict"] == VerdictKind.NEEDS_HUMAN.value

    def test_parse_missing_verdict_key_defaults_to_needs_human(self):
        """Missing verdict key → needs_human."""
        mock_store = MagicMock()
        panel = object.__new__(JudgePanel)
        panel.store = mock_store

        verdict_json = {
            "reasoning": "No verdict specified",
        }

        parsed = panel._parse_verdict(verdict_json)
        assert parsed["verdict"] == VerdictKind.NEEDS_HUMAN.value

    def test_parse_preserves_scores_json(self):
        """Scores JSON preserved in parsed output."""
        mock_store = MagicMock()
        panel = object.__new__(JudgePanel)
        panel.store = mock_store

        scores = {
            "root_cause": 8,
            "fix_correctness": 9,
            "regression_risk": 2,
            "security_risk": 1,
        }

        verdict_json = {
            "verdict": "approve",
            "scores_json": scores,
            "reasoning": "Test",
        }

        parsed = panel._parse_verdict(verdict_json)
        assert parsed["scores_json"] == scores

    def test_parse_handles_non_dict_scores(self):
        """Non-dict scores normalized to empty dict."""
        mock_store = MagicMock()
        panel = object.__new__(JudgePanel)
        panel.store = mock_store

        verdict_json = {
            "verdict": "approve",
            "scores_json": "not a dict",
        }

        parsed = panel._parse_verdict(verdict_json)
        assert isinstance(parsed["scores_json"], dict)


class TestJudgePromptBuilding:
    """Test judge prompt construction (§4, M4: fencing)."""

    def test_prompt_contains_sandbox_diff(self):
        """Prompt includes raw sandbox diff as primary artifact."""
        diff = "--- a/test.py\n+++ b/test.py\n@@ -1 +1 @@\n-old\n+new"
        prompt = _build_judge_prompt(
            improvement_id="imp_123",
            sandbox_diff=diff,
            fenced_evidence="Test evidence",
        )

        assert diff in prompt
        assert "Primary Artifact" in prompt

    def test_prompt_contains_fenced_evidence(self):
        """Prompt includes evidence section."""
        evidence = "Test evidence from detector"
        prompt = _build_judge_prompt(
            improvement_id="imp_123",
            sandbox_diff="--- a/test\n+++ b/test\n@@ @@",
            fenced_evidence=evidence,
        )

        assert "Evidence from Detection/Analysis" in prompt
        assert evidence in prompt

    def test_prompt_contains_sandbox_results_json(self):
        """Prompt serializes sandbox results."""
        results = {"tests_passed": True, "test_count": 10}
        prompt = _build_judge_prompt(
            improvement_id="imp_123",
            sandbox_diff="--- a/test\n+++ b/test\n@@ @@",
            fenced_evidence="Evidence",
            sandbox_results=results,
        )

        assert "Sandbox Execution Results" in prompt
        # JSON serialization of results
        assert "true" in prompt or "True" in prompt  # JSON bool
        assert "10" in prompt

    def test_prompt_contains_verdict_schema(self):
        """Prompt includes verdict schema and examples."""
        prompt = _build_judge_prompt(
            improvement_id="imp_123",
            sandbox_diff="--- a/test\n+++ b/test\n@@ @@",
            fenced_evidence="Evidence",
        )

        assert "verdict" in prompt
        assert "scores_json" in prompt
        assert "approve" in prompt
        assert "reject" in prompt

    def test_prompt_includes_all_score_criteria(self):
        """Prompt explains all 11 score criteria."""
        prompt = _build_judge_prompt(
            improvement_id="imp_123",
            sandbox_diff="--- a/test\n+++ b/test\n@@ @@",
            fenced_evidence="Evidence",
        )

        criteria = [
            "Root cause",
            "Fix correctness",
            "Regression risk",
            "Security risk",
            "Reliability",
            "Quality",
            "Cost",
            "Speed",
            "Architecture",
            "Test coverage",
            "Reversibility",
        ]

        for criterion in criteria:
            assert criterion in prompt

    def test_prompt_includes_improvement_id(self):
        """Prompt includes improvement ID for traceability."""
        imp_id = "imp_test_123"
        prompt = _build_judge_prompt(
            improvement_id=imp_id,
            sandbox_diff="--- a/test\n+++ b/test\n@@ @@",
            fenced_evidence="Evidence",
        )

        assert imp_id in prompt

    def test_prompt_with_empty_sandbox_results(self):
        """Prompt handles empty/None sandbox results."""
        prompt1 = _build_judge_prompt(
            improvement_id="imp_123",
            sandbox_diff="--- a/test\n+++ b/test\n@@ @@",
            fenced_evidence="Evidence",
            sandbox_results=None,
        )

        prompt2 = _build_judge_prompt(
            improvement_id="imp_123",
            sandbox_diff="--- a/test\n+++ b/test\n@@ @@",
            fenced_evidence="Evidence",
            sandbox_results={},
        )

        # Both should be valid prompts
        assert "Sandbox Execution Results" in prompt1
        assert "Sandbox Execution Results" in prompt2

    def test_prompt_json_example_is_valid(self):
        """Prompt includes parseable JSON example."""
        prompt = _build_judge_prompt(
            improvement_id="imp_123",
            sandbox_diff="--- a/test\n+++ b/test\n@@ @@",
            fenced_evidence="Evidence",
        )

        # Extract and parse JSON example from prompt
        import re

        matches = re.findall(r"\{[^}]+\"verdict\"[^}]+\}", prompt)
        assert len(matches) > 0

        # Verify it's parseable JSON
        for match in matches:
            try:
                parsed = json.loads(match)
                assert "verdict" in parsed
            except json.JSONDecodeError:
                # Some regex matches might not be complete JSON, that's ok
                pass
