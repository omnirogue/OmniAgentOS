"""Tests for the LLM-loop throughput registry.

Every test is hermetic: fixtures and in-memory dicts, never a live loop, a live
database or an SSH host.  The behaviours pinned here are the ones such a
harness must not get wrong — a running-but-idle loop reading OK, and a floor
invented out of history that does not exist.
"""

import datetime
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import loops  # noqa: E402

NOW = datetime.datetime(2026, 8, 15, 12, 0, 0, tzinfo=datetime.timezone.utc)


def _series(values, now=NOW):
    """Map the last ``len(values)`` days (ending yesterday) to *values*."""
    days = [(now - datetime.timedelta(days=offset)).strftime("%Y-%m-%d")
            for offset in range(1, len(values) + 1)]
    return dict(zip(days, values))


class TestDeriveFloor(unittest.TestCase):
    def test_floor_is_thirty_percent_of_trailing_median(self):
        # median of 100,200,300,400,500,600,700 is 400 -> floor 120.
        derived = loops.derive_floor(_series([100, 200, 300, 400, 500, 600, 700]), now=NOW)
        self.assertEqual(derived["median"], 400)
        self.assertEqual(derived["floor"], 120.0)
        self.assertEqual(derived["fraction"], 0.3)
        self.assertEqual(derived["sample_days"], 7)

    def test_floor_records_the_sample_it_came_from(self):
        derived = loops.derive_floor(_series([10, 20, 30, 40, 50, 60, 70]), now=NOW)
        self.assertEqual(derived["sample_days"], 7)
        self.assertEqual(derived["sample_window"], "2026-08-08..2026-08-14")
        self.assertEqual(sum(derived["sample"].values()), 280)
        self.assertIsNone(derived["reason"])

    def test_a_realistic_implementer_series_derives_the_documented_floor(self):
        # A realistic seven-day dispatcher series.
        derived = loops.derive_floor(
            _series([1300, 1450, 800, 1350, 700, 720, 600]), now=NOW)
        self.assertEqual(derived["median"], 800)
        self.assertEqual(derived["floor"], 240.0)

    def test_insufficient_history_returns_a_null_floor_and_says_why(self):
        derived = loops.derive_floor(_series([5, 5, 5]), now=NOW)
        self.assertIsNone(derived["floor"])
        self.assertIsNone(derived["median"])
        self.assertEqual(derived["sample_days"], 3)
        self.assertIn("insufficient history", derived["reason"])

    def test_absent_days_are_not_imputed_as_zero(self):
        # Seven covered days inside a 30-day window: the 23 uncovered days must
        # not be padded with zeros, which would drag the median to 0 and
        # manufacture a floor of 0 that no dead loop could ever fail.
        derived = loops.derive_floor(_series([100] * 7), window_days=30, now=NOW)
        self.assertEqual(derived["sample_days"], 7)
        self.assertEqual(derived["median"], 100)
        self.assertEqual(derived["floor"], 30.0)

    def test_today_is_excluded_from_the_sample(self):
        counts = _series([100] * 7)
        counts[NOW.strftime("%Y-%m-%d")] = 1  # partial day
        derived = loops.derive_floor(counts, now=NOW)
        self.assertEqual(derived["sample_days"], 7)
        self.assertEqual(derived["median"], 100)

    def test_empty_history_is_a_null_floor_not_a_zero_floor(self):
        derived = loops.derive_floor({}, now=NOW)
        self.assertIsNone(derived["floor"])
        self.assertEqual(derived["sample_days"], 0)


