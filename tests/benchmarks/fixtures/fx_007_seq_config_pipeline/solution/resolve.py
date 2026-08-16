"""Placeholder Resolver

This module resolves nested placeholder references (${section.key}) within configurations.
"""

from __future__ import annotations


class MissingReference(ValueError):
    """Raised when a referenced section or key is missing."""

    pass


class CircularReference(ValueError):
    """Raised when a circular reference chain is detected."""

    pass


def _find_placeholders(val: str) -> list[tuple[str, str]]:
    """Find all unique section.key targets from ${section.key} placeholders."""
    placeholders = []
    idx = 0
    while True:
        start_idx = val.find("${", idx)
        if start_idx == -1:
            break
        end_idx = val.find("}", start_idx + 2)
        if end_idx == -1:
            break
        content = val[start_idx + 2 : end_idx]
        if "." not in content:
            raise MissingReference(f"Invalid placeholder format (no dot): ${{{content}}}")
        ref_s, ref_k = content.split(".", 1)
        placeholders.append((ref_s, ref_k))
        idx = end_idx + 1
    return placeholders


def _expand_value(val: str, resolved_map: dict[tuple[str, str], str]) -> str:
    """Substitute resolved placeholders in the given string."""
    res = []
    idx = 0
    n = len(val)
    while idx < n:
        if val[idx] == "$" and idx + 1 < n and val[idx + 1] == "{":
            end_idx = val.find("}", idx + 2)
            if end_idx != -1:
                placeholder = val[idx + 2 : end_idx]
                if "." in placeholder:
                    ref_s, ref_k = placeholder.split(".", 1)
                    if (ref_s, ref_k) in resolved_map:
                        res.append(resolved_map[(ref_s, ref_k)])
                        idx = end_idx + 1
                        continue
                raise MissingReference(f"Invalid reference: ${{{placeholder}}}")
            else:
                res.append(val[idx])
                idx += 1
        else:
            res.append(val[idx])
            idx += 1
    return "".join(res)


def resolve(config: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
    """Resolve all ${section.key} placeholders in the configuration values.

    Args:
        config: The parsed INI dictionary.

    Returns:
        A new dict with all placeholders fully resolved.

    Raises:
        MissingReference: If a reference targets a missing section or key.
        CircularReference: If a reference loop is detected.
    """
    resolved: dict[tuple[str, str], str] = {}

    def resolve_key(s: str, k: str, visiting: list[tuple[str, str]]) -> str:
        if (s, k) in resolved:
            return resolved[(s, k)]
        if (s, k) in visiting:
            cycle_path = " -> ".join(f"{vs}.{vk}" for vs, vk in visiting)
            raise CircularReference(f"Circular reference detected: {cycle_path} -> {s}.{k}")

        visiting.append((s, k))

        if s not in config or k not in config[s]:
            raise MissingReference(f"Missing reference: {s}.{k}")

        raw_val = config[s][k]
        deps = _find_placeholders(raw_val)

        for dep_s, dep_k in deps:
            resolve_key(dep_s, dep_k, visiting)

        resolved[(s, k)] = _expand_value(raw_val, resolved)
        visiting.pop()
        return resolved[(s, k)]

    # Resolve every key across all sections
    for s in config:
        for k in config[s]:
            resolve_key(s, k, [])

    # Construct the resolved config
    result: dict[str, dict[str, str]] = {}
    for s in config:
        result[s] = {}
        for k in config[s]:
            result[s][k] = resolved[(s, k)]

    return result
