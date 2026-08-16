"""SPEC §4.6 — on no-progress the queue changes the ACTION, not the tier.

Tested and killed on evidence across four lineages: escalating the model on a
repeated failure does not fix the two recurring defect classes. So after a SECOND
candidate-defect the retry is dispatched to a DIFFERENT LINEAGE profile, never a
higher effort of the same one. A same-lineage swap fails this test.

Two layers:
  * the escalation TABLE in configs/workqueue.yaml is checked unconditionally —
    it is a lookup table, not a rule engine, so it is checkable statically;
  * the store-driven half drives the real WorkQueueStore through two defects.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omniagentos.workqueue.schema import Outcome
from omniagentos.workqueue.store import WorkQueueStore

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG = REPO_ROOT / "configs" / "workqueue.yaml"


@pytest.fixture(scope="module")
def config() -> dict:
    return yaml.safe_load(CONFIG.read_text())


def test_every_escalation_pair_crosses_lineages(config: dict) -> None:
    profiles = config["profiles"]
    escalation = config["escalation"]
    assert escalation, "an empty escalation table means no-progress retries the same lineage"
    for source, target in escalation.items():
        assert source in profiles, f"escalation source {source!r} is not a declared profile"
        assert target in profiles, f"escalation target {target!r} is not a declared profile"
        assert source != target, f"{source} escalates to itself"
        source_lineage = profiles[source].get("lineage")
        target_lineage = profiles[target].get("lineage")
        assert source_lineage and target_lineage, "every profile must declare a lineage"
        assert source_lineage != target_lineage, (
            f"{source} -> {target} stays inside lineage {source_lineage!r}: "
            "SPEC §4.6 requires a different LINEAGE, not a different effort"
        )


def test_every_agent_profile_has_an_escalation_target(config: dict) -> None:
    """A profile with no escalation target silently retries itself forever."""
    for name, profile in config["profiles"].items():
        if not profile.get("cmd"):
            continue  # the `script` profile has no agent turn to escalate
        assert name in config["escalation"], f"profile {name!r} has no §4.6 escalation target"


def test_two_candidate_defects_swap_the_profile(tmp_path: Path, config: dict) -> None:
    store = WorkQueueStore(str(tmp_path / "wq.sqlite3"))
    unit_id, _ = store.enqueue(
        {
            "idempotency_key": "escalation-probe",
            "repo_url": "https://example.invalid/repo.git",
            "repo_slug": "repo",
            "base_sha": "0" * 40,
            "branch": "wq/escalation-probe",
            "owned_paths": ["demo/**"],
            "agent_profile": "codex-exec",
            "acceptance_cmd": "python3 -c 'raise SystemExit(1)'",
            "risk_class": "standard",
            "max_attempts": 3,
        }
    )
    for _ in range(2):
        claim = store.claim("m1", "m1:1:aaaa", [])
        assert claim is not None
        store.record_result(
            claim["unit"]["id"],
            "m1:m1:1:aaaa",
            claim["lease_generation"],
            Outcome.CANDIDATE_DEFECT.value,
            exit_code=1,
            retryable=0,
            remedy="deliberate",
        )
    after = store.get_unit(unit_id)
    assert after is not None
    assert after["agent_profile"] != "codex-exec", "the second defect did not swap the profile"
    profiles = config["profiles"]
    assert profiles[after["agent_profile"]]["lineage"] != profiles["codex-exec"]["lineage"], (
        "the swap stayed inside one lineage"
    )
