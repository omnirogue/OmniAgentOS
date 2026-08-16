"""Contained on-disk storage for employee transcript bytes."""

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

from omniagentos.contracts import new_id
from omniagentos.employee_transcripts.models import TRANSCRIPT_ID_PREFIX
from omniagentos.employee_transcripts.store import TranscriptStore
from omniagentos.path_containment import inode_relative_parts
from omniagentos.runtime_paths import (
    TOKEN_VAR_ENV_KEYS,
    resolve_sim_context_or_none,
    resolve_var_root,
)

_LOG = logging.getLogger(__name__)

DEFAULT_MAX_BYTES = 10 * 1024 * 1024
TRANSCRIPT_SOURCES = frozenset(("manual", "chat", "dev_session", "slack"))


class TranscriptStorageError(RuntimeError):
    """A refused or failed contained-storage operation.

    The message is CLIENT-FACING: ``routes/employee_transcripts.py`` passes
    ``str(exc)`` straight into ``fail(...)`` and the app's ApiError handler
    returns it verbatim. So it must never carry an absolute path, a resolved
    candidate path, or a raw OSError repr — those name the transcript root's
    real location on this machine and hand a remote caller the storage layout
    for free (minor 10). The path and the underlying exception are logged
    here instead, and the original exception stays attached as ``__cause__``
    for operators and tests.

    ``kind`` is the machine-readable half and is deliberately unchanged: the
    route maps ``size_cap`` to 413, and the counterfeit corpus keys on it.
    """

    def __init__(self, message: str, *, kind: str = "storage") -> None:
        self.kind = kind
        super().__init__(message)


def _default_root() -> Path:
    """``<campaign root>/transcripts`` under simulation, else the historical path.

    The production branch deliberately ignores ``OMNIAGENTOS_VAR``/``VAR_DIR``:
    this call site never honoured them, the launchers always export them
    (``<repo>/var/runtime``), and honouring them here would silently
    relocate the live ``<repo>/var/transcripts`` tree with no migration.
    """
    if resolve_sim_context_or_none() is not None:
        return resolve_var_root(env_keys=TOKEN_VAR_ENV_KEYS, leaf=("transcripts",))
    return Path(__file__).resolve().parents[2] / "var" / "transcripts"


def _resolve_contained(root: Path, candidate: Path) -> Path:
    """Realpath-resolve and require inode-path ancestry under ``root``.

    Both paths are resolved to absolute form and their inode ancestry is
    verified using the shared inode containment primitive. A candidate path
    with the same inode as root or any symlink spelling is refused.
    """
    root_real = root.resolve(strict=True)
    candidate_real = candidate.resolve(strict=False)
    parts = inode_relative_parts(str(candidate_real), str(root_real))
    if parts is None:
        _LOG.warning(
            "refused transcript path outside root: candidate=%s root=%s", candidate_real, root_real
        )
        raise TranscriptStorageError("resolved path is outside transcript root", kind="containment")
    if not parts:
        raise TranscriptStorageError(
            "transcript path cannot be the storage root", kind="containment"
        )
    if candidate.is_symlink():
        raise TranscriptStorageError("symlink transcript paths are refused", kind="containment")
    return candidate_real


def _validate_filename(filename: str) -> None:
    """Accept a display basename only; it is never used to construct a path."""
    if not filename or filename in {".", ".."}:
        raise TranscriptStorageError("filename must be a non-empty basename", kind="validation")
    if Path(filename).is_absolute() or "/" in filename or "\\" in filename or "\x00" in filename:
        raise TranscriptStorageError(
            "absolute or traversal filename is refused; provide a basename only",
            kind="containment",
        )


