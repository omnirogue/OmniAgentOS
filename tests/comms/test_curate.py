"""Selection-matrix tests for omniagentos.comms.curate.select_for_extraction."""

from __future__ import annotations

from omniagentos.comms.curate import select_for_extraction
from omniagentos.steward.config import AlertsConfig, CurationConfig, StewardConfig


def _cfg(
    *, extract_vip: bool = True, extract_goal_keywords: bool = True, vip: list[str] | None = None
) -> StewardConfig:
    cfg = StewardConfig()
    cfg.alerts = AlertsConfig(vip_senders=vip or [])
    cfg.curation = CurationConfig(
        extract_vip=extract_vip, extract_goal_keywords=extract_goal_keywords
    )
    return cfg


def _row(**overrides: object) -> dict[str, object]:
    row = {
        "id": 1,
        "sender": "someone@example.com",
        "subject": "hello",
        "body_text": "just checking in",
        "kb_status": "none",
    }
    row.update(overrides)
    return row


def test_operator_flag_always_wins() -> None:
    cfg = _cfg(extract_vip=False, extract_goal_keywords=False)
    selected, reason, discipline = select_for_extraction(_row(kb_status="selected"), cfg, [])
    assert (selected, discipline) == (True, None)
    assert "operator" in reason


def test_vip_sender_case_insensitive_substring_match() -> None:
    cfg = _cfg(vip=["Boss@Example.com"])
    selected, reason, discipline = select_for_extraction(
        _row(sender="assistant-to-boss@example.com"), cfg, []
    )
    assert selected is True
    assert discipline is None
    assert "vip" in reason.lower()


def test_vip_disabled_by_config_even_on_match() -> None:
    cfg = _cfg(extract_vip=False, vip=["boss@example.com"])
    selected, reason, _ = select_for_extraction(_row(sender="boss@example.com"), cfg, [])
    assert selected is False
    assert reason == "not selected"


def test_goal_keyword_match_in_subject_sets_discipline() -> None:
    cfg = _cfg(vip=[])
    goals = [{"keywords": ["ROAS"], "discipline_id": "ads"}]
    selected, reason, discipline = select_for_extraction(
        _row(subject="Our ROAS dropped this week"), cfg, goals
    )
    assert selected is True
    assert discipline == "ads"
    assert "goal keyword" in reason


def test_goal_keyword_match_in_body_is_case_insensitive() -> None:
    cfg = _cfg(vip=[])
    goals = [{"keywords": ["refund policy"], "discipline_id": "support"}]
    selected, _reason, discipline = select_for_extraction(
        _row(body_text="Can you clarify your REFUND POLICY?"), cfg, goals
    )
    assert selected is True
    assert discipline == "support"


def test_goal_keywords_disabled_by_config() -> None:
    cfg = _cfg(extract_goal_keywords=False, vip=[])
    goals = [{"keywords": ["roas"], "discipline_id": "ads"}]
    selected, reason, _ = select_for_extraction(_row(subject="roas talk"), cfg, goals)
    assert selected is False
    assert reason == "not selected"


def test_no_match_returns_not_selected() -> None:
    cfg = _cfg(vip=["someone-else@example.com"])
    goals = [{"keywords": ["unrelated-term"], "discipline_id": "ads"}]
    result = select_for_extraction(_row(), cfg, goals)
    assert result == (False, "not selected", None)


def test_empty_vip_token_never_matches_everything() -> None:
    cfg = _cfg(vip=["", "   "])
    selected, _reason, _discipline = select_for_extraction(
        _row(sender="anyone@example.com"), cfg, []
    )
    assert selected is False
