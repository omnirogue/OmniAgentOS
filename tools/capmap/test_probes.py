"""Hermetic tests for the read-only service probes.

Every test injects a fake transport; no test opens a socket, reads
connections.env, or touches a vendor.  The fake records each request so the
suite can assert the two properties whose absence produces real incidents:
an explicit User-Agent on every request, and a credential value never appearing
anywhere in a probe's output.
"""

import datetime
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import probes  # noqa: E402
import registry  # noqa: E402
import store  # noqa: E402

SECRET = "sk_live_THIS_IS_THE_SECRET_VALUE_0123456789"


class FakeTransport:
    """Scripted transport.  ``responses`` may be one response or a per-call list."""

    def __init__(self, responses):
        self.responses = responses if isinstance(responses, list) else [responses]
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        index = min(len(self.requests) - 1, len(self.responses) - 1)
        return self.responses[index]


def response(status=None, body=b"", error=None):
    return probes.HttpResponse(status=status, body=body, error=error)


def json_response(status, payload):
    return response(status, json.dumps(payload).encode("utf-8"))


def fake_env(overrides=None, tmpdir=None):
    """A complete, entirely fictitious environment for every registered probe."""
    env = {"META_GRAPH_VERSION": "v19.0", "TELLER_API_BASE": "https://api.teller.example"}
    for spec in probes.PROBE_SPECS:
        for name in spec["vars"]:
            env.setdefault(name, SECRET)
    env["FORTPOINT_NMI_QUERY_URL"] = "https://gateway.example/api/query.php"
    env["GLACIER_NMI_QUERY_URL"] = "https://gateway2.example/api/query.php"
    env["INITECH_FANBASIS_PRODUCTION_API_URL"] = "https://fanbasis.example"
    env["JIRA_BASE_URL"] = "https://jira.example"
    env["ZENDESK_SUBDOMAIN"] = "example-sub"
    env["TELLER_CERT_PATH"] = os.path.join(tmpdir or "/nonexistent", "certificate.pem")
    env["TELLER_CERT_KEY_PATH"] = os.path.join(tmpdir or "/nonexistent", "private_key.pem")
    env.update(overrides or {})
    return env


# Probes whose single HTTP call is graded by status class alone.  The
# body-sensitive probes are asserted individually further down.
STATUS_CLASS_PROBES = ["stripe-estate-primary", "clerk-crm-prod", "paypal-readonly",
                       "zendesk-initech", "customerio-app", "clickup", "jira-initech",
                       "bunny-cdn"]


class StatusClassTests(unittest.TestCase):
    def test_401_and_403_are_suspended_not_down(self):
        for probe_id in STATUS_CLASS_PROBES:
            for status in (401, 403):
                transport = FakeTransport(response(status, b"{}"))
                result = probes.run_probe(probe_id, fake_env(), transport)
                self.assertEqual(result["state"], probes.SUSPENDED, (probe_id, status))
                self.assertEqual(result["exit_code"], probes.EXIT_SUSPENDED)
                self.assertFalse(result["billing"])

    def test_402_is_suspended_and_flagged_as_billing_with_its_own_exit_code(self):
        for probe_id in STATUS_CLASS_PROBES:
            transport = FakeTransport(response(402, b"{}"))
            result = probes.run_probe(probe_id, fake_env(), transport)
            self.assertEqual(result["state"], probes.SUSPENDED, probe_id)
            self.assertTrue(result["billing"], probe_id)
            self.assertEqual(result["exit_code"], probes.EXIT_SUSPENDED_BILLING)
            self.assertIn("billing", result["detail"])
            self.assertNotEqual(probes.EXIT_SUSPENDED, probes.EXIT_SUSPENDED_BILLING)

    def test_5xx_is_down(self):
        for status in (500, 502, 503):
            transport = FakeTransport(response(status, b"oops"))
            result = probes.run_probe("stripe-estate-primary", fake_env(), transport)
            self.assertEqual(result["state"], probes.DOWN, status)
            self.assertEqual(result["exit_code"], probes.EXIT_DOWN)

    def test_network_failure_is_down_not_suspended(self):
        transport = FakeTransport(response(error="URLError"))
        result = probes.run_probe("stripe-estate-primary", fake_env(), transport)
        self.assertEqual(result["state"], probes.DOWN)
        self.assertIn("network", result["detail"])

    def test_rate_limit_and_probe_side_4xx_are_unverified_never_up(self):
        for status in (429, 404, 400):
            transport = FakeTransport(response(status, b"{}"))
            result = probes.run_probe("stripe-estate-primary", fake_env(), transport)
            self.assertEqual(result["state"], probes.UNVERIFIED, status)
            self.assertEqual(result["exit_code"], probes.EXIT_UNVERIFIED)

    def test_2xx_is_up(self):
        transport = FakeTransport(response(200, b"{}"))
        result = probes.run_probe("stripe-estate-primary", fake_env(), transport)
        self.assertEqual(result["state"], probes.UP)
        self.assertEqual(result["exit_code"], probes.EXIT_UP)


