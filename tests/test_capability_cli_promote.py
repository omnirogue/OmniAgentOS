"""The operator `promote-capability` CLI is the production caller of
`promote_to_estate` (so the redacting company->estate gate is reachable outside
tests). This pins that wiring without a live Postgres: the store, gate, ledger,
and promotion call are all stubbed; the assertion is that the parsed operator
arguments reach `promote_to_estate` unchanged and that promotion is refused
without an `--estate-statement`.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from omniagentos.knowledge import cli


class _FakeStore:
    def __enter__(self) -> _FakeStore:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def test_promote_capability_cli_calls_promote_to_estate_with_operator_args(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: dict[str, Any] = {}

    monkeypatch.setattr(cli, "admin_dsn", lambda: "postgresql://admin/x")
    monkeypatch.setattr(cli, "KnowledgeStore", lambda **_: _FakeStore())
    monkeypatch.setattr(cli, "gate", lambda: "GATE-SENTINEL")
    monkeypatch.setattr(cli, "SqliteStore", lambda _dsn: "LEDGER-SENTINEL")

    def _fake_promote(store: object, fact_id: int, **kwargs: Any) -> SimpleNamespace:
        calls["fact_id"] = fact_id
        calls.update(kwargs)
        return SimpleNamespace(id=777)

    monkeypatch.setattr(cli, "promote_to_estate", _fake_promote)

    rc = cli.main(
        [
            "promote-capability",
            "42",
            "--estate-statement",
            "Tool X does audio+video synthesis",
            "--actor",
            "operator:owner",
            "--vault-dir",
            "/tmp/vault",
        ]
    )

    assert rc == 0
    assert calls["fact_id"] == 42
    assert calls["estate_statement"] == "Tool X does audio+video synthesis"
    assert calls["actor"] == "operator:owner"
    assert calls["vault_dir"] == "/tmp/vault"
    assert calls["promotion_gate"] == "GATE-SENTINEL"
    assert calls["ledger_store"] == "LEDGER-SENTINEL"
    assert "estate note 777" in capsys.readouterr().out


def test_promote_capability_requires_estate_statement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "admin_dsn", lambda: "postgresql://admin/x")
    # argparse enforces --estate-statement as required → SystemExit(2), before any
    # store/gate work: the operator cannot promote without a clean rewrite.
    with pytest.raises(SystemExit) as exc:
        cli.main(["promote-capability", "42", "--actor", "operator:owner"])
    assert exc.value.code == 2
