#!/usr/bin/env python3
"""Verify this system's access to each of the operator's mailboxes — the triage/reply foundation.

Reads ``configs/mailboxes.yaml`` and, for EACH account, checks READ and SEND access
*without sending anything or mutating any mailbox*:

    READ  Google : the OAuth refresh token still mints an access token.
          Titan  : IMAP4_SSL login + SELECT INBOX succeeds.
    SEND  Google : the granted scope set (returned by the token refresh) includes
                   ``gmail.send``.
          Titan  : SMTP_SSL login succeeds (send capability proven; no mail is sent).

Secrets are loaded from ``~/.config/omni/connections.env`` into the environment; their
VALUES are never printed. All failures are reduced to a short reason with no credential
in it.

Exit status: 0 iff every account is READ+SEND ready; otherwise 1. Use ``--json`` for a
machine-readable matrix, ``--only <id>`` to check a single mailbox.
"""

from __future__ import annotations

import argparse
import base64
import imaplib
import json
import os
import re
import smtplib
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - yaml ships in the repo venv
    print("PyYAML is required (run inside the repo venv: uv run python scripts/email/verify_mailboxes.py)")
    sys.exit(2)

REPO_ROOT = Path(__file__).resolve().parents[2]
REGISTRY = REPO_ROOT / "configs" / "mailboxes.yaml"
CONNECTIONS_ENV = Path.home() / ".config" / "omni" / "connections.env"
TOKEN_URL = "https://oauth2.googleapis.com/token"

OK = "OK"
MISSING_CRED = "missing-cred"
AUTH_FAIL = "auth-fail"
MISSING_SCOPE = "missing-scope"
WRONG_ACCOUNT = "wrong-account"
UNKNOWN = "unknown"

# RFC6749 + Google token-endpoint error codes. Anything outside this exact set is
# reported as http_<code>, never reflected verbatim — so a compromised/proxied endpoint
# cannot echo a token-shaped string (or any request parameter) into "safe" output.
_GOOGLE_OAUTH_ERRORS = frozenset({
    "invalid_request", "invalid_client", "invalid_grant", "unauthorized_client",
    "unsupported_grant_type", "invalid_scope", "access_denied", "server_error",
    "temporarily_unavailable", "admin_policy_enforced", "invalid_token", "expired_token",
})

# Exact scope URLs (matched by set membership, never substring: "mail.google.com" in a
# scope string would also match a look-alike like https://mail.google.com.evil/).
_GMAIL_FULL = "https://mail.google.com/"
_GMAIL_READ_SCOPES = frozenset({
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
    _GMAIL_FULL,
})
_GMAIL_SEND_SCOPES = frozenset({"https://www.googleapis.com/auth/gmail.send", _GMAIL_FULL})

# An email claim (from the UNSIGNED id_token) or a configured address is only ever
# surfaced if it is a bounded, email-SHAPED value — so a compromised endpoint cannot
# reflect a token/secret-shaped string through the email field into output.
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


def load_connections_env(path: Path) -> int:
    """Load KEY=VALUE lines from connections.env into os.environ (values never printed)."""
    if not path.exists():
        return 0
    loaded = 0
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key and key not in os.environ:  # never clobber an explicitly-exported value
            os.environ[key] = value.strip().strip('"').strip("'")
            loaded += 1
    return loaded


def _env_present(*keys: str) -> list[str]:
    return [k for k in keys if not os.environ.get(k)]


def _resolve_port(env_name: str, default: int) -> tuple[int | None, str | None]:
    """Return (port, None) or (None, error). A PRESENT env var must be an ASCII-digit port
    in 1..65535 — str.isdigit() alone accepts non-ASCII digits ("٤٤٣") and superscripts
    ("²") that then raise in int(); absent -> default (never a silent default on garbage)."""
    if env_name and env_name in os.environ:
        cleaned = os.environ[env_name].strip()
        if not (cleaned.isascii() and cleaned.isdigit()) or not (1 <= int(cleaned) <= 65535):
            return None, f"invalid port in {env_name}"
        return int(cleaned), None
    return default, None


def _email_from_id_token(id_token: str) -> str | None:
    """Extract the (lowercased) email claim from an OIDC id_token, or None. Decodes the
    JWT payload segment only — enough to catch a wrong-account mint; not a trust check.
    Only an email-SHAPED, bounded value is returned: the token is unsigned, so an arbitrary
    reflected claim (a token/secret) must not flow through into `detail`/output."""
    try:
        segment = id_token.split(".")[1]
        segment += "=" * (-len(segment) % 4)  # restore base64 padding
        claims = json.loads(base64.urlsafe_b64decode(segment))
        email = claims.get("email")
    except Exception:  # noqa: BLE001
        return None
    if isinstance(email, str):
        email = email.strip().lower()
        if len(email) <= 254 and _EMAIL_RE.match(email):
            return email
    return None


