"""GET-route posture classification helpers shared by the registry test."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx
from starlette.requests import Request

from omniagentos.api.main import (
    _is_employee_transcript_read,
    _is_gated_read_namespace,
    _is_mounts_request,
    _is_project_file_read,
    _is_server_inventory_request,
    _is_session_ledger_read,
    _is_workfs_request,
)
from omniagentos.api.main import app as _app
from tests.api.test_auth_posture import PUBLIC_GETS

GATED = "GATED"
EXPLICIT_PUBLIC = "EXPLICIT-PUBLIC"
UNCLASSIFIED = "UNCLASSIFIED"


@dataclass(frozen=True)
class GetPosture:
    """The declared authentication posture of one OpenAPI GET path."""

    path: str
    classification: str
    gate_predicates: tuple[str, ...] = ()


_GATED_READ_PREDICATES: tuple[tuple[str, Callable[[Request], bool]], ...] = (
    ("_is_project_file_read", _is_project_file_read),
    ("_is_mounts_request", _is_mounts_request),
    ("_is_workfs_request", _is_workfs_request),
    ("_is_server_inventory_request", _is_server_inventory_request),
    ("_is_employee_transcript_read", _is_employee_transcript_read),
    ("_is_session_ledger_read", _is_session_ledger_read),
    ("_is_gated_read_namespace", _is_gated_read_namespace),
)


def request_for_get_path(path: str) -> Request:
    """Make the minimal ASGI request needed by main.py's gate predicates."""
    return Request({"type": "http", "method": "GET", "path": path, "headers": []})


def gated_read_predicates(path: str) -> tuple[str, ...]:
    """Extract the matching app-level gated-read predicates for ``path``.

    The predicates are imported from ``main.py`` rather than mirrored, so this
    registry measures the same path-shape rules used by ``require_session_token``.
    """
    request = request_for_get_path(path)
    return tuple(name for name, predicate in _GATED_READ_PREDICATES if predicate(request))


_PATH_PARAM = re.compile(r"\{[^}]+\}")
_PROBE_ID = "posture-registry-probe"
# ``GET /api/events`` is an SSE stream that never ends, so it cannot be
# requested here. Named, not filtered quietly.
_UNPROBEABLE = frozenset({"/api/events"})


def observed_session_token_gate(path: str) -> bool:
    """Does this GET answer 401 to a request carrying no session token?

    The predicates above see only gates whose rule is a PATH SHAPE, declared in
    ``main.py``. Two other gate mechanisms exist in this app and neither is
    visible to them: a router-level ``dependencies=[Depends(_authorized)]``
    (sessions, swarm, system, filesearch, improvements, org, reliability,
    categories, board files, autonomy) and a ``verify_token`` call inside a
    handler body (``routes/access.py::tool_search``). Every route gated that way
    was therefore reported UNCLASSIFIED — 41 of them are sitting in
    ``unclassified_get_routes_baseline.json`` as if they were unfinished work,
    when in fact they are gated and always were.

    So ask the app instead of reading it. Requesting the route without a token
    answers for all three mechanisms at once and cannot be fooled by a gate
    moving between them.

    CONSERVATIVE BY CONSTRUCTION, deliberately: only a clean 401 reclassifies a
    route. A non-401 status, an exception (an unpopulated store, a route that
    opens a live connection in the offline lane) or an unprobeable stream all
    leave the route exactly where it was. This can therefore only ever SHRINK
    the unclassified set, never grow it — it cannot be used to launder a route
    past the ratchet in ``test_get_posture_registry``.

    Requires the caller to run under ``@pytest.mark.real_auth``; with the
    suite-wide bypass active the gate is a no-op and this returns False for
    everything, which is the safe direction.
    """
    if path in _UNPROBEABLE:
        return False
    url = _PATH_PARAM.sub(_PROBE_ID, path)

    async def _probe() -> int:
        transport = httpx.ASGITransport(app=_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return int((await client.get(url)).status_code)

    try:
        return asyncio.run(_probe()) == 401
    except BaseException:  # noqa: BLE001 — anything that escapes means no gate stopped us
        return False


def classify_get_path(path: str) -> GetPosture:
    """Classify an OpenAPI GET path using the application gate and public registry."""
    predicates = gated_read_predicates(path)
    if predicates:
        return GetPosture(path, GATED, predicates)
    if observed_session_token_gate(path):
        return GetPosture(path, GATED, ("observed:401-without-session-token",))
    if path in PUBLIC_GETS:
        return GetPosture(path, EXPLICIT_PUBLIC)
    return GetPosture(path, UNCLASSIFIED)


def get_posture_registry(paths: Iterable[str]) -> dict[str, GetPosture]:
    """Map every supplied GET path to its measured posture."""
    return {path: classify_get_path(path) for path in sorted(paths)}


def write_posture_report(registry: dict[str, GetPosture], report_path: Path) -> None:
    """Write a deterministic, machine-parseable Markdown measurement report."""
    entries = sorted(registry.values(), key=lambda entry: (entry.classification, entry.path))
    counts = {
        GATED: sum(entry.classification == GATED for entry in entries),
        EXPLICIT_PUBLIC: sum(entry.classification == EXPLICIT_PUBLIC for entry in entries),
        UNCLASSIFIED: sum(entry.classification == UNCLASSIFIED for entry in entries),
    }
    unclassified = [entry.path for entry in entries if entry.classification == UNCLASSIFIED]

    lines = [
        "# GET Posture Registry",
        "",
        f"Measurement timestamp (UTC): {datetime.now(UTC).isoformat()}",
        "",
        "This is a test-generated measurement of the FastAPI OpenAPI schema. "
        "GATED paths match the app-level `require_session_token` read predicates; "
        "EXPLICIT-PUBLIC paths are named in `PUBLIC_GETS`; all other paths are UNCLASSIFIED.",
        "",
        "## Summary",
        "",
        "| Metric | Count |",
        "| --- | ---: |",
        f"| Total GET routes | {len(entries)} |",
        f"| {GATED} | {counts[GATED]} |",
        f"| {EXPLICIT_PUBLIC} | {counts[EXPLICIT_PUBLIC]} |",
        f"| {UNCLASSIFIED} | {counts[UNCLASSIFIED]} |",
        "",
        "## Contradiction findings",
        "",
    ]
    if unclassified:
        lines.extend(
            [
                "The following routes are neither matched by the app-level gate nor "
                "declared in `PUBLIC_GETS`; the registry test fails on them:",
                "",
                *[f"- `{path}`" for path in unclassified],
            ]
        )
    else:
        lines.append(
            "None. No route is inferred safe merely because it is not gated; every "
            "non-gated route is explicitly declared public."
        )

    lines.extend(
        [
            "",
            "## Registry",
            "",
            "| Path | Classification | Gate predicate(s) |",
            "| --- | --- | --- |",
        ]
    )
    lines.extend(
        f"| `{entry.path}` | {entry.classification} | "
        f"{', '.join(f'`{predicate}`' for predicate in entry.gate_predicates) or '-'} |"
        for entry in entries
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
