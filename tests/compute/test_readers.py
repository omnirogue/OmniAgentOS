"""Reader unit tests: honesty semantics first — absence, unreachable, malformed."""

from __future__ import annotations

import json
import re
import urllib.error
from collections.abc import Callable

import pytest

from omniagentos.compute import read_local, read_pool, read_runners
from tests.compute.conftest import (
    POOL_STATUS_SAMPLE_BYTES,
    RUNNERS_SAMPLE,
    RUNNERS_SAMPLE_STDOUT,
    fake_fetch,
    fake_runner,
)

# A reason is operator-facing prose on a gated route; it still must not
# describe local layout or name an exception class (same rule as testobs).
_PATH_FRAGMENT = re.compile(r"(/|\\|\.env\b|\.json\b)")
_CLASS_NAME = re.compile(r"\b\w*(Error|Exception)\b")


def _assert_reason_is_clean(reason: str) -> None:
    assert reason and reason == reason.strip()
    assert not _PATH_FRAGMENT.search(reason), f"reason leaks layout: {reason!r}"
    assert not _CLASS_NAME.search(reason), f"reason leaks an exception class: {reason!r}"


# --------------------------------------------------------------------------- pool


def test_pool_missing_token_is_unavailable(connections_env: Callable[[str], None]) -> None:
    """No connections.env at all -> unavailable, before any fetch is attempted."""
    result = read_pool(fetch=fake_fetch(RuntimeError("must not be called")))
    assert result["available"] is False
    assert set(result) == {"available", "reason"}
    _assert_reason_is_clean(result["reason"])


def test_pool_token_parsed_from_connections_env(connections_env: Callable[[str], None]) -> None:
    connections_env("SOME_OTHER=1\nexport WQ_TOKEN='abc123'\n")
    result = read_pool(fetch=fake_fetch(POOL_STATUS_SAMPLE_BYTES))
    assert result["available"] is True


def test_pool_server_error_is_unavailable() -> None:
    error = urllib.error.HTTPError("http://x", 500, "boom", None, None)
    result = read_pool(fetch=fake_fetch(error), token="tok")
    assert result["available"] is False
    assert set(result) == {"available", "reason"}  # no coerced-zero facts alongside it
    _assert_reason_is_clean(result["reason"])


def test_pool_unreachable_is_unavailable() -> None:
    result = read_pool(fetch=fake_fetch(TimeoutError()), token="tok")
    assert result["available"] is False
    assert set(result) == {"available", "reason"}
    _assert_reason_is_clean(result["reason"])


def test_pool_malformed_response_is_unavailable() -> None:
    result = read_pool(fetch=fake_fetch(b"not json"), token="tok")
    assert result["available"] is False
    assert set(result) == {"available", "reason"}
    _assert_reason_is_clean(result["reason"])

    result2 = read_pool(fetch=fake_fetch(b"[1, 2]"), token="tok")  # valid json, wrong shape
    assert result2["available"] is False
    assert set(result2) == {"available", "reason"}


def test_pool_unexpected_shape_is_unavailable_not_a_healthy_empty_pool() -> None:
    """A 200 with a JSON *object* body is not the same claim as a healthy pool.

    ``{"error": "pool warming up"}`` is exactly the shape a server mid-restart
    answers with -- it must not read as "available, zero machines".
    """
    starting_up = json.dumps({"error": "pool warming up", "ok": False}).encode()
    result = read_pool(fetch=fake_fetch(starting_up), token="tok")
    assert result["available"] is False
    assert set(result) == {"available", "reason"}
    _assert_reason_is_clean(result["reason"])

    # "machines" present but the wrong type is the same lie, not a partial win.
    wrong_type = json.dumps({"machines": "none"}).encode()
    assert read_pool(fetch=fake_fetch(wrong_type), token="tok")["available"] is False

    # depth/capacity/refusals_24h, when present, must be dicts too -- not
    # silently defaulted to {} the way an ABSENT one honestly is.
    bad_depth = json.dumps({"machines": [], "depth": [1, 2]}).encode()
    assert read_pool(fetch=fake_fetch(bad_depth), token="tok")["available"] is False
    bad_capacity = json.dumps({"machines": [], "capacity": "full"}).encode()
    assert read_pool(fetch=fake_fetch(bad_capacity), token="tok")["available"] is False
    bad_refusals = json.dumps({"machines": [], "refusals_24h": [1]}).encode()
    refusals_result = read_pool(fetch=fake_fetch(bad_refusals), token="tok")
    assert refusals_result["available"] is False
    assert set(refusals_result) == {"available", "reason"}

    # A genuinely empty pool (machines really is []) is still a real success.
    really_empty = json.dumps({"machines": []}).encode()
    empty_result = read_pool(fetch=fake_fetch(really_empty), token="tok")
    assert empty_result["available"] is True and empty_result["machines"] == []
    assert empty_result["refusals_24h"] == {}  # absent is honestly {}, not invalid


def test_pool_machines_list_of_non_objects_is_unavailable_not_zero_machines() -> None:
    """A list of non-objects is not "zero machines" -- it invalidates the shape.

    Silently filtering non-dict entries out would let ``[1, 2, 3]`` (or any
    other malformed payload shaped like a list) read as a healthy empty fleet,
    the same favourable-absence bug as an outright missing ``machines`` key.
    """
    garbage = json.dumps({"machines": [1, 2, 3]}).encode()
    result = read_pool(fetch=fake_fetch(garbage), token="tok")
    assert result["available"] is False
    assert set(result) == {"available", "reason"}
    _assert_reason_is_clean(result["reason"])

    # Even ONE non-dict entry among otherwise-valid machines invalidates it --
    # a partially-corrupt list is not "the valid entries, minus the bad one".
    mixed = json.dumps({"machines": [{"machine_id": "x"}, 5]}).encode()
    assert read_pool(fetch=fake_fetch(mixed), token="tok")["available"] is False


