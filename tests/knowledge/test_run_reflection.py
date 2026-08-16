"""Mock-mode run reflections are deterministic and quarantined."""

from __future__ import annotations

from omniagentos.knowledge.contracts import EpisodeSource, FactStatus
from omniagentos.knowledge.ingest import ingest_run_reflection


def test_mock_run_reflection_is_written(knowledge_store: object) -> None:
    run = {"id": "run_mock_1", "task_title": "Summarize report", "state": "completed"}
    steps = [{"name": "draft", "status": "completed", "result_json": '{"lesson":"be concise"}'}]
    result = ingest_run_reflection(knowledge_store, run, steps)

    assert result is not None
    episode = knowledge_store.get_episode(result.episode_id)
    assert episode is not None
    assert episode.source is EpisodeSource.RUN
    assert episode.source_ref == "run_mock_1"
    assert result.fact_ids
    assert all(
        knowledge_store.get_fact(fact_id).status is FactStatus.QUARANTINED
        for fact_id in result.fact_ids
    )
