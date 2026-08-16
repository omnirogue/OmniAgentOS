"""Unit tests for omniagentos.chats.intent (INTENT-1 / INTENT-E1..E3)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from omniagentos.chats.intent import (
    AGREEMENT_EVENT_TYPE,
    PROMOTION_PROMOTED,
    PROMOTION_SHADOW,
    AgreementStats,
    classify_chat_intent,
    collect_agreement_stats,
    evaluate_promotion,
    load_promotion_config,
    log_agreement,
    suggest_intent,
)
from omniagentos.chats.store import ChatStore
from omniagentos.collab.store import CollabStore
from omniagentos.db.store import SqliteStore
from tests.support.db_template import make_store


class _FakeLLM:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.calls: list[Any] = []

    def complete(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
        self.calls.append({"messages": messages, "kwargs": kwargs})
        return json.dumps(self.payload)


@pytest.fixture
def store(tmp_path: Path) -> SqliteStore:
    return make_store(CollabStore, tmp_path / "intent.db")._store


@pytest.fixture
def promotion_config() -> dict[str, Any]:
    """Byte-exact D4 values from configs/intent_promotion.yaml."""
    return load_promotion_config()


def _seed_agreements(
    store: SqliteStore,
    *,
    suggested: str,
    n: int,
    n_agree: int,
    chat_id: str = "cht_seed",
) -> None:
    """Insert n agreement events; first n_agree match, rest disagree."""
    for i in range(n):
        chosen = suggested if i < n_agree else ("chat" if suggested != "chat" else "project")
        log_agreement(
            store,
            chat_id=chat_id,
            suggested_intent=suggested,
            chosen_intent=chosen,
        )


def test_config_byte_exact_d4_thresholds(promotion_config: dict[str, Any]) -> None:
    """INTENT-E3: configs/intent_promotion.yaml carries D4 values byte-exact."""
    assert promotion_config["loop"]["min_agreement"] == 0.95
    assert promotion_config["loop"]["min_suggestions"] == 200
    assert promotion_config["loop"]["window_days"] == 30
    assert promotion_config["loop"]["max_loop_for_chat_errors"] == 0
    assert promotion_config["chat"]["min_agreement"] == 0.90
    assert promotion_config["chat"]["min_suggestions"] == 100
    assert promotion_config["project"]["min_agreement"] == 0.90
    assert promotion_config["project"]["min_suggestions"] == 100
    assert promotion_config["demote_below"] == 0.80


def test_classify_chat_intent_uses_short_call_shape() -> None:
    client = _FakeLLM({"intent": "project", "confidence": 0.91, "rationale": "deliverable work"})
    result = classify_chat_intent("Ship the billing fix this week", client=client)
    assert result["intent"] == "project"
    assert result["confidence"] == 0.91
    assert client.calls
    assert client.calls[0]["kwargs"]["purpose"] == "chat_intent_classify"
    assert client.calls[0]["kwargs"]["response_format"] == {"type": "json_object"}


def test_classify_unknown_intent_becomes_unclassified() -> None:
    client = _FakeLLM({"intent": "wizard", "confidence": 0.99})
    result = classify_chat_intent("hello", client=client)
    assert result["intent"] == "unclassified"
    assert result["confidence"] == 0.0


def test_classify_llm_failure_is_unclassified() -> None:
    class _Boom:
        def complete(self, *args: Any, **kwargs: Any) -> str:
            raise RuntimeError("no llm")

    result = classify_chat_intent("hello", client=_Boom())
    assert result == {"intent": "unclassified", "confidence": 0.0, "rationale": ""}


def test_shadow_without_agreement_regardless_of_confidence(
    store: SqliteStore,
    promotion_config: dict[str, Any],
) -> None:
    """Suggest route / evaluator: promotion=shadow when agreement bar unmet."""
    client = _FakeLLM({"intent": "chat", "confidence": 0.99, "rationale": "high conf"})
    out = suggest_intent(
        store,
        "cht_missing",
        message="just chatting",
        client=client,
        config=promotion_config,
    )
    assert out["intent"] == "chat"
    assert out["confidence"] == 0.99
    assert out["promotion"] == PROMOTION_SHADOW


def test_promotion_thresholds_read_from_config(
    store: SqliteStore,
    promotion_config: dict[str, Any],
) -> None:
    """INTENT-E3: monkeypatch a config value and observe the promotion verdict change.

    Also the guard for counterfeit ``cf-promotion-threshold-inlined``: if
    thresholds are hardcoded in intent.py, lowering min_agreement via config
    will not promote.
    """
    # 100 samples, 85% agreement — below default chat min (0.90) → shadow.
    _seed_agreements(store, suggested="chat", n=100, n_agree=85)
    stats = collect_agreement_stats(store, "chat", config=promotion_config)
    assert stats.n_suggestions == 100
    assert stats.agreement_rate == pytest.approx(0.85)

    assert (
        evaluate_promotion("chat", stats, config=promotion_config) == PROMOTION_SHADOW
    )

    # Lower the bar via config only → promoted (rate 0.85 >= 0.50 and >= demote 0.80).
    patched = yaml.safe_load(yaml.safe_dump(promotion_config))
    patched["chat"]["min_agreement"] = 0.50
    assert evaluate_promotion("chat", stats, config=patched) == PROMOTION_PROMOTED


def test_demotion_below_trailing_agreement(
    store: SqliteStore,
    promotion_config: dict[str, Any],
) -> None:
    """F11 / INTENT-E3 demotion leg: trailing agreement < demote_below → shadow."""
    # First establish a promoted class (meet chat MODERATE bar).
    _seed_agreements(store, suggested="chat", n=100, n_agree=95)
    stats_good = collect_agreement_stats(store, "chat", config=promotion_config)
    assert evaluate_promotion("chat", stats_good, config=promotion_config) == PROMOTION_PROMOTED

    # Inject disagreements that pull trailing agreement under demote_below (0.80).
    # 95 agree + 40 disagree = 135 total, rate = 95/135 ≈ 0.704 < 0.80.
    _seed_agreements(
        store,
        suggested="chat",
        n=40,
        n_agree=0,
        chat_id="cht_demote",
    )
    stats_bad = collect_agreement_stats(store, "chat", config=promotion_config)
    assert stats_bad.agreement_rate is not None
    assert stats_bad.agreement_rate < float(promotion_config["demote_below"])
    assert evaluate_promotion("chat", stats_bad, config=promotion_config) == PROMOTION_SHADOW


def test_loop_strict_requires_zero_loop_for_chat_errors(
    store: SqliteStore,
    promotion_config: dict[str, Any],
) -> None:
    # 200 perfect agreements for loop — would promote under STRICT.
    _seed_agreements(store, suggested="loop", n=200, n_agree=200)
    stats = collect_agreement_stats(store, "loop", config=promotion_config)
    assert evaluate_promotion("loop", stats, config=promotion_config) == PROMOTION_PROMOTED

    # One Loop-suggested-for-Chat error blocks promotion (max_loop_for_chat_errors: 0).
    log_agreement(
        store,
        chat_id="cht_err",
        suggested_intent="loop",
        chosen_intent="chat",
    )
    stats2 = collect_agreement_stats(store, "loop", config=promotion_config)
    assert stats2.loop_for_chat_errors >= 1
    assert evaluate_promotion("loop", stats2, config=promotion_config) == PROMOTION_SHADOW


def test_suggestion_never_mutates_state(
    store: SqliteStore,
    promotion_config: dict[str, Any],
) -> None:
    """INTENT-E1: suggest never writes project_id / never creates or enables a routine."""
    chat_store = ChatStore(store)
    chat = chat_store.create_chat(title="Intent probe", project_id=None)
    chat_id = chat["id"]

    def _snapshot() -> tuple[list[Any], list[Any], str | None]:
        chats = store._connection.execute(
            "SELECT id, project_id, status, meta_json FROM chats ORDER BY id"
        ).fetchall()
        try:
            routines = store._connection.execute(
                "SELECT id, status, name FROM routines ORDER BY id"
            ).fetchall()
        except Exception:  # noqa: BLE001 — table may be empty/absent in lean fixtures
            routines = []
        row = chat_store.get_chat(chat_id)
        project_id = row.get("project_id") if row else None
        return list(chats), list(routines), project_id

    before = _snapshot()
    client = _FakeLLM({"intent": "loop", "confidence": 0.99, "rationale": "recurring"})
    out = suggest_intent(
        store,
        chat_id,
        message="run this every Monday morning",
        client=client,
        config=promotion_config,
    )
    assert out["intent"] == "loop"
    assert out["promotion"] == PROMOTION_SHADOW  # no agreement yet
    after = _snapshot()
    assert after == before


def test_log_agreement_writes_event_far_side(store: SqliteStore) -> None:
    event_id = log_agreement(
        store,
        chat_id="cht_log",
        suggested_intent="project",
        chosen_intent="chat",
    )
    assert event_id is not None
    rows = store.get_events_after(0, types=[AGREEMENT_EVENT_TYPE], limit=10)
    assert len(rows) == 1
    payload = json.loads(rows[0]["payload_json"])
    assert payload["suggested_intent"] == "project"
    assert payload["chosen_intent"] == "chat"


def test_log_agreement_failure_returns_none(store: SqliteStore, monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*args: Any, **kwargs: Any) -> int:
        raise RuntimeError("events down")

    monkeypatch.setattr(store, "insert_event", _boom)
    assert (
        log_agreement(
            store,
            chat_id="cht_x",
            suggested_intent="chat",
            chosen_intent="chat",
        )
        is None
    )


def test_evaluate_unknown_intent_is_shadow(promotion_config: dict[str, Any]) -> None:
    stats = AgreementStats(
        intent="wizard", n_suggestions=1000, n_agreements=1000, loop_for_chat_errors=0
    )
    assert evaluate_promotion("wizard", stats, config=promotion_config) == PROMOTION_SHADOW


def test_promoted_without_meeting_bar_must_fail(
    store: SqliteStore,
    promotion_config: dict[str, Any],
) -> None:
    """Counterfeit guard: promotion=promoted without logged-agreement bar must not happen."""
    # High confidence, zero agreement events → must remain shadow.
    client = _FakeLLM({"intent": "project", "confidence": 1.0, "rationale": "sure"})
    out = suggest_intent(
        store,
        "cht_cf",
        message="build the thing",
        client=client,
        config=promotion_config,
    )
    assert out["promotion"] != PROMOTION_PROMOTED
    assert out["promotion"] == PROMOTION_SHADOW


def _bulk_seed_loop_agreements(store: SqliteStore, n: int, *, chat_id: str = "cht_bulk") -> None:
    """Fast bulk insert of perfect loop agreements (bypasses per-row log_agreement)."""
    from omniagentos.chats.intent import AGREEMENT_ACTION, AGREEMENT_EVENT_TYPE
    from omniagentos.contracts import utc_now_iso

    ts = utc_now_iso()
    payload = json.dumps(
        {"suggested_intent": "loop", "chosen_intent": "loop", "ts": ts},
        separators=(",", ":"),
        sort_keys=True,
    )
    conn = store._connection
    conn.executemany(
        "INSERT INTO events "
        "(ts, type, actor, action, target_type, target_id, payload_json, trace_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (ts, AGREEMENT_EVENT_TYPE, "api", AGREEMENT_ACTION, "chat", chat_id, payload, "")
            for _ in range(n)
        ],
    )
    conn.commit()


def test_strict_loop_error_beyond_rate_cap_still_blocks_promotion(
    store: SqliteStore,
    promotion_config: dict[str, Any],
) -> None:
    """D4 zero-tolerance is unconditional: a loop→chat error AFTER the 50k rate
    sample cap must still refuse promotion (sol defect #1).

    Layout: 50_000 perfect loop agreements, then one Loop-suggested-for-Chat.
    Capped ASC scans see only the first 50k (zero errors) and would falsely
    promote; the untruncated COUNT path must see the error.
    """
    from omniagentos.chats.intent import _AGREEMENT_RATE_SAMPLE_CAP

    assert _AGREEMENT_RATE_SAMPLE_CAP == 50_000
    _bulk_seed_loop_agreements(store, _AGREEMENT_RATE_SAMPLE_CAP)
    log_agreement(
        store,
        chat_id="cht_late_err",
        suggested_intent="loop",
        chosen_intent="chat",
    )
    logged = store._connection.execute(
        "SELECT COUNT(*) AS n FROM events WHERE type = ?",
        ("chat.intent.agreement",),
    ).fetchone()["n"]
    assert logged == _AGREEMENT_RATE_SAMPLE_CAP + 1

    stats = collect_agreement_stats(store, "loop", config=promotion_config)
    # Rate sample may hit the cap, but the STRICT error aggregate must not.
    assert stats.loop_for_chat_errors >= 1, (
        f"STRICT error must be visible beyond the rate cap; got {stats!r}"
    )
    assert stats.errors_verified is True
    assert evaluate_promotion("loop", stats, config=promotion_config) == PROMOTION_SHADOW


def test_counterfeit_error_check_behind_cap_goes_red(
    store: SqliteStore,
    promotion_config: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Counterfeit: if loop_for_chat_errors is counted only inside the capped
    rate scan, the 50_001st disagreement is invisible and promotion goes green.
    This test forces that broken path and asserts the decisive property fails —
    proving the permanent 50k+1 test would go RED under that regression.
    """
    from omniagentos.chats import intent as intent_mod
    from omniagentos.chats.intent import _AGREEMENT_RATE_SAMPLE_CAP, AgreementStats

    _bulk_seed_loop_agreements(store, _AGREEMENT_RATE_SAMPLE_CAP)
    log_agreement(
        store,
        chat_id="cht_cf_cap",
        suggested_intent="loop",
        chosen_intent="chat",
    )

    def _broken_collect(
        store_arg: Any,
        intent: str,
        *,
        config: dict[str, Any] | None = None,
        now: Any = None,
    ) -> AgreementStats:
        # Recreate the REJECTED design: ASC + cap, count errors only in-sample.
        _ = config if config is not None else intent_mod.load_promotion_config()
        batch = store_arg.get_events_after(
            0, types=[intent_mod.AGREEMENT_EVENT_TYPE], limit=_AGREEMENT_RATE_SAMPLE_CAP
        )
        rows = list(batch or [])
        n_suggestions = 0
        n_agreements = 0
        loop_for_chat_errors = 0
        for row in rows:
            payload = json.loads(row["payload_json"])
            suggested = str(payload.get("suggested_intent") or "").strip().lower()
            chosen = str(payload.get("chosen_intent") or "").strip().lower()
            if suggested == "loop" and chosen == "chat":
                loop_for_chat_errors += 1
            if suggested != intent:
                continue
            n_suggestions += 1
            if suggested == chosen:
                n_agreements += 1
        return AgreementStats(
            intent=intent,
            n_suggestions=n_suggestions,
            n_agreements=n_agreements,
            loop_for_chat_errors=loop_for_chat_errors,
            window_truncated=len(rows) >= _AGREEMENT_RATE_SAMPLE_CAP,
            errors_verified=True,  # lies — errors only from capped sample
        )

    monkeypatch.setattr(intent_mod, "collect_agreement_stats", _broken_collect)
    monkeypatch.setattr(intent_mod, "_collect_agreement_stats", _broken_collect)

    broken = intent_mod.collect_agreement_stats(store, "loop", config=promotion_config)
    # Under the broken path the late error is invisible → false promotion.
    assert broken.loop_for_chat_errors == 0
    assert broken.n_suggestions >= 200
    assert (
        intent_mod.evaluate_promotion("loop", broken, config=promotion_config)
        == PROMOTION_PROMOTED
    )

    # Control: real collector still sees the error and stays shadow.
    monkeypatch.undo()
    real = collect_agreement_stats(store, "loop", config=promotion_config)
    assert real.loop_for_chat_errors >= 1
    assert evaluate_promotion("loop", real, config=promotion_config) == PROMOTION_SHADOW
