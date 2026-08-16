# validator.py (seed version)
from __future__ import annotations

import schema  # noqa: F401 -- imported and then ignored on purpose: the v1

# consumers hardcode their fields instead of deriving them from the shared schema,
# which is exactly the defect the task has to remove.


def validate(record: dict[str, object]) -> list[str]:
    # Seed version hardcodes the two v1 fields
    problems = []
    # Check unknown
    for k in record:
        if k not in ("id", "name"):
            problems.append(f"unknown field: {k}")
    # Check required
    if "id" not in record:
        problems.append("missing required field: id")
    if "name" not in record:
        problems.append("missing required field: name")
    # Check types
    if "id" in record:
        if not isinstance(record["id"], int) or isinstance(record["id"], bool):
            problems.append(
                "invalid type for field id: expected int, got " + type(record["id"]).__name__
            )
    if "name" in record:
        if not isinstance(record["name"], str):
            problems.append(
                "invalid type for field name: expected str, got " + type(record["name"]).__name__
            )
    return sorted(problems)