class TestResolve(unittest.TestCase):
    def test_running_with_zero_volume_is_degraded_never_ok(self):
        verdict = loops.resolve(process_state=loops.RUNNING, volume=0, floor=120.0)
        self.assertEqual(verdict["status"], loops.DEGRADED)
        self.assertNotEqual(verdict["status"], loops.OK)
        self.assertIn(loops.R_ZERO_VOLUME, verdict["reasons"])

    def test_running_with_zero_volume_and_no_floor_is_still_degraded(self):
        # A null floor must not launder a zero count into UNVERIFIED.
        verdict = loops.resolve(process_state=loops.RUNNING, volume=0, floor=None)
        self.assertEqual(verdict["status"], loops.DEGRADED)
        self.assertIn(loops.R_ZERO_VOLUME, verdict["reasons"])
        self.assertIn(loops.R_NO_FLOOR, verdict["reasons"])

    def test_volume_above_floor_is_ok(self):
        verdict = loops.resolve(process_state=loops.RUNNING, volume=1000, floor=240.0)
        self.assertEqual(verdict["status"], loops.OK)
        self.assertIn(loops.R_ABOVE_FLOOR, verdict["reasons"])

    def test_volume_exactly_at_floor_is_ok(self):
        verdict = loops.resolve(process_state=loops.RUNNING, volume=120, floor=120.0)
        self.assertEqual(verdict["status"], loops.OK)

    def test_volume_below_floor_is_degraded(self):
        verdict = loops.resolve(process_state=loops.RUNNING, volume=12, floor=240.0)
        self.assertEqual(verdict["status"], loops.DEGRADED)
        self.assertIn(loops.R_BELOW_FLOOR, verdict["reasons"])

    def test_missing_history_with_positive_volume_is_unverified_with_null_floor(self):
        verdict = loops.resolve(process_state=loops.RUNNING, volume=46, floor=None)
        self.assertEqual(verdict["status"], loops.UNVERIFIED)
        self.assertIsNone(verdict["dimensions"]["floor"])
        self.assertIn(loops.R_NO_FLOOR, verdict["reasons"])

    def test_no_observed_count_is_unverified_not_ok_and_not_degraded(self):
        # An untimestamped log (edc-triage, team-dispatch) cannot be windowed.
        verdict = loops.resolve(process_state=loops.RUNNING, volume=None, floor=None)
        self.assertEqual(verdict["status"], loops.UNVERIFIED)
        self.assertIn(loops.R_NO_SIGNAL, verdict["reasons"])

    def test_not_loaded_is_down(self):
        verdict = loops.resolve(process_state=loops.NOT_LOADED, volume=500, floor=10.0)
        self.assertEqual(verdict["status"], loops.DOWN)
        self.assertIn(loops.R_NOT_LOADED, verdict["reasons"])

    def test_not_loaded_beats_healthy_volume(self):
        # Process state has priority: a plist that is not loaded is an outage
        # even if the volume table still shows yesterday's throughput.
        verdict = loops.resolve(process_state=loops.NOT_LOADED, volume=99999, floor=1.0)
        self.assertEqual(verdict["status"], loops.DOWN)

    def test_stopped_is_down(self):
        self.assertEqual(
            loops.resolve(process_state=loops.STOPPED, volume=0, floor=None)["status"],
            loops.DOWN)

    def test_error_rate_above_threshold_degrades_a_busy_loop(self):
        verdict = loops.resolve(process_state=loops.RUNNING, volume=1000, floor=10.0,
                                error_rate=0.47, max_error_rate=0.25)
        self.assertEqual(verdict["status"], loops.DEGRADED)
        self.assertIn(loops.R_ERROR_RATE, verdict["reasons"])

    def test_error_rate_below_threshold_leaves_a_busy_loop_ok(self):
        verdict = loops.resolve(process_state=loops.RUNNING, volume=1000, floor=10.0,
                                error_rate=0.047, max_error_rate=0.25)
        self.assertEqual(verdict["status"], loops.OK)

    def test_unknown_error_rate_does_not_degrade(self):
        verdict = loops.resolve(process_state=loops.RUNNING, volume=1000, floor=10.0,
                                error_rate=None)
        self.assertEqual(verdict["status"], loops.OK)

    def test_absent_signal_is_unverified_even_with_a_number(self):
        verdict = loops.resolve(process_state=loops.RUNNING, volume=5, floor=1.0,
                                has_signal=False)
        self.assertEqual(verdict["status"], loops.UNVERIFIED)
        self.assertIn(loops.R_NO_SIGNAL, verdict["reasons"])

    def test_no_input_can_produce_ok_without_a_positive_volume(self):
        # Exhaustive guard on the module's whole reason for existing.
        for process in (loops.RUNNING, loops.PROCESS_UNKNOWN, loops.NOT_LOADED, loops.STOPPED):
            for floor in (None, 0.0, 5.0):
                for rate in (None, 0.0, 0.9):
                    for volume in (None, 0, -1):
                        verdict = loops.resolve(process_state=process, volume=volume,
                                                floor=floor, error_rate=rate)
                        self.assertNotEqual(
                            verdict["status"], loops.OK,
                            f"volume={volume!r} floor={floor!r} produced OK")


