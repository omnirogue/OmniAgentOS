"""curate() (omniagentos/selfimprove/curator.py) — the runner-ledger consumer
that wires capture_skill() into the live system (post-Wave-4)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from omniagentos.contracts import (
    AgentUsage,
    HarnessProfile,
    HarnessType,
    IdempotencyReceipt,
    RunManifest,
    RunState,
)
from omniagentos.knowledge.memory_store import InMemoryKnowledgeStore
from omniagentos.knowledge.testing import make_fake_embedder
from omniagentos.ledger import append_manifest
from omniagentos.selfimprove import curator
from omniagentos.selfimprove.paths import skill_relpath


def _manifest(
    run_id: str, *, state: RunState, task_id: str = "task-1", **overrides: Any
) -> RunManifest:
    base: dict[str, Any] = {
        "run_id": run_id,
        "task_id": task_id,
        "discipline": "code-changes",
        "harness": HarnessProfile(
            harness=HarnessType.CLI_CLAUDE, version="1.0", env_hash="deadbeef"
        ),
        "agent": "claude-coder",
        "model": "sonnet/high",
        "state": state,
        "started_at": "2026-07-20T12:00:00Z",
        "finished_at": "2026-07-20T12:30:00Z",
        "usage": AgentUsage(wall_ms=1000),
        "receipts": [
            IdempotencyReceipt(
                key=f"{run_id}-k1",
                run_id=run_id,
                step_name="write_code",
                created_at="2026-07-20T12:00:00Z",
            )
        ],
        "artifacts": ["file://a.txt"],
        "output_digest": "abc123",
        "vault_note": "runs/x.md",
    }
    base.update(overrides)
    return RunManifest(**base)


@pytest.fixture
def ledger_dir(tmp_path: Path) -> Path:
    d = tmp_path / "ledger"
    d.mkdir()
    return d


@dataclass
class MockSkills:
    """Small in-memory stand-in for S-A's skills service contract."""

    seeded: list[dict[str, Any]] = field(default_factory=list)
    records: dict[str, dict[str, Any]] = field(default_factory=dict)
    proposed: list[dict[str, Any]] = field(default_factory=list)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    upserts: list[dict[str, Any]] = field(default_factory=list)

    def list_tree(self) -> list[dict[str, Any]]:
        return list(self.seeded)

    def get_skill(self, skill_id: str) -> dict[str, Any]:
        return self.records[skill_id]

    def propose_update(self, skill_id: str, **kwargs: Any) -> str:
        self.proposed.append({"skill_id": skill_id, **kwargs})
        return f"proposal-{len(self.proposed)}"

    def decide_proposal(self, proposal_id: str, **kwargs: Any) -> dict[str, str]:
        self.decisions.append({"proposal_id": proposal_id, **kwargs})
        return {"state": "approved" if kwargs["approve"] else "rejected"}

    def upsert_skill(self, data: dict[str, Any]) -> str:
        self.upserts.append(data)
        return str(data["id"])


def _seeded_skill(
    *,
    skill_id: str = "skill-existing",
    title: str = "Workflow for task task-1 (code-changes)",
    preferred_method: str = "sonnet/high",
) -> dict[str, Any]:
    return {
        "id": skill_id,
        "slug": "unrelated-slug",
        "title": title,
        "discipline": "code-changes",
        "tags": ["code-changes", "cli-claude"],
        "preferred_method": preferred_method,
        "content": "# Existing workflow\nSummary: Previous wording.",
    }


def test_curate_captures_skill_for_completed_verified_run(
    ledger_dir: Path, vault_dir: Path
) -> None:
    manifest = _manifest("run-completed-1", state=RunState.COMPLETED)
    append_manifest(str(ledger_dir), manifest)

    result = curator.curate(ledger_dir=str(ledger_dir), vault_dir=str(vault_dir))

    assert result.captured == ["run-completed-1"]
    assert result.unverified == []
    assert result.errors == {}
    relpath = skill_relpath(curator._skill_id_for("run-completed-1"))
    note = vault_dir / relpath
    assert note.is_file()
    content = note.read_text(encoding="utf-8")
    assert "task-1" in content
    assert "write_code" in content


