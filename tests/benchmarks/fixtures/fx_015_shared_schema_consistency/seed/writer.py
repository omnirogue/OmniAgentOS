# writer.py (seed version)
from __future__ import annotations

import schema  # noqa: F401 -- imported and then ignored on purpose: the v1

# consumers hardcode their fields instead of deriving them from the shared schema,
# which is exactly the defect the task has to remove.


def encode(record: dict[str, object]) -> str:
    # Seed version hardcodes v1 fields id and name without escaping
    return f"id={record['id']}|name={record['name']}"
