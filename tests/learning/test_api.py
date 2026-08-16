import json

from omniagentos.learning import beta_binomial_rate, decay_weight, wilson_lower_bound
from omniagentos.learning.api import attach_outcome, log_decision, promote_champion


def test_learning_facade_reexports_pure_helpers() -> None:
    assert wilson_lower_bound(3, 3) > 0
    # parent prior dominates at n=0; with samples the estimate moves toward observed rate
    assert beta_binomial_rate(0, 0, parent_rate=0.5) == 0.5
    assert beta_binomial_rate(10, 10, parent_rate=0.0) > 0.5
    assert decay_weight(0) == 1.0
    assert decay_weight(100) == 0.0


def test_api_appends_jsonl_records(tmp_path) -> None:
    path = tmp_path / "learning.jsonl"
    log_decision({"id": "d1", "route": "fast"}, path=path)
    attach_outcome("d1", {"verified": True}, path=path)
    promote_champion("fast", evidence={"sample_size": 20}, path=path)

    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert [row["kind"] for row in rows] == ["decision", "outcome", "champion_promoted"]
    assert rows[1]["decision_id"] == "d1"
