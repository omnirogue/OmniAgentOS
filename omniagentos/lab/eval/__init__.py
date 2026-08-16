"""H2 protected evaluator (contracts/lab-interfaces.md §L02-eval; COMPETE).

Held-out expected values live ONLY inside ``ProtectedGrader``'s separate
SQLite file — see ``omniagentos.lab.eval.paths`` for the structural isolation
story (blueprint Section 11.7; design review HD-001/HD-ALT/HD-009/HD-015).
"""

from __future__ import annotations

from omniagentos.lab.eval.blind import BlindPair, build_blind_pairs, unlinkable_token
from omniagentos.lab.eval.evaluator import (
    ARMS,
    JudgeFn,
    JudgeView,
    ProtectedEvaluator,
    offline_heuristic_judge,
)
from omniagentos.lab.eval.grader import MissingExpectedError, ProtectedGrader
from omniagentos.lab.eval.paths import (
    PROTECTED_DB_ENV,
    SENSITIVE_ENV_VARS,
    assert_env_scrubbed,
    default_protected_db_path,
    scrubbed_env,
)
from omniagentos.lab.eval.scoring import aggregate_case_scores, score_case

__all__ = [
    "ARMS",
    "PROTECTED_DB_ENV",
    "SENSITIVE_ENV_VARS",
    "BlindPair",
    "JudgeFn",
    "JudgeView",
    "MissingExpectedError",
    "ProtectedEvaluator",
    "ProtectedGrader",
    "aggregate_case_scores",
    "assert_env_scrubbed",
    "build_blind_pairs",
    "default_protected_db_path",
    "offline_heuristic_judge",
    "score_case",
    "scrubbed_env",
    "unlinkable_token",
]
