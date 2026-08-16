#!/usr/bin/env python3
"""Unit tests for the capmap durable alert transport.

DELIVERY IS PROVEN WITHOUT DELIVERING. Every Alerter under test is constructed
with explicit fake channels, so no test touches urllib at all. Nothing here
monkeypatches a global: there is no patched-out network to leak through if a
test fails halfway, and no ordering dependency that could let a real request
escape. The only place a live channel can be built is build_alerter(), which no
test calls.

Run:  /usr/bin/python3 -m unittest discover -s tools/capmap -p 'test_*.py' -v
"""

import datetime
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import alert  # noqa: E402


class FakeChannel(alert.Channel):
    """Records what it was asked to deliver; fails on demand."""

    def __init__(self, name, fail=False, error="boom"):
        self.name = name
        self.fail = fail
        self.error = error
        self.sent = []

    def deliver(self, message, text):
        self.attempted = True
        if self.fail:
            raise alert.ChannelError(self.error)
        self.sent.append((message, text))
        return {"fake": self.name}


class Clock:
    """Controllable UTC clock so escalation and staleness are tested by arithmetic,
    not by sleeping."""

    def __init__(self, start=None):
        self.now = start or datetime.datetime(2026, 8, 15, 12, 0, 0,
                                              tzinfo=datetime.timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now = self.now + datetime.timedelta(seconds=seconds)
        return self.now


class AlertTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="capmap-alert-test-")
        self.incidents = os.path.join(self.tmp, "incidents.json")
        self.ledger = os.path.join(self.tmp, "alerts.jsonl")
        self.clock = Clock()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def alerter(self, channels, **kwargs):
        return alert.Alerter(channels=channels, incidents_path=self.incidents,
                             ledger_path=self.ledger, clock=self.clock, **kwargs)

    def ledger_rows(self):
        if not os.path.exists(self.ledger):
            return []
        with open(self.ledger) as fh:
            return [json.loads(line) for line in fh if line.strip()]


class TestDedupe(AlertTestBase):
    def test_second_alert_for_open_incident_is_suppressed(self):
        primary = FakeChannel("slack_dm")
        a = self.alerter([primary])
        first = a.send("freshdesk-initech", "UP", "DOWN", detail="http 502")
        self.clock.advance(900)
        second = a.send("freshdesk-initech", "DOWN", "DOWN", detail="http 502")
        self.assertEqual(first["action"], "alerted")
        self.assertEqual(second["action"], "suppressed_duplicate")
        self.assertEqual(len(primary.sent), 1, "an open incident must page exactly once")

    def test_close_then_reopen_alerts_again(self):
        primary = FakeChannel("slack_dm")
        a = self.alerter([primary], notify_recovery=False)
        a.send("freshdesk-initech", "UP", "DOWN", detail="http 502")
        self.clock.advance(900)
        closed = a.send("freshdesk-initech", "DOWN", "UP", detail="http 200")
        self.assertEqual(closed["action"], "closed_silently")
        self.clock.advance(900)
        reopened = a.send("freshdesk-initech", "UP", "DOWN", detail="http 502")
        self.assertEqual(reopened["action"], "alerted")
        self.assertEqual(len(primary.sent), 2,
                         "a genuinely new incident must page again")

    def test_recovery_notice_only_after_a_delivered_alert(self):
        primary = FakeChannel("slack_dm")
        a = self.alerter([primary])
        a.send("piedpiper-acmeuni", "UP", "SUSPENDED", detail="http 403")
        self.clock.advance(1800)
        result = a.send("piedpiper-acmeuni", "SUSPENDED", "UP", detail="http 200")
        self.assertEqual(result["action"], "recovered")
        self.assertEqual(primary.sent[-1][0].kind, "RECOVERY")

    def test_good_to_good_never_opens_an_incident(self):
        primary = FakeChannel("slack_dm")
        a = self.alerter([primary])
        result = a.send("tailscale", "UP", "UP", detail="daemon")
        self.assertEqual(result["action"], "no_incident")
        self.assertEqual(primary.sent, [])

    def test_escalation_renotifies_after_the_interval(self):
        primary = FakeChannel("slack_dm")
        a = self.alerter([primary], escalate_after=24 * 3600)
        a.send("freshdesk-initech", "UP", "DOWN")
        self.clock.advance(23 * 3600)
        self.assertEqual(a.send("freshdesk-initech", "DOWN", "DOWN")["action"],
                         "suppressed_duplicate")
        self.clock.advance(2 * 3600)          # now 25h since the first page
        escalated = a.send("freshdesk-initech", "DOWN", "DOWN")
        self.assertEqual(escalated["action"], "escalated")
        self.assertEqual(primary.sent[-1][0].kind, "ONGOING")

    def test_change_of_bad_status_is_news_not_a_duplicate(self):
        primary = FakeChannel("slack_dm")
        a = self.alerter([primary])
        a.send("piedpiper-initech", "UP", "DOWN", detail="timeout")
        self.clock.advance(900)
        changed = a.send("piedpiper-initech", "DOWN", "SUSPENDED", detail="http 403")
        self.assertEqual(changed["action"], "alerted")
        self.assertEqual(len(primary.sent), 2)

    def test_incident_state_survives_a_new_process(self):
        primary = FakeChannel("slack_dm")
        self.alerter([primary]).send("name.com", "UP", "DOWN")
        fresh = self.alerter([FakeChannel("slack_dm")])
        self.assertEqual(fresh.send("name.com", "DOWN", "DOWN")["action"],
                         "suppressed_duplicate")


