"""Phase 1 (A2): content-aware dispatch fast-lane rules.

Covers OMNIAGENTOS_CONTENT_DISPATCH_MODE (off default / shadow / enforce):

* off is byte-for-byte legacy classify_task_speed behavior — durations like
  "30 second" still trip the batch numeric heuristic; no content band rule.
* shadow computes and LOGS the content-aware decision (speed/band/batch) but
  returns the legacy speed unchanged.
* enforce applies it: durations/dimensions do not batch-route; multi-phase or
  substantial creative deliverables (e.g. webinars) are never banded
  solo_fast/simple.

Also asserts configs/dispatch.yaml seeds a ``content_creation`` route under
BOTH ``routes`` and ``category_routes``.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
import yaml

import omniagentos.api.main  # noqa: F401  -- settle intake<->api import cycle
from omniagentos.intake.fastlane import (
    CONTENT_DISPATCH_ENV,
    assess_content_dispatch,
    classify_task_speed,
    content_blocks_solo_fast,
    content_complexity_band,
    content_dispatch_mode,
)

# Regression utterance from the Lane A Phase 1 brief.
_WEBINAR_UTTERANCE = "30 second webinar for globex"

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DISPATCH_YAML = _REPO_ROOT / "configs" / "dispatch.yaml"


# ---------------------------------------------------------------------------
# content_dispatch_mode() — tri-state gate, DEFAULT off
# ---------------------------------------------------------------------------


def test_content_dispatch_mode_defaults_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(CONTENT_DISPATCH_ENV, raising=False)
    assert content_dispatch_mode() == "off"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("off", "off"),
        ("shadow", "shadow"),
        ("ENFORCE", "enforce"),
        ("enforce", "enforce"),
        ("bogus", "off"),
        ("", "off"),
        ("1", "off"),
        ("true", "off"),
    ],
)
def test_content_dispatch_mode_parses_tristate(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: str
) -> None:
    monkeypatch.setenv(CONTENT_DISPATCH_ENV, raw)
    assert content_dispatch_mode() == expected


# ---------------------------------------------------------------------------
# configs/dispatch.yaml — content_creation in routes AND category_routes
# ---------------------------------------------------------------------------


def test_dispatch_yaml_has_content_creation_in_routes_and_category_routes() -> None:
    raw = yaml.safe_load(_DISPATCH_YAML.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    routes = raw.get("routes")
    category_routes = raw.get("category_routes")
    assert isinstance(routes, dict), "routes section missing"
    assert isinstance(category_routes, dict), "category_routes section missing"
    assert "content_creation" in routes, "content_creation missing from routes"
    assert "content_creation" in category_routes, (
        "content_creation missing from category_routes"
    )
    route_utts = routes["content_creation"]
    cat_utts = category_routes["content_creation"]
    assert isinstance(route_utts, list) and len(route_utts) >= 8
    assert isinstance(cat_utts, list) and len(cat_utts) >= 8
    # Utterances should span webinar / multi-phase / dimension shapes.
    joined = " ".join(str(u) for u in route_utts).lower()
    assert "webinar" in joined
    assert "1080" in joined or "multi-phase" in joined or "multi phase" in joined


# ---------------------------------------------------------------------------
# Default-off: legacy behavior unchanged (incl. batch on duration digits)
# ---------------------------------------------------------------------------


def test_default_off_preserves_legacy_batch_on_duration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(CONTENT_DISPATCH_ENV, raising=False)
    # Pre-A2: "30" >= 4 forces planned (batch), even though it is a duration.
    assert classify_task_speed(_WEBINAR_UTTERANCE) == "planned"
    assert classify_task_speed("create a 30 second video") == "planned"
    assert classify_task_speed("change the default timeout constant to 30 seconds") == "planned"


def test_default_off_still_batch_routes_true_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(CONTENT_DISPATCH_ENV, raising=False)
    assert classify_task_speed("make 10 creatives") == "planned"
    assert classify_task_speed("create 5 files") == "planned"


def test_default_off_simple_goals_still_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(CONTENT_DISPATCH_ENV, raising=False)
    assert classify_task_speed("write a haiku about the ocean") == "fast"
    assert classify_task_speed("make a folder on my desktop called tiger") == "fast"


# ---------------------------------------------------------------------------
# Enforce: numeric heuristic + band rule
# ---------------------------------------------------------------------------


def test_enforce_duration_does_not_batch_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(CONTENT_DISPATCH_ENV, "enforce")
    assessment = assess_content_dispatch(_WEBINAR_UTTERANCE)
    assert assessment.batch_routed is False
    # Duration-only coding edit: no longer planned solely because of "30".
    assert classify_task_speed("change the default timeout constant to 30 seconds") == "fast"


def test_enforce_dimension_does_not_batch_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(CONTENT_DISPATCH_ENV, "enforce")
    # Bare 1080 is a media dimension, not "make 1080 files".
    assessment = assess_content_dispatch("export the asset at 1080")
    assert assessment.batch_routed is False
    assessment_pair = assess_content_dispatch("create a banner at 1080x1080")
    assert assessment_pair.batch_routed is False


def test_enforce_true_batch_counts_still_planned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(CONTENT_DISPATCH_ENV, "enforce")
    assert classify_task_speed("make 10 creatives") == "planned"
    assert classify_task_speed("create 5 files") == "planned"
    assessment = assess_content_dispatch("make 10 creatives")
    assert assessment.batch_routed is True


def test_enforce_band_rule_blocks_solo_fast_for_multi_phase_creative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(CONTENT_DISPATCH_ENV, "enforce")
    goal = "build a multi-phase video ad from storyboard to final render"
    assert content_blocks_solo_fast(goal) is True
    band = content_complexity_band(goal)
    assert band not in ("simple", "solo_fast")
    assert band in ("standard", "complex")
    assert classify_task_speed(goal) == "planned"


def test_enforce_band_rule_blocks_solo_fast_for_30_minute_creative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(CONTENT_DISPATCH_ENV, "enforce")
    goal = "produce a 30-minute training video with multi-phase storyboard"
    assert content_blocks_solo_fast(goal) is True
    assert content_complexity_band(goal) not in ("simple", "solo_fast")
    assert classify_task_speed(goal) == "planned"


def test_regression_30_second_webinar_for_globex_under_enforce(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replay: '30 second webinar for globex' under enforce.

    Must NOT be batch-routed (30 is a duration) and must NOT be banded
    solo_fast/simple (content band rule for webinar deliverables).
    """
    monkeypatch.setenv(CONTENT_DISPATCH_ENV, "enforce")
    assessment = assess_content_dispatch(_WEBINAR_UTTERANCE)

    assert assessment.batch_routed is False, "duration must not trigger batch routing"
    assert assessment.blocks_solo_fast is True
    assert assessment.band not in ("simple", "solo_fast")
    assert assessment.band in ("standard", "complex")
    assert assessment.is_content_creation is True
    assert assessment.speed == "planned"
    assert classify_task_speed(_WEBINAR_UTTERANCE) == "planned"
    # Band helper mirrors the assessment.
    assert content_complexity_band(_WEBINAR_UTTERANCE) not in ("simple", "solo_fast")
    assert content_blocks_solo_fast(_WEBINAR_UTTERANCE) is True


