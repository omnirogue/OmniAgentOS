#!/usr/bin/env python3
"""Unit tests for the capmap scheduled runner.

NOTHING HERE CAN PAGE A HUMAN. Every test either injects an ``alert.Alerter``
built with explicit fake channels, or drives run.py as a subprocess with
``--dry-run`` (which ``alert.build_alerter`` answers by replacing the whole
chain with a DryRunChannel before an Alerter ever sees a live carrier). No test
calls ``run.run`` with ``alerter=None`` against production paths, no test reads
or writes the production store, and no test touches the ARMED file.

Verification is injected too: the fakes never spawn a probe, so the suite makes
no network call and no subprocess except the one explicit CLI test.

Run:  /usr/bin/python3 -m unittest discover -s tools/capmap -p 'test_*.py' -v
"""

import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import alert  # noqa: E402
import run  # noqa: E402
import store  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


class FakeChannel(alert.Channel):
    """Records deliveries instead of performing them."""

    name = "fake"

    def __init__(self):
        self.sent = []

    def deliver(self, message, text):
        self.sent.append({"capability": message.capability, "kind": message.kind,
                          "transition": message.transition, "text": text})
        return {"fake": True}

    def titles(self):
        """Capabilities paged about, excluding the daily proof-of-life line."""
        return [item["capability"] for item in self.sent if item["kind"] != "DIGEST"]


def capability(cap_id, company="estate", **extra):
    value = {
        "id": cap_id, "company": company, "kind": "external-service",
        "host": "owner-mac", "repo": "/tmp/repo",
        "what_it_does": f"does {cap_id}", "why_it_matters": f"matters for {cap_id}",
        "blast_radius": f"blast for {cap_id}",
        "verification": {"type": "command", "argv": ["/usr/bin/true"]},
        "exit_semantics": {"0": "ok", "1": "down"},
        "cadence_seconds": 900, "staleness_slo_seconds": 86400,
        "escalation_tier": "high", "owner": "estate-ops",
        "last_verified": None, "last_status": "UNVERIFIED",
    }
    value.update(extra)
    return value


def write_registry(directory, capabilities):
    os.makedirs(directory, exist_ok=True)
    for cap in capabilities:
        with open(os.path.join(directory, cap["id"] + ".json"), "w") as fh:
            json.dump(cap, fh, indent=2, sort_keys=True)
    return directory


def fake_verify(statuses, now_iso=None):
    """A verification pass with predetermined results.

    It writes the snapshot exactly the way ``cli._verify`` does, because the
    runner reads the PREVIOUS status out of that snapshot -- a fake that skipped
    the write would make every run look like a first run."""
    def verify_fn(entries, store_dir):
        stamp = now_iso or store.now_iso()
        state = store.read_snapshot(store_dir)
        rows = []
        for cap in entries:
            status = statuses[cap["id"]]
            state[cap["id"]] = {"status": status, "last_verified": stamp,
                                "latency_ms": 1, "evidence": None,
                                "updated_at": stamp}
            rows.append({"company": cap["company"], "id": cap["id"],
                         "status": status, "last_verified": stamp})
        store.write_snapshot(state, store_dir)
        return rows, 0
    return verify_fn


class RunnerTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="capmap-run-")
        self.registry_dir = write_registry(
            os.path.join(self.tmp, "capabilities"),
            [capability("alpha"), capability("beta"), capability("gamma")])
        self.store_dir = os.path.join(self.tmp, "store")
        self.channel = FakeChannel()
        self.out = open(os.devnull, "w")

    def tearDown(self):
        self.out.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def alerter(self, seed_only=False, clock=None):
        return alert.Alerter(channels=[self.channel],
                             incidents_path=os.path.join(self.tmp, "incidents.json"),
                             ledger_path=os.path.join(self.tmp, "alerts.jsonl"),
                             seed_only=seed_only, clock=clock)

    def do_run(self, statuses, alerter, **kwargs):
        return run.run(registry_dir=self.registry_dir, store_dir=self.store_dir,
                       alerter=alerter, verify_fn=fake_verify(statuses),
                       out=self.out, **kwargs)


