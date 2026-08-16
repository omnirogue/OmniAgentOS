"""U-16 corpus hygiene: the mock-capture corpus can no longer crowd out real skills.

Background (measured at HEAD before this change): 34 of 41 live skills were
curator auto-captures of the shape ``skill: Workflow for task tsk_*`` whose
entire Steps section was "No step-level detail was recorded…", most of them
from ``harness=mock`` runs. They carried ``category='General'`` → ``domains=
['general']``, so they won every generic domain query and pushed the 7 real
skills out of the ``hits[:12]`` window the spawner injects.

DECISIVE TEST
    ``test_domain_query_returns_real_skills_with_zero_quarantined_entries``
    — a domain query over a corpus seeded exactly like the live one returns the
    real skill inside the injected window and ZERO quarantined entries.

COUNTERFEITS
    * ``test_renamed_mock_stub_is_still_excluded_by_status`` — the realistic
      fake: relabel a mock stub with the high-scoring domain term (and give it
      declared tools so it would out-rank on three signals). Status, not the
      name, is what excludes it.
    * ``test_mock_harness_run_does_not_become_a_skill`` — a newly captured
      ``harness=mock`` run produces no note, no row and no proposal.
    * ``test_quarantine_survives_a_vault_reindex`` — the note still says
      ``status: active``; a re-index must not resurrect the row.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from omniagentos.skills import (
    AUTO_CAPTURE_QUARANTINE_REASON,
    QUARANTINED_STATUS,
    get_skill,
    index_vault_playbook,
    list_skills,
    release_quarantine,
    upsert_skill,
)
from omniagentos.skills.select import select_skills

# Verbatim from omniagentos/selfimprove/curator.py's zero-step fallback.
_NO_STEPS = (
    "1. No step-level detail was recorded in the ledger manifest for this run; "
    "see the run's vault note (if any) for the full step-by-step record."
)


def _mock_capture_body(task_id: str, *, harness: str = "mock") -> str:
    return (
        f"# skill: Workflow for task {task_id} (general)\n\n"
        "## Summary\n\n"
        f"Reusable pattern captured from a COMPLETED run of task {task_id} ({harness}).\n\n"
        "## Steps\n\n"
        f"{_NO_STEPS}\n\n"
        "## Provenance\n\n"
        f"- **Evidence:** runner_state=completed; harness={harness}\n"
    )


def _seed_corpus(db_path: str, *, mock_count: int = 34) -> None:
    """One real skill plus a live-shaped pile of auto-captures."""
    upsert_skill(
        {
            "slug": "copywriting_brain_vault",
            "category": "Advertising",
            "subcategory": "Copywriting",
            "title": "Ground Ad Copy in the CopywritingBrain Vault",
            "summary": "Where the copywriting reference vault lives.",
            "content_snapshot": "## Preferred Method\n1. Read the vault's agent guide.\n",
            "tools": ["rg"],
            "artifacts": ["ad_copy"],
            "risk_classes": ["LOW"],
        }
    )
    for index in range(mock_count):
        upsert_skill(
            {
                "slug": f"run-run_{index:020x}",
                "category": "General",
                "subcategory": "General",
                "title": f"Workflow for task tsk_{index:020x} (general)",
                "content_snapshot": _mock_capture_body(f"tsk_{index:020x}"),
            }
        )
    del db_path


@pytest.fixture()
def skills_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    db_path = str(tmp_path / "skills.db")
    monkeypatch.setenv("OMNIAGENTOS_DB", db_path)
    return db_path


# --------------------------------------------------------------------------
# DECISIVE TEST
# --------------------------------------------------------------------------


def test_domain_query_returns_real_skills_with_zero_quarantined_entries(
    skills_db: str,
) -> None:
    _seed_corpus(skills_db)

    registry = list_skills(database=skills_db)
    names = {row["name"] for row in registry}
    assert "copywriting_brain_vault" in names
    # list_skills is the registry every selection path reads. Not one of the
    # 34 auto-captures may appear in it.
    assert not [name for name in names if name.startswith("run-run_")], sorted(names)

    # The injected window: spawn.py passes hits[:12] to the prompt builder.
    hits = select_skills(registry, domain="advertising", max_skills=32)
    window = hits[:12]
    assert window, "a domain query returned nothing at all"
    assert "copywriting_brain_vault" in {hit.name for hit in window}
    assert not [hit for hit in window if hit.name.startswith("run-run_")]

    # And the generic query the auto-captures used to monopolise.
    general = select_skills(registry, domain="general", max_skills=32)
    assert not [hit for hit in general if hit.name.startswith("run-run_")]

    # Evidence retained, not deleted: every one is still readable.
    for index in range(34):
        row = get_skill(f"run-run_{index:020x}")
        assert row["status"] == QUARANTINED_STATUS
        assert row["quarantine_reason"] == AUTO_CAPTURE_QUARANTINE_REASON
        assert _NO_STEPS in str(row["version"]["content_snapshot"])


# --------------------------------------------------------------------------
# COUNTERFEITS
# --------------------------------------------------------------------------


def test_renamed_mock_stub_is_still_excluded_by_status(skills_db: str) -> None:
    """The realistic fake: rename the junk to win the domain, and declare tools
    and artifacts so it would out-score the real skill on three of the four
    signals. Status is what excludes it, not the name."""
    _seed_corpus(skills_db, mock_count=1)
    upsert_skill(
        {
            "slug": "advertising_copywriting_master_playbook",
            "category": "Advertising",
            "subcategory": "Copywriting",
            "title": "Advertising Copywriting Master Playbook",
            "content_snapshot": _mock_capture_body("tsk_renamed"),
            "tools": ["rg"],
            "artifacts": ["ad_copy"],
            "risk_classes": ["LOW"],
        }
    )

    assert get_skill("advertising_copywriting_master_playbook")["status"] == QUARANTINED_STATUS

    registry = list_skills(database=skills_db)
    assert "advertising_copywriting_master_playbook" not in {row["name"] for row in registry}

    hits = select_skills(
        registry,
        domain="advertising",
        risk_class="LOW",
        allowed_tools=["rg"],
        expected_artifacts=["ad_copy"],
        max_skills=32,
        min_skills=3,
    )
    # Not as a match, and not as a min_skills filler either.
    assert "advertising_copywriting_master_playbook" not in {hit.name for hit in hits}
    assert "copywriting_brain_vault" in {hit.name for hit in hits}


def test_a_quarantined_row_is_hidden_even_with_include_archived(skills_db: str) -> None:
    """include_archived is an operator convenience, not a quarantine bypass."""
    _seed_corpus(skills_db, mock_count=2)
    registry = list_skills(database=skills_db, include_archived=True)
    assert not [row for row in registry if row["name"].startswith("run-run_")]


def test_quarantine_survives_a_vault_reindex(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 34 notes on disk all declare ``status: active``. Re-importing them
    must not undo the quarantine — that startup re-index runs on every API boot
    and would otherwise silently repopulate the corpus."""
    monkeypatch.setenv("OMNIAGENTOS_DB", str(tmp_path / "skills.db"))
    vault = tmp_path / "vault"
    playbook = vault / "playbook"
    playbook.mkdir(parents=True)
    monkeypatch.setenv("OMNIAGENTOS_VAULT_DIR", str(vault))
    note = playbook / "skill-run-run_deadbeef.md"
    note.write_text(
        "---\n"
        "id: run-run_deadbeef\n"
        "type: playbook\n"
        "discipline: general\n"
        "status: active\n"
        "---\n\n" + _mock_capture_body("tsk_deadbeef"),
        encoding="utf-8",
    )

    assert index_vault_playbook() == 1
    assert get_skill("run-run_deadbeef")["status"] == QUARANTINED_STATUS
    # Second pass: the note is unchanged and still says active.
    assert index_vault_playbook() == 1
    assert get_skill("run-run_deadbeef")["status"] == QUARANTINED_STATUS