# ---------------------------------------------------------------------------
# Shadow: logs would-be decision, does not apply it
# ---------------------------------------------------------------------------


def test_shadow_only_logs_does_not_apply(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv(CONTENT_DISPATCH_ENV, "shadow")
    # Legacy result for the webinar is planned (batch on 30).
    # Content-aware is also planned (band rule) — pick a case where they DIFF
    # so we can prove shadow returns legacy: timeout "30 seconds" is planned
    # under legacy batch, but fast under content-aware numeric exemption.
    goal = "change the default timeout constant to 30 seconds"
    legacy = "planned"  # digit 30 >= 4
    with caplog.at_level(logging.INFO, logger="omniagentos.intake.fastlane"):
        result = classify_task_speed(goal)
    assert result == legacy, "shadow must return legacy speed, not content-aware"
    assert any(
        "content_dispatch shadow" in rec.getMessage() for rec in caplog.records
    ), "shadow mode must log the would-be content-aware decision"


def test_shadow_webinar_returns_legacy_and_logs(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv(CONTENT_DISPATCH_ENV, "shadow")
    with caplog.at_level(logging.INFO, logger="omniagentos.intake.fastlane"):
        result = classify_task_speed(_WEBINAR_UTTERANCE)
    # Legacy: planned via batch digit. Content-aware: planned via band rule.
    # Either way speed matches; shadow must still log when band blocks solo_fast.
    assert result == "planned"
    assert any("content_dispatch shadow" in rec.getMessage() for rec in caplog.records)


def test_off_does_not_log_content_dispatch(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv(CONTENT_DISPATCH_ENV, raising=False)
    with caplog.at_level(logging.INFO, logger="omniagentos.intake.fastlane"):
        classify_task_speed(_WEBINAR_UTTERANCE)
    assert not any("content_dispatch shadow" in rec.getMessage() for rec in caplog.records)


# ---------------------------------------------------------------------------
# assess_content_dispatch is pure w.r.t. apply semantics
# ---------------------------------------------------------------------------


def test_assess_is_available_under_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(CONTENT_DISPATCH_ENV, raising=False)
    assessment = assess_content_dispatch(_WEBINAR_UTTERANCE)
    assert assessment.mode == "off"
    # Assessment always reports content-aware view even when mode is off.
    assert assessment.batch_routed is False
    assert assessment.blocks_solo_fast is True
    # Applied classify still uses legacy under off.
    assert classify_task_speed(_WEBINAR_UTTERANCE) == "planned"
