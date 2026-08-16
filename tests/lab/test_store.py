from __future__ import annotations

from omniagentos.lab.store import LabStore, PanelMember, VerdictProvenance


def _record(experiment_id: str, **changes: object) -> VerdictProvenance:
    values: dict[str, object] = {
        "experiment_id": experiment_id,
        "panel_composition": (
            PanelMember("judge-a", "lineage-a"),
            PanelMember("judge-b", "lineage-b"),
        ),
        "replicate_count": 3,
        "effective_n": 3,
        "agreement": 0.8,
        "mde": 0.1,
        "observed_effect": 0.2,
        "blind_presentation_seed": 77,
        "created_at": "2026-07-27T12:00:00Z",
    }
    values.update(changes)
    return VerdictProvenance(**values)  # type: ignore[arg-type]


def test_store_round_trips_verdict_provenance() -> None:
    store = LabStore(":memory:")
    verdict_id = store.record_verdict_provenance(_record("exp-valid"))

    fetched = store.get_verdict_provenance("exp-valid")

    assert fetched is not None
    assert fetched.verdict_id == verdict_id
    assert fetched.panel_composition[1].lineage == "lineage-b"
    assert fetched.replicate_count == 3
    assert fetched.agreement == 0.8
    assert fetched.mde == 0.1
    assert fetched.blind_presentation_seed == 77


def test_invalidation_query_reports_all_applicable_reasons() -> None:
    store = LabStore(":memory:")
    store.record_verdict_provenance(_record("exp-valid"))
    store.record_verdict_provenance(
        _record(
            "exp-invalid",
            panel_composition=(PanelMember("judge-a", "lineage-a"),),
            effective_n=1,
            agreement=0.5,
            observed_effect=0.05,
        )
    )

    invalidated = store.invalidated_verdicts()

    assert [item.provenance.experiment_id for item in invalidated] == ["exp-invalid"]
    assert invalidated[0].reasons == (
        "SINGLE_LINEAGE_PANEL",
        "LOW_AGREEMENT",
        "INSUFFICIENT_N",
        "EFFECT_BELOW_MDE",
    )


def test_explicit_invalidation_is_queryable() -> None:
    store = LabStore(":memory:")
    store.record_verdict_provenance(_record("exp-invalidated"))

    assert store.invalidate_verdict("exp-invalidated") is True
    assert store.invalidate_verdict("missing") is False
    assert store.invalidated_verdicts()[0].reasons == ("EXPLICITLY_INVALIDATED",)
