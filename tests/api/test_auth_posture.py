"""Mechanical auth-posture inventory over EVERY GET route the app mounts (I-13).

Auth on this control plane is deny-by-default for mutations and allow-by-default
for reads: ``require_session_token`` gates every POST/PUT/PATCH/DELETE, while a
GET is public unless something explicitly gates it (an app-level predicate /
namespace in ``omniagentos/api/main.py``, or a route/router-level
``_authorized``/``_require_token`` dependency). That default is why
``GET /api/accounts`` served account emails, ``config_dir`` paths, ``auth_type``
and status detail to an unauthenticated caller on the live API: nothing was
wrong, nobody had said the word "gated", and there was no registry that would
have noticed.

This file is that registry, expressed as a test rather than as code the gate
consults, so it stays true no matter WHICH mechanism gates a route:

    every GET route is either named in PUBLIC_GETS below, or it answers 401
    to a request that carries no session token.

A new sensitive GET therefore fails loudly here the moment it is mounted. The
failure is resolved in exactly one of two ways, and both are deliberate: gate
the route, or add its path to ``PUBLIC_GETS``. **Adding a path to PUBLIC_GETS is
a security decision** — it asserts the response carries nothing a confined agent
should not read off the loopback interface (no credentials, no secrets, no
filesystem layout, no fleet/recon inventory). It is not a formality to make a
red test green.

``PUBLIC_GETS`` is seeded from the posture that shipped, not from an ideal one.
Several entries are credential- or filesystem-adjacent and stay public only
because the dashboard fetches them WITHOUT a token: browser reads go
same-origin through ``dashboard/src/app/api/[...path]/route.ts``, which attaches
the session token only for the prefixes in its own ``AUTHORIZED_READ_PREFIXES``
allowlist and relays everything else via ``proxyPublicRead``. Gating one of
those without the matching dashboard change 401s a live page, so tightening them
is a paired change, not a one-line edit here.

Only routes that are EXPECTED to be gated are actually requested, and a gated
route rejects at the dependency layer, so no handler runs while this file is
green. ``real_auth`` opts out of the suite-wide ``require_session_token`` bypass
in ``tests/conftest.py`` — without it the app-level gate is a no-op and this
whole file would assert nothing.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from omniagentos.api.deps import get_store
from omniagentos.api.main import app
from omniagentos.db.store import SqliteStore
from omniagentos.sessions import token
from tests.support.db_template import migrated_db

pytestmark = pytest.mark.real_auth

# Every GET path that is public BY DECISION. Read the module docstring before
# adding one. Sorted; path templates exactly as the app mounts them.
PUBLIC_GETS: frozenset[str] = frozenset(
    {
        "/api/access/agents",
        "/api/access/agents/{agent_id}",
        "/api/access/calls",
        "/api/access/capabilities",
        "/api/access/log",
        # /api/access/tool-search is NOT here: it verifies the session token in
        # its own handler (routes/access.py), so it is gated, and listing it as
        # public was a bookkeeping error nothing could detect until
        # test_public_allowlist_names_only_routes_that_are_actually_public
        # below started requesting these entries instead of skipping them.
        "/api/alerts",
        "/api/alerts/count",
        "/api/approvals",
        "/api/artifacts/preview",
        "/api/artifacts/preview/raw",
        "/api/banking",
        "/api/board",
        "/api/board/needs-response",
        # Single-card read of a row the (public) board list already returns.
        # Its payload is asserted EQUAL to a list element — the raw swarm_json
        # envelope (acceptance text, verify commands, per-attempt dirty file
        # paths) is dropped server-side precisely so this route exposes nothing
        # the list does not. See tests/intake/test_board_read_contract.py::
        # TestSingleCard::test_carries_the_flat_swarm_fields_but_never_the_envelope.
        "/api/board/{task_id}",
        "/api/board/{task_id}/eta",
        "/api/briefings",
        "/api/briefings/latest",
        "/api/budgets",
        "/api/cbm/allocations/{allocation_id}",
        "/api/cbm/allocations/{allocation_id}/escalations",
        "/api/cbm/health",
        "/api/cbm/leaderboard",
        "/api/cbm/rungs",
        "/api/chats",
        "/api/chats/folders",
        "/api/chats/{chat_id}",
        "/api/chats/{chat_id}/messages",
        "/api/collab/agents",
        "/api/collab/channels",
        "/api/collab/channels/{channel_id}/messages",
        "/api/collab/messages/search",
        "/api/comms/messages",
        "/api/comms/messages/{message_id}",
        "/api/comms/sources",
        "/api/company-goals",
        "/api/company-goals/{goal_id}",
        "/api/company-goals/{goal_id}/jira-links",
        "/api/connections",
        "/api/dashboard/today",
        "/api/disciplines",
        "/api/employees",
        "/api/events",
        "/api/goals",
        # Same posture decision as the sibling single-goal read below: no
        # credentials, filesystem, or fleet/recon data, just this goal's own
        # metric readings and sustain progress across its own subtree.
        "/api/goals/tree/{goal_id}",
        "/api/goals/{goal_id}",
        "/api/graph/health",
        "/api/graph/runs",
        "/api/graph/runs/{run_id}",
        "/api/graph/runs/{run_id}/ready",
        "/api/graph/runs/{run_id}/view",
        "/api/graph/templates",
        "/api/grok/decision-center",
        "/api/grok/grants",
        "/api/grok/health",
        "/api/grok/interactions",
        "/api/grok/recommended-next-action",
        "/api/health",
        "/api/intake/plan/{job_id}",
        "/api/jira/health",
        "/api/jira/projects",
        "/api/jira/projects/{key}/statuses",
        "/api/knowledge/facts/{fact_id}",
        "/api/knowledge/graph",
        "/api/knowledge/recalls",
        "/api/knowledge/search",
        "/api/knowledge/stats",
        "/api/lab/champions",
        "/api/lab/disciplines",
        "/api/lab/experiments",
        "/api/lab/experiments/{experiment_id}",
        "/api/lab/jobs/{job_id}",
        "/api/lab/leaderboard",
        "/api/lab/playbook",
        "/api/lab/surfaces",
        "/api/lab/surfaces/{surface_id}",
        "/api/lab/tournaments",
        "/api/lab/tournaments/{tournament_id}",
        "/api/lab/vault/note",
        "/api/lab/vault/search",
        "/api/lab/vault/tree",
        "/api/ledger",
        "/api/memlife/queue",
        "/api/metacog/artifacts",
        "/api/metacog/artifacts/{artifact_id}",
        "/api/metacog/checkpoints/{checkpoint_id}",
        "/api/metacog/health",
        "/api/metacog/memories",
        "/api/metacog/memory",
        "/api/models",
        "/api/models/formation",
        "/api/notifications",
        "/api/notifications/count",
        "/api/orgdims/agents/grok",
        "/api/orgdims/board/{task_id}",
        "/api/orgdims/companies",
        "/api/orgdims/health",
        "/api/orgdims/loops",
        "/api/orgdims/objects/{object_type}",
        "/api/orgdims/views/matrix",
        "/api/orgdims/views/portfolio",
        "/api/orgdims/workstreams",
        "/api/pause",
        "/api/projects",
        "/api/projects/portfolio",
        "/api/projects/tree",
        "/api/projects/{project_id}",
        "/api/projects/{project_id}/activity",
        "/api/projects/{project_id}/conversation",
        "/api/pulse/metrics",
        "/api/pulse/series",
        "/api/reflection/proposals",
        "/api/reflection/{id}",
        "/api/revenue",
        "/api/revenue/verticals",
        "/api/routines",
        "/api/routines/engine",
        "/api/routines/runs",
        "/api/routines/{routine_id}",
        "/api/routines/{routine_id}/runs",
        "/api/runs",
        "/api/runs/{run_id}",
        "/api/skills",
        "/api/skills/search",
        "/api/skills/tree",
        "/api/skills/{id}",
        "/api/suggestions",
        "/api/system-jobs",
        "/api/tasks",
        "/api/tasks/{task_id}",
        "/api/tasks/{task_id}/conversation",
        "/api/updates",
        "/api/voice/audio/{artifact_id}",
        "/api/voice/providers",
    }
)

# The credential-adjacent roster this file was written for. Called out by name
# so I-13 has a named regression, not just an inventory row.
_ACCOUNT_ROSTER_GETS = ("/api/accounts", "/api/accounts/usage")

_PATH_PARAM = re.compile(r"\{[^}]+\}")
# Never a real id: a gated route must reject before the id is ever looked up.
_PROBE_ID = "auth-posture-probe"

# The one PUBLIC_GETS entry the reverse check below cannot request: /api/events
# is an SSE stream that by design never ends, so `client.get` on it does not
# return. Named here rather than filtered silently, because a quiet exclusion in
# an inventory test reads as coverage it does not have. It is safe to exclude on
# its own terms: the dashboard's EventSource reaches it through proxyPublicRead
# with no token today and the streams work, which is the same evidence the
# request would have produced.
_UNREQUESTABLE_PUBLIC_GETS = frozenset({"/api/events"})


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _mounted_get_paths() -> list[str]:
    """Every GET path template the app mounts.

    The OpenAPI schema is the flat inventory — ``app.routes`` is a tree of
    ``_IncludedRouter`` wrappers whose entries carry router-local (unprefixed)
    paths. No route sets ``include_in_schema=False``, so nothing hides here.
    """
    return sorted(path for path, ops in app.openapi()["paths"].items() if "get" in ops)


def _probe_url(template: str) -> str:
    return _PATH_PARAM.sub(_PROBE_ID, template)


@pytest.fixture
def store() -> SqliteStore:
    return SqliteStore(":memory:")


@pytest.fixture
def client(store: SqliteStore) -> Iterator[httpx.AsyncClient]:
    app.dependency_overrides[get_store] = lambda: store
    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")
    try:
        yield c
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def token_header(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, str]:
    monkeypatch.setattr(token, "TOKEN_PATH", tmp_path / "sessions-token")
    return {"X-Session-Token": token.load_or_create_token()}


@pytest.fixture
def isolated_accounts_backend(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The accounts roster on an isolated DB, reading no operator config dir.

    Both roster routes bypass the injected store: they resolve their own
    ``default_db_path()`` and scan real Claude/codex config dirs off disk.
    Same isolation as ``tests/api/test_accounts_pause_api.py``.
    """
    db_path = str(tmp_path / "accounts.db")
    migrated_db(SqliteStore, db_path)
    monkeypatch.setattr("omniagentos.accounts.service.default_db_path", lambda: db_path)
    monkeypatch.setattr("omniagentos.accounts.service.detect_config_dirs", lambda: [])
    monkeypatch.setattr("omniagentos.accounts.usage.detect_config_dirs", lambda: [])
    monkeypatch.setattr("omniagentos.accounts.usage._codex_home", lambda: tmp_path / "none")


