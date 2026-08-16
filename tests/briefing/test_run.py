from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest

from omniagentos.briefing.run import run_briefing
from omniagentos.contracts import Events
from omniagentos.db.store import SqliteStore
from omniagentos.steward.config import BriefingConfig, StewardConfig
from omniagentos.steward.notify import NotifyResult
from omniagentos.steward.store import StewardStore


def test_dry_run_has_no_pipeline_writes(
    sqlite_store: SqliteStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIAGENTOS_KNOWLEDGE", "0")
    monkeypatch.setattr(
        "omniagentos.briefing.run.write_note",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("vault write")),
    )
    cfg = StewardConfig(briefing=BriefingConfig(deliver_slack=True, voice_provider="elevenlabs"))
    result = run_briefing(
        dry_run=True,
        database=sqlite_store,
        cfg=cfg,
        vault_dir=str(tmp_path / "vault"),
    )
    assert result["headline"] == "Nothing to report"
    assert StewardStore(sqlite_store).list_briefings() == []
    assert sqlite_store.get_events_after(0, [Events.BRIEFING_READY]) == []


def test_full_run_writes_note_row_and_delivery_results(
    sqlite_store: SqliteStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIAGENTOS_KNOWLEDGE", "0")
    monkeypatch.setattr(
        "omniagentos.briefing.run.send_piedpiper_email",
        lambda *args, **kwargs: NotifyResult(True, "piedpiper_email", "sent", "piedpiper-msg-123"),
    )
    monkeypatch.setattr(
        "omniagentos.briefing.run.send_slack",
        lambda *args, **kwargs: NotifyResult(False, "slack", "not configured"),
    )

    def voice(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"ok": False, "status": "no_key", "detail": "missing key"}

    monkeypatch.setattr("omniagentos.voice.service.synthesize_to_artifact", voice)
    vault = tmp_path / "vault"
    cfg = StewardConfig(
        briefing=BriefingConfig(
            deliver_email="owner@example.com",
            deliver_slack=True,
            voice_provider="elevenlabs",
        )
    )
    target = date(2026, 7, 13)
    result = run_briefing(
        database=sqlite_store,
        cfg=cfg,
        vault_dir=str(vault),
        briefing_date=target,
    )

    assert result["composed_by"] == "deterministic"
    note = vault / "briefings/2026/07/13.md"
    assert note.is_file()
    content = note.read_text(encoding="utf-8")
    assert "type: briefing" in content
    assert "discipline: steward" in content
    assert "## Deliveries" in content
    assert "voice: skipped" in content
    row = StewardStore(sqlite_store).latest_briefing()
    assert row is not None
    assert row["summary"] == result
    assert [item["channel"] for item in row["deliveries"]] == [
        "piedpiper_email",
        "slack",
        "voice",
    ]
    # The provider-assigned message id from the send path is captured in the
    # structured result and persisted — not discarded in favour of a
    # placeholder literal.
    piedpiper_delivery = next(item for item in row["deliveries"] if item["channel"] == "piedpiper_email")
    assert piedpiper_delivery["provider_id"] == "piedpiper-msg-123"
    event = sqlite_store.get_events_after(0, [Events.BRIEFING_READY])
    assert len(event) == 1 and event[0]["target_id"] == row["id"]


def test_briefing_same_date_rerun_is_idempotent(
    sqlite_store: SqliteStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # H2/INT-003: re-running the pipeline for the same date (scheduled job + manual
    # Generate, or a retry after a delivery failure) must NOT raise a UNIQUE
    # violation and must leave exactly one briefings row + the note in place.
    monkeypatch.setenv("OMNIAGENTOS_KNOWLEDGE", "0")
    monkeypatch.setattr(
        "omniagentos.briefing.run.send_slack",
        lambda *args, **kwargs: NotifyResult(False, "slack", "not configured"),
    )
    monkeypatch.setattr(
        "omniagentos.voice.service.synthesize_to_artifact",
        lambda *args, **kwargs: {"ok": False, "status": "no_key", "detail": "missing key"},
    )
    vault = tmp_path / "vault"
    cfg = StewardConfig(briefing=BriefingConfig(deliver_slack=True))
    target = date(2026, 7, 13)

    first = run_briefing(database=sqlite_store, cfg=cfg, vault_dir=str(vault), briefing_date=target)
    # Same date again — the assertion is simply that this does not raise.
    second = run_briefing(
        database=sqlite_store, cfg=cfg, vault_dir=str(vault), briefing_date=target
    )
    assert first["subject"] == second["subject"]

    store = StewardStore(sqlite_store)
    same_date = [b for b in store.list_briefings() if b["briefing_date"] == "2026-07-13"]
    assert len(same_date) == 1  # upsert, not a duplicate row
    assert (vault / "briefings/2026/07/13.md").is_file()
