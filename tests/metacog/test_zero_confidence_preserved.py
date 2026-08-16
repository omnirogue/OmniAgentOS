"""Zero confidence must not be rewritten as a flattering default on read.

Defect class: non-result presented as a favourable result.
store._memory_from_row used ``float(row.get("confidence") or 0.5)``, so a
stored confidence of 0.0 was returned as 0.5. That is the classic
``x or default`` zero-collapsing bug: a known failure/zero is presented as
medium confidence.

Downstream, promote_memory refuses confidence < 0.5 without force — so the
inflated 0.5 would silently pass the guard for a memory that was recorded as
zero-confidence.

Counterfeits this test rejects:
- Keep ``or 0.5`` but only special-case the test's statement text.
- Change the *default* for missing rows to 0.0 while still collapsing real 0.0
  via ``or`` (e.g. ``float(row.get("confidence", 0.5) or 0.5)``).
- Clamp zeros up to 0.05 / 0.5 on write without fixing the read path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omniagentos.metacog.config import clear_metacog_config_cache
from omniagentos.metacog.contracts import MemoryRecord
from omniagentos.metacog.service import MetacogService
from omniagentos.metacog.store import MetacogStore


@pytest.fixture()
def svc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MetacogService:
    monkeypatch.setenv("OMNIAGENTOS_METACOG_ARTIFACTS_ROOT", str(tmp_path / "arts"))
    monkeypatch.delenv("OMNIAGENTOS_METACOG_MODE", raising=False)
    monkeypatch.delenv("OMNIAGENTOS_METACOG_MEMORY_PROMOTION", raising=False)
    clear_metacog_config_cache()
    store = MetacogStore(str(tmp_path / "metacog.db"))
    return MetacogService(store=store)


def test_zero_confidence_survives_round_trip(svc: MetacogService) -> None:
    """A memory stored at confidence 0.0 must read back as 0.0, not 0.5."""
    art = svc.register_artifact(
        artifact_type="test_log",
        content="zero-conf-evidence",
        task_id="task_zero_conf",
    )
    mem = MemoryRecord(
        type="warning",
        statement="this lesson has zero measured confidence",
        evidence=[art.id],
        confidence=0.0,
        sample_count=3,
        success_count=0,
        failure_count=3,
        promotion_status="pending",
        company_id="default",
    )
    stored = svc.store.upsert_memory(mem)
    assert stored.confidence == 0.0, "write path must accept confidence=0.0"

    loaded = svc.store.get_memory(stored.id)
    assert loaded is not None
    # THE REQUIREMENT: zero is a measured value, not a missing field.
    assert loaded.confidence == 0.0, (
        f"zero confidence must not be inflated on read; got {loaded.confidence!r}"
    )


def test_zero_confidence_blocks_promote_without_force(svc: MetacogService) -> None:
    """Promote guard must see real 0.0 and refuse (not treat it as 0.5)."""
    art = svc.register_artifact(
        artifact_type="test_log",
        content="zero-conf-promote",
        task_id="task_zero_promote",
    )
    mem = MemoryRecord(
        type="lesson",
        statement="zero confidence must not promote",
        evidence=[art.id],
        confidence=0.0,
        sample_count=1,
        promotion_status="pending",
    )
    stored = svc.store.upsert_memory(mem)

    with pytest.raises(ValueError, match="low-confidence"):
        svc.promote_memory(stored.id)

    # Force still works — the gate is about silent promotion of unknown/zero.
    forced = svc.promote_memory(stored.id, force=True)
    assert forced.promotion_status == "promoted"
    # After force, confidence must still reflect the recorded zero unless
    # promote itself rewrites it (it currently does not).
    reloaded = svc.store.get_memory(stored.id)
    assert reloaded is not None
    assert reloaded.confidence == 0.0


def test_missing_confidence_still_defaults_on_raw_row(svc: MetacogService) -> None:
    """Absent confidence on a row may default; that is not the zero bug.

    Counterfeit guard: a fix that always returns 0.0 for any absent field
    would incorrectly treat 'unknown' as 'measured zero'. Defaulting None /
    missing to the schema default (0.5) remains acceptable; only *stored*
    0.0 must survive.
    """
    # Synthetic row (column NOT NULL in SQLite, so probe the mapper directly).
    row = {
        "id": "mem_synthetic_null_conf",
        "type": "lesson",
        "statement": "row missing confidence key",
        "applicability_json": "{}",
        "evidence_json": "[]",
        # confidence key deliberately omitted
        "sample_count": 0,
        "success_count": 0,
        "failure_count": 0,
        "promotion_status": "pending",
        "valid_from": None,
        "expires_at": None,
        "invalidated_by_json": "{}",
        "supersedes_json": "[]",
        "owner": "memory_curator",
        "company_id": "default",
        "project_id": None,
        "domain": None,
        "helpfulness_score": 0.0,
        "created_at": "2026-07-29T00:00:00Z",
        "updated_at": "2026-07-29T00:00:00Z",
    }
    loaded = svc.store._memory_from_row(row)  # noqa: SLF001
    assert loaded.confidence == 0.5

    row_none = dict(row)
    row_none["confidence"] = None
    loaded_none = svc.store._memory_from_row(row_none)  # noqa: SLF001
    assert loaded_none.confidence == 0.5

    # Explicit zero must still not collapse (mapper-level twin of the round-trip).
    row_zero = dict(row)
    row_zero["confidence"] = 0.0
    loaded_zero = svc.store._memory_from_row(row_zero)  # noqa: SLF001
    assert loaded_zero.confidence == 0.0
