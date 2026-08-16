"""Re-enqueueing the same idempotency_key is a no-op, not a duplicate unit."""

from __future__ import annotations

import pytest

from tests.workqueue.conftest import submit


def test_same_key_dedupes_to_the_same_unit(store):
    first_id, first_dedup = store.enqueue(submit("demo-1"))
    second_id, second_dedup = store.enqueue(submit("demo-1", brief_inline="changed my mind"))

    assert first_dedup is False
    assert second_dedup is True
    assert second_id == first_id
    # The second call must not have rewritten the unit: dedupe means "already
    # queued", not "update in place".
    assert store.get_unit(first_id)["brief_inline"] == "do the thing"

    depth = store.status()["depth"]
    assert depth["queued"] == 1


def test_distinct_keys_make_distinct_units(store):
    a, _ = store.enqueue(submit("demo-a"))
    b, _ = store.enqueue(submit("demo-b"))
    assert a != b
    assert store.status()["depth"]["queued"] == 2


def test_owned_paths_and_labels_round_trip_as_lists(store):
    unit_id, _ = store.enqueue(
        submit("demo-lists", owned_paths=["a/**", "b/**"], labels=["build", "pytest"])
    )
    unit = store.get_unit(unit_id)
    assert unit["owned_paths"] == ["a/**", "b/**"]
    assert unit["labels"] == ["build", "pytest"]


def test_base_sha_must_be_a_sha_not_a_ref(store):
    # A branch name here silently destroys the §4 input fingerprint: two
    # machines resolving 'main' 90 seconds apart build different trees and the
    # key means nothing. Refuse at enqueue, where it is cheap.
    with pytest.raises(ValueError, match="40-hex"):
        store.enqueue(submit("demo-ref", base_sha="main"))


def test_missing_required_field_is_refused(store):
    payload = submit("demo-missing")
    del payload["acceptance_cmd"]
    with pytest.raises(ValueError, match="acceptance_cmd"):
        store.enqueue(payload)
