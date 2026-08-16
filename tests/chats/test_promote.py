"""Chat-to-board promotion: item identity, provenance, context, and workfs.

DOCTRINE FOR THIS FILE — the promotion tests that matter run the REAL
``_extract_action_items``. An earlier round of this lane mocked the extractor in
every test, so the suite stayed green while the primary path collapsed three
distinct asks into one card: the mock supplied the ``source_message_index`` the
extractor never produced. Only the LLM transport (``ShortCallClient.complete``)
and ``dispatch_spec`` are faked here; the extraction and identity code under test
is the code that ships.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import subprocess
import sys
import threading
import unicodedata
from pathlib import Path
from typing import Any

import httpx
import pytest

from omniagentos.api.main import app
from omniagentos.api.routes.chats import _promoted_item_key
from omniagentos.chats import ChatStore
from omniagentos.collab.contracts import BoardTask
from omniagentos.collab.store import CollabStore
from omniagentos.contracts import utc_now_iso
from omniagentos.conversations.store import ConversationStore
from omniagentos.db.store import SqliteStore
from omniagentos.projects import ProjectStore
from omniagentos.workfs import EnsureResult


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _dispatch_onto(collab_store: CollabStore, spec: Any, board_task_id: str | None) -> str:
    """Stand in for dispatch_spec's card handling: reuse the caller's card, or make one.

    The real ``dispatch_spec`` updates the pre-created card when ``board_task_id``
    is supplied (intake/service.py) and only creates one when it is not; the fake
    has to do the same or the promote path's reservation would be invisible to it.
    """
    if board_task_id:
        collab_store.update_board_task(
            board_task_id,
            {"title": spec.title, "description": spec.description, "status": "open"},
        )
        return board_task_id
    task = BoardTask(title=spec.title, description=spec.description)
    collab_store.create_board_task(task)
    return task.id


def _fake_dispatch(calls: list[dict[str, Any]] | None = None) -> Any:
    def dispatch(
        *, collab_store: CollabStore, spec: Any, board_task_id: str | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        if calls is not None:
            calls.append({"board_task_id": board_task_id, **kwargs})
        task_id = _dispatch_onto(collab_store, spec, board_task_id)
        return {
            "task_id": task_id,
            "board_task": collab_store.get_board_task(task_id),
        }

    return dispatch


def _no_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the documented heuristic branch of the REAL extractor.

    This fakes the network boundary, not the unit under test: the extractor, its
    per-message index derivation and the identity code all run for real.
    """

    def unavailable(*_args: Any, **_kwargs: Any) -> str:
        raise RuntimeError("no LLM available in tests")

    monkeypatch.setattr("omniagentos.llm.client.ShortCallClient.complete", unavailable)


def _llm_returns(monkeypatch: pytest.MonkeyPatch, payloads: list[Any]) -> None:
    """Drive the REAL extractor's LLM branch with canned model output."""
    remaining = list(payloads)

    def complete(*_args: Any, **_kwargs: Any) -> str:
        return json.dumps(remaining.pop(0) if len(remaining) > 1 else remaining[0])

    monkeypatch.setattr("omniagentos.llm.client.ShortCallClient.complete", complete)


def _install_promotion_fakes(
    monkeypatch: pytest.MonkeyPatch, calls: list[dict[str, Any]] | None = None
) -> None:
    _no_llm(monkeypatch)
    monkeypatch.setattr("omniagentos.api.routes.chats.dispatch_spec", _fake_dispatch(calls))


def _create_chat(asgi_client: httpx.AsyncClient, store: SqliteStore, **kwargs: Any) -> dict[str, Any]:
    response = _run(asgi_client.post("/api/chats", json={"title": "Promotion chat", **kwargs}))
    assert response.status_code == 201
    chat = response.json()
    ConversationStore(store).append("chat", chat["id"], "user", "Please ship these tasks.")
    return chat


def _say(store: SqliteStore, chat_id: str, *contents: str) -> None:
    conversations = ConversationStore(store)
    for content in contents:
        conversations.append("chat", chat_id, "user", content)


def _task_count(store: SqliteStore) -> int:
    return int(store._connection.execute("SELECT COUNT(*) FROM board_tasks").fetchone()[0])


def _org(store: SqliteStore, task_id: str) -> dict[str, Any]:
    raw = store._connection.execute(
        "SELECT org_json FROM board_tasks WHERE id = ?", (task_id,)
    ).fetchone()["org_json"]
    parsed = json.loads(raw)
    assert isinstance(parsed, dict)
    return parsed


def _description(store: SqliteStore, task_id: str) -> str:
    return str(
        store._connection.execute(
            "SELECT description FROM board_tasks WHERE id = ?", (task_id,)
        ).fetchone()["description"]
    )


def _chat_status(store: SqliteStore, chat_id: str) -> str:
    return str(
        store._connection.execute(
            "SELECT status FROM chats WHERE id = ?", (chat_id,)
        ).fetchone()["status"]
    )


def _dirs_under(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_dir())


def _company(store: SqliteStore, company_id: str, slug: str, name: str) -> None:
    store._write(
        "INSERT INTO org_companies (id, slug, name, status, created_at) VALUES (?, ?, ?, ?, ?)",
        (company_id, slug, name, "active", utc_now_iso()),
    )


def _work_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    root = tmp_path / "work"
    root.mkdir()
    monkeypatch.setenv("OMNIAGENTOS_WORK_ROOT", str(root))

    def portable_ensure(
        *,
        company: str,
        department: str | None = None,
        subfolder: str | None = None,
    ) -> EnsureResult:
        """Exercise route scope selection without Darwin's atomic install primitive."""
        target = root.joinpath(
            *(part for part in (company, department, subfolder) if part is not None)
        )
        existed = target.exists()
        target.mkdir(parents=True, exist_ok=True)
        return EnsureResult(
            path=str(target),
            created=not existed,
            company=company,
            department=department,
            subfolder=subfolder,
        )

    monkeypatch.setattr("omniagentos.api.routes.chats.workfs_ensure", portable_ensure)
    return root


# --------------------------------------------------------------------------
# The real extraction path — no extractor mock anywhere below.
# --------------------------------------------------------------------------


