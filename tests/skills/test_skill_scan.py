"""Tests for the skill content security scanner."""

from __future__ import annotations

from pathlib import Path

import pytest

from omniagentos.skills.scan import (
    Finding,
    ScanResult,
    scan_content,
    scan_file,
    scan_multiple,
)


class TestScanContentBasic:
    """Test basic scanning functionality."""

    def test_scan_clean_content_passes(self, clean_content: str) -> None:
        """Clean content should pass scanning with no findings."""
        result = scan_content(clean_content)
        assert result.passed is True
        assert len(result.findings) == 0
        assert "passed" in result.summary.lower()

    def test_scan_result_structure(self, clean_content: str) -> None:
        """ScanResult should have the expected structure."""
        result = scan_content(clean_content)
        assert isinstance(result.passed, bool)
        assert isinstance(result.findings, list)
        assert isinstance(result.summary, str)
        assert all(isinstance(f, Finding) for f in result.findings)

    def test_finding_attributes(self, content_with_api_key: str) -> None:
        """Finding objects should have required attributes."""
        result = scan_content(content_with_api_key)
        assert len(result.findings) > 0
        finding = result.findings[0]
        assert hasattr(finding, "severity")
        assert hasattr(finding, "category")
        assert hasattr(finding, "pattern")
        assert hasattr(finding, "message")
        assert finding.severity in ("low", "medium", "high", "critical")


class TestSecretDetection:
    """Test detection of secrets and credentials."""

    def test_detect_api_key(self, content_with_api_key: str) -> None:
        """Should detect API keys."""
        result = scan_content(content_with_api_key)
        assert not result.passed
        assert any(f.category == "secret" for f in result.findings)
        assert any("api" in f.pattern.lower() for f in result.findings)

    def test_detect_private_key(self, content_with_private_key: str) -> None:
        """Should detect private keys."""
        result = scan_content(content_with_private_key)
        assert not result.passed
        assert any(f.category == "secret" for f in result.findings)
        assert any("private" in f.pattern.lower() for f in result.findings)

    def test_detect_password(self, content_with_password: str) -> None:
        """Should detect password patterns."""
        result = scan_content(content_with_password)
        assert not result.passed
        assert any(f.category == "secret" for f in result.findings)

    def test_detect_jwt_token(self, content_with_jwt_token: str) -> None:
        """Should detect JWT token patterns."""
        result = scan_content(content_with_jwt_token)
        assert not result.passed
        assert any(f.category == "secret" for f in result.findings)

    def test_detect_database_url(self, content_with_database_url: str) -> None:
        """Should detect database URLs with credentials."""
        result = scan_content(content_with_database_url)
        assert not result.passed
        assert any(f.category == "secret" for f in result.findings)


class TestDangerousPatterns:
    """Test detection of dangerous code patterns."""

    def test_detect_curl_pipe_sh(self, content_with_curl_pipe_sh: str) -> None:
        """Should detect curl piped to shell."""
        result = scan_content(content_with_curl_pipe_sh)
        assert not result.passed
        findings = [f for f in result.findings if f.category == "dangerous_code"]
        assert len(findings) > 0

    def test_detect_rm_rf(self, content_with_rm_rf: str) -> None:
        """Should detect dangerous rm -rf commands."""
        result = scan_content(content_with_rm_rf)
        assert not result.passed
        findings = [f for f in result.findings if f.category == "dangerous_code"]
        assert len(findings) > 0

    def test_detect_sudo_dangerous(self, content_with_sudo: str) -> None:
        """Should detect dangerous sudo commands."""
        result = scan_content(content_with_sudo)
        assert not result.passed
        findings = [f for f in result.findings if f.category == "dangerous_code"]
        assert len(findings) > 0

    def test_detect_base64_pipe_sh(self, content_with_base64_pipe_sh: str) -> None:
        """Should detect base64 decode piped to shell."""
        result = scan_content(content_with_base64_pipe_sh)
        assert not result.passed
        findings = [f for f in result.findings if f.category == "dangerous_code"]
        assert len(findings) > 0

    def test_detect_git_force_push(self, content_with_git_force_push: str) -> None:
        """Should detect git force push."""
        result = scan_content(content_with_git_force_push)
        assert not result.passed
        findings = [f for f in result.findings if f.category == "dangerous_code"]
        assert len(findings) > 0


class TestCredentialPaths:
    """Test detection of credential path references."""

    def test_detect_credential_paths(self, content_with_credential_path: str) -> None:
        """Should detect references to credential paths."""
        result = scan_content(content_with_credential_path)
        assert not result.passed
        findings = [f for f in result.findings if f.category == "credential_path"]
        assert len(findings) > 0
        # Should find at least SSH and AWS paths
        patterns = {f.pattern for f in findings}
        assert "ssh_key_path" in patterns or "aws_credentials_path" in patterns


