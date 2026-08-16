# validator.py (solution version)
from __future__ import annotations

import schema


def validate(record: dict[str, object]) -> list[str]:
    problems = []

    # Check for unknown keys
    allowed_names = set(schema.field_names())
    for k in record:
        if k not in allowed_names:
            problems.append(f"unknown field: {k}")

    # Check each schema field
    for field in schema.FIELDS:
        if field.required and field.name not in record:
            problems.append(f"missing required field: {field.name}")
        elif field.name in record:
            val = record[field.name]
            if field.kind == "int":
                if not isinstance(val, int) or isinstance(val, bool):
                    problems.append(
                        f"invalid type for field {field.name}: expected int, got {type(val).__name__}"
                    )
            elif field.kind == "bool":
                if not isinstance(val, bool):
                    problems.append(
                        f"invalid type for field {field.name}: expected bool, got {type(val).__name__}"
                    )
            elif field.kind == "str":
                if not isinstance(val, str):
                    problems.append(
                        f"invalid type for field {field.name}: expected str, got {type(val).__name__}"
                    )

    return sorted(problems)