class TestFallbackChain(AlertTestBase):
    def test_fallback_advances_on_channel_failure(self):
        dm = FakeChannel("slack_dm", fail=True, error="invalid_auth")
        hook = FakeChannel("slack_webhook")
        ntfy = FakeChannel("ntfy")
        a = self.alerter([dm, hook, ntfy])
        result = a.send("freshdesk-initech", "UP", "DOWN")
        self.assertTrue(result["delivered"])
        self.assertEqual(len(hook.sent), 1, "the webhook must catch the DM's failure")
        self.assertEqual(ntfy.sent, [], "a later channel must not fire after success")
        channels = [(row["channel"], row["ok"]) for row in self.ledger_rows()]
        self.assertEqual(channels, [("slack_dm", False), ("slack_webhook", True)],
                         "both the failure and the recovery are on the record")

    def test_third_channel_catches_a_double_failure(self):
        dm = FakeChannel("slack_dm", fail=True)
        hook = FakeChannel("slack_webhook", fail=True)
        ntfy = FakeChannel("ntfy")
        a = self.alerter([dm, hook, ntfy])
        self.assertTrue(a.send("freshdesk-initech", "UP", "DOWN")["delivered"])
        self.assertEqual(len(ntfy.sent), 1)

    def test_all_channels_fail_is_recorded_loudly(self):
        channels = [FakeChannel("slack_dm", fail=True),
                    FakeChannel("slack_webhook", fail=True),
                    FakeChannel("ntfy", fail=True)]
        a = self.alerter(channels)
        result = a.send("freshdesk-initech", "UP", "DOWN", detail="http 502")
        self.assertFalse(result["delivered"])
        rows = self.ledger_rows()
        final = rows[-1]
        self.assertEqual(final["channel"], "ALL")
        self.assertFalse(final["ok"])
        self.assertEqual(final["marker"], alert.UNDELIVERED_MARKER)
        self.assertTrue(final["undelivered"])
        self.assertEqual(final["tried"], ["slack_dm", "slack_webhook", "ntfy"])
        self.assertIn("freshdesk-initech", final["text"])
        self.assertEqual(len(rows), 4, "3 attempts + 1 undelivered marker")

    def test_undelivered_incident_is_retried_next_cadence(self):
        dead = [FakeChannel("slack_dm", fail=True), FakeChannel("ntfy", fail=True)]
        a = self.alerter(dead)
        a.send("freshdesk-initech", "UP", "DOWN")
        alive = FakeChannel("slack_dm")
        b = self.alerter([alive])
        retried = b.send("freshdesk-initech", "DOWN", "DOWN")
        self.assertEqual(retried["action"], "alerted")
        self.assertEqual(len(alive.sent), 1,
                         "an alert that never reached the operator is not a duplicate")

    def test_no_channels_configured_is_undelivered_not_success(self):
        a = self.alerter([])
        result = a.send("freshdesk-initech", "UP", "DOWN")
        self.assertFalse(result["delivered"])
        self.assertEqual(self.ledger_rows()[-1]["marker"], alert.UNDELIVERED_MARKER)

    def test_ledger_is_append_only(self):
        a = self.alerter([FakeChannel("slack_dm")])
        a.send("a-cap", "UP", "DOWN")
        a.send("b-cap", "UP", "DOWN")
        rows = self.ledger_rows()
        self.assertEqual([row["capability"] for row in rows], ["a-cap", "b-cap"])
        for row in rows:
            for field in ("ts", "capability", "transition", "channel", "ok", "error"):
                self.assertIn(field, row)


