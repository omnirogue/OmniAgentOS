from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from omniagentos.api.main import app
from omniagentos.api.routes import notifications as notifications_route
from omniagentos.notifications.dal import NotificationsDal


@pytest.fixture
def dal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> NotificationsDal:
    real = NotificationsDal(str(tmp_path / "api-notify.db"))
    monkeypatch.setattr(notifications_route, "get_notifications_dal", lambda: real)
    return real


@pytest.fixture
def client() -> Iterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    c = httpx.AsyncClient(transport=transport, base_url="http://testserver")
    try:
        yield c
    finally:
        asyncio.run(c.aclose())


def _set_approval(monkeypatch: pytest.MonkeyPatch, approval: dict | None) -> None:
    """Inject a live sessions reader that returns ``approval`` for any id.

    The route builds lookups via :func:`build_approval_lookup` on
    :func:`_open_sessions_reader` — patch that opener (not a deleted private
    fail-open factory).
    """

    class _Reader:
        def get_approval_by_id(self, _id: str) -> dict | None:
            return approval

    monkeypatch.setattr(notifications_route, "_open_sessions_reader", lambda: _Reader())
    # Board path unused for approval-only rows; keep opener inert/healthy.
    monkeypatch.setattr(notifications_route, "_open_collab_store", lambda: None)


def test_list_empty_returns_empty(dal: NotificationsDal, client: httpx.AsyncClient) -> None:
    response = asyncio.run(client.get("/api/notifications"))
    assert response.status_code == 200
    assert response.json() == []


def test_count_reports_unread(dal: NotificationsDal, client: httpx.AsyncClient) -> None:
    dal.create({"kind": "info", "title": "a"})
    response = asyncio.run(client.get("/api/notifications/count"))
    assert response.status_code == 200
    assert response.json() == {"unread": 1}


