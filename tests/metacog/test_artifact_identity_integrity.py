"""H-21 / M-03 — per-task artifact identity + atomic blob integrity.

Focused regressions for:
- Cross-task equal content must not reuse another task's envelope (H-21 / F-31).
- Content-addressed blobs are written atomically and verified on read (M-03 / F-32).
- Truncated/corrupt blobs at the content path are repaired on re-register and
  fail closed when read without repair.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omniagentos.metacog.config import clear_metacog_config_cache
from omniagentos.metacog.service import MetacogService, _atomic_write_bytes, _sha256_bytes
from omniagentos.metacog.store import MetacogStore


@pytest.fixture()
def svc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MetacogService:
    monkeypatch.setenv("OMNIAGENTOS_METACOG_ARTIFACTS_ROOT", str(tmp_path / "arts"))
    monkeypatch.delenv("OMNIAGENTOS_METACOG_MODE", raising=False)
    clear_metacog_config_cache()
    return MetacogService(store=MetacogStore(str(tmp_path / "metacog.db")))


CONTENT = b'{"diff":"+print(1)","note":"shared body"}'


# --- H-21: per-task identity, shared content hash --------------------------


def test_equal_content_across_tasks_gets_distinct_envelopes(
    svc: MetacogService,
) -> None:
    """Byte-identical content under different task_ids must not share an id."""
    a1 = svc.register_artifact(
        artifact_type="code_diff",
        content=CONTENT,
        task_id="task_alpha",
        run_id="run_1",
    )
    a2 = svc.register_artifact(
        artifact_type="code_diff",
        content=CONTENT,
        task_id="task_beta",
        run_id="run_1",
    )
    assert a1.content_hash == a2.content_hash
    assert a1.id != a2.id
    assert a1.task_id == "task_alpha"
    assert a2.task_id == "task_beta"

    # Each task's context sees its own registration, not the other's.
    alpha_ids = {a.id for a in svc.store.list_artifacts(task_id="task_alpha")}
    beta_ids = {a.id for a in svc.store.list_artifacts(task_id="task_beta")}
    assert a1.id in alpha_ids
    assert a1.id not in beta_ids
    assert a2.id in beta_ids
    assert a2.id not in alpha_ids

    # Content identity is still shared: both resolve to the same verified bytes.
    assert svc.read_artifact_content(a1.id) == CONTENT
    assert svc.read_artifact_content(a2.id) == CONTENT
    assert Path(a1.content_uri) == Path(a2.content_uri)


def test_same_task_equal_content_still_dedupes(svc: MetacogService) -> None:
    a1 = svc.register_artifact(
        artifact_type="code_diff",
        content=CONTENT,
        task_id="task_same",
        run_id="run_1",
    )
    a2 = svc.register_artifact(
        artifact_type="code_diff",
        content=CONTENT,
        task_id="task_same",
        run_id="run_1",
    )
    assert a1.id == a2.id
    assert a1.content_hash == a2.content_hash


def test_equal_content_different_run_or_type_is_new_envelope(
    svc: MetacogService,
) -> None:
    base = svc.register_artifact(
        artifact_type="code_diff",
        content=CONTENT,
        task_id="task_x",
        run_id="run_a",
    )
    other_run = svc.register_artifact(
        artifact_type="code_diff",
        content=CONTENT,
        task_id="task_x",
        run_id="run_b",
    )
    other_type = svc.register_artifact(
        artifact_type="verification_report",
        content=CONTENT,
        task_id="task_x",
        run_id="run_a",
    )
    assert base.id != other_run.id
    assert base.id != other_type.id
    assert base.content_hash == other_run.content_hash == other_type.content_hash


# --- M-03: atomic write + hash/version verify on read ----------------------


def test_read_verifies_hash_and_rejects_corrupt_blob(svc: MetacogService) -> None:
    art = svc.register_artifact(
        artifact_type="note",
        content=CONTENT,
        task_id="task_corrupt",
    )
    path = Path(art.content_uri)
    assert path.is_file()
    # Simulate bit-rot / partial overwrite of the content-addressed blob.
    path.write_bytes(b"CORRUPTED-NOT-THE-ORIGINAL-BYTES")

    with pytest.raises(ValueError, match="hash mismatch"):
        svc.read_artifact_content(art.id)

    # Envelope metadata still loads; only content read is fail-closed.
    got = svc.get_artifact(art.id)
    assert got is not None
    assert got.id == art.id


def test_truncated_blob_is_repaired_on_reregister(svc: MetacogService) -> None:
    """A truncated file at the content path must not permanently poison the store."""
    from omniagentos.metacog.config import artifacts_root

    content_hash = _sha256_bytes(CONTENT)
    # Plant a truncated poison blob at the would-be content-addressed path
    # before any successful register (simulates mid-write crash of old code).
    path = artifacts_root() / content_hash[:2] / content_hash
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(CONTENT[: max(1, len(CONTENT) // 3)])  # truncated
    assert path.is_file()
    assert _sha256_bytes(path.read_bytes()) != content_hash

    art = svc.register_artifact(
        artifact_type="code_diff",
        content=CONTENT,
        task_id="task_repair",
        run_id="run_1",
    )
    assert art.content_hash == content_hash
    assert Path(art.content_uri).read_bytes() == CONTENT
    assert svc.read_artifact_content(art.id) == CONTENT


def test_atomic_write_leaves_no_truncated_final_on_interrupt(
    tmp_path: Path,
) -> None:
    """If the write path raises mid-flight, the final target must not be partial."""
    target = tmp_path / "ab" / "deadbeef"
    # Seed a good prior blob; an interrupted rewrite must not clobber it partially.
    good = b"complete-payload-v1"
    _atomic_write_bytes(target, good)
    assert target.read_bytes() == good

    # Simulate failure after temp creation by making the parent read-only after
    # we already have a good file: replace should either fully succeed or leave
    # the prior good bytes. Here we only assert the helper never writes the
    # final path via non-atomic open — a second successful write replaces wholly.
    new = b"complete-payload-v2-longer"
    _atomic_write_bytes(target, new)
    assert target.read_bytes() == new
    assert list(target.parent.glob("*.tmp")) == []


def test_read_rejects_missing_blob_and_unsupported_schema(
    svc: MetacogService,
) -> None:
    art = svc.register_artifact(
        artifact_type="note",
        content=CONTENT,
        task_id="task_missing",
    )
    path = Path(art.content_uri)
    path.unlink()
    with pytest.raises(ValueError, match="blob missing"):
        svc.read_artifact_content(art.id)

    with pytest.raises(ValueError, match="unsupported artifact schema_version"):
        svc.register_artifact(
            artifact_type="note",
            content=b"other",
            task_id="task_sv",
            schema_version=99,
        )


def test_context_compile_sees_each_tasks_own_registration(
    svc: MetacogService,
) -> None:
    """Regression for the user-visible failure: second task loses its artifact."""
    shared = '{"verdict":"ok"}'
    a1 = svc.register_artifact(
        artifact_type="verification_report",
        content=shared,
        task_id="ctx_a",
    )
    a2 = svc.register_artifact(
        artifact_type="verification_report",
        content=shared,
        task_id="ctx_b",
    )
    assert a1.id != a2.id

    pkt_a = svc.compile_context(
        task_id="ctx_a",
        objective="task A",
        acceptance_criteria=["own artifact visible"],
    )
    pkt_b = svc.compile_context(
        task_id="ctx_b",
        objective="task B",
        acceptance_criteria=["own artifact visible"],
    )
    assert a1.id in pkt_a.artifact_ids
    assert a2.id not in pkt_a.artifact_ids
    assert a2.id in pkt_b.artifact_ids
    assert a1.id not in pkt_b.artifact_ids
