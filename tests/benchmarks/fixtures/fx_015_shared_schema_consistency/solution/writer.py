# writer.py (solution version)
from __future__ import annotations

import schema
import validator


def escape_val(s: str) -> str:
    res = []
    for char in s:
        if char in ("|", "\\"):
            res.append("\\")
        res.append(char)
    return "".join(res)


def encode(record: dict[str, object]) -> str:
    problems = validator.validate(record)
    if problems:
        raise ValueError(f"Invalid record: {problems}")

    parts = []
    for field in schema.FIELDS:
        val = record.get(field.name)
        if val is None:
            # fill default for optional/non-required field
            if field.kind == "int":
                val = 0
            elif field.kind == "str":
                val = ""
            elif field.kind == "bool":
                val = False

        # Serialize based on kind
        if field.kind == "int":
            serialized = str(val)
        elif field.kind == "bool":
            serialized = "true" if val else "false"
        elif field.kind == "str":
            serialized = escape_val(str(val))
        else:
            serialized = str(val)

        parts.append(f"{field.name}={serialized}")

    return "|".join(parts)
