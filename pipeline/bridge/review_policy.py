"""Deterministic risk-tiered build-review policy shared by every lander path."""

from __future__ import annotations

import re

KNOWN_LINEAGES = {"anthropic", "openai", "google", "xai", "moonshot", "human"}
_APPROVAL_VERDICT_RE = re.compile(
    r"^(?:approve(?:$|\s*[—–-]\s*(?:with[- ]nits\b|zero blockers\b))|"
    r"green\b|pass\b|blockers\s*=\s*0\b|"
    r"zero blockers\b)",
    re.IGNORECASE,
)

_RISKY_EXACT = {
    ".mcp.json",
    "configs/accounts.yaml",
    "configs/mcp-approved.yaml",
    "configs/northstar-cert/manifest.yaml",
    "configs/northstar-cert/writers.yaml",
    "omniagentos/api/routes/sessions.py",
    "omniagentos/scheduler/gate_evidence.py",
    # The North Star instrument grades ITSELF: the recorder decides which checks
    # are masked and what a verdict is, emit_gaps decides which reds become
    # queue work, and the registry migration can rewrite every sticky
    # obligation in one pass. Protecting only the two config files it reads
    # left the code that interprets them unreviewed.
    "scripts/northstar_cert/emit_gaps.py",
    "scripts/northstar_cert/migrate_writers_v3.py",
    "scripts/northstar_cert/record_results.py",
    "pipeline/bridge/gate_host.py",
    "pipeline/bridge/gate_loop.py",
    "pipeline/bridge/integration.py",
    "pipeline/bridge/review_policy.py",
    "pipeline/bridge/train_assembler.py",
    "scripts/merge-gate.sh",
}
_RISKY_PREFIXES = (
    ".claude/",
    ".github/workflows/",
    "configs/security/",
    "contracts/",
    "omniagentos/db/migrations/",
    "omniagentos/policy/",
    "pipeline/launchd/",
    "pipeline/prompts/",
    "pipeline/schema/",
    "schema/",
    "system-prompts/",
)
_RISKY_PATH_WORDS = {
    "approval",
    "approvals",
    "auth",
    "authentication",
    "authorization",
    "billing",
    "charge",
    "charges",
    "credential",
    "credentials",
    "gate",
    "migration",
    "migrations",
    "money",
    "oauth",
    "payment",
    "payments",
    "paypal",
    "permission",
    "permissions",
    "policy",
    "refund",
    "refunds",
    "secret",
    "secrets",
    "stripe",
}


def lineage(value: object) -> str | None:
    """Normalize a model provider/lab name for exact lineage comparison."""
    if not isinstance(value, str):
        return None
    return value.strip().casefold() or None


def risky_review_paths(paths: set[str]) -> list[str]:
    """Return real-diff paths that require cross-lineage build review."""
    hits: list[str] = []
    for original in sorted(paths):
        path = original.replace("\\", "/").casefold()
        while path.startswith("./"):
            path = path[2:]
        words = {word for word in re.split(r"[/_.-]+", path) if word}
        if path in _RISKY_EXACT or path.startswith(_RISKY_PREFIXES) or words & _RISKY_PATH_WORDS:
            hits.append(original)
    return hits


def approved_cross_lineage(art: dict, expected_sha: str) -> bool:
    """Require named final approval from a known different lineage at exact SHA."""
    producer = art.get("producer")
    producer = producer if isinstance(producer, dict) else {}
    producer_lineage = lineage(producer.get("lineage"))
    if producer_lineage not in KNOWN_LINEAGES:
        return False
    verdicts = art.get("verdicts")
    if not isinstance(verdicts, list):
        return False
    for verdict in verdicts:
        if not isinstance(verdict, dict):
            continue
        reviewer_lineage = lineage(verdict.get("lineage"))
        reviewer = verdict.get("model") or verdict.get("reviewer")
        decision = verdict.get("verdict")
        decision = decision.strip() if isinstance(decision, str) else ""
        reviewed_sha = verdict.get("reviewed_sha")
        if (
            reviewer_lineage in KNOWN_LINEAGES
            and reviewer_lineage != producer_lineage
            and isinstance(reviewer, str)
            and reviewer.strip()
            and isinstance(reviewed_sha, str)
            and reviewed_sha.casefold() == expected_sha.casefold()
            and _APPROVAL_VERDICT_RE.search(decision)
        ):
            return True
    return False
