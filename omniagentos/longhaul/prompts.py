"""Longhaul prompt generation: initial, continuation, and steering formatting.

All untrusted content (steering, workbook, etc.) is wrapped with quote_untrusted.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from omniagentos.steward.quoting import quote_untrusted


def initial_prompt(
    task_dict: dict[str, Any],
    workbook_path: str,
    acceptance: str,
    category: Mapping[str, Any] | str | None,
    steering: list[dict[str, Any]] | None,
) -> str:
    """Generate the initial prompt for a longhaul task.

    Includes the longhaul protocol and acceptance criteria.
    """
    title = task_dict.get("title", "Untitled Task")
    brief = task_dict.get("brief", "")

    # Build the prompt
    prompt_parts = [
        f"# {title}",
        "",
        "## Brief",
        brief if brief else "(No brief provided)",
        "",
        "## Acceptance criteria",
        quote_untrusted(acceptance, source="acceptance"),
        "",
        "## Longhaul Protocol",
        "You are working on a long-horizon task that may take multiple sessions to complete.",
        "Follow these practices:",
        "",
        "1. **Maintain TodoWrite continuously**: Keep a checkboxed list of todos in your assistant context. Update it after each milestone.",
        "2. **Update WORKBOOK.md after milestones**: Write to "
        + workbook_path
        + " (in your extra write roots) after completing each major step:",
        "   - Update the ## Progress log section with what you've done",
        "   - Record any ## Decisions made",
        "   - Update ## Next steps with what's remaining",
        "3. **Commit early and often**: If in a git repo, commit frequently with clear messages. Commits are continuity markers.",
        "4. **Self-verify before finishing**: Review your work against the acceptance criteria. Ensure all requirements are met.",
        "5. **Finish by setting Status: DONE**: When complete, update WORKBOOK.md with `## Status` section set to `DONE` plus a completion summary.",
        "6. **If blocked**: Set `## Status: BLOCKED` and explain why. The operator will read and respond.",
        "7. **Accept steering mid-run**: Steering messages may arrive as tool feedback during your execution. Treat them as the operator's voice and adapt accordingly.",
        "",
        "## Steering",
    ]

    if steering:
        prompt_parts.append(steering_wrap(steering))
    else:
        prompt_parts.append("(No steering messages yet)")

    if category:
        category_name = (
            category.get("name", "Uncategorized") if isinstance(category, dict) else str(category)
        )
        prompt_parts.extend(
            [
                "",
                f"## Category: {category_name}",
                "Work in this category is serialized (one task at a time, in order).",
            ]
        )

    return "\n".join(prompt_parts)


# Heading for the structured RESUME section (U1). Provider-neutral by
# contract: this scaffolding reaches every model family, so it must never name
# one — see the foreign-family lint in tests/swarm/test_spawn.py.
RESUME_SECTION_HEADING = "### Structured resume state (predecessor-authored)"

# Recency precedence. The two carriers are cut differently: the RESUME block is
# taken from the WHOLE workbook (so it is the predecessor's latest word), while
# the workbook slice below it is HEAD-kept and may therefore end at the
# predecessor's EARLIEST content. That is not a contradiction to resolve
# silently — the successor is told which record is newer.
RESUME_PRECEDENCE_NOTE = (
    "This is the predecessor's LATEST checkpoint. The workbook below is a "
    "head-kept excerpt and may stop at earlier content, so where the two "
    "disagree, this block is the newer record."
)


def continuation_prompt(
    task_dict: dict[str, Any],
    workbook_content: str,
    git_summary: str | None,
    undelivered_steering: list[dict[str, Any]],
    prior_end_reason: str,
    *,
    workbook_block: str | None = None,
    resume_block: str | None = None,
) -> str:
    """Generate the continuation prompt for a handoff to the next executor.

    Includes the workbook (with quote_untrusted), last N steering turns, and git summary.

    ``workbook_block`` lets a caller supply an ALREADY-DELIMITED rendering of
    the workbook to use in place of the default ``quote_untrusted`` wrapper.
    It exists because the two containment schemes cannot be composed by
    nesting: ``quote_untrusted`` escapes ``<`` and ``>``, which is exactly what
    a caller's outer fence markers are made of, so fencing first and quoting
    second silently mangles the fence. The caller therefore chooses ONE
    delimiter for this slice, and the surrounding prompt — the successor's own
    task, which is genuine instruction — stays outside it either way.
    ``workbook_content`` is still read for the TodoWrite snapshot.

    ``resume_block`` is the caller's ALREADY-DELIMITED rendering of the
    predecessor's structured RESUME block (``swarm.resume_block``), placed
    AHEAD of the workbook because it is the distilled state and the workbook is
    the (truncated) long form. Same delimiter rule as ``workbook_block``: the
    caller fences it, this function does not re-quote it. Absent or invalid
    blocks simply omit the section.
    """
    title = task_dict.get("title", "Untitled Task")
    brief = task_dict.get("brief", "")

    todos_matches = list(re.finditer(r"^todos_json:\s?(.*)$", workbook_content, re.MULTILINE))
    last_todos = todos_matches[-1].group(1) if todos_matches else None

    resume_parts = (
        [RESUME_SECTION_HEADING, RESUME_PRECEDENCE_NOTE, "", resume_block, ""]
        if resume_block
        else []
    )

    prompt_parts = [
        "# You are taking over from a colleague mid-task",
        "",
        f"Task: {title}",
        "",
        "Brief:",
        brief if brief else "(No brief provided)",
        "",
        "## Your colleague's work so far",
        "",
        *resume_parts,
        "### Workbook (continuity record)",
        workbook_block
        if workbook_block is not None
        else quote_untrusted(workbook_content, source="workbook"),
        "",
        "### Last TodoWrite snapshot (verbatim)",
        quote_untrusted(last_todos, source="workbook:last_todos_json")
        if last_todos is not None
        else "No TodoWrite snapshot was found in the workbook.",
        "",
    ]

    if git_summary:
        prompt_parts.extend(
            [
                "### Git history",
                quote_untrusted(git_summary, source="git"),
                "",
            ]
        )

    # Include last 10 steering turns regardless of delivery status
    if undelivered_steering:
        prompt_parts.extend(
            [
                "### Steering (last operator messages)",
                "The following are pending or earlier steering messages. Apply them if they are still relevant.",
                "",
                steering_wrap(undelivered_steering[-10:]),  # Last 10
                "",
            ]
        )

    prompt_parts.extend(
        [
            "## Your task",
            "Continue where your colleague left off. Use the workbook to understand progress.",
            f"Prior session ended with: {prior_end_reason}",
            "",
            "Complete the work according to the acceptance criteria and finish with Status: DONE.",
        ]
    )

    return "\n".join(prompt_parts)


def steering_wrap(turns: list[dict[str, Any]]) -> str:
    """Format steering turns for delivery (prompt injection or hook response).

    Formats each turn with delivery status indication.
    """
    if not turns:
        return "(No steering turns)"

    lines = []
    for turn in turns:
        role = turn.get("role", "unknown").upper()
        content = turn.get("content", "")
        if not isinstance(content, str):
            content = str(content)
        seq = turn.get("seq", "?")
        meta = turn.get("meta_json", turn.get("meta", {}))
        if isinstance(meta, str):
            import json

            try:
                meta = json.loads(meta)
            except (json.JSONDecodeError, ValueError):
                meta = {}

        delivery = meta.get("delivery", {})
        if isinstance(delivery, dict):
            if delivery.get("delivered_at"):
                status = f" (delivered at {delivery['delivered_at']})"
            elif delivery.get("pending"):
                status = " (pending)"
            else:
                status = ""
        else:
            status = ""

        turn_id = turn.get("turn_id") or turn.get("id") or seq
        lines.append(f"**[Turn {seq}] {role}{status}**")
        lines.append(quote_untrusted(content, source=f"steering:{turn_id}"))
        lines.append("")

    return "\n".join(lines)