class TestLaunchdState(unittest.TestCase):
    LISTING = (
        "-\t0\tcom.omniagentos.edc-triage\n"
        "-\t2\tcom.omniagentos.reflection-watchdog\n"
        "73197\t-9\tcom.apple.managedcorespotlightd\n"
    )

    def test_listed_label_is_running(self):
        self.assertEqual(
            loops.launchd_state("com.omniagentos.edc-triage", self.LISTING),
            loops.RUNNING)

    def test_unlisted_label_is_not_loaded(self):
        self.assertEqual(
            loops.launchd_state("com.omniagentos.swarm-optimizer", self.LISTING),
            loops.NOT_LOADED)

    def test_not_loaded_label_resolves_down_end_to_end(self):
        state = loops.launchd_state("com.omniagentos.swarm-optimizer", self.LISTING)
        self.assertEqual(loops.resolve(process_state=state, volume=None)["status"], loops.DOWN)

    def test_substring_label_does_not_false_match(self):
        self.assertEqual(
            loops.launchd_state("com.omniagentos.edc", self.LISTING),
            loops.NOT_LOADED)


class TestCounters(unittest.TestCase):
    RECORDS = [
        {"ts": "2026-08-15T11:00:00Z", "role": "implementer"},
        {"ts": "2026-08-15T09:00:00Z", "role": "reviewer"},
        {"ts": "2026-08-14T23:00:00Z", "role": "implementer"},
        {"ts": "2026-08-10T09:00:00Z", "role": "implementer"},
        {"role": "implementer"},  # untimestamped: uncountable, must be dropped
        {"ts": "not-a-timestamp", "role": "implementer"},
    ]

    def test_window_count_counts_only_the_trailing_window(self):
        # cutoff is 2026-08-14T12:00Z: the two 08-15 rows and the 08-14T23:00 row.
        self.assertEqual(loops.window_count(self.RECORDS, hours=24, now=NOW), 3)

    def test_window_count_honours_a_predicate(self):
        implementers = loops.window_count(
            self.RECORDS, hours=24, now=NOW,
            predicate=lambda r: r.get("role") == "implementer")
        self.assertEqual(implementers, 2)

    def test_untimestamped_records_never_become_volume(self):
        # A timestamp is not a count, and the lack of one is not a zero either:
        # these rows are simply absent from every bucket.
        self.assertEqual(sum(loops.daily_counts(self.RECORDS).values()), 4)

    def test_daily_counts_buckets_by_utc_day(self):
        counts = loops.daily_counts(self.RECORDS)
        self.assertEqual(counts["2026-08-15"], 2)
        self.assertEqual(counts["2026-08-14"], 1)
        self.assertEqual(counts["2026-08-10"], 1)

    def test_read_jsonl_skips_torn_lines_and_missing_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "ledger.jsonl")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(json.dumps({"ts": "2026-08-15T00:00:00Z"}) + "\n")
                handle.write('{"ts": "2026-08-15T01:00:00Z"\n')  # torn
                handle.write("\n")
                handle.write("[1,2,3]\n")  # not an object
                handle.write(json.dumps({"ts": "2026-08-15T02:00:00Z"}) + "\n")
            self.assertEqual(len(loops.read_jsonl(path)), 2)
            self.assertEqual(loops.read_jsonl(os.path.join(tmp, "absent.jsonl")), [])


