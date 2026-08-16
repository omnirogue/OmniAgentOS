import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import registry  # noqa: E402


def capability(identifier="demo", **overrides):
    value = {
        "id": identifier, "company": "estate", "kind": "external-service", "host": "test",
        "repo": "test", "what_it_does": "test", "why_it_matters": "test", "blast_radius": "test",
        "verification": {"type": "unset"}, "exit_semantics": {}, "cadence_seconds": 10,
        "staleness_slo_seconds": 20, "escalation_tier": "low", "owner": "test",
        "last_verified": None, "last_status": "UNVERIFIED",
    }
    value.update(overrides)
    return value


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="capmap-registry-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, name, value):
        with open(os.path.join(self.tmp, name + ".json"), "w") as fh:
            json.dump(value, fh)

    def test_load_get_and_filter(self):
        self.write("alpha", capability("alpha", company="estate"))
        self.write("beta", capability("beta", company="acmeuni", kind="data-store"))
        self.assertEqual([item["id"] for item in registry.load_registry(self.tmp)], ["alpha", "beta"])
        self.assertEqual(registry.get("beta", self.tmp)["company"], "acmeuni")
        self.assertEqual([item["id"] for item in registry.filter(company="acmeuni", dir=self.tmp)], ["beta"])
        self.assertEqual([item["id"] for item in registry.filter(kind="external-service", dir=self.tmp)], ["alpha"])

    def test_missing_id_or_exit_semantics_is_rejected(self):
        missing_id = capability()
        del missing_id["id"]
        self.write("missing-id", missing_id)
        with self.assertRaises(ValueError):
            registry.load_registry(self.tmp)
        os.unlink(os.path.join(self.tmp, "missing-id.json"))
        missing_semantics = capability("no-semantics")
        del missing_semantics["exit_semantics"]
        self.write("missing-semantics", missing_semantics)
        with self.assertRaises(ValueError):
            registry.load_registry(self.tmp)

    def test_seed_registry_contains_the_thirteen_scoped_capabilities(self):
        entries = registry.load_registry()
        # The registry GROWS as W5/W6 add services and loops — asserting an exact
        # count makes every coverage increase look like a regression. The seed set
        # is a floor, not a ceiling.
        self.assertGreaterEqual(len(entries), 13)
        # Subset, not equality — later units legitimately add services and loops.
        # Equality here would mean "coverage may never grow", which inverts the goal.
        seed = {
            "tailscale", "airtop", "telegram-bot", "piedpiper-acmeuni", "piedpiper-globex", "piedpiper-initech",
            "name.com", "freshdesk-acmeuni", "freshdesk-initech", "hang-recycler", "reflection-watchdog",
            "loop-cadence", "queue-pressure-fixture",
        }
        self.assertLessEqual(seed, {entry["id"] for entry in entries})

