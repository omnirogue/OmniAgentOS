"""Every row of the SPEC §4.4b signature table, plus the no-match case.

The table's whole job is to keep an instrument fault out of the defect count.
Each case below asserts the three things the store branches on: the outcome, the
retryability (0 == terminal, do not re-run), and that a remedy exists and names
the instrument rather than the code.
"""

from __future__ import annotations

import pytest

from omniagentos.workqueue.classify import classify_log, classify_start_load

# (log line, expected outcome, expected retryable)
ROWS = [
    ("openai: quota exceeded for this org", "environment", 1),
    ("Error: quota exhausted", "environment", 1),
    ("HTTP 429 Too Many Requests", "environment", 1),
    ("Error: rate limit exceeded", "environment", 1),
    ("provider said rate-limit reached", "environment", 1),
    ('{"error": {"code": "insufficient_quota"}}', "environment", 1),
    ("HTTP 401 Unauthorized", "environment", 0),
    ("returned 403 from the API", "environment", 0),
    ("invalid_api_key supplied", "environment", 0),
    ("authentication failed for user", "environment", 0),
    ("this account is suspended", "environment", 0),
    ("sqlite3.OperationalError: database is locked", "instrument-error", 1),
    ("disk I/O error while writing", "instrument-error", 1),
    ("OSError: [Errno 28] No space left on device", "instrument-error", 1),
    ("ssh: connect to host mw0001-owner port 22: Operation timed out", "environment", 1),
    ("Connection refused", "environment", 1),
    ("Could not resolve host: github.com", "environment", 1),
    ("error: .git/index.lock: Operation not permitted", "instrument-error", 1),
]


@pytest.mark.parametrize(("text", "outcome", "retryable"), ROWS)
def test_signature_rows(text: str, outcome: str, retryable: int) -> None:
    signature = classify_log(f"...\nsome preamble\n{text}\nsome trailer\n")
    assert signature is not None, f"unmatched §4.4b signature: {text!r}"
    assert signature.outcome == outcome
    assert signature.retryable == retryable
    assert signature.remedy and len(signature.remedy) > 20


def test_auth_wins_over_quota_when_both_appear() -> None:
    """Ordering is load-bearing: a 429 storm that ended in a 401 must PARK.

    Retrying three times against a dead credential is the failure mode this
    ordering exists to stop; the auth row is terminal at the first occurrence.
    """
    signature = classify_log("429 rate limit\n...\nHTTP 401 Unauthorized")
    assert signature is not None
    assert signature.retryable == 0


@pytest.mark.parametrize(
    "text",
    [
        "",
        "3 passed, 0 failed in 1.2s",
        "AssertionError: expected 4, got 5",
        "Operation not permitted",  # NOT under .git/ — ordinary noise, not an instrument fact
    ],
)
def test_no_match_leaves_the_exit_code_alone(text: str) -> None:
    assert classify_log(text) is None


def test_none_log_is_not_a_match() -> None:
    assert classify_log(None) is None


def test_loadavg_over_four_times_perf_cores_is_contention() -> None:
    signature = classify_start_load(70.0, 16)
    assert signature is not None
    assert signature.outcome == "contention-flake"
    assert signature.retryable == 1
    assert classify_start_load(3.0, 16) is None


@pytest.mark.parametrize(("load1", "perf"), [(None, 16), (70.0, None), (70.0, 0)])
def test_unknown_box_state_is_never_a_free_reclassification(load1, perf) -> None:
    """Favourable absence: an unknown load is not a busy box."""
    assert classify_start_load(load1, perf) is None