def test_pool_token_used_for_auth_but_never_leaked_in_the_result() -> None:
    """The bearer token reaches the request, but never rides back out in JSON."""
    token = "wq-secret-abc123"
    captured: dict[str, str] = {}

    def fetch(url: str, headers: dict[str, str], timeout: float) -> bytes:
        captured["auth"] = headers.get("Authorization", "")
        return POOL_STATUS_SAMPLE_BYTES

    result = read_pool(fetch=fetch, token=token)
    assert captured["auth"] == f"Bearer {token}"
    assert token not in json.dumps(result)

    # And on the failure path -- an absent envelope must carry no stray key
    # a token (or anything else) could have been coerced into.
    error = read_pool(fetch=fake_fetch(TimeoutError()), token=token)
    assert set(error) == {"available", "reason"}
    assert token not in json.dumps(error)


def test_pool_happy_path_parses_the_real_status_shape() -> None:
    result = read_pool(fetch=fake_fetch(POOL_STATUS_SAMPLE_BYTES), token="tok")
    assert result["available"] is True and result["reason"] is None
    assert len(result["machines"]) == 2

    mac = next(m for m in result["machines"] if m["machine_id"] == "macstudio-b")
    assert mac["hostname"] == "macstudio-b.local"
    assert mac["os"] == "darwin"
    assert mac["labels"] == ["darwin", "build", "pytest", "script"]
    assert mac["max_concurrent"] == 3
    assert mac["in_flight"] == 1
    assert mac["drain"] == 0
    assert mac["last_seen_at"] == "2026-08-14T20:07:09Z"
    assert mac["last_seen_age_s"] == 0.0
    assert mac["ncpu"] == 16 and mac["perf_cores"] == 12
    assert mac["mem_gb"] == 128.0 and mac["mem_free_gb"] == 94.44
    assert mac["load1"] == 4.58 and mac["load5"] == 5.18
    assert mac["done_1h"] == 0 and mac["done_6h"] == 2
    # server-side computed: load1 / ncpu, rounded to 2
    assert mac["load_ratio"] == round(4.58 / 16, 2)
    # Fields NOT in the brief's exhaustive per-machine list are not relayed.
    assert "ceiling_fraction" not in mac and "attempts_per_completion" not in mac

    assert result["depth"] == {
        "queued": 0, "claimed": 0, "running": 0, "review": 0,
        "done": 27, "parked": 12, "cancelled": 37,
    }
    assert result["capacity"]["total_slots"] == 16 and result["capacity"]["free_slots"] == 16
    assert result["refusals_24h"]["total"] == 43


def test_pool_load_ratio_is_none_when_ncpu_missing_or_zero() -> None:
    machine = {"machine_id": "x", "load1": 2.0, "ncpu": 0}
    payload = {"machines": [machine], "depth": {}, "capacity": {}, "refusals_24h": {}}
    result = read_pool(fetch=fake_fetch(json.dumps(payload).encode()), token="tok")
    assert result["machines"][0]["load_ratio"] is None


# --------------------------------------------------------------------------- runners


def test_runners_no_gh_binary_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("omniagentos.compute.readers._resolve_gh", lambda: None)
    result = read_runners(runner=fake_runner(raises=RuntimeError("must not be called")))
    assert result["available"] is False
    assert set(result) == {"available", "reason"}
    _assert_reason_is_clean(result["reason"])


def test_runners_gh_failure_is_unavailable_never_500(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("omniagentos.compute.readers._resolve_gh", lambda: "/usr/bin/gh")
    for kwargs in (
        {"returncode": 1, "stdout": ""},  # auth / rate limit / any gh CLI error
        {"raises": TimeoutError()},  # subprocess timeout
        {"stdout": "not json"},  # malformed stdout
        {"stdout": '{"not": "a list"}'},  # wrong shape
    ):
        result = read_runners(runner=fake_runner(**kwargs))
        assert result["available"] is False
        assert set(result) == {"available", "reason"}
        _assert_reason_is_clean(result["reason"])


def test_runners_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("omniagentos.compute.readers._resolve_gh", lambda: "/usr/bin/gh")
    result = read_runners(runner=fake_runner(stdout=RUNNERS_SAMPLE_STDOUT))
    assert result["available"] is True and result["reason"] is None
    assert result["runners"] == RUNNERS_SAMPLE


def test_runners_list_of_non_objects_is_unavailable_not_an_empty_fleet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BACKEND-5: malformed gh output must refuse, never read as zero runners."""
    monkeypatch.setattr("omniagentos.compute.readers._resolve_gh", lambda: "/usr/bin/gh")
    for stdout in ("[1, 2, 3]", '[{"name": "ok"}, 5]'):
        result = read_runners(runner=fake_runner(stdout=stdout))
        assert result["available"] is False
        assert set(result) == {"available", "reason"}


# --------------------------------------------------------------------------- local


def test_local_is_always_available() -> None:
    result = read_local()
    assert result["available"] is True and result["reason"] is None
    assert isinstance(result["hostname"], str) and result["hostname"]
    assert isinstance(result["load1"], float)
    assert isinstance(result["ncpu"], int) and result["ncpu"] > 0
    assert result["load_ratio"] == round(result["load1"] / result["ncpu"], 2)
