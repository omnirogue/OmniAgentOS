"""Voice / TTS API surface."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from omniagentos.api.deps import StoreDep
from omniagentos.api.services import fail
from omniagentos.contracts import default_vault_dir
from omniagentos.path_containment import inode_relative_parts_anchored
from omniagentos.steward.config import load_steward_config
from omniagentos.voice.service import (
    TEXT_CAP,
    VOICE_RUN_ID,
    list_provider_statuses,
    synthesize_to_artifact,
)

router = APIRouter(prefix="/api/voice", tags=["voice"])


class SpeakRequest(BaseModel):
    text: str = Field(default="")
    provider: str | None = None
    voice_id: str | None = None


def _vault_artifacts_root() -> Path:
    return (Path(default_vault_dir()) / "artifacts").resolve()


def _find_voice_artifact(store: StoreDep, artifact_id: str) -> dict[str, Any] | None:
    for row in store.get_artifacts(VOICE_RUN_ID):
        if row.get("id") == artifact_id:
            return row
    return None


def _path_is_safe(path: Path) -> bool:
    """Serve only files resolved under ``<vault>/artifacts``."""
    root = _vault_artifacts_root()
    try:
        resolved = path.resolve()
    except (OSError, ValueError):
        return False
    return inode_relative_parts_anchored(resolved, root) is not None and resolved.is_file()


def _status_response(status_code: int, status: str, detail: str) -> JSONResponse:
    """Return ``{"status","detail"}`` at the given HTTP status (not the global error envelope)."""
    return JSONResponse(
        status_code=status_code,
        content={"status": status, "detail": detail},
    )


@router.get("/providers")
def providers() -> list[dict[str, str]]:
    """Return readiness for every built-in TTS provider."""
    cfg = load_steward_config()
    return list_provider_statuses(cfg)


@router.post("/speak", response_model=None)
def speak(body: SpeakRequest, store: StoreDep) -> dict[str, Any] | JSONResponse:
    """Synthesize text to an mp3 artifact."""
    text = body.text if body.text is not None else ""
    if not text.strip():
        return _status_response(400, "error", "text is required")

    if len(text) > TEXT_CAP:
        return _status_response(
            413,
            "error",
            f"text exceeds {TEXT_CAP} character limit ({len(text)} chars)",
        )

    cfg = load_steward_config()
    try:
        result = synthesize_to_artifact(
            text,
            provider=body.provider,
            voice_id=body.voice_id,
            store=store,
            cfg=cfg,
        )
    except Exception as exc:
        return fail(500, "voice_error", "voice synthesis failed", str(exc))

    if result.get("ok"):
        return result

    status = str(result.get("status") or "error")
    detail = str(result.get("detail") or "")
    if status in {"no_key", "unavailable"}:
        return _status_response(503, status, detail)
    return _status_response(400, status, detail)


@router.get("/audio/{artifact_id}", response_model=None)
def audio(artifact_id: str, store: StoreDep) -> FileResponse | JSONResponse:
    """Stream a previously synthesized mp3. Path-safe against vault escape."""
    row = _find_voice_artifact(store, artifact_id)
    if row is None:
        return _status_response(404, "error", "unknown artifact")

    uri = row.get("uri")
    if not uri or not isinstance(uri, str):
        return _status_response(404, "error", "unknown artifact")

    path = Path(uri)
    if not _path_is_safe(path):
        return _status_response(404, "error", "artifact path not allowed")

    return FileResponse(
        path=str(path.resolve()),
        media_type="audio/mpeg",
        filename=path.name,
    )


__all__ = ["router"]
