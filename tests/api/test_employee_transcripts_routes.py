"""HTTP binding tests for the intentionally manual-only transcript surface."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from collections.abc import AsyncIterator, Callable, Iterable
from pathlib import Path
from typing import Any

import httpx
import pytest
from starlette.requests import Request

from omniagentos.api.deps import get_store
from omniagentos.api.main import app
from omniagentos.api.routes.employee_transcripts import _UPLOAD_ENVELOPE_BYTES
from omniagentos.contracts import utc_now_iso
from omniagentos.db.store import SqliteStore
from omniagentos.sessions import token
from tests.support.db_template import make_store


@pytest.fixture
def database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SqliteStore:
    monkeypatch.setenv("OMNIAGENTOS_TRANSCRIPTS_ROOT", str(tmp_path / "transcripts"))
    monkeypatch.setenv("OMNIAGENTOS_TRANSCRIPTS_MAX_BYTES", "16")
    store = make_store(SqliteStore, tmp_path / "api.db")
    store._connection.execute(
        "INSERT INTO employees (id, name, status, created_at) VALUES (?,?,?,?)",
        ("emp_api", "API Employee", "active", utc_now_iso()),
    )
    return store


def _call(database: SqliteStore, request: Callable[[httpx.AsyncClient], Any]) -> Any:
    async def driver() -> Any:
        app.dependency_overrides[get_store] = lambda: database
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                return await request(client)
        finally:
            app.dependency_overrides.clear()

    return asyncio.run(driver())


def _rows(database: SqliteStore) -> list[sqlite3.Row]:
    return database._connection.execute(
        "SELECT * FROM transcript_uploads ORDER BY uploaded_at, id"
    ).fetchall()


def _files() -> list[Path]:
    return list(Path(__import__("os").environ["OMNIAGENTOS_TRANSCRIPTS_ROOT"]).glob("*"))


def test_upload_happy_path_has_db_disk_and_sse_witness(database: SqliteStore) -> None:
    content = "meeting notes"

    async def request(client: httpx.AsyncClient) -> httpx.Response:
        return await client.post(
            "/api/employee-transcripts",
            json={"employee_id": "emp_api", "filename": "meeting.txt", "content": content},
        )

    response = _call(database, request)
    assert response.status_code == 201, response.text
    body = response.json()
    rows = _rows(database)
    assert len(rows) == 1 and dict(rows[0]) == body
    disk_path = (
        Path(__import__("os").environ["OMNIAGENTOS_TRANSCRIPTS_ROOT"]) / body["storage_path"]
    )
    disk = disk_path.read_bytes()
    assert disk == content.encode()
    assert hashlib.sha256(disk).hexdigest() == body["content_hash"]
    event = database._connection.execute(
        "SELECT * FROM events WHERE type = 'employee_transcript.updated'"
    ).fetchone()
    assert event is not None
    assert event["action"] == "transcript_upload"
    assert event["target_type"] == "transcript"
    assert event["target_id"] == body["id"]
    assert json.loads(event["payload_json"])["id"] == body["id"]


def test_multipart_upload_is_supported(database: SqliteStore) -> None:
    async def request(client: httpx.AsyncClient) -> httpx.Response:
        return await client.post(
            "/api/employee-transcripts",
            data={"employee_id": "emp_api", "source": "manual"},
            files={"file": ("upload.txt", b"multipart", "text/plain")},
        )

    response = _call(database, request)
    assert response.status_code == 201, response.text
    assert response.json()["filename"] == "upload.txt"
    assert _files()[0].read_bytes() == b"multipart"


def test_unknown_employee_is_400_and_leaves_no_row_or_file(database: SqliteStore) -> None:
    async def request(client: httpx.AsyncClient) -> httpx.Response:
        return await client.post(
            "/api/employee-transcripts",
            json={"employee_id": "emp_missing", "filename": "x.txt", "content": "x"},
        )

    response = _call(database, request)
    assert response.status_code == 400, response.text
    assert _rows(database) == []
    assert _files() == []


def test_cap_refusal_is_413_and_leaves_no_row_or_file(database: SqliteStore) -> None:
    async def request(client: httpx.AsyncClient) -> httpx.Response:
        return await client.post(
            "/api/employee-transcripts",
            json={"employee_id": "emp_api", "filename": "x.txt", "content": "x" * 17},
        )

    response = _call(database, request)
    assert response.status_code == 413, response.text
    assert _rows(database) == []
    assert _files() == []


class _WitnessBody:
    """A request body that REFUSES to be drained past what the route may read.

    The 413 alone does not distinguish "refused before buffering" from "refused
    after buffering" — the pre-fix implementation already answered 413, having
    first read the whole 64 MB body into memory (and, for multipart, spooled it
    to a temp file). The honest witness is therefore the receive channel itself:
    ``pulled`` counts the bytes the application actually accepted, and asking for
    more than ``allowed`` raises here instead of quietly handing over another
    megabyte.
    """

    def __init__(self, total: int, *, allowed: int, chunk: int = 4096) -> None:
        self.total = total
        self.allowed = allowed
        self.chunk = chunk
        self.pulled = 0

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self._chunks()

    async def _chunks(self) -> AsyncIterator[bytes]:
        sent = 0
        while sent < self.total:
            size = min(self.chunk, self.total - sent)
            if self.pulled + size > self.allowed:
                raise AssertionError(
                    "request body was drained past the cap: the route pulled "
                    f"{self.pulled + size} bytes with only {self.allowed} allowed"
                )
            self.pulled += size
            sent += size
            yield b"x" * size


_OVERSIZE_TOTAL = 64 * 1024 * 1024  # the 64MB body that previously went 269MB resident


@pytest.mark.parametrize(
    ("declared", "allowance", "refused_on"),
    [
        # Declared Content-Length already over the limit: not one byte of the
        # body may be read.
        pytest.param(str(_OVERSIZE_TOTAL), 0, "content-length", id="declared-oversize"),
        # No Content-Length at all (chunked): the counted read must abandon the
        # stream within one chunk of the limit.
        pytest.param(None, None, "streamed-bytes", id="chunked-no-content-length"),
        # Content-Length is a claim, not a guarantee — a lying small header must
        # not buy the sender an unbounded read.
        pytest.param("10", None, "streamed-bytes", id="lying-content-length"),
    ],
)
def test_oversize_body_is_refused_before_buffering(
    database: SqliteStore,
    monkeypatch: pytest.MonkeyPatch,
    declared: str | None,
    allowance: int | None,
    refused_on: str,
) -> None:
    # The `database` fixture pins OMNIAGENTOS_TRANSCRIPTS_MAX_BYTES=16.
    limit = 16 + _UPLOAD_ENVELOPE_BYTES
    chunk = 4096
    # One chunk of slack: the route cannot know a chunk crosses the limit until
    # it holds that chunk.
    allowed = limit + chunk if allowance is None else allowance
    body = _WitnessBody(_OVERSIZE_TOTAL, allowed=allowed, chunk=chunk)

    # Second, independent witness: no parser may be entered at all. Both raise if
    # reached, so a regression cannot pass by silently returning empty data.
    parsed: list[str] = []

    def _no_json(self: Request) -> Any:
        parsed.append("json")
        raise AssertionError("request.json() buffered the body despite the size guard")

    def _no_form(self: Request, *args: Any, **kwargs: Any) -> Any:
        parsed.append("form")
        raise AssertionError("request.form() buffered the body despite the size guard")

    monkeypatch.setattr(Request, "json", _no_json)
    monkeypatch.setattr(Request, "form", _no_form)

    headers = {"content-type": "application/json"}
    if declared is not None:
        headers["content-length"] = declared

    async def request(client: httpx.AsyncClient) -> httpx.Response:
        return await client.post("/api/employee-transcripts", content=body, headers=headers)

    response = _call(database, request)

    assert response.status_code == 413, response.text
    error = response.json()["error"]
    assert error["code"] == "size_cap"
    assert error["detail"]["refused_on"] == refused_on
    assert error["detail"]["max_bytes"] == 16
    assert parsed == [], f"a parser ran before the size guard refused: {parsed}"
    assert body.pulled <= allowed, (
        f"body drained {body.pulled} bytes; the limit is {limit} (+ at most one {chunk}-byte chunk)"
    )
    if declared == str(_OVERSIZE_TOTAL):
        assert body.pulled == 0, "a declared oversize Content-Length must be refused unread"
    assert _rows(database) == []
    assert _files() == []


def test_oversize_multipart_body_is_refused_before_spooling(
    database: SqliteStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The multipart path is the worse half of the defect and gets its own probe.

    ``MultiPartParser`` spools every part over ``spool_max_size`` (1 MiB) to a
    temp FILE before the route ever sees a byte count, so an unguarded multipart
    upload wrote the attacker's 64 MB to disk on the way to its 413. The guard
    runs before the content-type branch, so this refusal happens without the
    parser being constructed at all.
    """
    limit = 16 + _UPLOAD_ENVELOPE_BYTES
    body = _WitnessBody(_OVERSIZE_TOTAL, allowed=0, chunk=4096)
    parsed: list[str] = []

    def _no_form(self: Request, *args: Any, **kwargs: Any) -> Any:
        parsed.append("form")
        raise AssertionError("MultiPartParser ran despite the size guard")

    monkeypatch.setattr(Request, "form", _no_form)

    async def request(client: httpx.AsyncClient) -> httpx.Response:
        return await client.post(
            "/api/employee-transcripts",
            content=body,
            headers={
                "content-type": "multipart/form-data; boundary=witness",
                "content-length": str(_OVERSIZE_TOTAL),
            },
        )

    response = _call(database, request)
    assert response.status_code == 413, response.text
    assert response.json()["error"]["detail"]["limit_bytes"] == limit
    assert parsed == [], "the multipart parser ran before the size guard refused"
    assert body.pulled == 0, "not one byte of an oversize multipart body may be read"
    assert _rows(database) == []
    assert _files() == []


