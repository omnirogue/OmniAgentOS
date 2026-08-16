"""Structured RESUME block — the swarm documents contract's machine-readable
hand-off record (U1, ``Task-Contract-Integration-Plan-2026-08-08.md``).

A worker appends the block to its ``WORKBOOK.md`` at checkpoints, fenced with
the canonical tag::

    ```resume
    {"resume_v": 1, "status": "...", ...}
    ```

The relay (``swarm.spawn.UnifiedSpawner._build_prompt``) extracts the LAST such
block before the workbook is head-truncated, so the structured state survives a
hand-off that the 8 KB truncation would otherwise eat, and re-emits it inside
its own ``fence_data_block`` DATA section. The block is predecessor-authored
content: it is DATA, never instructions, and it never widens the relay's byte
budget beyond its own cap.

Design notes:

* **Two carriers, one meaning.** ``RESUME_BLOCK_SCHEMA`` is the declarative
  JSON Schema (the documentation of record, shippable to anything that speaks
  JSON Schema); ``validate_resume_block`` is a dependency-free hand validator
  used at relay time, because the relay must not acquire a runtime dependency
  on an undeclared transitive package. ``tests/swarm/test_spawn.py`` proves the
  two agree wherever ``jsonschema`` is importable.
* **Never crash, never block the relay.** Every failure mode — no block, bad
  JSON, wrong shape, over the byte cap — returns ``(None, reason)`` and the
  caller falls back to today's truncated-workbook behaviour.
* **Permissive on unknown keys** (``additionalProperties: true``), matching the
  estate's contract convention: a newer worker may record more than this
  version names, and that must relay rather than be dropped — but permissive is
  not unbounded: unknown content is bounded in DEPTH and in RENDERED SIZE (see
  ``MAX_RESUME_NESTING_DEPTH`` / :func:`resume_render_cap_bytes`), because a
  byte cap on the input does not bound the work the input causes.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

# The canonical fence tag. ONE spelling, so extraction is mechanical; the
# workbook template deliberately shows its example under a DIFFERENT tag
# (``resume-example``) so a freshly seeded workbook cannot be mistaken for a
# worker-authored checkpoint.
RESUME_FENCE_TAG = "resume"

# The fence label used when the block is re-emitted into a relay prompt.
# ``prompt_safety.fence_data_block`` requires ``[A-Z0-9_]+``.
RESUME_DATA_LABEL = "WORKER_RESUME_STATE"

# Schema version of record. An unrecognised ``resume_v`` is refused rather than
# guessed at — carrying a block we cannot interpret is worse than falling back.
RESUME_BLOCK_VERSION = 1
SUPPORTED_RESUME_VERSIONS: frozenset[int] = frozenset({RESUME_BLOCK_VERSION})

RESUME_CAP_BYTES_ENV = "OMNIAGENTOS_RESUME_CAP_BYTES"
DEFAULT_RESUME_CAP_BYTES = 4096

# -- structural bounds (F5) --------------------------------------------------
# An INPUT byte cap does not bound the WORK the input causes. A 2 KB body of
# ``{"resume_v":1,"x":[[[[ ... ]]]]}`` sits far under the 4096-byte cap, parses
# fine (``json.loads`` is iterative enough to survive ~5000 levels), and then
# blows up downstream: ``json.dumps`` recurses, so depth >= 1000 raised an
# UNCAUGHT RecursionError on the relay path, and at depth 900 it rendered
# 1.6 MB from 1.8 KB of input. Predecessor-controlled data must never be able
# to do either, so BOTH dimensions are bounded here:
#
#   * DEPTH — nesting deeper than ``MAX_RESUME_NESTING_DEPTH`` is CLAMPED (the
#     over-deep subtree is replaced by a marker) by an ITERATIVE walk, so the
#     bound-enforcer cannot itself recurse. Clamping rather than rejecting is
#     deliberate: the schema's own fields are at most three levels deep, so
#     depth can only come from the forward-compat unknown-key space, and
#     throwing away a worker's real status/failed_decisions because something
#     nested an unknown blob would trade a DoS for a data-loss.
#   * OUTPUT — the rendered string is capped independently of the input, so a
#     small input can never produce a large prompt slice.
#
# Every violation still ends in the same place as every other bad block: a
# ``(None, reason)`` fallback to the plain truncated-workbook relay.
MAX_RESUME_NESTING_DEPTH = 32
RESUME_RENDER_CAP_BYTES_ENV = "OMNIAGENTOS_RESUME_RENDER_CAP_BYTES"
DEFAULT_RESUME_RENDER_CAP_BYTES = 8192
DEPTH_CLAMP_MARKER = f"[omniagentos: nesting beyond depth {MAX_RESUME_NESTING_DEPTH} omitted]"

_FENCE_RE = re.compile(
    rf"^[ \t]*```{re.escape(RESUME_FENCE_TAG)}[ \t]*\r?\n(?P<body>.*?)^[ \t]*```[ \t]*$",
    re.MULTILINE | re.DOTALL,
)

_STRING_FIELDS: tuple[str, ...] = ("status", "progress")
_STRING_LIST_FIELDS: tuple[str, ...] = ("remaining", "next_actions")
# field -> (required object keys, optional object keys)
_OBJECT_LIST_FIELDS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "best_decisions": (("item", "reason"), ()),
    "failed_decisions": (("item", "reason"), ("evidence",)),
    "completed_experiments": (("experiment", "winner"), ("evidence",)),
}

_DECISION_ITEM_SCHEMA = {
    "type": "object",
    "required": ["item", "reason"],
    "properties": {"item": {"type": "string"}, "reason": {"type": "string"}},
    "additionalProperties": True,
}
_FAILED_ITEM_SCHEMA = {
    "type": "object",
    "required": ["item", "reason"],
    "properties": {
        "item": {"type": "string"},
        "reason": {"type": "string"},
        "evidence": {"type": "string"},
    },
    "additionalProperties": True,
}
_EXPERIMENT_ITEM_SCHEMA = {
    "type": "object",
    "required": ["experiment", "winner"],
    "properties": {
        "experiment": {"type": "string"},
        "winner": {"type": "string"},
        "evidence": {"type": "string"},
    },
    "additionalProperties": True,
}
_TEST_RUN_ITEM_SCHEMA = {
    "type": "object",
    "required": ["command", "exit_code"],
    "properties": {"command": {"type": "string"}, "exit_code": {"type": "integer"}},
    "additionalProperties": True,
}

RESUME_BLOCK_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "swarm RESUME block (v1)",
    "type": "object",
    "required": ["resume_v"],
    "additionalProperties": True,
    "properties": {
        "resume_v": {"const": RESUME_BLOCK_VERSION},
        "status": {"type": "string"},
        "progress": {"type": "string"},
        "remaining": {"type": "array", "items": {"type": "string"}},
        "next_actions": {"type": "array", "items": {"type": "string"}},
        "best_decisions": {"type": "array", "items": _DECISION_ITEM_SCHEMA},
        "failed_decisions": {"type": "array", "items": _FAILED_ITEM_SCHEMA},
        "completed_experiments": {"type": "array", "items": _EXPERIMENT_ITEM_SCHEMA},
        "tests_run": {"type": "array", "items": _TEST_RUN_ITEM_SCHEMA},
    },
}


def _env_bytes(name: str, default: int) -> int:
    raw = os.environ.get(name, "")
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


def resume_cap_bytes() -> int:
    """The byte cap for one RESUME block; malformed settings fall back."""

    return _env_bytes(RESUME_CAP_BYTES_ENV, DEFAULT_RESUME_CAP_BYTES)


def resume_render_cap_bytes() -> int:
    """The byte cap for the RENDERED block that reaches the prompt.

    Independent of the input: re-serialisation can be far larger than the text
    it came from (pretty-printing multiplies by the nesting depth), so the
    output needs its own ceiling. Configuring a bigger INPUT cap raises this
    one with it, because the two only make sense together.
    """

    raw = os.environ.get(RESUME_RENDER_CAP_BYTES_ENV, "").strip()
    if raw:
        try:
            configured = int(raw)
        except (TypeError, ValueError):
            configured = -1
        if configured >= 0:
            return configured
    return max(DEFAULT_RESUME_RENDER_CAP_BYTES, 2 * resume_cap_bytes())


def clamp_nesting(value: Any, max_depth: int = MAX_RESUME_NESTING_DEPTH) -> tuple[Any, bool]:
    """Return ``(bounded_copy, was_clamped)``: containers nested deeper than
    ``max_depth`` are replaced by :data:`DEPTH_CLAMP_MARKER`.

    ITERATIVE by construction. A recursive depth-limiter would blow the stack
    on exactly the input it exists to defend against, which is how this class
    of guard usually fails.
    """

    if not isinstance(value, (dict, list)):
        return value, False
    root: Any = {} if isinstance(value, dict) else []
    clamped = False
    # (source container, destination container, depth of the source's children)
    stack: list[tuple[Any, Any, int]] = [(value, root, 1)]
    while stack:
        source, destination, depth = stack.pop()
        items = source.items() if isinstance(source, dict) else enumerate(source)
        for key, item in items:
            if isinstance(item, (dict, list)):
                if depth >= max_depth:
                    new_item: Any = DEPTH_CLAMP_MARKER
                    clamped = True
                else:
                    new_item = {} if isinstance(item, dict) else []
                    stack.append((item, new_item, depth + 1))
            else:
                new_item = item
            if isinstance(destination, dict):
                destination[key] = new_item
            else:
                destination.append(new_item)
    return root, clamped


def _is_str(value: Any) -> bool:
    return isinstance(value, str)


def _is_int(value: Any) -> bool:
    # ``bool`` is an ``int`` subclass in Python; an exit code of ``true`` is a
    # malformed record, and JSON Schema agrees (booleans are not integers).
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_object_list(
    field: str, value: Any, required_keys: tuple[str, ...], optional_keys: tuple[str, ...]
) -> str | None:
    if not isinstance(value, list):
        return f"{field} must be a list"
    for index, entry in enumerate(value):
        if not isinstance(entry, dict):
            return f"{field}[{index}] must be an object"
        for key in required_keys:
            if key not in entry:
                return f"{field}[{index}] is missing {key!r}"
            if not _is_str(entry[key]):
                return f"{field}[{index}].{key} must be a string"
        for key in optional_keys:
            if key in entry and not _is_str(entry[key]):
                return f"{field}[{index}].{key} must be a string"
    return None


def validate_resume_block(block: Any) -> str | None:
    """Return ``None`` when ``block`` is a valid RESUME block, else the reason.

    Dependency-free mirror of :data:`RESUME_BLOCK_SCHEMA`. Every list field is
    optional but typed; ``resume_v`` is required and must name a supported
    version. Unknown keys are allowed and preserved.
    """

    if not isinstance(block, dict):
        return "resume block must be a JSON object"
    if "resume_v" not in block:
        return "resume block is missing 'resume_v'"
    version = block["resume_v"]
    if not _is_int(version) or version not in SUPPORTED_RESUME_VERSIONS:
        return f"unsupported resume_v: {version!r}"

    for field in _STRING_FIELDS:
        if field in block and not _is_str(block[field]):
            return f"{field} must be a string"

    for field in _STRING_LIST_FIELDS:
        if field not in block:
            continue
        value = block[field]
        if not isinstance(value, list):
            return f"{field} must be a list"
        for index, entry in enumerate(value):
            if not _is_str(entry):
                return f"{field}[{index}] must be a string"

    for field, (required_keys, optional_keys) in _OBJECT_LIST_FIELDS.items():
        if field in block:
            error = _validate_object_list(field, block[field], required_keys, optional_keys)
            if error is not None:
                return error

    if "tests_run" in block:
        value = block["tests_run"]
        if not isinstance(value, list):
            return "tests_run must be a list"
        for index, entry in enumerate(value):
            if not isinstance(entry, dict):
                return f"tests_run[{index}] must be an object"
            if "command" not in entry:
                return f"tests_run[{index}] is missing 'command'"
            if not _is_str(entry["command"]):
                return f"tests_run[{index}].command must be a string"
            if "exit_code" not in entry:
                return f"tests_run[{index}] is missing 'exit_code'"
            if not _is_int(entry["exit_code"]):
                return f"tests_run[{index}].exit_code must be an integer"

    return None


def extract_last_resume_block(
    workbook_text: str, *, cap_bytes: int | None = None
) -> tuple[dict[str, Any] | None, str | None]:
    """Return ``(block, None)`` for the LAST valid RESUME block, else ``(None, reason)``.

    Never raises: an unreadable, malformed, oversized, or over-nested block
    degrades the relay to its pre-U1 behaviour rather than failing the hand-off.
    Only the LAST block is considered — earlier checkpoints are superseded
    state, and scanning back for "the last one that happens to validate" would
    let a stale record outrank the worker's most recent word.

    The returned block is GUARANTEED renderable: depth is clamped and the
    rendered form is proved to fit :func:`resume_render_cap_bytes` before the
    block is handed back, so no caller can be surprised downstream (F5).
    """

    if not isinstance(workbook_text, str) or not workbook_text:
        return None, "no resume block found"
    matches = list(_FENCE_RE.finditer(workbook_text))
    if not matches:
        return None, "no resume block found"

    body = matches[-1].group("body")
    cap = resume_cap_bytes() if cap_bytes is None else cap_bytes
    size = len(body.encode("utf-8"))
    if size > cap:
        return None, f"resume block is {size} bytes; cap is {cap}"

    try:
        parsed = json.loads(body)
    except (ValueError, TypeError) as exc:
        return None, f"resume block is not valid JSON: {exc}"
    except RecursionError:
        # ``json.loads`` recurses in the C scanner; a body small enough to pass
        # the cap can still out-nest the interpreter's stack.
        return None, "resume block nesting is too deep to parse"
    except Exception as exc:  # noqa: BLE001 -- the never-raises contract is absolute
        return None, f"resume block could not be parsed: {exc}"

    # Bound DEPTH before anything walks the structure (F5). Clamping happens
    # before validation, but cannot change a validation verdict: every field
    # the schema names lives at depth <= 3.
    parsed, _ = clamp_nesting(parsed)

    error = validate_resume_block(parsed)
    if error is not None:
        return None, error
    # ``validate_resume_block`` already refused anything that is not an object.
    block = dict(parsed)
    if render_resume_block(block) is None:
        # Prove the block is carriable HERE, so "extract succeeded" is a real
        # promise to the relay rather than a problem deferred to the caller.
        return None, f"resume block does not render within {resume_render_cap_bytes()} bytes"
    return block, None


# ``ensure_ascii=False`` is deliberate and load-bearing, NOT a style choice —
# do not "simplify" it away. It keeps a successor's RESUME block human-readable
# (real UTF-8, not ``\uXXXX`` soup), but it also lets ``json.dumps`` pass a LONE
# SURROGATE through as a raw str char WITHOUT raising (F6): a worker who pastes a
# truncated emoji — the six ASCII bytes ``\ud83d`` are valid UTF-8 on disk, and
# ``json.loads`` turns them into an actual lone high-surrogate. The subsequent
# ``.encode("utf-8")`` is what raises ``UnicodeEncodeError`` on such a value, so
# every renderer returns its output ALREADY UTF-8 ENCODED with that encode
# INSIDE the guard — a surrogate-poisoned block yields ``None`` (the documented
# fallback), never an exception, and the render/encode boundary can never drift
# back apart.


def _dump_pretty(payload: Any) -> bytes | None:
    try:
        return json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=False).encode("utf-8")
    except Exception:  # noqa: BLE001 -- incl. RecursionError/UnicodeEncodeError; never raises
        return None


def _dump_compact(payload: Any) -> bytes | None:
    try:
        return json.dumps(
            payload, separators=(",", ":"), ensure_ascii=False, sort_keys=False
        ).encode("utf-8")
    except Exception:  # noqa: BLE001 -- incl. RecursionError/UnicodeEncodeError; never raises
        return None


def render_resume_block(block: dict[str, Any]) -> str | None:
    """Render a validated block as the canonical JSON carried by the relay.

    Returns ``None`` — never raises, never an oversized string — when the block
    cannot be rendered inside :func:`resume_render_cap_bytes`. Callers treat
    ``None`` exactly like an absent block: fall back to the plain workbook.

    Pretty-printing is preferred (a successor reads this) but is not worth an
    unbounded prompt: over the cap the renderer retries compact, and only then
    gives up. Depth is already clamped by ``extract_last_resume_block``; the
    belt-and-braces ``RecursionError`` guard is here because this function is
    public and a caller may hand it a dict that never went through extraction.
    The renderers encode to UTF-8 inside their own guard, so a lone-surrogate
    value (F6) drops to ``None`` here rather than raising on the byte count.
    """

    cap = resume_render_cap_bytes()
    # Idempotent for anything that came through ``extract_last_resume_block``;
    # load-bearing for a direct caller. Iterative, so it cannot itself recurse.
    bounded, _ = clamp_nesting(block)
    for renderer in (_dump_pretty, _dump_compact):
        encoded = renderer(bounded)
        if encoded is not None and len(encoded) <= cap:
            # Decoding cannot fail: the bytes came from a successful UTF-8
            # encode inside the renderer's guard.
            return encoded.decode("utf-8")
    return None


# Worker-facing template lines. Kept here (not inlined at the call sites) so the
# workbook seed and the worker brief cannot drift apart, and so the
# foreign-family lint has one place to check.
RESUME_INSTRUCTION_LINES: tuple[str, ...] = (
    "## Resume state",
    "",
    "At each checkpoint append a NEW fenced block tagged `resume` (the last one",
    "wins) so a successor inherits your structured state — it survives relay",
    "truncation, prose does not. Record what FAILED as well as what worked, so",
    "nobody repeats a dead experiment. Shape (copy it, and fence yours with",
    "```resume):",
    "",
    "```resume-example",
    '{"resume_v": 1, "status": "WORKING", "progress": "1 of 3 modules landed",',
    ' "remaining": ["wire the parser"],',
    ' "best_decisions": [{"item": "streaming parse", "reason": "bounded memory"}],',
    ' "failed_decisions": [{"item": "regex split", "reason": "quadratic on nested input",',
    '   "evidence": "10s on the fixture corpus"}],',
    ' "completed_experiments": [{"experiment": "tokenizer A vs B", "winner": "B",',
    '   "evidence": "2x faster on the corpus"}],',
    ' "tests_run": [{"command": "pytest tests/parser", "exit_code": 1}],',
    ' "next_actions": ["fix the failing case, then re-run the suite"]}',
    "```",
    "",
)

RESUME_BRIEF_LINES: tuple[str, ...] = (
    "- append a `resume` block (see the workbook's '## Resume state' section)",
    "  at each checkpoint: status, progress, remaining, best/failed decisions,",
    "  completed experiments, tests run, next actions. The LAST one is what a",
    "  successor inherits, so keep it current and honest about failures.",
)


__all__ = [
    "DEFAULT_RESUME_CAP_BYTES",
    "DEFAULT_RESUME_RENDER_CAP_BYTES",
    "DEPTH_CLAMP_MARKER",
    "MAX_RESUME_NESTING_DEPTH",
    "RESUME_BLOCK_SCHEMA",
    "RESUME_BLOCK_VERSION",
    "RESUME_BRIEF_LINES",
    "RESUME_CAP_BYTES_ENV",
    "RESUME_DATA_LABEL",
    "RESUME_FENCE_TAG",
    "RESUME_INSTRUCTION_LINES",
    "RESUME_RENDER_CAP_BYTES_ENV",
    "SUPPORTED_RESUME_VERSIONS",
    "clamp_nesting",
    "extract_last_resume_block",
    "render_resume_block",
    "resume_cap_bytes",
    "resume_render_cap_bytes",
    "validate_resume_block",
]
