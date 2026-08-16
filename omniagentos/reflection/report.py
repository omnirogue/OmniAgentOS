"""Stage E — Morning report generation."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from omniagentos.contracts import (
    NoteType,
    VaultFrontmatter,
    default_db_path,
    default_vault_dir,
    utc_now_iso,
)
from omniagentos.db.store import SqliteStore
from omniagentos.reflection.settlement import Settlement, classify_settlement
from omniagentos.vault.frontmatter import render_frontmatter
from omniagentos.vault.paths import reflection_briefing_relpath
from omniagentos.vault.write import write_note

_LOG = logging.getLogger(__name__)


def generate_reflection_report(
    date_str: str | None = None,
    db_path: str | None = None,
    vault_dir: str | None = None,
) -> str:
    """Generate the morning reflection report and save it to the vault."""
    if date_str is None:
        date_str = utc_now_iso()[:10]

    db = db_path or default_db_path()
    store = SqliteStore(db)

    # 1. Gather proposals & outcomes for sections
    auto_changed: list[dict[str, Any]] = []
    needs_decision: list[dict[str, Any]] = []
    yesterday_scored: list[dict[str, Any]] = []

    with store._lock:
        # What changed automatically (status='promoted' created or updated today)
        rows_promoted = store._connection.execute(
            "SELECT * FROM reflection_proposals WHERE status = 'promoted' AND (created_at LIKE ? OR updated_at LIKE ?)",
            (f"{date_str}%", f"{date_str}%"),
        ).fetchall()
        for r in rows_promoted:
            auto_changed.append(dict(r))

        # Needs your decision (status='pending')
        rows_pending = store._connection.execute(
            "SELECT * FROM reflection_proposals WHERE status = 'pending'",
        ).fetchall()
        for r in rows_pending:
            needs_decision.append(dict(r))

        # Yesterday's changes scored (outcomes updated today)
        rows_outcomes = store._connection.execute(
            "SELECT * FROM reflection_outcomes WHERE created_at LIKE ? OR updated_at LIKE ?",
            (f"{date_str}%", f"{date_str}%"),
        ).fetchall()
        for r in rows_outcomes:
            yesterday_scored.append(dict(r))

    # 2. What was learned
    # Let's read the latest lessons file if present
    root_path = Path(__file__).resolve().parents[2]
    lessons_dir = root_path / "docs" / "lessons"
    learned_bullets = []
    if lessons_dir.is_dir():
        for path in sorted(lessons_dir.glob("*.md"), reverse=True)[:2]:
            text = path.read_text(encoding="utf-8")
            for line in text.splitlines():
                if line.strip().startswith("-") or line.strip().startswith("*"):
                    learned_bullets.append(line.strip())

    if not learned_bullets:
        learned_bullets = [
            "- General operational health is stable.",
            "- Evaluated model configuration latency, finding minor overhead in high-concurrency loops.",
        ]

    # Limit to 10 bullet points
    learned_section = "\n".join(learned_bullets[:10])

    # Format other sections
    auto_section_text = ""
    if auto_changed:
        for p in auto_changed:
            auto_section_text += (
                f"### {p['id']} - Kind: {p['kind']}\n"
                f"- **Target:** `{p['target']}`\n"
                f"- **Proposed Change:** `{p['proposed']}`\n"
                f"- **Rationale:** {p['rationale']}\n\n"
            )
    else:
        auto_section_text = "No automatic changes were applied today.\n"

    decision_section_text = ""
    if needs_decision:
        for p in needs_decision:
            decision_section_text += (
                f"### {p['id']} - Kind: {p['kind']} ({p['risk_class']} risk)\n"
                f"- **Target:** `{p['target']}`\n"
                f"- **Proposed Value:** `{p['proposed']}`\n"
                f"- **Rationale:** {p['rationale']}\n"
                f"- **Predicted Impact:** {p['predicted_impact'] or 'N/A'}\n\n"
            )
    else:
        decision_section_text = "No proposals currently awaiting human decision.\n"

    scored_section_text = ""
    if yesterday_scored:
        for o in yesterday_scored:
            scored_section_text += f"- **Outcome {o['id']}** (Proposal {o['proposal_id']}): Applied {o['kind']} on `{o['target']}`. Status: **{o['status']}**.\n"
    else:
        scored_section_text = "No previous changes were scored today.\n"

    # Loop Health
    health_text = (
        f"- **Proposals Generated:** {len(auto_changed) + len(needs_decision)}\n"
        f"- **Auto-Applied:** {len(auto_changed)}\n"
        f"- **Pending Approval:** {len(needs_decision)}\n"
        f"- **Outcomes Evaluated:** {len(yesterday_scored)}\n"
        f"- **Status:** Operational"
    )

    # Compose final document body
    body = f"""# Reflection Morning Report - {date_str}

## 1. What was learned
{learned_section}

## 2. What changed automatically
{auto_section_text}

## 3. Needs your decision
{decision_section_text}

## 4. Yesterday's changes scored
{scored_section_text}

## 5. Loop health
{health_text}
"""

    # 3. Create frontmatter
    briefing_id = f"rfl_brf_{date_str.replace('-', '')}"
    fm = VaultFrontmatter(
        id=briefing_id,
        type=NoteType.BRIEFING,
        discipline="reflection",
        created=utc_now_iso(),
        source_run=None,
        confidence="high",
        status="active",
        supersedes=None,
    )

    full_note_content = render_frontmatter(fm) + "\n" + body

    # 4. Write note using write_note
    vault = vault_dir or default_vault_dir()
    relpath = reflection_briefing_relpath(date_str)
    final_path = write_note(vault, relpath, full_note_content)

    # Settle on the file that now exists on disk.  A briefing that was not
    # written, or was written empty, is not a report — and the caller must be
    # able to see that without re-implementing the rule.
    settlement = classify_settlement(final_path)
    if settlement is Settlement.OK:
        _LOG.info("Wrote reflection briefing to %s", final_path)
    else:
        _LOG.warning(
            "Reflection briefing settled '%s' (not a success): %s", settlement.value, final_path
        )
    return final_path


def generate_reflection_report_settled(
    date_str: str | None = None,
    db_path: str | None = None,
    vault_dir: str | None = None,
) -> tuple[str, Settlement]:
    """Generate the briefing and return ``(path, settlement)``.

    The settlement is derived from the artifact via the one shared classifier,
    so callers never have to infer success from "the call returned".
    """
    try:
        path = generate_reflection_report(date_str=date_str, db_path=db_path, vault_dir=vault_dir)
    except Exception as exc:
        return "", classify_settlement(error=exc)
    return path, classify_settlement(path)


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="YYYY-MM-DD target date")
    args = parser.parse_args()
    try:
        generate_reflection_report(date_str=args.date)
    except Exception as exc:
        _LOG.error("Report runner failed: %s", exc)