@pytest.mark.parametrize("path", _mounted_get_paths())
def test_every_get_route_is_allowlisted_public_or_token_gated(
    client: httpx.AsyncClient, path: str
) -> None:
    if path in PUBLIC_GETS:
        return
    response = _run(client.get(_probe_url(path)))
    assert response.status_code == 401, (
        f"GET {path} answered {response.status_code} without a session token. "
        "Gate it, or — if its response is provably safe to serve unauthenticated "
        "— add it to PUBLIC_GETS in this file and say why in the review."
    )


def test_public_allowlist_names_only_routes_that_exist() -> None:
    """A removed/renamed route must not leave a permanent hole in the allowlist."""
    stale = sorted(PUBLIC_GETS - set(_mounted_get_paths()))
    assert not stale, f"PUBLIC_GETS names GET paths the app no longer mounts: {stale}"


@pytest.mark.parametrize("path", sorted(PUBLIC_GETS - _UNREQUESTABLE_PUBLIC_GETS))
def test_public_allowlist_names_only_routes_that_are_actually_public(
    client: httpx.AsyncClient, path: str
) -> None:
    """A PUBLIC_GETS entry that answers 401 is a mirror the dashboard cannot see.

    The gated direction above returns early for every PUBLIC_GETS entry, so it
    never requests one — which means a route can be listed here as public while
    being gated in its own handler, and nothing anywhere notices. That is not a
    harmless bookkeeping error. This file's docstring records the coupling:
    those entries "stay public only because the dashboard fetches them WITHOUT a
    token", via ``proxyPublicRead`` in ``dashboard/src/app/api/[...path]``. A
    gated route sitting in this list is therefore a live 401 on whichever page
    calls it, and the only registry that could have caught it is this one.

    Found this way: ``GET /api/access/tool-search`` verifies the session token
    inside its handler (``routes/access.py`` — "GATED, unlike its neighbours"),
    so ``main.py``'s app-level predicates never see it and neither did this
    file. Any status other than 401 satisfies this test — a 404/422/500 still
    proves the request reached the handler rather than the gate.
    """
    try:
        status = _run(client.get(_probe_url(path))).status_code
    except BaseException:  # noqa: BLE001
        # The handler ran and raised — the ``:memory:`` store is unpopulated and
        # ``/api/jira/*`` opens a live connection the offline lane refuses. That
        # IS the answer: the request got past the gate, so the route is public,
        # which is what this test asserts. Only the gate's own 401 fails here.
        return
    assert status != 401, (
        f"GET {path} is named in PUBLIC_GETS but answered 401 without a session "
        "token, so it is gated — most likely inside its own handler, where the "
        "app-level predicates in omniagentos/api/main.py cannot see it. Remove "
        "it from PUBLIC_GETS, and add it to AUTHORIZED_READ_PREFIXES / "
        "isAuthorizedReadPath in dashboard/src/app/api/[...path]/route.ts in "
        "the same change, or the dashboard keeps proxying it without a token."
    )


@pytest.mark.parametrize("path", _ACCOUNT_ROSTER_GETS)
def test_account_roster_requires_the_session_token(client: httpx.AsyncClient, path: str) -> None:
    response = _run(client.get(path))
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


@pytest.mark.parametrize("path", _ACCOUNT_ROSTER_GETS)
@pytest.mark.usefixtures("isolated_accounts_backend")
def test_account_roster_is_reachable_with_the_session_token(
    client: httpx.AsyncClient, token_header: dict[str, str], path: str
) -> None:
    """The gate admits a browser-resolved, non-system principal to the handler."""
    response = _run(
        client.get(
            path,
            headers={**token_header, "X-Omni-Authenticated-Principal": "owner@example.test"},
        )
    )
    assert response.status_code == 200, response.text


def test_team_namespace_requires_the_session_token(client: httpx.AsyncClient) -> None:
    response = _run(client.get("/api/team/board"))
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_team_namespace_is_reachable_with_session_token_and_principal(
    client: httpx.AsyncClient, token_header: dict[str, str]
) -> None:
    response = _run(
        client.get(
            "/api/team/board",
            headers={**token_header, "X-Omni-Authenticated-Principal": "owner@example.test"},
        )
    )
    assert response.status_code == 200, response.text
