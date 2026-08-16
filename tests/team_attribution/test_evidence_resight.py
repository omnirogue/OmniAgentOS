from __future__ import annotations

from omniagentos.team.store import TeamStore


def test_deterministic_resight_updates_gate_and_meta(team_store: TeamStore) -> None:
    evidence_id = team_store.add_evidence(
        kind="pr",
        repo="org/repo",
        ref="org/repo#42",
        quality_gate="rejected",
        meta={"state": "CLOSED", "generation": 1},
    )

    same_id = team_store.add_evidence(
        kind="pr",
        repo="org/repo",
        ref="org/repo#42",
        quality_gate="pass",
        meta={"state": "MERGED", "generation": 2},
    )

    assert same_id == evidence_id
    stored = team_store.get_evidence(evidence_id)
    assert stored is not None
    assert stored["quality_gate"] == "pass"
    assert stored["meta"] == {"state": "MERGED", "generation": 2}


def test_deterministic_resight_never_overwrites_manual_row(team_store: TeamStore) -> None:
    evidence_id = team_store.add_evidence(
        kind="pr",
        repo="org/repo",
        ref="org/repo#43",
        attribution="manual",
        quality_gate="rejected",
        meta={"state": "HUMAN_REVIEWED"},
    )

    same_id = team_store.add_evidence(
        kind="pr",
        repo="org/repo",
        ref="org/repo#43",
        attribution="deterministic",
        quality_gate="pass",
        meta={"state": "MERGED"},
    )

    assert same_id == evidence_id
    stored = team_store.get_evidence(evidence_id)
    assert stored is not None
    assert stored["attribution"] == "manual"
    assert stored["quality_gate"] == "rejected"
    assert stored["meta"] == {"state": "HUMAN_REVIEWED"}