class TestDeadMan(AlertTestBase):
    def heartbeat(self, stamp):
        path = os.path.join(self.tmp, "health.json")
        with open(path, "w") as fh:
            json.dump({"probe_ran_at": stamp, "integrations": {}}, fh)
        return path

    def test_does_not_fire_inside_the_threshold(self):
        primary = FakeChannel("slack_dm")
        path = self.heartbeat(alert.iso(self.clock.now - datetime.timedelta(seconds=1000)))
        a = self.alerter([primary])
        result = a.check_liveness(heartbeat_file=path, cadence_seconds=900,
                                  max_cadences=2)
        self.assertFalse(result["stale"])
        self.assertEqual(primary.sent, [])

    def test_fires_past_the_threshold(self):
        primary = FakeChannel("slack_dm")
        path = self.heartbeat(alert.iso(self.clock.now - datetime.timedelta(seconds=2000)))
        a = self.alerter([primary])
        result = a.check_liveness(heartbeat_file=path, cadence_seconds=900,
                                  max_cadences=2)
        self.assertTrue(result["stale"])
        self.assertEqual(len(primary.sent), 1)
        self.assertEqual(primary.sent[0][0].kind, "STALE_CHECKER")

    def test_stale_alert_is_deduped_then_recovers(self):
        primary = FakeChannel("slack_dm")
        stale = self.heartbeat(alert.iso(self.clock.now - datetime.timedelta(seconds=9000)))
        a = self.alerter([primary])
        a.check_liveness(heartbeat_file=stale, cadence_seconds=900, max_cadences=2)
        self.clock.advance(900)
        again = a.check_liveness(heartbeat_file=stale, cadence_seconds=900,
                                 max_cadences=2)
        self.assertEqual(again["action"], "suppressed_duplicate")
        self.assertEqual(len(primary.sent), 1)
        fresh = self.heartbeat(alert.iso(self.clock.now))
        recovered = a.check_liveness(heartbeat_file=fresh, cadence_seconds=900,
                                     max_cadences=2)
        self.assertEqual(recovered["action"], "recovered")

    def test_missing_heartbeat_file_is_stale_not_healthy(self):
        primary = FakeChannel("slack_dm")
        a = self.alerter([primary])
        result = a.check_liveness(heartbeat_file=os.path.join(self.tmp, "nope.json"))
        self.assertTrue(result["stale"])
        self.assertEqual(len(primary.sent), 1)

    def test_unparseable_heartbeat_is_stale(self):
        path = os.path.join(self.tmp, "corrupt.json")
        with open(path, "w") as fh:
            fh.write("{ truncated")
        primary = FakeChannel("slack_dm")
        self.assertTrue(self.alerter([primary]).check_liveness(heartbeat_file=path)["stale"])


