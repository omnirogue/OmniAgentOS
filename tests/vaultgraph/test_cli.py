from __future__ import annotations

from pathlib import Path

import pytest

from omniagentos.vaultgraph.cli import main


def test_cli_stats(fixture_vault: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["--vault", str(fixture_vault), "stats"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "nodes:" in out
    assert "edges:" in out


def test_cli_communities(fixture_vault: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["--vault", str(fixture_vault), "communities", "--method", "connected_components"])
    assert rc == 0
    assert "communities" in capsys.readouterr().out


def test_cli_local(fixture_vault: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["--vault", str(fixture_vault), "local", "model-a", "--hops", "1"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "hub" in out


def test_cli_moc_then_global(tmp_vault: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["--vault", str(tmp_vault), "moc", "--method", "connected_components"])
    assert rc == 0
    assert "wrote" in capsys.readouterr().out

    rc = main(["--vault", str(tmp_vault), "global", "composting"])
    assert rc == 0
    assert "Map of Content" in capsys.readouterr().out


def test_cli_classify(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["classify", "Model A supports streaming", "Model A does not support streaming"])
    assert rc == 0
    assert "contradiction" in capsys.readouterr().out


def test_cli_suggest(fixture_vault: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["--vault", str(fixture_vault), "suggest", "hub"])
    assert rc == 0
    assert "capability-speed" in capsys.readouterr().out
