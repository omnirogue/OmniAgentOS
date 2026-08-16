from __future__ import annotations

import json
import subprocess

import pytest

from omniagentos.team.ingest import IngestUnavailable, iter_prs


def test_iter_prs_maps_merged_and_closed_quality_gates(monkeypatch) -> None:
    payload = [
        {
            "number": 9,
            "title": "Still open",
            "body": "refs U3",
            "author": {"login": "example-org"},
            "createdAt": "2026-08-09T00:00:00Z",
            "mergedAt": None,
            "closedAt": None,
            "state": "OPEN",
            "headRefName": "feature/U3-open",
            "headRefOid": "aaa",
        },
        {
            "number": 10,
            "title": "Merged",
            "body": "refs U3",
            "author": {"login": "example-org"},
            "createdAt": "2026-08-01T00:00:00Z",
            "mergedAt": "2026-08-09T00:00:00Z",
            "closedAt": "2026-08-09T00:00:00Z",
            "state": "MERGED",
            "headRefName": "feature/U3",
            "headRefOid": "abc",
        },
        {
            "number": 11,
            "title": "Closed",
            "body": "",
            "author": {"login": "alice-dev"},
            "createdAt": "2026-08-02T00:00:00Z",
            "mergedAt": None,
            "closedAt": "2026-08-09T01:00:00Z",
            "state": "CLOSED",
            "headRefName": "feature/closed",
            "headRefOid": "def",
        },
    ]
    results = iter(
        [
            subprocess.CompletedProcess([], 0, stdout="example-org\n", stderr=""),
            subprocess.CompletedProcess([], 0, stdout=json.dumps(payload), stderr=""),
        ]
    )
    calls: list[list[str]] = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        return next(results)

    monkeypatch.setattr("omniagentos.team.ingest.subprocess.run", fake_run)
    prs = list(iter_prs("org/repo", "2026-08-05T00:00:00Z"))
    assert [pr["quality_gate"] for pr in prs] == ["pass", "rejected"]
    assert [pr["occurred_at"] for pr in prs] == [
        "2026-08-09T00:00:00Z",
        "2026-08-09T01:00:00Z",
    ]
    assert prs[0]["ref"] == "org/repo#10"
    assert prs[0]["meta"]["body"] == "refs U3"
    assert len(calls) == 2


def test_iter_prs_stops_after_failed_preflight(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="not logged in")

    monkeypatch.setattr("omniagentos.team.ingest.subprocess.run", fake_run)
    with pytest.raises(IngestUnavailable, match="gh preflight failed"):
        list(iter_prs("org/repo", "2026-08-05T00:00:00Z"))
    assert len(calls) == 1
    assert calls[0][:3] == ["gh", "api", "user"]


def test_iter_prs_wraps_preflight_oserror(monkeypatch) -> None:
    def unavailable(*_args, **_kwargs):
        raise OSError("gh executable missing")

    monkeypatch.setattr("omniagentos.team.ingest.subprocess.run", unavailable)
    with pytest.raises(IngestUnavailable, match="gh preflight failed.*gh executable missing"):
        list(iter_prs("org/repo", "2026-08-05T00:00:00Z"))
