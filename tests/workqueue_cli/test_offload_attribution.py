"""`wq enqueue --by <person>` through to the OFFLOADS block in `wq status`.

the operator, 2026-08-11: "we should be aware when one of us is offloading or has a
pending job on a computer." That is one path with three links — the flag, the
stored column, the rendered line — and it is worth testing end to end because a
break in any one of them looks exactly like "nobody is using the pool".
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

import omniagentos.workqueue.cli as cli
from omniagentos.workqueue.store import WorkQueueStore

SUBMIT = {
    "idempotency_key": "offload-1",
    "repo_url": "https://example.invalid/repo.git",
    "repo_slug": "repo",
    "base_sha": "a" * 40,
    "branch": "wq/offload",
    "owned_paths": ["demo/**"],
    "agent_profile": "script",
    "acceptance_cmd": "python3 -c 'print(1)'",
    "risk_class": "mechanical",
}


@pytest.fixture
def wq(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[WorkQueueStore]:
    store = WorkQueueStore(str(tmp_path / "wq.sqlite3"))
    monkeypatch.setattr(cli, "open_queue", lambda server, db: store)
    try:
        yield store
    finally:
        store.close()


def _run(*argv: str) -> int:
    return cli.main(["--db", "/tmp/ignored.sqlite3", *argv])


def _unit(key: str, **overrides) -> str:
    return json.dumps({**SUBMIT, "idempotency_key": key, **overrides})


def _ids(store: WorkQueueStore) -> list[str]:
    import sqlite3

    with sqlite3.connect(store.db_path) as conn:
        return [str(row[0]) for row in conn.execute("SELECT id FROM wq_units ORDER BY id")]


def test_by_flag_attributes_the_unit_and_status_shows_the_person(wq, capsys) -> None:
    assert _run("enqueue", "--json", _unit("owner-1"), "--by", "owner") == 0
    assert _run("enqueue", "--json", _unit("owner-2"), "--by", "owner") == 0
    assert _run("enqueue", "--json", _unit("alice-1"), "--by", "alice") == 0
    capsys.readouterr()

    # Two of owner's units go in flight on two different boxes.
    assert wq.claim("mw0001-owner", "w1", []) is not None
    assert wq.claim("mw0002", "w1", []) is not None

    offloads = {row["person"]: row for row in wq.status()["offloads"]}
    assert offloads["owner"]["running"] + offloads["owner"]["queued"] == 2
    assert offloads["alice"]["queued"] + offloads["alice"]["running"] == 1
    machines = sorted(offloads["owner"]["machines"] + offloads["alice"]["machines"])
    assert machines == ["mw0001-owner", "mw0002"]

    assert _run("status") == 0
    rendered = capsys.readouterr().out
    assert "OFFLOADS" in rendered
    assert "owner:" in rendered and "alice:" in rendered


def test_wq_user_is_the_default_and_the_payload_still_wins(wq, monkeypatch, capsys) -> None:
    monkeypatch.setenv("WQ_USER", "bob")
    assert _run("enqueue", "--json", _unit("env-default")) == 0
    # An orchestrator writing JSONL attributes on someone else's behalf, and
    # that explicit value must not be overwritten by whoever ran the command.
    assert _run("enqueue", "--json", _unit("explicit", submitted_by="alice"), "--by", "owner") == 0
    capsys.readouterr()

    people = {row["person"] for row in wq.status()["offloads"]}
    assert people == {"bob", "alice"}


def test_unattributed_work_is_shown_not_dropped(wq, monkeypatch, capsys) -> None:
    monkeypatch.delenv("WQ_USER", raising=False)
    assert _run("enqueue", "--json", _unit("anon")) == 0
    capsys.readouterr()

    (row,) = wq.status()["offloads"]
    assert row["person"] == "(unattributed)"
    # Stored as empty, RENDERED as a name: one spelling of "nobody" in the DB.
    assert wq.get_unit(_ids(wq)[0])["submitted_by"] == ""

    assert _run("status") == 0
    assert "(unattributed): 1 queued" in capsys.readouterr().out


def test_render_puts_each_person_on_one_line() -> None:
    text = cli.render_status(
        {
            "machines": [],
            "depth": {},
            "offloads": [
                {
                    "person": "owner",
                    "queued": 3,
                    "running": 2,
                    "in_review": 0,
                    "machines": ["mw0001-owner", "mw0002"],
                },
                {"person": "alice", "queued": 0, "running": 0, "in_review": 1, "machines": []},
            ],
        }
    )
    assert "owner: 2 running (mw0001-owner, mw0002) · 3 queued" in text
    assert "alice: 1 in review" in text


def test_submit_names_the_resubmitter_without_stealing_the_unit(wq, capsys) -> None:
    assert _run("enqueue", "--json", _unit("parked-one"), "--by", "alice") == 0
    unit_id = _ids(wq)[0]
    wq.park(unit_id, "attempts-exhausted", "fix the named cause")
    capsys.readouterr()

    assert _run("submit", "--unit", unit_id, "--because", "fixed it", "--by", "owner") == 0
    out = capsys.readouterr().out
    assert "re-queued by owner" in out
    # The requester still owns it: re-attributing on resubmit would erase them
    # from every status block the moment somebody else helped out.
    assert wq.get_unit(unit_id)["submitted_by"] == "alice"
    assert {row["person"] for row in wq.status()["offloads"]} == {"alice"}
