"""A dedicated Next route file shadows the catch-all, and nothing checked it.

``dashboard/src/app/api/[...path]/route.ts`` is not the only thing that decides
whether a browser read carries the local session token. A more-specific
``route.ts`` shadows the catch-all **for every method on that path** -- a fact
this codebase learned in production and wrote down in
``dashboard/src/app/api/pause/route.ts``:

    "this file being more specific than the `api/[...path]` catch-all means
    Next.js resolves every method for `/api/pause` to THIS route, not the
    catch-all -- an unhandled GET 405s instead of falling through. PauseControl
    polls GET /api/pause on every page ... the header showed a bare `405: Method
    Not Allowed` on every load until this GET was added."

So a dedicated GET handler makes its OWN token decision, by calling either
``proxyRead`` (attaches the token) or ``proxyPublicRead`` (does not), and
``isAuthorizedReadPath`` never participates. That decision was unpinned in both
directions:

  * If the upstream route is token-gated and the dedicated file relays it
    tokenless, the browser gets a 401 it cannot recover from -- by SEC-005
    design it never holds the token.

  * Worse, and silent: every prefix in ``AUTHORIZED_READ_PREFIXES`` is gated
    today only because it falls THROUGH to the catch-all. Adding a dedicated
    ``route.ts`` for one of those paths -- the pattern this codebase actively
    encourages, five such files already, and ``memlife/queue/route.ts`` written
    specifically to pre-empt a future shadow -- silently drops the token from a
    read that works today.

WHY DISCOVERY AND NOT A LIST. The three dedicated GET routes that exist today
are found by walking ``dashboard/src/app/api``, not named here. A hand-written
list of them has exactly the failure mode of the omission it is meant to catch
(the operator, #38/#52): the next dedicated route added is the one the list would miss,
and it is precisely the one that matters.

Complements ``tests/api/test_auth_posture.py`` (which knows nothing about the
dashboard) from the other side: that file asks whether a route is gated, this
one asks whether the dashboard's own handler for it agrees.
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
_APP_API = _REPO_ROOT / "dashboard" / "src" / "app" / "api"
_CATCH_ALL_DIR = "[...path]"
_CATCH_ALL_ROUTE = _APP_API / _CATCH_ALL_DIR / "route.ts"

# Never a real id: a gated route must refuse before any id is looked up.
_PROBE_ID = "dedicated-route-gate-probe"

_GET_EXPORT = re.compile(r"^export async function GET", re.MULTILINE)
_DYNAMIC_SEGMENT = re.compile(r"^\[.+\]$")

# Next-only routes with no FastAPI counterpart to compare against. Named rather
# than filtered quietly -- a silent exclusion in an inventory test reads as
# coverage it does not have.
#
#   /api/auth/login  mints the signed browser credential from a Caddy-injected
#                    identity and never proxies upstream. It guards itself with
#                    requireTrustedHop; app/api/auth/login/route.ts owns that,
#                    and serverProxy.trustedHop.test.ts pins it.
_NEXT_ONLY = frozenset({"/api/auth/login"})


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _upstream_template(route_dir: Path) -> str:
    """The FastAPI path template a dedicated route file proxies to.

    ``app/api/board/[id]/files/reveal`` -> ``/api/board/{id}/files/reveal``.
    """
    parts = route_dir.relative_to(_APP_API).parts
    rendered = [
        f"{{{part.strip('[]')}}}" if _DYNAMIC_SEGMENT.match(part) else part for part in parts
    ]
    return "/api/" + "/".join(rendered)


def _probe_url(template: str) -> str:
    return re.sub(r"\{[^}]+\}", _PROBE_ID, template)


def _dedicated_get_routes() -> list[tuple[str, Path, bool]]:
    """(upstream template, route file, sends_token) for every dedicated GET.

    ``sends_token`` is read from which helper the file calls: ``proxyRead``
    attaches the session token, ``proxyPublicRead`` does not.
    """
    found: list[tuple[str, Path, bool]] = []
    for route_file in sorted(_APP_API.rglob("route.ts")):
        if _CATCH_ALL_DIR in route_file.parts:
            continue
        source = route_file.read_text(encoding="utf-8")
        if not _GET_EXPORT.search(source):
            continue
        template = _upstream_template(route_file.parent)
        if template in _NEXT_ONLY:
            continue
        # proxyRead( and proxyPublicRead( are distinct calls; the former is a
        # substring of neither, so test for the tokenless one first.
        sends_token = "proxyRead(" in source and "proxyPublicRead(" not in source
        found.append((template, route_file, sends_token))
    return found


def _authorized_read_prefixes() -> frozenset[str]:
    """``AUTHORIZED_READ_PREFIXES`` as the catch-all actually declares it."""
    source = _CATCH_ALL_ROUTE.read_text(encoding="utf-8")
    block = re.search(
        r"const AUTHORIZED_READ_PREFIXES = new Set\(\[(.*?)\]\);", source, re.DOTALL
    )
    assert block, (
        f"could not find AUTHORIZED_READ_PREFIXES in {_CATCH_ALL_ROUTE}; the mirror "
        "moved and this test is no longer reading the live allowlist"
    )
    entries: set[str] = set()
    for line in block.group(1).splitlines():
        stripped = line.strip()
        if stripped.startswith("//") or stripped.startswith("*"):
            continue
        entries.update(re.findall(r'"([^"]+)"', stripped))
    assert entries, "parsed an EMPTY AUTHORIZED_READ_PREFIXES -- refusing to pass vacuously"
    return frozenset(entries)


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

    Both answer the only question asked here -- did the gate stop this request?
    -- and either way it did not. Letting a handler's own exception escape would
    mark a PUBLIC route as a failure, the opposite of the truth.
    """
    try:
        return int(_run(client.get(url, headers=headers)).status_code)
    except BaseException:  # noqa: BLE001 -- any escape means the gate admitted us
        return _HANDLER_RAN


