"""Key-aware credential redaction for persistence surfaces.

This module extends scrub.py with:
- Credential-shaped key detection (AUTH/TOKEN/SECRET/KEY/PASSWORD/CREDENTIAL)
- Recursive value redaction under matching keys
- Safe JSON string parsing and scrubbing
- DoS protection via depth/size caps
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

# Credential-shaped key pattern: normalized to uppercase, checks for credential indicators
_CREDENTIAL_KEY_PATTERN = re.compile(
    r"(?i)(auth|token|secret|key|password|credential|api_key|access_key|private_key|bearer)",
)

# Scrubber version for tracking across persistence layers
SCRUBBER_VERSION = "1.0.0"
REDACTED = "[REDACTED]"

# DoS protection limits
MAX_JSON_PARSE_DEPTH = 10
MAX_JSON_PARSE_SIZE = 100_000  # bytes


def _is_credential_key(key: str) -> bool:
    """Check if a key name matches credential shape patterns.

    Normalizes key to uppercase and checks for patterns like:
    - AUTH, TOKEN, SECRET, KEY, PASSWORD, CREDENTIAL
    - API_KEY, ACCESS_KEY, PRIVATE_KEY, BEARER
    """
    normalized = str(key).upper()
    return bool(_CREDENTIAL_KEY_PATTERN.search(normalized))


def _is_json_shaped(value: str) -> bool:
    """Whether ``value`` is structured content we would have tried to parse.

    Used only to scope the size cap's fail-closed edge. A string that opens with
    ``{`` or ``[`` is a payload whose keys the key-aware pass was going to read;
    anything else (an HTML page, prose, a log tail) was never going to parse as
    JSON, so the cap did not cause us to skip any key inspection on it.
    """
    return value.lstrip()[:1] in ("{", "[")


def _get_scrub_text() -> Callable[[str], str]:
    """Lazily import scrub_text to avoid circular imports."""
    from omniagentos.toolplane.scrub import scrub_text

    return scrub_text


def _try_parse_json_string(
    value: str, current_depth: int = 0
) -> tuple[Any, bool]:
    """Safely parse a JSON string if it's valid and within size/depth limits.

    Returns (parsed_value, was_valid_json).
    If parsing fails or limits exceeded, returns (original_string, False).

    The depth guard uses the SAME cap as the recursion, so every over-cap value
    exits through the one fail-closed edge in ``_redact_value_recursively``
    rather than through a second, quieter one here that hands back an
    unparsed -- and therefore un-key-checked -- JSON string.
    """
    if current_depth > MAX_JSON_PARSE_DEPTH:
        return value, False

    if len(value) > MAX_JSON_PARSE_SIZE:
        return value, False

    try:
        parsed = json.loads(value)
        return parsed, True
    except (json.JSONDecodeError, ValueError, TypeError):
        return value, False


def _redact_value_recursively(
    value: Any,
    redaction_counts: dict[str, int],
    current_depth: int = 0,
    parent_key: str | None = None,
) -> Any:
    """Recursively redact values under credential-shaped keys.

    Tracks redaction counts by surface. Also scrubs all string values
    through the pattern-based scrubber to catch inline credentials.
    """
    scrub_text = _get_scrub_text()

    if current_depth > MAX_JSON_PARSE_DEPTH:
        # FAIL CLOSED at the DoS cap. Returning the raw value here made the cap
        # a deterministic bypass of the whole control: anything an attacker
        # could push past ten levels of nesting -- including a value under a
        # credential-shaped key, and including a JSON string carrying one --
        # round-tripped verbatim with ``redactions: 0``, and this is the
        # toolplane RESULT boundary, where the nesting is attacker-shaped.
        #
        # Past the cap we have stopped looking, and "we stopped looking" is not
        # "we looked and it was clean". So the subtree is redacted wholesale:
        # the cap now bounds the WORK, never the guarantee. Legitimate payloads
        # nested deeper than ten levels lose their tail in the persisted copy,
        # which is the correct direction to be wrong on a security boundary.
        redaction_counts["depth_cap_redaction"] = (
            redaction_counts.get("depth_cap_redaction", 0) + 1
        )
        return REDACTED

    # If parent key is credential-shaped, redact this value
    if parent_key is not None and _is_credential_key(parent_key):
        if isinstance(value, str) and value:
            redaction_counts["credential_key_redaction"] = (
                redaction_counts.get("credential_key_redaction", 0) + 1
            )
            return REDACTED
        elif isinstance(value, (dict, list)) and value:
            # Redact the entire structure under credential key
            redaction_counts["credential_key_redaction"] = (
                redaction_counts.get("credential_key_redaction", 0) + 1
            )
            return REDACTED

    # Process based on type
    if isinstance(value, str):
        # Try to parse as JSON and scrub internally
        parsed, was_json = _try_parse_json_string(value, current_depth)
        if was_json and isinstance(parsed, (dict, list)):
            # Recursively scrub the parsed structure
            scrubbed = _redact_value_recursively(
                parsed,
                redaction_counts,
                current_depth + 1,
                parent_key,
            )
            # Re-serialize with redactions applied
            redaction_counts["json_string_scrubbed"] = (
                redaction_counts.get("json_string_scrubbed", 0) + 1
            )
            return json.dumps(scrubbed)

        if len(value) > MAX_JSON_PARSE_SIZE and _is_json_shaped(value):
            # FAIL CLOSED at the size cap, for the same reason as the depth cap
            # above. This string is structured content whose keys the key-aware
            # pass was going to read, and the cap stopped us reading them -- so
            # a credential under a credential-shaped key inside it reached the
            # persisted copy verbatim, with `redactions: 0`. The pattern
            # scrubber does not cover the gap: `_PATTERNS` cannot see a
            # JSON-QUOTED key, because in `"api_key": "..."` the closing quote
            # sits between the name and the colon.
            #
            # Scoped to JSON-shaped strings deliberately. An over-cap HTML page
            # had no keys for the cap to hide, and blanking it would destroy the
            # payload `web_fetch` exists to return; there is nothing to fail
            # closed ABOUT. The edge covers exactly what we declined to inspect.
            redaction_counts["size_cap_redaction"] = (
                redaction_counts.get("size_cap_redaction", 0) + 1
            )
            return REDACTED

        # Not JSON or failed parse; apply pattern-based scrubbing
        scrubbed = scrub_text(value)
        if scrubbed != value:
            redaction_counts["pattern_redaction"] = (
                redaction_counts.get("pattern_redaction", 0) + 1
            )
        return scrubbed

    if isinstance(value, dict):
        result = {}
        for k, v in value.items():
            # Key names themselves get scrubbed (in case they contain patterns)
            scrubbed_key = scrub_text(str(k))
            if scrubbed_key != str(k):
                redaction_counts["key_scrubbed"] = (
                    redaction_counts.get("key_scrubbed", 0) + 1
                )
            # Values under credential keys get redacted
            result[scrubbed_key] = _redact_value_recursively(
                v,
                redaction_counts,
                current_depth + 1,
                parent_key=k,
            )
        return result

    if isinstance(value, list):
        return [
            _redact_value_recursively(
                item,
                redaction_counts,
                current_depth + 1,
                parent_key,
            )
            for item in value
        ]

    if isinstance(value, tuple):
        return tuple(
            _redact_value_recursively(
                item,
                redaction_counts,
                current_depth + 1,
                parent_key,
            )
            for item in value
        )

    if isinstance(value, set):
        return [
            _redact_value_recursively(
                item,
                redaction_counts,
                current_depth + 1,
                parent_key,
            )
            for item in sorted(value, key=repr)
        ]

    # Primitive types pass through
    return value


def sanitize_for_persistence(
    surface: str,
    payload: Any,
) -> tuple[Any, int, str]:
    """Sanitize a payload for safe persistence.

    Args:
        surface: Name of the persistence surface (e.g., "toolplane_result")
        payload: The data structure to sanitize (dict, list, str, etc)

    Returns:
        (sanitized_payload, total_redaction_count, scrubber_version)

    The function:
    - Redacts values under credential-shaped keys (auth/token/secret/key/password)
    - Parses and scrubs JSON strings when safe/bounded
    - Applies pattern-based scrubbing to all string values
    - Caps parse depth and size to prevent DoS, and REDACTS whatever is past the
      cap rather than passing it through (the cap bounds the work, never the
      guarantee) -- so the returned structure may be shallower than the input
    - Counts all redactions made
    """
    redaction_counts: dict[str, int] = {}
    sanitized = _redact_value_recursively(
        payload,
        redaction_counts,
        current_depth=0,
        parent_key=None,
    )

    # Sum all redaction types
    total_redactions = sum(redaction_counts.values())

    return sanitized, total_redactions, SCRUBBER_VERSION
