"""Egress guardrail tests for bridge/notify.py.

The push transport POSTs to a PUBLIC ntfy relay. These tests prove that what
is actually sent over the wire (POST body + every header value) carries no
secret, no private key, no token, no absolute filesystem path, and no
multi-line content — regardless of what the caller passed. The caller's local
ALERTS.md line is deliberately NOT the concern here; only egress is.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest import mock

import pytest

BRIDGE = Path(__file__).resolve().parents[1] / "bridge"
if str(BRIDGE) not in sys.path:
    sys.path.insert(0, str(BRIDGE))

import notify  # noqa: E402

TOPIC = "https://ntfy.sh/example-topic-0000000000000000"


class _FakeResp:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def getcode(self):
        return 200


def _send(body, title="alert", *, url=TOPIC, root="/tmp/nope", **kw):
    """Push once through a faked urlopen and return what would hit the wire."""
    captured: dict = {}

    def fake_urlopen(req, timeout=None):
        captured["body"] = (req.data or b"").decode("utf-8", "replace")
        # urllib capitalises header keys as "X-title"; normalise to lower.
        captured["headers"] = {k.lower(): v for k, v in req.header_items()}
        return _FakeResp()

    with mock.patch.object(notify.urllib.request, "urlopen", fake_urlopen):
        captured["rc"] = notify.push_alert(root, title, body, url=url, **kw)
    return captured


# --- secret shapes never egress ---------------------------------------------

SECRETS = [
    "sk-ABCDEFGHIJKLMNOPQRSTUVWX1234567890",
    "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
    "gho_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
    "xoxb-1234567890-abcdefABCDEF",
    "AKIAIOSFODNN7EXAMPLE",
    "AIzaSyAABBCCDDEEFF00112233445566778899xyz",
    "EAAGm0PX4ZCpsBA1234567890abcdefABCDEF",
    "pit-1234-abcd-5678-efgh-ij",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTYifQ.dozjgNryP4J3jVmNHl0w5N",
    "wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY123",  # 40-char aws-secret shape
]


@pytest.mark.parametrize("secret", SECRETS)
def test_secret_never_in_body(secret):
    got = _send(f"candidate parked: leaked {secret} in payload")
    assert secret not in got["body"], f"secret egressed in body: {got['body']!r}"
    assert "[redacted]" in got["body"]


@pytest.mark.parametrize("secret", SECRETS)
def test_secret_never_in_title(secret):
    got = _send("body ok", title=f"ALERT {secret}")
    assert secret not in got["headers"].get("x-title", "")


def test_kv_secret_redacted():
    # both secrets gone (hard-label 'password' redacts to end-of-line, which
    # subsumes the trailing 'token:' value too — safe over-redaction).
    got = _send("db failed password=hunter2 token: abc123XYZ")
    assert "hunter2" not in got["body"]
    assert "abc123XYZ" not in got["body"]
    assert "[redacted]" in got["body"]


def test_private_key_block_redacted():
    body = "boom -----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA\n-----END RSA PRIVATE KEY----- done"
    got = _send(body)
    assert "PRIVATE KEY" not in got["body"]
    assert "MIIEpA" not in got["body"]


# --- ntfy topic itself never echoed -----------------------------------------

def test_topic_url_not_echoed():
    got = _send(f"push failed to {TOPIC} retrying")
    assert "example-topic-0000000000000000" not in got["body"]
    # host may remain for context, but never the secret topic path
    assert TOPIC not in got["body"]


# --- absolute paths reduced to basename -------------------------------------

def test_abs_path_reduced():
    got = _send("cannot read /Users/youruser/.config/omni/connections.env now")
    assert "/Users/youruser" not in got["body"]
    assert "connections.env" in got["body"]


def test_multiple_path_flavours():
    got = _send("paths /private/tmp/x/secret.key and /etc/shadow and /var/run/id")
    assert "/private/tmp" not in got["body"]
    assert "/etc/shadow" not in got["body"]
    assert "/var/run" not in got["body"]


# --- structural guarantees ---------------------------------------------------

def test_only_first_line_egresses():
    got = _send("SAFE HEADLINE\nSECRET-ON-LINE-2 sk-ABCDEFGHIJKLMNOP1234")
    assert "SAFE HEADLINE" in got["body"]
    assert "SECRET-ON-LINE-2" not in got["body"]
    assert "\n" not in got["body"]


def test_body_length_capped():
    # Safe prose (no 32+ char token, so nothing is redacted) must still be
    # length-capped so a very long alert line cannot flood the push channel.
    got = _send("word " * 1000)
    assert len(got["body"]) <= notify._PUSH_MAX_BODY
    assert got["body"].endswith("…")


def test_giant_token_redacted_not_just_capped():
    # A 5000-char unbroken token is a high-entropy blob -> redacted whole.
    got = _send("Z" * 5000)
    assert got["body"] == "[redacted]"


def test_no_newline_in_any_header():
    got = _send("body\nwith\nlines", title="title\nX-Evil: injected", tags="a\nb", priority="3\nX-Evil: y")
    for k, v in got["headers"].items():
        assert "\n" not in v and "\r" not in v, f"header {k} has a newline: {v!r}"
    assert "x-evil" not in got["headers"]


def test_title_capped():
    got = _send("b", title="T" * 500)
    assert len(got["headers"].get("x-title", "")) <= notify._PUSH_MAX_TITLE


# --- _terse never raises -----------------------------------------------------

@pytest.mark.parametrize("val", [None, 12345, b"\xff\xfe", "x" * 100000, "​\U0001f600", ["a", "b"], {"k": "v"}])
def test_terse_never_raises(val):
    out = notify._terse(val)
    assert isinstance(out, str)
    assert "\n" not in out


# --- fail-soft + ALERTS.md is the caller's job, not ours ---------------------

def test_unset_url_no_push(tmp_path):
    alerts = tmp_path / "ALERTS.md"
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop(notify.NTFY_ENV, None)
        rc = notify.push_alert(tmp_path, "t", "b")  # no url kwarg, env unset
    assert rc is None
    assert not alerts.exists()  # nothing written on the no-push path


def test_success_writes_nothing_to_alerts(tmp_path):
    alerts = tmp_path / "ALERTS.md"
    alerts.write_text("- pre-existing line kept by caller\n", encoding="utf-8")
    before = alerts.read_text(encoding="utf-8")
    _send("some alert body", root=str(tmp_path))
    # push_alert must not touch ALERTS.md on success — the caller owns that line.
    assert alerts.read_text(encoding="utf-8") == before


def test_failure_note_does_not_leak_topic(tmp_path):
    alerts = tmp_path / "ALERTS.md"

    def boom(req, timeout=None):
        raise OSError("connection refused")

    with mock.patch.object(notify.urllib.request, "urlopen", boom):
        rc = notify.push_alert(tmp_path, "t", "b", url=TOPIC)
    assert rc is None
    note = alerts.read_text(encoding="utf-8")
    assert "example-topic-0000000000000000" not in note  # note must not echo the topic


def test_safe_text_survives():
    got = _send("disk_free_gb=12 below floor 20 on host mw0001")
    assert "disk_free_gb=12" in got["body"]
    assert "below floor 20" in got["body"]


# --- red-team round 1 regressions (grok + gemini) ----------------------------

@pytest.mark.parametrize(
    "path",
    [
        "/Library/Keychains/login.keychain-db",
        "/run/secrets/stripe_api_key",
        "/.ssh/id_rsa",
        "/data/secrets/prod.key",
        "/Applications/SecretApp.app/Contents/creds.plist",
        "/System/Library/x",
        "/usr/local/etc/secret.conf",
    ],
)
def test_grok1_gemini4_nonallowlisted_abs_path_reduced(path):
    got = _send(f"cannot read {path} now")
    assert path not in got["body"], f"full path egressed: {got['body']!r}"


def test_grok2_pgp_private_key_block_redacted():
    body = "boom -----BEGIN PGP PRIVATE KEY BLOCK----- lQdGBGSecretKeyMaterial99 -----END PGP PRIVATE KEY BLOCK----- done"
    got = _send(body)
    assert "PGP PRIVATE KEY BLOCK" not in got["body"]
    assert "SecretKeyMaterial99" not in got["body"]


def test_grok3_private_key_underscore_kv_redacted():
    got = _send("config dump private_key=supersekrit99 loaded")
    assert "supersekrit99" not in got["body"]


def test_grok4_space_separated_labeled_secret_redacted():
    got = _send("provider rejected api-key live_sk_abcDEF12345 retry")
    assert "live_sk_abcDEF12345" not in got["body"]


def test_grok4_over_redaction_guard_prose_survives():
    # A SOFT label (auth/token) followed by an ordinary word must NOT be redacted.
    # (Hard labels like 'password'/'secret' DO redact to end-of-line by design.)
    for prose in ["auth failed for provider openai", "token bucket empty"]:
        got = _send(prose)
        assert "[redacted]" not in got["body"], f"over-redacted soft-label prose: {got['body']!r}"


@pytest.mark.parametrize(
    "secret",
    ["sk_live_2b17ccd23ce26ab2", "sk_test_ABCdef0123456789", "whsec_0123456789abcdefABCDEF", "pk_live_51HxxYYzz00"],
)
def test_gemini1_underscore_vendor_secret_redacted(secret):
    got = _send(f"stripe error with {secret} in call")
    assert secret not in got["body"], f"vendor secret egressed: {got['body']!r}"


def test_gemini2_url_basic_auth_credentials_stripped():
    got = _send("push to https://admin:my_secret_pwd@internal.net/hook failed")
    assert "my_secret_pwd" not in got["body"]
    assert "admin:" not in got["body"]


def test_gemini3_bare_topic_redacted_when_configured():
    # topic present in env, mentioned BARE (no scheme, <32 chars logic) in text
    topic_id = "example-topic-0000000000000000"
    captured: dict = {}

    def fake_urlopen(req, timeout=None):
        captured["body"] = (req.data or b"").decode("utf-8", "replace")
        return _FakeResp()

    with mock.patch.object(notify.urllib.request, "urlopen", fake_urlopen):
        with mock.patch.dict(os.environ, {notify.NTFY_ENV: TOPIC}):
            notify.push_alert("/tmp/nope", "t", f"republishing to {topic_id} now")
    assert topic_id not in captured["body"], f"bare topic egressed: {captured['body']!r}"


# --- red-team round 2 regressions (URL userinfo + topic redaction) -----------

def test_r2_multi_at_url_no_leak():
    # userinfo strip must consume to the LAST '@', so nothing between two '@'
    # survives as the "host".
    out = notify._terse("failed https://user:p@b:c@evil.example.com/api call")
    assert "b:c" not in out
    assert "user:p" not in out


def test_r2_slash_in_password_url_no_leak():
    out = notify._terse("failed https://bob:pa/ss@host.example.com/api call")
    assert "pa/ss" not in out
    assert "bob:pa" not in out


def test_r2_url_query_token_not_leaked():
    out = notify._terse("GET https://api.example.com/v1?access_token=SECRETtok12345 failed")
    assert "SECRETtok12345" not in out


def test_r2_topic_case_insensitive_redacted():
    topic_short = "SHORTTOPIC1"
    with mock.patch.dict(os.environ, {notify.NTFY_ENV: f"https://ntfy.sh/{topic_short}"}):
        out = notify._terse("mirror to shorttopic1 please")  # different case
    assert "shorttopic1" not in out.lower()


def test_r2_topic_truncated_prefix_redacted():
    # a 16-char truncated mention of the long topic must still be caught
    with mock.patch.dict(os.environ, {notify.NTFY_ENV: TOPIC}):
        out = notify._terse("see example-topic-00 for the feed")
    assert "example-topic-00" not in out


@pytest.mark.parametrize(
    "uri,leak",
    [
        ("postgres://app:SuperSecret99@db.internal:5432/prod", "SuperSecret99"),
        ("mysql://root:hunter2ABC@10.0.0.5/db", "hunter2ABC"),
        ("redis://:AuthPass123@cache.host:6379/0", "AuthPass123"),
        ("mongodb+srv://u:P%40ssw0rd@cluster0.mongodb.net/db", "P%40ssw0rd"),
        ("amqp://guest:guestpw99@broker:5672/vh", "guestpw99"),
        ("ftp://user:ftppw1234@files.example.com/", "ftppw1234"),
    ],
)
def test_r2_grok_nonhttp_scheme_creds_stripped(uri, leak):
    out = notify._terse(f"connect {uri} failed")
    assert leak not in out, f"non-http cred egressed: {out!r}"


def test_r2_grok_schemeless_userinfo_stripped():
    out = notify._terse("webhook admin:SuperSecret99@internal.net/hook down")
    assert "SuperSecret99" not in out
    assert "admin:" not in out


def test_r2_grok_plain_email_survives():
    # user@host with no ':' in the userinfo is NOT a credential — leave it alone
    out = notify._terse("alert from ops@example.com about disk")
    assert "ops@example.com" in out


@pytest.mark.parametrize("hard", ["password", "passwd", "secret", "api_key", "private_key", "credential", "bearer"])
def test_r2_grok_hard_label_redacts_short_value(hard):
    out = notify._terse(f"config {hard} letmein loaded")
    assert "letmein" not in out, f"hard-label value leaked for {hard}: {out!r}"


def test_r2_soft_label_prose_still_survives():
    for prose in ["auth failed for provider openai", "token bucket empty"]:
        out = notify._terse(prose)
        assert out.split()[1] != "[redacted]", f"soft label over-redacted: {out!r}"


# --- red-team round 3 regressions (credential authority + hard-label prose) ---

@pytest.mark.parametrize(
    "text,leak",
    [
        ("connect user:pa/ss@db.internal:5432 failed", "pa/ss"),          # gemini R3-1: '/' in pw
        ("auth user:P@ssword1@host rejected", "ssword1"),                 # grok R3-1: '@' in pw
        ("redis :hunter2@db.internal down", "hunter2"),                   # grok R3-3: empty user
        ("mirror user:pa/ss@http://proxy.com now", "pa/ss"),             # gemini R3-1 nested
    ],
)
def test_r3_credential_authority_no_leak(text, leak):
    out = notify._terse(text)
    assert leak not in out, f"credential leaked: {out!r}"


@pytest.mark.parametrize(
    "text,leak",
    [
        ("the password is supersecret99 for db", "supersecret99"),        # gemini R3-2
        ("password was hunter2ABC now", "hunter2ABC"),                    # grok R3-2 filler
        ("secret : is topsecretvalue", "topsecretvalue"),
        ("bearer token abc123XYZ expired", "abc123XYZ"),
    ],
)
def test_r3_hard_label_prose_redacted_to_eol(text, leak):
    out = notify._terse(text)
    assert leak not in out, f"hard-label value leaked: {out!r}"


def test_r3_plain_email_still_survives():
    assert "ops@example.com" in notify._terse("alert from ops@example.com re disk")


def test_r3_zero_width_split_secret_redacted():
    # a zero-width space inside a secret must not let it evade the shape patterns
    zwsp = "​"
    out = notify._terse(f"leaked sk{zwsp}-ABCDEFGH12345678 in log")
    assert "ABCDEFGH12345678" not in out


# --- red-team round 4 regressions (JSON-KV, soft-label filler, path spaces) ---

@pytest.mark.parametrize(
    "text,leak",
    [
        ('{"password":"letmein","user":"admin"}', "letmein"),            # grok R4-1
        ("err {'secret': 'topsecretval'}", "topsecretval"),
        ('{"token":"abc123XYZ","x":1}', "abc123XYZ"),
        ('config "api_key" : "sk-LIVE9988abcd"', "sk-LIVE9988abcd"),
    ],
)
def test_r4_json_quoted_kv_redacted(text, leak):
    out = notify._terse(text)
    assert leak not in out, f"JSON/quoted secret leaked: {out!r}"


@pytest.mark.parametrize(
    "text,leak",
    [
        ("the token is abc123XYZ999 yesterday", "abc123XYZ999"),          # gemini R4-1
        ("auth was supersecret99 in the db", "supersecret99"),
    ],
)
def test_r4_soft_label_filler_value_redacted(text, leak):
    out = notify._terse(text)
    assert leak not in out, f"soft-label filler value leaked: {out!r}"


def test_r4_soft_label_plain_prose_still_survives():
    # ordinary prose after a soft label (no secretish value) must NOT be redacted
    for prose in ["auth failed for provider openai", "token bucket empty", "token is set"]:
        out = notify._terse(prose)
        assert "[redacted]" not in out, f"over-redacted soft prose: {out!r}"


@pytest.mark.parametrize(
    "text,leaked_dir",
    [
        ("Loaded from /Volumes/My Great Files/output.csv now", "My Great Files"),   # gemini R4-3
        ("cannot write /Users/owner/Library/Application Support/App/config.json", "Application Support"),
        ("see /Users/bob/Google Drive/Secrets/keys.txt", "Google Drive"),
    ],
)
def test_r4_path_with_spaces_structure_not_leaked(text, leaked_dir):
    out = notify._terse(text)
    assert leaked_dir not in out, f"path directory structure leaked: {out!r}"


def test_r4_path_with_spaces_trailing_prose_preserved():
    # the basename stops at the first space so trailing prose isn't swallowed
    out = notify._terse("wrote /Users/owner/file and then it failed")
    assert "and then it failed" in out


# --- red-team round 5 regressions (pipeline order + soft-label shape detect) ---

def test_r5_grok_spaced_path_not_mutilated_by_high_entropy():
    # a realistic OSError with a spaced macOS path must reduce to basename, not
    # get its prefix eaten by _HIGH_ENTROPY leaving 'Support/app/config.yaml'.
    out = notify._terse(
        "OSError: No such file: /Users/youruser/Library/Application Support/app/config.yaml"
    )
    assert "Support/app/config.yaml" not in out
    assert "Application Support" not in out
    assert "config.yaml" in out  # basename stays actionable


@pytest.mark.parametrize(
    "text,leak",
    [
        ("the token is now abc123XYZ999secret", "abc123XYZ999secret"),   # gemini R5: double filler
        ("token set to abc123XYZ999secret", "abc123XYZ999secret"),       # verb not in old list
        ("auth updated to abc123XYZ999secret", "abc123XYZ999secret"),
        ("token has become abc123XYZ999secret", "abc123XYZ999secret"),   # grok R5 Probe E
    ],
)
def test_r5_soft_label_shape_detection_any_phrasing(text, leak):
    out = notify._terse(text)
    assert leak not in out, f"soft-label secret leaked despite phrasing: {out!r}"


def test_r5_soft_label_prose_still_survives_any_phrasing():
    for prose in [
        "auth failed for provider openai",
        "token bucket algorithm is set correctly now",
        "auth error rate limited, retry later",
        "token expired, re-auth needed",
    ]:
        out = notify._terse(prose)
        assert "[redacted]" not in out, f"over-redacted soft prose: {out!r}"


def test_r5_base64_secret_with_slashes_still_caught():
    # reordering must not weaken the high-entropy backstop for '/'-containing keys
    out = notify._terse("leaked wJalr/K7MDENG/bPxRfiCYEXAMPLEKEY1234 in log")
    assert "wJalr/K7MDENG/bPxRfiCYEXAMPLEKEY1234" not in out