def test_there_is_at_least_one_dedicated_get_route_to_check() -> None:
    """Refuse to pass vacuously if the walk stops finding anything.

    Every assertion below is parametrised over the discovered set. If the
    dashboard tree moves and the walk returns nothing, pytest reports a
    contented zero-test pass -- the exact silence this file exists to remove.
    """
    routes = _dedicated_get_routes()
    assert routes, (
        f"found no dedicated GET route files under {_APP_API}. Either the "
        "dashboard tree moved or the discovery above broke; both make every "
        "other test in this file vacuous."
    )


@pytest.mark.parametrize(
    ("template", "route_file", "sends_token"),
    _dedicated_get_routes(),
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_a_dedicated_get_route_attaches_the_token_exactly_when_upstream_gates(
    client: httpx.AsyncClient,
    token_header: dict[str, str],
    template: str,
    route_file: Path,
    sends_token: bool,
) -> None:
    """The dedicated handler's own token decision must match the real gate.

    The gate is DISCOVERED, not declared: gated means "401 without the token and
    something other than 401 with it". That form sees a gate wherever it lives --
    an app-level predicate in ``omniagentos/api/main.py``, a router-level
    ``dependencies=[Depends(_authorized)]``, or a ``verify_token`` inside the
    handler body.
    """
    url = _probe_url(template)
    without = _status_or_handler_ran(client, url, {})
    if without != 401:
        return  # public upstream: relaying it tokenless is correct
    with_token = _status_or_handler_ran(client, url, token_header)
    if with_token == 401:
        return  # gated on something other than the session token

    relative = route_file.relative_to(_REPO_ROOT)
    assert sends_token, (
        f"GET {template} requires the session token, but {relative} relays it "
        "through proxyPublicRead, which sends none. That file is MORE SPECIFIC "
        "than app/api/[...path]/route.ts, so Next.js resolves this path to it and "
        "the catch-all never runs -- adding the prefix to AUTHORIZED_READ_PREFIXES "
        "would not help. Switch this handler to proxyRead. The browser cannot "
        "supply the token itself (SEC-005), so this 401 has no recovery path."
    )


@pytest.mark.parametrize(
    ("template", "route_file", "sends_token"),
    _dedicated_get_routes(),
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_a_dedicated_get_route_never_silently_drops_a_prefix_the_catch_all_gates(
    template: str, route_file: Path, sends_token: bool
) -> None:
    """The reverse direction, and the one that breaks a working read.

    Every entry in ``AUTHORIZED_READ_PREFIXES`` carries the token today only
    because it falls through to the catch-all. A dedicated ``route.ts`` added
    under such a prefix takes the path over and decides for itself; if it
    chooses ``proxyPublicRead`` the read starts 401ing, and a mirror test that
    reads only the catch-all stays green because the prefix is still listed
    there.

    Pure source comparison on purpose: it holds even for a path whose upstream
    gate is unreachable in an offline lane, where the discovery test above
    cannot classify anything.
    """
    first_segment = template.strip("/").split("/")[1:2]
    if not first_segment or first_segment[0] not in _authorized_read_prefixes():
        return
    relative = route_file.relative_to(_REPO_ROOT)
    assert sends_token, (
        f"{relative} serves GET {template}, whose first segment "
        f"'{first_segment[0]}' is in AUTHORIZED_READ_PREFIXES -- the catch-all "
        "would have sent the session token for this read. Because this file is "
        "more specific, Next.js resolves the path here instead and the catch-all "
        "never runs, so choosing proxyPublicRead silently drops the token from a "
        "read that works today. Use proxyRead, or remove the prefix from "
        "app/api/[...path]/route.ts if the read is genuinely public."
    )
