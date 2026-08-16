"""Connection-free regression coverage for injection e2e DSN selection."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import psycopg
from psycopg.conninfo import conninfo_to_dict


def _injection_e2e_module():
    path = Path(__file__).with_name("test_injection_e2e.py")
    spec = importlib.util.spec_from_file_location("injection_e2e_dsn_unit", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_role_dsn_parses_keyword_conninfo_without_connecting(monkeypatch) -> None:
    """Keyword conninfo retains its database name without opening a connection."""

    def fail_connect(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("this DSN resolver must not connect")

    monkeypatch.setenv(
        "OMNIAGENTOS_KNOWLEDGE_TEST_DSN",
        "dbname=injection_keyword_db host=localhost port=5432",
    )
    monkeypatch.setattr(psycopg, "connect", fail_connect)

    role_dsn = _injection_e2e_module()._role_dsn("knowledge_agent")

    assert conninfo_to_dict(role_dsn).get("dbname") == "injection_keyword_db"