class SeedTests(RunnerTestCase):
    def test_seed_only_sends_nothing_and_records_every_baseline(self):
        """66 never-verified capabilities must not become 66 pages."""
        alerter = self.alerter(seed_only=True)
        summary = self.do_run({"alpha": "DOWN", "beta": "UNVERIFIED",
                               "gamma": "OK"}, alerter, seed_only=True)

        self.assertEqual([], self.channel.sent, "seed run delivered something")
        self.assertTrue(summary["seeding"])
        self.assertEqual([], summary["sent"])
        recorded = alerter.state()["last_status"]
        self.assertEqual({"alpha", "beta", "gamma"}, set(recorded))
        self.assertEqual("DOWN", recorded["alpha"]["status"])
        self.assertEqual("OK", recorded["gamma"]["status"])
        self.assertFalse(summary["digest_emitted"])

    def test_first_run_without_the_flag_seeds_itself(self):
        """A missing baseline forces seed mode even if the caller forgot."""
        alerter = self.alerter(seed_only=False)
        summary = self.do_run({"alpha": "DOWN", "beta": "DOWN",
                               "gamma": "DOWN"}, alerter)

        self.assertTrue(summary["seeding"])
        self.assertTrue(alerter.seed_only, "runner did not force seed mode")
        self.assertEqual([], self.channel.sent)
        self.assertEqual(["seeded"] * 3,
                         [action["action"] for action in summary["actions"]])

    def test_baseline_is_recorded_so_seeding_happens_once(self):
        alerter = self.alerter()
        self.do_run({"alpha": "OK", "beta": "OK", "gamma": "OK"}, alerter)
        self.assertTrue(run.seeding_required(self.store_dir) is False)


class TransitionTests(RunnerTestCase):
    def seed(self, statuses):
        self.do_run(statuses, self.alerter(seed_only=True), seed_only=True)
        self.channel.sent = []

    def test_ok_to_down_alerts_once_and_a_repeat_run_is_silent(self):
        self.seed({"alpha": "OK", "beta": "OK", "gamma": "OK"})

        first = self.do_run({"alpha": "DOWN", "beta": "OK", "gamma": "OK"},
                            self.alerter())
        self.assertEqual(["alpha"], self.channel.titles())
        self.assertEqual([{"capability": "alpha", "from": "OK", "to": "DOWN",
                           "action": "alerted", "delivered": True}],
                         first["sent"])

        second = self.do_run({"alpha": "DOWN", "beta": "OK", "gamma": "OK"},
                             self.alerter())
        self.assertEqual(["alpha"], self.channel.titles(),
                         "an unchanged DOWN was alerted twice")
        self.assertEqual([], second["sent"])
        actions = {a["capability"]: a["action"] for a in second["actions"]}
        self.assertEqual("suppressed_duplicate", actions["alpha"])

    def test_recovery_reaches_alert_send(self):
        self.seed({"alpha": "OK", "beta": "OK", "gamma": "OK"})
        self.do_run({"alpha": "DOWN", "beta": "OK", "gamma": "OK"}, self.alerter())
        self.channel.sent = []

        summary = self.do_run({"alpha": "OK", "beta": "OK", "gamma": "OK"},
                              self.alerter())

        self.assertEqual(["alpha"], self.channel.titles())
        self.assertEqual("RECOVERY", self.channel.sent[0]["kind"])
        self.assertEqual("DOWN->OK", self.channel.sent[0]["transition"])
        actions = {a["capability"]: a["action"] for a in summary["actions"]}
        self.assertEqual("recovered", actions["alpha"])
        self.assertEqual("no_incident", actions["beta"],
                         "healthy capabilities must still be fed to alert.send")

    def test_every_capability_is_offered_to_the_transport(self):
        self.seed({"alpha": "OK", "beta": "OK", "gamma": "OK"})
        summary = self.do_run({"alpha": "OK", "beta": "DEGRADED", "gamma": "OK"},
                              self.alerter())
        self.assertEqual({"alpha", "beta", "gamma"},
                         {action["capability"] for action in summary["actions"]})


