"""Lane C acceptance: graduation reaches the production recall front door.

The decisive test deliberately does not import the rendering/search helpers it
verifies. It posts through the ASGI app, reads the resulting file as plain text,
and calls :func:`omniagentos.retrieval.recall.recall`.

Named counterfeits killed here:

* ``counterfeit_lesson_row_without_render``
* ``counterfeit_recall_reads_db_not_rendered``
* ``counterfeit_render_drops_the_status_filter``
* ``counterfeit_render_single_lesson``
* ``counterfeit_memlife_leg_registered_not_defaulted``
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from omniagentos.api.deps import get_store
from omniagentos.api.main import app
from omniagentos.contracts import utc_now_iso
from omniagentos.db.migrate import migrate
from omniagentos.db.store import SqliteStore
from omniagentos.retrieval.recall import recall

_BEGIN = "<!-- omniagentos:memlife:lessons:begin -->"
_END = "<!-- omniagentos:memlife:lessons:end -->"


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "memlife.db"
    assert migrate(str(path)) >= 90
    monkeypatch.setenv("OMNIAGENTOS_DB", str(path))
    return path


@pytest.fixture
def store(db_path: Path) -> Iterator[SqliteStore]:
    sqlite = SqliteStore(str(db_path))
    try:
        yield sqlite
    finally:
        sqlite.close()


@pytest.fixture
def store_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    memories = tmp_path / "memories"
    root = memories / "memlife"
    root.mkdir(parents=True)
    monkeypatch.setenv("OMNIAGENTOS_MEMLIFE_STORE", str(root))
    monkeypatch.setenv("OMNIAGENTOS_MEMORIES_DIR", str(memories))
    monkeypatch.setenv("OMNIAGENTOS_KNOWLEDGE", "0")
    return root


@pytest.fixture
def client(
    store: SqliteStore,
    store_root: Path,
) -> Iterator[httpx.AsyncClient]:
    assert store_root.is_dir()
    app.dependency_overrides[get_store] = lambda: store
    http = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://testserver",
    )
    try:
        yield http
    finally:
        app.dependency_overrides.clear()


def _insert_candidate(
    store: SqliteStore,
    *,
    candidate_id: str,
    claim: str,
    status: str = "staged",
) -> None:
    now = utc_now_iso()
    with store._lock:
        store._connection.execute(
            """
            INSERT INTO memlife_candidates (
                id, key, claim, conditions, evidence_ids_json, cluster_size,
                status, rejection_count, salience, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                candidate_id,
                f"key/{candidate_id}",
                claim,
                "during production verification",
                '["evidence-1"]',
                1,
                status,
                0,
                None,
                now,
                now,
            ),
        )
        store._connection.execute(
            """
            INSERT INTO memlife_decisions (id, candidate_id, action, at, actor, reason)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                f"dec_{candidate_id}_stage",
                candidate_id,
                "stage",
                now,
                "dream-cycle",
                "",
            ),
        )


def _seed_provisional_lesson(
    store: SqliteStore,
    store_root: Path,
    *,
    candidate_id: str,
    claim: str,
) -> None:
    """Seed both system-of-record and working mirror with an unaccepted lesson."""
    _insert_candidate(
        store,
        candidate_id=candidate_id,
        claim=claim,
        status="graduated",
    )
    now = utc_now_iso()
    lesson_id = f"lesson_{candidate_id}"
    with store._lock:
        store._connection.execute(
            """
            INSERT INTO memlife_lessons (
                id, candidate_id, claim, conditions, status,
                graduated_at, graduated_by, evidence_ids_json, provenance_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                lesson_id,
                candidate_id,
                claim,
                "",
                "provisional",
                now,
                "test",
                "[]",
                '{"source":"test"}',
                now,
            ),
        )

    lessons_dir = store_root / "lessons"
    lessons_dir.mkdir()
    (lessons_dir / f"{lesson_id}.json").write_text(
        json.dumps(
            {
                "id": lesson_id,
                "candidate_id": candidate_id,
                "claim": claim,
                "conditions": "",
                "status": "provisional",
                "graduated_at": now,
                "graduated_by": "test",
                "evidence_ids": [],
                "provenance": {"source": "test"},
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _graduate(client: httpx.AsyncClient, candidate_id: str) -> httpx.Response:
    return _run(
        client.post(
            f"/api/memlife/{candidate_id}/graduate",
            json={"decided_by": "owner", "reason": "verified"},
        )
    )


def _sentinel_block(text: str) -> str:
    begin = text.index(_BEGIN) + len(_BEGIN)
    end = text.index(_END)
    assert begin < end
    return text[begin:end]


def test_graduated_claim_is_recallable_through_the_front_door(
    client: httpx.AsyncClient,
    store: SqliteStore,
    store_root: Path,
) -> None:
    claim = "Cerulean kestrels verify lunar checksum ledgers before archival."
    _insert_candidate(store, candidate_id="cand_front_door", claim=claim)

    response = _graduate(client, "cand_front_door")

    assert response.status_code == 200, response.text
    lessons_path = store_root / "LESSONS.md"
    assert lessons_path.is_file()
    assert claim in _sentinel_block(lessons_path.read_text(encoding="utf-8"))

    # Decisive assertion: no sources override, so this proves memlife is a
    # production default rather than merely an opt-in registered backend.
    lines = recall("kestrels checksum archival", top_k=8)
    assert any(line.source == "memlife" and claim in line.text for line in lines)

    # Secondary assertion isolates the leg only after the default path passed.
    isolated = recall(
        "kestrels checksum archival",
        top_k=8,
        sources=("memlife",),
    )
    assert any(line.source == "memlife" and claim in line.text for line in isolated)


def test_provisional_lesson_never_reaches_recall(
    client: httpx.AsyncClient,
    store: SqliteStore,
    store_root: Path,
) -> None:
    provisional = "Provisional topaz otters may bypass all checksum gates."
    accepted = "Accepted amber falcons preserve signed audit receipts."
    _seed_provisional_lesson(
        store,
        store_root,
        candidate_id="cand_provisional",
        claim=provisional,
    )
    _insert_candidate(store, candidate_id="cand_accepted", claim=accepted)

    response = _graduate(client, "cand_accepted")

    assert response.status_code == 200, response.text
    rendered = (store_root / "LESSONS.md").read_text(encoding="utf-8")
    assert accepted in _sentinel_block(rendered)
    assert provisional not in rendered
    assert all(provisional not in line.text for line in recall("topaz otters checksum", top_k=8))


def test_graduation_failure_leaves_no_lesson_row(
    client: httpx.AsyncClient,
    store: SqliteStore,
    store_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claim = "Indigo herons retain rollback evidence across failed renders."
    _insert_candidate(store, candidate_id="cand_rollback", claim=claim)

    def fail_render(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise OSError("counterfeit render outage")

    monkeypatch.setattr(
        "omniagentos.api.routes.memlife.render_lessons",
        fail_render,
    )

    response = _graduate(client, "cand_rollback")

    assert response.status_code == 500, response.text
    with store._lock:
        candidate = store._connection.execute(
            "SELECT status FROM memlife_candidates WHERE id = ?",
            ("cand_rollback",),
        ).fetchone()
        lesson_count = store._connection.execute(
            "SELECT COUNT(*) AS n FROM memlife_lessons"
        ).fetchone()
        graduate_count = store._connection.execute(
            """
            SELECT COUNT(*) AS n FROM memlife_decisions
            WHERE candidate_id = ? AND action = 'graduate'
            """,
            ("cand_rollback",),
        ).fetchone()

    assert candidate["status"] == "staged"
    assert int(lesson_count["n"]) == 0
    assert int(graduate_count["n"]) == 0
    assert not list((store_root / "lessons").glob("*.json"))
    assert not (store_root / "LESSONS.md").exists()


def test_graduation_failure_restores_prior_rendered_content(
    client: httpx.AsyncClient,
    store: SqliteStore,
    store_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prior LESSONS.md bytes must be restored when commit fails after render.

    The empty-file failure path only proves unlink-on-rollback. This test seeds
    a successful prior render, then forces failure *after* the new render writes
    (db._commit raises), so the restore branch is the only path that keeps the
    old content. Disabling that restore must turn this red.
    """
    prior_claim = "Crimson plovers archive prior rendered checksums at dusk."
    failed_claim = "Emerald plovers overwrite rendered checksums at dawn."
    _insert_candidate(store, candidate_id="cand_prior_render", claim=prior_claim)
    prior_response = _graduate(client, "cand_prior_render")
    assert prior_response.status_code == 200, prior_response.text

    rendered = store_root / "LESSONS.md"
    assert rendered.is_file()
    prior_bytes = rendered.read_text(encoding="utf-8")
    assert prior_claim in prior_bytes

    _insert_candidate(store, candidate_id="cand_restore_fail", claim=failed_claim)

    def fail_commit(self: Any) -> None:
        del self
        raise sqlite3.OperationalError("counterfeit commit outage after render")

    monkeypatch.setattr(store.__class__, "_commit", fail_commit)

    response = _graduate(client, "cand_restore_fail")

    assert response.status_code == 500, response.text
    restored = rendered.read_text(encoding="utf-8")
    assert restored == prior_bytes
    assert prior_claim in restored
    assert failed_claim not in restored

    with store._lock:
        failed_row = store._connection.execute(
            "SELECT status FROM memlife_candidates WHERE id = ?",
            ("cand_restore_fail",),
        ).fetchone()
        prior_row = store._connection.execute(
            "SELECT status FROM memlife_candidates WHERE id = ?",
            ("cand_prior_render",),
        ).fetchone()
        failed_lessons = store._connection.execute(
            "SELECT COUNT(*) AS n FROM memlife_lessons WHERE candidate_id = ?",
            ("cand_restore_fail",),
        ).fetchone()

    assert failed_row["status"] == "staged"
    assert prior_row["status"] == "graduated"
    assert int(failed_lessons["n"]) == 0
    assert not list(
        p
        for p in (store_root / "lessons").glob("*.json")
        if failed_claim in p.read_text(encoding="utf-8")
    )


def test_recall_survives_a_missing_lessons_file(store_root: Path) -> None:
    assert not (store_root / "LESSONS.md").exists()

    lines = recall("anything", top_k=8)

    assert all(line.source != "memlife" for line in lines)


def test_recall_follows_the_rendered_file(
    client: httpx.AsyncClient,
    store: SqliteStore,
    store_root: Path,
) -> None:
    claim = "Magenta badgers reconcile quartz indexes after midnight."
    _insert_candidate(store, candidate_id="cand_render_truth", claim=claim)
    response = _graduate(client, "cand_render_truth")
    assert response.status_code == 200, response.text

    rendered = store_root / "LESSONS.md"
    assert claim in rendered.read_text(encoding="utf-8")
    rendered.write_text("", encoding="utf-8")

    assert all(
        not (line.source == "memlife" and claim in line.text)
        for line in recall("badgers quartz midnight", top_k=8)
    )


def test_second_graduation_preserves_first_lesson(
    client: httpx.AsyncClient,
    store: SqliteStore,
    store_root: Path,
) -> None:
    first = "Azure narwhals calibrate copper beacons before sunrise."
    second = "Violet narwhals calibrate silver beacons after sunset."
    _insert_candidate(store, candidate_id="cand_first", claim=first)
    _insert_candidate(store, candidate_id="cand_second", claim=second)

    first_response = _graduate(client, "cand_first")
    second_response = _graduate(client, "cand_second")

    assert first_response.status_code == 200, first_response.text
    assert second_response.status_code == 200, second_response.text
    rendered = (store_root / "LESSONS.md").read_text(encoding="utf-8")
    block = _sentinel_block(rendered)
    assert first in block
    assert second in block

    recalled = recall(
        "narwhals calibrate beacons",
        top_k=8,
        sources=("memlife",),
    )
    texts = {line.text for line in recalled}
    assert first in texts
    assert second in texts
