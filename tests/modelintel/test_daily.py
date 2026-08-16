"""The daily update's operator log must not describe a FAILURE with a benign
reason. A corrupt/unreadable ~/.claude/fusion/model-rankings.json is not the
same event as "the file isn't there yet"."""

from __future__ import annotations

from pathlib import Path

import pytest

from omniagentos.modelintel import daily, vault_notes
from omniagentos.modelintel import registry as registry_mod


@pytest.fixture()
def isolated_daily(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("OMNIAGENTOS_MODELINTEL_DIR", str(tmp_path / "var"))
    monkeypatch.setenv("OMNIAGENTOS_MODELINTEL_RESEARCH", "0")
    monkeypatch.setattr(registry_mod, "FUSION_DIGEST", tmp_path / "model-intel.json")
    monkeypatch.setattr(daily, "fetch_all", lambda cfg: {})
    monkeypatch.setattr(vault_notes, "render_all", lambda cfg, registry: [])
    return tmp_path


def test_corrupt_rankings_file_is_not_logged_as_absent(
    isolated_daily: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rankings = isolated_daily / "model-rankings.json"
    rankings.write_text("{ this is not json", encoding="utf-8")
    monkeypatch.setattr(registry_mod, "FUSION_RANKINGS", rankings)

    summary = daily.run()

    assert "no model-rankings.json" not in summary["rankings"], summary["rankings"]
    assert "unreadable" in summary["rankings"] or "corrupt" in summary["rankings"], summary[
        "rankings"
    ]


def test_genuinely_absent_rankings_file_is_logged_as_absent(
    isolated_daily: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(registry_mod, "FUSION_RANKINGS", isolated_daily / "nope.json")

    summary = daily.run()

    assert "no model-rankings.json" in summary["rankings"], summary["rankings"]