class TestSeedMode(AlertTestBase):
    def test_seed_mode_sends_nothing(self):
        primary = FakeChannel("slack_dm")
        a = self.alerter([primary], seed_only=True)
        for name in ("cap-a", "cap-b", "cap-c"):
            self.assertEqual(a.send(name, "UNKNOWN", "DOWN")["action"], "seeded")
        self.assertEqual(primary.sent, [], "seeding must never page the operator")
        self.assertTrue(all(row["suppressed"] == "seed_only"
                            for row in self.ledger_rows()))

    def test_seeded_incident_stays_silent_while_unchanged(self):
        """Baseline-unhealthy is not news, and at real scale it is a storm.

        SUPERSEDES `test_seeded_incident_pages_exactly_once_when_live`, which
        asserted the opposite ("it does page once ... seeding suppresses the first
        storm, not every future report"). That reasoning holds for a 9-capability
        registry. Against a representative 66-capability registry, 57 read
        unhealthy at baseline, so "pages exactly once each" is 57 messages one
        cadence after the --seed-only run whose entire purpose was preventing
        exactly that.

        The information is NOT lost: capabilities that are unhealthy at baseline
        are reported by the once-per-day digest (one line listing them), which is
        the T1 'digested' tier. Individual pages are the T2 'escalated' tier and
        are reserved for genuine transitions. A capability that is broken before
        we started watching is a backlog item, not a page.
        """
        a = self.alerter([FakeChannel("slack_dm")], seed_only=True)
        a.send("cap-a", "UNKNOWN", "DOWN")
        primary = FakeChannel("slack_dm")
        live = self.alerter([primary])
        self.assertEqual(live.send("cap-a", "DOWN", "DOWN")["action"],
                         "suppressed_seed_baseline")
        self.assertEqual(primary.sent, [])

    def test_seeded_incident_still_alerts_when_the_status_actually_moves(self):
        """Suppression must be scoped to 'unchanged', never to the capability."""
        a = self.alerter([FakeChannel("slack_dm")], seed_only=True)
        a.send("cap-a", "UNKNOWN", "DOWN")
        primary = FakeChannel("slack_dm")
        live = self.alerter([primary])
        # DOWN -> SUSPENDED is a different fact with a different remedy: news.
        self.assertEqual(live.send("cap-a", "DOWN", "SUSPENDED")["action"], "alerted")
        self.assertEqual(len(primary.sent), 1)

    def test_a_genuine_delivery_failure_still_retries_after_seeding_exists(self):
        """The seed suppression must not swallow the undelivered-retry path."""
        dead = FakeChannel("slack_dm", fail=True)
        a = self.alerter([dead])
        a.send("cap-b", "OK", "DOWN")           # every channel fails -> undelivered
        primary = FakeChannel("slack_dm")
        live = self.alerter([primary])
        self.assertEqual(live.send("cap-b", "DOWN", "DOWN")["action"], "alerted")
        self.assertEqual(len(primary.sent), 1)

    def test_seed_mode_closes_without_sending_a_recovery(self):
        primary = FakeChannel("slack_dm")
        a = self.alerter([primary], seed_only=True)
        a.send("cap-a", "UNKNOWN", "DOWN")
        self.assertEqual(a.send("cap-a", "DOWN", "UP")["action"], "seeded")
        self.assertEqual(primary.sent, [])


class TestDryRunChannel(AlertTestBase):
    def test_dry_run_renders_without_delivering(self):
        live = FakeChannel("slack_dm")
        dry = alert.DryRunChannel(wrapping=[live])
        a = self.alerter([dry])
        a.send("freshdesk-initech", "UP", "DOWN", detail="http 502",
               blast_radius="Initech support inbox", remedy="check the vendor")
        self.assertEqual(live.sent, [], "a dry run must not reach a live channel")
        self.assertEqual(len(dry.sink), 1)
        self.assertIn("freshdesk-initech", dry.sink[0])
        self.assertIn("Initech support inbox", dry.sink[0])


