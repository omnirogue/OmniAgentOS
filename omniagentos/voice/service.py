"""Materialize TTS output as vault artifacts."""

from __future__ import annotations

import secrets
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from omniagentos.contracts import Store, default_vault_dir, digest, new_id, utc_now_iso
from omniagentos.steward.config import StewardConfig, VoiceConfig
from omniagentos.voice.elevenlabs import ElevenLabsProvider
from omniagentos.voice.providers import TTSProvider, TTSResult
from omniagentos.voice.xai import XaiProvider

TEXT_CAP = 5000
VOICE_RUN_ID = "voice"
ARTIFACT_TYPE = "audio/mpeg"


def ensure_voice_run(store: Store) -> None:
    """Create the terminal system task/run used to own voice artifacts.

    M8: Handles concurrent first synthesis by using try/except IntegrityError.
    Replaces get-then-create with INSERT OR IGNORE semantics, so concurrent
    calls don't race and result in a 500 error.
    """
    now = utc_now_iso()
    task_row = {
        "id": VOICE_RUN_ID,
        "discipline_id": None,
        "title": "System — Voice synthesis",
        "input_json": "{}",
        "acceptance_json": "{}",
        "state": "completed",
        "risk": "low",
        "created_at": now,
        "updated_at": now,
    }
    run_row = {
        "id": VOICE_RUN_ID,
        "task_id": VOICE_RUN_ID,
        "discipline_id": None,
        "arm": None,
        "harness": "mock",
        "harness_version": "",
        "env_hash": "",
        "harness_params": "{}",
        "agent": None,
        "model": None,
        "attempt": 1,
        "state": "completed",
        "worker_id": None,
        "plan_json": "[]",
        "output_text": None,
        "output_json": None,
        "error": None,
        "wall_ms": None,
        "turns": None,
        "input_tokens": None,
        "output_tokens": None,
        "cost_usd": None,
        "usage_estimated": 1,
        "usage_source": "estimator",
        "budget_json": "{}",
        "cancel_requested": 0,
        "trace_id": VOICE_RUN_ID,
        "session_ref": None,
        "manifest_path": None,
        "vault_note_path": None,
        "queued_at": now,
        "started_at": None,
        "finished_at": None,
        "created_at": now,
        "updated_at": now,
    }

    # Try to create the task; if it already exists (IntegrityError), treat as success.
    try:
        store.create_task(task_row)
    except sqlite3.IntegrityError:
        # Task already exists; this is fine in concurrent scenarios.
        pass

    # Try to create the run; if it already exists (IntegrityError), treat as success.
    try:
        store.enqueue_run(run_row)
    except sqlite3.IntegrityError:
        # Run already exists; this is fine in concurrent scenarios.
        pass


def _voice_cfg(cfg: StewardConfig | VoiceConfig) -> VoiceConfig:
    if isinstance(cfg, StewardConfig):
        return cfg.voice
    return cfg


def get_provider(name: str, voice_cfg: VoiceConfig) -> TTSProvider | None:
    """Return a provider instance for ``name``, or ``None`` if unknown."""
    key = name.strip().lower()
    if key in {"elevenlabs", "el"}:
        return ElevenLabsProvider(voice_cfg)
    if key in {"xai", "grok", "xai_voice"}:
        return XaiProvider(voice_cfg)
    return None


def list_provider_statuses(cfg: StewardConfig | VoiceConfig) -> list[dict[str, str]]:
    """Status rows for both built-in providers."""
    voice_cfg = _voice_cfg(cfg)
    rows: list[dict[str, str]] = []
    for provider in (ElevenLabsProvider(voice_cfg), XaiProvider(voice_cfg)):
        status, detail = provider.status()
        rows.append({"provider": provider.name, "status": status, "detail": detail})
    return rows


def voice_artifacts_dir(vault_dir: str | None = None) -> Path:
    root = Path(vault_dir if vault_dir is not None else default_vault_dir())
    return root / "artifacts" / "voice"


def synthesize_to_artifact(
    text: str,
    *,
    provider: str | None = None,
    voice_id: str | None = None,
    store: Store,
    cfg: StewardConfig | VoiceConfig,
    vault_dir: str | None = None,
) -> dict[str, Any]:
    """Synthesize ``text`` and persist an mp3 artifact under the vault.

    Returns a dict with keys: ``ok``, ``provider``, ``status``, ``detail``,
    and on success ``artifact_id`` and ``path``.
    """
    ensure_voice_run(store)
    voice_cfg = _voice_cfg(cfg)
    provider_name = (provider or voice_cfg.default_provider or "elevenlabs").strip()

    if not text or not text.strip():
        return {
            "ok": False,
            "provider": provider_name,
            "status": "error",
            "detail": "text is required",
        }

    if len(text) > TEXT_CAP:
        return {
            "ok": False,
            "provider": provider_name,
            "status": "error",
            "detail": f"text exceeds {TEXT_CAP} character limit ({len(text)} chars)",
        }

    impl = get_provider(provider_name, voice_cfg)
    if impl is None:
        return {
            "ok": False,
            "provider": provider_name,
            "status": "error",
            "detail": f"unknown provider: {provider_name}",
        }

    result: TTSResult = impl.synthesize(text, voice_id=voice_id)
    if not result.ok or result.audio is None:
        return {
            "ok": False,
            "provider": impl.name,
            "status": result.status,
            "detail": result.detail,
        }

    audio = result.audio
    out_dir = voice_artifacts_dir(vault_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    filename = f"{stamp}-{secrets.token_hex(4)}.mp3"
    path = out_dir / filename
    path.write_bytes(audio)

    sha = digest(audio)
    artifact_id = new_id("art")
    store.add_artifact(
        {
            "id": artifact_id,
            "run_id": VOICE_RUN_ID,
            "type": ARTIFACT_TYPE,
            "uri": str(path.resolve()),
            "sha256": sha,
            "bytes": len(audio),
            "created_at": utc_now_iso(),
        }
    )

    return {
        "ok": True,
        "provider": impl.name,
        "artifact_id": artifact_id,
        "path": str(path.resolve()),
        "status": "ready",
        "detail": "",
    }


__all__ = [
    "ARTIFACT_TYPE",
    "TEXT_CAP",
    "VOICE_RUN_ID",
    "ensure_voice_run",
    "get_provider",
    "list_provider_statuses",
    "synthesize_to_artifact",
    "voice_artifacts_dir",
]
