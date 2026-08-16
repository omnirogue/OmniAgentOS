"""Capability-manifest parsing and fail-closed validation."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class ManifestValidationError(ValueError):
    """A manifest omitted a security-critical field or has an invalid shape."""

    def __init__(self, error: str, detail: str) -> None:
        super().__init__(detail)
        self.error = error
        self.detail = detail


def _string_list(value: Any, field_name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)) or not all(isinstance(item, str) for item in value):
        raise ManifestValidationError("invalid_manifest", f"{field_name} must be a list of strings")
    return list(value)


@dataclass(frozen=True)
class CapabilityManifest:
    """The complete capability ceiling for one agent invocation."""

    run_id: str
    session_id: str
    holder_generation: int
    read_roots: list[str] = field(default_factory=list)
    write_roots: list[str] = field(default_factory=list)
    allowed_ops: list[str] = field(default_factory=list)
    connector_grants: list[str] = field(default_factory=list)  # Server-side connector capabilities
    max_spend_usd: float | None = None
    max_subprocess_seconds: float | None = None
    max_read_bytes: int | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> CapabilityManifest:
        if "holder_generation" not in raw or raw["holder_generation"] is None:
            raise ManifestValidationError(
                "missing_holder_generation", "holder_generation is required for every tool call"
            )
        holder_generation = raw["holder_generation"]
        if isinstance(holder_generation, bool) or not isinstance(holder_generation, int):
            raise ManifestValidationError(
                "invalid_manifest", "holder_generation must be an integer"
            )
        for required in ("run_id", "session_id"):
            if not isinstance(raw.get(required), str) or not raw[required].strip():
                raise ManifestValidationError(
                    "invalid_manifest", f"{required} must be a non-empty string"
                )

        def optional_number(name: str) -> float | None:
            value = raw.get(name)
            if value is None:
                return None
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                raise ManifestValidationError(
                    "invalid_manifest", f"{name} must be a non-negative number"
                )
            return float(value)

        max_read_bytes = raw.get("max_read_bytes")
        if max_read_bytes is not None and (
            isinstance(max_read_bytes, bool)
            or not isinstance(max_read_bytes, int)
            or max_read_bytes < 0
        ):
            raise ManifestValidationError(
                "invalid_manifest", "max_read_bytes must be a non-negative integer"
            )
        return cls(
            run_id=raw["run_id"],
            session_id=raw["session_id"],
            holder_generation=holder_generation,
            read_roots=_string_list(raw.get("read_roots"), "read_roots"),
            write_roots=_string_list(raw.get("write_roots"), "write_roots"),
            allowed_ops=_string_list(raw.get("allowed_ops"), "allowed_ops"),
            connector_grants=_string_list(raw.get("connector_grants"), "connector_grants"),
            max_spend_usd=optional_number("max_spend_usd"),
            max_subprocess_seconds=optional_number("max_subprocess_seconds"),
            max_read_bytes=max_read_bytes,
        )


def load_manifest(path_or_json: str | dict[str, Any]) -> CapabilityManifest:
    """Load a manifest from a JSON file, inline JSON, or parsed dictionary."""
    if isinstance(path_or_json, dict):
        return CapabilityManifest.from_dict(path_or_json)
    if not isinstance(path_or_json, str) or not path_or_json.strip():
        raise ManifestValidationError(
            "invalid_manifest", "manifest must be a JSON object or non-empty string"
        )
    raw_text = path_or_json
    candidate = Path(path_or_json)
    try:
        is_file = candidate.is_file()
    except OSError:
        is_file = False
    if is_file:
        try:
            raw_text = candidate.read_text(encoding="utf-8")
        except OSError as exc:
            raise ManifestValidationError(
                "invalid_manifest", "manifest file could not be read"
            ) from exc
    try:
        decoded = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ManifestValidationError("invalid_manifest", "manifest is not valid JSON") from exc
    if not isinstance(decoded, dict):
        raise ManifestValidationError("invalid_manifest", "manifest JSON must be an object")
    return CapabilityManifest.from_dict(decoded)
