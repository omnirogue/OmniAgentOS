# reader.py (solution version)
from __future__ import annotations

import schema


class DecodeError(ValueError):
    pass


def unescape_val(s: str) -> str:
    res = []
    escaped = False
    for char in s:
        if escaped:
            res.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        else:
            res.append(char)
    if escaped:
        raise DecodeError("Trailing backslash in escape sequence")
    return "".join(res)


def parse_pairs(line: str) -> dict[str, str]:
    pairs = {}
    current = []
    escaped = False
    for char in line:
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
            current.append(char)
        elif char == "|":
            pair_str = "".join(current)
            if "=" not in pair_str:
                raise DecodeError("Invalid pair format: missing '='")
            k, v = pair_str.split("=", 1)
            pairs[k] = v
            current = []
        else:
            current.append(char)
    if current or not line:
        pair_str = "".join(current)
        if "=" not in pair_str:
            raise DecodeError("Invalid pair format: missing '='")
        k, v = pair_str.split("=", 1)
        pairs[k] = v
    return pairs


def decode(line: str) -> dict[str, object]:
    try:
        raw_pairs = parse_pairs(line)
    except Exception as e:
        raise DecodeError(f"Failed to parse pairs: {e}") from e

    # Check for unknown keys
    allowed_names = set(schema.field_names())
    for k in raw_pairs:
        if k not in allowed_names:
            raise DecodeError(f"Unknown key: {k}")

    res = {}
    for field in schema.FIELDS:
        if field.name in raw_pairs:
            raw_val = raw_pairs[field.name]
            if field.kind == "int":
                try:
                    res[field.name] = int(raw_val)
                except ValueError:
                    raise DecodeError(
                        f"Field {field.name} must be an integer, got {raw_val}"
                    ) from None
            elif field.kind == "bool":
                if raw_val == "true":
                    res[field.name] = True
                elif raw_val == "false":
                    res[field.name] = False
                else:
                    raise DecodeError(f"Field {field.name} must be true/false, got {raw_val}")
            elif field.kind == "str":
                try:
                    res[field.name] = unescape_val(raw_val)
                except Exception as e:
                    raise DecodeError(f"Field {field.name} string unescape failed: {e}") from None
        else:
            # Field missing from the encoded line
            # Check back-compat special case for active
            if field.name == "active":
                res[field.name] = False
            elif field.required:
                raise DecodeError(f"Missing required field: {field.name}")
            else:
                # Default for optional field
                if field.kind == "int":
                    res[field.name] = 0
                elif field.kind == "str":
                    res[field.name] = ""
                elif field.kind == "bool":
                    res[field.name] = False

    return res