class TestDigest(AlertTestBase):
    def test_all_green_is_one_line(self):
        a = self.alerter([FakeChannel("slack_dm")])
        line, delivered = a.digest(statuses={"a": "UP", "b": "UP", "c": "UP"})
        self.assertTrue(delivered)
        self.assertEqual(len(line.splitlines()), 1)
        self.assertIn("3 capabilities checked, all green", line)

    def test_broken_capabilities_are_listed(self):
        a = self.alerter([FakeChannel("slack_dm")])
        line, _ = a.digest(statuses={"a": "UP", "b": "DOWN", "c": "SUSPENDED"})
        self.assertIn("2 NOT green", line)
        self.assertIn("b DOWN", line)
        self.assertIn("c SUSPENDED", line)

    def test_empty_map_reads_red_not_green(self):
        a = self.alerter([FakeChannel("slack_dm")])
        line, _ = a.digest(statuses={})
        self.assertIn("NO CAPABILITIES CHECKED", line)
        self.assertNotIn("all green", line)

    def test_digest_can_render_without_sending(self):
        primary = FakeChannel("slack_dm")
        line, _ = self.alerter([primary]).digest(statuses={"a": "UP"}, send=False)
        self.assertIn("all green", line)
        self.assertEqual(primary.sent, [])


class TestRedaction(AlertTestBase):
    def test_secret_values_never_reach_message_or_ledger(self):
        secret = "xoxb-000000000000-111111111111-abcdefghijklmnopqrstuvwx"
        primary = FakeChannel("slack_dm")
        a = self.alerter([primary], secrets={"SLACK_BOT_TOKEN": secret})
        a.send("cap", "UP", "DOWN", detail=f"auth failed with {secret}")
        rendered = primary.sent[0][1]
        self.assertNotIn(secret, rendered)
        self.assertIn("<redacted:SLACK_BOT_TOKEN>", rendered)
        with open(self.ledger) as fh:
            self.assertNotIn(secret, fh.read())
        with open(self.incidents) as fh:
            self.assertNotIn(secret, fh.read())

    def test_token_shaped_strings_are_masked_even_if_unknown(self):
        primary = FakeChannel("slack_dm")
        a = self.alerter([primary])
        a.send("cap", "UP", "DOWN",
               detail="posting to https://hooks.slack.com/services/T0/B0/zzzzzzzz failed")
        self.assertNotIn("hooks.slack.com/services", primary.sent[0][1])

    def test_ordinary_env_words_do_not_corrupt_the_alert_text(self):
        """Regression: the first live dry run rendered
        "freshdesk-<redacted:INITECH_EVAL_LOCAL_POSTGRES_USER>" because a
        non-credential env value happened to be the brand name. Over-redaction
        mangles the alert the operator has to act on."""
        primary = FakeChannel("slack_dm")
        a = self.alerter([primary], secrets={
            "INITECH_EVAL_LOCAL_POSTGRES_USER": "initech",
            "FRESHDESK_INITECH_DOMAIN": "initechsupport",
        })
        a.send("freshdesk-initech", "UP", "DOWN", detail="http 502")
        self.assertIn("freshdesk-initech", primary.sent[0][1])
        self.assertNotIn("redacted", primary.sent[0][1])

    def test_long_mixed_values_are_redacted_even_under_an_odd_key(self):
        value = "a1b2c3d4e5f6g7h8i9j0k1l2"
        primary = FakeChannel("slack_dm")
        a = self.alerter([primary], secrets={"WEIRDLY_NAMED_THING": value})
        a.send("cap", "UP", "DOWN", detail="failed with " + value)
        self.assertNotIn(value, primary.sent[0][1])

    def test_short_values_are_not_redacted(self):
        primary = FakeChannel("slack_dm")
        a = self.alerter([primary], secrets={"TEAM": "T01"})
        a.send("cap", "UP", "DOWN", detail="http 502")
        self.assertIn("http 502", primary.sent[0][1])