def test_upload_at_the_cap_still_succeeds_through_both_encodings(
    database: SqliteStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The envelope allowance must not refuse a legitimate upload AT the cap.

    A guard that refuses the request body at exactly ``max_bytes`` would reject
    every full-size transcript, because JSON quoting and multipart framing both
    ride along with the content. This pins the accepted path at the boundary for
    BOTH encodings, and — since the guard hands its buffered bytes to the parser
    via Starlette's body cache — it also proves that handoff is byte-exact.
    """
    monkeypatch.setenv("OMNIAGENTOS_TRANSCRIPTS_MAX_BYTES", "4096")
    content = "a" * 4096

    async def request(client: httpx.AsyncClient) -> dict[str, httpx.Response]:
        return {
            "json": await client.post(
                "/api/employee-transcripts",
                json={"employee_id": "emp_api", "filename": "at-cap.txt", "content": content},
            ),
            "multipart": await client.post(
                "/api/employee-transcripts",
                data={"employee_id": "emp_api", "source": "manual"},
                files={"file": ("at-cap.txt", content.encode(), "text/plain")},
            ),
        }

    out = _call(database, request)
    assert out["json"].status_code == 201, out["json"].text
    assert out["json"].json()["size_bytes"] == 4096
    assert out["multipart"].status_code == 201, out["multipart"].text
    assert out["multipart"].json()["size_bytes"] == 4096
    assert {path.read_bytes() for path in _files()} == {content.encode()}


def test_traversal_refusal_through_http_leaves_no_row_or_file(database: SqliteStore) -> None:
    async def request(client: httpx.AsyncClient) -> httpx.Response:
        return await client.post(
            "/api/employee-transcripts",
            json={
                "employee_id": "emp_api",
                "filename": "../../../etc/passwd",
                "content": "x",
            },
        )

    response = _call(database, request)
    assert response.status_code == 400, response.text
    assert _rows(database) == []
    assert _files() == []


def test_list_metadata_missing_and_bounds(database: SqliteStore) -> None:
    async def request(client: httpx.AsyncClient) -> dict[str, httpx.Response]:
        created = await client.post(
            "/api/employee-transcripts",
            json={"employee_id": "emp_api", "filename": "x.txt", "content": "x"},
        )
        transcript_id = created.json()["id"]
        return {
            "list": await client.get("/api/employee-transcripts"),
            "filtered": await client.get(
                "/api/employee-transcripts", params={"employee_id": "emp_api", "status": "uploaded"}
            ),
            "get": await client.get(f"/api/employee-transcripts/{transcript_id}"),
            "missing": await client.get("/api/employee-transcripts/tu_missing"),
            "low": await client.get("/api/employee-transcripts", params={"limit": 0}),
            "high": await client.get("/api/employee-transcripts", params={"limit": 5001}),
        }

    out = _call(database, request)
    assert out["list"].status_code == 200
    assert len(out["list"].json()["transcripts"]) == 1
    assert len(out["filtered"].json()["transcripts"]) == 1
    assert out["get"].status_code == 200
    assert out["missing"].status_code == 404
    assert out["low"].status_code == 400
    assert out["high"].status_code == 400


# --- app-level session-token gate (real_auth) ----------------------------------


@pytest.mark.real_auth
class TestTranscriptReadsAreGated:
    """Both transcript GETs require the local session token (major 6).

    The list and metadata reads return operator-supplied FILENAMES, per-file
    SHA-256 digests and the ``storage_path`` leaf names of the contained
    transcript root — a directory listing of a private HR corpus plus the exact
    names an attacker would aim at that root. That is the workfs /
    server-inventory recon tier, so ``omniagentos.api.main`` gates them via
    ``_is_employee_transcript_read``.

    These tests carry ``@pytest.mark.real_auth`` on purpose: the suite-wide
    ``_bypass_global_auth`` fixture in ``tests/conftest.py`` overrides
    ``require_session_token`` to a no-op for every other test, so WITHOUT the
    marker an unauthenticated-401 assertion would pass vacuously against a
    disabled gate and would keep passing with the predicate deleted.
    """

    @pytest.fixture
    def token_header(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, str]:
        monkeypatch.setattr(token, "TOKEN_PATH", tmp_path / "sessions-token")
        return {"X-Session-Token": token.load_or_create_token()}

    def test_list_401_without_token(self, database: SqliteStore) -> None:
        async def request(client: httpx.AsyncClient) -> httpx.Response:
            return await client.get("/api/employee-transcripts")

        response = _call(database, request)
        assert response.status_code in {401, 403}, response.text
        assert response.json()["error"]["code"] == "unauthorized"
        # No filename, hash or storage leaf may appear in the refusal body.
        assert "transcripts" not in response.text

    def test_metadata_401_without_token(
        self, database: SqliteStore, token_header: dict[str, str]
    ) -> None:
        async def request(client: httpx.AsyncClient) -> dict[str, httpx.Response]:
            created = await client.post(
                "/api/employee-transcripts",
                json={"employee_id": "emp_api", "filename": "secret.txt", "content": "x"},
                headers=token_header,
            )
            transcript_id = created.json()["id"]
            return {
                "created": created,
                "existing": await client.get(f"/api/employee-transcripts/{transcript_id}"),
                "missing": await client.get("/api/employee-transcripts/tu_missing"),
            }

        out = _call(database, request)
        assert out["created"].status_code == 201, out["created"].text
        assert out["existing"].status_code in {401, 403}, out["existing"].text
        assert out["existing"].json()["error"]["code"] == "unauthorized"
        assert "secret.txt" not in out["existing"].text
        assert "storage_path" not in out["existing"].text
        # The gate runs BEFORE the handler, so an unknown id must not even leak
        # existence via a 404 — that distinction is the recon signal.
        assert out["missing"].status_code in {401, 403}, out["missing"].text

    def test_machine_token_cannot_read_transcripts_but_asserted_principal_can(
        self, database: SqliteStore, token_header: dict[str, str]
    ) -> None:
        async def request(client: httpx.AsyncClient) -> dict[str, httpx.Response]:
            created = await client.post(
                "/api/employee-transcripts",
                json={"employee_id": "emp_api", "filename": "secret.txt", "content": "x"},
                headers=token_header,
            )
            transcript_id = created.json()["id"]
            return {
                "created": created,
                "list": await client.get("/api/employee-transcripts", headers=token_header),
                "get": await client.get(
                    f"/api/employee-transcripts/{transcript_id}", headers=token_header
                ),
                "operator_list": await client.get(
                    "/api/employee-transcripts",
                    headers={
                        **token_header,
                        "X-Omni-Authenticated-Principal": "human:operator",
                    },
                ),
                "operator_get": await client.get(
                    f"/api/employee-transcripts/{transcript_id}",
                    headers={
                        **token_header,
                        "X-Omni-Authenticated-Principal": "human:operator",
                    },
                ),
            }

        out = _call(database, request)
        assert out["created"].status_code == 201, out["created"].text
        assert out["list"].status_code == 403, out["list"].text
        assert out["list"].json()["error"]["code"] == "system_principal_forbidden"
        assert out["get"].status_code == 403, out["get"].text
        assert out["get"].json()["error"]["code"] == "system_principal_forbidden"
        assert out["operator_list"].status_code == 200, out["operator_list"].text
        assert len(out["operator_list"].json()["transcripts"]) == 1
        assert out["operator_get"].status_code == 200, out["operator_get"].text
        assert out["operator_get"].json()["filename"] == "secret.txt"


def test_mounted_routes_are_exactly_these_three() -> None:
    def terminal_routes(routes: Iterable[Any]) -> Iterable[Any]:
        for route in routes:
            original = getattr(route, "original_router", None)
            if original is not None:
                yield from terminal_routes(getattr(original, "routes", []))
                continue
            nested = getattr(route, "routes", None)
            if nested is not None and not hasattr(route, "path"):
                yield from terminal_routes(nested)
                continue
            yield route

    found = {
        (method, route.path)
        for route in terminal_routes(app.routes)
        if getattr(route, "path", "").startswith("/api/employee-transcripts")
        for method in (getattr(route, "methods", set()) or set())
    }
    expected = {
        ("POST", "/api/employee-transcripts"),
        ("GET", "/api/employee-transcripts"),
        ("GET", "/api/employee-transcripts/{transcript_id}"),
    }
    assert found == expected, (
        f"mounted paths must be exactly the three manual routes; "
        f"unexpected auto/chat/ingest route found: {sorted(found - expected)}"
    )