def test_release_is_a_real_reversal_that_survives_reindex(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Quarantine is reversible: an operator release restores selection and is
    not re-applied by the next index run."""
    monkeypatch.setenv("OMNIAGENTOS_DB", str(tmp_path / "skills.db"))
    vault = tmp_path / "vault"
    playbook = vault / "playbook"
    playbook.mkdir(parents=True)
    monkeypatch.setenv("OMNIAGENTOS_VAULT_DIR", str(vault))
    (playbook / "skill-run-run_cafe.md").write_text(
        "---\nid: run-run_cafe\ntype: playbook\ndiscipline: general\nstatus: active\n---\n\n"
        + _mock_capture_body("tsk_cafe"),
        encoding="utf-8",
    )
    index_vault_playbook()

    released = release_quarantine("run-run_cafe", released_by="human:owner", note="kept on purpose")
    assert released["status"] == "active"
    index_vault_playbook()
    assert get_skill("run-run_cafe")["status"] == "active"

    # Releasing something that was never held is an error, not a silent success.
    with pytest.raises(ValueError):
        release_quarantine("run-run_cafe", released_by="human:owner")
    with pytest.raises(KeyError):
        release_quarantine("no-such-skill", released_by="human:owner")


def test_mock_harness_run_does_not_become_a_skill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A newly captured ``harness=mock`` run leaves no note, no row, no proposal."""
    from omniagentos.contracts import HarnessProfile, HarnessType, RunManifest, RunState
    from omniagentos.ledger import append_manifest
    from omniagentos.selfimprove import curator as curator_module

    ledger_dir = tmp_path / "ledger"
    ledger_dir.mkdir()
    vault_dir = tmp_path / "vault"
    (vault_dir / "playbook").mkdir(parents=True)

    def _manifest(run_id: str, harness: HarnessType) -> RunManifest:
        return RunManifest(
            run_id=run_id,
            task_id=f"tsk_{run_id}",
            discipline="general",
            harness=HarnessProfile(harness=harness, version="1.0", env_hash="deadbeef"),
            state=RunState.COMPLETED,
            started_at="2026-08-03T00:00:00Z",
            finished_at="2026-08-03T00:01:00Z",
        )

    append_manifest(str(ledger_dir), _manifest("run_mockcapture", HarnessType.MOCK))
    append_manifest(str(ledger_dir), _manifest("run_realcapture", HarnessType.CLI_CLAUDE))

    calls: list[tuple[str, Any]] = []

    class _RecordingApi:
        def list_tree(self) -> list[dict[str, Any]]:
            return []

        def get_skill(self, skill_id: str) -> dict[str, Any]:  # pragma: no cover
            raise AssertionError("should not be reached")

        def propose_update(self, skill_id: str, **kwargs: Any) -> str:  # pragma: no cover
            calls.append(("propose", skill_id))
            return "upd_x"

        def decide_proposal(self, proposal_id: str, **kwargs: Any) -> dict[str, Any]:
            return {}  # pragma: no cover

        def upsert_skill(self, data: dict[str, Any]) -> str:
            calls.append(("upsert", data.get("slug")))
            return str(data.get("id") or "")

    monkeypatch.setenv("OMNIAGENTOS_VAULT_DIR", str(vault_dir))
    result = curator_module.curate(
        ledger_dir=str(ledger_dir),
        vault_dir=str(vault_dir),
        skills_api=_RecordingApi(),
        autocommit=False,
    )

    assert result.scanned == 2
    assert result.not_real_work == ["run_mockcapture"]
    assert "run_mockcapture" not in result.captured
    assert result.errors == {}
    # The real-harness run still captures — the refusal is targeted, not a
    # blanket "capture nothing" that would trivially satisfy this test.
    assert result.captured == ["run_realcapture"]

    notes = sorted(path.name for path in (vault_dir / "playbook").glob("*.md"))
    assert not [name for name in notes if "mockcapture" in name], notes
    assert [name for name in notes if "realcapture" in name], notes
    assert not [call for call in calls if "mockcapture" in str(call[1] or "")]
    assert [call for call in calls if "realcapture" in str(call[1] or "")]


def test_declared_selection_metadata_reaches_the_registry(skills_db: str) -> None:
    """(c) tools/artifacts/risk_classes are real declared fields now, not
    hardcoded ``[]`` with preferred_method's prose sentence stuffed into
    ``tools``. All four scoring signals must be able to fire."""
    upsert_skill(
        {
            "slug": "declaring_skill",
            "category": "Advertising",
            "subcategory": "Copywriting",
            "title": "Declaring Skill",
            "preferred_method": "Search the vault for proven patterns and adapt them",
            "content_snapshot": "## Preferred Method\n1. Do the thing.\n",
            "tools": ["rg", "read"],
            "artifacts": ["ad_copy"],
            "risk_classes": ["LOW"],
        }
    )
    row = next(r for r in list_skills(database=skills_db) if r["name"] == "declaring_skill")
    assert row["tools"] == ["rg", "read"]
    assert row["artifacts"] == ["ad_copy"]
    assert row["risk_classes"] == ["LOW"]
    # The prose sentence must NOT be masquerading as a tool name.
    assert not any(" " in tool for tool in row["tools"])
    assert row["preferred_method"].startswith("Search the vault")

    hit = select_skills(
        [row],
        domain="advertising",
        risk_class="LOW",
        allowed_tools=["rg"],
        expected_artifacts=["ad_copy"],
    )[0]
    # domain 2.0 + risk 1.0 + tools 1.5 + artifacts 1.0 — every signal fired.
    assert hit.score == pytest.approx(5.5)
    assert {"risk:LOW", "tool_overlap", "artifact_overlap"} <= set(hit.reason.split(","))


def test_out_of_vocabulary_status_is_refused(skills_db: str) -> None:
    """One vocabulary: the DAL refuses a status the table CHECK cannot hold."""
    with pytest.raises(ValueError, match="invalid skill status"):
        upsert_skill(
            {
                "slug": "bad_status",
                "category": "Advertising",
                "subcategory": "Copywriting",
                "title": "Bad Status",
                "status": "approved",
            }
        )
