from __future__ import annotations

from pathlib import Path

from omniagentos.steward.config import StewardConfig, load_steward_config


def test_model_defaults_are_independent() -> None:
    first = StewardConfig()
    second = StewardConfig()
    first.alerts.vip_senders.append("vip@example.com")
    assert second.alerts.vip_senders == []
    assert first.briefing.hour == 7
    assert first.alerts.urgent_patterns == ["urgent", "asap", "immediately"]
    assert first.comms.inbound_max_bytes == 262144
    assert first.curation.batch_limit == 50
    assert first.autonomy.rung == 1
    assert first.voice.default_provider == "elevenlabs"


def test_load_default_and_custom_file(tmp_path: Path) -> None:
    shipped = load_steward_config()
    assert shipped.comms.sources["zapier"].secret_env == "COMMS_WEBHOOK_SECRET_ZAPIER"
    path = tmp_path / "steward.yaml"
    path.write_text("briefing:\n  hour: 9\nautonomy:\n  rung: 2\n", encoding="utf-8")
    loaded = load_steward_config(str(path))
    assert loaded.briefing.hour == 9
    assert loaded.briefing.minute == 30
    assert loaded.autonomy.rung == 2


def test_missing_file_returns_all_defaults(tmp_path: Path) -> None:
    loaded = load_steward_config(str(tmp_path / "missing.yaml"))
    assert loaded == StewardConfig()
