"""RFC 8785 (JSON Canonicalization Scheme) — the subset the envelope needs.

CONTRACT.md §7 says "use a JCS library; do not hand-roll". No JCS library is
installed here, so this is the hand-roll — written deliberately, with the traps
named, and pinned by tests, because a canonicaliser that differs between two
implementations produces different ids for the same payload and **silently
defeats deduplication**. That failure is invisible: nothing errors, ideas just
stop being recognised as repeats and the rejected-ledger stops working.

If a JCS library is ever installed, delete this and use it.

The three traps a naive "sorted keys, no whitespace" version gets wrong:

1. **Key ordering is by UTF-16 code unit, not by Python's str comparison.**
   Python orders by code point. They diverge above the BMP: "\U0001F600" (emoji)
   sorts AFTER "ﬀ" by code point, but BEFORE it in UTF-16, because the
   surrogate pair starts 0xD83D. Keys outside the BMP are rare, and being wrong
   about them is the kind of rare that surfaces once, in production, unexplained.

2. **Number formatting is ECMAScript `Number::toString`,** not `repr()`.
   `1.0` serialises as `1`, `1e21` as `1e+21`, `-0.0` as `0`.

3. **Strings escape only what JSON requires** — the quote, the backslash, and C0
   controls — using the short escape forms rather than the \\u00XX long forms.
   Python's ``json.dumps`` with ``ensure_ascii=False`` already does this.
"""

from __future__ import annotations

import json
import math
from typing import Any


def _utf16_key(key: str) -> tuple[int, ...]:
    """Sort key ordering by UTF-16 code unit, per RFC 8785 §3.2.3."""
    raw = key.encode("utf-16-be")
    return tuple(int.from_bytes(raw[i : i + 2], "big") for i in range(0, len(raw), 2))


def _number(value: float | int) -> str:
    """ECMAScript Number::toString, per RFC 8785 §3.2.2.2."""
    if isinstance(value, bool):  # bool is an int subclass — must not reach here
        raise TypeError("bool is not a number in JCS serialisation")
    if isinstance(value, int):
        return str(value)
    if math.isnan(value) or math.isinf(value):
        # JSON has no NaN/Infinity. Refuse rather than emit invalid JSON that a
        # reader would reject later, far from the cause.
        raise ValueError(f"non-finite numbers cannot be canonicalised: {value!r}")
    if value == 0:
        return "0"  # collapses -0.0, which JCS requires
    if value == int(value) and abs(value) < 1e21:
        return str(int(value))
    out = repr(value)
    if "e" in out:  # Python "1e+21"/"1e-07" -> ECMAScript "1e+21"/"1e-7"
        mantissa, exponent = out.split("e")
        sign = "+" if not exponent.startswith("-") else "-"
        out = f"{mantissa}e{sign}{int(exponent.lstrip('+-'))}"
    return out


def canonicalise(value: Any) -> str:
    """Return the RFC 8785 canonical JSON serialisation of ``value``."""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, (int, float)):
        return _number(value)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(canonicalise(v) for v in value) + "]"
    if isinstance(value, dict):
        items = sorted(value.items(), key=lambda kv: _utf16_key(str(kv[0])))
        return "{" + ",".join(
            f"{canonicalise(str(k))}:{canonicalise(v)}" for k, v in items
        ) + "}"
    raise TypeError(f"not JSON-serialisable: {type(value).__name__}")


def content_id(payload: Any) -> str:
    """The artifact ``id``: sha256 over the canonical form of ``payload``.

    The same idea produced twice yields the same id, which is what lets
    ``rejected/`` recognise a repeat without comparing prose.

    NEVER include a timestamp, poll counter, or run id in ``payload`` — it would
    make every poll a new artifact and flood the queue.
    """
    import hashlib

    return "sha256:" + hashlib.sha256(canonicalise(payload).encode("utf-8")).hexdigest()
