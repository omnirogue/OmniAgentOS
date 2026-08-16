#!/usr/bin/env python3
"""CLI: evaluate a coverage.json against configs/coverage_policy.yaml (M-10/M-22).

Usage:
  python scripts/coverage/check_coverage_policy.py path/to/coverage.json
  python scripts/coverage/check_coverage_policy.py --demo-subset

Exit codes:
  0  — whole-package report OK, OR --demo-subset correctly rejected as subset
  1  — real coverage report failed policy evaluation
  2  — demo-subset self-check failed (unexpected pass / wrong rejection shape)
  3  — missing config, unreadable report, or crash-class setup error

--demo-subset uses a structured assertion (not a shell ``test $? -eq 1`` trick)
so a crash or missing config cannot masquerade as "policy rejected subset".
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from omniagentos.testpolicy.coverage_policy import (  # noqa: E402
    CoverageEvaluation,
    CoveragePolicy,
    evaluate_coverage_report,
    load_coverage_policy,
    summary_from_coverage_json,
    summary_from_module_list,
)

# Structured demo outcome labels (stable for Make/CI grepping).
DEMO_EXPECTED = "policy_rejected_subset_as_expected"
DEMO_UNEXPECTED_PASS = "unexpected_policy_pass"
DEMO_WRONG_SHAPE = "unexpected_rejection_shape"
DEMO_CONFIG_ERROR = "config_or_setup_error"


def _demo_subset_assertion(policy: CoveragePolicy) -> tuple[dict[str, Any], int]:
    """Prove the audited 17-module subset is rejected for the right reasons (M-10).

    Distinguishes:
    * expected policy rejection (exit 0)
    * unexpected accept (exit 2)
    * rejection for the wrong reasons (exit 2)
    """
    modules = [f"omniagentos.subset.mod_{i}" for i in range(17)]
    report = summary_from_module_list(modules, statements=2025, covered=1721)
    result = evaluate_coverage_report(report, policy)

    checks = {
        "ok_is_false": result.ok is False,
        "is_subset": result.is_subset is True,
        "measured_modules_is_17": result.measured_modules == 17,
        "has_min_measured_reason": any(
            "min_measured_modules" in r or "measured_modules=" in r for r in result.reasons
        ),
        "has_subset_message": any("subset" in r.lower() for r in result.reasons),
        "has_boundary_gap": bool(result.missing_boundaries)
        or any("boundary" in r.lower() for r in result.reasons),
    }
    all_ok = all(checks.values())

    if result.ok:
        status = DEMO_UNEXPECTED_PASS
        code = 2
    elif not all_ok:
        status = DEMO_WRONG_SHAPE
        code = 2
    else:
        status = DEMO_EXPECTED
        code = 0

    payload: dict[str, Any] = {
        "mode": "demo_subset",
        "status": status,
        "checks": checks,
        "evaluation": result.as_dict(),
        "exit_code": code,
    }
    return payload, code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("coverage_json", nargs="?", help="coverage.py JSON report path")
    parser.add_argument(
        "--demo-subset",
        action="store_true",
        help=(
            "Run the audited 17-module subset self-check; exit 0 only when policy "
            "rejects it with the expected subset/boundary reasons"
        ),
    )
    args = parser.parse_args(argv)

    try:
        policy = load_coverage_policy()
    except Exception as exc:  # noqa: BLE001
        print(
            json.dumps(
                {
                    "mode": "demo_subset" if args.demo_subset else "evaluate",
                    "status": DEMO_CONFIG_ERROR,
                    "error": f"failed to load coverage policy: {exc}",
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 3

    if args.demo_subset:
        try:
            payload, code = _demo_subset_assertion(policy)
        except Exception as exc:  # noqa: BLE001
            print(
                json.dumps(
                    {
                        "mode": "demo_subset",
                        "status": DEMO_CONFIG_ERROR,
                        "error": str(exc),
                        "traceback": traceback.format_exc(),
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 3
        print(json.dumps(payload, indent=2, sort_keys=True))
        return code

    if not args.coverage_json:
        parser.error("coverage_json path is required unless --demo-subset is set")

    path = Path(args.coverage_json)
    if not path.is_file():
        print(
            json.dumps(
                {
                    "mode": "evaluate",
                    "status": DEMO_CONFIG_ERROR,
                    "error": f"coverage report not found: {path}",
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 3

    try:
        payload_json = json.loads(path.read_text(encoding="utf-8"))
        report = summary_from_coverage_json(payload_json)
        result: CoverageEvaluation = evaluate_coverage_report(report, policy)
    except Exception as exc:  # noqa: BLE001
        print(
            json.dumps(
                {
                    "mode": "evaluate",
                    "status": DEMO_CONFIG_ERROR,
                    "error": str(exc),
                    "traceback": traceback.format_exc(),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 3

    out = {
        "mode": "evaluate",
        "status": "ok" if result.ok else "policy_failed",
        "evaluation": result.as_dict(),
    }
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
