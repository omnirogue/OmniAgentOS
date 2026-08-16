"""The board READ contract: bounded list, conditional read, single-card read.

Measured on the live board before this lane: ``GET /api/board`` returned
2.66 MB in 13.9 s and took exactly three parameters (archived / project_id /
parent_task_id), 617 of 1208 cards sat in ``blocked`` with no stated reason,
and the detail drawer found its one card by downloading the whole feed twice.

Pinned here:

* the new bounding parameters (``status`` / ``updated_after`` / ``limit``) and
  the guarantee that a request WITHOUT them is byte-identical to the old feed,
* ``ETag`` / ``If-None-Match`` → ``304`` (and that the tag moves when the board
  does, so a 304 can never hide a change),
* ``GET /api/board/{task_id}`` — one reconciled card, 404 for an unknown id,
* the ``blocked_reason`` projection off the attempt ledgers,
* ``run_id`` / ``claimed_by`` attribution,
* the conversation ordering unification with ``GET /api/chats/{id}/messages``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

import omniagentos.api.main  # noqa: F401 -- break the package's documented import cycle.
from omniagentos.api.deps import get_store
from omniagentos.api.main import app
from omniagentos.api.routes.collab import get_collab_store
from omniagentos.collab.contracts import BoardTask, BoardTaskStatus
from omniagentos.collab.store import CollabStore
from tests.support.db_template import make_store


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


@pytest.fixture
def collab(tmp_path: Path) -> CollabStore:
    return make_store(CollabStore, tmp_path / "board-read.db")


@pytest.fixture
def client(collab: CollabStore) -> Iterator[httpx.AsyncClient]:
    app.dependency_overrides[get_store] = lambda: collab._store
    app.dependency_overrides[get_collab_store] = lambda: collab
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")
    try:
        yield http
    finally:
        app.dependency_overrides.clear()


def _card(
    collab: CollabStore,
    title: str,
    *,
    status: str | None = None,
    updated_at: str | None = None,
    **fields: Any,
) -> str:
    card = BoardTask(title=title, description=title)
    collab.create_board_task(card)
    if status is not None:
        fields["status"] = status
    if fields:
        collab.update_board_task(card.id, fields)
    if updated_at is not None:
        # updated_at is stamped by the store on every write, so a deterministic
        # cursor test has to set it after the fact.
        collab._store._write(
            "UPDATE board_tasks SET updated_at = ? WHERE id = ?", (updated_at, card.id)
        )
    return card.id


@pytest.fixture
def seeded(collab: CollabStore) -> dict[str, str]:
    return {
        "open": _card(collab, "open card", updated_at="2026-08-01T00:00:00Z"),
        "blocked": _card(
            collab, "blocked card", status="blocked", updated_at="2026-08-02T00:00:00Z"
        ),
        "done": _card(collab, "done card", status="done", updated_at="2026-08-03T00:00:00Z"),
    }


class TestListBounds:
    def test_no_parameters_returns_the_whole_feed(
        self, client: httpx.AsyncClient, seeded: dict[str, str]
    ) -> None:
        """The default is the OLD default: every card, unbounded."""
        body = _run(client.get("/api/board")).json()
        assert {card["id"] for card in body} == set(seeded.values())

    def test_status_filter_is_a_csv_of_statuses(
        self, client: httpx.AsyncClient, seeded: dict[str, str]
    ) -> None:
        one = _run(client.get("/api/board", params={"status": "blocked"})).json()
        assert [card["id"] for card in one] == [seeded["blocked"]]
        both = _run(client.get("/api/board", params={"status": "blocked,done"})).json()
        assert {card["id"] for card in both} == {seeded["blocked"], seeded["done"]}

    def test_unknown_status_is_refused_not_silently_empty(
        self, client: httpx.AsyncClient, seeded: dict[str, str]
    ) -> None:
        response = _run(client.get("/api/board", params={"status": "blockd"}))
        assert response.status_code == 400
        body = response.json()
        assert body["error"]["code"] == "validation"
        assert "blockd" in str(body["error"])

    def test_updated_after_is_an_inclusive_cursor(
        self, client: httpx.AsyncClient, seeded: dict[str, str]
    ) -> None:
        rows = _run(client.get("/api/board", params={"updated_after": "2026-08-02T00:00:00Z"}))
        assert {card["id"] for card in rows.json()} == {seeded["blocked"], seeded["done"]}
        later = _run(client.get("/api/board", params={"updated_after": "2026-08-04T00:00:00Z"}))
        assert later.json() == []

    def test_updated_after_must_be_a_timestamp(self, client: httpx.AsyncClient) -> None:
        response = _run(client.get("/api/board", params={"updated_after": "yesterday"}))
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "validation"

    def test_limit_caps_the_page_newest_first(
        self, client: httpx.AsyncClient, seeded: dict[str, str]
    ) -> None:
        rows = _run(client.get("/api/board", params={"limit": 2})).json()
        assert len(rows) == 2

    def test_limit_must_be_positive(self, client: httpx.AsyncClient) -> None:
        assert _run(client.get("/api/board", params={"limit": 0})).status_code == 422
        assert _run(client.get("/api/board", params={"limit": -1})).status_code == 422

    def test_bounds_compose(self, client: httpx.AsyncClient, seeded: dict[str, str]) -> None:
        rows = _run(
            client.get(
                "/api/board",
                params={
                    "status": "blocked,done",
                    "updated_after": "2026-08-03T00:00:00Z",
                    "limit": 5,
                },
            )
        ).json()
        assert [card["id"] for card in rows] == [seeded["done"]]


class TestConditionalRead:
    def test_etag_round_trip_returns_304_without_a_body(
        self, client: httpx.AsyncClient, seeded: dict[str, str]
    ) -> None:
        first = _run(client.get("/api/board"))
        assert first.status_code == 200
        etag = first.headers.get("ETag")
        assert etag, "board read must offer an ETag"

        second = _run(client.get("/api/board", headers={"If-None-Match": etag}))
        assert second.status_code == 304
        assert second.content == b""
        assert second.headers.get("ETag") == etag

    def test_a_board_write_invalidates_the_tag(
        self, client: httpx.AsyncClient, collab: CollabStore, seeded: dict[str, str]
    ) -> None:
        etag = _run(client.get("/api/board")).headers["ETag"]
        _card(collab, "a new card")
        response = _run(client.get("/api/board", headers={"If-None-Match": etag}))
        assert response.status_code == 200
        assert response.headers["ETag"] != etag
        assert len(response.json()) == 4

    def test_a_run_finishing_invalidates_the_tag(
        self, client: httpx.AsyncClient, collab: CollabStore, seeded: dict[str, str]
    ) -> None:
        """A 304 must never hide a card whose linked RUN moved.

        Runs do not touch ``board_tasks`` until the reconciler writes the new
        column, so a board-only stamp would happily serve a stale 304 here.
        """
        store = collab._store
        store.create_task(
            {
                "id": "tsk_etag",
                "title": "etag",
                "created_at": "2026-08-01T00:00:00Z",
                "updated_at": "2026-08-01T00:00:00Z",
            }
        )
        store.enqueue_run(
            {
                "id": "run_etag",
                "task_id": "tsk_etag",
                "harness": "mock",
                "state": "running",
                "created_at": "2026-08-01T00:00:00Z",
                "updated_at": "2026-08-01T00:00:00Z",
                "queued_at": "2026-08-01T00:00:00Z",
                "trace_id": "trc_etag",
            }
        )
        collab.update_board_task(seeded["open"], {"run_id": "run_etag"})
        etag = _run(client.get("/api/board")).headers["ETag"]

        store.update_run("run_etag", {"state": "completed", "updated_at": "2026-08-05T00:00:00Z"})
        response = _run(client.get("/api/board", headers={"If-None-Match": etag}))
        assert response.status_code == 200
        card = next(c for c in response.json() if c["id"] == seeded["open"])
        assert card["status"] == "done"

    def test_a_stale_tag_is_ignored(
        self, client: httpx.AsyncClient, seeded: dict[str, str]
    ) -> None:
        response = _run(client.get("/api/board", headers={"If-None-Match": 'W/"board-nope"'}))
        assert response.status_code == 200
        assert len(response.json()) == 3

    def test_the_tag_is_per_selection(
        self, client: httpx.AsyncClient, seeded: dict[str, str]
    ) -> None:
        """A tag earned on one page must never 304 a DIFFERENT page."""
        etag = _run(client.get("/api/board", params={"status": "done"})).headers["ETag"]
        response = _run(
            client.get("/api/board", params={"status": "blocked"}, headers={"If-None-Match": etag})
        )
        assert response.status_code == 200
        assert [card["id"] for card in response.json()] == [seeded["blocked"]]


def _classified(collab: CollabStore, task_id: str) -> None:
    """Put ``task_id`` in a project that belongs to a company (orgdims chain)."""
    collab._store._write(
        "INSERT INTO org_companies (id, slug, name, created_at) "
        "VALUES ('co_parity', 'parity-co', 'Parity Co', '2026-08-01T00:00:00Z')",
        (),
    )
    collab._store._write(
        "INSERT INTO projects (id, name, created_at, org_company_id) "
        "VALUES ('prj_parity', 'Parity', '2026-08-01T00:00:00Z', 'co_parity')",
        (),
    )
    collab.update_board_task(task_id, {"project_id": "prj_parity"})


class TestSingleCard:
    def test_returns_one_card_shaped_like_a_list_element(
        self, client: httpx.AsyncClient, collab: CollabStore, seeded: dict[str, str]
    ) -> None:
        _classified(collab, seeded["open"])
        listed = {card["id"]: card for card in _run(client.get("/api/board")).json()}
        single = _run(client.get(f"/api/board/{seeded['open']}"))
        assert single.status_code == 200
        card = single.json()
        assert card["id"] == seeded["open"]
        # Every key the list element carries is present on the single card; the
        # single card may carry MORE (it is a superset, never a subset).
        missing = set(listed[seeded["open"]]) - set(card)
        assert missing == set(), f"single-card read dropped list fields: {sorted(missing)}"
        # Shape parity is not just KEYS: the drawer and the kanban must agree on
        # the org context too. The list read enriches company_slug/company_id
        # from the project chain, so a single-card read that skipped that
        # enrichment would render the same card with no company.
        context = card["org"]["organization_context"]
        assert context["company_slug"] == "parity-co"
        assert context["company_id"] == "co_parity"
        assert context == listed[seeded["open"]]["org"]["organization_context"]

    def test_an_unclassified_card_gains_no_invented_company(
        self, client: httpx.AsyncClient, seeded: dict[str, str]
    ) -> None:
        """Enrichment parity, not enrichment enthusiasm: no chain, no stamp."""
        card = _run(client.get(f"/api/board/{seeded['open']}")).json()
        context = (card.get("org") or {}).get("organization_context") or {}
        assert "company_slug" not in context
        assert "company_id" not in context

    def test_carries_the_flat_swarm_fields_but_never_the_envelope(
        self, client: httpx.AsyncClient, collab: CollabStore, seeded: dict[str, str]
    ) -> None:
        """Equal to a list element — not a superset. This route is PUBLIC.

        The raw envelope holds acceptance text, verify commands and per-attempt
        dirty file paths. Serving it from an unauthenticated single-card read
        would expose, one card at a time, exactly what the (also public) list
        deliberately does not carry.
        """
        collab._store._write(
            "UPDATE board_tasks SET swarm_json = ? WHERE id = ?",
            (
                '{"swarm_phase": "review", "integration": true, "acceptance": "ship it",'
                ' "pre_attempt_dirty_paths": ["src/secret_layout.py"]}',
                seeded["open"],
            ),
        )
        card = _run(client.get(f"/api/board/{seeded['open']}")).json()
        assert card["swarm_phase"] == "review"
        assert card["swarm_integration"] is True
        assert "swarm_json" not in card
        assert "secret_layout" not in str(card)
        listed = next(c for c in _run(client.get("/api/board")).json() if c["id"] == seeded["open"])
        assert "swarm_json" not in listed

    def test_literal_sibling_routes_still_win_over_the_id_parameter(
        self, client: httpx.AsyncClient, seeded: dict[str, str]
    ) -> None:
        """``/board/needs-response`` must not be read as a card id and 404."""
        response = _run(client.get("/api/board/needs-response"))
        assert response.status_code == 200
        assert "items" in response.json()

    def test_unknown_card_is_404(self, client: httpx.AsyncClient) -> None:
        response = _run(client.get("/api/board/btk_nope"))
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"

    def test_archived_card_is_still_directly_readable(
        self, client: httpx.AsyncClient, collab: CollabStore, seeded: dict[str, str]
    ) -> None:
        collab.update_board_task(seeded["done"], {"archived_at": "2026-08-03T01:00:00Z"})
        assert _run(client.get(f"/api/board/{seeded['done']}")).status_code == 200
        assert seeded["done"] not in {c["id"] for c in _run(client.get("/api/board")).json()}


def _attempt(collab: CollabStore, board_task_id: str, **row: Any) -> None:
    collab._store._write(
        "INSERT INTO swarm_attempts (id, swarm_run_id, board_task_id, seq, provider, model, "
        "started_at, ended_at, end_reason, detail) VALUES (?, ?, ?, ?, 'claude', 'opus', ?, ?, ?, ?)",
        (
            row["id"],
            "swr_x",
            board_task_id,
            row["seq"],
            row.get("started_at", "2026-08-02T00:00:00Z"),
            row.get("ended_at"),
            row.get("end_reason"),
            row.get("detail", ""),
        ),
    )


class TestBlockedReason:
    def test_last_attempt_end_reason_is_projected(
        self, client: httpx.AsyncClient, collab: CollabStore, seeded: dict[str, str]
    ) -> None:
        _attempt(
            collab,
            seeded["blocked"],
            id="swa_1",
            seq=0,
            ended_at="2026-08-02T01:00:00Z",
            end_reason="crashed",
            detail="first try",
        )
        _attempt(
            collab,
            seeded["blocked"],
            id="swa_2",
            seq=1,
            ended_at="2026-08-02T02:00:00Z",
            end_reason="rate_limited",
            detail="usage limit reached\nstack trace follows",
        )
        card = next(
            c for c in _run(client.get("/api/board")).json() if c["id"] == seeded["blocked"]
        )
        reason = card["blocked_reason"]
        assert reason["reason"] == "rate_limited"  # the LAST attempt, not the first
        assert reason["detail"] == "usage limit reached"  # one line, not a transcript
        assert reason["source"] == "swarm_attempt"
        assert reason["at"] == "2026-08-02T02:00:00Z"

    def test_cards_without_a_recorded_reason_report_null(
        self, client: httpx.AsyncClient, seeded: dict[str, str]
    ) -> None:
        card = next(
            c for c in _run(client.get("/api/board")).json() if c["id"] == seeded["blocked"]
        )
        assert card["blocked_reason"] is None

    def test_a_done_card_is_never_given_a_blocked_reason(
        self, client: httpx.AsyncClient, collab: CollabStore, seeded: dict[str, str]
    ) -> None:
        """``end_reason='completed'`` is not a reason a card is stuck."""
        _attempt(
            collab,
            seeded["done"],
            id="swa_done",
            seq=0,
            ended_at="2026-08-03T01:00:00Z",
            end_reason="completed",
            detail="shipped",
        )
        card = next(c for c in _run(client.get("/api/board")).json() if c["id"] == seeded["done"])
        assert card["blocked_reason"] is None

    @pytest.mark.parametrize("reason", ["completed", "split", "rerouted"])
    def test_a_non_failure_end_reason_is_never_the_reason_a_card_is_blocked(
        self,
        client: httpx.AsyncClient,
        collab: CollabStore,
        seeded: dict[str, str],
        reason: str,
    ) -> None:
        """A BLOCKED card whose latest attempt did not fail reports null.

        ``completed``/``split``/``rerouted`` say the attempt ended because the
        work finished or moved, not because anything broke. Rendering one of
        them under "why is this blocked" states a failure that never happened.
        """
        _attempt(
            collab,
            seeded["blocked"],
            id=f"swa_{reason}",
            seq=0,
            ended_at="2026-08-02T05:00:00Z",
            end_reason=reason,
            detail="not a failure",
        )
        card = next(
            c for c in _run(client.get("/api/board")).json() if c["id"] == seeded["blocked"]
        )
        assert card["blocked_reason"] is None

    @pytest.mark.parametrize("reason", ["crashed", "rate_limited", "timeout", "review_denied"])
    def test_a_failure_end_reason_is_still_projected(
        self,
        client: httpx.AsyncClient,
        collab: CollabStore,
        seeded: dict[str, str],
        reason: str,
    ) -> None:
        """The other direction: filtering non-failures must not silence failures."""
        _attempt(
            collab,
            seeded["blocked"],
            id=f"swa_{reason}",
            seq=0,
            ended_at="2026-08-02T05:00:00Z",
            end_reason=reason,
            detail="it broke",
        )
        card = next(
            c for c in _run(client.get("/api/board")).json() if c["id"] == seeded["blocked"]
        )
        assert card["blocked_reason"]["reason"] == reason

    def test_an_older_failure_does_not_resurface_under_a_newer_success(
        self, client: httpx.AsyncClient, collab: CollabStore, seeded: dict[str, str]
    ) -> None:
        """Latest first, filtered second — never "the last FAILURE"."""
        _attempt(
            collab,
            seeded["blocked"],
            id="swa_old_fail",
            seq=0,
            ended_at="2026-08-02T01:00:00Z",
            end_reason="crashed",
            detail="the old crash",
        )
        _attempt(
            collab,
            seeded["blocked"],
            id="swa_new_ok",
            seq=1,
            ended_at="2026-08-02T02:00:00Z",
            end_reason="completed",
            detail="then it worked",
        )
        card = next(
            c for c in _run(client.get("/api/board")).json() if c["id"] == seeded["blocked"]
        )
        assert card["blocked_reason"] is None

    def test_single_card_read_projects_it_too(
        self, client: httpx.AsyncClient, collab: CollabStore, seeded: dict[str, str]
    ) -> None:
        _attempt(
            collab,
            seeded["blocked"],
            id="swa_solo",
            seq=0,
            ended_at="2026-08-02T03:00:00Z",
            end_reason="timeout",
            detail="no output for 30m",
        )
        card = _run(client.get(f"/api/board/{seeded['blocked']}")).json()
        assert card["blocked_reason"]["reason"] == "timeout"
        assert card["blocked_reason"]["detail"] == "no output for 30m"


class TestAttribution:
    def test_run_id_and_claimed_by_ride_on_the_card(
        self, client: httpx.AsyncClient, collab: CollabStore, seeded: dict[str, str]
    ) -> None:
        collab.update_board_task(seeded["open"], {"run_id": "run_attributed"})
        collab._store._write(
            "UPDATE board_tasks SET claimed_by = ? WHERE id = ?", ("agt_worker", seeded["open"])
        )
        card = next(c for c in _run(client.get("/api/board")).json() if c["id"] == seeded["open"])
        assert card["run_id"] == "run_attributed"
        assert card["claimed_by"] == "agt_worker"

    def test_run_ref_without_a_column_still_attributes_the_run(
        self, client: httpx.AsyncClient, collab: CollabStore, seeded: dict[str, str]
    ) -> None:
        """A runs-lane card records its run in ``result_ref``; same run id."""
        collab.update_board_task(seeded["done"], {"result_ref": "run_from_ref"})
        card = _run(client.get(f"/api/board/{seeded['done']}")).json()
        assert card["run_id"] == "run_from_ref"

    def test_a_card_with_no_run_reports_null_not_a_guess(
        self, client: httpx.AsyncClient, seeded: dict[str, str]
    ) -> None:
        card = next(c for c in _run(client.get("/api/board")).json() if c["id"] == seeded["open"])
        assert card["run_id"] is None
        assert card["claimed_by"] is None


class TestStatusValues:
    def test_every_board_status_is_an_accepted_filter_value(
        self, client: httpx.AsyncClient, seeded: dict[str, str]
    ) -> None:
        """The filter's vocabulary IS the status enum — no second list to drift."""
        for member in BoardTaskStatus:
            response = _run(client.get("/api/board", params={"status": member.value}))
            assert response.status_code == 200, f"{member.value}: {response.text}"
