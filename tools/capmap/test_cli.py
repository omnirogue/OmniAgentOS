import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cli  # noqa: E402
import store  # noqa: E402


def capability(identifier, command, semantics, company="estate"):
    return {
        "id": identifier, "company": company, "kind": "mechanical-automation", "host": "test",
        "repo": "test", "what_it_does": "fixture", "why_it_matters": "fixture", "blast_radius": "test",
        "verification": {"type": "command", "argv": ["/bin/sh", "-c", command], "fixture": True},
        "exit_semantics": semantics, "cadence_seconds": 10, "staleness_slo_seconds": 60,
        "escalation_tier": "low", "owner": "test", "last_verified": None, "last_status": "UNVERIFIED",
    }


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="capmap-cli-")
        self.registry_dir = os.path.join(self.tmp, "registry")
        self.store_dir = os.path.join(self.tmp, "state")
        os.makedirs(self.registry_dir)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, value):
        with open(os.path.join(self.registry_dir, value["id"] + ".json"), "w") as fh:
            json.dump(value, fh)

    def invoke(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = cli.main(["--registry-dir", self.registry_dir, "--store-dir", self.store_dir] + list(args))
        return code, output.getvalue()

    def test_verify_worst_status_and_cannot_evaluate_is_not_green(self):
        self.write(capability("good", "exit 0", {"0": "ok"}))
        self.write(capability("unknown", "exit 9", {"0": "ok"}))
        self.write(capability("cannot", "exit 2", {"2": "cannot_evaluate"}))
        self.write(capability("down", "exit 1", {"1": "down"}))
        code, table = self.invoke("verify", "--all")
        self.assertEqual(code, cli.EXIT_HARD_FAILURE)
        self.assertIn("CANNOT_EVALUATE", table)
        self.assertNotIn("cannot | OK", table)
        code, rendered = self.invoke("status", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(next(row for row in json.loads(rendered) if row["id"] == "cannot")["status"], store.CANNOT_EVALUATE)

    def test_verify_unverified_gets_distinct_exit_code_and_run_history_appends(self):
        self.write(capability("unmapped", "exit 8", {"0": "ok"}))
        code, _ = self.invoke("verify", "--all")
        self.assertEqual(code, cli.EXIT_UNVERIFIED)
        code, _ = self.invoke("verify", "--all")
        self.assertEqual(code, cli.EXIT_UNVERIFIED)
        with open(os.path.join(self.store_dir, "runs.jsonl")) as fh:
            self.assertEqual(len([line for line in fh if line.strip()]), 2)

    def test_accept_allows_an_explicit_non_ok_fixture(self):
        self.write(capability("degraded", "exit 3", {"3": "degraded"}))
        code, _ = self.invoke("verify", "--all", "--accept", "degraded")
        self.assertEqual(code, 0)

    def test_snapshot_field_is_read_without_reimplementing_the_probe(self):
        source = os.path.join(self.tmp, "integrations-health.json")
        with open(source, "w") as fh:
            json.dump({"integrations": {"demo": {"status": "UP", "checked_at": store.now_iso()}}}, fh)
        entry = capability("snapshot", "exit 99", {"UP": "ok"})
        entry["verification"] = {"type": "snapshot_field", "source": source,
                                 "field": "integrations.demo.status",
                                 "checked_at_field": "integrations.demo.checked_at"}
        self.write(entry)
        code, output = self.invoke("verify", "--all")
        self.assertEqual(code, 0)
        self.assertIn("snapshot", output)
        self.assertEqual(store.read_snapshot(self.store_dir)["snapshot"]["status"], store.OK)

    def test_status_renders_an_old_ok_row_as_stale(self):
        self.write(capability("old", "exit 0", {"0": "ok"}))
        store.write_snapshot({"old": {"status": store.OK, "last_verified": "2000-01-01T00:00:00Z"}}, self.store_dir)
        code, output = self.invoke("status")
        self.assertEqual(code, 0)
        self.assertIn("STALE", output)