def test_three_asks_promote_to_three_distinct_cards(
    asgi_client: httpx.AsyncClient,
    store: SqliteStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE regression test for the data-loss defect (cb-promote B1).

    Three distinct asks in one thread must become three distinct cards through
    the REAL extractor. When every item silently keyed to message 0, this thread
    produced ONE card and ``task_ids`` repeated that single id three times.
    """
    _install_promotion_fakes(monkeypatch)
    chat = _run(asgi_client.post("/api/chats", json={"title": "Three asks"})).json()
    _say(
        store,
        chat["id"],
        "Please rebuild the billing exporter.",
        "Also migrate the auth service to OIDC.",
        "Finally, write the launch runbook.",
    )
    before = _task_count(store)

    response = _run(asgi_client.post(f"/api/chats/{chat['id']}/promote", json={}))

    assert response.status_code == 201
    task_ids = response.json()["task_ids"]
    assert _task_count(store) - before == 3, "three asks must create three cards"
    assert len(set(task_ids)) == 3, f"expected three distinct card ids, got {task_ids}"
    provenance = [_org(store, task_id) for task_id in task_ids]
    assert sorted(p["promoted_source_message_index"] for p in provenance) == [0, 1, 2], (
        "each card must record the message it actually came from"
    )
    assert len({p["promoted_item_key"] for p in provenance}) == 3, (
        "each card must carry its own content-derived identity"
    )
    titles = sorted(
        store._connection.execute(
            "SELECT title FROM board_tasks WHERE id = ?", (task_id,)
        ).fetchone()["title"]
        for task_id in task_ids
    )
    assert titles == [
        "Also migrate the auth service to OIDC.",
        "Finally, write the launch runbook.",
        "Please rebuild the billing exporter.",
    ]


def test_repromote_after_thread_grows_creates_only_the_new_cards(
    asgi_client: httpx.AsyncClient,
    store: SqliteStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Promotion is repeatable: a grown thread promotes its NEW asks (B2)."""
    _install_promotion_fakes(monkeypatch)
    chat = _run(asgi_client.post("/api/chats", json={"title": "Growing thread"})).json()
    _say(store, chat["id"], "Please rebuild the billing exporter.")
    first = _run(asgi_client.post(f"/api/chats/{chat['id']}/promote", json={}))
    assert len(first.json()["task_ids"]) == 1
    after_first = _task_count(store)

    _say(
        store,
        chat["id"],
        "Also migrate the auth service to OIDC.",
        "Finally, write the launch runbook.",
    )
    second = _run(asgi_client.post(f"/api/chats/{chat['id']}/promote", json={}))

    assert second.status_code == 201
    assert _task_count(store) - after_first == 2, "two new asks must yield two new cards"
    assert len(set(second.json()["task_ids"])) == 3, "the response covers all three items"
    assert first.json()["task_ids"][0] in second.json()["task_ids"], "the old card is reused"


def test_repromote_with_reworded_llm_output_stays_one_card(
    asgi_client: httpx.AsyncClient,
    store: SqliteStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Identity is content-derived, so LLM re-wording cannot fork a card."""
    _llm_returns(
        monkeypatch,
        [
            [{"title": "Fix the login bug", "description": "Auth needs fixing.",
              "source_message_index": 0}],
            [{"title": "Repair authentication", "description": "The sign-in flow is broken.",
              "source_message_index": 0}],
        ],
    )
    _install_promotion_fakes(monkeypatch)
    chat = _run(asgi_client.post("/api/chats", json={"title": "Rewording"})).json()
    _say(store, chat["id"], "The login is broken, please fix it.")

    first = _run(asgi_client.post(f"/api/chats/{chat['id']}/promote_item", json={"item_index": 0}))
    after_first = _task_count(store)
    second = _run(asgi_client.post(f"/api/chats/{chat['id']}/promote_item", json={"item_index": 0}))

    assert second.status_code == 201
    assert second.json()["task_id"] == first.json()["task_id"], (
        "same source message must resolve to the same card despite different prose"
    )
    assert _task_count(store) == after_first


def test_unusable_llm_source_index_still_yields_distinct_cards(
    asgi_client: httpx.AsyncClient,
    store: SqliteStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bad/missing model index falls back to item content — never to message 0."""
    _llm_returns(
        monkeypatch,
        [
            [
                {"title": "Rebuild exporter", "description": "Billing exporter.",
                 "source_message_index": 999},
                {"title": "Migrate auth", "description": "Move to OIDC."},
                {"title": "Write runbook", "description": "Launch runbook.",
                 "source_message_index": "not-an-index"},
            ]
        ],
    )
    monkeypatch.setattr("omniagentos.api.routes.chats.dispatch_spec", _fake_dispatch())
    chat = _run(asgi_client.post("/api/chats", json={"title": "Bad indices"})).json()
    _say(store, chat["id"], "Three things please: exporter, auth, runbook.")
    before = _task_count(store)

    response = _run(asgi_client.post(f"/api/chats/{chat['id']}/promote", json={}))

    task_ids = response.json()["task_ids"]
    assert _task_count(store) - before == 3, "an unusable index must not collapse the items"
    assert len(set(task_ids)) == 3
    provenance = [_org(store, task_id) for task_id in task_ids]
    assert all(p["promoted_source_message_index"] is None for p in provenance), (
        "an unvalidated index is never recorded as provenance"
    )
    assert len({p["promoted_item_key"] for p in provenance}) == 3


def test_two_items_from_one_message_are_not_collapsed(
    asgi_client: httpx.AsyncClient,
    store: SqliteStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One message can carry two asks; both get a card, and re-promote adds none."""
    payload = [
        {"title": "Rebuild exporter", "description": "Billing exporter.", "source_message_index": 0},
        {"title": "Migrate auth", "description": "Move to OIDC.", "source_message_index": 0},
    ]
    _llm_returns(monkeypatch, [payload])
    monkeypatch.setattr("omniagentos.api.routes.chats.dispatch_spec", _fake_dispatch())
    chat = _run(asgi_client.post("/api/chats", json={"title": "Two asks, one message"})).json()
    _say(store, chat["id"], "Rebuild the exporter and migrate auth to OIDC.")
    before = _task_count(store)

    first = _run(asgi_client.post(f"/api/chats/{chat['id']}/promote", json={}))
    assert _task_count(store) - before == 2
    assert len(set(first.json()["task_ids"])) == 2

    second = _run(asgi_client.post(f"/api/chats/{chat['id']}/promote", json={}))
    assert _task_count(store) - before == 2, "re-promote must not duplicate either item"
    assert set(second.json()["task_ids"]) == set(first.json()["task_ids"])


# --------------------------------------------------------------------------
# Concurrency: one ask is one card even when two promotes race.
# --------------------------------------------------------------------------


def _slow_dispatch(hold_s: float = 0.10, calls: list[str] | None = None) -> Any:
    """A dispatch slow enough for a second promote to overlap it.

    Each invocation stands for one executor (a run or a live session) actually
    starting — which is why the concurrency tests count invocations, not cards.
    """

    def dispatch(
        *, collab_store: CollabStore, spec: Any, board_task_id: str | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        if calls is not None:
            calls.append(str(board_task_id))
        task_id = _dispatch_onto(collab_store, spec, board_task_id)
        threading.Event().wait(hold_s)
        return {"task_id": task_id, "board_task": collab_store.get_board_task(task_id)}

    return dispatch


def _widen_the_reservation_window(
    monkeypatch: pytest.MonkeyPatch, *, parties: int = 0, hold_s: float = 0.25
) -> list[int]:
    """Hold callers inside the lookup → reserve window at the same time.

    ``_ensure_promotion_workfs`` is a real production call made after the dedupe
    lookup and before the reservation INSERT, so hooking it is how a test gets
    both racers into that window. Without it the window is microseconds wide and a
    concurrency test passes whether or not the claim exists — the "green while
    broken" shape this lane was rejected for twice.

    ``parties>0`` uses a BARRIER, not a sleep: every caller signals that it has
    passed the lookup and blocks until the others have too, so both are provably
    inside the window before either INSERTs. A sleep only makes that likely; if
    one racer is slow its lookup can see the other's finished card and return
    early, never exercising the claim at all. Returns the arrival counter so a
    test can assert the race actually happened.
    """
    arrivals: list[int] = []
    barrier = threading.Barrier(parties) if parties else None

    def slow_workfs(_scope: Any) -> None:
        arrivals.append(1)
        if barrier is not None:
            try:
                barrier.wait(timeout=20)
            except threading.BrokenBarrierError:  # pragma: no cover - timeout safety
                pass
        else:
            threading.Event().wait(hold_s)
        return None

    monkeypatch.setattr("omniagentos.api.routes.chats._ensure_promotion_workfs", slow_workfs)
    return arrivals


def _promote_item_concurrently(chat_id: str, item_index: int, count: int = 2) -> list[Any]:
    """Fire ``count`` identical promote_item calls that enter the route together."""
    barrier = threading.Barrier(count)
    results: list[Any] = [None] * count

    def call(slot: int) -> None:
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        )
        barrier.wait(timeout=5)
        try:
            results[slot] = asyncio.run(
                client.post(f"/api/chats/{chat_id}/promote_item", json={"item_index": item_index})
            )
        finally:
            asyncio.run(client.aclose())

    threads = [threading.Thread(target=call, args=(slot,)) for slot in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    return results


def _live_cards(store: SqliteStore) -> list[str]:
    return [
        str(row["id"])
        for row in store._connection.execute(
            "SELECT id FROM board_tasks WHERE archived_at IS NULL AND status != 'cancelled' "
            "AND json_extract(CASE WHEN json_valid(org_json) THEN org_json ELSE '{}' END, "
            "'$.promoted') IS NOT NULL"
        ).fetchall()
    ]


def test_two_concurrent_promotes_of_one_item_create_one_card(
    asgi_client: httpx.AsyncClient,
    store: SqliteStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dedupe lookup and the reservation are separate statements; the gap is a race.

    Two threads promote the SAME item at the same time with a dispatch slow enough
    to overlap. Exactly one card, exactly one dispatch, and both callers are told
    about the same card.
    """
    _no_llm(monkeypatch)
    _widen_the_reservation_window(monkeypatch)
    dispatches: list[str] = []
    monkeypatch.setattr(
        "omniagentos.api.routes.chats.dispatch_spec", _slow_dispatch(calls=dispatches)
    )
    chat = _create_chat(asgi_client, store)
    before = _task_count(store)

    responses = _promote_item_concurrently(chat["id"], 0)

    assert [r.status_code for r in responses] == [201, 201]
    assert _task_count(store) - before == 1, "a double-click must not create two cards"
    assert len(dispatches) == 1, "a double-click must not start two executors"
    assert len({r.json()["task_id"] for r in responses}) == 1, "both callers get the same card"
    assert len(_live_cards(store)) == 1


def test_the_reservation_is_atomic_even_without_the_in_process_gate(
    asgi_client: httpx.AsyncClient,
    store: SqliteStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The durable half, with the per-item gate disabled (as another PROCESS is).

    The claim is taken by the INSERT that creates the card, before any executor
    exists, so the loser never reaches ``dispatch_spec`` at all: one card, one
    dispatch, no cancelled litter, and both callers get the winner.
    """
    _no_llm(monkeypatch)
    # A barrier, not a sleep: both promotes are provably inside the window.
    arrivals = _widen_the_reservation_window(monkeypatch, parties=2)
    dispatches: list[str] = []
    monkeypatch.setattr(
        "omniagentos.api.routes.chats.dispatch_spec", _slow_dispatch(calls=dispatches)
    )
    monkeypatch.setattr(
        "omniagentos.api.routes.chats._promote_item_gate",
        lambda *_args: contextlib.nullcontext(),
    )
    chat = _create_chat(asgi_client, store)
    before = _task_count(store)

    responses = _promote_item_concurrently(chat["id"], 0)

    assert len(arrivals) == 2, "both promotes must have reached the window; the race is the test"
    assert [r.status_code for r in responses] == [201, 201]
    assert len(dispatches) == 1, "the loser must never start an executor"
    assert _task_count(store) - before == 1, "the loser must never leave a card behind"
    live = _live_cards(store)
    assert len(live) == 1, f"exactly one card may hold the item, got {live}"
    assert len({r.json()["task_id"] for r in responses}) == 1, "both callers get the winner"
    assert responses[0].json()["task_id"] == live[0]


_CROSS_PROCESS_CHILD = '''
"""Child of tests/chats/test_promote.py's cross-process probe. Not a test."""
import asyncio, json, os, pathlib, sys, time

db_path, chat_id, workdir, slot = sys.argv[1:5]
work = pathlib.Path(workdir)

import httpx
from omniagentos.api.deps import get_policy_config, get_store
from omniagentos.api.main import app, require_session_token
from omniagentos.api.routes.collab import get_collab_store
import omniagentos.api.routes.chats as chats_routes
from omniagentos.collab.contracts import BoardTask
from omniagentos.collab.store import CollabStore
from omniagentos.llm.client import ShortCallClient
from omniagentos.policy import load_policy

collab = CollabStore(db_path)


def fake_dispatch(*, collab_store, spec, board_task_id=None, **kwargs):
    # One marker file per executor actually started, across BOTH processes.
    (work / ("dispatch-%s-%d" % (slot, os.getpid()))).write_text(str(board_task_id))
    time.sleep(0.4)
    if board_task_id:
        collab_store.update_board_task(
            board_task_id,
            {"title": spec.title, "description": spec.description, "status": "open"},
        )
        task_id = board_task_id
    else:
        task = BoardTask(title=spec.title, description=spec.description)
        collab_store.create_board_task(task)
        task_id = task.id
    return {"task_id": task_id, "board_task": collab_store.get_board_task(task_id)}


def no_llm(*args, **kwargs):
    raise RuntimeError("no LLM in the probe")


def slow_workfs(scope):
    # The real call sitting between the dedupe lookup and the reservation INSERT.
    # A RENDEZVOUS, not a sleep: this process announces that it has passed the
    # lookup and blocks until the other has too, so neither can INSERT before both
    # are inside the window. With a sleep, a slow sibling's lookup could see the
    # winner's finished card and return early — the probe would then pass without
    # ever exercising the claim, which is exactly the false green being fixed.
    (work / ("inwindow-%s" % slot)).write_text("1")
    deadline = time.time() + 60
    while time.time() < deadline and len(list(work.glob("inwindow-*"))) < 2:
        time.sleep(0.005)
    return None


chats_routes.dispatch_spec = fake_dispatch
chats_routes._ensure_promotion_workfs = slow_workfs
ShortCallClient.complete = no_llm
app.dependency_overrides[get_store] = lambda: collab._store
app.dependency_overrides[get_collab_store] = lambda: collab
app.dependency_overrides[get_policy_config] = load_policy
# Same bypass tests/conftest.py applies in-process; the probe is about the
# promotion claim, not about the session-token gate.
app.dependency_overrides[require_session_token] = lambda: None

client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")
(work / ("ready-%s" % slot)).write_text("1")
deadline = time.time() + 30
while time.time() < deadline and len(list(work.glob("ready-*"))) < 2:
    time.sleep(0.005)

response = asyncio.run(client.post("/api/chats/%s/promote_item" % chat_id, json={"item_index": 0}))
(work / ("response-%s.json" % slot)).write_text(
    json.dumps({"status": response.status_code, "body": response.json()})
)
'''


def test_two_processes_promoting_one_item_start_one_executor(
    asgi_client: httpx.AsyncClient,
    store: SqliteStore,
    tmp_db_path: Path,
    tmp_path: Path,
) -> None:
    """CROSS-PROCESS probe: an in-process lock cannot be the whole answer.

    Two real interpreters, one SQLite file, the same item. Both are held INSIDE
    the lookup → reserve window by a filesystem rendezvous (each announces it has
    passed the lookup and blocks until the other has), so neither can INSERT until
    both are provably racing — a sleep alone would let a slow sibling's lookup see
    the winner's finished card and return early, and the probe would pass without
    ever exercising the claim.

    Because the (chat_id, item_key) claim is taken by the INSERT that creates the
    card — before ``dispatch_spec`` is called — the loser returns the winner's
    card without ever starting an executor. Asserts exactly one card AND exactly
    one dispatch, counted from the far side of the DB and the filesystem.
    """
    chat = _create_chat(asgi_client, store)
    store.checkpoint_wal()
    work = tmp_path / "probe"
    work.mkdir()
    child = tmp_path / "promote_child.py"
    child.write_text(_CROSS_PROCESS_CHILD)
    repo_root = Path(__file__).resolve().parents[2]
    env = {**os.environ, "PYTHONPATH": str(repo_root), "OMNIAGENTOS_WORK_ROOT": str(work / "wr")}
    (work / "wr").mkdir()

    procs = [
        subprocess.Popen(  # noqa: S603
            [sys.executable, str(child), str(tmp_db_path), chat["id"], str(work), slot],
            cwd=str(repo_root),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for slot in ("a", "b")
    ]
    outputs = [proc.communicate(timeout=180) for proc in procs]
    for proc, (out, err) in zip(procs, outputs, strict=True):
        assert proc.returncode == 0, f"probe child failed:\n{out}\n{err}"

    in_window = sorted(p.name for p in work.glob("inwindow-*"))
    assert in_window == ["inwindow-a", "inwindow-b"], (
        f"both processes must have raced inside the lookup→reserve window, got {in_window}; "
        "a probe that did not race cannot prove anything"
    )
    responses = [
        json.loads((work / f"response-{slot}.json").read_text()) for slot in ("a", "b")
    ]
    assert [r["status"] for r in responses] == [201, 201]
    dispatches = sorted(p.name for p in work.glob("dispatch-*"))
    assert len(dispatches) == 1, f"two processes started two executors: {dispatches}"

    promoted_cards = [
        str(row["id"])
        for row in store._connection.execute(
            "SELECT id FROM board_tasks WHERE json_extract("
            "CASE WHEN json_valid(org_json) THEN org_json ELSE '{}' END, "
            "'$.promoted_item_key') IS NOT NULL"
        ).fetchall()
    ]
    assert len(promoted_cards) == 1, f"two processes created two cards: {promoted_cards}"
    assert {r["body"]["task_id"] for r in responses} == set(promoted_cards)
    assert _task_count(store) == 2, "the companion card plus exactly one promoted card"


def test_a_malformed_org_json_row_does_not_break_promotion(
    asgi_client: httpx.AsyncClient,
    store: SqliteStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SQLite RAISES on json_extract over malformed JSON — one bad row must not
    take down every promotion in the workspace."""
    _install_promotion_fakes(monkeypatch)
    store._write(
        "INSERT INTO board_tasks (id, title, description, status, created_at, updated_at, org_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("btk_malformed", "Legacy", "", "open", utc_now_iso(), utc_now_iso(), "not json"),
    )
    chat = _create_chat(asgi_client, store)

    response = _run(asgi_client.post(f"/api/chats/{chat['id']}/promote", json={}))

    assert response.status_code == 201
    assert len(response.json()["task_ids"]) == 1


# --------------------------------------------------------------------------
# The failure path: a dispatch that dies after starting work.
# --------------------------------------------------------------------------


def _insert_run(store: SqliteStore, run_id: str) -> None:
    """A live run row, the far side of what dispatch_spec's readonly/tools path makes."""
    now = utc_now_iso()
    store._write(
        "INSERT INTO tasks (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
        (f"tsk_{run_id}", "orphan task", now, now),
    )
    store._write(
        "INSERT INTO runs (id, task_id, harness, trace_id, state, queued_at, "
        "created_at, updated_at) VALUES (?, ?, 'agent', ?, 'running', ?, ?, ?)",
        (run_id, f"tsk_{run_id}", f"trace_{run_id}", now, now, now),
    )


def test_a_dispatch_that_dies_after_starting_work_cancels_it_and_holds_the_item(
    asgi_client: httpx.AsyncClient,
    store: SqliteStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``dispatch_spec`` can spawn and THEN raise (session mode spawns before it
    records result_ref; run mode creates the run before attaching run_id).

    Releasing blindly there would archive the card out of sight while its executor
    kept working, and free the item so the next retry started a SECOND one. The
    executor must be told to stop, the card must stay visible, and the item must
    stay held.
    """
    _no_llm(monkeypatch)
    run_id = "run_orphan"
    _insert_run(store, run_id)
    dispatches: list[str] = []

    def dispatch_then_die(
        *, collab_store: CollabStore, spec: Any, board_task_id: str | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        dispatches.append(str(board_task_id))
        # The executor is live and linked, exactly as service.py leaves it.
        collab_store.update_board_task(str(board_task_id), {"run_id": run_id})
        raise RuntimeError("spawned, then the bookkeeping blew up")

    monkeypatch.setattr("omniagentos.api.routes.chats.dispatch_spec", dispatch_then_die)
    chat = _create_chat(asgi_client, store)

    first = _run(asgi_client.post(f"/api/chats/{chat['id']}/promote", json={}))

    assert first.status_code == 201
    assert first.json()["task_ids"] == [], "a failed dispatch promotes nothing"
    card = store._connection.execute(
        "SELECT id, status, archived_at, org_json FROM board_tasks WHERE run_id = ?", (run_id,)
    ).fetchone()
    assert card is not None, "the card that owns the live run must still exist"
    assert card["archived_at"] is None, "never archive a card whose executor is alive"
    assert json.loads(card["org_json"])["promoted_item_key"], "the item stays held"
    assert store.get_run(run_id)["cancel_requested"] == 1, "the orphan must be told to stop"

    # A retry must NOT start a second executor for the same ask.
    second = _run(asgi_client.post(f"/api/chats/{chat['id']}/promote", json={}))
    assert len(dispatches) == 1, "the held card must not be dispatched again"
    assert second.json()["task_ids"] == [str(card["id"])]


def test_a_dispatch_that_dies_before_starting_work_releases_the_item(
    asgi_client: httpx.AsyncClient,
    store: SqliteStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half: nothing was attached, so the ask is handed back cleanly."""
    _no_llm(monkeypatch)
    attempts: list[str] = []

    def die_before_starting(
        *, collab_store: CollabStore, spec: Any, board_task_id: str | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        attempts.append(str(board_task_id))
        if len(attempts) == 1:
            raise RuntimeError("failed before anything was created")
        task_id = _dispatch_onto(collab_store, spec, board_task_id)
        return {"task_id": task_id, "board_task": collab_store.get_board_task(task_id)}

    monkeypatch.setattr("omniagentos.api.routes.chats.dispatch_spec", die_before_starting)
    chat = _create_chat(asgi_client, store)

    first = _run(asgi_client.post(f"/api/chats/{chat['id']}/promote", json={}))
    assert first.json()["task_ids"] == []
    released = store._connection.execute(
        "SELECT status, archived_at, org_json FROM board_tasks WHERE id = ?", (attempts[0],)
    ).fetchone()
    assert released["status"] == "cancelled"
    assert released["archived_at"] is not None
    assert "promoted_item_key" not in json.loads(released["org_json"]), "the item is free again"

    second = _run(asgi_client.post(f"/api/chats/{chat['id']}/promote", json={}))
    assert len(second.json()["task_ids"]) == 1, "the ask can be promoted again"
    assert len(attempts) == 2


def test_a_reservation_is_not_claimable_before_its_executor_attaches(
    asgi_client: httpx.AsyncClient,
    store: SqliteStore,
    collab_store: CollabStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The board hands out ``open`` cards to any matching agent; a reservation has
    no executor yet, so it is inserted ``pending`` and only becomes ``open`` when
    dispatch attaches one."""
    _no_llm(monkeypatch)
    seen: list[tuple[str, int]] = []

    def observe_then_dispatch(
        *, collab_store: CollabStore, spec: Any, board_task_id: str | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        card = collab_store.get_board_task(str(board_task_id)) or {}
        claimable = {t["id"] for t in collab_store.open_tasks_for([])}
        seen.append((str(card.get("status")), str(board_task_id) in claimable))
        task_id = _dispatch_onto(collab_store, spec, board_task_id)
        return {"task_id": task_id, "board_task": collab_store.get_board_task(task_id)}

    monkeypatch.setattr("omniagentos.api.routes.chats.dispatch_spec", observe_then_dispatch)
    chat = _create_chat(asgi_client, store)

    response = _run(asgi_client.post(f"/api/chats/{chat['id']}/promote", json={}))

    assert seen == [("pending", False)], "a reservation must not be claimable work"
    task_id = response.json()["task_ids"][0]
    assert (collab_store.get_board_task(task_id) or {})["status"] == "open"
    assert task_id in {t["id"] for t in collab_store.open_tasks_for([])}, (
        "once its executor is attached the card is normal claimable work"
    )


def test_a_stranded_reservation_is_redispatched_not_reported_as_done(
    asgi_client: httpx.AsyncClient,
    store: SqliteStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crash between the reservation and its executor must self-heal on retry.

    Without the check, the pending card holds the item and every later promote
    answers "already promoted" — the ask is stranded on a card nothing is working.
    """
    _install_promotion_fakes(monkeypatch)
    chat = _create_chat(asgi_client, store)
    chat_store = ChatStore(store)
    messages = ConversationStore(store).read("chat", chat["id"])
    item_key = _promoted_item_key(chat["id"], "msg", messages[0]["content"])
    stranded = chat_store.reserve_promoted_card(
        chat_id=chat["id"],
        parent_task_id=str(chat["board_task_id"]),
        item_key=item_key,
        title="Please ship these tasks.",
        description="",
        source_message_index=0,
    )
    assert stranded is not None
    before = _task_count(store)

    response = _run(asgi_client.post(f"/api/chats/{chat['id']}/promote", json={}))

    assert response.json()["task_ids"] == [stranded], "the same card, now dispatched"
    assert _task_count(store) == before, "self-healing must not create a second card"
    card = store._connection.execute(
        "SELECT status FROM board_tasks WHERE id = ?", (stranded,)
    ).fetchone()
    assert card["status"] == "open", "the executor attached this time"


def test_only_one_retry_can_adopt_a_stranded_reservation(
    asgi_client: httpx.AsyncClient,
    store: SqliteStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The adoption is a compare-and-swap, so two retries cannot both re-dispatch."""
    _install_promotion_fakes(monkeypatch)
    chat = _create_chat(asgi_client, store)
    chat_store = ChatStore(store)
    stranded = chat_store.reserve_promoted_card(
        chat_id=chat["id"],
        parent_task_id=str(chat["board_task_id"]),
        item_key="key_for_adoption",
        title="Stranded",
        description="",
    )
    assert stranded is not None

    assert chat_store.adopt_stranded_reservation(stranded) is True
    assert chat_store.adopt_stranded_reservation(stranded) is False, "second retry must lose"
    # A card with a live executor is never adoptable, however old it is.
    store._write("UPDATE board_tasks SET run_id = 'run_live' WHERE id = ?", (stranded,))
    assert chat_store.adopt_stranded_reservation(stranded, stale_after_s=0) is False


def test_a_corrupted_promoted_card_fails_open_to_a_new_card(
    asgi_client: httpx.AsyncClient,
    store: SqliteStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The json_valid guard FAILS OPEN, deliberately — pinned so it is a decision.

    If the card holding an item's identity has its own org_json corrupted, the
    dedupe lookup can no longer see it and a re-promote makes a NEW card. The
    alternative — refusing — would silently drop the operator's ask on a row we
    cannot read; a duplicate card is visible and archivable.
    """
    _install_promotion_fakes(monkeypatch)
    chat = _create_chat(asgi_client, store)
    first = _run(asgi_client.post(f"/api/chats/{chat['id']}/promote", json={}))
    held_id = first.json()["task_ids"][0]
    after_first = _task_count(store)

    store._write("UPDATE board_tasks SET org_json = ? WHERE id = ?", ("{not json", held_id))
    second = _run(asgi_client.post(f"/api/chats/{chat['id']}/promote", json={}))

    assert second.status_code == 201
    assert second.json()["task_ids"] != [held_id], "the corrupted card is unreadable, not reused"
    assert _task_count(store) == after_first + 1, "fails OPEN: a fresh card, never a silent drop"


def test_message_indices_1_and_10_do_not_collide(
    asgi_client: httpx.AsyncClient,
    store: SqliteStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for the LIKE-prefix dedupe: ``index":1`` once matched ``index":10``."""
    _install_promotion_fakes(monkeypatch)
    chat = _run(asgi_client.post("/api/chats", json={"title": "Eleven asks"})).json()
    _say(store, chat["id"], *[f"Please deliver work package number {n}." for n in range(11)])
    before = _task_count(store)

    first = _run(asgi_client.post(f"/api/chats/{chat['id']}/promote_item", json={"item_index": 1}))
    tenth = _run(asgi_client.post(f"/api/chats/{chat['id']}/promote_item", json={"item_index": 10}))

    assert first.json()["task_id"] != tenth.json()["task_id"]
    assert _task_count(store) - before == 2
    assert _org(store, first.json()["task_id"])["promoted_source_message_index"] == 1
    assert _org(store, tenth.json()["task_id"])["promoted_source_message_index"] == 10


# --------------------------------------------------------------------------
# Seams: the operator's company + Work folder choices drive brief and workfs.
# --------------------------------------------------------------------------


def test_chat_label_never_reaches_the_filesystem(
    asgi_client: httpx.AsyncClient,
    store: SqliteStore,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``meta.folder`` is a colour-coded CHAT LABEL (migration 088), not a path."""
    work_root = _work_root(monkeypatch, tmp_path)
    _company(store, "co_label", "label-co", "Acme Corp")
    ProjectStore(store).create_project(
        {"id": "proj_label", "name": "Labelled", "org_company_id": "co_label"}
    )
    _install_promotion_fakes(monkeypatch)
    chat = _create_chat(
        asgi_client, store, project_id="proj_label", meta={"folder": "Q3 Ideas"}
    )

    response = _run(asgi_client.post(f"/api/chats/{chat['id']}/promote", json={}))

    created = _dirs_under(work_root)
    assert created == ["Acme Corp"], f"only the company folder may be created, got {created}"
    task_id = response.json()["task_ids"][0]
    assert "Q3 Ideas" not in _description(store, task_id)
    assert "Q3 Ideas" not in str(_org(store, task_id).get("working_dir") or "")


def test_nested_work_folder_maps_onto_workfs_scope(
    asgi_client: httpx.AsyncClient,
    store: SqliteStore,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``work_folder`` is a WORK-root-relative path; its segments are scope levels."""
    work_root = _work_root(monkeypatch, tmp_path)
    _company(store, "co_nested", "nested-co", "Acme Corp")
    ProjectStore(store).create_project(
        {"id": "proj_nested", "name": "Nested", "org_company_id": "co_nested"}
    )
    _install_promotion_fakes(monkeypatch)
    chat = _create_chat(
        asgi_client, store, project_id="proj_nested", meta={"work_folder": "Acme/Product"}
    )

    response = _run(asgi_client.post(f"/api/chats/{chat['id']}/promote", json={}))

    task_id = response.json()["task_ids"][0]
    working_dir = _org(store, task_id)["working_dir"]
    assert working_dir == str(work_root / "Acme" / "Product")
    assert os.stat(working_dir)
    assert _dirs_under(work_root) == ["Acme", "Acme/Product"]
    assert "**Folder**: Acme/Product" in _description(store, task_id)


def test_single_segment_work_folder_is_work_root_relative(
    asgi_client: httpx.AsyncClient,
    store: SqliteStore,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A one-segment pick lands at ``<root>/Acme``, not nested under the company name."""
    work_root = _work_root(monkeypatch, tmp_path)
    _company(store, "co_single", "single-co", "Acme Corp")
    ProjectStore(store).create_project(
        {"id": "proj_single", "name": "Single", "org_company_id": "co_single"}
    )
    _install_promotion_fakes(monkeypatch)
    chat = _create_chat(
        asgi_client, store, project_id="proj_single", meta={"work_folder": "Acme"}
    )

    response = _run(asgi_client.post(f"/api/chats/{chat['id']}/promote", json={}))

    working_dir = _org(store, response.json()["task_ids"][0])["working_dir"]
    assert working_dir == str(work_root / "Acme")
    assert _dirs_under(work_root) == ["Acme"]


def test_too_deep_work_folder_is_refused_loudly(
    asgi_client: httpx.AsyncClient,
    store: SqliteStore,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Beyond the three workfs scope levels, promotion warns — it never silently drops."""
    work_root = _work_root(monkeypatch, tmp_path)
    _install_promotion_fakes(monkeypatch)
    chat = _create_chat(asgi_client, store, meta={"work_folder": "a/b/c/d"})

    with caplog.at_level(logging.WARNING, logger="omniagentos.api.routes.chats"):
        response = _run(asgi_client.post(f"/api/chats/{chat['id']}/promote", json={}))

    task_id = response.json()["task_ids"][0]
    assert _dirs_under(work_root) == []
    assert _org(store, task_id).get("working_dir") is None
    assert any("a/b/c/d" in record.getMessage() for record in caplog.records), (
        f"a dropped work folder must be logged, got {[r.getMessage() for r in caplog.records]}"
    )
    assert "**Folder**: a/b/c/d" in _description(store, task_id), (
        "the operator's choice still belongs in the brief"
    )


def test_company_chosen_in_the_composer_drives_brief_and_workfs(
    asgi_client: httpx.AsyncClient,
    store: SqliteStore,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A fresh chat has no project; the composer's company must still be honoured (B7)."""
    work_root = _work_root(monkeypatch, tmp_path)
    _company(store, "co_meta", "acme", "Acme Corp")
    _install_promotion_fakes(monkeypatch)
    chat = _create_chat(
        asgi_client, store, meta={"org_company_id": "co_meta", "company_slug": "acme"}
    )

    response = _run(asgi_client.post(f"/api/chats/{chat['id']}/promote", json={}))

    task_id = response.json()["task_ids"][0]
    assert "**Company**: Acme Corp" in _description(store, task_id)
    assert _org(store, task_id)["working_dir"] == str(work_root / "Acme Corp")
    assert _dirs_under(work_root) == ["Acme Corp"]


def test_company_slug_alone_resolves_the_company(
    asgi_client: httpx.AsyncClient,
    store: SqliteStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``company_slug`` is the second first-class company source from chat meta."""
    _company(store, "co_slug", "slugco", "Slug Company")
    _install_promotion_fakes(monkeypatch)
    chat = _create_chat(asgi_client, store, meta={"company_slug": "slugco"})

    response = _run(asgi_client.post(f"/api/chats/{chat['id']}/promote", json={}))

    assert "**Company**: Slug Company" in _description(store, response.json()["task_ids"][0])


def test_chat_meta_company_wins_over_the_project_company(
    asgi_client: httpx.AsyncClient,
    store: SqliteStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The up-front composer choice is authoritative over an inherited project."""
    _company(store, "co_meta_win", "meta-win", "Composer Company")
    _company(store, "co_proj", "proj-co", "Project Company")
    ProjectStore(store).create_project(
        {"id": "proj_conflict", "name": "Conflict", "org_company_id": "co_proj"}
    )
    _install_promotion_fakes(monkeypatch)
    chat = _create_chat(
        asgi_client, store, project_id="proj_conflict", meta={"org_company_id": "co_meta_win"}
    )

    response = _run(asgi_client.post(f"/api/chats/{chat['id']}/promote", json={}))

    description = _description(store, response.json()["task_ids"][0])
    assert "**Company**: Composer Company" in description
    assert "Project Company" not in description


def test_company_from_project_still_reaches_the_brief(
    asgi_client: httpx.AsyncClient,
    store: SqliteStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pre-composer path (company via the chat's project) keeps working."""
    _company(store, "co_promote", "promote-co", "Promote Company")
    ProjectStore(store).create_project(
        {"id": "proj_promote", "name": "Promote Project", "org_company_id": "co_promote"}
    )
    _install_promotion_fakes(monkeypatch)
    chat = _create_chat(
        asgi_client, store, project_id="proj_promote", meta={"work_folder": "Promote/Docs"}
    )

    response = _run(
        asgi_client.post(f"/api/chats/{chat['id']}/promote_item", json={"item_index": 0})
    )
    description = _description(store, response.json()["task_id"])

    assert "**Company**: Promote Company" in description
    assert "**Folder**: Promote/Docs" in description


def test_promotion_without_company_or_work_folder_creates_nothing(
    asgi_client: httpx.AsyncClient,
    store: SqliteStore,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """No scope, no writes: promotion never invents a directory."""
    work_root = _work_root(monkeypatch, tmp_path)
    _install_promotion_fakes(monkeypatch)
    chat = _create_chat(asgi_client, store)

    response = _run(asgi_client.post(f"/api/chats/{chat['id']}/promote", json={}))

    assert _dirs_under(work_root) == []
    assert _org(store, response.json()["task_ids"][0]).get("working_dir") is None


def test_send_path_brief_carries_the_composer_company_and_work_folder(
    asgi_client: httpx.AsyncClient,
    store: SqliteStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The chat SEND path shares ``_promotion_context``; assert the preamble itself.

    ``test_routes.py`` only asserts the message content survives into the spec, so
    deleting the preamble injection would pass there. This test fails if the
    company/folder lines stop reaching the dispatched brief.
    """
    _company(store, "co_send", "send-co", "Send Company")
    dispatched: list[Any] = []

    def mock_dispatch(*_args: Any, spec: Any = None, **kwargs: Any) -> dict[str, Any]:
        dispatched.append(kwargs.get("spec") if spec is None else spec)
        return {"session_id": "ses_send", "execute": "session"}

    monkeypatch.setattr("omniagentos.api.routes.chats.dispatch_spec", mock_dispatch)
    chat = _run(
        asgi_client.post(
            "/api/chats",
            json={
                "title": "Composer chat",
                "meta": {"org_company_id": "co_send", "work_folder": "Send/Docs"},
            },
        )
    ).json()

    response = _run(
        asgi_client.post(
            f"/api/chats/{chat['id']}/messages", json={"content": "Draft the launch memo."}
        )
    )

    assert response.status_code == 201
    assert len(dispatched) == 1
    description = dispatched[0].description
    assert "**Company**: Send Company" in description
    assert "**Folder**: Send/Docs" in description
    assert "Draft the launch memo." in description


# --------------------------------------------------------------------------
# Identity edge cases — what the key survives, and what it deliberately does not.
# --------------------------------------------------------------------------


def _edit_message(store: SqliteStore, chat_id: str, seq: int, content: str) -> None:
    """Rewrite a stored message in place (the operator editing their own ask)."""
    store._write(
        "UPDATE conversations SET content = ? WHERE scope_type = 'chat' AND scope_id = ? AND seq = ?",
        (content, chat_id, seq),
    )


def test_editing_the_source_message_yields_a_new_card_by_design(
    asgi_client: httpx.AsyncClient,
    store: SqliteStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An EDITED ask is a different ask: the key changes and a new card appears.

    This is the documented trade-off of a content-derived key (store.py's
    ``reserve_promoted_card``). The old card is left alone rather than silently
    re-pointed at prose the operator has since changed.
    """
    _install_promotion_fakes(monkeypatch)
    chat = _run(asgi_client.post("/api/chats", json={"title": "Edited ask"})).json()
    _say(store, chat["id"], "Please rebuild the billing exporter.")
    first = _run(asgi_client.post(f"/api/chats/{chat['id']}/promote", json={}))
    assert len(first.json()["task_ids"]) == 1

    _edit_message(store, chat["id"], 1, "Please rebuild the invoicing exporter instead.")
    second = _run(asgi_client.post(f"/api/chats/{chat['id']}/promote", json={}))

    assert second.json()["task_ids"] != first.json()["task_ids"], (
        "an edited source message is a new item"
    )
    assert _org(store, second.json()["task_ids"][0])["promoted_item_key"] != (
        _org(store, first.json()["task_ids"][0])["promoted_item_key"]
    )


def test_deleting_another_message_preserves_the_existing_key(
    asgi_client: httpx.AsyncClient,
    store: SqliteStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Index shift is exactly what killed the old scheme; content keys ignore it."""
    _install_promotion_fakes(monkeypatch)
    chat = _run(asgi_client.post("/api/chats", json={"title": "Deletion"})).json()
    _say(
        store,
        chat["id"],
        "Please rebuild the billing exporter.",
        "Also migrate the auth service to OIDC.",
    )
    first = _run(asgi_client.post(f"/api/chats/{chat['id']}/promote", json={}))
    keys_before = {_org(store, task_id)["promoted_item_key"] for task_id in first.json()["task_ids"]}
    after_first = _task_count(store)

    store._write(
        "DELETE FROM conversations WHERE scope_type = 'chat' AND scope_id = ? AND seq = 1",
        (chat["id"],),
    )
    second = _run(asgi_client.post(f"/api/chats/{chat['id']}/promote", json={}))

    assert _task_count(store) == after_first, "the surviving ask must not be promoted twice"
    surviving = _org(store, second.json()["task_ids"][0])
    assert surviving["promoted_item_key"] in keys_before, "the key is unmoved by the deletion"
    # The display hint is NOT re-stamped on a dedupe hit, so it still reads 1 while
    # the message now sits at position 0. That is why it is a hint and not the key:
    # a scheme that keyed off it would have made a duplicate card right here.
    assert surviving["promoted_source_message_index"] == 1


def test_identical_text_in_two_chats_does_not_collide(
    asgi_client: httpx.AsyncClient,
    store: SqliteStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The chat id is inside the digest, so two workspaces never share a card."""
    _install_promotion_fakes(monkeypatch)
    text = "Please rebuild the billing exporter."
    first_chat = _run(asgi_client.post("/api/chats", json={"title": "Chat one"})).json()
    second_chat = _run(asgi_client.post("/api/chats", json={"title": "Chat two"})).json()
    _say(store, first_chat["id"], text)
    _say(store, second_chat["id"], text)
    before = _task_count(store)

    first = _run(asgi_client.post(f"/api/chats/{first_chat['id']}/promote", json={}))
    second = _run(asgi_client.post(f"/api/chats/{second_chat['id']}/promote", json={}))

    assert _task_count(store) - before == 2
    assert first.json()["task_ids"] != second.json()["task_ids"]
    assert _org(store, first.json()["task_ids"][0])["promoted_item_key"] != (
        _org(store, second.json()["task_ids"][0])["promoted_item_key"]
    )


def test_whitespace_only_differences_keep_the_same_identity(
    asgi_client: httpx.AsyncClient,
    store: SqliteStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tabs, newlines and runs of spaces normalise away; the ask is unchanged."""
    _install_promotion_fakes(monkeypatch)
    chat = _run(asgi_client.post("/api/chats", json={"title": "Whitespace"})).json()
    _say(store, chat["id"], "Please rebuild the billing exporter.")
    first = _run(asgi_client.post(f"/api/chats/{chat['id']}/promote", json={}))
    after_first = _task_count(store)

    _edit_message(store, chat["id"], 1, "Please\trebuild   the\nbilling exporter.")
    second = _run(asgi_client.post(f"/api/chats/{chat['id']}/promote", json={}))

    assert _task_count(store) == after_first, "re-spacing an ask must not fork the card"
    assert second.json()["task_ids"] == first.json()["task_ids"]


def test_unicode_nfc_and_nfd_forms_are_different_identities(
    asgi_client: httpx.AsyncClient,
    store: SqliteStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DOCUMENTED limitation: the digest is over bytes, not a unicode normal form.

    ``café`` composed (NFC) and decomposed (NFD) look identical to a human and
    hash differently, so a re-promote after a form change makes a second card.
    Editors do not silently re-normalise stored text, so this stays theoretical;
    it is asserted here so the behaviour is a decision rather than a surprise.
    """
    _install_promotion_fakes(monkeypatch)
    nfc = unicodedata.normalize("NFC", "Please rebuild the café exporter.")
    nfd = unicodedata.normalize("NFD", nfc)
    assert nfc != nfd
    chat = _run(asgi_client.post("/api/chats", json={"title": "Unicode"})).json()
    _say(store, chat["id"], nfc)
    first = _run(asgi_client.post(f"/api/chats/{chat['id']}/promote", json={}))

    _edit_message(store, chat["id"], 1, nfd)
    second = _run(asgi_client.post(f"/api/chats/{chat['id']}/promote", json={}))

    assert second.json()["task_ids"] != first.json()["task_ids"]


# --------------------------------------------------------------------------
# Selection, lifecycle and execution mode.
# --------------------------------------------------------------------------


def test_single_item_promote_creates_exactly_one_card(
    asgi_client: httpx.AsyncClient,
    store: SqliteStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_promotion_fakes(monkeypatch)
    chat = _run(asgi_client.post("/api/chats", json={"title": "Selection"})).json()
    _say(
        store,
        chat["id"],
        "Please rebuild the billing exporter.",
        "Also migrate the auth service to OIDC.",
        "Finally, write the launch runbook.",
    )
    before = _task_count(store)

    response = _run(
        asgi_client.post(f"/api/chats/{chat['id']}/promote_item", json={"item_index": 1})
    )

    assert response.status_code == 201
    assert _task_count(store) == before + 1
    assert response.json()["task"]["title"] == "Also migrate the auth service to OIDC."
    assert _org(store, response.json()["task_id"])["promoted_source_message_index"] == 1


def test_single_item_promote_leaves_the_chat_open(
    asgi_client: httpx.AsyncClient,
    store: SqliteStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Promoting one ask out of a live conversation must not close it (B8)."""
    _install_promotion_fakes(monkeypatch)
    chat = _run(asgi_client.post("/api/chats", json={"title": "Still talking"})).json()
    _say(
        store,
        chat["id"],
        "Please rebuild the billing exporter.",
        "Also migrate the auth service to OIDC.",
    )

    _run(asgi_client.post(f"/api/chats/{chat['id']}/promote_item", json={"item_index": 0}))

    assert _chat_status(store, chat["id"]) == "active"
    assert (
        store._connection.execute(
            "SELECT promoted_at FROM chats WHERE id = ?", (chat["id"],)
        ).fetchone()["promoted_at"]
        is None
    )


def test_thread_promote_closes_the_chat(
    asgi_client: httpx.AsyncClient,
    store: SqliteStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Promoting the WHOLE thread is the operator saying the conversation is done."""
    _install_promotion_fakes(monkeypatch)
    chat = _create_chat(asgi_client, store)

    _run(asgi_client.post(f"/api/chats/{chat['id']}/promote", json={}))

    assert _chat_status(store, chat["id"]) == "promoted"


def test_repromote_idempotent(
    asgi_client: httpx.AsyncClient,
    store: SqliteStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_promotion_fakes(monkeypatch)
    chat = _create_chat(asgi_client, store)

    first = _run(
        asgi_client.post(f"/api/chats/{chat['id']}/promote_item", json={"item_index": 0})
    )
    assert first.status_code == 201
    count_after_first = _task_count(store)
    second = _run(
        asgi_client.post(f"/api/chats/{chat['id']}/promote_item", json={"item_index": 0})
    )

    assert second.status_code == 201
    assert _task_count(store) == count_after_first
    assert second.json()["task_id"] == first.json()["task_id"]


def test_promoted_card_carries_provenance_on_readback(
    asgi_client: httpx.AsyncClient,
    store: SqliteStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_promotion_fakes(monkeypatch)
    chat = _create_chat(asgi_client, store)
    response = _run(
        asgi_client.post(f"/api/chats/{chat['id']}/promote_item", json={"item_index": 0})
    )

    provenance = _org(store, response.json()["task_id"])

    assert provenance["chat_id"] == chat["id"]
    assert provenance["parent_task_id"] == chat["board_task_id"]
    assert provenance["promoted"] is True
    assert provenance["promoted_source_message_index"] == 0
    assert len(str(provenance["promoted_item_key"])) == 64, "sha256 hex identity"


def test_promoted_card_not_running_by_default(
    asgi_client: httpx.AsyncClient,
    store: SqliteStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    _install_promotion_fakes(monkeypatch, calls)
    chat = _create_chat(asgi_client, store)
    response = _run(
        asgi_client.post(f"/api/chats/{chat['id']}/promote_item", json={"item_index": 0})
    )
    task_id = response.json()["task_id"]
    status = store._connection.execute(
        "SELECT status FROM board_tasks WHERE id = ?", (task_id,)
    ).fetchone()["status"]

    assert calls[0]["project_id"] is None
    assert calls[0]["execute"] == "readonly"
    assert status == "open"


def test_thread_promote_keeps_promoting_all_items(
    asgi_client: httpx.AsyncClient,
    store: SqliteStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_promotion_fakes(monkeypatch)
    chat = _run(asgi_client.post("/api/chats", json={"title": "All items"})).json()
    _say(
        store,
        chat["id"],
        "Please rebuild the billing exporter.",
        "Also migrate the auth service to OIDC.",
    )
    before = _task_count(store)

    response = _run(asgi_client.post(f"/api/chats/{chat['id']}/promote", json={}))

    assert response.status_code == 201
    assert _task_count(store) == before + 2
    assert len(response.json()["task_ids"]) == 2


def test_chat_send_with_intent_logs_agreement(
    asgi_client: httpx.AsyncClient,
    store: SqliteStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that chat send carrying both suggested_intent and chosen_intent
    does not error (the intent logging path works with real values from Lane C)."""

    # This test owns the intent-event seam, not native session execution.  Keep
    # the route real while replacing only the dispatch boundary, as the rest of
    # this module does, so the assertion never depends on host sandbox support.
    monkeypatch.setattr("omniagentos.api.routes.chats.dispatch_spec", _fake_dispatch())

    # Create a chat
    chat_response = _run(
        asgi_client.post(
            "/api/chats",
            json={
                "title": "Intent Agreement Test",
                "meta": {
                    "suggested_intent": "loop",
                    "chosen_intent": "project",
                }
            }
        )
    )
    assert chat_response.status_code == 201
    chat = chat_response.json()

    # Send a message with the same intent meta (this is what Lane C now sends)
    send_response = _run(
        asgi_client.post(
            f"/api/chats/{chat['id']}/messages",
            json={
                "content": "Let's make a project plan.",
                "meta": {
                    "suggested_intent": "loop",
                    "chosen_intent": "project",
                }
            }
        )
    )

    # Verify: no error occurred and the agreement was actually recorded.
    assert send_response.status_code == 201
    events = store._connection.execute(
        "SELECT action, target_type, target_id, payload_json FROM events "
        "WHERE type = ? AND target_id = ?",
        ("chat.intent.agreement", chat["id"]),
    ).fetchall()
    assert len(events) == 1
    event = events[0]
    assert event["action"] == "intent_agreement"
    assert event["target_type"] == "chat"
    assert event["target_id"] == chat["id"]
    payload = json.loads(event["payload_json"])
    assert payload["suggested_intent"] == "loop"
    assert payload["chosen_intent"] == "project"
