"""Manual employee transcript upload and metadata retrieval (JG3-BE)."""

from __future__ import annotations

import json
import logging
from typing import Any, NoReturn, cast

from fastapi import APIRouter, Query, Request
from starlette.datastructures import UploadFile

from omniagentos.api.deps import StoreDep
from omniagentos.api.routes.control import fail
from omniagentos.contracts import JiraGoalsEvents
from omniagentos.db.store import SqliteStore
from omniagentos.employee_transcripts.storage import (
    TRANSCRIPT_SOURCES,
    TranscriptStorageError,
    TranscriptStorageService,
)
from omniagentos.employee_transcripts.store import TranscriptStore

_LOG = logging.getLogger(__name__)

employee_transcripts_router = APIRouter(
    prefix="/api/employee-transcripts", tags=["employee-transcripts"]
)

#: Framing budget allowed ON TOP of the content cap by the pre-parse size guard.
#: A transcript never arrives bare: the JSON form carries object keys plus
#: quoting/escaping (~120 bytes, more if the content needs escaping), and the
#: multipart form carries part headers and boundaries (~200 bytes per part). So a
#: request may be legitimately larger than ``max_bytes`` while the transcript
#: itself is not. 4 KiB is far above any real envelope and far below the point
#: where buffering matters, and the *content* cap in
#: ``TranscriptStorageService.write`` is still the exact, authoritative one — this
#: allowance only decides what is refused WITHOUT being parsed.
_UPLOAD_ENVELOPE_BYTES = 4096


def _transcript_store(store: StoreDep) -> TranscriptStore:
    return TranscriptStore(cast(SqliteStore, store))


def _storage(store: StoreDep) -> TranscriptStorageService:
    return TranscriptStorageService(_transcript_store(store))