class MissingCredentialTests(unittest.TestCase):
    def test_absent_variable_is_no_cred_and_makes_no_request(self):
        for spec in probes.PROBE_SPECS:
            if spec.get("no_safe_probe"):
                continue
            env = fake_env()
            for name in spec["vars"]:
                env.pop(name, None)
            transport = FakeTransport(response(200, b"{}"))
            result = probes.run_probe(spec["id"], env, transport)
            self.assertEqual(result["state"], probes.NO_CRED, spec["id"])
            self.assertEqual(result["exit_code"], probes.EXIT_NO_CRED, spec["id"])
            self.assertEqual(transport.requests, [], spec["id"])
            for name in spec["vars"]:
                self.assertIn(name, result["detail"] + " " + " ".join(result["credential_vars"]))

    def test_empty_string_credential_counts_as_missing(self):
        env = fake_env({"STRIPE_PRIMARY_SECRET_KEY": ""})
        transport = FakeTransport(response(200, b"{}"))
        result = probes.run_probe("stripe-estate-primary", env, transport)
        self.assertEqual(result["state"], probes.NO_CRED)


class NoSafeProbeTests(unittest.TestCase):
    def test_service_without_a_safe_probe_is_unverified_and_never_ok(self):
        transport = FakeTransport(response(200, b'{"ok":true}'))
        result = probes.run_probe("slack-webhook-ops-alert", fake_env(), transport)
        self.assertEqual(result["state"], probes.UNVERIFIED)
        self.assertEqual(result["exit_code"], probes.EXIT_UNVERIFIED)
        self.assertEqual(transport.requests, [], "an unprobeable capability must send nothing")

    def test_its_capability_document_declares_verification_unset(self):
        document = probes.capability_document(probes.PROBES["slack-webhook-ops-alert"])
        self.assertEqual(document["verification"]["type"], "unset")
        self.assertEqual(document["exit_semantics"], {})
        self.assertEqual(document["last_status"], "UNVERIFIED")
        self.assertEqual(store.compute_status(document, 0), store.UNVERIFIED)


class UserAgentTests(unittest.TestCase):
    def test_every_probe_sets_an_explicit_non_urllib_user_agent(self):
        transport = FakeTransport([json_response(200, {"ok": True, "success": True,
                                                       "status": "ok", "result": "SUCCESS"})] * 4)
        env = fake_env()
        sent = 0
        for spec in probes.PROBE_SPECS:
            probes.run_probe(spec["id"], env, transport)
        for request in transport.requests:
            agent = request.headers.get("User-Agent")
            self.assertTrue(agent, f"missing User-Agent on {request.url}")
            self.assertNotIn("urllib", agent.lower())
            self.assertEqual(agent, probes.USER_AGENT)
            sent += 1
        self.assertGreater(sent, 20, "expected most probes to have issued a request")

    def test_user_agent_survives_a_probe_supplying_its_own_headers(self):
        transport = FakeTransport(json_response(200, {"ok": True}))
        probes.run_probe("slack-bot", fake_env(), transport)
        self.assertEqual(transport.requests[0].headers["User-Agent"], probes.USER_AGENT)
        self.assertIn("Authorization", transport.requests[0].headers)


