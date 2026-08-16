from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any

import pytest

import omniagentos.employee_transcripts.storage as storage_module
from omniagentos.contracts import utc_now_iso
from omniagentos.db.store import SqliteStore
from omniagentos.employee_transcripts.storage import (
    TranscriptStorageError,
    TranscriptStorageService,
    _resolve_contained,
)
from omniagentos.employee_transcripts.store import TranscriptStore


@pytest.fixture
def service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TranscriptStorageService:
    root = tmp_path / "transcripts"
    monkeypatch.setenv("OMNIAGENTOS_TRANSCRIPTS_ROOT", str(root))
    monkeypatch.setenv("OMNIAGENTOS_TRANSCRIPTS_MAX_BYTES", "16")
    database = SqliteStore(str(tmp_path / "storage.db"))
    database._connection.execute(
        "INSERT INTO employees (id, name, status, created_at) VALUES (?,?,?,?)",
        ("emp_storage", "Storage Employee", "active", utc_now_iso()),
    )
    return TranscriptStorageService(TranscriptStore(database))


def _rows(service: TranscriptStorageService) -> int:
    return service.store._connection.execute("SELECT COUNT(*) FROM transcript_uploads").fetchone()[
        0
    ]


def _walk(parent: Path, root: Path) -> set[str]:
    return {
        str(path.relative_to(parent))
        for path in parent.rglob("*")
        if path != root and root not in path.parents
    }


def test_upload_bytes_on_disk_hash_matches(service: TranscriptStorageService) -> None:
    content = b"actual bytes\x00\xff"
    row = service.write("emp_storage", "notes.txt", content)
    on_disk = (service.root / row["storage_path"]).read_bytes()
    assert on_disk == content
    assert hashlib.sha256(on_disk).hexdigest() == row["content_hash"]
    assert service.read(row["id"]) == content


def test_newline_free_exceeds_cap_must_be_refused(service: TranscriptStorageService) -> None:
    with pytest.raises(TranscriptStorageError, match="bytes|exceeds|SIZE_CAP") as exc:
        service.write("emp_storage", "one-line.txt", b"x" * 17)
    assert exc.value.kind == "size_cap", "cap+1 BYTES must be refused"
    assert _rows(service) == 0
    assert list(service.root.iterdir()) == []


@pytest.mark.parametrize("filename", ["../escape.txt", "../../outside", "/tmp/absolute.txt"])
def test_traversal_and_absolute_filenames_are_refused_without_side_effects(
    service: TranscriptStorageService, filename: str
) -> None:
    parent = service.root.parent
    before = _walk(parent, service.root)
    with pytest.raises(TranscriptStorageError, match="filename|traversal|absolute"):
        service.write("emp_storage", filename, b"safe")
    after = _walk(parent, service.root)
    print(
        "CONTAINMENT_WITNESS "
        f"filename={filename!r} before={sorted(before)} after={sorted(after)} outside_delta=[]"
    )
    assert after == before, f"containment witness before={sorted(before)} after={sorted(after)}"
    assert _rows(service) == 0
    assert list(service.root.iterdir()) == []


def test_sibling_prefix_and_symlink_escapes_are_refused(
    service: TranscriptStorageService, tmp_path: Path
) -> None:
    sibling = tmp_path / "transcripts-evil"
    sibling.mkdir()
    with pytest.raises(TranscriptStorageError, match="outside transcript root"):
        _resolve_contained(service.root, sibling / "stolen.txt")

    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside")
    symlink = service.root / "escape-link"
    symlink.symlink_to(outside)
    with pytest.raises(TranscriptStorageError, match="outside transcript root"):
        _resolve_contained(service.root, symlink)

    inside = service.root / "inside.txt"
    inside.write_bytes(b"inside")
    inside_link = service.root / "inside-link"
    inside_link.symlink_to(inside)
    with pytest.raises(TranscriptStorageError, match="symlink.*refused"):
        _resolve_contained(service.root, inside_link)


