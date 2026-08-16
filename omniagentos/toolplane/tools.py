"""Scoped tool implementations for the toolplane CLI."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from omniagentos.path_containment import inode_relative_parts_anchored
from omniagentos.secret_registry import references_secret
from omniagentos.security.sanitize import sanitize_for_persistence

from .manifest import CapabilityManifest

Tool = Callable[[CapabilityManifest, dict[str, Any]], dict[str, Any]]


class ToolplaneError(ValueError):
    """An expected, safe-to-return tool failure."""

    def __init__(self, error: str, detail: str) -> None:
        super().__init__(detail)
        self.error = error
        self.detail = detail


class ReadBeforeWriteMiddleware:
    """Require each session to observe the current file before overwriting it."""

    _DISABLED = frozenset({"0", "false", "no", "off"})
    _ENV = "OMNIAGENTOS_READBEFOREWRITE_ENABLED"

    def __init__(self) -> None:
        self._ledgers: dict[tuple[str, str, int], dict[str, str]] = {}
        self._lock = threading.Lock()

    @classmethod
    def enabled(cls) -> bool:
        return os.environ.get(cls._ENV, "true").strip().lower() not in cls._DISABLED

    @staticmethod
    def _session_key(manifest: CapabilityManifest) -> tuple[str, str, int]:
        return (manifest.run_id, manifest.session_id, manifest.holder_generation)

    def record_read(
        self, manifest: CapabilityManifest, path: str, digest: str | None = None
    ) -> None:
        if not self.enabled():
            return
        try:
            observed = digest or _sha256_file(path)
        except OSError:
            return
        with self._lock:
            self._ledgers.setdefault(self._session_key(manifest), {})[path] = observed

    def require_read(self, manifest: CapabilityManifest, path: str) -> None:
        if not self.enabled() or not os.path.isfile(path):
            return
        detail = f"read {path} first; it changed since your last read"
        try:
            current = _sha256_file(path)
        except OSError as exc:
            raise ToolplaneError("read_before_write", detail) from exc
        with self._lock:
            ledger = self._ledgers.get(self._session_key(manifest))
            observed = ledger.pop(path, None) if ledger is not None else None
        if observed != current:
            raise ToolplaneError("read_before_write", detail)


def _ok(result: Any) -> dict[str, Any]:
    return {"ok": True, "result": result}


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


_READ_BEFORE_WRITE = ReadBeforeWriteMiddleware()


def _realpath(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ToolplaneError("invalid_path", "path must be a non-empty string")
    try:
        return os.path.realpath(value)
    except (OSError, ValueError) as exc:
        raise ToolplaneError("invalid_path", "path could not be resolved") from exc


def _roots(values: list[str], access: str) -> list[str]:
    if not values:
        raise ToolplaneError("out_of_scope", f"no {access} roots were granted")
    roots: list[str] = []
    for raw in values:
        root = _realpath(raw)
        if not os.path.isdir(root):
            raise ToolplaneError("out_of_scope", f"a granted {access} root is unavailable")
        roots.append(root)
    return roots


def _within(target: str, root: str) -> bool:
    return inode_relative_parts_anchored(target, root) is not None


def _scoped_path(manifest: CapabilityManifest, raw_path: Any, access: str) -> str:
    target = _realpath(raw_path)
    roots = _roots(manifest.read_roots if access == "read" else manifest.write_roots, access)
    if not any(_within(target, root) for root in roots):
        raise ToolplaneError("out_of_scope", f"{access} path is outside granted roots")
    if access == "read" and references_secret(target, os.getcwd()):
        raise ToolplaneError("secret_path", "reading credential material is not permitted")
    return target


def _allow(manifest: CapabilityManifest, name: str) -> None:
    if name not in manifest.allowed_ops:
        # Phase 1.3: G3 records a deny decision on real toolplane path.
        try:
            from omniagentos.gates.service import GateService

            GateService().g3_tool(
                {
                    "tool": name,
                    "tools_allowed": list(manifest.allowed_ops or []),
                    "tool_allowed": False,
                }
            )
        except Exception:  # noqa: BLE001
            pass
        raise ToolplaneError("not_allowed", f"{name} is not in allowed_ops")


def _limit_read(manifest: CapabilityManifest, size: int) -> None:
    if manifest.max_read_bytes is not None and size > manifest.max_read_bytes:
        raise ToolplaneError("read_limit_exceeded", "requested read exceeds max_read_bytes")


def _read_file(manifest: CapabilityManifest, args: dict[str, Any]) -> dict[str, Any]:
    path = _scoped_path(manifest, args.get("path"), "read")
    if not os.path.isfile(path):
        raise ToolplaneError("not_found", "requested file does not exist")
    offset = args.get("offset", 0)
    limit = args.get("limit")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ToolplaneError("invalid_args", "offset must be a non-negative integer")
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 0):
        raise ToolplaneError("invalid_args", "limit must be a non-negative integer")
    size = os.path.getsize(path)
    requested = max(0, size - offset) if limit is None else min(limit, max(0, size - offset))
    _limit_read(manifest, requested)
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            handle.seek(offset)
            content = handle.read(limit)
    except OSError as exc:
        raise ToolplaneError("read_failed", "file could not be read") from exc
    _READ_BEFORE_WRITE.record_read(manifest, path)
    return _ok({"path": path, "content": content, "bytes": len(content.encode("utf-8"))})


def _list_files(manifest: CapabilityManifest, args: dict[str, Any]) -> dict[str, Any]:
    path = _scoped_path(manifest, args.get("path"), "read")
    recursive = args.get("recursive", False)
    if not isinstance(recursive, bool):
        raise ToolplaneError("invalid_args", "recursive must be a boolean")
    if not os.path.isdir(path):
        raise ToolplaneError("not_found", "requested directory does not exist")
    found: list[str] = []
    iterator = os.walk(path) if recursive else [(path, [], os.listdir(path))]
    for directory, _dirs, names in iterator:
        for name in sorted(names):
            candidate = os.path.join(directory, name)
            resolved = os.path.realpath(candidate)
            if (
                os.path.isfile(resolved)
                and any(_within(resolved, root) for root in _roots(manifest.read_roots, "read"))
                and not references_secret(resolved, os.getcwd())
            ):
                found.append(resolved)
    return _ok({"files": found})


def _fallback_search(manifest: CapabilityManifest, path: str, pattern: str) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    files = (
        [path] if os.path.isfile(path) else [str(p) for p in Path(path).rglob("*") if p.is_file()]
    )
    for filename in files:
        try:
            filename = _scoped_path(manifest, filename, "read")
        except ToolplaneError:
            continue
        try:
            with open(filename, encoding="utf-8", errors="replace") as handle:
                for line_no, line in enumerate(handle, 1):
                    if pattern in line:
                        matches.append(
                            {
                                "path": os.path.realpath(filename),
                                "line": line_no,
                                "text": line.rstrip("\n"),
                            }
                        )
        except OSError:
            continue
    return matches


def _search_files(manifest: CapabilityManifest, args: dict[str, Any]) -> dict[str, Any]:
    path = _scoped_path(manifest, args.get("path") or args.get("root"), "read")
    pattern = args.get("pattern")
    if not isinstance(pattern, str) or not pattern:
        raise ToolplaneError("invalid_args", "pattern must be a non-empty string")
    if not os.path.exists(path):
        raise ToolplaneError("not_found", "requested search path does not exist")
    rg = shutil.which("rg")
    if not rg:
        return _ok({"matches": _fallback_search(manifest, path, pattern), "engine": "python"})
    completed = subprocess.run(  # noqa: S603 -- fixed executable and argv list
        [rg, "-n", "--no-heading", "--color", "never", "--", pattern, path],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode not in (0, 1):
        raise ToolplaneError("search_failed", "search command failed")
    matches: list[dict[str, Any]] = []
    for line in completed.stdout.splitlines():
        filename, separator, remaining = line.partition(":")
        line_no, separator2, text = remaining.partition(":")
        if not separator or not separator2 or not line_no.isdigit():
            continue
        resolved = os.path.realpath(filename)
        try:
            _scoped_path(manifest, resolved, "read")
        except ToolplaneError:
            continue
        matches.append({"path": resolved, "line": int(line_no), "text": text})
    return _ok({"matches": matches, "engine": "rg"})


def _hash_file(manifest: CapabilityManifest, args: dict[str, Any]) -> dict[str, Any]:
    path = _scoped_path(manifest, args.get("path"), "read")
    if not os.path.isfile(path):
        raise ToolplaneError("not_found", "requested file does not exist")
    _limit_read(manifest, os.path.getsize(path))
    try:
        digest = _sha256_file(path)
    except OSError as exc:
        raise ToolplaneError("read_failed", "file could not be hashed") from exc
    _READ_BEFORE_WRITE.record_read(manifest, path, digest)
    return _ok({"path": path, "sha256": digest})


def _write_file(manifest: CapabilityManifest, args: dict[str, Any]) -> dict[str, Any]:
    path = _scoped_path(manifest, args.get("path"), "write")
    content = args.get("content")
    if not isinstance(content, str):
        raise ToolplaneError("invalid_args", "content must be a string")
    _READ_BEFORE_WRITE.require_read(manifest, path)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)
    except OSError as exc:
        raise ToolplaneError("write_failed", "file could not be written") from exc
    return _ok({"path": path, "bytes": len(content.encode("utf-8"))})


def _edit_file(manifest: CapabilityManifest, args: dict[str, Any]) -> dict[str, Any]:
    path = _scoped_path(manifest, args.get("path"), "write")
    old_str, new_str = args.get("old_str"), args.get("new_str")
    if not isinstance(old_str, str) or not isinstance(new_str, str) or not old_str:
        raise ToolplaneError(
            "invalid_args", "old_str and new_str must be strings; old_str cannot be empty"
        )
    _READ_BEFORE_WRITE.require_read(manifest, path)
    try:
        content = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ToolplaneError("not_found", "requested file does not exist") from exc
    count = content.count(old_str)
    if count != 1:
        raise ToolplaneError("edit_conflict", "old_str must occur exactly once")
    try:
        Path(path).write_text(content.replace(old_str, new_str, 1), encoding="utf-8")
    except OSError as exc:
        raise ToolplaneError("write_failed", "file could not be edited") from exc
    return _ok({"path": path, "replacements": 1})


def _make_dir(manifest: CapabilityManifest, args: dict[str, Any]) -> dict[str, Any]:
    path = _scoped_path(manifest, args.get("path"), "write")
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as exc:
        raise ToolplaneError("write_failed", "directory could not be created") from exc
    return _ok({"path": path})


def _run_test(manifest: CapabilityManifest, args: dict[str, Any]) -> dict[str, Any]:
    argv, cwd_raw = args.get("argv"), args.get("cwd")
    if (
        not isinstance(argv, list)
        or not argv
        or not all(isinstance(item, str) and item for item in argv)
    ):
        raise ToolplaneError("invalid_args", "argv must be a non-empty list of strings")
    try:
        cwd = _scoped_path(manifest, cwd_raw, "write")
    except ToolplaneError as write_error:
        if write_error.error != "out_of_scope":
            raise
        cwd = _scoped_path(manifest, cwd_raw, "read")
    if not os.path.isdir(cwd):
        raise ToolplaneError("not_found", "cwd does not exist")
    timeout = manifest.max_subprocess_seconds
    try:
        completed = subprocess.run(  # noqa: S603 -- argv is passed without a shell
            argv, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise ToolplaneError(
            "subprocess_timeout", "command exceeded max_subprocess_seconds"
        ) from exc
    except OSError as exc:
        raise ToolplaneError("subprocess_failed", "command could not be started") from exc
    return _ok(
        {"returncode": completed.returncode, "stdout": completed.stdout, "stderr": completed.stderr}
    )


def _image_info(manifest: CapabilityManifest, args: dict[str, Any]) -> dict[str, Any]:
    path = _scoped_path(manifest, args.get("path"), "read")
    try:
        from PIL import Image  # type: ignore[import-not-found]
    except ImportError:
        return {"ok": False, "error": "unavailable", "detail": "Pillow is not installed"}
    try:
        with Image.open(path) as image:
            return _ok(
                {
                    "path": path,
                    "format": image.format,
                    "width": image.width,
                    "height": image.height,
                    "mode": image.mode,
                }
            )
    except (OSError, ValueError) as exc:
        raise ToolplaneError("image_failed", "image could not be inspected") from exc


def _validate_artifact_output(manifest: CapabilityManifest, args: dict[str, Any]) -> dict[str, Any]:
    outputs = args.get("outputs")
    if not isinstance(outputs, list) or not all(isinstance(item, str) for item in outputs):
        raise ToolplaneError("invalid_args", "outputs must be a list of strings")
    try:
        from omniagentos.workmodes.modes import WorkModeError, validate_artifact_outputs

        return _ok({"outputs": list(validate_artifact_outputs(outputs))})
    except ImportError:
        return {"ok": False, "error": "unavailable", "detail": "artifact validation is unavailable"}
    except WorkModeError as exc:
        raise ToolplaneError("invalid_artifact_output", str(exc)) from exc


def _connector_invoke(manifest: CapabilityManifest, args: dict[str, Any]) -> dict[str, Any]:
    """Invoke a connector capability with SERVER-SIDE grant validation.

    Security Fix: S1-selfgrant
    The granted capabilities are derived from the manifest (server-side), not
    from the caller's assertion. This prevents a caller from self-granting
    capabilities it does not actually hold.
    """
    cap_id = args.get("cap_id")
    if not isinstance(cap_id, str) or not cap_id:
        raise ToolplaneError("invalid_args", "cap_id is required")

    # S1: Reject caller-supplied grants, use server-derived manifest grants.
    # The manifest.connector_grants is populated by the framework/orchestrator
    # and is the authoritative source of truth for what capabilities this
    # session may invoke.
    granted = manifest.connector_grants
    if not granted:
        # Fail closed: if no grants were provided in the manifest, deny the call.
        # The framework should have populated connector_grants if the session
        # should have connector access.
        raise ToolplaneError(
            "unauthorized",
            "no connector capabilities granted for this session"
        )

    try:
        from omniagentos.connectors import broker

        broker.authorize(cap_id, granted, approval_token=args.get("approval_token"))
        result = broker.call(
            cap_id,
            granted,
            method=args.get("method", "GET"),
            path=args.get("path", "/"),
            query=args.get("query"),
            body=args.get("body"),
            form=args.get("form"),
            approval_token=args.get("approval_token"),
        )
    except Exception as exc:  # BrokerDenied stays isolated from toolplane's public contract.
        from omniagentos.connectors.broker import BrokerDenied

        if isinstance(exc, BrokerDenied):
            return {"ok": False, "error": "broker_denied", "detail": "connector request was denied"}
        return {"ok": False, "error": "broker_error", "detail": "connector request failed"}
    return _ok(result)


def _globex_generate_image(
    manifest: CapabilityManifest, args: dict[str, Any]
) -> dict[str, Any]:
    """Local Globex Studio image generation (facade wired into toolplane).

    Production code calls the real supported provider seam. Artifact verification
    is mandatory: ok:true requires a produced file that exists and passes checks.
    Fake bytes belong ONLY in tests via provider injection.
    """
    _allow(manifest, "globex_generate_image")
    raw_payload = args.get("payload")
    payload: dict[str, Any] = raw_payload if isinstance(raw_payload, dict) else args
    try:
        # Extract and validate output_path for containment before any generation
        output_path = args.get("output_path") or payload.get("output_path")
        if not output_path:
            raise ToolplaneError("missing_output", "output_path is required for media generation")
        target = _scoped_path(manifest, output_path, "write")

        # Call the real supported provider seam
        from omniagentos.connectors.globex_studio import generate_image

        # Build generation payload (exclude output_path from API call)
        gen_payload = {k: v for k, v in (payload or {}).items() if k != "output_path"}
        gen_payload["output_path"] = target  # Pass scoped target to provider

        result = generate_image(gen_payload)

        if not result.ok:
            raise ToolplaneError(
                "generation_failed",
                f"provider returned error: {result.error or result.status_code}",
            )

        # Verify produced artifact exists and is non-empty (identity check)
        if not os.path.exists(target):
            raise ToolplaneError(
                "missing_artifact", "provider did not produce artifact at target path"
            )
        if os.path.getsize(target) == 0:
            raise ToolplaneError("empty_artifact", "produced artifact is empty")

        # Compute artifact hash for provenance
        artifact_hash = hashlib.sha256()
        with open(target, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                artifact_hash.update(chunk)

        return _ok(
            {
                "ok": True,
                "path": target,
                "artifact_sha256": artifact_hash.hexdigest(),
                "artifact_bytes": os.path.getsize(target),
                "status_code": result.status_code,
                "body": result.body,
                "error": None,
            }
        )
    except ToolplaneError as exc:
        return {"ok": False, "error": exc.error, "detail": exc.detail}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "studio_failed", "detail": str(exc)}


def _globex_generate_video(
    manifest: CapabilityManifest, args: dict[str, Any]
) -> dict[str, Any]:
    """Local Globex Studio video generation (facade wired into toolplane).

    Production code calls the real supported provider seam. Artifact verification
    is mandatory: ok:true requires a produced file that exists and passes checks.
    Fake bytes belong ONLY in tests via provider injection.
    """
    _allow(manifest, "globex_generate_video")
    raw_payload = args.get("payload")
    payload: dict[str, Any] = raw_payload if isinstance(raw_payload, dict) else args
    try:
        # Extract and validate output_path for containment before any generation
        output_path = args.get("output_path") or payload.get("output_path")
        if not output_path:
            raise ToolplaneError("missing_output", "output_path is required for media generation")
        target = _scoped_path(manifest, output_path, "write")

        # Call the real supported provider seam
        from omniagentos.connectors.globex_studio import generate_video

        # Build generation payload (exclude output_path from API call)
        gen_payload = {k: v for k, v in (payload or {}).items() if k != "output_path"}
        gen_payload["output_path"] = target  # Pass scoped target to provider

        result = generate_video(gen_payload)

        if not result.ok:
            raise ToolplaneError(
                "generation_failed",
                f"provider returned error: {result.error or result.status_code}",
            )

        # Verify produced artifact exists and is non-empty (identity check)
        if not os.path.exists(target):
            raise ToolplaneError(
                "missing_artifact", "provider did not produce artifact at target path"
            )
        if os.path.getsize(target) == 0:
            raise ToolplaneError("empty_artifact", "produced artifact is empty")

        # Compute artifact hash for provenance
        artifact_hash = hashlib.sha256()
        with open(target, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                artifact_hash.update(chunk)

        return _ok(
            {
                "ok": True,
                "path": target,
                "artifact_sha256": artifact_hash.hexdigest(),
                "artifact_bytes": os.path.getsize(target),
                "status_code": result.status_code,
                "body": result.body,
                "error": None,
            }
        )
    except ToolplaneError as exc:
        return {"ok": False, "error": exc.error, "detail": exc.detail}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "studio_failed", "detail": str(exc)}


def _voice_tts(manifest: CapabilityManifest, args: dict[str, Any]) -> dict[str, Any]:
    """Synthesize a scoped audio artifact through the normal dispatch gate."""
    _allow(manifest, "voice_tts")
    try:
        text = args.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ToolplaneError("invalid_args", "text is required")
        output_path = args.get("output_path")
        if not output_path:
            raise ToolplaneError("missing_output", "output_path is required for voice synthesis")
        target = _scoped_path(manifest, output_path, "write")

        injected_provider = args.get("_provider")
        if callable(injected_provider):
            provider_result = injected_provider(text, voice_id=args.get("voice_id"))
            audio = (
                provider_result
                if isinstance(provider_result, bytes)
                else getattr(provider_result, "audio", None)
            )
            provider_name = str(getattr(provider_result, "provider", "injected"))
            detail = str(getattr(provider_result, "detail", "synthesis failed"))
        else:
            from omniagentos.steward.config import load_steward_config
            from omniagentos.voice.service import get_provider

            cfg = load_steward_config().voice
            provider_name = str(args.get("provider") or cfg.default_provider)
            provider = get_provider(provider_name, cfg)
            if provider is None:
                raise ToolplaneError("invalid_args", f"unknown voice provider: {provider_name}")
            provider_result = provider.synthesize(text, voice_id=args.get("voice_id"))
            audio = provider_result.audio if provider_result.ok else None
            detail = provider_result.detail

        if not isinstance(audio, bytes) or not audio:
            raise ToolplaneError("generation_failed", detail)
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "wb") as handle:
                handle.write(audio)
        except OSError as exc:
            raise ToolplaneError("write_failed", "audio artifact could not be written") from exc

        if not os.path.isfile(target):
            raise ToolplaneError(
                "missing_artifact", "provider did not produce artifact at target path"
            )
        artifact_bytes = os.path.getsize(target)
        if artifact_bytes == 0:
            raise ToolplaneError("empty_artifact", "produced artifact is empty")
        artifact_hash = hashlib.sha256()
        with open(target, "rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                artifact_hash.update(chunk)
        return _ok(
            {
                "path": target,
                "artifact_sha256": artifact_hash.hexdigest(),
                "artifact_bytes": artifact_bytes,
                "provider": provider_name,
            }
        )
    except ToolplaneError as exc:
        return {"ok": False, "error": exc.error, "detail": exc.detail}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "voice_failed", "detail": str(exc)}


def _repomap(manifest: CapabilityManifest, args: dict[str, Any]) -> dict[str, Any]:
    """Query the repository code map for structure and symbols."""
    try:
        from omniagentos.contracts import _repo_root
        from omniagentos.repomap import build_repo_map

        repo_dir = args.get("repo_dir")
        if not repo_dir:
            repo_dir = _repo_root()
        elif not isinstance(repo_dir, str) or not repo_dir.strip():
            return {"ok": False, "error": "invalid_args", "detail": "repo_dir must be a string"}

        focus_files = args.get("focus_files")
        focus_terms = args.get("focus_terms")
        max_tokens = args.get("max_tokens", 1024)

        if focus_files is not None and not isinstance(focus_files, list):
            return {"ok": False, "error": "invalid_args", "detail": "focus_files must be a list"}
        if focus_terms is not None and not isinstance(focus_terms, list):
            return {"ok": False, "error": "invalid_args", "detail": "focus_terms must be a list"}
        if not isinstance(max_tokens, int) or max_tokens < 1:
            return {
                "ok": False,
                "error": "invalid_args",
                "detail": "max_tokens must be a positive integer",
            }

        map_str = build_repo_map(
            repo_dir,
            focus_files=focus_files or None,
            focus_terms=focus_terms or None,
            max_tokens=max_tokens,
        )
        return {"ok": True, "result": map_str}
    except ImportError:
        return {"ok": False, "error": "unavailable", "detail": "repomap subsystem is unavailable"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "repomap_failed", "detail": str(exc)}


def _semantic_search(manifest: CapabilityManifest, args: dict[str, Any]) -> dict[str, Any]:
    """Search files semantically via pgvector embeddings."""
    try:
        from omniagentos.filesearch.semantic import SemanticUnavailable, semantic_query

        query = args.get("query")
        if not isinstance(query, str) or not query.strip():
            return {
                "ok": False,
                "error": "invalid_args",
                "detail": "query is required and must be non-empty",
            }

        root = args.get("root")
        category = args.get("category")
        limit = args.get("limit", 20)

        if not isinstance(limit, int) or limit < 1:
            return {
                "ok": False,
                "error": "invalid_args",
                "detail": "limit must be a positive integer",
            }

        rows = semantic_query(query, root=root, category=category, limit=limit)
        return {"ok": True, "result": rows}
    except ImportError:
        return {
            "ok": False,
            "error": "unavailable",
            "detail": "semantic search subsystem is unavailable",
        }
    except SemanticUnavailable:
        return {
            "ok": False,
            "error": "unavailable",
            "detail": "pgvector or embedder unavailable",
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "search_failed", "detail": str(exc)}


def _knowledge_recall(manifest: CapabilityManifest, args: dict[str, Any]) -> dict[str, Any]:
    """Recall learned facts from the knowledge store using hybrid retrieval."""
    try:
        query = args.get("query")
        if not isinstance(query, str) or not query.strip():
            return {
                "ok": False,
                "error": "invalid_args",
                "detail": "query is required and must be non-empty",
            }

        raw_scope = args.get("scope")
        # Validate scope: must be a 2-element sequence of strings if provided.
        # Normalized to tuple[str, str] because retrieval.recall.recall() is typed
        # for exactly that; a bare list would unpack fine at runtime but violates
        # the contract the two lanes agreed on.
        scope: tuple[str, str] | None = None
        if raw_scope is not None:
            if (
                not isinstance(raw_scope, (list, tuple))
                or len(raw_scope) != 2
                or not all(isinstance(part, str) for part in raw_scope)
            ):
                return {
                    "ok": False,
                    "error": "invalid_args",
                    "detail": "scope must be a 2-element sequence of strings [scope_type, scope_id]",
                }
            scope = (str(raw_scope[0]), str(raw_scope[1]))

        top_k = args.get("top_k", 8)
        sources = args.get("sources")

        if not isinstance(top_k, int) or top_k < 1:
            return {
                "ok": False,
                "error": "invalid_args",
                "detail": "top_k must be a positive integer",
            }

        # FIRST: Try the new retrieval.recall module (parallel development)
        recall_fn: Callable[..., Any] | None
        try:
            from omniagentos.retrieval.recall import recall as _recall

            recall_fn = _recall
        except ImportError:
            recall_fn = None

        if recall_fn is not None:
            # New module available: use it
            lines = recall_fn(query, scope=scope, top_k=top_k, sources=sources)
            # Convert RecallLine objects to dicts
            result = [
                {"text": line.text, "source": line.source, "score": line.score, "ref": line.ref}
                for line in lines
            ]
            return {"ok": True, "result": result}

        # FALLBACK: Use the existing knowledge.recall module
        try:
            from omniagentos.knowledge import config as knowledge_config
            from omniagentos.knowledge.recall import _get_store, recall

            # Check if knowledge subsystem is enabled
            if not knowledge_config.knowledge_enabled():
                return {
                    "ok": False,
                    "error": "unavailable",
                    "detail": "knowledge subsystem is disabled",
                }

            # Get the process-wide knowledge store and call recall
            store = _get_store()
            result_obj = recall(store, prompt=query, run_id=None)
            # Extract facts from RecallResult
            result = []
            for recalled_fact in result_obj.facts[:top_k]:
                # recalled_fact is a RecalledFact with .fact (Fact) and .score
                fact = recalled_fact.fact
                result.append(
                    {
                        "text": fact.statement,
                        "source": str(fact.provenance),
                        "score": recalled_fact.score,
                        "ref": f"fact_id:{fact.id}",
                    }
                )
            return {"ok": True, "result": result}
        except ImportError:
            return {
                "ok": False,
                "error": "unavailable",
                "detail": "knowledge subsystem is unavailable",
            }

    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "recall_failed", "detail": str(exc)}


def _memory_search(manifest: CapabilityManifest, args: dict[str, Any]) -> dict[str, Any]:
    """Search metacognitive memory records (lessons, procedures, warnings)."""
    try:
        from omniagentos.metacog.service import MetacogService

        query = args.get("query")
        if not isinstance(query, str) or not query.strip():
            return {
                "ok": False,
                "error": "invalid_args",
                "detail": "query is required and must be non-empty",
            }

        limit = args.get("limit", 20)
        if not isinstance(limit, int) or limit < 1:
            return {
                "ok": False,
                "error": "invalid_args",
                "detail": "limit must be a positive integer",
            }

        service = MetacogService()
        # Filter to promoted records only, excluding shadow candidates
        memories = service.search_memory(query, limit=limit, statuses=["promoted"])

        result = []
        for mem in memories:
            result.append(
                {
                    "id": mem.id,
                    "text": mem.statement,
                    "type": mem.type,
                    "confidence": mem.confidence,
                    "sample_count": mem.sample_count,
                    "success_count": mem.success_count,
                    "created_at": mem.created_at,
                    "promotion_status": mem.promotion_status,
                }
            )
        return {"ok": True, "result": result}
    except ImportError:
        return {"ok": False, "error": "unavailable", "detail": "metacog subsystem is unavailable"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "memory_failed", "detail": str(exc)}


def _vault_search(manifest: CapabilityManifest, args: dict[str, Any]) -> dict[str, Any]:
    """Search vault notes and MOCs for broad knowledge queries."""
    try:
        from omniagentos.contracts import default_vault_dir
        from omniagentos.vaultgraph.search import global_search

        query = args.get("query")
        if not isinstance(query, str) or not query.strip():
            return {
                "ok": False,
                "error": "invalid_args",
                "detail": "query is required and must be non-empty",
            }

        vault_dir = args.get("vault_dir")
        if not vault_dir:
            vault_dir = default_vault_dir()
        elif not isinstance(vault_dir, str) or not vault_dir.strip():
            return {"ok": False, "error": "invalid_args", "detail": "vault_dir must be a string"}

        limit = args.get("limit", 5)
        if not isinstance(limit, int) or limit < 1:
            return {
                "ok": False,
                "error": "invalid_args",
                "detail": "limit must be a positive integer",
            }

        hits = global_search(query, vault_dir, limit=limit)

        result = []
        for hit in hits:
            result.append(
                {
                    "relpath": hit.relpath,
                    "title": hit.title,
                    "snippet": hit.snippet,
                    "score": hit.score,
                }
            )
        return {"ok": True, "result": result}
    except ImportError:
        return {
            "ok": False,
            "error": "unavailable",
            "detail": "vaultgraph subsystem is unavailable",
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "vault_failed", "detail": str(exc)}


TOOLS: dict[str, Tool] = {
    "read_file": _read_file,
    "list_files": _list_files,
    "search_files": _search_files,
    "hash_file": _hash_file,
    "write_file": _write_file,
    "edit_file": _edit_file,
    "make_dir": _make_dir,
    "run_test": _run_test,
    "image_info": _image_info,
    "validate_artifact_output": _validate_artifact_output,
    "connector_invoke": _connector_invoke,
    "globex_generate_image": _globex_generate_image,
    "globex_generate_video": _globex_generate_video,
    "voice_tts": _voice_tts,
    "repomap": _repomap,
    "semantic_search": _semantic_search,
    "knowledge_recall": _knowledge_recall,
    "memory_search": _memory_search,
    "vault_search": _vault_search,
}


def dispatch(tool_name: str, manifest: CapabilityManifest, args: dict[str, Any]) -> dict[str, Any]:
    """Authorize and run one registered tool, always returning a scrubbed result.

    Deny order (fail-closed):
    1. Missing holder_generation -> reject (no identity)
    2. Unknown tool -> reject with "unknown_capability" (deny-by-default)
    3. Tool not in allowed_ops -> reject with "not_allowed"
    4. Invalid args -> reject
    5. Execute and return scrubbed result
    """
    try:
        if getattr(manifest, "holder_generation", None) is None:
            raise ToolplaneError(
                "missing_holder_generation", "holder_generation is required for every tool call"
            )
        # Deny unknown capabilities FIRST (M-07: deny unknown capabilities)
        tool = TOOLS.get(tool_name)
        if tool is None:
            raise ToolplaneError("unknown_capability", f"unknown tool: {tool_name}")
        # Then check if allowed in manifest
        _allow(manifest, tool_name)
        if not isinstance(args, dict):
            raise ToolplaneError("invalid_args", "args must be an object")
        result = tool(manifest, args)
    except ToolplaneError as exc:
        result = {"ok": False, "error": exc.error, "detail": exc.detail}
    except Exception:
        result = {"ok": False, "error": "internal_error", "detail": "tool execution failed"}
    return sanitize_for_persistence("toolplane_result", result)[0]


# Capability inventory for bypass audit (M-08: publish bypass inventory)
CAPABILITY_INVENTORY: dict[str, dict[str, Any]] = {
    "read_file": {"category": "fs_read", "risk": "low", "requires_scope": True},
    "list_files": {"category": "fs_read", "risk": "low", "requires_scope": True},
    "search_files": {"category": "fs_read", "risk": "low", "requires_scope": True},
    "hash_file": {"category": "fs_read", "risk": "low", "requires_scope": True},
    "write_file": {"category": "fs_write", "risk": "medium", "requires_scope": True},
    "edit_file": {"category": "fs_write", "risk": "medium", "requires_scope": True},
    "make_dir": {"category": "fs_write", "risk": "low", "requires_scope": True},
    "run_test": {"category": "subprocess", "risk": "high", "requires_scope": True},
    "image_info": {"category": "fs_read", "risk": "low", "requires_scope": True},
    "validate_artifact_output": {"category": "validation", "risk": "low", "requires_scope": False},
    "connector_invoke": {"category": "network", "risk": "high", "requires_scope": False},
    "globex_generate_image": {"category": "media", "risk": "medium", "requires_scope": True},
    "globex_generate_video": {"category": "media", "risk": "medium", "requires_scope": True},
    "voice_tts": {"category": "media", "risk": "medium", "requires_scope": True},
    "repomap": {"category": "fs_read", "risk": "low", "requires_scope": True},
    "semantic_search": {"category": "fs_read", "risk": "low", "requires_scope": True},
    "knowledge_recall": {"category": "validation", "risk": "low", "requires_scope": False},
    "memory_search": {"category": "validation", "risk": "low", "requires_scope": False},
    "vault_search": {"category": "fs_read", "risk": "low", "requires_scope": True},
}


def get_capability_inventory() -> dict[str, dict[str, Any]]:
    """Return the capability inventory for bypass audit."""
    return dict(CAPABILITY_INVENTORY)
