"""Deterministic supply-chain content scanner for skill vault notes.

Scans skill content for secrets, dangerous patterns, path traversal, and oversized
bodies. No LLM involvement — pure pattern matching with structured findings.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

LOG = logging.getLogger(__name__)

# Secret patterns: detect API keys, tokens, private keys, passwords
_SECRET_PATTERNS = [
    # API keys and tokens (common formats)
    (r'(?i)(?:api[_-]?key|apikey|sk-[a-z0-9]+|pk-[a-z0-9]+)', "potential_api_key"),
    (r'(?i)(token|auth[_-]?token|access[_-]?token)[\s:=]+["\']?[a-z0-9]{20,}', "potential_token"),
    (r'(?i)bearer\s+[a-z0-9\-_.]+', "potential_bearer_token"),
    (r'(?i)jwt[\s:=]+[a-z0-9\-_.]+', "potential_jwt"),
    # Private keys
    (r'BEGIN\s+(RSA\s+)?PRIVATE\s+KEY', "private_key_start"),
    (r'BEGIN\s+EC\s+PRIVATE\s+KEY', "private_key_start"),
    (r'BEGIN\s+OPENSSH\s+PRIVATE\s+KEY', "private_key_start"),
    (r'BEGIN\s+PGP\s+PRIVATE\s+KEY', "private_key_start"),
    # Database and service credentials
    (r'(?i)password\s*[:=]\s*["\']?[^\s\n"\']+["\']?', "potential_password"),
    (r'(?i)(mongodb|mysql|postgres|psql)://[^\s\n]+', "database_url"),
    (r'(?i)connection[_-]?string\s*[:=]', "connection_string"),
    # AWS, GCP, Azure patterns
    (r'(?i)aws[_-]?key\s*[:=]\s*[A-Z0-9]{20}', "aws_access_key"),
    (r'AKIA[0-9A-Z]{16}', "aws_access_key_id"),
    (r'(?i)gcp[_-]?key|google[_-]?cloud[_-]?key', "gcp_key"),
    (r'(?i)azure[_-]?key|azure[_-]?secret', "azure_credential"),
]

# Dangerous execution patterns: shell commands that execute untrusted input
_DANGER_PATTERNS = [
    # Shell piping of untrusted sources
    (r'curl\s+.*?\|\s*sh(?:\s|$|[;`&])', "curl_pipe_sh"),
    (r'wget\s+.*?\|\s*sh(?:\s|$|[;`&])', "wget_pipe_sh"),
    (r'curl\s+.*?\|\s*bash(?:\s|$|[;`&])', "curl_pipe_bash"),
    (r'wget\s+.*?\|\s*bash(?:\s|$|[;`&])', "wget_pipe_bash"),
    # Base64 decode piped to shell
    (r'base64\s+-d\s*\|\s*(?:sh|bash)(?:\s|$|[;`&])', "base64_decode_pipe_sh"),
    (r'echo\s+.*?base64.*?\|\s*(?:sh|bash)(?:\s|$|[;`&])', "base64_decode_pipe_sh"),
    # Dangerous deletion commands
    (r'rm\s+-rf\s+/', "rm_rf_root_deletion"),
    (r'rm\s+-rf\s*~', "rm_rf_home_deletion"),
    (r'rm\s+-rf\s+\*', "rm_rf_wildcard_deletion"),
    # Privilege escalation
    (r'(?:^|\s)sudo\s+(?:rm|dd|reboot|shutdown)', "sudo_dangerous_command"),
    (r'(?:^|\s)sudo\s+-[isuH]', "sudo_privilege_escalation"),
    # Git dangerous operations
    (r'git\s+push\s+--force', "git_force_push"),
    (r'git\s+reset\s+--hard', "git_hard_reset"),
    # dd command abuse
    (r'dd\s+of=/dev/\w+', "dd_device_overwrite"),
]

# Credential path patterns: sensitive file paths that shouldn't be in content
_CREDENTIAL_PATHS = [
    (r'~?/?\.ssh', "ssh_key_path"),
    (r'~?/?\.aws', "aws_credentials_path"),
    (r'~?/?\.azure', "azure_credentials_path"),
    (r'~?/?\.gcp', "gcp_credentials_path"),
    (r'~?/?\.config', "config_path"),
    (r'~?/?\.kube/config', "kubernetes_config"),
    (r'/etc/shadow', "system_shadow_file"),
    (r'/etc/passwd', "system_password_file"),
]

# Path traversal patterns
_PATH_TRAVERSAL_PATTERNS = [
    (r'\.\./.*?[/\\]+(etc|proc|sys|dev|etc)', "path_traversal_system"),
    (r'\.\./.*?password', "path_traversal_sensitive_file"),
    (r'\.\./.*?key', "path_traversal_sensitive_file"),
]

# Size limits
_MAX_BODY_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB

SeverityLevel = Literal["low", "medium", "high", "critical"]


@dataclass(frozen=True)
class Finding:
    """A single security/content finding with severity and context."""

    severity: SeverityLevel
    category: str
    pattern: str
    message: str
    line_number: int | None = None
    snippet: str | None = None


@dataclass(frozen=True)
class ScanResult:
    """Result of scanning a skill content body.

    Attributes:
        findings: List of security/content findings, sorted by severity (critical first).
        passed: True if no high/critical findings exist.
        summary: Human-readable summary of results.
    """

    findings: list[Finding]
    passed: bool
    summary: str


def _compile_patterns(patterns: list[tuple[str, str]]) -> list[tuple[re.Pattern, str]]:
    """Pre-compile regex patterns for efficiency."""
    compiled = []
    for pattern_str, category in patterns:
        try:
            # DOTALL alongside MULTILINE (M6, round-3 review, Sol): several
            # patterns above use `.` to span "the rest of this shell
            # command" (curl ... | sh, echo ... base64 ... | sh, path
            # traversal). Under MULTILINE alone, `.` still never crosses a
            # newline, so `curl foo \` + a line-continuation newline + `|
            # sh` — ordinary, working shell syntax — evaded every one of
            # them. The `.*?` in each is non-greedy, so DOTALL does not risk
            # runaway over-matching; it only lets the match reach across the
            # line break these patterns were always meant to span.
            compiled.append(
                (re.compile(pattern_str, re.MULTILINE | re.IGNORECASE | re.DOTALL), category)
            )
        except re.error as e:
            LOG.warning(f"Failed to compile pattern {category}: {e}")
    return compiled


_COMPILED_SECRETS = _compile_patterns(_SECRET_PATTERNS)
_COMPILED_DANGERS = _compile_patterns(_DANGER_PATTERNS)
_COMPILED_PATHS = _compile_patterns(_CREDENTIAL_PATHS)
_COMPILED_TRAVERSALS = _compile_patterns(_PATH_TRAVERSAL_PATTERNS)


def _severity_for_category(category: str) -> SeverityLevel:
    """Map finding category to severity level."""
    # Critical: secrets and major code execution risks
    if category in {
        "private_key_start",
        "aws_access_key_id",
        "curl_pipe_sh",
        "curl_pipe_bash",
        "wget_pipe_sh",
        "wget_pipe_bash",
        "base64_decode_pipe_sh",
        "rm_rf_root_deletion",
        "sudo_privilege_escalation",
        "database_url",
    }:
        return "critical"
    # High: other secrets and dangerous patterns
    if category in {
        "potential_api_key",
        "potential_token",
        "potential_bearer_token",
        "potential_jwt",
        "potential_password",
        "aws_access_key",
        "gcp_key",
        "azure_credential",
        "rm_rf_home_deletion",
        "sudo_dangerous_command",
        "path_traversal_system",
        "dd_device_overwrite",
        "git_force_push",
        "git_hard_reset",
        "ssh_key_path",
        "aws_credentials_path",
        "azure_credentials_path",
        "gcp_credentials_path",
        "kubernetes_config",
    }:
        return "high"
    # Medium: risky patterns
    if category in {
        "rm_rf_wildcard_deletion",
        "path_traversal_sensitive_file",
    }:
        return "medium"
    # Low: connection strings, generic paths
    if category in {
        "connection_string",
        "config_path",
        "system_password_file",
        "system_shadow_file",
    }:
        return "low"
    return "low"
    return "low"


def _get_snippet(content: str, match_obj: re.Match, context_chars: int = 60) -> str:
    """Extract a snippet around a match for human readability."""
    start = max(0, match_obj.start() - context_chars)
    end = min(len(content), match_obj.end() + context_chars)
    snippet = content[start:end]
    # Truncate and clean up
    if start > 0:
        snippet = "..." + snippet
    if end < len(content):
        snippet = snippet + "..."
    # Replace newlines for display
    snippet = snippet.replace("\n", " ").replace("\r", " ")
    return snippet[:120]  # Max 120 chars


def _get_line_number(content: str, position: int) -> int:
    """Get line number from character position."""
    return content[:position].count("\n") + 1


def scan_content(content: str, *, max_size: int | None = None) -> ScanResult:
    """Scan skill content for security issues and dangerous patterns.

    Args:
        content: Skill body content to scan.
        max_size: Maximum allowed size in bytes. Defaults to 10MB.

    Returns:
        ScanResult with findings, pass/fail status, and summary.
    """
    max_size = max_size or _MAX_BODY_SIZE_BYTES
    findings: list[Finding] = []

    # Check body size
    if len(content.encode("utf-8")) > max_size:
        size_mb = len(content.encode("utf-8")) / (1024 * 1024)
        findings.append(
            Finding(
                severity="high",
                category="oversized_body",
                pattern="size_limit",
                message=f"Content exceeds size limit ({size_mb:.2f}MB > {max_size / (1024*1024):.2f}MB)",
            )
        )

    # Scan for secrets
    for pattern, category in _COMPILED_SECRETS:
        for match in pattern.finditer(content):
            severity = _severity_for_category(category)
            findings.append(
                Finding(
                    severity=severity,
                    category="secret",
                    pattern=category,
                    message=f"Potential secret detected: {category}",
                    line_number=_get_line_number(content, match.start()),
                    snippet=_get_snippet(content, match),
                )
            )

    # Scan for dangerous patterns
    for pattern, category in _COMPILED_DANGERS:
        for match in pattern.finditer(content):
            severity = _severity_for_category(category)
            findings.append(
                Finding(
                    severity=severity,
                    category="dangerous_code",
                    pattern=category,
                    message=f"Dangerous pattern detected: {category}",
                    line_number=_get_line_number(content, match.start()),
                    snippet=_get_snippet(content, match),
                )
            )

    # Scan for credential paths
    for pattern, category in _COMPILED_PATHS:
        for match in pattern.finditer(content):
            severity = _severity_for_category(category)
            findings.append(
                Finding(
                    severity=severity,
                    category="credential_path",
                    pattern=category,
                    message=f"Sensitive path reference: {category}",
                    line_number=_get_line_number(content, match.start()),
                    snippet=_get_snippet(content, match),
                )
            )

    # Scan for path traversal
    for pattern, category in _COMPILED_TRAVERSALS:
        for match in pattern.finditer(content):
            severity = _severity_for_category(category)
            findings.append(
                Finding(
                    severity=severity,
                    category="path_traversal",
                    pattern=category,
                    message=f"Path traversal pattern detected: {category}",
                    line_number=_get_line_number(content, match.start()),
                    snippet=_get_snippet(content, match),
                )
            )

    # Sort by severity (critical first, then high, medium, low)
    severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    findings.sort(key=lambda f: (severity_order.get(f.severity, 4), f.line_number or 0))

    # Determine pass/fail: fail if any critical or high findings exist
    passed = not any(f.severity in ("critical", "high") for f in findings)

    # Build summary
    if not findings:
        summary = "Content scan passed with no issues detected."
    else:
        counts_by_severity: dict[str, int] = {}
        for f in findings:
            counts_by_severity[f.severity] = counts_by_severity.get(f.severity, 0) + 1
        summary_parts = [
            f"{count} {severity} finding{'s' if count > 1 else ''}"
            for severity, count in sorted(counts_by_severity.items())
        ]
        status = "FAILED" if not passed else "PASSED WITH WARNINGS"
        summary = f"Content scan {status}: {', '.join(summary_parts)}"

    return ScanResult(findings=findings, passed=passed, summary=summary)


def scan_file(path: Path, *, max_size: int | None = None) -> ScanResult:
    """Scan a skill content file for security issues.

    Args:
        path: Path to the skill file to scan.
        max_size: Maximum allowed size in bytes.

    Returns:
        ScanResult with findings, pass/fail status, and summary.

    Raises:
        FileNotFoundError: If the file does not exist.
    """
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")
    content = path.read_text(encoding="utf-8")
    return scan_content(content, max_size=max_size)


def scan_multiple(paths: list[Path], *, max_size: int | None = None) -> dict[str, ScanResult]:
    """Scan multiple files and return a mapping of paths to results.

    Args:
        paths: List of paths to scan.
        max_size: Maximum allowed size in bytes.

    Returns:
        Dictionary mapping file paths (as strings) to ScanResult objects.
    """
    results = {}
    for path in paths:
        try:
            results[str(path)] = scan_file(path, max_size=max_size)
        except Exception as e:
            # Record scan error as a critical finding
            results[str(path)] = ScanResult(
                findings=[
                    Finding(
                        severity="critical",
                        category="scan_error",
                        pattern="file_read_error",
                        message=f"Failed to scan file: {e}",
                    )
                ],
                passed=False,
                summary=f"Scan error: {e}",
            )
    return results


if __name__ == "__main__":
    import json
    import sys

    if len(sys.argv) < 2:
        print("Usage: python -m omniagentos.skills.scan <file_or_dir> [--json]")
        sys.exit(1)

    path = Path(sys.argv[1])
    as_json = "--json" in sys.argv

    if path.is_file():
        result = scan_file(path)
    elif path.is_dir():
        files = list(path.glob("**/*.md"))
        results = scan_multiple(files)
        # Aggregate results
        all_findings = []
        all_passed = True
        for r in results.values():
            all_findings.extend(r.findings)
            all_passed = all_passed and r.passed

        if as_json:
            output = {
                "passed": all_passed,
                "files_scanned": len(results),
                "findings": [
                    {
                        "severity": f.severity,
                        "category": f.category,
                        "pattern": f.pattern,
                        "message": f.message,
                        "line_number": f.line_number,
                        "snippet": f.snippet,
                    }
                    for f in all_findings
                ],
            }
            print(json.dumps(output, indent=2))
        else:
            print(f"Scanned {len(results)} files")
            print(f"Status: {'PASSED' if all_passed else 'FAILED'}")
            for severity in ("critical", "high", "medium", "low"):
                count = sum(1 for f in all_findings if f.severity == severity)
                if count > 0:
                    print(f"  {severity}: {count}")
        sys.exit(0 if all_passed else 1)
    else:
        print(f"Path not found: {path}")
        sys.exit(1)

    if as_json:
        output = {
            "passed": result.passed,
            "summary": result.summary,
            "findings": [
                {
                    "severity": f.severity,
                    "category": f.category,
                    "pattern": f.pattern,
                    "message": f.message,
                    "line_number": f.line_number,
                    "snippet": f.snippet,
                }
                for f in result.findings
            ],
        }
        print(json.dumps(output, indent=2))
    else:
        print(result.summary)
        if result.findings:
            print("\nFindings:")
            for f in result.findings:
                print(f"  [{f.severity.upper()}] {f.pattern} (line {f.line_number}): {f.message}")
                if f.snippet:
                    print(f"    {f.snippet}")

    sys.exit(0 if result.passed else 1)