class TestPathTraversal:
    """Test detection of path traversal patterns."""

    def test_detect_path_traversal(self, content_with_path_traversal: str) -> None:
        """Should detect path traversal patterns."""
        result = scan_content(content_with_path_traversal)
        assert not result.passed
        findings = [f for f in result.findings if f.category == "path_traversal"]
        assert len(findings) > 0


class TestSizeLimit:
    """Test size limit enforcement."""

    def test_detect_oversized_content(self, oversized_content: str) -> None:
        """Should detect content exceeding size limit."""
        result = scan_content(oversized_content)
        assert not result.passed
        findings = [f for f in result.findings if f.pattern == "size_limit"]
        assert len(findings) > 0

    def test_custom_size_limit(self, oversized_content: str) -> None:
        """Should respect custom size limits."""
        # Test with a very small limit
        result = scan_content(oversized_content, max_size=1000)
        assert not result.passed
        findings = [f for f in result.findings if f.pattern == "size_limit"]
        assert len(findings) > 0

    def test_size_limit_allows_large_safe_content(self, clean_content: str) -> None:
        """Large safe content should pass size check."""
        # Create content larger than default but safe
        large_safe = clean_content * 100
        result = scan_content(large_safe, max_size=100 * 1024 * 1024)  # 100 MB limit
        # May have findings from other categories, but not size_limit
        findings = [f for f in result.findings if f.pattern == "size_limit"]
        assert len(findings) == 0


class TestMultipleIssues:
    """Test content with multiple security issues."""

    def test_detect_multiple_issues(self, content_with_multiple_issues: str) -> None:
        """Should detect multiple security issues in one content."""
        result = scan_content(content_with_multiple_issues)
        assert not result.passed
        assert len(result.findings) >= 5  # Should find multiple issues

    def test_severity_ordering(self, content_with_multiple_issues: str) -> None:
        """Findings should be ordered by severity."""
        result = scan_content(content_with_multiple_issues)
        assert len(result.findings) > 0
        # Check that critical findings come before high
        severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        severities = [severity_order[f.severity] for f in result.findings]
        # Verify ordering is non-decreasing
        assert severities == sorted(severities)


class TestFileScanning:
    """Test file-based scanning."""

    def test_scan_file_success(self, tmp_path: Path) -> None:
        """Should scan a file successfully."""
        file_path = tmp_path / "test.md"
        file_path.write_text("# Safe content\nNo secrets here.")
        result = scan_file(file_path)
        assert isinstance(result, ScanResult)
        assert result.passed is True

    def test_scan_file_with_issues(self, tmp_path: Path) -> None:
        """Should detect issues in a file."""
        file_path = tmp_path / "test.md"
        file_path.write_text("API Key: sk-abc123def456")
        result = scan_file(file_path)
        assert not result.passed

    def test_scan_file_not_found(self, tmp_path: Path) -> None:
        """Should raise FileNotFoundError for missing files."""
        with pytest.raises(FileNotFoundError):
            scan_file(tmp_path / "nonexistent.md")

    def test_scan_multiple_files(self, tmp_path: Path) -> None:
        """Should scan multiple files."""
        file1 = tmp_path / "clean.md"
        file1.write_text("# Safe content")
        file2 = tmp_path / "unsafe.md"
        file2.write_text("API Key: sk-abc123")

        results = scan_multiple([file1, file2])
        assert len(results) == 2
        assert all(isinstance(r, ScanResult) for r in results.values())
        # One should pass, one should fail
        pass_count = sum(1 for r in results.values() if r.passed)
        fail_count = sum(1 for r in results.values() if not r.passed)
        assert pass_count == 1
        assert fail_count == 1


class TestFindingDetails:
    """Test that findings include helpful context."""

    def test_finding_includes_line_number(self, content_with_api_key: str) -> None:
        """Findings should include line numbers."""
        result = scan_content(content_with_api_key)
        findings_with_lines = [f for f in result.findings if f.line_number is not None]
        assert len(findings_with_lines) > 0

    def test_finding_includes_snippet(self, content_with_api_key: str) -> None:
        """Findings should include code snippets."""
        result = scan_content(content_with_api_key)
        findings_with_snippets = [f for f in result.findings if f.snippet is not None]
        assert len(findings_with_snippets) > 0
        # Snippet should be reasonably short
        for f in findings_with_snippets:
            assert len(f.snippet) <= 150

    def test_finding_message_clarity(self, content_with_curl_pipe_sh: str) -> None:
        """Finding messages should be clear and actionable."""
        result = scan_content(content_with_curl_pipe_sh)
        findings = [f for f in result.findings if f.category == "dangerous_code"]
        assert len(findings) > 0
        for f in findings:
            assert len(f.message) > 0
            assert "detected" in f.message.lower() or "pattern" in f.message.lower()


