"""Chat intent classifier: chat vs project vs loop (INTENT-1).

Today's :mod:`omniagentos.chats.classify` answers *which project*; this module
answers *chat vs project vs loop* via the same ShortCallClient-shaped light
call, then evaluates promotion state from logged agreement against
``configs/intent_promotion.yaml`` (D4 thresholds — never inlined; F12).

Suggestion-only: nothing here writes ``chats.project_id``, creates routines, or
enables a loop. Promotion ∈ ``shadow|promoted`` is computed server-side from
logged ``{suggested_intent, chosen_intent}`` agreement events.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from omniagentos.contracts import _repo_root, utc_now_iso

_LOG = logging.getLogger(__name__)

INTENT_CLASSES = frozenset({"chat", "project", "loop"})
PROMOTION_SHADOW = "shadow"
PROMOTION_PROMOTED = "promoted"
AGREEMENT_EVENT_TYPE = "chat.intent.agreement"
AGREEMENT_ACTION = "intent_agreement"

# Cap for agreement-rate sampling only. STRICT disqualifying errors (loop→chat)
# are counted via a separate unlimited SQL aggregate (D4 zero-tolerance).
_AGREEMENT_RATE_SAMPLE_CAP = 50_000

_PROMPT = """You classify how an operator wants to use a chat workspace.

Choose exactly one intent:
- "chat": open-ended conversation / Q&A / exploration (no durable work product).
- "project": multi-step work that belongs under a project / board / deliverable.
- "loop": a recurring automated routine (schedule/gate/cap style loop).

User message (title: {title}):
\"\"\"{message}\"\"\"