class DigestTests(RunnerTestCase):
    def digests(self):
        return [item for item in self.channel.sent if item["kind"] == "DIGEST"]

    def test_digest_fires_once_per_day_and_not_twice(self):
        self.do_run({"alpha": "OK", "beta": "OK", "gamma": "OK"},
                    self.alerter(seed_only=True), seed_only=True)
        self.assertEqual([], self.digests(), "a seed run emitted a digest")

        day = datetime.datetime(2026, 8, 15, 9, 0, tzinfo=datetime.timezone.utc)
        first = self.do_run({"alpha": "OK", "beta": "OK", "gamma": "OK"},
                            self.alerter(), now=day)
        self.assertEqual(1, len(self.digests()))
        self.assertTrue(first["digest_emitted"])
        self.assertIn("3 capabilities checked, all green", first["digest_line"])

        later = day + datetime.timedelta(hours=6)
        second = self.do_run({"alpha": "OK", "beta": "OK", "gamma": "OK"},
                             self.alerter(), now=later)
        self.assertEqual(1, len(self.digests()), "two digests in one day")
        self.assertFalse(second["digest_emitted"])
        self.assertIn("already emitted today", second["digest_reason"])

        tomorrow = day + datetime.timedelta(days=1)
        third = self.do_run({"alpha": "OK", "beta": "OK", "gamma": "OK"},
                            self.alerter(), now=tomorrow)
        self.assertEqual(2, len(self.digests()), "the next day was skipped")
        self.assertTrue(third["digest_emitted"])

    def test_a_skipped_day_is_reported_not_swallowed(self):
        self.do_run({"alpha": "OK", "beta": "OK", "gamma": "OK"},
                    self.alerter(seed_only=True), seed_only=True)
        day = datetime.datetime(2026, 8, 15, 9, 0, tzinfo=datetime.timezone.utc)
        self.do_run({"alpha": "OK", "beta": "OK", "gamma": "OK"},
                    self.alerter(), now=day)
        gap_run = self.do_run({"alpha": "OK", "beta": "OK", "gamma": "OK"},
                              self.alerter(), now=day + datetime.timedelta(days=3))
        self.assertEqual(2, gap_run["digest_gap_days"])
        self.assertTrue(gap_run["digest_emitted"])
        self.assertTrue(any("digest gap" in warning
                            for warning in gap_run.get("warnings", [])))

    def test_digest_counts_unhealthy_capabilities(self):
        self.do_run({"alpha": "OK", "beta": "OK", "gamma": "OK"},
                    self.alerter(seed_only=True), seed_only=True)
        summary = self.do_run({"alpha": "DOWN", "beta": "OK", "gamma": "UNVERIFIED"},
                              self.alerter())
        self.assertIn("2 NOT green", summary["digest_line"])