class TranscriptStorageService:
    def __init__(
        self,
        store: TranscriptStore,
        *,
        root: Path | str | None = None,
        max_bytes: int | None = None,
    ) -> None:
        self.store = store
        configured_root = root or os.environ.get("OMNIAGENTOS_TRANSCRIPTS_ROOT") or _default_root()
        self.root = Path(configured_root).expanduser()
        self.root.mkdir(parents=True, exist_ok=True)
        self.root = self.root.resolve(strict=True)
        root_stat = self.root.stat()
        self._root_identity = (root_stat.st_dev, root_stat.st_ino)
        configured_cap = (
            str(max_bytes)
            if max_bytes is not None
            else os.environ.get("OMNIAGENTOS_TRANSCRIPTS_MAX_BYTES", str(DEFAULT_MAX_BYTES))
        )
        try:
            self.max_bytes = int(configured_cap)
        except ValueError as exc:
            raise TranscriptStorageError(
                "transcript max bytes must be an integer", kind="config"
            ) from exc
        if self.max_bytes < 0:
            raise TranscriptStorageError("transcript max bytes must be non-negative", kind="config")

    @contextmanager
    def _root_fd(self) -> Iterator[int]:
        """Open the root itself, refusing symlink or inode replacement."""
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self.root, flags)
        except OSError as exc:
            _LOG.warning("cannot securely open transcript root %s: %s", self.root, exc)
            raise TranscriptStorageError(
                "cannot securely open transcript root", kind="containment"
            ) from exc
        try:
            current = os.fstat(descriptor)
            if (current.st_dev, current.st_ino) != self._root_identity:
                raise TranscriptStorageError("transcript root inode changed", kind="containment")
            yield descriptor
        finally:
            os.close(descriptor)

    @staticmethod
    def _leaf_syntax(storage_path: str) -> str:
        if (
            not storage_path
            or storage_path in {".", ".."}
            or Path(storage_path).is_absolute()
            or "/" in storage_path
            or "\\" in storage_path
            or "\x00" in storage_path
        ):
            raise TranscriptStorageError(
                "storage path is outside transcript root", kind="containment"
            )
        return storage_path

    def _leaf(self, storage_path: str) -> str:
        storage_path = self._leaf_syntax(storage_path)
        _resolve_contained(self.root, self.root / storage_path)
        return storage_path

    @staticmethod
    def _read_at(root_fd: int, leaf: str) -> bytes:
        descriptor = os.open(leaf, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=root_fd)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise TranscriptStorageError(
                    "transcript path is not a regular file", kind="containment"
                )
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                return handle.read()
        finally:
            os.close(descriptor)

    def _read_leaf(self, storage_path: str) -> bytes:
        leaf = self._leaf(storage_path)
        try:
            with self._root_fd() as root_fd:
                return self._read_at(root_fd, leaf)
        except (FileNotFoundError, TranscriptStorageError):
            raise
        except OSError as exc:
            _LOG.warning("secure transcript read refused for leaf %r: %s", leaf, exc)
            raise TranscriptStorageError(
                "secure transcript read refused", kind="containment"
            ) from exc

    def _unlink_leaf(self, storage_path: str, *, missing_ok: bool = False) -> None:
        leaf = self._leaf(storage_path)
        self._unlink_entry(leaf, missing_ok=missing_ok)

    def _unlink_entry(self, storage_path: str, *, missing_ok: bool = False) -> None:
        """Remove a root-local directory entry without following its target."""
        leaf = self._leaf_syntax(storage_path)
        try:
            with self._root_fd() as root_fd:
                os.unlink(leaf, dir_fd=root_fd)
        except FileNotFoundError:
            if not missing_ok:
                raise

    def _place_content(self, storage_path: str, content: bytes) -> None:
        """Atomically link a fully written temp inode to a new root-local name."""
        leaf = self._leaf(storage_path)
        temporary = f".upload-{uuid4().hex}"
        with self._root_fd() as root_fd:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=root_fd,
            )
            linked = False
            try:
                with os.fdopen(descriptor, "wb", closefd=False) as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                expected_hash = hashlib.sha256(content).hexdigest()
                on_disk_hash = hashlib.sha256(self._read_at(root_fd, temporary)).hexdigest()
                if on_disk_hash != expected_hash:
                    raise TranscriptStorageError(
                        "on-disk hash mismatch before placement", kind="hash"
                    )
                # Atomic no-overwrite placement: a colliding ID or attacker-created
                # entry is refused rather than overwriting existing bytes.
                os.link(
                    temporary,
                    leaf,
                    src_dir_fd=root_fd,
                    dst_dir_fd=root_fd,
                    follow_symlinks=False,
                )
                linked = True
                os.unlink(temporary, dir_fd=root_fd)
            except BaseException as primary:
                if linked:
                    try:
                        os.unlink(leaf, dir_fd=root_fd)
                    except FileNotFoundError:
                        pass
                    except OSError as cleanup_error:
                        primary.add_note(f"final-leaf cleanup also failed: {cleanup_error}")
                try:
                    os.unlink(temporary, dir_fd=root_fd)
                except FileNotFoundError:
                    pass
                except OSError as cleanup_error:
                    primary.add_note(f"temporary cleanup also failed: {cleanup_error}")
                raise
            finally:
                os.close(descriptor)

    def write(
        self,
        employee_id: str,
        filename: str,
        content: bytes,
        *,
        source: str = "manual",
        meta_json: str | None = None,
    ) -> dict[str, Any]:
        if source not in TRANSCRIPT_SOURCES:
            raise TranscriptStorageError(
                f"source must be one of {TRANSCRIPT_SOURCES}", kind="validation"
            )
        _validate_filename(filename)
        # Cap filename size at 512 bytes (encoded)
        filename_bytes = len(filename.encode("utf-8"))
        if filename_bytes > 512:
            raise TranscriptStorageError(
                f"SIZE_CAP: filename is {filename_bytes} bytes and exceeds 512 bytes",
                kind="size_cap",
            )
        # Cap meta_json size at 64 KiB (encoded)
        if meta_json is not None:
            meta_json_bytes = len(meta_json.encode("utf-8"))
            if meta_json_bytes > 65536:
                raise TranscriptStorageError(
                    f"SIZE_CAP: meta_json is {meta_json_bytes} bytes and exceeds 65536 bytes",
                    kind="size_cap",
                )
        if not isinstance(content, bytes):
            raise TranscriptStorageError("content must be bytes", kind="validation")
        if len(content) > self.max_bytes:
            raise TranscriptStorageError(
                f"SIZE_CAP: content is {len(content)} bytes and exceeds {self.max_bytes} bytes",
                kind="size_cap",
            )

        content_hash = hashlib.sha256(content).hexdigest()
        transcript_id = new_id(TRANSCRIPT_ID_PREFIX)
        relative_path = f"{transcript_id}-{content_hash}.bin"
        wrote_destination = False
        row_inserted = False

        def compensate_database(primary: BaseException) -> None:
            """Remove an ambiguously committed row without masking ``primary``."""
            if not wrote_destination or row_inserted:
                return
            try:
                self.store.delete(transcript_id)
            except Exception as cleanup_error:  # noqa: BLE001
                primary.add_note(f"database compensation also failed: {cleanup_error}")

        try:
            self._place_content(relative_path, content)
            wrote_destination = True
            if hashlib.sha256(self._read_leaf(relative_path)).hexdigest() != content_hash:
                raise TranscriptStorageError("on-disk hash mismatch after placement", kind="hash")
            created = self.store.create(
                employee_id=employee_id,
                filename=filename,
                content_hash=content_hash,
                size_bytes=len(content),
                storage_path=relative_path,
                source=source,
                meta_json=meta_json,
                transcript_id=transcript_id,
            )
            row_inserted = True
            return created
        except TranscriptStorageError as exc:
            compensate_database(exc)
            raise
        except sqlite3.IntegrityError as exc:
            _LOG.warning("employee transcript database refusal for %s: %s", employee_id, exc)
            wrapped = TranscriptStorageError(
                "employee transcript database refusal", kind="foreign_key"
            )
            compensate_database(wrapped)
            raise wrapped from exc
        except Exception as exc:
            # The underlying exception can be an OSError naming the transcript
            # root, so it is logged and chained rather than interpolated into a
            # message the API hands back to the caller.
            _LOG.warning("transcript write failed under root %s", self.root, exc_info=True)
            wrapped = TranscriptStorageError("transcript write failed", kind="storage")
            compensate_database(wrapped)
            raise wrapped from exc
        finally:
            if wrote_destination and not row_inserted:
                self._unlink_entry(relative_path, missing_ok=True)

    def read(self, transcript_id: str) -> bytes | None:
        row = self.store.get(transcript_id)
        if row is None:
            return None
        try:
            content = self._read_leaf(str(row["storage_path"]))
        except FileNotFoundError:
            return None
        if hashlib.sha256(content).hexdigest() != str(row["content_hash"]):
            raise TranscriptStorageError("stored transcript hash mismatch", kind="hash")
        return content

    def delete(self, transcript_id: str) -> bool:
        removed: list[tuple[str, bytes]] = []

        def remove_file(row: dict[str, Any]) -> None:
            storage_path = str(row["storage_path"])
            try:
                content = self._read_leaf(storage_path)
            except FileNotFoundError:
                return
            removed.append((storage_path, content))
            self._unlink_leaf(storage_path)

        try:
            return self.store.delete_with(transcript_id, remove_file)
        except Exception as primary:
            # A wrapper/driver can raise after COMMIT. If the row is already
            # absent, the delete succeeded and restoring bytes would create an
            # orphan; report the committed outcome truthfully.
            try:
                if self.store.get(transcript_id) is None:
                    return True
            except Exception as probe_error:  # noqa: BLE001
                primary.add_note(f"delete commit probe also failed: {probe_error}")
            for storage_path, content in removed:
                try:
                    current = self._read_leaf(storage_path)
                except FileNotFoundError:
                    self._place_content(storage_path, content)
                except TranscriptStorageError:
                    # A raced symlink/non-regular entry is safe to unlink by
                    # directory entry (never by following its target), then the
                    # original bytes are restored under the generated leaf.
                    self._unlink_entry(storage_path, missing_ok=True)
                    self._place_content(storage_path, content)
                else:
                    if current != content:
                        self._unlink_entry(storage_path)
                        self._place_content(storage_path, content)
            raise