class TestEvaluate(unittest.TestCase):
    def _capability(self, **throughput):
        block = {
            "signal_path": "/fixture/ledger.jsonl",
            "window": "24h",
            "max_error_rate": 0.25,
            "floor": {"floor": 240.0, "median": 800, "fraction": 0.3, "sample_days": 7},
            "observed": {"process_state": loops.RUNNING, "volume_24h": 1000},
        }
        block.update(throughput)
        return {"id": "loop-fixture", "throughput": block}

    def test_recorded_observation_grades_ok(self):
        verdict = loops.evaluate(self._capability())
        self.assertEqual(verdict["status"], loops.OK)
        self.assertEqual(verdict["id"], "loop-fixture")
        self.assertEqual(verdict["signal_path"], "/fixture/ledger.jsonl")

    def test_fresh_observation_overrides_the_recorded_one(self):
        verdict = loops.evaluate(self._capability(), observation={"volume_24h": 0})
        self.assertEqual(verdict["status"], loops.DEGRADED)

    def test_daily_counts_in_the_observation_re_derive_the_floor(self):
        capability = self._capability(observed={
            "process_state": loops.RUNNING,
            "volume_24h": 50,
            "daily_counts": _series([1000] * 7),
        })
        verdict = loops.evaluate(capability, now=NOW)
        self.assertEqual(verdict["floor_derivation"]["floor"], 300.0)
        self.assertEqual(verdict["status"], loops.DEGRADED)

    def test_missing_signal_path_is_unverified(self):
        verdict = loops.evaluate(self._capability(signal_path=None))
        self.assertEqual(verdict["status"], loops.UNVERIFIED)

    def test_signal_declared_absent_is_unverified(self):
        verdict = loops.evaluate(self._capability(signal_exists=False))
        self.assertEqual(verdict["status"], loops.UNVERIFIED)


class TestShippedCapabilities(unittest.TestCase):
    """The registry entries this unit ships must load, validate and grade."""

    def setUp(self):
        self.capabilities = loops.load_loop_capabilities()

    def test_loop_capabilities_are_present(self):
        self.assertTrue(self.capabilities, "no loop-* capabilities with a throughput block")

    def test_every_loop_capability_validates_against_the_registry(self):
        import registry
        ids = {c["id"] for c in registry.load_registry()}
        for capability in self.capabilities:
            self.assertIn(capability["id"], ids)
            self.assertEqual(capability["kind"], "llm-loop")

    def test_every_loop_capability_declares_an_auditable_floor(self):
        for capability in self.capabilities:
            floor = capability["throughput"]["floor"]
            self.assertIn("floor", floor)
            if floor.get("floor") is None:
                self.assertTrue(floor.get("reason"), "{} null floor needs a reason".format(capability["id"]))
            else:
                self.assertTrue(floor.get("sample"), "{} floor needs its sample".format(capability["id"]))
                self.assertGreaterEqual(floor["sample_days"], loops.MIN_FLOOR_SAMPLE_DAYS)
                self.assertAlmostEqual(
                    floor["floor"], round(floor["median"] * floor["fraction"], 2), places=2)

    def test_a_null_floor_capability_never_records_status_ok(self):
        for capability in self.capabilities:
            if capability["throughput"]["floor"].get("floor") is None:
                self.assertNotEqual(capability["last_status"], loops.OK, capability["id"])

    def test_recorded_status_matches_what_evaluate_derives(self):
        for capability in self.capabilities:
            verdict = loops.evaluate(capability)
            self.assertEqual(
                verdict["status"], capability["last_status"],
                "{} records {} but grades {} ({})".format(capability["id"], capability["last_status"],
                   verdict["status"], verdict["reasons"]))

    def test_no_capability_carries_a_credential_value(self):
        import re
        shapes = re.compile(r"(sk-[A-Za-z0-9]|ghp_|xoxb-|AKIA|EAA[A-Za-z0-9]|pit-|BEGIN [A-Z ]*PRIVATE KEY|postgres(ql)?://[^ \"]*:[^ \"@]+@)")
        for capability in self.capabilities:
            blob = json.dumps(capability)
            self.assertIsNone(shapes.search(blob), "{} may carry a secret".format(capability["id"]))

    def test_render_table_covers_every_loop(self):
        table = loops.render_table([loops.evaluate(c) for c in self.capabilities])
        for capability in self.capabilities:
            self.assertIn(capability["id"], table)


if __name__ == "__main__":
    unittest.main(verbosity=2)