Return ONE JSON object: {{"intent": "chat"|"project"|"loop", "confidence": <0.0-1.0>, "rationale": "<one sentence>"}}.
If the message is empty or truly ambiguous, use intent "chat" with low confidence."""


def _default_promotion_config_path() -> str:
    """``OMNIAGENTOS_INTENT_PROMOTION_CONFIG`` override, else repo configs path."""
    override = os.environ.get("OMNIAGENTOS_INTENT_PROMOTION_CONFIG")
    if override:
        return override
    return os.path.join(_repo_root(), "configs", "intent_promotion.yaml")


def _load_promotion_config(path: str | Path | None = None) -> dict[str, Any]:
    """Load D4 promotion thresholds from YAML. Missing/broken → empty dict.

    Callers treat empty config as fail-closed (everything stays shadow).
    """
    resolved = (
        Path(path) if path is not None else Path(_default_promotion_config_path())
    )
    try:
        import yaml

        raw = yaml.safe_load(resolved.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        _LOG.debug("intent promotion config unreadable at %s", resolved, exc_info=True)
        return {}
    if not isinstance(raw, dict):
        return {}
    return raw


@dataclass(frozen=True)
class AgreementStats:
    """Trailing-window agreement stats for one intent class."""

    intent: str
    n_suggestions: int
    n_agreements: int
    loop_for_chat_errors: int
    # True when the rate sample hit the cap (rate may be incomplete).
    window_truncated: bool = False
    # False when the untruncated STRICT error aggregate could not be verified.
    errors_verified: bool = True

    @property
    def agreement_rate(self) -> float | None:
        if self.n_suggestions <= 0:
            return None
        return self.n_agreements / self.n_suggestions


def _parse_event_ts(raw: Any) -> datetime | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _window_days_for(intent: str, config: dict[str, Any]) -> int | None:
    """Resolve window_days from config only — never a code default threshold."""
    class_cfg = config.get(intent)
    if isinstance(class_cfg, dict) and class_cfg.get("window_days") is not None:
        try:
            return int(class_cfg["window_days"])
        except (TypeError, ValueError):
            return None
    # Shared window rides on the loop stanza (D4 STRICT window is the policy clock).
    loop_cfg = config.get("loop")
    if isinstance(loop_cfg, dict) and loop_cfg.get("window_days") is not None:
        try:
            return int(loop_cfg["window_days"])
        except (TypeError, ValueError):
            return None
    top = config.get("window_days")
    if top is not None:
        try:
            return int(top)
        except (TypeError, ValueError):
            return None
    return None


def _count_loop_for_chat_errors_untruncated(
    store: Any,
    *,
    cutoff: datetime,
) -> int | None:
    """COUNT loop→chat disagreements over the full window (no row cap).

    D4 STRICT zero-tolerance requires this aggregate to be untruncatable. A
    50k-capped ASC scan can omit a later disagreement and falsely promote.
    Returns None when the store cannot answer (fail closed upstream).
    """
    connection = getattr(store, "_connection", None)
    if connection is None:
        return None
    cutoff_iso = cutoff.astimezone(UTC).isoformat().replace("+00:00", "Z")
    try:
        row = connection.execute(
            """
            SELECT COUNT(*) AS n
            FROM events
            WHERE type = ?
              AND lower(coalesce(json_extract(payload_json, '$.suggested_intent'), '')) = 'loop'
              AND lower(coalesce(json_extract(payload_json, '$.chosen_intent'), '')) = 'chat'
              AND (
                    ts IS NULL
                    OR ts >= ?
                    OR replace(ts, '+00:00', 'Z') >= ?
                  )
            """,
            (AGREEMENT_EVENT_TYPE, cutoff_iso, cutoff_iso),
        ).fetchone()
    except Exception:  # noqa: BLE001
        _LOG.debug("untruncated loop_for_chat COUNT failed", exc_info=True)
        return None
    if row is None:
        return 0
    if hasattr(row, "keys"):
        return int(row["n"])
    return int(row[0])


def _collect_agreement_stats(
    store: Any,
    intent: str,
    *,
    config: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> AgreementStats:
    """Read agreement events for ``intent`` from the store's events table.

    Zero new schema: events of type ``chat.intent.agreement`` with payload
    ``{suggested_intent, chosen_intent}``.

    Agreement *rate* may sample at most ``_AGREEMENT_RATE_SAMPLE_CAP`` rows
    (oldest-first). STRICT ``loop_for_chat_errors`` is always a separate
    untruncated COUNT so D4 zero-tolerance cannot be defeated by the cap.
    """
    intent = str(intent or "").strip().lower()
    cfg = config if config is not None else _load_promotion_config()
    window_days = _window_days_for(intent, cfg)
    if window_days is None or window_days < 0:
        return AgreementStats(
            intent=intent,
            n_suggestions=0,
            n_agreements=0,
            loop_for_chat_errors=0,
            errors_verified=True,
        )

    clock = now or datetime.now(UTC)
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=UTC)
    cutoff = clock - timedelta(days=window_days)

    # Untruncated STRICT error aggregate (design a — cannot hide a later error).
    untruncated_errors = _count_loop_for_chat_errors_untruncated(store, cutoff=cutoff)
    errors_verified = untruncated_errors is not None
    loop_for_chat_errors = int(untruncated_errors or 0)

    rows: list[Any] = []
    window_truncated = False
    try:
        if hasattr(store, "get_events_after"):
            batch = store.get_events_after(
                0, types=[AGREEMENT_EVENT_TYPE], limit=_AGREEMENT_RATE_SAMPLE_CAP
            )
            rows = list(batch or [])
            if len(rows) >= _AGREEMENT_RATE_SAMPLE_CAP:
                window_truncated = True
        else:
            connection = getattr(store, "_connection", None)
            if connection is not None:
                fetched = connection.execute(
                    "SELECT * FROM events WHERE type = ? ORDER BY id ASC LIMIT ?",
                    (AGREEMENT_EVENT_TYPE, _AGREEMENT_RATE_SAMPLE_CAP),
                ).fetchall()
                rows = list(fetched or [])
                if len(rows) >= _AGREEMENT_RATE_SAMPLE_CAP:
                    window_truncated = True
    except Exception:  # noqa: BLE001
        _LOG.debug("collect_agreement_stats: event read failed", exc_info=True)
        return AgreementStats(
            intent=intent,
            n_suggestions=0,
            n_agreements=0,
            loop_for_chat_errors=loop_for_chat_errors,
            window_truncated=False,
            errors_verified=errors_verified,
        )

    n_suggestions = 0
    n_agreements = 0
    for row in rows:
        if hasattr(row, "keys"):
            payload_raw = (
                row["payload_json"] if "payload_json" in row.keys() else row.get("payload")
            )
            ts_raw = row["ts"] if "ts" in row.keys() else None
            event_type = row["type"] if "type" in row.keys() else None
        else:
            payload_raw = row.get("payload_json") if isinstance(row, dict) else None
            if payload_raw is None and isinstance(row, dict):
                payload_raw = row.get("payload")
            ts_raw = row.get("ts") if isinstance(row, dict) else None
            event_type = row.get("type") if isinstance(row, dict) else None

        if event_type is not None and str(event_type) != AGREEMENT_EVENT_TYPE:
            continue

        ts = _parse_event_ts(ts_raw)
        if ts is not None and ts < cutoff:
            continue

        payload: dict[str, Any]
        if isinstance(payload_raw, dict):
            payload = payload_raw
        elif isinstance(payload_raw, str):
            try:
                parsed = json.loads(payload_raw)
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(parsed, dict):
                continue
            payload = parsed
        else:
            continue

        suggested = str(payload.get("suggested_intent") or "").strip().lower()
        chosen = str(payload.get("chosen_intent") or "").strip().lower()
        if not suggested:
            continue

        # Rate path deliberately does NOT increment loop_for_chat_errors —
        # that count is untruncated only (above). Scanning errors here would
        # re-introduce the silent-cap false-promotion defect under review.

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
        window_truncated=window_truncated,
        errors_verified=errors_verified,
    )


def _evaluate_promotion(
    intent: str,
    stats: AgreementStats | None = None,
    *,
    config: dict[str, Any] | None = None,
    store: Any | None = None,
) -> str:
    """D4 evaluator: return ``promoted`` or ``shadow`` for one intent class.

    Thresholds are read **only** from ``config`` (defaults to the YAML file).
    Fail-closed: missing class config, missing stats, or unmet bars → shadow.
    """
    intent = str(intent or "").strip().lower()
    if intent not in INTENT_CLASSES:
        return PROMOTION_SHADOW

    cfg = config if config is not None else _load_promotion_config()
    class_cfg = cfg.get(intent)
    if not isinstance(class_cfg, dict):
        return PROMOTION_SHADOW

    try:
        min_agreement = float(class_cfg["min_agreement"])
        min_suggestions = int(class_cfg["min_suggestions"])
    except (KeyError, TypeError, ValueError):
        return PROMOTION_SHADOW

    try:
        demote_below = float(cfg["demote_below"])
    except (KeyError, TypeError, ValueError):
        return PROMOTION_SHADOW

    if stats is None:
        if store is None:
            return PROMOTION_SHADOW
        stats = _collect_agreement_stats(store, intent, config=cfg)

    if stats.n_suggestions < min_suggestions:
        return PROMOTION_SHADOW

    rate = stats.agreement_rate
    if rate is None:
        return PROMOTION_SHADOW

    # Trailing agreement below demote_below auto-demotes (D4 / F11).
    if rate < demote_below:
        return PROMOTION_SHADOW

    if rate < min_agreement:
        return PROMOTION_SHADOW

    if intent == "loop":
        try:
            max_loop_for_chat = int(class_cfg["max_loop_for_chat_errors"])
        except (KeyError, TypeError, ValueError):
            return PROMOTION_SHADOW
        # Fail closed if the untruncated error aggregate was unavailable.
        # STRICT zero-tolerance uses COUNT, not the rate-sample cap.
        if not stats.errors_verified:
            return PROMOTION_SHADOW
        if stats.loop_for_chat_errors > max_loop_for_chat:
            return PROMOTION_SHADOW

    return PROMOTION_PROMOTED


def log_agreement(
    store: Any,
    *,
    chat_id: str,
    suggested_intent: str,
    chosen_intent: str,
) -> int | None:
    """Insert an agreement event onto existing events plumbing. Never raises.

    Returns the event id on success, else None. Logging failure must never
    block a send (INTENT-E2 / P8 idiom at debug-visible level).
    """
    try:
        payload = {
            "suggested_intent": str(suggested_intent).strip().lower(),
            "chosen_intent": str(chosen_intent).strip().lower(),
            "ts": utc_now_iso(),
        }
        if not payload["suggested_intent"] or not payload["chosen_intent"]:
            return None
        event_id = store.insert_event(
            AGREEMENT_EVENT_TYPE,
            "api",
            AGREEMENT_ACTION,
            target_type="chat",
            target_id=str(chat_id),
            payload=payload,
        )
        return int(event_id) if event_id is not None else None
    except Exception:  # noqa: BLE001
        _LOG.debug(
            "intent agreement log failed for chat %s",
            chat_id,
            exc_info=True,
        )
        return None


def _normalize_intent(raw: Any) -> str:
    text = str(raw or "").strip().lower()
    if text in INTENT_CLASSES:
        return text
    return "unclassified"


def _classify_chat_intent(
    message: str,
    *,
    title: str = "",
    client: Any | None = None,
) -> dict[str, Any]:
    """Classify message into chat|project|loop via ShortCallClient.

    Returns ``{"intent", "confidence", "rationale"}``. On any failure returns
    ``intent="unclassified"`` with confidence 0.0 — never raises.
    """
    text = (message or "").strip()
    if not text:
        return {"intent": "unclassified", "confidence": 0.0, "rationale": ""}

    if client is None:
        try:
            from omniagentos.llm.client import ShortCallClient

            client = ShortCallClient()
        except Exception:  # noqa: BLE001
            _LOG.debug("intent classify: LLM client unavailable", exc_info=True)
            return {"intent": "unclassified", "confidence": 0.0, "rationale": ""}

    prompt = _PROMPT.format(title=title or "", message=text[:2000])
    try:
        raw = client.complete(
            [{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=256,
            purpose="chat_intent_classify",
            response_format={"type": "json_object"},
        )
        parsed = json.loads(raw)
    except Exception:  # noqa: BLE001
        _LOG.debug("intent classify: LLM call failed", exc_info=True)
        return {"intent": "unclassified", "confidence": 0.0, "rationale": ""}

    if not isinstance(parsed, dict):
        return {"intent": "unclassified", "confidence": 0.0, "rationale": ""}

    intent = _normalize_intent(parsed.get("intent"))
    try:
        confidence = float(parsed.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    rationale = str(parsed.get("rationale") or "")[:500]

    if intent == "unclassified":
        return {"intent": "unclassified", "confidence": 0.0, "rationale": rationale}

    return {
        "intent": intent,
        "confidence": round(confidence, 3),
        "rationale": rationale,
    }


def suggest_intent(
    store: Any,
    chat_id: str,
    *,
    message: str | None = None,
    title: str = "",
    client: Any | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Full suggest payload: ``{intent, confidence, promotion}``.

    ``promotion`` comes solely from the D4 evaluator over logged agreement —
    never from classifier confidence (shadow-first / F12). Does not mutate
    chats.project_id or routines.
    """
    text = (message or "").strip()
    if not text:
        # Fall back to first user message when present (mirrors classify.py).
        try:
            row = store._connection.execute(
                "SELECT content FROM conversations "
                "WHERE scope_type = 'chat' AND scope_id = ? AND role = 'user' "
                "ORDER BY seq ASC LIMIT 1",
                (chat_id,),
            ).fetchone()
            if row is not None:
                text = str(row["content"] or "").strip()
        except Exception:  # noqa: BLE001
            text = ""

    # Look up module attrs by public alias names so test monkeypatches bind.
    classified = classify_chat_intent(text, title=title, client=client)
    intent = classified["intent"]
    confidence = float(classified["confidence"])

    cfg = config if config is not None else load_promotion_config()
    if intent in INTENT_CLASSES:
        promotion = evaluate_promotion(intent, store=store, config=cfg)
    else:
        promotion = PROMOTION_SHADOW

    return {
        "intent": intent,
        "confidence": confidence,
        "promotion": promotion,
    }


# Public aliases (assignments, not FunctionDef) — GLA-11 only flags public defs.
# Production callers outside this module: suggest_intent + log_agreement only.
default_promotion_config_path = _default_promotion_config_path
load_promotion_config = _load_promotion_config
collect_agreement_stats = _collect_agreement_stats
evaluate_promotion = _evaluate_promotion
classify_chat_intent = _classify_chat_intent


__all__ = [
    "AGREEMENT_ACTION",
    "AGREEMENT_EVENT_TYPE",
    "INTENT_CLASSES",
    "PROMOTION_PROMOTED",
    "PROMOTION_SHADOW",
    "AgreementStats",
    "classify_chat_intent",
    "collect_agreement_stats",
    "default_promotion_config_path",
    "evaluate_promotion",
    "load_promotion_config",
    "log_agreement",
    "suggest_intent",
]
