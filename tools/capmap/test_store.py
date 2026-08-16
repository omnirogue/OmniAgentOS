import datetime
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import store  # noqa: E402


def cap(verification, semantics, **extra):
    value = {"verification": verification, "exit_semantics": semantics, "staleness_slo_seconds": 20}
    value.update(extra)
    return value


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="capmap-store-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_command_exit_semantics_map_all_four_statuses(self):
        capability = cap({"type": "command"}, {"0": "ok", "3": "degraded", "2": "cannot_evaluate", "1": "down"})
        self.assertEqual(store.compute_status(capability, 0), store.OK)
        self.assertEqual(store.compute_status(capability, 3), store.DEGRADED)
        self.assertEqual(store.compute_status(capability, 2), store.CANNOT_EVALUATE)
        self.assertEqual(store.compute_status(capability, 1), store.DOWN)

    def test_snapshot_semantics_map_all_four_statuses(self):
        capability = cap({"type": "snapshot_field"}, {"UP": "ok", "SUSPENDED": "degraded", "NO-CRED": "cannot_evaluate", "DOWN": "down"})
        self.assertEqual(store.compute_status(capability, "UP"), store.OK)
        self.assertEqual(store.compute_status(capability, "suspended"), store.DEGRADED)
        self.assertEqual(store.compute_status(capability, "NO-CRED"), store.CANNOT_EVALUATE)
        self.assertEqual(store.compute_status(capability, "DOWN"), store.DOWN)

    def test_unset_or_unmapped_never_becomes_ok(self):
        self.assertEqual(store.compute_status(cap({"type": "unset"}, {}), 0), store.UNVERIFIED)
        self.assertEqual(store.compute_status({"exit_semantics": {"0": "ok"}}, 0), store.UNVERIFIED)
        self.assertEqual(store.compute_status(cap({"type": "command"}, {"0": "ok"}), 77), store.UNVERIFIED)

    def test_stale_uses_absolute_staleness_slo_threshold(self):
        now = datetime.datetime(2026, 8, 15, 12, 1, 1, tzinfo=datetime.timezone.utc)
        capability = cap({"type": "command"}, {"0": "ok"}, staleness_slo_seconds=60)
        self.assertTrue(store.is_stale(capability, "2026-08-15T12:00:00Z", now))
        self.assertFalse(store.is_stale(capability, "2026-08-15T12:00:01Z", now))

    def test_run_history_appends_and_snapshot_leaves_no_tmp(self):
        first = {"ts": "2026-08-15T12:00:00Z", "capability_id": "a", "exit_code": 0,
                 "status": store.OK, "latency_ms": 1, "metric": {}, "evidence": None}
        second = dict(first, ts="2026-08-15T12:01:00Z", capability_id="b")
        store.append_run(first, self.tmp)
        store.append_run(second, self.tmp)
        runs_path = os.path.join(self.tmp, "runs.jsonl")
        with open(runs_path) as fh:
            self.assertEqual(len([line for line in fh if line.strip()]), 2)
        store.write_snapshot({"a": {"status": store.OK}}, self.tmp)
        self.assertEqual(store.read_snapshot(self.tmp)["a"]["status"], store.OK)
        self.assertEqual([name for name in os.listdir(self.tmp) if ".tmp" in name], [])
