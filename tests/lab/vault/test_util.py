"""omniagentos.lab.vault.util — JSON tolerance + the reward-hacking
`scrub_held_out` guard (Section 11.7: no L08 note may ever render a held-out
`expected` value, contracts/lab-interfaces.md §L08-labvault acceptance)."""

from __future__ import annotations

import json

from omniagentos.lab.vault.util import (
    common_value,
    fmt_list,
    fmt_metrics,
    latest_timestamp,
    maybe_json,
    scrub_held_out,
)


def test_maybe_json_decodes_json_strings() -> None:
    assert maybe_json(json.dumps({"a": 1})) == {"a": 1}
    assert maybe_json(json.dumps([1, 2, 3])) == [1, 2, 3]


def test_maybe_json_passes_through_non_strings_and_bad_json() -> None:
    assert maybe_json({"a": 1}) == {"a": 1}
    assert maybe_json(None) is None
    assert maybe_json("not json") == "not json"


def test_scrub_held_out_drops_expected_key_at_top_level() -> None:
    scrubbed = scrub_held_out({"pass_rate": 0.9, "expected": {"c1": "leak"}})
    assert "expected" not in scrubbed
    assert scrubbed == {"pass_rate": 0.9}


def test_scrub_held_out_drops_expected_key_case_insensitively_and_nested() -> None:
    scrubbed = scrub_held_out(
        {
            "metrics": {"pass_rate": 0.9},
            "Expected": "leak",
            "per_case": {"c1": {"score": 1.0, "EXPECTED_ANSWER": "leak"}},
        }
    )
    assert "Expected" not in scrubbed
    assert "EXPECTED_ANSWER" not in scrubbed["per_case"]["c1"]
    assert scrubbed["per_case"]["c1"]["score"] == 1.0


def test_scrub_held_out_recurses_into_lists() -> None:
    scrubbed = scrub_held_out([{"score": 1.0, "expected": "leak"}, {"score": 0.5}])
    assert scrubbed == [{"score": 1.0}, {"score": 0.5}]


def test_scrub_held_out_leaves_scalars_and_clean_data_untouched() -> None:
    assert scrub_held_out(None) is None
    assert scrub_held_out(3.14) == 3.14
    assert scrub_held_out({"pass_rate": 0.5}) == {"pass_rate": 0.5}


def test_fmt_metrics_scrubs_held_out_and_formats_compactly() -> None:
    text = fmt_metrics(json.dumps({"pass_rate": 0.9, "expected": "leak"}))
    assert "leak" not in text
    assert "expected" not in text
    assert "pass_rate=0.9" in text


def test_fmt_metrics_handles_empty_and_non_dict() -> None:
    assert fmt_metrics({}) == "_no metrics recorded_"
    assert fmt_metrics(None) == "_no metrics recorded_"
    assert fmt_metrics("not json") == "_no metrics recorded_"


def test_fmt_list_decodes_json_string_and_non_list_returns_empty() -> None:
    assert fmt_list(json.dumps(["a", "b"])) == ["a", "b"]
    assert fmt_list(["x", 1]) == ["x", "1"]
    assert fmt_list("not a list") == []
    assert fmt_list(None) == []


def test_common_value() -> None:
    assert common_value(["coding", "coding", "coding"]) == "coding"
    assert common_value(["coding", "research", None]) is None
    assert common_value([None, None]) is None
    assert common_value([]) is None


def test_latest_timestamp() -> None:
    assert latest_timestamp(["2026-07-11T10:00:00Z", "2026-07-11T14:00:00Z", None]) == (
        "2026-07-11T14:00:00Z"
    )
    assert latest_timestamp([]) is None
    assert latest_timestamp([None, None]) is None