class HeartbeatTests(RunnerTestCase):
    def test_run_writes_a_heartbeat_in_the_shape_check_liveness_reads(self):
        summary = self.do_run({"alpha": "OK", "beta": "OK", "gamma": "OK"},
                              self.alerter(seed_only=True), seed_only=True)
        path = summary["heartbeat_file"]
        with open(path) as fh:
            payload = json.load(fh)
        self.assertIn(run.DEFAULT_HEARTBEAT_FIELD, payload)
        self.assertIsNotNone(alert.parse_iso(payload[run.DEFAULT_HEARTBEAT_FIELD]))
        self.assertEqual(3, payload["capabilities_checked"])

        result = run.check_liveness(store_dir=self.store_dir,
                                    alerter=self.alerter(), out=self.out)
        self.assertFalse(result["stale"])

    def test_a_missing_heartbeat_reads_stale_never_healthy(self):
        missing = os.path.join(self.tmp, "nope", "heartbeat.json")
        result = run.check_liveness(store_dir=self.store_dir,
                                    heartbeat_file=missing,
                                    alerter=self.alerter(), out=self.out)
        self.assertTrue(result["stale"])
        self.assertEqual(["checker:capmap"], self.channel.titles())
        self.assertEqual("STALE_CHECKER", self.channel.sent[0]["kind"])

    def test_an_old_heartbeat_reads_stale(self):
        stale_at = alert.now_utc() - datetime.timedelta(hours=6)
        path = os.path.join(self.tmp, "hb.json")
        alert.atomic_write_json(path, {run.DEFAULT_HEARTBEAT_FIELD:
                                       alert.iso(stale_at)})
        result = run.check_liveness(store_dir=self.store_dir, heartbeat_file=path,
                                    alerter=self.alerter(), out=self.out)
        self.assertTrue(result["stale"])

    def test_dry_run_writes_no_heartbeat(self):
        summary = self.do_run({"alpha": "OK", "beta": "OK", "gamma": "OK"},
                              self.alerter(seed_only=True), seed_only=True,
                              dry_run=True)
        self.assertIsNone(summary["heartbeat"])
        self.assertFalse(os.path.exists(summary["heartbeat_file"]))


class ExitCodeTests(RunnerTestCase):
    def test_unhealthy_capabilities_still_exit_zero(self):
        """An outage is a successful CHECK, not a failed run."""
        self.do_run({"alpha": "OK", "beta": "OK", "gamma": "OK"},
                    self.alerter(seed_only=True), seed_only=True)
        summary = self.do_run({"alpha": "DOWN", "beta": "CANNOT_EVALUATE",
                               "gamma": "STALE"}, self.alerter())
        self.assertEqual(3, len(summary["unhealthy"]))
        self.assertEqual([], summary["runner_errors"])
        self.assertEqual(run.EXIT_OK, run.exit_code(summary))

    def test_a_broken_transport_is_a_runner_failure(self):
        class ExplodingAlerter:
            seed_only = True

            def send(self, *args, **kwargs):
                raise OSError("incidents.json is unwritable")

            def digest(self, statuses=None):
                return "unused", True

        summary = self.do_run({"alpha": "OK", "beta": "OK", "gamma": "OK"},
                              ExplodingAlerter(), seed_only=True)
        self.assertEqual(3, len(summary["runner_errors"]))
        self.assertEqual(run.EXIT_RUNNER_FAILED, run.exit_code(summary))

    def test_an_unreadable_registry_is_a_runner_failure(self):
        with self.assertRaises(run.RunnerError):
            run.run(registry_dir=os.path.join(self.tmp, "absent"),
                    store_dir=self.store_dir, alerter=self.alerter(),
                    verify_fn=fake_verify({}), out=self.out)

    def test_cli_dry_run_exits_zero_with_everything_down(self):
        """End-to-end through main(): real CLI, real verify path, nothing sent.

        --dry-run makes alert.build_alerter substitute a DryRunChannel for the
        entire chain, so this cannot deliver even though the estate is armed."""
        down_dir = write_registry(
            os.path.join(self.tmp, "down"),
            [capability("delta", verification={"type": "command",
                                               "argv": ["/usr/bin/false"]})])
        completed = subprocess.run(
            [sys.executable, os.path.join(HERE, "run.py"), "--dry-run", "--json",
             "--registry-dir", down_dir,
             "--store-dir", os.path.join(self.tmp, "cli-store")],
            capture_output=True, timeout=120)
        self.assertEqual(0, completed.returncode, completed.stderr.decode())
        payload = json.loads(completed.stdout.decode())
        self.assertEqual(["delta"], payload["unhealthy"])
        self.assertTrue(payload["dry_run"])
        self.assertEqual([], payload["runner_errors"])


if __name__ == "__main__":
    unittest.main()
