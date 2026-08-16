"""Nothing compared the dashboard's read-gate mirror to the gate it mirrors.

``dashboard/src/app/api/[...path]/route.ts`` decides, per browser read, whether
the Next proxy attaches the local session token (``proxyRead``) or relays the
request without it (``proxyPublicRead``). That decision is a hand-written
transcription of FastAPI's gating rules, kept in ``AUTHORIZED_READ_PREFIXES``
and ``isAuthorizedReadPath``. It is the SEC-005 seam: the browser cannot supply
the token itself, so a route the mirror gets wrong is a hard 401 no caller can
recover from, and there is no fallback path.

Two sides, seventeen-odd hand-copied rules, and — before this file — nothing
that compared them. ``tests/api/test_auth_posture.py`` enumerates every GET
route and requires it to be gated or explicitly public, but it knows nothing
about the dashboard, so it stayed green with the mirror drifted; the
``serverProxy.*.test.ts`` family exercises the proxy helpers against a mocked
upstream and never classifies a route at all.

WHY DISCOVERY AND NOT A LIST. A hand-written list of the gated routes known
today has exactly the failure mode of the omission it is meant to catch (the operator,
#38/#52). So the gated set is DISCOVERED by driving the real app -- each GET
requested with and without a session token, and called gated when the first
answers 401 and the second does not. That form catches a gate wherever it
lives: the app-level predicates in ``omniagentos/api/main.py``, a router-level
``dependencies=[Depends(_authorized)]``, or a ``verify_token`` call inside the
handler itself. The last of those is what drifted -- ``GET
/api/access/tool-search`` gates in its own body, so no static read of
``main.py`` could have found it, and none did.

The dashboard side is likewise read FROM the TypeScript source rather than
restated here: the prefix set is parsed out of the file, and
``test_transcription_still_matches_the_typescript`` fails if the shape of
``isAuthorizedReadPath`` changes underneath the Python transcription below.
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

pytestmark = pytest.mark.real_auth

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CATCH_ALL_ROUTE = _REPO_ROOT / "dashboard" / "src" / "app" / "api" / "[...path]" / "route.ts"

_PATH_PARAM = re.compile(r"\{[^}]+\}")
# Never a real id: a gated route must reject before the id is ever looked up.
_PROBE_ID = "read-gate-mirror-probe"

# ``GET /api/events`` is an SSE stream that by design never ends, so a request
# for it does not return and cannot be classified this way. Named rather than
# filtered quietly -- a silent exclusion in an inventory test reads as coverage
# it does not have. Its posture is settled by other means: it is in
# ``PUBLIC_GETS`` and the dashboard's EventSource reaches it through
# ``proxyPublicRead`` with no token today, and the streams work.
_UNCLASSIFIABLE = frozenset({"/api/events"})


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _mounted_get_paths() -> list[str]:
    """Every GET path template the app mounts.

    From the OpenAPI schema, never ``app.routes``: under FastAPI 0.139 the
    latter is a tree of ``_IncludedRouter`` wrappers with no ``.path``, so
    walking it yields zero routes and a probe over it reads exactly like a
    clean result.
    """
    return sorted(path for path, ops in app.openapi()["paths"].items() if "get" in ops)


def _probe_url(template: str) -> str:
    return _PATH_PARAM.sub(_PROBE_ID, template)


# --------------------------------------------------------------------------
# The dashboard side, read from the TypeScript rather than restated.
# --------------------------------------------------------------------------

_PREFIX_BLOCK = re.compile(
    r"const AUTHORIZED_READ_PREFIXES = new Set\(\[(.*?)\]\);", re.DOTALL
)
_QUOTED = re.compile(r'"([^"]+)"')
# The clauses of isAuthorizedReadPath that are NOT the prefix-set lookup. The
# Python transcription below models exactly these; if a clause is added,
# removed or reshaped in the TypeScript, the count changes and the guard test
# fails rather than letting the transcription drift into a comfortable lie.
_EXPECTED_EXTRA_CLAUSES = 11


def _authorized_read_prefixes() -> frozenset[str]:
    source = _CATCH_ALL_ROUTE.read_text(encoding="utf-8")
    block = _PREFIX_BLOCK.search(source)
    assert block, (
        f"could not find AUTHORIZED_READ_PREFIXES in {_CATCH_ALL_ROUTE}; the "
        "mirror moved and this test is no longer reading the live allowlist"
    )
    # Comment lines inside the Set literal carry prose in quotes ("swarm" is
    # named in several); take only the quoted strings on non-comment lines.
    entries: set[str] = set()
    for line in block.group(1).splitlines():
        stripped = line.strip()
        if stripped.startswith("//") or stripped.startswith("*"):
            continue
        entries.update(_QUOTED.findall(stripped))
    assert entries, "parsed an EMPTY AUTHORIZED_READ_PREFIXES — refusing to pass vacuously"
    return frozenset(entries)


def _isolated_body() -> str:
    """The body of isAuthorizedReadPath with comments stripped."""
    source = _CATCH_ALL_ROUTE.read_text(encoding="utf-8")
    start = source.index("function isAuthorizedReadPath")
    end = source.index("\n}", start)
    body = source[start:end]
    return "\n".join(
        line for line in body.splitlines() if not line.strip().startswith("//")
    )


def _dashboard_sends_token(path_segments: list[str], prefixes: frozenset[str]) -> bool:
    """Transcription of ``isAuthorizedReadPath`` (segments exclude the ``api`` root)."""
    if not path_segments:
        return False
    if path_segments[0] in prefixes:
        return True
    length = len(path_segments)
    return (
        (length >= 3 and path_segments[0] == "projects" and path_segments[2] == "files")
        or (length >= 3 and path_segments[0] == "board" and path_segments[2] == "files")
        or path_segments[0] == "sessions"
        or path_segments[0] == "mounts"
        or (
            length == 2
            and path_segments[0] == "access"
            and path_segments[1] in {"servers", "tool-search"}
        )
        or (
            length == 3
            and path_segments[0] == "board"
            and path_segments[2] in {"conversation", "longhaul", "sessions"}
        )
        or (length == 1 and path_segments[0] == "categories")
        or (length == 2 and path_segments[0] == "ledger" and path_segments[1] in {"claims", "tail"})
    )


def _segments(api_path: str) -> list[str]:
    """The segments the Next catch-all sees: everything after ``/api``."""
    return [part for part in _probe_url(api_path).strip("/").split("/") if part][1:]


# --------------------------------------------------------------------------


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


_HANDLER_RAN = 599
"""Not a real status: the sentinel for "this request reached the handler"."""


def _status_or_handler_ran(client: httpx.AsyncClient, url: str, headers: dict[str, str]) -> int:
    """The status, or ``_HANDLER_RAN`` when the handler ran and raised.

    Both are the SAME answer to the only question this test asks — did the gate
    stop this request? — and either way it did not. The distinction matters
    because several read handlers reach past the ``:memory:`` fixture: some want
    a populated store, and ``/api/jira/*`` opens a live HTTP connection, which
    raises in the offline lane. Letting those exceptions escape would mark a
    PUBLIC route as a mirror failure, which is the opposite of the truth and
    exactly the misclassification this file exists to prevent elsewhere.
    """
    try:
        return int(_run(client.get(url, headers=headers)).status_code)
    except BaseException:  # noqa: BLE001 — any escape means the gate admitted us
        return _HANDLER_RAN


def test_transcription_still_matches_the_typescript() -> None:
    """Guard the transcription above against a silent reshape of the source.

    Without this, someone adding a ninth clause to ``isAuthorizedReadPath``
    gets a mirror test that keeps passing while modelling the old function --
    the same failure the mirror itself has.
    """
    body = _isolated_body()
    clauses = body.count("||")
    assert clauses == _EXPECTED_EXTRA_CLAUSES, (
        f"isAuthorizedReadPath now has {clauses} non-prefix clauses, not "
        f"{_EXPECTED_EXTRA_CLAUSES}. Update _dashboard_sends_token in this file "
        "to model the new shape, then update _EXPECTED_EXTRA_CLAUSES."
    )
    # The clauses the transcription models, by the literals they turn on.
    for literal in ('"projects"', '"board"', '"sessions"', '"mounts"', '"access"',
                    '"servers"', '"tool-search"', '"categories"', '"ledger"',
                    '"claims"', '"tail"'):
        assert literal in body, f"{literal} vanished from isAuthorizedReadPath"


@pytest.mark.parametrize("path", [p for p in _mounted_get_paths() if p not in _UNCLASSIFIABLE])
def test_every_token_gated_get_is_mirrored_by_the_dashboard_proxy(
    client: httpx.AsyncClient, token_header: dict[str, str], path: str
) -> None:
    """A GET that needs the token must be one the dashboard proxy attaches it to.

    The gate is DISCOVERED, not declared: gated means "401 without the token and
    something other than 401 with it". A route that 401s in both directions is
    not classified here -- that is a broken route, not a mirror question, and
    ``test_auth_posture.py`` owns it.
    """
    url = _probe_url(path)
    without = _status_or_handler_ran(client, url, {})
    if without != 401:
        return  # public: the dashboard may relay it tokenless, which it does
    with_token = _status_or_handler_ran(client, url, token_header)
    if with_token == 401:
        return  # gated on something other than the session token

    prefixes = _authorized_read_prefixes()
    assert _dashboard_sends_token(_segments(path), prefixes), (
        f"GET {path} requires the session token, but "
        "dashboard/src/app/api/[...path]/route.ts routes it to proxyPublicRead, "
        "which sends none — so a browser read of it is a 401 no caller can fix "
        "(the browser never holds the token, by SEC-005 design). Add it to "
        "AUTHORIZED_READ_PREFIXES or isAuthorizedReadPath there."
    )


# --------------------------------------------------------------------------
# The converse direction: mirrored => gated.
#
# The test above is one-directional (gated => mirrored), and that blind side is
# load-bearing: a namespace listed in AUTHORIZED_READ_PREFIXES pre-authorises
# EVERY future route under it, so a new ``GET /api/<prefix>/<anything>`` is
# proxied with the operator's session token the moment it is mounted — no
# route.ts edit, no test turning red. The boot-receipt lane's ``/api/ops`` entry
# is exactly that shape (Class-A review B1), so the converse is asserted here
# rather than trusted.
#
# "Mirrored" means the proxy attaches a credential the browser is not otherwise
# entitled to hold. If the route is in fact PUBLIC that is not a disclosure — the
# response was readable anyway — but it IS an unreviewed grant, so each case is
# named with its reason instead of being tolerated in bulk.
# --------------------------------------------------------------------------

#: Mirrored GETs that are deliberately NOT token-gated server-side. Adding a line
#: here is a review decision: it asserts the route serves nothing a tokenless
#: caller could not already read, and that the proxy attaches the token only
#: because SIBLINGS under the same clause need it.
#:
#: It is EMPTY as of this test's introduction — every currently mirrored GET is
#: genuinely gated — which is what makes the assertion below a real invariant
#: rather than a rubber stamp. Keep it that way: a growing list is the signal
#: that prefix entries are outrunning the gates they are supposed to mirror.
_MIRRORED_BUT_PUBLIC: dict[str, str] = {}


@pytest.mark.parametrize("path", [p for p in _mounted_get_paths() if p not in _UNCLASSIFIABLE])
def test_mirrored_get_that_is_not_gated_is_an_overbroad_prefix(
    client: httpx.AsyncClient, path: str
) -> None:
    """A GET the proxy sends the operator's token to must be one that NEEDS it.

    Named for what a failure MEANS: the prefix in ``AUTHORIZED_READ_PREFIXES`` is
    broader than the set of routes actually gated behind it.
    """
    prefixes = _authorized_read_prefixes()
    if not _dashboard_sends_token(_segments(path), prefixes):
        return  # not mirrored: the forward direction above owns this path
    if path in _MIRRORED_BUT_PUBLIC:
        return
    without = _status_or_handler_ran(client, _probe_url(path), {})
    assert without == 401, (
        f"GET {path} is mirrored by dashboard/src/app/api/[...path]/route.ts — the "
        f"proxy attaches the operator's session token to it — but it answered "
        f"{without} to a caller with NO token, so nothing gates it server-side. "
        "Either gate it (a route-level dependency, or its namespace in "
        "_GATED_READ_NAMESPACES in omniagentos/api/main.py), narrow the route.ts "
        "clause so it stops pre-authorising this path, or name it in "
        "_MIRRORED_BUT_PUBLIC above with the reason it is safe."
    )
