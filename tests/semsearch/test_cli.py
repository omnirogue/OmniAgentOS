"""CLI exit codes and JSON envelopes."""

from __future__ import annotations

import json

import pytest

from omniagentos.semsearch import __main__ as cli
from omniagentos.semsearch.constants import MAX_QUERY_LENGTH, MAX_RESULT_COUNT
from omniagentos.semsearch.index import IndexStats
from omniagentos.semsearch.search import SemHit


def test_query_cli_prints_hits_and_exits_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        cli,
        "search",
        lambda query, kind, limit: [SemHit("skill", "release", "Release", 0.9, "semantic")],
    )
    assert cli.main(["release to prod", "--kind", "skill", "--limit", "4"]) == 0
    assert json.loads(capsys.readouterr().out) == [
        {
            "kind": "skill",
            "ref_id": "release",
            "score": 0.9,
            "source": "semantic",
            "title": "Release",
        }
    ]


def test_reindex_cli_success_and_failure_exit_codes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        cli,
        "reindex",
        lambda kinds: {"tool": IndexStats(scanned=3, embedded=2, skipped=1)},
    )
    assert cli.main(["reindex", "--kind", "tool"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "tool": {"embedded": 2, "scanned": 3, "skipped": 1}
    }

    def fail_reindex(kinds: object) -> object:
        raise RuntimeError("database offline")

    monkeypatch.setattr(cli, "reindex", fail_reindex)
    assert cli.main(["reindex"]) == 1
    assert "reindex failed: database offline" in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv",
    [
        ["x" * (MAX_QUERY_LENGTH + 1)],
        ["release", "--limit", str(MAX_RESULT_COUNT + 1)],
    ],
)
def test_cli_rejects_queries_and_limits_above_shared_bounds(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.main(argv)

    assert exc_info.value.code == 2