def test_curate_never_captures_failed_or_cancelled_runs(ledger_dir: Path, vault_dir: Path) -> None:
    failed = _manifest("run-failed-1", state=RunState.FAILED)
    cancelled = _manifest("run-cancelled-1", state=RunState.CANCELLED)
    append_manifest(str(ledger_dir), failed)
    append_manifest(str(ledger_dir), cancelled)

    result = curator.curate(ledger_dir=str(ledger_dir), vault_dir=str(vault_dir))

    assert result.captured == []
    assert set(result.unverified) == {"run-failed-1", "run-cancelled-1"}
    assert not (vault_dir / "playbook").exists()


def test_curate_is_idempotent_across_repeated_invocations(
    ledger_dir: Path, vault_dir: Path
) -> None:
    manifest = _manifest("run-completed-2", state=RunState.COMPLETED)
    append_manifest(str(ledger_dir), manifest)

    first = curator.curate(ledger_dir=str(ledger_dir), vault_dir=str(vault_dir))
    second = curator.curate(ledger_dir=str(ledger_dir), vault_dir=str(vault_dir))

    assert first.captured == ["run-completed-2"]
    assert second.captured == []
    assert second.already_captured == ["run-completed-2"]
    assert second.errors == {}


def test_curate_records_per_manifest_errors_without_aborting_the_scan(
    ledger_dir: Path, vault_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ok = _manifest("run-ok-1", state=RunState.COMPLETED)
    boom = _manifest("run-boom-1", state=RunState.COMPLETED)
    append_manifest(str(ledger_dir), ok)
    append_manifest(str(ledger_dir), boom)

    real_capture_skill = curator.capture_skill

    def _flaky(metadata: Any, gate: Any, vault_dir_arg: Any, **kwargs: Any) -> Any:
        if gate.source_run_id == "run-boom-1":
            raise RuntimeError("boom")
        return real_capture_skill(metadata, gate, vault_dir_arg, **kwargs)

    monkeypatch.setattr(curator, "capture_skill", _flaky)

    # Must not raise -- a capture failure for one run can never blow up the scan.
    result = curator.curate(ledger_dir=str(ledger_dir), vault_dir=str(vault_dir))

    assert result.captured == ["run-ok-1"]
    assert "run-boom-1" in result.errors
    assert "boom" in result.errors["run-boom-1"]
    assert result.unverified == []


def test_curate_isolates_malformed_capability_manifest_and_normalizes_valid_company(
    ledger_dir: Path, vault_dir: Path
) -> None:
    malformed = _manifest(
        "run-malformed-capability",
        state=RunState.COMPLETED,
        extra={"capabilities": [{"domains": ["video"], "kind": "tool"}]},
    )
    valid = _manifest(
        "run-valid-capability",
        state=RunState.COMPLETED,
        task_id="task-2",
        extra={
            "company_id": "initech",
            "brand": "Initech",
            "capabilities": [
                {
                    "statement": "renderer supports synchronized media",
                    "domains": ["video"],
                    "kind": "tool",
                }
            ],
        },
    )
    append_manifest(str(ledger_dir), malformed)
    append_manifest(str(ledger_dir), valid)
    store = InMemoryKnowledgeStore(embedder=make_fake_embedder())

    result = curator.curate(
        ledger_dir=str(ledger_dir),
        vault_dir=str(vault_dir),
        capability_store=store,
    )

    assert result.captured == ["run-valid-capability"]
    assert "run-malformed-capability" in result.errors
    [fact] = store._facts.values()
    assert fact.company_id == "co_initech"


def test_curate_defaults_to_env_ledger_and_vault_dirs_when_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger_dir = tmp_path / "ledger"
    ledger_dir.mkdir()
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    monkeypatch.setenv("OMNIAGENTOS_LEDGER_DIR", str(ledger_dir))
    monkeypatch.setenv("OMNIAGENTOS_VAULT_DIR", str(vault_dir))

    manifest = _manifest("run-completed-3", state=RunState.COMPLETED)
    append_manifest(str(ledger_dir), manifest)

    result = curator.curate()

    assert result.captured == ["run-completed-3"]


def test_curate_matches_learning_to_existing_skill_and_proposes_low_risk_update(
    ledger_dir: Path, vault_dir: Path
) -> None:
    manifest = _manifest("run-match-low", state=RunState.COMPLETED)
    append_manifest(str(ledger_dir), manifest)
    existing = _seeded_skill()
    skills = MockSkills(seeded=[existing], records={existing["id"]: existing})

    result = curator.curate(ledger_dir=str(ledger_dir), vault_dir=str(vault_dir), skills_api=skills)

    assert result.captured == []
    assert result.proposals == {"run-match-low": "proposal-1"}
    assert result.pending_proposals == []
    assert skills.proposed[0]["skill_id"] == "skill-existing"
    assert skills.proposed[0]["risk"] == "low"
    assert skills.decisions == [
        {"proposal_id": "proposal-1", "approve": True, "decided_by": "curator"}
    ]


def test_curate_proposes_model_swap_as_high_risk_pending(ledger_dir: Path, vault_dir: Path) -> None:
    manifest = _manifest("run-model-swap", state=RunState.COMPLETED, model="Claude Opus")
    append_manifest(str(ledger_dir), manifest)
    existing = _seeded_skill(preferred_method="GPT-4o")
    skills = MockSkills(seeded=[existing], records={existing["id"]: existing})

    result = curator.curate(ledger_dir=str(ledger_dir), vault_dir=str(vault_dir), skills_api=skills)

    assert result.captured == []
    assert result.proposals == {"run-model-swap": "proposal-1"}
    assert result.pending_proposals == ["proposal-1"]
    assert skills.proposed[0]["risk"] == "model"
    assert skills.decisions == []


def test_curate_unknown_preferred_method_does_not_auto_approve_model_bearing_update(
    ledger_dir: Path, vault_dir: Path
) -> None:
    """Non-result-as-favourable: unknown current preferred_method must not be
    scored risk=low (and auto-approved) when the learning carries a model.

    Before the fix, ``isinstance(preferred_method, str) and model != preferred``
    short-circuited to False when preferred_method was missing/None/non-str, so
    the update fell through to risk=low and ``decide_proposal(approve=True)``.
    That is the same class of defect as recording unknown cost as 0.0 — an
    unmeasurable baseline presented as "safe to auto-approve".

    Counterfeit this catches: treating unknown as matching the learning's
    model (``preferred = skill.get("preferred_method") or metadata.model``)
    would still return low and auto-approve. The assertion requires risk to
    be non-low AND no auto-approval decision.
    """
    manifest = _manifest("run-unknown-method", state=RunState.COMPLETED, model="Claude Opus")
    append_manifest(str(ledger_dir), manifest)
    existing = _seeded_skill()
    del existing["preferred_method"]  # unmeasurable current method
    skills = MockSkills(seeded=[existing], records={existing["id"]: existing})

    result = curator.curate(ledger_dir=str(ledger_dir), vault_dir=str(vault_dir), skills_api=skills)

    assert result.proposals == {"run-unknown-method": "proposal-1"}
    assert skills.proposed[0]["risk"] == "model"
    assert skills.decisions == [], (
        "unknown preferred_method must not auto-approve a model-bearing update"
    )
    assert result.pending_proposals == ["proposal-1"]


@pytest.mark.parametrize(
    "preferred_method",
    [None, "", "   ", 0, 42, True, ["sonnet/high"], {"v": "x"}],
)
def test_risk_for_update_unknown_preferred_method_is_model_not_low(
    preferred_method: object,
) -> None:
    """Unit-level: any unmeasurable preferred_method + a known learning model
    must return risk='model', never 'low'.

    Counterfeit: ``if preferred_method is None: return "model"`` only — still
    fails for "", non-str, or whitespace. Counterfeit: ``or metadata.model``
    fallback still returns low.
    """
    metadata = curator._skill_metadata_from_manifest(
        _manifest("run-unit-risk", state=RunState.COMPLETED, model="Claude Opus")
    )
    skill: dict[str, Any] = {
        "id": "skill-existing",
        "title": metadata.title,
        "discipline": "code-changes",
        "content": "# Existing workflow\nSummary: Previous wording.",
        # Always set the key (missing-key case is the separate e2e test).
        "preferred_method": preferred_method,  # type: ignore[dict-item]
    }

    risk = curator._risk_for_update(metadata, skill, "x" * 100)
    assert risk == "model", f"unmeasurable preferred_method={preferred_method!r} scored {risk!r}"


def test_risk_for_update_missing_preferred_method_key_is_model_not_low() -> None:
    metadata = curator._skill_metadata_from_manifest(
        _manifest("run-unit-risk-missing", state=RunState.COMPLETED, model="Claude Opus")
    )
    skill = {
        "id": "skill-existing",
        "title": metadata.title,
        "discipline": "code-changes",
        "content": "# Existing workflow\nSummary: Previous wording.",
        # preferred_method key deliberately absent
    }
    assert curator._risk_for_update(metadata, skill, "x" * 100) == "model"


def test_risk_for_update_unknown_preferred_method_stays_low_without_learning_model() -> None:
    """Wording-only learnings (no model on the learning) must still auto-path
    when preferred_method is unknown — otherwise the fail-closed model check
    would block every routine wording update. Counterfeit: unconditional
    ``if preferred_method is None: return "model"`` fails this.
    """
    metadata = curator._skill_metadata_from_manifest(
        _manifest("run-wording", state=RunState.COMPLETED, model=None)
    )
    assert metadata.model is None
    skill = {
        "id": "skill-existing",
        "title": metadata.title,
        "discipline": "code-changes",
        "content": "# Existing workflow\nSummary: Previous wording.",
    }
    assert curator._risk_for_update(metadata, skill, "x" * 100) == "low"


def test_curate_proposes_interface_change_as_major_pending(
    ledger_dir: Path, vault_dir: Path
) -> None:
    manifest = _manifest("run-interface-change", state=RunState.COMPLETED)
    append_manifest(str(ledger_dir), manifest)
    existing = _seeded_skill()
    existing["input_format"] = "A previously documented input contract"
    skills = MockSkills(seeded=[existing], records={existing["id"]: existing})

    result = curator.curate(ledger_dir=str(ledger_dir), vault_dir=str(vault_dir), skills_api=skills)

    assert result.pending_proposals == ["proposal-1"]
    assert skills.proposed[0]["risk"] == "major"
    assert skills.decisions == []


def test_curate_captures_new_skill_when_no_match(ledger_dir: Path, vault_dir: Path) -> None:
    manifest = _manifest("run-new-skill", state=RunState.COMPLETED, task_id="unrelated-task")
    append_manifest(str(ledger_dir), manifest)
    existing = _seeded_skill(title="A completely separate marketing workflow")
    skills = MockSkills(seeded=[existing], records={existing["id"]: existing})

    result = curator.curate(ledger_dir=str(ledger_dir), vault_dir=str(vault_dir), skills_api=skills)

    assert result.captured == ["run-new-skill"]
    assert result.proposals == {}
    assert skills.proposed == []
    assert len(skills.upserts) == 1
    assert (vault_dir / skill_relpath(curator._skill_id_for("run-new-skill"))).is_file()


def test_curate_never_double_matches_same_run_id(ledger_dir: Path, vault_dir: Path) -> None:
    manifest = _manifest("run-match-once", state=RunState.COMPLETED)
    append_manifest(str(ledger_dir), manifest)
    existing = _seeded_skill()
    skills = MockSkills(seeded=[existing], records={existing["id"]: existing})

    curator.curate(ledger_dir=str(ledger_dir), vault_dir=str(vault_dir), skills_api=skills)
    second = curator.curate(ledger_dir=str(ledger_dir), vault_dir=str(vault_dir), skills_api=skills)

    assert len(skills.proposed) == 1
    assert second.already_captured == ["run-match-once"]


def test_curate_ignores_routine_chatter_below_match_threshold(
    ledger_dir: Path, vault_dir: Path
) -> None:
    manifest = _manifest("run-chatter", state=RunState.COMPLETED, task_id="brief")
    append_manifest(str(ledger_dir), manifest)
    existing = _seeded_skill(title="Weekly finance reconciliation process")
    skills = MockSkills(seeded=[existing], records={existing["id"]: existing})

    result = curator.curate(ledger_dir=str(ledger_dir), vault_dir=str(vault_dir), skills_api=skills)

    assert result.captured == ["run-chatter"]
    assert skills.proposed == []
