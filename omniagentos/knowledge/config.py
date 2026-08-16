"""Knowledge subsystem configuration — typed accessors over environment variables and secrets."""

from __future__ import annotations

import logging
import math
import os
from pathlib import Path

from omniagentos.knowledge.contracts import (
    DEFAULT_BUDGET_TOKENS,
    DEFAULT_DSN,
    DEFAULT_TEST_DSN,
    EMBED_DIM,
    ENV_ADMIN_DSN,
    ENV_BUDGET_TOKENS,
    ENV_DSN,
    ENV_EMBED,
    ENV_ENABLED,
    ENV_EXTRACT_MODEL,
    ENV_MIGRATE_DSN,
    ENV_RECALL_MODE,
    ENV_REQUIRE_PG,
    ENV_TEST_ADMIN_DSN,
    ENV_TEST_DSN,
)

logger = logging.getLogger(__name__)

# Rank-1 RRF (k=60), 1.5x multi-signal/usefulness bonuses, and maximum fact
# modulation bound a plausible recalled score to roughly 0.07 (see B5 repro).
RECALL_SCORE_FLOOR_PLAUSIBLE_MAX = 0.07


def _load_secrets() -> dict[str, str]:
    """Load var/secrets/knowledge-pg.env if present (dotenv-style)."""
    secrets_path = Path("var/secrets/knowledge-pg.env")
    if not secrets_path.exists():
        return {}

    result = {}
    try:
        with open(secrets_path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, value = line.split("=", 1)
                    # Remove quotes if present
                    if value.startswith('"') and value.endswith('"'):
                        value = value[1:-1]
                    elif value.startswith("'") and value.endswith("'"):
                        value = value[1:-1]
                    result[key.strip()] = value
    except Exception:
        pass
    return result


_SECRETS = _load_secrets()


def knowledge_enabled() -> bool:
    """Whether knowledge subsystem is enabled."""
    return os.getenv(ENV_ENABLED, "0") == "1"


def dsn() -> str:
    """Agent-role DSN for knowledge store."""
    # Check secrets first (post-flip), then environment, then default
    return _SECRETS.get(ENV_DSN) or os.getenv(ENV_DSN) or DEFAULT_DSN


def admin_dsn() -> str:
    """Admin-role DSN for consolidator/operator."""
    return _SECRETS.get(ENV_ADMIN_DSN) or os.getenv(ENV_ADMIN_DSN) or ""


def migrate_dsn() -> str:
    """Superuser/owner DSN for running DDL."""
    return _SECRETS.get(ENV_MIGRATE_DSN) or os.getenv(ENV_MIGRATE_DSN) or ""


def test_dsn() -> str:
    """Test database DSN (agent-role)."""
    return os.getenv(ENV_TEST_DSN, DEFAULT_TEST_DSN)


def test_admin_dsn() -> str:
    """Test database admin-role DSN."""
    return os.getenv(ENV_TEST_ADMIN_DSN) or ""


def embed_spec() -> str:
    """Embedding provider spec: 'ollama:bge-m3' (default) or 'fake' for tests."""
    return os.getenv(ENV_EMBED, "ollama:bge-m3")


def budget_tokens() -> int:
    """Token budget for recall rendering."""
    try:
        return int(os.getenv(ENV_BUDGET_TOKENS, str(DEFAULT_BUDGET_TOKENS)))
    except (ValueError, TypeError):
        return DEFAULT_BUDGET_TOKENS


def recall_mode() -> str:
    """Recall mode: 'full' (default, with graph+Hebbian) or 'lean' (vector+FTS+RRF only)."""
    mode = os.getenv(ENV_RECALL_MODE, "full").lower()
    return mode if mode in ("full", "lean") else "full"


def recall_score_floor() -> float:
    """Optional absolute modulated-score gate for runner recall injection."""
    try:
        floor = float(os.getenv("OMNIAGENTOS_KNOWLEDGE_RECALL_SCORE_FLOOR", "0"))
    except (ValueError, TypeError):
        return 0.0
    if not math.isfinite(floor) or floor < 0:
        return 0.0
    if floor > RECALL_SCORE_FLOOR_PLAUSIBLE_MAX:
        logger.warning(
            "Ignoring OMNIAGENTOS_KNOWLEDGE_RECALL_SCORE_FLOOR=%s above plausible maximum %s",
            floor,
            RECALL_SCORE_FLOOR_PLAUSIBLE_MAX,
        )
        return 0.0
    return floor


def recall_floor_fraction() -> float:
    """Relative-to-top-score injection gate for runner recall."""
    default = 0.15
    try:
        fraction = float(os.getenv("OMNIAGENTOS_KNOWLEDGE_RECALL_FLOOR_FRACTION", str(default)))
    except (ValueError, TypeError):
        return default
    if not math.isfinite(fraction) or fraction < 0:
        return default
    return min(fraction, math.nextafter(1.0, 0.0))


def extract_model() -> str:
    """LLM model for extraction and offline contradiction judgement."""
    return os.getenv(ENV_EXTRACT_MODEL, "haiku")


def require_pg() -> bool:
    """Whether PG tests must pass (vs. skip if DB unreachable)."""
    return os.getenv(ENV_REQUIRE_PG, "0") == "1"


def embed_dim() -> int:
    """Embedding dimension — must match kb_meta and provider (1024 only)."""
    return EMBED_DIM