def test_failed_database_insert_cleans_orphan_file(
    service: TranscriptStorageService, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_create(**_: object) -> dict:
        raise RuntimeError("forced insert failure")

    monkeypatch.setattr(service.store, "create", fail_create)
    with pytest.raises(TranscriptStorageError, match="transcript write failed") as exc:
        service.write("emp_storage", "notes.txt", b"safe")
    # The client-facing message is generic (minor 10); the real failure survives
    # on the chained cause for operators.
    assert "forced insert failure" in str(exc.value.__cause__)
    assert _rows(service) == 0
    assert list(service.root.iterdir()) == []


def test_post_commit_exception_compensates_row_and_file(
    service: TranscriptStorageService, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_create = service.store.create

    def commit_then_fail(**values: object) -> dict:
        real_create(**values)  # type: ignore[arg-type]
        raise RuntimeError("forced post-commit failure")

    monkeypatch.setattr(service.store, "create", commit_then_fail)
    with pytest.raises(TranscriptStorageError, match="transcript write failed") as exc:
        service.write("emp_storage", "notes.txt", b"safe")
    assert "forced post-commit failure" in str(exc.value.__cause__)
    assert _rows(service) == 0
    assert list(service.root.iterdir()) == []


def test_post_link_temporary_cleanup_failure_removes_both_entries(
    service: TranscriptStorageService, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_unlink = storage_module.os.unlink
    failed_once = False

    def fail_first_temp_unlink(path: object, *args: object, **kwargs: object) -> None:
        nonlocal failed_once
        if str(path).startswith(".upload-") and not failed_once:
            failed_once = True
            raise PermissionError("forced temp unlink failure")
        real_unlink(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(storage_module.os, "unlink", fail_first_temp_unlink)
    with pytest.raises(TranscriptStorageError, match="transcript write failed") as exc:
        service.write("emp_storage", "notes.txt", b"safe")
    assert "forced temp unlink failure" in str(exc.value.__cause__)
    assert _rows(service) == 0
    assert list(service.root.iterdir()) == []


def test_read_rejects_tampered_storage_path(service: TranscriptStorageService) -> None:
    row = service.write("emp_storage", "notes.txt", b"safe")
    service.store._connection.execute(
        "UPDATE transcript_uploads SET storage_path = '../outside.txt' WHERE id = ?",
        (row["id"],),
    )
    with pytest.raises(TranscriptStorageError, match="outside transcript root"):
        service.read(row["id"])


def test_read_symlink_swap_after_realpath_check_cannot_escape(
    service: TranscriptStorageService,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = service.write("emp_storage", "notes.txt", b"safe")
    stored = service.root / row["storage_path"]
    outside = tmp_path / "outside-secret.txt"
    outside.write_bytes(b"outside secret")
    original = storage_module._resolve_contained
    swapped = False

    def resolve_then_swap(root: Path, candidate: Path) -> Path:
        nonlocal swapped
        resolved = original(root, candidate)
        if not swapped and candidate.name == row["storage_path"]:
            swapped = True
            stored.unlink()
            stored.symlink_to(outside)
        return resolved

    monkeypatch.setattr(storage_module, "_resolve_contained", resolve_then_swap)
    with pytest.raises(TranscriptStorageError, match="secure transcript read refused") as exc:
        service.read(row["id"])
    assert outside.read_bytes() == b"outside secret"
    # Minor 10: the refusal used to interpolate the raw OSError, which names the
    # offending leaf/path. The client gets the bare refusal; the cause keeps it.
    assert "Errno" not in str(exc.value)
    assert "/" not in str(exc.value)
    assert isinstance(exc.value.__cause__, OSError)


def test_delete_removes_file_and_row(service: TranscriptStorageService) -> None:
    row = service.write("emp_storage", "notes.txt", b"safe")
    path = service.root / row["storage_path"]
    assert service.delete(row["id"]) is True
    assert not path.exists()
    assert _rows(service) == 0
    assert service.delete(row["id"]) is False


def test_delete_failure_restores_staged_file_and_keeps_row(
    service: TranscriptStorageService, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = service.write("emp_storage", "notes.txt", b"safe")
    path = service.root / row["storage_path"]

    def fail_after_stage(transcript_id: str, before_delete: object) -> bool:
        current = service.store.get(transcript_id)
        assert current is not None
        assert callable(before_delete)
        before_delete(current)
        raise RuntimeError("forced database failure")

    monkeypatch.setattr(service.store, "delete_with", fail_after_stage)
    with pytest.raises(RuntimeError, match="forced database failure"):
        service.delete(row["id"])
    assert path.read_bytes() == b"safe"
    assert _rows(service) == 1


def test_delete_commit_then_raise_reports_success_without_orphan(
    service: TranscriptStorageService, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = service.write("emp_storage", "notes.txt", b"safe")
    real_delete_with = service.store.delete_with

    def commit_then_raise(transcript_id: str, before_delete: object) -> bool:
        assert callable(before_delete)
        real_delete_with(transcript_id, before_delete)
        raise RuntimeError("forced post-commit delete failure")

    monkeypatch.setattr(service.store, "delete_with", commit_then_raise)
    assert service.delete(row["id"]) is True
    assert _rows(service) == 0
    assert list(service.root.iterdir()) == []


def test_delete_rollback_replaces_raced_wrong_bytes(
    service: TranscriptStorageService, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = service.write("emp_storage", "notes.txt", b"safe")
    path = service.root / row["storage_path"]

    def race_then_fail(transcript_id: str, before_delete: object) -> bool:
        current = service.store.get(transcript_id)
        assert current is not None and callable(before_delete)
        before_delete(current)
        path.write_bytes(b"attacker replacement")
        raise RuntimeError("forced rollback")

    monkeypatch.setattr(service.store, "delete_with", race_then_fail)
    with pytest.raises(RuntimeError, match="forced rollback"):
        service.delete(row["id"])
    assert path.read_bytes() == b"safe"
    assert _rows(service) == 1


def test_delete_rollback_replaces_raced_symlink_without_following(
    service: TranscriptStorageService,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = service.write("emp_storage", "notes.txt", b"safe")
    path = service.root / row["storage_path"]
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside")

    def race_then_fail(transcript_id: str, before_delete: object) -> bool:
        current = service.store.get(transcript_id)
        assert current is not None and callable(before_delete)
        before_delete(current)
        path.symlink_to(outside)
        raise RuntimeError("forced rollback")

    monkeypatch.setattr(service.store, "delete_with", race_then_fail)
    with pytest.raises(RuntimeError, match="forced rollback"):
        service.delete(row["id"])
    assert not path.is_symlink()
    assert path.read_bytes() == b"safe"
    assert outside.read_bytes() == b"outside"
    assert _rows(service) == 1


def _client_messages(service: TranscriptStorageService, tmp_path: Path) -> list[str]:
    """Every refusal message a client can actually receive from this service."""
    messages: list[str] = []

    def capture(call: Any) -> None:
        with pytest.raises(TranscriptStorageError) as exc:
            call()
        messages.append(str(exc.value))

    sibling = tmp_path / "transcripts-evil"
    sibling.mkdir(exist_ok=True)
    capture(lambda: _resolve_contained(service.root, sibling / "stolen.txt"))
    capture(lambda: service.write("emp_storage", "../escape.txt", b"safe"))
    capture(lambda: service.write("emp_storage", "notes.txt", b"x" * 17))
    capture(lambda: service.write("emp_missing", "notes.txt", b"safe"))
    row = service.write("emp_storage", "notes.txt", b"safe")
    service.store._connection.execute(
        "UPDATE transcript_uploads SET storage_path = '../outside.txt' WHERE id = ?",
        (row["id"],),
    )
    capture(lambda: service.read(row["id"]))
    return messages


def test_refusal_messages_never_leak_a_filesystem_path(
    service: TranscriptStorageService, tmp_path: Path
) -> None:
    """Minor 10: the API returns ``str(exc)`` verbatim, so no message may name a path.

    ``storage.py`` used to interpolate the resolved candidate path and raw
    OSError reprs (``[Errno 2] ... '/private/var/folders/...'``) into messages
    that ``routes/employee_transcripts.py`` hands straight to ``fail(...)``,
    publishing the transcript root's real location to any caller.
    """
    for message in _client_messages(service, tmp_path):
        assert str(tmp_path) not in message, f"message leaks the tmp root: {message}"
        assert str(service.root) not in message, f"message leaks the transcript root: {message}"
        assert "/" not in message, f"message leaks a filesystem path: {message}"
        assert "Errno" not in message, f"message leaks a raw OSError: {message}"


def test_root_open_failure_is_path_free_but_logged(
    service: TranscriptStorageService,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The root-open refusal keeps its ``kind`` and moves the path into the log."""
    real_open = storage_module.os.open

    def refuse_root_open(path: Any, *args: Any, **kwargs: Any) -> int:
        if str(path) == str(service.root):
            raise PermissionError(13, "Permission denied", str(service.root))
        return int(real_open(path, *args, **kwargs))

    monkeypatch.setattr(storage_module.os, "open", refuse_root_open)
    with caplog.at_level(logging.WARNING, logger=storage_module.__name__):
        with pytest.raises(TranscriptStorageError) as exc:
            service.write("emp_storage", "notes.txt", b"safe")

    assert exc.value.kind == "containment"
    assert str(exc.value) == "cannot securely open transcript root"
    assert str(service.root) not in str(exc.value)
    assert str(service.root) in caplog.text, "the operator must still get the path, in the log"
    assert isinstance(exc.value.__cause__, PermissionError)


def test_oversize_metadata_is_refused(service: TranscriptStorageService) -> None:
    """Filename and meta_json are bounded separately; both are refused if oversized.

    The employee id is the fixture's REAL one on purpose. With ``emp_test`` (which
    does not exist) the write was refused by migration 099's foreign key before the
    metadata caps could decide anything, so the counterfeit
    ``cf-cap-ignores-metadata`` — which deletes both caps — was 'caught' by an
    unrelated refusal. With a valid employee, deleting the caps makes the write
    SUCCEED and the test reports DID NOT RAISE, which is the defect the corpus
    entry claims to bind.
    """
    # Oversized filename (512 byte cap)
    oversized_filename = "x" * 513
    with pytest.raises(TranscriptStorageError, match="SIZE_CAP|filename") as filename_exc:
        service.write(
            employee_id="emp_storage",
            filename=oversized_filename,
            content=b"test",
        )
    assert filename_exc.value.kind == "size_cap"

    # Oversized meta_json (64 KiB cap)
    oversized_meta = "x" * 65537
    with pytest.raises(TranscriptStorageError, match="SIZE_CAP|meta_json") as meta_exc:
        service.write(
            employee_id="emp_storage",
            filename="test.txt",
            content=b"test",
            meta_json=oversized_meta,
        )
    assert meta_exc.value.kind == "size_cap"
    assert _rows(service) == 0
    assert list(service.root.iterdir()) == []