def _refresh_google_token(mb: dict[str, Any]) -> tuple[str, str, str | None, str | None]:
    """Return (status, raw_scope, email, error). status is OK/MISSING_CRED/AUTH_FAIL."""
    creds = mb.get("credentials", {})
    rt_env = creds.get("refresh_token_env", "")
    cid_env = creds.get("client_id_env", "")
    sec_env = creds.get("client_secret_env", "")
    missing = _env_present(rt_env, cid_env, sec_env)
    if missing:
        return MISSING_CRED, "", None, f"missing {missing[0]}"
    data = urllib.parse.urlencode(
        {
            "client_id": os.environ[cid_env],
            "client_secret": os.environ[sec_env],
            "refresh_token": os.environ[rt_env],
            "grant_type": "refresh_token",
        }
    ).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(TOKEN_URL, data=data), timeout=20) as resp:
            payload = json.load(resp)
    except urllib.error.HTTPError as exc:
        # Google returns {"error": "invalid_grant", ...}. Surface the code ONLY if it is a
        # known OAuth error token; otherwise http_<code>. Never str() an arbitrary JSON
        # value — a compromised/proxied endpoint could otherwise reflect a request
        # parameter (client_secret/refresh_token) into "safe" output.
        code = f"http_{exc.code}"
        try:
            err = json.loads(exc.read().decode()).get("error")
            if isinstance(err, str) and err in _GOOGLE_OAUTH_ERRORS:
                code = err
        except Exception:  # noqa: BLE001
            pass
        return AUTH_FAIL, "", None, code
    except Exception as exc:  # noqa: BLE001 — network/other; type only
        return AUTH_FAIL, "", None, type(exc).__name__
    raw_scope = str(payload.get("scope", ""))
    email = _email_from_id_token(str(payload.get("id_token", "")))
    return OK, raw_scope, email, None


def check_google(mb: dict[str, Any]) -> dict[str, str]:
    status, raw_scope, email, error = _refresh_google_token(mb)
    if status == MISSING_CRED:
        return {"read": MISSING_CRED, "send": MISSING_CRED, "detail": error or ""}
    if status == AUTH_FAIL:
        return {"read": AUTH_FAIL, "send": AUTH_FAIL, "detail": error or ""}
    # A registry entry with no address can't be identity-verified; a missing config value
    # must not read as verified-healthy (favourable absence) — flag it as unverifiable.
    want = str(mb.get("address", "")).strip().lower()
    if not want:
        return {"read": UNKNOWN, "send": UNKNOWN, "detail": "registry entry has no address — cannot verify"}
    # The minted token must belong to the address this entry claims — otherwise a
    # mis-consented OAuth (signed in as the wrong Google account) would read as OK.
    if email and email != want:
        return {
            "read": WRONG_ACCOUNT,
            "send": WRONG_ACCOUNT,
            "detail": f"token is for {email}, not {want} — re-mint as the correct account",
        }
    # Read/send require an actually-granted scope, by EXACT scope-URL membership (not a
    # minted token, and not a substring match). Read: gmail.readonly|modify|full; send:
    # gmail.send|full.
    granted = set(raw_scope.split())
    read_ok = bool(granted & _GMAIL_READ_SCOPES)
    send_ok = bool(granted & _GMAIL_SEND_SCOPES)
    if not read_ok and not send_ok:
        detail = "token lacks gmail read and send scopes — re-mint with both"
    elif not send_ok:
        detail = "token lacks gmail.send — re-mint with send scope"
    elif not read_ok:
        detail = "token lacks gmail.readonly — re-mint with read scope"
    elif not email:
        detail = "scopes OK; identity unverified (no id_token)"
    else:
        detail = ""
    return {"read": OK if read_ok else MISSING_SCOPE, "send": OK if send_ok else MISSING_SCOPE, "detail": detail}