class CredentialLeakTests(unittest.TestCase):
    def test_no_probe_output_ever_contains_a_credential_value(self):
        env = fake_env()
        bodies = [json_response(200, {"ok": False, "error": "invalid_auth", "success": False,
                                      "result": "ERROR", "message": SECRET}),
                  response(401, SECRET.encode("utf-8")),
                  response(500, SECRET.encode("utf-8"))]
        for scripted in bodies:
            transport = FakeTransport([scripted] * 3)
            for spec in probes.PROBE_SPECS:
                result = probes.run_probe(spec["id"], env, transport)
                rendered = json.dumps(result)
                self.assertNotIn(SECRET, rendered, spec["id"])
                self.assertNotIn(SECRET[:20], rendered, spec["id"])

    def test_credential_variables_are_reported_by_name(self):
        transport = FakeTransport(response(200, b"{}"))
        result = probes.run_probe("stripe-estate-primary", fake_env(), transport)
        self.assertEqual(result["credential_vars"], ["STRIPE_PRIMARY_SECRET_KEY"])


class ReadOnlyTests(unittest.TestCase):
    def test_no_probe_issues_a_method_outside_the_read_only_set(self):
        transport = FakeTransport([json_response(200, {"ok": True, "success": True,
                                                       "status": "ok"})] * 4)
        env = fake_env()
        for spec in probes.PROBE_SPECS:
            probes.run_probe(spec["id"], env, transport)
        for request in transport.requests:
            self.assertIn(request.method, ("GET", "POST"), request.url)
            if request.method == "POST":
                self.assertTrue(
                    request.url.endswith("query.php") or "oauth2/token" in request.url,
                    f"unexpected POST target {request.url}")

    def test_nmi_refuses_any_endpoint_that_is_not_query_php(self):
        env = fake_env({"FORTPOINT_NMI_QUERY_URL": "https://gateway.example/api/transact.php"})
        transport = FakeTransport(response(200, b"<nm_response></nm_response>"))
        result = probes.run_probe("nmi-fortpoint", env, transport)
        self.assertEqual(result["state"], probes.UNVERIFIED)
        self.assertEqual(transport.requests, [], "must not call a non-query endpoint")


