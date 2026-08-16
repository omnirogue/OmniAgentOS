"""Flag-gated selector parity and semantic reranking."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import asdict
from importlib import import_module
from pathlib import Path

import pytest

from omniagentos.semsearch.search import SemHit
from omniagentos.skills.select import SkillHit, select_skills

search_module = import_module("omniagentos.semsearch.search")


REGISTRY = [
    {
        "id": "alpha-id",
        "slug": "alpha",
        "name": "alpha",
        "version": "1",
        "domains": ["ops"],
        "status": "active",
    },
    {
        "id": "beta-id",
        "slug": "beta",
        "name": "beta",
        "version": "1",
        "domains": ["ops"],
        "status": "active",
    },
]

PARITY_REGISTRY = [
    {
        "id": "all-signals",
        "name": "all-signals",
        "version": "1",
        "domains": ["ops"],
        "risk_classes": ["major"],
        "tools": ["shell"],
        "artifacts": ["report"],
        "status": "active",
    },
    {
        "id": "deprecated",
        "name": "deprecated",
        "version": "2",
        "domains": ["ops"],
        "risk_classes": ["major"],
        "tools": ["shell"],
        "artifacts": ["report"],
        "status": "deprecated",
    },
    {
        "id": "experimental",
        "name": "experimental",
        "version": "3",
        "tools": ["shell"],
        "status": "experimental",
    },
    {"id": "tie-b", "name": "tie-b", "domains": ["ops"], "status": "active"},
    {"id": "tie-a", "name": "tie-a", "domains": ["ops"], "status": "active"},
    {"id": "filler-b", "name": "filler-b", "status": "active"},
    {"id": "filler-a", "name": "filler-a", "status": "active"},
    {
        "id": "archived",
        "name": "archived",
        "domains": ["ops"],
        "status": "archived",
    },
    {"id": "unknown", "name": "unknown", "domains": ["ops"], "status": "mystery"},
]

FROZEN_PARENT_OUTPUTS = [
    (
        {
            "domain": "ops",
            "risk_class": "major",
            "allowed_tools": ["shell"],
            "expected_artifacts": ["report"],
            "max_skills": 8,
        },
        '[{"name":"all-signals","reason":"domain:ops,risk:MAJOR,tool_overlap,'
        'artifact_overlap","score":5.5,"version":"1"},{"name":"deprecated",'
        '"reason":"domain:ops,risk:MAJOR,tool_overlap,artifact_overlap,status:deprecated",'
        '"score":2.75,"version":"2"},{"name":"tie-a","reason":"domain:ops",'
        '"score":2.0,"version":"0"},{"name":"tie-b","reason":"domain:ops",'
        '"score":2.0,"version":"0"},{"name":"experimental","reason":"tool_overlap,'
        'status:experimental","score":0.75,"version":"3"}]',
    ),
    (
        {"domain": "ops", "max_skills": 2},
        '[{"name":"all-signals","reason":"domain:ops","score":2.0,"version":"1"},'
        '{"name":"tie-a","reason":"domain:ops","score":2.0,"version":"0"}]',
    ),
    (
        {"max_skills": 5, "min_skills": 3},
        '[{"name":"all-signals","reason":"min_skills_fill","score":0.0,"version":"1"},'
        '{"name":"filler-a","reason":"min_skills_fill","score":0.0,"version":"0"},'
        '{"name":"filler-b","reason":"min_skills_fill","score":0.0,"version":"0"}]',
    ),
]


def _serialized_hits(**kwargs: object) -> str:
    return json.dumps(
        [asdict(hit) for hit in select_skills(PARITY_REGISTRY, **kwargs)],
        sort_keys=True,
        separators=(",", ":"),
    )


def test_flag_off_is_exact_taxonomy_parity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OMNIAGENTOS_SEMSEARCH", raising=False)

    def forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("semantic search must not be called while the flag is off")

    monkeypatch.setattr(search_module, "search", forbidden)
    assert select_skills(REGISTRY, domain="ops") == [
        SkillHit("alpha", "1", 2.0, "domain:ops"),
        SkillHit("beta", "1", 2.0, "domain:ops"),
    ]


@pytest.mark.parametrize("flag_value", [None, "0"], ids=["unset", "zero"])
def test_flag_off_matches_frozen_parent_output_matrix(
    monkeypatch: pytest.MonkeyPatch,
    flag_value: str | None,
) -> None:
    if flag_value is None:
        monkeypatch.delenv("OMNIAGENTOS_SEMSEARCH", raising=False)
    else:
        monkeypatch.setenv("OMNIAGENTOS_SEMSEARCH", flag_value)

    assert [_serialized_hits(**inputs) for inputs, _expected in FROZEN_PARENT_OUTPUTS] == [
        expected for _inputs, expected in FROZEN_PARENT_OUTPUTS
    ]


def test_flag_off_import_does_not_load_semsearch_modules() -> None:
    env = os.environ.copy()
    env.pop("OMNIAGENTOS_SEMSEARCH", None)
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import importlib,json,sys; "
            "importlib.import_module('omniagentos.skills.select'); "
            "print(json.dumps(sorted(name for name in sys.modules "
            "if name == 'omniagentos.semsearch' "
            "or name.startswith('omniagentos.semsearch.'))))",
        ],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(probe.stdout) == []


def test_flag_on_blends_semantic_score_and_reranks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIAGENTOS_SEMSEARCH", "1")
    monkeypatch.setattr(
        search_module,
        "search",
        lambda query, kind, limit: [
            SemHit("skill", "beta-id", "Beta", 1.0, "semantic"),
            SemHit("skill", "alpha-id", "Alpha", -1.0, "semantic"),
        ],
    )

    hits = select_skills(REGISTRY, domain="ops")

    assert [hit.name for hit in hits] == ["beta", "alpha"]
    assert hits[0].score == 1.0
    assert hits[1].score == 0.75
    assert all(hit.reason == "domain:ops,semantic_blend" for hit in hits)


def test_flag_on_dependency_fallback_preserves_taxonomy_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNIAGENTOS_SEMSEARCH", "1")
    monkeypatch.setattr(
        search_module,
        "search",
        lambda query, kind, limit: [SemHit("skill", "beta-id", "Beta", 1.0, "lexical-fallback")],
    )

    assert select_skills(REGISTRY, domain="ops") == [
        SkillHit("alpha", "1", 2.0, "domain:ops"),
        SkillHit("beta", "1", 2.0, "domain:ops"),
    ]
