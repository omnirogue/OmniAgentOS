"""MEM-I capacity-curve carrier (deterministic half, DESIGN-v2.md §5).

Certifies how retrieval sufficiency behaves as the store grows (scales S/M/L =
14/28/56 sessions). Floors ratcheted 2026-08-13 to the sentence-grain curve
(seed 42, budget 12000):

    system (hybrid):  S 1.0     M 0.8704  L 0.9074   (pooled sufficiency)
    system_legacy:    S 0.5741  M 0.2593  L 0.0926

(The first turn-grain measurement collapsed to M 0.48 / L 0.56 — whole-turn
rendering burned the history reserve on filler; sentence granularity fixed it
and the floors moved up with the evidence.) The certified properties are
RELATIVE and floored under the observation so fixture drift on a rotation
cannot false-red while a real capacity collapse (retrieval leg dead at scale)
does.
"""

from __future__ import annotations

import pytest

from scripts.memcert.capacity import capacity_curve

SEEDS = [42]
SCALES = ("S", "M", "L")


@pytest.fixture(scope="module")
def curve() -> dict:
    return capacity_curve(SEEDS, ["system_legacy", "system"], scales=SCALES)


def _pooled(curve: dict, arm: str, scale: str) -> float:
    return curve["arms"][arm][scale]["pooled_sufficiency"]


def _axis(curve: dict, arm: str, scale: str, axis: str) -> float:
    return curve["arms"][arm][scale]["summary"][axis]["sufficiency"]


def test_hybrid_beats_legacy_at_every_scale(curve: dict) -> None:
    for scale in SCALES:
        assert _pooled(curve, "system", scale) > _pooled(curve, "system_legacy", scale), (
            f"hybrid lost its capacity edge at scale {scale}"
        )


def test_hybrid_capacity_floors(curve: dict) -> None:
    # Ratcheted 2026-08-13 after sentence-grain retrieval: measured S 1.0 /
    # M 0.87 / L 0.91 (turn-grain had collapsed to 0.48/0.56 at M/L — whole
    # turns were ~10x the render cost of the evidence sentence inside them).
    assert _pooled(curve, "system", "S") >= 0.95
    assert _pooled(curve, "system", "M") >= 0.75
    assert _pooled(curve, "system", "L") >= 0.80


def test_hybrid_keeps_multiples_of_legacy_at_large_scale(curve: dict) -> None:
    # v1 collapses at L (measured 0.0926); the hybrid retrieval leg is what
    # keeps memory usable at 4x corpus. 3x is the alarm line under the
    # measured 6x.
    legacy_l = _pooled(curve, "system_legacy", "L")
    assert _pooled(curve, "system", "L") >= 3.0 * max(legacy_l, 0.01)


def test_lesson_retrievability_is_scale_robust(curve: dict) -> None:
    # G measured 1.0 at every scale: relevance retrieval does not care how far
    # the lesson has scrolled into the past. This is THE property the estate's
    # MEMORY.md ritual depends on.
    for scale in SCALES:
        assert _axis(curve, "system", scale, "G") >= 0.8, f"lessons fell off at {scale}"


def test_update_spine_never_collapses(curve: dict) -> None:
    # D (knowledge updates) may degrade under the fixed 1200-token assemble
    # budget at 4x corpus (measured 0.625 at M) but must never collapse below
    # a half — that would mean the recency spine itself broke.
    for scale in SCALES:
        assert _axis(curve, "system", scale, "D") >= 0.5, f"D spine collapsed at {scale}"
