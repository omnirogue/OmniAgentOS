"""RFC 8785 (JCS) canonicalization and content identity for certification artifacts.

Two North Star adapters need the *same* artifact identity function:
``emit_gaps.py`` (gap signatures) and ``file_gap_findings.py`` (the loop-queue
envelope ``id``).  Two copies of a canonicalizer is the worst possible clone
family: a divergence produces *different ids for the same payload*, nothing
errors, and deduplication silently stops working.  So there is exactly one
implementation, here, and both adapters import it.

Deliberately **stdlib only**.  ``emit_gaps.py`` pulls in ``omniagentos.lab.db``;
``file_gap_findings.py`` must stay importable without it, because the queue
filer runs from launchd with a pinned interpreter and no repo import surface
beyond ``scripts/``.

This is a re-implementation of the ThreeLoops ``bridge/canonical.py`` contract,
not an import of it: ``~/.omniagentos/ops/ThreeLoops`` is a separate repository and a
read-only dependency.  The three traps it names are reproduced verbatim in
behaviour:

1. Key ordering is by **UTF-16 code unit** (RFC 8785 §3.2.3), not by Python's
   code-point ordering.  They diverge above the BMP.
2. Number formatting is ECMAScript ``Number::toString`` (§3.2.2.2): ``1.0`` is
   ``1``, ``1e21`` is ``1e+21``, ``-0.0`` is ``0``.
3. Strings escape only what JSON requires, which ``json.dumps(...,
   ensure_ascii=False)`` already does.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any


def _utf16_key(value: str) -> tuple[int, ...]:
    raw = value.encode("utf-16-be")
    return tuple(int.from_bytes(raw[index : index + 2], "big") for index in range(0, len(raw), 2))


def _number(value: int | float) -> str:
    if isinstance(value, bool):
        raise TypeError("bool is not a canonical number")
    if isinstance(value, int):
        return str(value)
    if not math.isfinite(value):
        raise ValueError("non-finite numbers cannot be canonicalized")
    if value == 0:
        return "0"
    if value == int(value) and abs(value) < 1e21:
        return str(int(value))
    rendered = repr(value)
    if "e" in rendered:
        mantissa, exponent = rendered.split("e")
        sign = "-" if exponent.startswith("-") else "+"
        rendered = f"{mantissa}e{sign}{int(exponent.lstrip('+-'))}"
    return rendered


def canonicalize(value: Any) -> str:
    """Local RFC-8785 subset used by the ThreeLoops artifact identity pattern."""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, int | float):
        return _number(value)
    if isinstance(value, list | tuple):
        return "[" + ",".join(canonicalize(item) for item in value) + "]"
    if isinstance(value, dict):
        items = sorted(value.items(), key=lambda item: _utf16_key(str(item[0])))
        return (
            "{"
            + ",".join(f"{canonicalize(str(key))}:{canonicalize(item)}" for key, item in items)
            + "}"
        )
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


def content_identity(payload: dict[str, Any]) -> str:
    """``sha256:`` over the canonical form of ``payload`` — and of nothing else.

    NEVER put a timestamp, run id, receipt path or counter in ``payload``: every
    run would then mint a new identity and the queue would flood with repeats of
    one gap.  Run-specific data belongs beside the payload, never inside it.
    """
    return "sha256:" + hashlib.sha256(canonicalize(payload).encode("utf-8")).hexdigest()