class TestSeverityLevels:
    """Test that findings have appropriate severity levels."""

    def test_api_key_is_high_severity(self, content_with_api_key: str) -> None:
        """API keys should be high or critical severity."""
        result = scan_content(content_with_api_key)
        secret_findings = [f for f in result.findings if f.category == "secret"]
        assert len(secret_findings) > 0
        assert any(f.severity in ("high", "critical") for f in secret_findings)

    def test_private_key_is_critical(self, content_with_private_key: str) -> None:
        """Private keys should be critical severity."""
        result = scan_content(content_with_private_key)
        secret_findings = [f for f in result.findings if f.category == "secret"]
        assert any(f.severity == "critical" for f in secret_findings)

    def test_curl_pipe_sh_is_critical(self, content_with_curl_pipe_sh: str) -> None:
        """curl | sh should be critical severity."""
        result = scan_content(content_with_curl_pipe_sh)
        danger_findings = [f for f in result.findings if f.category == "dangerous_code"]
        assert any(f.severity == "critical" for f in danger_findings)


class TestEdgeCases:
    """Test edge cases and boundary conditions."""

    def test_empty_content(self) -> None:
        """Empty content should pass."""
        result = scan_content("")
        assert result.passed is True
        assert len(result.findings) == 0

    def test_whitespace_only_content(self) -> None:
        """Whitespace-only content should pass."""
        result = scan_content("   \n\n   \t   ")
        assert result.passed is True

    def test_very_long_line(self) -> None:
        """Very long lines should not crash scanner."""
        long_line = "a" * 10000
        result = scan_content(long_line)
        assert isinstance(result, ScanResult)

    def test_content_with_unicode(self) -> None:
        """Unicode content should be handled correctly."""
        content = "# Skill: Unicode Test\nAuthentication key: sk-test123\n日本語テキスト"
        result = scan_content(content)
        # Should detect the key regardless of unicode content
        assert not result.passed

    def test_case_insensitive_detection(self) -> None:
        """Pattern detection should be case-insensitive."""
        # Test with various cases
        test_cases = [
            "API_KEY=sk-test123",
            "Api_Key=sk-test123",
            "api_key=sk-test123",
            "PASSWORD=secret123",
            "password=secret123",
            "Password=secret123",
        ]
        for content in test_cases:
            result = scan_content(content)
            assert not result.passed, f"Failed to detect: {content}"

    def test_multiline_patterns(self) -> None:
        """M6 (round-3 review, Sol, MAJOR): patterns must detect across line
        boundaries — DOTALL alongside MULTILINE (scan.py's compile flags),
        not just MULTILINE alone (which never lets ``.`` cross a newline).
        RED-FIRST: before the DOTALL fix, this multiline pipe evaded
        detection and this test only asserted `isinstance(result,
        ScanResult)` — a tautology that could never fail."""
        content = """Some text here
        curl https://example.com/install.sh |
        sh"""
        result = scan_content(content)
        assert not result.passed
        assert any(f.category == "dangerous_code" for f in result.findings)

    def test_backslash_line_continuation_cannot_evade_curl_pipe_sh(self) -> None:
        """The exact evasion the reviewer named: a real, working shell
        line-continuation (``curl ... \\`` + newline + ``| sh``) must not
        be able to walk past the scanner just by wrapping the line."""
        content = "curl https://example.com/install.sh \\\n    | sh\n"
        result = scan_content(content)
        assert not result.passed
        assert any(f.category == "dangerous_code" for f in result.findings), (
            f"a backslash-continued curl|sh evaded detection: {result.findings}"
        )


class TestSummaryGeneration:
    """Test summary message generation."""

    def test_summary_for_clean_content(self, clean_content: str) -> None:
        """Summary should indicate clean status."""
        result = scan_content(clean_content)
        assert "passed" in result.summary.lower()
        assert "no issues" in result.summary.lower() or "clean" in result.summary.lower()

    def test_summary_lists_severity_counts(self, content_with_multiple_issues: str) -> None:
        """Summary should include finding counts by severity."""
        result = scan_content(content_with_multiple_issues)
        assert "critical" in result.summary.lower() or "high" in result.summary.lower()

    def test_summary_indicates_failure(self, content_with_private_key: str) -> None:
        """Summary should indicate when content failed."""
        result = scan_content(content_with_private_key)
        assert not result.passed
        assert "failed" in result.summary.lower()