class BodySensitiveVendorTests(unittest.TestCase):
    """Vendors that report auth failure without a 4xx status."""

    def test_meta_reports_an_expired_token_as_http_400_oauth_and_maps_to_suspended(self):
        payload = {"error": {"message": "Error validating access token",
                             "type": "OAuthException", "code": 190}}
        transport = FakeTransport(json_response(400, payload))
        result = probes.run_probe("meta-ads-initech", fake_env(), transport)
        self.assertEqual(result["state"], probes.SUSPENDED)
        self.assertEqual(result["exit_code"], probes.EXIT_SUSPENDED)
        self.assertIn("oauth", result["detail"])

    def test_meta_non_oauth_400_stays_unverified(self):
        transport = FakeTransport(json_response(400, {"error": {"type": "GraphMethodException",
                                                                "code": 100}}))
        result = probes.run_probe("meta-ads-initech", fake_env(), transport)
        self.assertEqual(result["state"], probes.UNVERIFIED)

    def test_slack_http_200_with_ok_false_is_suspended_not_up(self):
        transport = FakeTransport(json_response(200, {"ok": False, "error": "token_revoked"}))
        result = probes.run_probe("slack-bot", fake_env(), transport)
        self.assertEqual(result["state"], probes.SUSPENDED)

    def test_slack_http_200_with_ok_true_is_up(self):
        transport = FakeTransport(json_response(200, {"ok": True, "team": "t"}))
        self.assertEqual(probes.run_probe("slack-bot", fake_env(), transport)["state"], probes.UP)

    def test_checkout_champ_http_200_error_envelope_is_suspended(self):
        transport = FakeTransport(json_response(200, {"result": "ERROR",
                                                      "message": "Invalid login"}))
        result = probes.run_probe("vandelay", fake_env(), transport)
        self.assertEqual(result["state"], probes.SUSPENDED)

    def test_checkout_champ_ip_allowlist_refusal_is_unverified_not_suspended(self):
        transport = FakeTransport(json_response(200, {"result": "ERROR",
                                                      "message": "IP must be whitelisted - 1.2.3.4"}))
        result = probes.run_probe("vandelay", fake_env(), transport)
        self.assertEqual(result["state"], probes.UNVERIFIED)
        self.assertNotIn("1.2.3.4", json.dumps(result))

    def test_checkout_champ_success_envelope_is_up(self):
        transport = FakeTransport(json_response(200, {"result": "SUCCESS", "message": []}))
        self.assertEqual(probes.run_probe("vandelay", fake_env(), transport)["state"],
                         probes.UP)

    def test_nmi_error_response_element_is_suspended_despite_http_200(self):
        body = b"<?xml version='1.0'?><nm_response><error_response>Specified API key not found" \
               b"</error_response></nm_response>"
        transport = FakeTransport(response(200, body))
        result = probes.run_probe("nmi-fortpoint", fake_env(), transport)
        self.assertEqual(result["state"], probes.SUSPENDED)
        self.assertNotIn("REFID", json.dumps(result))

    def test_nmi_clean_nm_response_is_up(self):
        transport = FakeTransport(response(200, b"<?xml version='1.0'?><nm_response>\n</nm_response>"))
        self.assertEqual(probes.run_probe("nmi-fortpoint", fake_env(), transport)["state"],
                         probes.UP)

    def test_nmi_query_window_is_future_dated_so_no_transaction_data_is_returned(self):
        transport = FakeTransport(response(200, b"<nm_response></nm_response>"))
        probes.run_probe("nmi-fortpoint", fake_env(), transport)
        self.assertIn("start_date=20990101000000", transport.requests[0].body)

    def test_vultr_ip_allowlist_401_is_unverified_not_a_dead_key(self):
        transport = FakeTransport(json_response(401, {"error": "Unauthorized IP address: 1.2.3.4",
                                                      "status": 401}))
        result = probes.run_probe("vultr-api", fake_env(), transport)
        self.assertEqual(result["state"], probes.UNVERIFIED)
        self.assertNotIn("1.2.3.4", json.dumps(result))

    def test_vultr_plain_401_is_still_suspended(self):
        transport = FakeTransport(json_response(401, {"error": "Invalid API key", "status": 401}))
        self.assertEqual(probes.run_probe("vultr-api", fake_env(), transport)["state"],
                         probes.SUSPENDED)

    def test_cloudflare_success_false_is_suspended(self):
        transport = FakeTransport(json_response(401, {"success": False,
                                                      "errors": [{"code": 1000}]}))
        self.assertEqual(probes.run_probe("cloudflare-api", fake_env(), transport)["state"],
                         probes.SUSPENDED)

    def test_fanbasis_liveness_reports_up_only_on_an_ok_health_envelope(self):
        transport = FakeTransport(json_response(200, {"status": "ok"}))
        self.assertEqual(probes.run_probe("fanbasis-initech-prod-liveness",
                                          fake_env(), transport)["state"], probes.UP)
        transport = FakeTransport(json_response(200, {"page": "html"}))
        self.assertEqual(probes.run_probe("fanbasis-initech-prod-liveness",
                                          fake_env(), transport)["state"], probes.UNVERIFIED)

    def test_ovh_uses_the_vendor_clock_and_signs_the_request(self):
        transport = FakeTransport([response(200, b"1755000000"), json_response(200, {"nichandle": "x"})])
        result = probes.run_probe("ovh-api", fake_env(), transport)
        self.assertEqual(result["state"], probes.UP)
        self.assertEqual(len(transport.requests), 2)
        signed = transport.requests[1].headers
        self.assertTrue(signed["X-Ovh-Signature"].startswith("$1$"))
        self.assertEqual(signed["X-Ovh-Timestamp"], "1755000000")
        self.assertNotIn(SECRET, signed["X-Ovh-Signature"])

    def test_ovh_time_preflight_failure_is_unverified_not_suspended(self):
        transport = FakeTransport([response(error="URLError")])
        self.assertEqual(probes.run_probe("ovh-api", fake_env(), transport)["state"],
                         probes.UNVERIFIED)


class TellerCertTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="capmap-probes-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, not_after, now):
        path = os.path.join(self.tmp, "certificate.pem")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("not a real certificate")
        env = fake_env(tmpdir=self.tmp)
        original = probes._decode_cert_not_after
        probes._decode_cert_not_after = lambda _path: not_after
        try:
            spec = probes.PROBES["teller-client-cert"]
            return probes.probe_teller_cert(spec, env, None, now=now)
        finally:
            probes._decode_cert_not_after = original

    def test_expired_certificate_is_down(self):
        now = datetime.datetime(2026, 8, 15, tzinfo=datetime.timezone.utc)
        result = self._run(now - datetime.timedelta(days=3), now)
        self.assertEqual(result["state"], probes.DOWN)

    def test_certificate_expiring_inside_the_warning_band_is_suspended(self):
        now = datetime.datetime(2026, 8, 15, tzinfo=datetime.timezone.utc)
        result = self._run(now + datetime.timedelta(days=10), now)
        self.assertEqual(result["state"], probes.SUSPENDED)

    def test_long_lived_certificate_is_up(self):
        now = datetime.datetime(2026, 8, 15, tzinfo=datetime.timezone.utc)
        result = self._run(now + datetime.timedelta(days=900), now)
        self.assertEqual(result["state"], probes.UP)

    def test_missing_certificate_file_is_no_cred(self):
        env = fake_env({"TELLER_CERT_PATH": os.path.join(self.tmp, "absent.pem")}, tmpdir=self.tmp)
        result = probes.run_probe("teller-client-cert", env, FakeTransport(response(200)))
        self.assertEqual(result["state"], probes.NO_CRED)

    def test_teller_api_probe_requests_mutual_tls(self):
        transport = FakeTransport(response(200, b"[]"))
        probes.run_probe("teller-api", fake_env(tmpdir=self.tmp), transport)
        self.assertIsNotNone(transport.requests[0].client_cert)

    def test_unloadable_client_cert_is_unverified_not_down(self):
        transport = FakeTransport(response(error="client_cert_SSLError"))
        result = probes.run_probe("teller-api", fake_env(tmpdir=self.tmp), transport)
        self.assertEqual(result["state"], probes.UNVERIFIED)


class ProbeFaultTests(unittest.TestCase):
    def test_a_raising_probe_is_unverified_never_up(self):
        def explode(spec, env, transport):
            raise RuntimeError("boom")

        original = probes.PROBES["clickup"]["fn"]
        probes.PROBES["clickup"]["fn"] = explode
        try:
            result = probes.run_probe("clickup", fake_env(), FakeTransport(response(200)))
        finally:
            probes.PROBES["clickup"]["fn"] = original
        self.assertEqual(result["state"], probes.UNVERIFIED)
        self.assertEqual(result["exit_code"], probes.EXIT_UNVERIFIED)
        self.assertIn("RuntimeError", result["detail"])

    def test_unknown_probe_id_raises(self):
        with self.assertRaises(KeyError):
            probes.run_probe("no-such-probe", fake_env(), FakeTransport(response(200)))


class CapabilityDocumentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="capmap-caps-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_every_document_passes_registry_validation_and_loads(self):
        written, _ = probes.emit_capabilities(self.tmp)
        self.assertEqual(len(written), len(probes.PROBE_SPECS))
        entries = registry.load_registry(self.tmp)
        self.assertEqual(len(entries), len(probes.PROBE_SPECS))

    def test_emit_never_overwrites_an_existing_capability_file(self):
        path = os.path.join(self.tmp, "stripe-estate-primary.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write('{"sentinel": true}')
        written, skipped = probes.emit_capabilities(self.tmp)
        self.assertIn(path, skipped)
        self.assertNotIn(path, written)
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle), {"sentinel": True})

    def test_exit_semantics_never_map_a_failure_code_to_ok(self):
        for spec in probes.PROBE_SPECS:
            document = probes.capability_document(spec)
            for code, semantic in document["exit_semantics"].items():
                if code != "0":
                    self.assertNotEqual(semantic, "ok", (spec["id"], code))
            self.assertEqual(store.compute_status(document, probes.EXIT_UNVERIFIED),
                             store.UNVERIFIED, spec["id"])

    def test_payment_capabilities_treat_a_refused_credential_as_down(self):
        for probe_id in ("stripe-estate-primary", "clerk-crm-prod", "nmi-fortpoint",
                         "vandelay", "paypal-readonly", "teller-api", "meta-ads-acmeuni"):
            document = probes.capability_document(probes.PROBES[probe_id])
            self.assertEqual(store.compute_status(document, probes.EXIT_SUSPENDED),
                             store.DOWN, probe_id)
            self.assertEqual(store.compute_status(document, probes.EXIT_SUSPENDED_BILLING),
                             store.DOWN, probe_id)

    def test_supporting_capabilities_treat_a_refused_credential_as_degraded(self):
        for probe_id in ("clickup", "bunny-cdn", "jira-initech"):
            document = probes.capability_document(probes.PROBES[probe_id])
            self.assertEqual(store.compute_status(document, probes.EXIT_SUSPENDED),
                             store.DEGRADED, probe_id)

    def test_each_document_maps_every_status_class_the_probes_can_emit(self):
        for spec in probes.PROBE_SPECS:
            if spec.get("no_safe_probe"):
                continue
            document = probes.capability_document(spec)
            self.assertEqual(store.compute_status(document, probes.EXIT_UP), store.OK)
            self.assertEqual(store.compute_status(document, probes.EXIT_DOWN), store.DOWN)
            self.assertEqual(store.compute_status(document, probes.EXIT_NO_CRED),
                             store.CANNOT_EVALUATE)

    def test_documents_reference_credentials_by_name_and_carry_no_values(self):
        for spec in probes.PROBE_SPECS:
            rendered = json.dumps(probes.capability_document(spec))
            self.assertNotIn(SECRET, rendered)
            for name in spec["vars"]:
                self.assertIn(name, rendered, spec["id"])


class EnvLoaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="capmap-env-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_quotes_and_comments_are_handled_and_a_missing_file_is_not_fatal(self):
        path = os.path.join(self.tmp, "connections.env")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("# comment\nFOO=\"bar\"\nBAZ='qux'\nEMPTY=\n")
        env = probes.load_env(path)
        self.assertEqual(env["FOO"], "bar")
        self.assertEqual(env["BAZ"], "qux")
        self.assertEqual(env["EMPTY"], "")
        self.assertIsInstance(probes.load_env(os.path.join(self.tmp, "absent.env")), dict)


class CliTests(unittest.TestCase):
    def test_list_and_emit_are_offline_and_exit_zero(self):
        tmp = tempfile.mkdtemp(prefix="capmap-cli-")
        try:
            self.assertEqual(probes.main(["--list"]), 0)
            self.assertEqual(probes.main(["--emit-capabilities", tmp]), 0)
            self.assertEqual(len(os.listdir(tmp)), len(probes.PROBE_SPECS))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_unknown_probe_id_exits_64(self):
        self.assertEqual(probes.main(["no-such-probe"]), 64)


if __name__ == "__main__":
    unittest.main()
