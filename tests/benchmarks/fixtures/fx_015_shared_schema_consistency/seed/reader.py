# reader.py (seed version)
from __future__ import annotations

import schema  # noqa: F401 -- imported and then ignored on purpose: the v1

# consumers hardcode their fields instead of deriving them from the shared schema,
# which is exactly the defect the task has to remove.


class DecodeError(ValueError):
    pass


def decode(line: str) -> dict[str, object]:
    # Seed version splits on | and hardcodes the two v1 keys
    parts = line.split("|")
    res = {}
    for part in parts:
        if not part:
            continue
        if "=" not in part:
            raise DecodeError("Invalid line format: missing '='")
        k, v = part.split("=", 1)
        if k == "id":
            try:
                res[k] = int(v)
            except ValueError:
                raise DecodeError("id must be an integer") from None
        elif k == "name":
            res[k] = v
        else:
            raise DecodeError(f"Unknown key: {k}")

    # Check required keys for v1
    if "id" not in res or "name" not in res:
        raise DecodeError("Missing required field")
    return res
