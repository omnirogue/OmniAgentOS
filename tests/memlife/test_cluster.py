"""L2 acceptance: deterministic Jaccard clustering.

Decisive: a known 6-event fixture yields exactly 2 clusters with the
expected canonical claims. Identical input → byte-identical output.

Named counterfeit: a cluster whose canonical text would be empty must be
REJECTED, never returned with ``claim=""`` (contracts already refuse empty
claims — do not invent filler text to work around that).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from omniagentos.memlife.cluster import cluster
from omniagentos.memlife.contracts import EpisodicEvent, EventResult

NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)

# ---------------------------------------------------------------------------
# 6-event fixture: two natural clusters of three events each.
# Claims are the lexicographically-first non-empty canonical text per cluster
# (reflection preferred over action).
# ---------------------------------------------------------------------------

EXPECTED_CLAIM_SANDBOX = "Agents cannot commit inside a sandboxed worktree"
EXPECTED_CLAIM_RATE_LIMIT = "Switch accounts when rate limited instead of waiting forever"


def _ev(
    event_id: str,
    *,
    skill: str,
    action: str,
    reflection: str,
    pain: float = 7.0,
    importance: float = 8.0,
    offset_hours: int = 0,
) -> EpisodicEvent:
    return EpisodicEvent(
        id=event_id,
        ts=NOW - timedelta(hours=offset_hours),
        skill=skill,
        action=action,
        result=EventResult.DENIED,
        pain=pain,
        importance=importance,
        reflection=reflection,
    )


def six_event_fixture() -> list[EpisodicEvent]:
    """Three near-duplicate sandbox-commit events + three rate-limit events."""
    return [
        _ev(
            "ev_s1",
            skill="swarm.coder",
            action="commit",
            reflection="Agents cannot commit inside a sandboxed worktree",
            offset_hours=1,
        ),
        _ev(
            "ev_s2",
            skill="swarm.coder",
            action="commit",
            reflection="Agents cannot commit inside sandboxed worktrees",
            offset_hours=2,
        ),
        _ev(
            "ev_s3",
            skill="swarm.coder",
            action="git commit",
            reflection="Cannot commit inside a sandboxed worktree",
            offset_hours=3,
        ),
        _ev(
            "ev_r1",
            skill="accounts.rotate",
            action="retry",
            reflection="Switch accounts when rate limited instead of waiting forever",
            offset_hours=4,
        ),
        _ev(
            "ev_r2",
            skill="accounts.rotate",
            action="retry",
            reflection="Switch accounts when rate limited rather than waiting",
            offset_hours=5,
        ),
        _ev(
            "ev_r3",
            skill="accounts.rotate",
            action="wait",
            reflection="When rate limited switch accounts instead of waiting forever",
            offset_hours=6,
        ),
    ]


class TestDecisiveSixEventFixture:
    def test_six_events_yield_exactly_two_clusters_with_expected_claims(self) -> None:
        candidates = cluster(six_event_fixture())
        assert len(candidates) == 2, (
            f"expected exactly 2 clusters, got {len(candidates)}: "
            f"{[c.claim for c in candidates]}"
        )
        claims = {c.claim for c in candidates}
        assert claims == {EXPECTED_CLAIM_SANDBOX, EXPECTED_CLAIM_RATE_LIMIT}, (
            f"unexpected claims: {claims}"
        )

        by_claim = {c.claim: c for c in candidates}
        sandbox = by_claim[EXPECTED_CLAIM_SANDBOX]
        rate = by_claim[EXPECTED_CLAIM_RATE_LIMIT]
        assert sandbox.cluster_size == 3
        assert rate.cluster_size == 3
        assert set(sandbox.evidence_ids) == {"ev_s1", "ev_s2", "ev_s3"}
        assert set(rate.evidence_ids) == {"ev_r1", "ev_r2", "ev_r3"}

    def test_identical_input_gives_byte_identical_output(self) -> None:
        a = [c.model_dump_json() for c in cluster(six_event_fixture())]
        b = [c.model_dump_json() for c in cluster(six_event_fixture())]
        assert a == b

    def test_input_order_does_not_affect_output(self) -> None:
        base = six_event_fixture()
        reversed_events = list(reversed(base))
        shuffled = [base[i] for i in (3, 0, 5, 1, 4, 2)]
        out_base = [c.model_dump_json() for c in cluster(base)]
        out_rev = [c.model_dump_json() for c in cluster(reversed_events)]
        out_shuf = [c.model_dump_json() for c in cluster(shuffled)]
        assert out_base == out_rev == out_shuf


class TestCounterfeitEmptyCanonical:
    """THE named counterfeit for clustering.

    Upstream can emit a candidate with claim=="". Contracts refuse that;
    cluster must reject the group, not invent text and not return blanks.
    """

    def test_empty_canonical_cluster_is_rejected_not_returned(self) -> None:
        events = [
            _ev("e1", skill="s", action="", reflection="", offset_hours=0),
            _ev("e2", skill="s", action="   ", reflection="\n", offset_hours=1),
        ]
        result = cluster(events)
        assert result == [], (
            "empty-canonical clusters must be rejected, not returned; "
            f"got {[c.claim for c in result]!r}"
        )

    def test_mixed_empty_and_real_keeps_only_real_cluster(self) -> None:
        events = [
            _ev("empty1", skill="s", action="", reflection="", offset_hours=0),
            _ev(
                "real1",
                skill="swarm.coder",
                action="commit",
                reflection="Prefer clones over worktrees for sandboxed agents",
                offset_hours=1,
            ),
            _ev(
                "real2",
                skill="swarm.coder",
                action="commit",
                reflection="Prefer clones over worktrees for sandboxed agents always",
                offset_hours=2,
            ),
        ]
        result = cluster(events)
        assert len(result) == 1
        assert result[0].claim.strip()  # non-empty
        assert "empty1" not in result[0].evidence_ids