def check_titan(mb: dict[str, Any]) -> dict[str, str]:
    creds = mb.get("credentials", {})
    send_cfg = mb.get("send", {})
    result = {"read": UNKNOWN, "send": UNKNOWN, "detail": ""}

    # READ — IMAP login + SELECT INBOX
    imap_missing = _env_present(
        creds.get("imap_host_env", ""), creds.get("imap_user_env", ""), creds.get("imap_password_env", "")
    )
    if imap_missing:
        result["read"] = MISSING_CRED
        result["detail"] = f"missing {imap_missing[0]}"
    else:
        host = os.environ[creds["imap_host_env"]]
        try:
            conn = imaplib.IMAP4_SSL(host, ssl_context=ssl.create_default_context())
            try:
                conn.login(os.environ[creds["imap_user_env"]], os.environ[creds["imap_password_env"]])
                typ, _ = conn.select("INBOX", readonly=True)
                result["read"] = OK if typ == "OK" else AUTH_FAIL
            finally:
                try:
                    conn.logout()
                except Exception:  # noqa: BLE001
                    pass
        except Exception as exc:  # noqa: BLE001 — type only, never the credential
            result["read"] = AUTH_FAIL
            result["detail"] = type(exc).__name__

    # SEND — SMTP login only (capability proof; nothing is sent)
    smtp_missing = _env_present(
        send_cfg.get("host_env", ""), send_cfg.get("user_env", ""), send_cfg.get("password_env", "")
    )
    if smtp_missing:
        result["send"] = MISSING_CRED
        if not result["detail"]:
            result["detail"] = f"missing {smtp_missing[0]}"
    else:
        host = os.environ[send_cfg["host_env"]]
        port, port_err = _resolve_port(send_cfg.get("port_env", ""), 465)
        if port_err:
            result["send"] = AUTH_FAIL
            if not result["detail"]:
                result["detail"] = port_err
            return result
        try:
            with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=20) as smtp:
                smtp.login(os.environ[send_cfg["user_env"]], os.environ[send_cfg["password_env"]])
            result["send"] = OK
        except Exception as exc:  # noqa: BLE001 — type only
            result["send"] = AUTH_FAIL
            if not result["detail"]:
                result["detail"] = type(exc).__name__
    return result


def check_mailbox(mb: dict[str, Any]) -> dict[str, str]:
    provider = mb.get("provider")
    if provider == "google":
        return check_google(mb)
    if provider == "titan":
        return check_titan(mb)
    return {"read": UNKNOWN, "send": UNKNOWN, "detail": f"unknown provider {provider!r}"}


_GLYPH = {OK: "✅", MISSING_CRED: "⛔", AUTH_FAIL: "❌", MISSING_SCOPE: "⚠️ ", WRONG_ACCOUNT: "🚫", UNKNOWN: "❔"}


def main() -> int:
    ap = argparse.ArgumentParser(description="Verify mailbox access for the triage/reply foundation.")
    ap.add_argument("--json", action="store_true", help="emit a JSON matrix")
    ap.add_argument("--only", metavar="ID", help="check a single mailbox id")
    args = ap.parse_args()

    load_connections_env(CONNECTIONS_ENV)
    if not REGISTRY.exists():
        print(f"registry not found: {REGISTRY}")
        return 2
    registry = yaml.safe_load(REGISTRY.read_text()) or {}
    mailboxes = registry.get("mailboxes", [])
    if args.only:
        mailboxes = [m for m in mailboxes if m.get("id") == args.only]
        if not mailboxes:
            print(f"no mailbox with id {args.only!r}")
            return 2

    rows = []
    for mb in mailboxes:
        res = check_mailbox(mb)
        ready = res["read"] == OK and res["send"] == OK
        rows.append({"id": mb.get("id"), "address": mb.get("address"), "provider": mb.get("provider"), **res, "ready": ready})
    # An empty set is NOT "ready" — a registry parse issue or bad filter must never read as
    # healthy through the favourable absence of any rows to check.
    all_ready = bool(rows) and all(r["ready"] for r in rows)

    if args.json:
        print(json.dumps({"ready": all_ready, "mailboxes": rows}, indent=2))
        return 0 if all_ready else 1

    print("Mailbox access — email triage/reply foundation")
    print(f"  registry: {REGISTRY}")
    print("")
    print(f"  {'ID':<12} {'ADDRESS':<38} {'READ':<7} {'SEND':<7} DETAIL")
    print(f"  {'-'*12} {'-'*38} {'-'*7} {'-'*7} {'-'*30}")
    for r in rows:
        print(
            f"  {r['id']:<12} {r['address']:<38} "
            f"{_GLYPH.get(r['read'],'?')}{r['read']:<5} {_GLYPH.get(r['send'],'?')}{r['send']:<5} {r['detail']}"
        )
    print("")
    ready_n = sum(1 for r in rows if r["ready"])
    print(f"  {ready_n}/{len(rows)} mailboxes READ+SEND ready.")
    if not all_ready:
        print("  Next: see docs/operations/email-access-setup.md for the per-account setup steps.")
    return 0 if all_ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
