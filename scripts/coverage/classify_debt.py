#!/usr/bin/env python3
"""CLI: classify broad handlers and dead-code stubs (M-20, L-02).

Prints JSON summary. Does not rewrite source. Use --actionable-only to list
items that policy marks as actionable on sensitive paths / non-protocol stubs.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from omniagentos.testpolicy.deadcode import classify_tree_deadcode  # noqa: E402
from omniagentos.testpolicy.handlers import classify_tree_handlers  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--actionable-only",
        action="store_true",
        help="Only emit actionable handlers and actionable_dead stubs",
    )
    parser.add_argument(
        "--root",
        default=str(ROOT),
        help="Repository root to scan (default: this checkout)",
    )
    args = parser.parse_args(argv)
    root = Path(args.root)

    handlers = classify_tree_handlers(root)
    stubs = classify_tree_deadcode(root)

    if args.actionable_only:
        handlers = [h for h in handlers if h.actionable]
        stubs = [s for s in stubs if s.category == "actionable_dead"]

    by_fallback: dict[str, int] = {}
    for h in handlers:
        by_fallback[h.fallback] = by_fallback.get(h.fallback, 0) + 1
    by_category: dict[str, int] = {}
    for s in stubs:
        by_category[s.category] = by_category.get(s.category, 0) + 1

    payload = {
        "summary": {
            "broad_handlers": len(handlers),
            "handlers_by_fallback": by_fallback,
            "actionable_handlers": sum(1 for h in handlers if h.actionable),
            "stubs": len(stubs),
            "stubs_by_category": by_category,
        },
        "handlers": [h.as_dict() for h in handlers],
        "stubs": [s.as_dict() for s in stubs],
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