def test_list_enriches_pending_approval_target(
    dal: NotificationsDal, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    dal.create(
        {
            "kind": "approval",
            "title": "Approval required",
            "ref_type": "approval",
            "ref_id": "apr_1",
        }
    )
    _set_approval(monkeypatch, {"id": "apr_1", "state": "pending", "action_class": "consequential"})
    response = asyncio.run(client.get("/api/notifications"))
    body = response.json()
    assert len(body) == 1
    target = body[0]["target"]
    assert target["approval_id"] == "apr_1"
    assert target["actionable"] is True
    assert target["resolved"] is False


def test_list_absent_approval_is_not_resolved(
    dal: NotificationsDal, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Live lookup None must not render as favourable resolved on the list path.

    The route calls package :func:`serialize_notification` → :func:`resolve_target`.
    Absence is not a terminal success — ``resolved`` stays False with
    ``state='missing'``.
    """
    dal.create(
        {
            "kind": "approval",
            "title": "Approval required",
            "ref_type": "approval",
            "ref_id": "apr_x",
        }
    )
    _set_approval(monkeypatch, None)  # queue is empty / approval absent
    response = asyncio.run(client.get("/api/notifications"))
    target = response.json()[0]["target"]
    assert target["resolved"] is False, (
        f"absent approval reported resolved={target.get('resolved')}; target={target!r}"
    )
    assert target["actionable"] is False
    assert target.get("state") == "missing"


def test_list_lookup_failure_is_not_resolved(
    dal: NotificationsDal, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HTTP list path: sessions open failure must surface as unavailable.

    Requirement: a downed sessions DB must NOT clear Approve buttons by
    masquerading as measured absence (``state='missing'``). Production must
    call package :func:`build_approval_lookup` + :func:`serialize_notification`
    so open failures become a raising lookup → ``state='unavailable'``.

    The open seam is patched at ``control._open_sessions_reader`` — both the
    rewired route (via :func:`build_approval_lookup`) and the old private
    ``_approval_lookup`` call that function. Behaviour differs:
    - fixed: open error → raising lookup → ``state='unavailable'``
    - unfixed fail-open factory: open error → ``lambda _id: None`` →
      ``state='missing'`` (measured absence) → this assertion fails

    Binding proof: restore private ``_approval_lookup`` / ``_serialize`` and
    this test fails on ``state`` (not on a missing monkeypatch target).
    """
    dal.create(
        {
            "kind": "approval",
            "title": "Approval required",
            "ref_type": "approval",
            "ref_id": "apr_probe",
        }
    )

    def boom_open() -> object:
        raise RuntimeError("sessions database unavailable")

    # Shared seam both old and new production paths open through.
    monkeypatch.setattr(
        "omniagentos.api.routes.control._open_sessions_reader",
        boom_open,
    )

    response = asyncio.run(client.get("/api/notifications"))
    assert response.status_code == 200
    target = response.json()[0]["target"]
    assert target["resolved"] is False, (
        f"lookup failure reported resolved={target.get('resolved')}; target={target!r}"
    )
    assert target["actionable"] is False
    # Distinct from live-miss 'missing': open failure is unmeasured.
    assert target.get("state") == "unavailable", (
        "sessions open failure must report state='unavailable' via "
        "build_approval_lookup + serialize_notification; "
        f"got state={target.get('state')!r} target={target!r}. "
        "If this is 'missing', the route is still using a fail-open factory "
        "that collapses open errors to lambda:None (measured absence)."
    )


def test_list_route_calls_package_serialize_and_builders(
    dal: NotificationsDal, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failing-on-revert: list must invoke the three package entry points.

    A route that still uses private ``_serialize`` / ``_approval_lookup`` /
    ``_board_lookup`` never touches these symbols, so the call counters stay 0.
    """
    calls = {"serialize": 0, "approval": 0, "board": 0}

    real_serialize = notifications_route.serialize_notification
    real_approval = notifications_route.build_approval_lookup
    real_board = notifications_route.build_board_lookup

    def wrap_serialize(*args: object, **kwargs: object) -> object:
        calls["serialize"] += 1
        return real_serialize(*args, **kwargs)

    def wrap_approval(*args: object, **kwargs: object) -> object:
        calls["approval"] += 1
        return real_approval(*args, **kwargs)

    def wrap_board(*args: object, **kwargs: object) -> object:
        calls["board"] += 1
        return real_board(*args, **kwargs)

    monkeypatch.setattr(notifications_route, "serialize_notification", wrap_serialize)
    monkeypatch.setattr(notifications_route, "build_approval_lookup", wrap_approval)
    monkeypatch.setattr(notifications_route, "build_board_lookup", wrap_board)
    _set_approval(monkeypatch, {"id": "apr_1", "state": "pending"})

    dal.create(
        {
            "kind": "approval",
            "title": "Approval required",
            "ref_type": "approval",
            "ref_id": "apr_1",
        }
    )
    response = asyncio.run(client.get("/api/notifications"))
    assert response.status_code == 200
    assert calls["serialize"] >= 1, (
        "list path never called serialize_notification — route not rewired"
    )
    assert calls["approval"] >= 1, (
        "list path never called build_approval_lookup — route not rewired"
    )
    assert calls["board"] >= 1, "list path never called build_board_lookup — route not rewired"


def test_unread_filter(dal: NotificationsDal, client: httpx.AsyncClient) -> None:
    a = dal.create({"kind": "info", "title": "a"})
    dal.create({"kind": "info", "title": "b"})
    dal.mark_read(a["id"])
    response = asyncio.run(client.get("/api/notifications?unread=true"))
    titles = [row["title"] for row in response.json()]
    assert titles == ["b"]


def test_mark_read_endpoint(
    dal: NotificationsDal, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_approval(monkeypatch, None)
    row = dal.create({"kind": "info", "title": "a"})
    response = asyncio.run(client.post(f"/api/notifications/{row['id']}/read"))
    assert response.status_code == 200
    assert response.json()["read"] is True
    assert dal.unread_count() == 0


def test_mark_read_missing_is_404(dal: NotificationsDal, client: httpx.AsyncClient) -> None:
    response = asyncio.run(client.post("/api/notifications/ntf_missing/read"))
    assert response.status_code == 404


# --- P1.3 bulk mark-read + actionable count -----------------------------------


def _seed_mixed_feed(dal: NotificationsDal) -> None:
    """A feed shaped like the live one: mostly noise, a few real decisions."""
    dal.create({"kind": "done", "title": "task complete"})
    dal.create({"kind": "info", "title": "fyi"})
    dal.create({"kind": "swarm_failed", "title": "run failed"})
    dal.create({"kind": "approval", "title": "approve me", "ref_type": "approval", "ref_id": "a1"})
    dal.create({"kind": "escalation", "title": "escalated", "ref_type": "session", "ref_id": "s1"})


def test_count_actionable_mode_counts_only_allowlist_kinds(
    dal: NotificationsDal, client: httpx.AsyncClient
) -> None:
    _seed_mixed_feed(dal)
    response = asyncio.run(client.get("/api/notifications/count?actionable=1"))
    assert response.status_code == 200
    body = response.json()
    assert body["unread"] == 5, "the durable feed still reports every unread row"
    assert body["actionable"] == 2, f"actionable badge counted noise: {body!r}"


def test_count_without_actionable_is_unchanged(
    dal: NotificationsDal, client: httpx.AsyncClient
) -> None:
    _seed_mixed_feed(dal)
    response = asyncio.run(client.get("/api/notifications/count"))
    assert response.json() == {"unread": 5}


def test_read_all_marks_everything_and_drops_actionable_count(
    dal: NotificationsDal, client: httpx.AsyncClient
) -> None:
    _seed_mixed_feed(dal)
    response = asyncio.run(client.post("/api/notifications/read-all"))
    assert response.status_code == 200
    assert response.json() == {"updated": 5}
    assert dal.unread_count() == 0
    after = asyncio.run(client.get("/api/notifications/count?actionable=1")).json()
    assert after == {"unread": 0, "actionable": 0}


def test_read_all_kinds_filter_leaves_approvals_outstanding(
    dal: NotificationsDal, client: httpx.AsyncClient
) -> None:
    _seed_mixed_feed(dal)
    response = asyncio.run(
        client.post("/api/notifications/read-all", json={"kinds": ["done", "info"]})
    )
    assert response.json() == {"updated": 2}
    after = asyncio.run(client.get("/api/notifications/count?actionable=1")).json()
    assert after == {"unread": 3, "actionable": 2}


def test_read_all_is_idempotent(dal: NotificationsDal, client: httpx.AsyncClient) -> None:
    _seed_mixed_feed(dal)
    asyncio.run(client.post("/api/notifications/read-all"))
    second = asyncio.run(client.post("/api/notifications/read-all"))
    assert second.json() == {"updated": 0}


def test_read_all_preserves_the_original_read_stamp(
    dal: NotificationsDal, client: httpx.AsyncClient
) -> None:
    row = dal.create({"kind": "info", "title": "already read"})
    first = dal.mark_read(row["id"])
    assert first is not None
    asyncio.run(client.post("/api/notifications/read-all"))
    again = dal.get(row["id"])
    assert again is not None
    assert again["read_at"] == first["read_at"]


def test_read_all_rejects_unknown_kind(dal: NotificationsDal, client: httpx.AsyncClient) -> None:
    dal.create({"kind": "info", "title": "a"})
    response = asyncio.run(client.post("/api/notifications/read-all", json={"kinds": ["nonsense"]}))
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_kind"
    assert dal.unread_count() == 1, "a rejected request must not mark anything read"


def test_read_all_empty_kinds_marks_nothing(
    dal: NotificationsDal, client: httpx.AsyncClient
) -> None:
    """An explicit empty filter is a restriction, never a silent 'everything'."""
    _seed_mixed_feed(dal)
    response = asyncio.run(client.post("/api/notifications/read-all", json={"kinds": []}))
    assert response.json() == {"updated": 0}
    assert dal.unread_count() == 5


@pytest.mark.real_auth
def test_read_all_requires_the_session_token(
    dal: NotificationsDal,
    client: httpx.AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bulk mutation inherits the app-level deny-by-default gate.

    Nothing route-local enforces this, so it is pinned here: a new mutating
    route must not be reachable without the local session token.
    """
    from omniagentos.sessions import token

    monkeypatch.setattr(token, "TOKEN_PATH", tmp_path / "sessions-token")
    dal.create({"kind": "info", "title": "a"})

    response = asyncio.run(client.post("/api/notifications/read-all"))
    assert response.status_code == 401
    assert dal.unread_count() == 1, "an unauthenticated sweep must change nothing"

    authorized = asyncio.run(
        client.post(
            "/api/notifications/read-all",
            headers={"X-Session-Token": token.load_or_create_token()},
        )
    )
    assert authorized.status_code == 200
    assert dal.unread_count() == 0
