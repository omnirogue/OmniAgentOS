from __future__ import annotations

from pathlib import Path

from omniagentos.contracts import HarnessType, RunState
from omniagentos.wrapper import import_improve_tsv

FIXTURES = Path(__file__).parent / "fixtures"


def test_import_improve_tsv_maps_each_row() -> None:
    manifests = import_improve_tsv(str(FIXTURES / "results.tsv"))

    assert len(manifests) == 2
    first, second = manifests
    assert first.harness.harness is HarnessType.IMPROVE
    assert first.model == "gpt-5"
    assert first.state is RunState.COMPLETED
    assert first.receipts == []
    assert first.usage is not None
    assert first.usage.source == "imported"
    assert first.usage.estimated is True
    assert first.usage.cost_usd is None
    assert first.extra == {
        "wave": "1",
        "exp_id": "exp-fast",
        "model": "gpt-5",
        "metric": "pass_rate",
        "baseline": "0.70",
        "delta": "0.10",
        "status": "completed",
        "description": "Faster prompt",
        "commit": "abc123",
    }
    assert second.state is RunState.FAILED


def test_import_improve_tsv_missing_file_warns_and_returns_empty(
    caplog: object, tmp_path: Path
) -> None:
    assert import_improve_tsv(str(tmp_path / "missing.tsv")) == []
    assert "does not exist" in caplog.text  # type: ignore[attr-defined]


def test_import_improve_tsv_missing_columns_degrades_to_none(
    tmp_path: Path, caplog: object
) -> None:
    results = tmp_path / "partial.tsv"
    results.write_text("experiment\tmodel\nrenamed\tgpt-5\n", encoding="utf-8")

    manifests = import_improve_tsv(str(results))

    assert len(manifests) == 1
    assert manifests[0].model == "gpt-5"
    assert manifests[0].extra["metric"] is None
    assert "missing columns" in caplog.text  # type: ignore[attr-defined]