def _metadata_json(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as exc:
            fail(400, "validation", f"meta_json is not valid JSON: {exc.msg}")
    else:
        decoded = value
    try:
        return json.dumps(decoded, separators=(",", ":"), sort_keys=True)
    except (TypeError, ValueError) as exc:
        fail(400, "validation", f"meta_json is not serializable: {exc}")


async def _guard_upload_size(request: Request, service: TranscriptStorageService) -> None:
    """Refuse an oversize body BEFORE any parser buffers it (major 7).

    The cap used to be enforced only after ``request.json()`` / ``request.form()``
    had already read the WHOLE body, so a 64 MB body against a 16-byte cap
    answered a correct 413 with ~269 MB resident — and the multipart path had
    additionally spooled every part over ``MultiPartParser.spool_max_size`` (1 MiB)
    to a temp file before any check ran. Both refusals below happen before a
    single byte reaches a parser.

    Two independent checks, because ``Content-Length`` is a claim rather than a
    guarantee: it is absent under chunked transfer-encoding and a client can
    simply lie about it.

    1. If the declared length parses and already exceeds the limit, refuse
       immediately — the body is never read at all.
    2. Otherwise count the body as it arrives and abandon the read the moment it
       passes the limit, so the stream is never drained past ``limit`` bytes.

    The bytes that survive are handed to the parser via Starlette's own body
    cache, so the accepted path costs exactly one bounded copy.
    """
    limit = service.max_bytes + _UPLOAD_ENVELOPE_BYTES

    def refuse(observed: str) -> NoReturn:
        fail(
            413,
            "size_cap",
            "request body exceeds the transcript size cap",
            {"max_bytes": service.max_bytes, "limit_bytes": limit, "refused_on": observed},
        )

    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            declared_bytes: int | None = int(declared)
        except ValueError:
            # An unparseable header is not a licence to buffer; the counted read
            # below still bounds the request.
            declared_bytes = None
        if declared_bytes is not None and declared_bytes > limit:
            refuse("content-length")

    buffered = bytearray()
    stream = request.stream()
    try:
        async for chunk in stream:
            buffered.extend(chunk)
            if len(buffered) > limit:
                refuse("streamed-bytes")
    finally:
        # Close the generator explicitly on the refusal path rather than leaving
        # it to the event loop's async-generator finalizer.
        await stream.aclose()
    # Starlette's ``Request.stream()`` yields a cached ``_body`` when present —
    # exactly what ``Request.body()`` sets — so ``json()``/``form()`` below re-read
    # these already-bounded bytes instead of pulling the receive channel again.
    request._body = bytes(buffered)  # type: ignore[attr-defined]


async def _upload_payload(
    request: Request, service: TranscriptStorageService
) -> tuple[str, str, bytes, str, str | None]:
    content_type = request.headers.get("content-type", "").lower()
    is_multipart = content_type.startswith("multipart/form-data")
    if not is_multipart and "application/json" not in content_type:
        fail(400, "validation", "content type must be application/json or multipart/form-data")
    # Header checks first (free), then the size guard — which must precede EVERY
    # parse below, JSON and multipart alike.
    await _guard_upload_size(request, service)
    if is_multipart:
        try:
            form = await request.form()
        except Exception as exc:  # noqa: BLE001
            fail(400, "validation", f"invalid multipart body: {exc}")
        employee_id = form.get("employee_id")
        uploaded = form.get("file")
        if not isinstance(employee_id, str) or not employee_id.strip():
            fail(400, "validation", "employee_id is required")
        if not isinstance(uploaded, UploadFile):
            fail(400, "validation", "file is required for multipart upload")
        filename = uploaded.filename or "transcript.txt"
        content = await uploaded.read(service.max_bytes + 1)
        source_value = form.get("source", "manual")
        if not isinstance(source_value, str):
            fail(400, "validation", "source must be text")
        return (
            employee_id.strip(),
            filename,
            content,
            source_value,
            _metadata_json(form.get("meta_json")),
        )

    try:
        body = await request.json()
    except Exception as exc:  # noqa: BLE001
        fail(400, "validation", f"invalid JSON body: {exc}")
    if not isinstance(body, dict):
        fail(400, "validation", "JSON body must be an object")
    employee_id = body.get("employee_id")
    json_content = body.get("content")
    filename = body.get("filename", "transcript.txt")
    source = body.get("source", "manual")
    if not isinstance(employee_id, str) or not employee_id.strip():
        fail(400, "validation", "employee_id is required")
    if not isinstance(json_content, str):
        fail(400, "validation", "content must be text")
    if not isinstance(filename, str):
        fail(400, "validation", "filename must be text")
    if not isinstance(source, str):
        fail(400, "validation", "source must be text")
    return (
        employee_id.strip(),
        filename,
        json_content.encode("utf-8"),
        source,
        _metadata_json(body.get("meta_json")),
    )


@employee_transcripts_router.post("", status_code=201)
async def _upload_employee_transcript(request: Request, store: StoreDep) -> dict[str, Any]:
    service = _storage(store)
    employee_id, filename, content, source, meta_json = await _upload_payload(request, service)
    if source not in TRANSCRIPT_SOURCES:
        fail(400, "validation", f"source must be one of {sorted(TRANSCRIPT_SOURCES)}")
    try:
        transcript = service.write(
            employee_id,
            filename,
            content,
            source=source,
            meta_json=meta_json,
        )
    except TranscriptStorageError as exc:
        if exc.kind == "size_cap":
            fail(413, "size_cap", str(exc), {"max_bytes": service.max_bytes})
        fail(400, exc.kind, str(exc))

    try:
        store.insert_event(
            JiraGoalsEvents.EMPLOYEE_TRANSCRIPT_UPDATED,
            "api",
            "transcript_upload",
            target_type="transcript",
            target_id=str(transcript["id"]),
            payload=transcript,
        )
    except Exception:  # noqa: BLE001 -- persistence succeeded; SSE is best effort
        _LOG.debug(
            "failed to emit employee_transcript.updated for %s",
            transcript["id"],
            exc_info=True,
        )
    return transcript


@employee_transcripts_router.get("")
def _list_employee_transcripts(
    store: StoreDep,
    employee_id: str | None = None,
    status: str | None = None,
    limit: int = Query(default=500),
) -> dict[str, Any]:
    if not 1 <= limit <= 5000:
        fail(400, "validation", "limit must be between 1 and 5000")
    try:
        transcripts = _transcript_store(store).list(
            employee_id=employee_id, status=status, limit=limit
        )
    except ValueError as exc:
        fail(400, "validation", str(exc))
    return {"transcripts": transcripts}


@employee_transcripts_router.get("/{transcript_id}")
def _get_employee_transcript(transcript_id: str, store: StoreDep) -> dict[str, Any]:
    transcript = _transcript_store(store).get(transcript_id)
    if transcript is None:
        fail(404, "not_found", "employee transcript not found", {"id": transcript_id})
    return transcript


__all__ = ["employee_transcripts_router"]
