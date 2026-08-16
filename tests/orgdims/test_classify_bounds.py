"""Bounds for deterministic org-dimension classification."""

from __future__ import annotations

from typing import Any, cast

from omniagentos.orgdims.classify import ClassificationService
from omniagentos.orgdims.store import OrgDimsStore


class _Store:
    def get_company_by_slug(self, _: str) -> None:
        return None

    def get_product_by_slug(self, _: str, __: str) -> None:
        return None

    def get_board_org(self, _: str) -> None:
        return None


def test_classification_caps_text_before_vocabulary_matching(monkeypatch: Any) -> None:
    """Large caller strings must not feed unbounded text to regex matching."""
    service = ClassificationService(cast(OrgDimsStore, _Store()))
    observed: list[int] = []

    def capture_text(text: str, tokens: set[str], discipline: str | None) -> tuple[None, float, list[str]]:
        del tokens, discipline
        observed.append(len(text))
        return None, 0.0, []

    monkeypatch.setattr(service, "_match_workstream", capture_text)
    service.classify_text(
        object_type="board_task",
        object_id="btk_bound",
        title="x" * 100_000,
        apply=False,
    )

    assert observed and observed[0] <= 4096