class TestMessageContent(AlertTestBase):
    def test_message_carries_the_four_required_facts(self):
        primary = FakeChannel("slack_dm")
        a = self.alerter([primary])
        since = self.clock.now - datetime.timedelta(hours=50)
        a.send("freshdesk-initech", "UP", "DOWN", detail="http 502",
               blast_radius="Initech customer support — tickets unanswered",
               remedy="check status.freshworks.com; verify FRESHDESK_INITECH_API_KEY",
               since=since)
        text = primary.sent[0][1]
        self.assertIn("*what:*", text)
        self.assertIn("UP->DOWN", text)
        self.assertIn("*since:*", text)
        self.assertIn(alert.iso(since), text)
        self.assertIn("*blast radius:*", text)
        self.assertIn("*recommended:*", text)

    def test_unknown_status_is_treated_as_bad(self):
        self.assertFalse(alert.is_good("WEIRD"))
        self.assertFalse(alert.is_good(None))
        self.assertTrue(alert.is_good("up"))


class TestAtomicState(AlertTestBase):
    def test_incident_file_is_replaced_atomically_and_leaves_no_tmp(self):
        a = self.alerter([FakeChannel("slack_dm")])
        a.send("cap", "UP", "DOWN")
        self.assertTrue(os.path.exists(self.incidents))
        leftovers = [n for n in os.listdir(self.tmp) if ".tmp." in n]
        self.assertEqual(leftovers, [])
        with open(self.incidents) as fh:
            state = json.load(fh)
        self.assertIn("cap", state["open"])

    def test_corrupt_incident_file_does_not_crash_the_alerter(self):
        with open(self.incidents, "w") as fh:
            fh.write("{ not json")
        primary = FakeChannel("slack_dm")
        a = self.alerter([primary])
        self.assertEqual(a.send("cap", "UP", "DOWN")["action"], "alerted")


class TestBuildChannels(unittest.TestCase):
    """Channel construction only — no delivery, so nothing here can send."""

    def test_chain_order_is_dm_then_webhook_then_ntfy(self):
        channels = alert.build_channels(env={
            "SLACK_BOT_TOKEN": "x" * 20,
            "OPS_ALERT_SLACK_WEBHOOK_URL": "https://example.invalid/hook",
            "OMNI_NTFY_URL": "https://example.invalid/ntfy",
        })
        self.assertEqual([c.name for c in channels],
                         [alert.CHANNEL_SLACK_DM, alert.CHANNEL_SLACK_WEBHOOK,
                          alert.CHANNEL_NTFY])

    def test_absent_credentials_drop_their_channel(self):
        channels = alert.build_channels(env={"OMNI_NTFY_URL": "https://example.invalid/n"})
        self.assertEqual([c.name for c in channels], [alert.CHANNEL_NTFY])

    def test_channel_plans_never_print_a_credential(self):
        secret_url = "https://hooks.slack.com/services/T0/B0/supersecretvalue"
        channels = alert.build_channels(env={
            "SLACK_BOT_TOKEN": "xoxb-secret-token-value-here",
            "OPS_ALERT_SLACK_WEBHOOK_URL": secret_url,
            "OMNI_NTFY_URL": "https://ntfy.example.invalid/secret-topic",
        })
        for channel in channels:
            plan = channel.plan()
            self.assertNotIn("xoxb-", plan)
            self.assertNotIn(secret_url, plan)
            self.assertNotIn("secret-topic", plan)


if __name__ == "__main__":
    unittest.main(verbosity=2)
