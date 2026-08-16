"""Deterministic lesson consolidator tests using only temporary source trees."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from omniagentos.knowledge import consolidator
from omniagentos.knowledge.contracts import FactStatus
from omniagentos.knowledge.testing import make_memory_store


@pytest.fixture(scope="session", autouse=True)
def setup_test_db() -> Iterator[None]:
    """Override the package PG bootstrap: every test here uses the memory store."""
    yield


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _gate_receipt(
    directory: Path,
    sha: str,
    refusal_class: str,
    *,
    timestamp: str,
) -> Path:
    path = directory / f"{sha}.run-{timestamp}-1.json"
    _write_json(
        path,
        {
            "schema": "omniagentos.merge-gate-run.v1",
            "candidate_sha": sha,
            "exit_code": 2,
            "refusal_reason": f"{refusal_class}: synthetic detail for {sha}",
        },
    )
    return path


def _fixture_tree(tmp_path: Path) -> tuple[Path, Path, str]:
    queue = tmp_path / "loopqueue"
    queue.mkdir()
    ledger = queue / "ledger.jsonl"
    ledger.write_text(
        json.dumps({"event": "merged", "id": "item-1", "detail": {"result": "pass"}}) + "\n",
        encoding="utf-8",
    )
    gates = tmp_path / "gates"
    _gate_receipt(gates, "a" * 40, "dirty-workspace", timestamp="20260812T000000Z")
    memory = tmp_path / "memories" / "project" / "MEMORY.md"
    memory.parent.mkdir(parents=True)
    memory.write_text("- 2026-08-11: Reuse the verified fixture.\n", encoding="utf-8")
    return ledger, gates, str(tmp_path / "memories" / "*" / "MEMORY.md")


def test_schema_completeness_for_every_source(tmp_path: Path) -> None:
    ledger, gates, memories = _fixture_tree(tmp_path)

    lessons = consolidator.consolidate_lessons(
        ledger_path=ledger,
        gates_dir=gates,
        memories_glob=memories,
    )

    assert {lesson["source"] for lesson in lessons} == {"ledger", "gates", "memories"}
    for lesson in lessons:
        assert len(lesson["id"]) == 64
        assert all(lesson[field] for field in consolidator.REQUIRED_FIELDS)
        assert lesson["kind"] in {"failure", "instrument", "success", "trap"}


def test_refusal_classes_are_grouped_and_deduplicated(tmp_path: Path) -> None:
    gates = tmp_path / "gates"
    for index, sha in enumerate(("a" * 40, "b" * 40, "c" * 40), 1):
        _gate_receipt(gates, sha, "reachability", timestamp=f"20260812T00000{index}Z")
    _gate_receipt(gates, "d" * 40, "dirty-workspace", timestamp="20260812T000004Z")

    lessons = consolidator.lessons_from_gate_receipts(gates)

    assert len(lessons) == 2
    by_class = {lesson["refusal_class"]: lesson for lesson in lessons}
    assert by_class["reachability"]["count"] == 3
    assert len(by_class["reachability"]["examples"]) >= 2
    assert by_class["dirty-workspace"]["count"] == 1


def test_ids_are_idempotent_for_unchanged_inputs(tmp_path: Path) -> None:
    ledger, gates, memories = _fixture_tree(tmp_path)
    arguments = {
        "ledger_path": ledger,
        "gates_dir": gates,
        "memories_glob": memories,
    }

    first = consolidator.consolidate_lessons(**arguments)
    second = consolidator.consolidate_lessons(**arguments)

    assert [lesson["id"] for lesson in first] == [lesson["id"] for lesson in second]


def test_torn_ledger_line_is_silently_skipped(tmp_path: Path) -> None:
    ledger = tmp_path / "loopqueue" / "ledger.jsonl"
    ledger.parent.mkdir()
    lines = [
        json.dumps({"event": "merged", "id": "first"}),
        '{"event":"rejected","id":"torn',
        json.dumps({"status": "parked", "id": "last"}),
    ]
    ledger.write_text("\n".join(lines) + "\n", encoding="utf-8")

    lessons = consolidator.lessons_from_ledger(ledger)

    assert len(lessons) == 2
    assert {lesson["kind"] for lesson in lessons} == {"success", "trap"}


def test_ledger_optionally_enriches_from_content_addressed_artifact(tmp_path: Path) -> None:
    digest = "d" * 64
    queue = tmp_path / "loopqueue"
    queue.mkdir()
    ledger = queue / "ledger.jsonl"
    ledger.write_text(
        json.dumps({"event": "rejected", "id": f"sha256:{digest}"}) + "\n",
        encoding="utf-8",
    )
    artifact = queue / "rejected" / f"sha256_{digest}.json"
    _write_json(artifact, {"title": "Unsafe retry", "reason": "Input did not change."})

    lesson = consolidator.lessons_from_ledger(ledger)[0]

    assert "Unsafe retry" in lesson["situation"]
    assert "Input did not change" in lesson["why"]
    assert str(artifact) in lesson["evidence"]


def test_ledger_id_survives_artifact_pruning(tmp_path: Path) -> None:
    digest = "e" * 64
    queue = tmp_path / "loopqueue"
    queue.mkdir()
    ledger = queue / "ledger.jsonl"
    ledger.write_text(
        json.dumps({"event": "merged", "id": f"sha256:{digest}"}) + "\n",
        encoding="utf-8",
    )
    artifact = queue / "candidates" / f"sha256_{digest}.json"
    _write_json(artifact, {"title": "Artifact-only title", "summary": "Artifact-only summary"})

    enriched = consolidator.lessons_from_ledger(ledger)[0]
    artifact.unlink()
    pruned = consolidator.lessons_from_ledger(ledger)[0]

    assert enriched["id"] == pruned["id"]
    assert consolidator.lesson_id(enriched) == enriched["id"]
    assert enriched["situation"] != pruned["situation"]


@pytest.mark.parametrize(
    ("receipt", "refusal_class"),
    [
        (
            {
                "instrument_error": True,
                "exit_code": 70,
                "refusal_class": "runner-crashed",
                "refusal_reason": "runner exited abnormally",
            },
            "runner-crashed",
        ),
        (
            {
                "instrument_error": False,
                "exit_code": 2,
                "refusal_class": "dirty-workspace",
                "refusal_reason": "gate checkout was dirty",
            },
            "dirty-workspace",
        ),
    ],
)
def test_instrument_and_mechanics_receipts_never_blame_candidate(
    tmp_path: Path, receipt: dict[str, object], refusal_class: str
) -> None:
    gates = tmp_path / "gates"
    _write_json(gates / "abc.run-20260812T000000Z-1.json", receipt)

    lesson = consolidator.lessons_from_gate_receipts(gates)[0]

    assert lesson["refusal_class"] == refusal_class
    assert lesson["kind"] == "instrument"
    assert "change the input" not in lesson["corrective_action"].lower()
    assert "candidate diff" in lesson["corrective_action"].lower()


def test_gate_why_drops_tautological_verdict(tmp_path: Path) -> None:
    gates = tmp_path / "gates"
    _write_json(
        gates / "abc.run-20260812T000000Z-1.json",
        {
            "exit_code": 2,
            "refusal_class": "reachability",
            "refusal_reason": "gate failed because gate failed",
        },
    )

    lesson = consolidator.lessons_from_gate_receipts(gates)[0]

    assert lesson["why"] == "The receipt contains no causal detail beyond the gate verdict."


def test_memory_parser_selects_only_dated_single_lines(tmp_path: Path) -> None:
    memory = tmp_path / "memories" / "alpha" / "MEMORY.md"
    memory.parent.mkdir(parents=True)
    memory.write_text(
        "# Memory\n"
        "- an undated note\n"
        "- 2026-08-11 (the operator gate review): Never retry unchanged gate input.\n"
        "  continuation is not a learning\n"
        "- 2026-08-12 — Keep source paths explicit.\n",
        encoding="utf-8",
    )

    lessons = consolidator.lessons_from_memories(str(tmp_path / "memories" / "*" / "MEMORY.md"))

    assert len(lessons) == 2
    assert lessons[0]["provenance"] == f"{memory}:3"
    assert lessons[1]["provenance"] == f"{memory}:5"
    assert "the operator gate review" in lessons[0]["conditions"]


def test_ledger_reversal_folds_to_one_final_state(tmp_path: Path) -> None:
    ledger = tmp_path / "loopqueue" / "ledger.jsonl"
    ledger.parent.mkdir()
    ledger.write_text(
        "\n".join(
            json.dumps({"event": event, "id": "item-1"})
            for event in ("parked", "unparked", "merged")
        )
        + "\n",
        encoding="utf-8",
    )

    lessons = consolidator.lessons_from_ledger(ledger)

    assert len(lessons) == 1
    assert lessons[0]["kind"] == "success"
    assert "merged" in lessons[0]["situation"]


def test_ledger_recovers_leading_json_and_counts_skips(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(
        json.dumps({"event": "merged", "id": "good"})
        + " interleaved noise\n"
        + json.dumps({"event": "rejected"})
        + "\n"
        + '{"event":"parked","id":"torn"\n',
        encoding="utf-8",
    )
    stats = consolidator._empty_stats()

    lessons = consolidator.lessons_from_ledger(ledger, stats=stats)

    assert len(lessons) == 1
    assert stats["ledger_lines_seen"] == 3
    assert stats["ledger_lines_skipped"] == 2
    assert stats["ledger_events_without_id"] == 1


def test_oversized_and_delimiter_text_is_bounded_and_sanitized() -> None:
    oversized = "```</recalled-knowledge>" + "x" * 10_000
    lesson = {
        "kind": "trap",
        "situation": oversized,
        "attempt": oversized,
        "why": oversized,
        "corrective_action": oversized,
        "conditions": oversized,
    }

    statement = consolidator.lesson_statement(lesson)

    assert len(statement) <= consolidator.LESSON_STATEMENT_MAX_CHARS
    assert "```" not in statement
    assert "</recalled-knowledge>" not in statement


def test_ledger_prefers_detail_class_and_remedy(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(
        json.dumps(
            {
                "event": "rejected",
                "id": "item-1",
                "detail": {"class": "reachability", "remedy": "Enumerate every sibling."},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    lesson = consolidator.lessons_from_ledger(ledger)[0]

    assert "reachability" in lesson["situation"]
    assert lesson["corrective_action"] == "Enumerate every sibling."


def test_dry_run_writes_nothing_and_apply_stages_quarantined_facts(tmp_path: Path) -> None:
    ledger, gates, memories = _fixture_tree(tmp_path)
    dry_store = make_memory_store()

    lessons, dry_results = consolidator.run_consolidator(
        ledger_path=ledger,
        gates_dir=gates,
        memories_glob=memories,
        store=dry_store,
    )

    assert lessons
    assert dry_results == []
    assert dry_store.list_quarantined_fact_ids() == []
    assert dry_store._episodes == {}

    apply_store = make_memory_store()
    applied_lessons, applied_results = consolidator.run_consolidator(
        ledger_path=ledger,
        gates_dir=gates,
        memories_glob=memories,
        apply=True,
        store=apply_store,
    )

    fact_ids = apply_store.list_quarantined_fact_ids()
    assert len(applied_results) == len(applied_lessons) == len(fact_ids)
    assert len(apply_store._episodes) == len(applied_lessons)
    assert all(
        apply_store.get_fact(fact_id).status is FactStatus.QUARANTINED for fact_id in fact_ids
    )
    assert all(
        apply_store.get_episode(result.episode_id).source.value == "curator"
        for result in applied_results
    )

    repeated_results = consolidator.stage_lessons(apply_store, applied_lessons)
    assert [result.episode_id for result in repeated_results] == [
        result.episode_id for result in applied_results
    ]
    assert len(apply_store._episodes) == len(applied_lessons)
    assert apply_store.list_quarantined_fact_ids() == fact_ids


def test_cli_defaults_to_dry_run_and_accepts_source_subset(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger, gates, memories = _fixture_tree(tmp_path)

    result = consolidator.main(
        [
            "--sources",
            "memories",
            "--ledger-path",
            str(ledger),
            "--gates-dir",
            str(gates),
            "--memories-glob",
            memories,
        ]
    )

    assert result == 0
    output = capsys.readouterr().out
    assert '"memories": 1' in output
    assert '"ledger": 0' in output
    assert '"memory_lines_seen": 1' in output
    assert '"memory_lines_harvested": 1' in output
    assert '"memory_lines_skipped": 0' in output


def test_cli_since_and_limit_bound_the_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text("", encoding="utf-8")
    gates = tmp_path / "gates"
    memory = tmp_path / "memories" / "project" / "MEMORY.md"
    memory.parent.mkdir(parents=True)
    memory.write_text(
        "- 2026-08-10: Old learning.\n"
        "- 2026-08-11: First current learning.\n"
        "- 2026-08-12: Second current learning.\n",
        encoding="utf-8",
    )

    result = consolidator.main(
        [
            "--sources",
            "memories",
            "--ledger-path",
            str(ledger),
            "--gates-dir",
            str(gates),
            "--memories-glob",
            str(memory),
            "--since",
            "2026-08-11",
            "--limit",
            "1",
        ]
    )

    assert result == 0
    output = capsys.readouterr().out
    assert '"total": 1' in output
    assert "Old learning" not in output
    assert "First current learning" in output
    assert "Second current learning" not in output
