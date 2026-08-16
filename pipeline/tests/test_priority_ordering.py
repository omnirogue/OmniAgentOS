"""Priority-aware claim/publish ordering (feat/queue-priority-0809).

CONTRACT.md §3 / §6: envelopes carry an OPTIONAL `priority` (0=bottleneck,
1=fix, 2=normal/default, 3=background); `state/queue.json`'s `items` list —
the thing a claimant reads to decide what to work on next — is ordered by
that priority, then by age, with anti-starvation aging so an item does not
wait forever behind a stream of fresher higher-priority arrivals.

This is ONLY a selection/presentation-order change: it does not touch the
O_EXCL claim arbiter in bridge/claim.py, which is exercised separately by
tests/test_claim.py and left byte-for-byte here.

Three things pinned, matching the brief:
  (a) a priority-1 item sorts before a priority-2 item
  (b) aging floats an old priority-2 item above a fresh priority-2 item
  (c) a legacy envelope with NO `priority` field still validates and is
      treated as priority 2 (both by the schema and by the queue rebuild)
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PKG))

from bridge import integration as I  # noqa: E402
from bridge import validate_envelope as V  # noqa: E402

assert Path(I.__file__).resolve() == (PKG / "bridge" / "integration.py").resolve()


@pytest.fixture
def root(tmp_path: Path) -> Path:
    r = tmp_path / "loopqueue"
    for sub in ("proposals", "candidates", "findings", "inquiries", "rejected", "parked", "state"):
        (r / sub).mkdir(parents=True)
    return r


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_finding(root: Path, ident: str, *, priority: int | None, created_at: str,
                    title: str) -> None:
    body: dict = {
        "id": ident,
        "kind": "finding",
        "title": title,
        "created_at": created_at,
        "producer": {"role": "external"},
        "payload": {"symptom": "smoke test symptom"},
    }
    if priority is not None:
        body["priority"] = priority
    stem = ident.replace(":", "_", 1)
    (root / "findings" / f"{stem}.json").write_text(json.dumps(body), encoding="utf-8")


def _write_ledger(root: Path, lines: list[str]) -> None:
    (root / "ledger.jsonl").write_text("\n".join(lines) + ("\n" if lines else ""),
                                        encoding="utf-8")


def _found_ev(ident: str, title: str, ts: str) -> str:
    return json.dumps({"ts": ts, "role": "planning", "event": "found", "id": ident,
                       "detail": {"title": title}})


def _rebuild(root: Path) -> dict:
    ledger = I.LedgerView.build(root)
    ledger.kinds.update(I.kinds_from_disk(root))
    return I.rebuild_queue(root, ledger, wip_cap=4)


# --------------------------------------------------------------------------
# (a) priority 1 sorts before priority 2
# --------------------------------------------------------------------------

def test_priority_1_selected_before_priority_2(root: Path) -> None:
    now = datetime.now(UTC)
    ts = _iso(now)
    _write_finding(root, "sha256:" + "a" * 64, priority=2, title="normal item", created_at=ts)
    _write_finding(root, "sha256:" + "b" * 64, priority=1, title="fix item", created_at=ts)
    _write_ledger(root, [
        _found_ev("sha256:" + "a" * 64, "normal item", ts),
        _found_ev("sha256:" + "b" * 64, "fix item", ts),
    ])

    q = _rebuild(root)
    order = [i["id"] for i in q["items"]]
    assert order.index("sha256:" + "b" * 64) < order.index("sha256:" + "a" * 64)
    by_id = {i["id"]: i for i in q["items"]}
    assert by_id["sha256:" + "b" * 64]["priority"] == 1
    assert by_id["sha256:" + "a" * 64]["priority"] == 2


# --------------------------------------------------------------------------
# (b) anti-starvation aging floats an OLD normal item above a FRESH one
# --------------------------------------------------------------------------

def test_aging_floats_old_normal_above_fresh_normal(root: Path) -> None:
    now = datetime.now(UTC)
    old_ts = _iso(now - timedelta(seconds=I.PRIORITY_AGING_PROMOTE_AFTER_SECONDS + 3600))
    fresh_ts = _iso(now)

    old_id = "sha256:" + "c" * 64
    fresh_id = "sha256:" + "d" * 64
    _write_finding(root, old_id, priority=2, title="old normal", created_at=old_ts)
    _write_finding(root, fresh_id, priority=2, title="fresh normal", created_at=fresh_ts)
    _write_ledger(root, [
        _found_ev(old_id, "old normal", old_ts),
        _found_ev(fresh_id, "fresh normal", fresh_ts),
    ])

    q = _rebuild(root)
    order = [i["id"] for i in q["items"]]
    assert order.index(old_id) < order.index(fresh_id)
    by_id = {i["id"]: i for i in q["items"]}
    assert by_id[old_id]["effective_priority"] == 1  # aged one full day, one capped promotion
    assert by_id[fresh_id]["effective_priority"] == 2  # unaged

    # Even after its one promotion, an old normal item cannot overtake fresh
    # P0 work: P2 ages only to P1, never to P0.
    fix_id = "sha256:" + "e" * 64
    _write_finding(root, fix_id, priority=0, title="fresh P0", created_at=fresh_ts)
    _write_ledger(root, [
        _found_ev(old_id, "old normal", old_ts),
        _found_ev(fresh_id, "fresh normal", fresh_ts),
        _found_ev(fix_id, "fresh fix", fresh_ts),
    ])
    q2 = _rebuild(root)
    by_id2 = {i["id"]: i for i in q2["items"]}
    assert by_id2[old_id]["effective_priority"] == 1
    assert by_id2[fix_id]["effective_priority"] == 0
    order2 = [i["id"] for i in q2["items"]]
    assert order2.index(fix_id) < order2.index(old_id) < order2.index(fresh_id)


def test_fresh_p0_sorts_before_45_minute_old_p2(root: Path) -> None:
    now = datetime.now(UTC)
    fresh_ts = _iso(now)
    aged_ts = _iso(now - timedelta(minutes=45))
    p0_id = "sha256:" + "0" * 64
    p2_id = "sha256:" + "2" * 64
    _write_finding(root, p0_id, priority=0, title="fresh P0", created_at=fresh_ts)
    _write_finding(root, p2_id, priority=2, title="45-minute P2", created_at=aged_ts)
    _write_ledger(root, [_found_ev(p0_id, "fresh P0", fresh_ts),
                         _found_ev(p2_id, "45-minute P2", aged_ts)])

    q = _rebuild(root)
    by_id = {item["id"]: item for item in q["items"]}
    assert by_id[p0_id]["effective_priority"] == 0
    assert by_id[p2_id]["effective_priority"] == 2
    assert [item["id"] for item in q["items"]] == [p0_id, p2_id]


def test_p2_gets_one_promotion_after_a_day_and_never_more() -> None:
    now = datetime(2026, 8, 10, tzinfo=UTC)
    after_threshold = _iso(now - timedelta(seconds=I.PRIORITY_AGING_PROMOTE_AFTER_SECONDS + 3600))
    thirty_days_old = _iso(now - timedelta(days=30))

    assert I.effective_priority(2, after_threshold, now=now) == 1
    assert I.effective_priority(2, thirty_days_old, now=now) == 1


def test_within_effective_priority_band_oldest_known_age_sorts_first(root: Path) -> None:
    now = datetime.now(UTC)
    older_ts = _iso(now - timedelta(hours=2))
    fresh_ts = _iso(now)
    old_id = "sha256:" + "z" * 64
    fresh_id = "sha256:" + "a" * 64
    unknown_id = "sha256:" + "0" * 64
    _write_finding(root, old_id, priority=2, title="older P2", created_at=older_ts)
    _write_finding(root, fresh_id, priority=2, title="fresh P2", created_at=fresh_ts)
    _write_finding(root, unknown_id, priority=2, title="unknown-age P2", created_at="not-a-date")
    _write_ledger(root, [_found_ev(old_id, "older P2", older_ts),
                         _found_ev(fresh_id, "fresh P2", fresh_ts),
                         _found_ev(unknown_id, "unknown-age P2", fresh_ts)])

    q = _rebuild(root)
    assert [item["id"] for item in q["items"]] == [old_id, fresh_id, unknown_id]


def test_mixed_age_queue_order_is_byte_deterministic(root: Path) -> None:
    now = datetime.now(UTC)
    common_ts = _iso(now - timedelta(hours=3))
    fixture = [
        ("sha256:" + "f" * 64, 2, _iso(now - timedelta(minutes=45)), "freshish P2"),
        ("sha256:" + "a" * 64, 0, _iso(now - timedelta(minutes=1)), "P0"),
        ("sha256:" + "e" * 64, 2, _iso(now - timedelta(days=2)), "aged P2"),
        ("sha256:" + "d" * 64, 1, common_ts, "P1 same timestamp later id"),
        ("sha256:" + "c" * 64, 1, common_ts, "P1 same timestamp earlier id"),
        ("sha256:" + "b" * 64, 3, _iso(now - timedelta(days=3)), "aged P3"),
    ]
    for ident, priority, created_at, title in fixture:
        _write_finding(root, ident, priority=priority, title=title, created_at=created_at)
    _write_ledger(root, [_found_ev(ident, title, created_at)
                         for ident, _priority, created_at, title in fixture])

    first = _rebuild(root)
    second = _rebuild(root)
    assert json.dumps(first["items"], separators=(",", ":")) == json.dumps(
        second["items"], separators=(",", ":")
    )
    order = [item["id"] for item in first["items"]]
    assert order.index("sha256:" + "c" * 64) < order.index("sha256:" + "d" * 64)


def test_new_formula_preserves_priority_where_old_formula_collapsed_it(root: Path) -> None:
    now = datetime.now(UTC)
    six_hours_old = _iso(now - timedelta(hours=6))
    fresh_ts = _iso(now)
    p0_id = "sha256:" + "0" * 64
    p2_id = "sha256:" + "2" * 64
    _write_finding(root, p0_id, priority=0, title="fresh P0", created_at=fresh_ts)
    _write_finding(root, p2_id, priority=2, title="six-hour P2", created_at=six_hours_old)
    _write_ledger(root, [_found_ev(p0_id, "fresh P0", fresh_ts),
                         _found_ev(p2_id, "six-hour P2", six_hours_old)])

    def old_effective_priority(priority: int, created_at: str) -> int:
        when = I._parse(created_at)
        assert when is not None
        age_s = max(0.0, (now - when).total_seconds())
        return max(0, priority - int(age_s // 900))

    assert old_effective_priority(2, six_hours_old) == 0
    queue = _rebuild(root)
    by_id = {item["id"]: item for item in queue["items"]}
    assert by_id[p0_id]["effective_priority"] == 0
    assert by_id[p2_id]["effective_priority"] == 2


@pytest.mark.parametrize("p2_age", [
    timedelta(0), timedelta(hours=1), timedelta(hours=6), timedelta(hours=23),
    timedelta(hours=24), timedelta(hours=25), timedelta(hours=100), timedelta(days=365),
])
def test_fresh_p0_always_sorts_ahead_of_p2_at_any_age(p2_age: timedelta) -> None:
    now = datetime(2026, 8, 10, tzinfo=UTC)
    fresh_p0 = I.effective_priority(0, _iso(now), now=now)
    aged_p2 = I.effective_priority(2, _iso(now - p2_age), now=now)
    assert fresh_p0 < aged_p2


# --------------------------------------------------------------------------
# (c) legacy (no priority field) envelopes still validate and act as 2
# --------------------------------------------------------------------------

def test_legacy_envelope_without_priority_still_validates(root: Path) -> None:
    ident = "sha256:" + "f" * 64
    ts = _iso(datetime.now(UTC))
    envelope = {
        "id": ident,
        "kind": "finding",
        "title": "legacy finding, no priority field",
        "created_at": ts,
        "producer": {"role": "external"},
        "payload": {"symptom": "legacy smoke"},
    }
    validate, _note = V._build_validator()
    validate(envelope)  # must not raise -- absence of `priority` is valid


def test_legacy_envelope_without_priority_processes_as_2(root: Path) -> None:
    ident = "sha256:" + "9" * 64
    ts = _iso(datetime.now(UTC))
    _write_finding(root, ident, priority=None, title="legacy finding", created_at=ts)
    _write_ledger(root, [_found_ev(ident, "legacy finding", ts)])

    q = _rebuild(root)
    by_id = {i["id"]: i for i in q["items"]}
    assert by_id[ident]["priority"] == 2
    assert by_id[ident]["effective_priority"] == 2


def test_priority_out_of_range_fails_schema_validation() -> None:
    ident = "sha256:" + "0" * 64
    envelope = {
        "id": ident,
        "kind": "finding",
        "title": "bad priority",
        "created_at": "2026-08-09T00:00:00Z",
        "priority": 4,
        "producer": {"role": "external"},
        "payload": {"symptom": "out of range"},
    }
    import jsonschema
    validate, _note = V._build_validator()
    with pytest.raises(jsonschema.ValidationError):
        validate(envelope)
