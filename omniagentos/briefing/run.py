"""Run, persist, and deliver the daily Steward briefing."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import date
from html import escape
from typing import Any

from omniagentos.briefing.compose import compose
from omniagentos.briefing.gather import gather
from omniagentos.contracts import (
    Events,
    NoteType,
    VaultFrontmatter,
    default_db_path,
    default_vault_dir,
    utc_now_iso,
)
from omniagentos.db.store import SqliteStore
from omniagentos.steward.config import StewardConfig, load_steward_config
from omniagentos.steward.notify import NotifyResult, send_piedpiper_email, send_slack
from omniagentos.steward.store import StewardStore
from omniagentos.vault.frontmatter import render_frontmatter
from omniagentos.vault.write import write_note


def _html(composed: dict[str, Any]) -> str:
    sections = "".join(
        f"<h2>{escape(str(section['title']))}</h2>"
        f"<p>{escape(str(section['body'])).replace(chr(10), '<br>')}</p>"
        for section in composed["sections"]
    )
    return f"<h1>{escape(str(composed['headline']))}</h1>{sections}"


def _notify_row(result: NotifyResult) -> dict[str, Any]:
    return asdict(result)


def _deliver(
    composed: dict[str, Any],
    cfg: StewardConfig,
    database: SqliteStore,
    vault_dir: str,
) -> tuple[list[dict[str, Any]], str | None]:
    deliveries: list[dict[str, Any]] = []
    if cfg.briefing.deliver_email:
        try:
            deliveries.append(
                _notify_row(
                    send_piedpiper_email(
                        cfg.briefing.deliver_email,
                        str(composed["subject"]),
                        _html(composed),
                    )
                )
            )
        except Exception as exc:
            deliveries.append({"ok": False, "channel": "piedpiper_email", "detail": str(exc)})

    if cfg.briefing.deliver_slack:
        first = composed["sections"][0]
        slack_text = f"{composed['headline']}\n\n{first['title']}\n{first['body']}"
        try:
            deliveries.append(_notify_row(send_slack(slack_text)))
        except Exception as exc:
            deliveries.append({"ok": False, "channel": "slack", "detail": str(exc)})

    voice_artifact_id: str | None = None
    if cfg.briefing.voice_provider:
        first = composed["sections"][0]
        voice_text = f"{composed['headline']} {first['body']}"
        try:
            from omniagentos.voice.service import synthesize_to_artifact

            voice = synthesize_to_artifact(
                voice_text,
                provider=cfg.briefing.voice_provider,
                store=database,
                cfg=cfg,
                vault_dir=vault_dir,
            )
            ok = bool(voice.get("ok"))
            if ok and voice.get("artifact_id"):
                voice_artifact_id = str(voice["artifact_id"])
            detail = str(voice.get("detail") or voice.get("status") or "")
            deliveries.append(
                {
                    "ok": ok,
                    "channel": "voice",
                    "detail": detail if ok else f"skipped: {detail or 'synthesis failed'}",
                    **({"artifact_id": voice_artifact_id} if voice_artifact_id else {}),
                }
            )
        except Exception as exc:
            deliveries.append({"ok": False, "channel": "voice", "detail": f"skipped: {exc}"})
    return deliveries, voice_artifact_id


def _render_note(
    briefing_date: date, composed: dict[str, Any], deliveries: list[dict[str, Any]]
) -> str:
    briefing_id = f"brf_{briefing_date.isoformat()}"
    frontmatter = VaultFrontmatter(
        id=briefing_id,
        type=NoteType.BRIEFING,
        discipline="steward",
        created=utc_now_iso(),
        source_run=None,
        confidence="high" if composed["composed_by"] == "llm" else "medium",
        status="active",
        supersedes=None,
    )
    lines = [
        render_frontmatter(frontmatter),
        f"# {composed['subject']}",
        "",
        str(composed["headline"]),
        "",
    ]
    for section in composed["sections"]:
        lines.extend([f"## {section['title']}", "", str(section["body"]), ""])
    lines.extend(["## Deliveries", ""])
    if deliveries:
        for delivery in deliveries:
            channel = str(delivery.get("channel") or "unknown")
            detail = str(delivery.get("detail") or ("sent" if delivery.get("ok") else "failed"))
            lines.append(f"- {channel}: {detail}")
    else:
        lines.append("- No delivery channels configured.")
    lines.append("")
    return "\n".join(lines)


def _record_deliveries(
    database: SqliteStore,
    briefing_id: str,
    deliveries: list[dict[str, Any]],
    voice_artifact_id: str | None,
) -> None:
    """Fill the delivery fields after the provisional briefing upsert.

    StewardStore intentionally has no briefing-update accessor yet. Keep this
    concrete SQLite write isolated here so delivery happens after persistence
    while the final row still records every best-effort result.
    """
    database._write(
        "UPDATE briefings SET deliveries_json = ?, voice_artifact_id = ? WHERE id = ?",
        (
            json.dumps(deliveries, separators=(",", ":"), sort_keys=True, default=str),
            voice_artifact_id,
            briefing_id,
        ),
    )


def run_briefing(
    *,
    dry_run: bool = False,
    briefing_date: date | None = None,
    database: SqliteStore | None = None,
    cfg: StewardConfig | None = None,
    vault_dir: str | None = None,
) -> dict[str, Any]:
    """Execute the pipeline and return the composed briefing.

    H2 + DATA-003 + INT-003: Uses upsert_briefing to ensure idempotency.
    Order: compute → upsert row → write note, so a re-run doesn't fail.
    """
    target_date = briefing_date or date.today()
    sqlite = database or SqliteStore(default_db_path())
    config = cfg or load_steward_config()
    steward = StewardStore(sqlite)
    gathered = gather(steward, sqlite, config, date=target_date)
    composed = compose(gathered, config)
    if dry_run:
        return composed

    vault = vault_dir or default_vault_dir()
    relpath = f"briefings/{target_date.year:04d}/{target_date.month:02d}/{target_date.day:02d}.md"

    # H2: Upsert the briefing row BEFORE writing the note (order: compute → upsert → write note).
    # This ensures a re-run doesn't fail with a duplicate key error.
    row = steward.upsert_briefing(
        {
            "briefing_date": target_date.isoformat(),
            "vault_path": "",  # Will be updated below
            "summary": composed,
            "deliveries": [],
            "voice_artifact_id": None,
        }
    )

    # Now that the row is safely persisted, write the note and get its final path.
    vault_path = write_note(vault, relpath, _render_note(target_date, composed, []))

    # Update the vault_path in the row.
    sqlite._write(
        "UPDATE briefings SET vault_path = ? WHERE id = ?",
        (vault_path, str(row["id"])),
    )

    deliveries, voice_artifact_id = _deliver(composed, config, sqlite, vault)
    _record_deliveries(sqlite, str(row["id"]), deliveries, voice_artifact_id)
    write_note(vault, relpath, _render_note(target_date, composed, deliveries))
    sqlite.insert_event(
        Events.BRIEFING_READY,
        "scheduler",
        "briefing.ready",
        target_type="briefing",
        target_id=str(row["id"]),
        payload={
            "briefing_date": target_date.isoformat(),
            "headline": composed["headline"],
            "vault_path": vault_path,
        },
    )
    return composed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="compose without writes or delivery")
    parser.add_argument("--date", help="briefing date in YYYY-MM-DD format")
    args = parser.parse_args(argv)
    target = date.fromisoformat(args.date) if args.date else None
    result = run_briefing(dry_run=args.dry_run, briefing_date=target)
    print(json.dumps(result, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main", "run_briefing"]
