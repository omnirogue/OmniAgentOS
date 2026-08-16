#!/usr/bin/env python3
"""alert.py — the durable alert transport for capmap (mission gobig-capmap-0815, W2).

WHY THIS EXISTS
---------------
Detection already worked. Delivery was the hole. ~/.omniagentos/ops/bin/integrations-probe.py
runs every 15 minutes under launchd, probes 9 integrations and fires on state
transitions -- but it alerted through notify-owner.sh, which delivers an `osascript
display notification` banner. If the operator was not looking at this Mac in that second, the
alert was gone forever. A multi-day helpdesk outage can produce exactly one
banner, days ago, and the operator was never told.

So every design choice here answers one question: can this path lose a message?

  * DURABLE      -- Slack DM first, then an ops webhook, then ntfy push. Three
                    independent carriers; the first that succeeds wins.
  * VERIFIABLE   -- every attempt on every channel appends to alerts.jsonl. The
                    ledger is append-only and is written before the process can exit.
  * NEVER SILENT -- if all channels fail, that is itself written to the ledger with
                    an UNDELIVERED marker and printed to stderr, and the incident is
                    NOT marked notified, so the next cadence retries it. An
                    undeliverable alert is a louder event than a delivered one.
  * NEVER NOISY  -- one alert per incident, not per run. An incident opens on a
                    transition into a bad state and closes on return to good.
                    A long outage re-notifies once per escalation interval (24h).
  * NEVER BLIND  -- check_liveness() is a dead-man's switch: if the checker's own
                    last-run stamp is older than N cadences, that fires an alert.
                    Silence must never read as healthy. It reads a timestamp file
                    and nothing else, so it works when the checker is dead and can
                    be driven by an independent launchd job.
  * PROVABLE     -- digest() emits one line a day so "no alerts" is evidence that
                    checking happened, not an assumption.

SAFETY GATES (deliberate, read before removing)
-----------------------------------------------
1. ARM FILE. Real delivery requires ~/.omniagentos/ops/var/capmap/ARMED to exist. Until the operator
   creates it (`alert.py --arm`), every "real" send degrades to a rendered dry run
   recorded as suppressed=not_armed. Sending the first real message to the operator is a
   pre-declared pause in the mission contract that belongs to the operator, and a flag that
   defaults to "do not page a human" is the only version of that pause a program can
   actually enforce.
2. SEED MODE. --seed-only establishes baseline state without sending anything, so
   first contact with a large capability set cannot page the operator N times.
3. SELFTESTS DRY-RUN BY DEFAULT. --selftest-* render and never deliver unless --live.
4. NO CREDENTIAL EVER LEAVES. Credentials are read from connections.env by NAME and
   held only in memory. Every string that reaches a message, the ledger or stdout is
   passed through _redact(), which replaces any known secret value (and anything
   token-shaped) with "<redacted:KEY>". Tokens are never printed, not even truncated.

stdlib only, absolute paths only: this runs under launchd's stripped environment.
"""

import argparse
import datetime
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

HOME = "/Users/youruser"
ENV_FILE = HOME + "/.config/omni/connections.env"
STATE_DIR = HOME + "/Work/Ops/var/capmap"
INCIDENTS_FILE = STATE_DIR + "/incidents.json"
ALERTS_LEDGER = STATE_DIR + "/alerts.jsonl"
ARM_FILE = STATE_DIR + "/ARMED"
SELFTEST_DIR = STATE_DIR + "/selftest"   # selftests never touch production state

# Default liveness source: the existing probe's snapshot and the field it stamps.
DEFAULT_HEARTBEAT_FILE = HOME + "/Work/Ops/var/integrations-health.json"
DEFAULT_HEARTBEAT_FIELD = "probe_ran_at"
DEFAULT_CADENCE_SECONDS = 900          # com.owner.integrations-probe StartInterval
DEFAULT_MAX_CADENCES = 2               # stale after 2 missed cadences
DEFAULT_ESCALATE_AFTER = 24 * 3600     # re-notify an still-open incident once a day

HTTP_TIMEOUT = 10
USER_AGENT = "omni-capmap-alert/1.0 (+estate-alert-transport)"

# Status vocabulary is shared with integrations-probe.py. Anything not GOOD is an
# incident-worthy state; a status we have never seen before is treated as BAD on
# purpose -- an unknown state must not read as healthy.
GOOD_STATUSES = {"UP", "OK", "GREEN", "HEALTHY", "PASS"}

CHANNEL_SLACK_DM = "slack_dm"
CHANNEL_SLACK_WEBHOOK = "slack_webhook"
CHANNEL_NTFY = "ntfy"

UNDELIVERED_MARKER = "ALERT_UNDELIVERED"

# Token shapes that must never reach a log, a message or the ledger even if they
# arrive from somewhere we did not load (e.g. a probe detail echoing a URL).
_TOKEN_SHAPES = re.compile(
    r"(xox[abposr]-[A-Za-z0-9-]{8,}"
    r"|https://hooks\.slack\.com/\S+"
    r"|sk-[A-Za-z0-9_-]{12,}"
    r"|ghp_[A-Za-z0-9]{20,}"
    r"|AKIA[0-9A-Z]{12,}"
    r"|Bearer\s+[A-Za-z0-9._-]{12,})"
)

# Key names whose VALUES are credentials. Used with _is_secret_value() below.
_SECRETISH_KEY = re.compile(
    r"(TOKEN|SECRET|PASSWORD|PASSWD|_KEY|KEY_|APIKEY|CREDENTIAL|WEBHOOK"
    r"|NTFY_URL|SESSION|COOKIE|PRIVATE|SIGNING|SALT|AUTH)", re.I)


# --------------------------------------------------------------------------- utils

def now_utc():
    return datetime.datetime.now(datetime.timezone.utc)


def iso(dt):
    return dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text):
    """Parse the estate's UTC stamp format. Returns None if unparseable.

    None is deliberately NOT 'fresh': callers treat an unreadable heartbeat as a
    dead checker, because an unparseable timestamp is exactly what a half-written
    or corrupted state file looks like."""
    if not text:
        return None
    try:
        cleaned = str(text).strip().replace("Z", "+00:00")
        dt = datetime.datetime.fromisoformat(cleaned)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone(datetime.timezone.utc)
    except (ValueError, TypeError):
        return None


def load_env(path=ENV_FILE):
    """Read connections.env directly. launchd hands us a stripped environment, so
    inheriting these from the process env is not an option."""
    env = {}
    try:
        with open(path, errors="ignore") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                env[key.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        pass
    return env


def _is_secret_value(key, value):
    """Decide whether an env pair is a credential worth blanking out of text.

    This is deliberately narrow. connections.env holds ~460 entries and many are
    ordinary words: the first live dry run redacted the brand name out of
    "freshdesk-initech" because INITECH_EVAL_LOCAL_POSTGRES_USER happens to be
    "initech". Over-redaction is not a safe default -- it mangles the very alert
    text the operator has to act on. So a value is redacted only when it is credential-
    shaped: long, whitespace-free, and either under a credential-shaped key or
    long-and-mixed-character enough that it cannot be an ordinary word."""
    if not value or len(value) < 12 or any(ch.isspace() for ch in value):
        return False
    if value.isalpha():                    # a plain word is a name, never a secret
        return False
    if _SECRETISH_KEY.search(key or ""):
        return True
    return (len(value) >= 20 and any(ch.isdigit() for ch in value)
            and any(ch.isalpha() for ch in value))


def _redact(text, secrets=None):
    """Replace credential values with <redacted:KEY> and mask token-shaped strings.

    Longest values first, so a secret that contains another secret as a substring
    cannot leave a fragment behind."""
    out = "" if text is None else str(text)
    for key, value in sorted((secrets or {}).items(), key=lambda kv: -len(kv[1] or "")):
        if _is_secret_value(key, value) and value in out:
            out = out.replace(value, f"<redacted:{key}>")
    return _TOKEN_SHAPES.sub("<redacted:token-shape>", out)


def is_good(status):
    return str(status or "").strip().upper() in GOOD_STATUSES


def human_duration(seconds):
    seconds = int(max(0, seconds))
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds // 60}m"
    if seconds < 172800:
        return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"
    return f"{seconds // 86400}d{(seconds % 86400) // 3600}h"


def atomic_write_json(path, payload):
    """tmp + fsync + os.replace. A torn incidents.json would either resurrect a
    closed incident (spurious page) or hide an open one (missed page)."""
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w") as fh:
        json.dump(payload, fh, indent=1, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


# ------------------------------------------------------------------------ message

class Message:
    """One rendered alert, carrying: what broke, since when, blast radius, action."""

    def __init__(self, capability, from_status, to_status, detail="",
                 blast_radius="", remedy="", since=None, kind="TRANSITION",
                 ongoing_for=None):
        self.capability = capability
        self.from_status = from_status or "UNKNOWN"
        self.to_status = to_status
        self.detail = detail or ""
        self.blast_radius = blast_radius or "unknown — not declared for this capability"
        self.remedy = remedy or "investigate manually; no remedy declared"
        self.since = since or now_utc()
        self.kind = kind
        self.ongoing_for = ongoing_for

    @property
    def transition(self):
        return f"{self.from_status}->{self.to_status}"

    def title(self):
        if self.kind == "RECOVERY":
            return f"RECOVERED: {self.capability} is {self.to_status}"
        if self.kind == "STALE_CHECKER":
            return f"STALE_CHECKER: {self.capability} has stopped checking"
        if self.kind == "ONGOING":
            return f"STILL DOWN: {self.capability} {self.to_status}"
        return f"{self.capability} {self.transition}"

    def render(self, secrets=None):
        age = human_duration((now_utc() - self.since).total_seconds())
        icon = {"RECOVERY": ":white_check_mark:", "STALE_CHECKER": ":skull:",
                "ONGOING": ":hourglass:"}.get(self.kind, ":rotating_light:")
        lines = [
            f"{icon} *{self.title()}*",
            "*what:* {} {}{}".format(self.capability, self.transition,
                                 (f" ({self.detail})") if self.detail else ""),
            f"*since:* {iso(self.since)} ({age} ago)",
            f"*blast radius:* {self.blast_radius}",
            f"*recommended:* {self.remedy}",
        ]
        if self.kind == "ONGOING" and self.ongoing_for:
            lines.insert(1, f"_still open after {human_duration(self.ongoing_for)} — this is a reminder, not a new incident_")
        return _redact("\n".join(lines), secrets)

    def to_dict(self, secrets=None):
        return {
            "capability": self.capability,
            "transition": self.transition,
            "kind": self.kind,
            "detail": _redact(self.detail, secrets),
            "since": iso(self.since),
            "blast_radius": _redact(self.blast_radius, secrets),
            "remedy": _redact(self.remedy, secrets),
        }


# ------------------------------------------------------------------------ channels

class ChannelError(Exception):
    """Delivery failed on one channel. Carries a redacted, loggable reason."""


class Channel:
    """A carrier. deliver() returns a receipt dict on success, raises ChannelError
    on failure. Channels never write state and never decide policy -- the Alerter
    owns the fallback order, so a channel cannot silently swallow a failure."""

    name = "channel"

    def deliver(self, message, text):
        raise NotImplementedError


def _http(url, data=None, headers=None, method=None, timeout=HTTP_TIMEOUT):
    headers = dict(headers or {})
    headers.setdefault("User-Agent", USER_AGENT)
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=timeout,
                                context=ssl.create_default_context()) as response:
        return response.status, response.read().decode("utf-8", "replace")


class SlackDMChannel(Channel):
    """Primary carrier: a Slack DM via chat.postMessage with the bot token.

    Slack answers HTTP 200 with {"ok": false, "error": "..."} for most real
    failures, so a status-code-only check would report an invalid_auth as a
    delivered alert. We parse the body and fail on ok=false."""

    name = CHANNEL_SLACK_DM
    API = "https://slack.com/api/"

    def __init__(self, token, target=None, email=None, team_id=None):
        self.token = token
        self.target = target        # channel id (C…/D…/G…) or user id (U…/W…)
        self.email = email
        self.team_id = team_id

    def _call(self, method, payload):
        body = json.dumps(payload).encode()
        headers = {"Authorization": "Bearer " + self.token,
                   "Content-Type": "application/json; charset=utf-8"}
        try:
            _status, raw = _http(self.API + method, data=body, headers=headers)
        except (urllib.error.URLError, OSError) as exc:
            raise ChannelError(f"{method}: {type(exc).__name__}") from None
        try:
            parsed = json.loads(raw)
        except ValueError:
            raise ChannelError(f"{method}: unparseable response") from None
        if not parsed.get("ok"):
            raise ChannelError("{}: {}".format(method, parsed.get("error", "unknown")))
        return parsed

    def _get(self, method, params):
        query = self.API + method + "?" + urllib.parse.urlencode(params)
        headers = {"Authorization": "Bearer " + self.token}
        try:
            _status, raw = _http(query, headers=headers, method="GET")
        except (urllib.error.URLError, OSError) as exc:
            raise ChannelError(f"{method}: {type(exc).__name__}") from None
        try:
            parsed = json.loads(raw)
        except ValueError:
            raise ChannelError(f"{method}: unparseable response") from None
        if not parsed.get("ok"):
            raise ChannelError("{}: {}".format(method, parsed.get("error", "unknown")))
        return parsed

    def resolve_target(self):
        """Where the DM goes, in declared order. Kept separate from deliver() so
        --dry-run can print the resolution *plan* without touching the network."""
        target = self.target
        if target and target[0] in "CDG":
            return target
        if not target and self.email:
            target = self._get("users.lookupByEmail", {"email": self.email})["user"]["id"]
        if not target:
            raise ChannelError("no Slack target: set CAPMAP_SLACK_TARGET or an email")
        opened = self._call("conversations.open", {"users": target})
        return opened["channel"]["id"]

    def plan(self):
        if self.target and self.target[0] in "CDG":
            return f"post to channel {self.target}"
        if self.target:
            return f"conversations.open(users={self.target}) then post"
        return "users.lookupByEmail(%s) -> conversations.open -> post" % (self.email or "?")

    def deliver(self, message, text):
        channel = self.resolve_target()
        result = self._call("chat.postMessage", {
            "channel": channel, "text": text, "unfurl_links": False,
            "unfurl_media": False,
        })
        return {"channel_id": channel, "ts": result.get("ts"), "team": self.team_id}


class SlackWebhookChannel(Channel):
    """First fallback: OPS_ALERT_SLACK_WEBHOOK_URL. No scopes, no user lookup —
    it survives a token that has been revoked or de-scoped, which is the most
    likely way the primary dies."""

    name = CHANNEL_SLACK_WEBHOOK

    def __init__(self, url):
        self.url = url

    def plan(self):
        return "POST to OPS_ALERT_SLACK_WEBHOOK_URL (value never printed)"

    def deliver(self, message, text):
        body = json.dumps({"text": text}).encode()
        try:
            status, raw = _http(self.url, data=body,
                                headers={"Content-Type": "application/json"})
        except urllib.error.HTTPError as exc:
            raise ChannelError(f"webhook http {exc.code}") from None
        except (urllib.error.URLError, OSError) as exc:
            raise ChannelError(f"webhook {type(exc).__name__}") from None
        if status >= 300 or raw.strip() not in ("ok", ""):
            raise ChannelError(f"webhook http {status}: {raw.strip()[:60]}")
        return {"http": status}


class NtfyChannel(Channel):
    """Last resort: OMNI_NTFY_URL push. Reaches the operator's phone when Slack itself is
    the thing that is down — the carrier of last resort must not share a vendor
    with the two above it."""

    name = CHANNEL_NTFY

    def __init__(self, url):
        self.url = url

    def plan(self):
        return "POST plaintext to OMNI_NTFY_URL (value never printed)"

    def deliver(self, message, text):
        headers = {
            "Title": message.title()[:120],
            "Priority": "default" if message.kind == "RECOVERY" else "high",
            "Tags": "warning" if message.kind != "RECOVERY" else "white_check_mark",
            "Content-Type": "text/plain; charset=utf-8",
        }
        plain = re.sub(r"[*_]", "", text)
        try:
            status, _raw = _http(self.url, data=plain.encode(), headers=headers)
        except urllib.error.HTTPError as exc:
            raise ChannelError(f"ntfy http {exc.code}") from None
        except (urllib.error.URLError, OSError) as exc:
            raise ChannelError(f"ntfy {type(exc).__name__}") from None
        if status >= 300:
            raise ChannelError(f"ntfy http {status}")
        return {"http": status}


class DryRunChannel(Channel):
    """Renders, records, never touches the network. Used by --dry-run, by the
    not-armed gate, and as the safe default for selftests."""

    name = "dry_run"

    def __init__(self, sink=None, wrapping=None):
        self.sink = sink if sink is not None else []
        self.wrapping = wrapping or []

    def plan(self):
        return "DRY RUN (would have tried: %s)" % (
            ", ".join(c.name for c in self.wrapping) or "no live channel configured")

    def deliver(self, message, text):
        self.sink.append(text)
        return {"dry_run": True, "would_use": [c.name for c in self.wrapping]}


# ------------------------------------------------------------------------- alerter

class Alerter:
    """Owns policy: dedupe, fallback order, ledger, dead-man's switch.

    Channels are injected. Tests construct an Alerter with fake channels, which is
    why nothing here monkeypatches urllib -- there is no global to un-patch and no
    path by which a test can accidentally reach the network."""

    def __init__(self, channels=None, incidents_path=INCIDENTS_FILE,
                 ledger_path=ALERTS_LEDGER, secrets=None, seed_only=False,
                 escalate_after=DEFAULT_ESCALATE_AFTER, notify_recovery=True,
                 clock=None):
        self.channels = list(channels or [])
        self.incidents_path = incidents_path
        self.ledger_path = ledger_path
        self.secrets = secrets or {}
        self.seed_only = seed_only
        self.escalate_after = escalate_after
        self.notify_recovery = notify_recovery
        self.clock = clock or now_utc
        self._state = None

    # ---- state -----------------------------------------------------------

    def state(self):
        if self._state is None:
            try:
                with open(self.incidents_path) as fh:
                    loaded = json.load(fh)
            except (OSError, ValueError):
                loaded = {}
            if not isinstance(loaded, dict):
                loaded = {}
            loaded.setdefault("version", 1)
            loaded.setdefault("open", {})
            loaded.setdefault("last_status", {})
            self._state = loaded
        return self._state

    def save(self):
        state = self.state()
        state["updated_at"] = iso(self.clock())
        atomic_write_json(self.incidents_path, state)

    # ---- ledger ----------------------------------------------------------

    def _ledger(self, row):
        """Append-only, one JSON object per line, opened "a" and flushed+fsynced.
        This file is the only proof that an alert was attempted; it is never
        rewritten and never truncated."""
        row = dict(row)
        row.setdefault("ts", iso(self.clock()))
        line = json.dumps(row, sort_keys=True)
        try:
            os.makedirs(os.path.dirname(self.ledger_path), exist_ok=True)
            with open(self.ledger_path, "a") as fh:
                fh.write(line + "\n")
                fh.flush()
                os.fsync(fh.fileno())
        except OSError as exc:
            # The ledger itself failing is the loudest possible failure: say so on
            # stderr rather than losing the record silently.
            sys.stderr.write(f"capmap-alert: LEDGER WRITE FAILED ({type(exc).__name__}): {line}\n")
        return row

    # ---- delivery --------------------------------------------------------

    def deliver(self, message):
        """Walk the fallback chain. Returns (ok, attempts).

        Every attempt is ledgered, including the ones that failed before a later
        channel succeeded -- a fallback that hides the primary's failure would let
        Slack rot undetected until the day the fallback also fails."""
        text = message.render(self.secrets)
        attempts = []

        if self.seed_only:
            attempts.append(self._ledger({
                "capability": message.capability, "transition": message.transition,
                "kind": message.kind, "channel": "none", "ok": True,
                "error": None, "suppressed": "seed_only",
            }))
            return True, attempts

        if not self.channels:
            attempts.append(self._ledger({
                "capability": message.capability, "transition": message.transition,
                "kind": message.kind, "channel": "none", "ok": False,
                "error": "no channels configured", "marker": UNDELIVERED_MARKER,
                "undelivered": True, "text": text,
            }))
            sys.stderr.write(f"capmap-alert: {UNDELIVERED_MARKER} {message.capability} — NO CHANNELS CONFIGURED\n")
            return False, attempts

        for channel in self.channels:
            try:
                receipt = channel.deliver(message, text)
                attempts.append(self._ledger({
                    "capability": message.capability, "transition": message.transition,
                    "kind": message.kind, "channel": channel.name, "ok": True,
                    "error": None, "receipt": _json_safe(receipt),
                }))
                return True, attempts
            except Exception as exc:            # a carrier must never crash the run
                reason = _redact(f"{type(exc).__name__}: {exc}", self.secrets)
                attempts.append(self._ledger({
                    "capability": message.capability, "transition": message.transition,
                    "kind": message.kind, "channel": channel.name, "ok": False,
                    "error": reason,
                }))

        # Every channel failed. This is the failure mode the whole unit exists to
        # prevent, so it is recorded with a marker, shouted on stderr, and the
        # caller leaves the incident un-notified so the next cadence retries.
        attempts.append(self._ledger({
            "capability": message.capability, "transition": message.transition,
            "kind": message.kind, "channel": "ALL", "ok": False,
            "error": "all channels failed", "marker": UNDELIVERED_MARKER,
            "undelivered": True, "tried": [c.name for c in self.channels],
            "text": text,
        }))
        sys.stderr.write("capmap-alert: {} {} {} — every channel failed ({})\n".format(UNDELIVERED_MARKER, message.capability, message.transition,
                            ", ".join(c.name for c in self.channels)))
        return False, attempts

    # ---- the public entry point -----------------------------------------

    def send(self, capability, from_status, to_status, detail="", blast_radius="",
             remedy="", kind=None, since=None):
        """Record a transition and page the operator if — and only if — it is news.

        Returns a result dict: {action, delivered, attempts, incident}.
        action is one of: alerted · suppressed_duplicate · escalated · recovered ·
        closed_silently · seeded · no_incident.
        """
        state = self.state()
        now = self.clock()
        open_incidents = state["open"]
        state["last_status"][capability] = {"status": to_status, "at": iso(now)}
        incident = open_incidents.get(capability)

        # --- transition back to good: close the incident -------------------
        if kind is None and is_good(to_status):
            if not incident:
                self.save()
                return {"action": "no_incident", "delivered": True, "attempts": [],
                        "incident": None}
            was_notified = bool(incident.get("last_notified_at"))
            opened = parse_iso(incident.get("opened_at")) or now
            del open_incidents[capability]
            state.setdefault("closed_recent", {})[capability] = {
                "opened_at": incident.get("opened_at"),
                "closed_at": iso(now),
                "notified": was_notified,
            }
            if self.seed_only or not (was_notified and self.notify_recovery):
                # Never sent about it, so there is nothing for the operator to un-worry about.
                self.save()
                return {"action": "seeded" if self.seed_only else "closed_silently",
                        "delivered": True, "attempts": [], "incident": incident}
            message = Message(capability, incident.get("to_status", "DOWN"), to_status,
                              detail=detail or f"recovered after {human_duration((now - opened).total_seconds())}",
                              blast_radius=incident.get("blast_radius", ""),
                              remedy="no action needed — confirming the recovery",
                              since=opened, kind="RECOVERY")
            ok, attempts = self.deliver(message)
            self.save()
            return {"action": "recovered", "delivered": ok, "attempts": attempts,
                    "incident": incident}

        # --- transition into (or continuation of) a bad state ---------------
        if incident is None:
            incident = {
                "capability": capability,
                "opened_at": iso(since or now),
                "from_status": from_status,
                "to_status": to_status,
                "detail": _redact(detail, self.secrets),
                "blast_radius": blast_radius,
                "remedy": remedy,
                "kind": kind or "TRANSITION",
                "notify_count": 0,
                "delivery_failures": 0,
                "last_notified_at": None,
            }
            open_incidents[capability] = incident
            new_incident = True
        else:
            new_incident = False
            incident["to_status"] = to_status
            incident["detail"] = _redact(detail, self.secrets) or incident.get("detail", "")

        opened = parse_iso(incident["opened_at"]) or now
        last_notified = parse_iso(incident.get("last_notified_at"))

        if self.seed_only:
            incident["seeded"] = True
            # Remember WHAT was seeded. The next run must stay silent while the
            # status is unchanged, but must still alert when it genuinely moves.
            incident["seed_status"] = to_status
            self._ledger({"capability": capability,
                          "transition": f"{from_status}->{to_status}",
                          "kind": incident["kind"], "channel": "none", "ok": True,
                          "error": None, "suppressed": "seed_only"})
            self.save()
            return {"action": "seeded", "delivered": True, "attempts": [],
                    "incident": incident}

        message_kind = incident["kind"]
        if not new_incident:
            if (last_notified is None and incident.get("seeded")
                    and not incident.get("undelivered")
                    and incident.get("seed_status") == to_status):
                # SEEDED baseline, status unchanged. A seeded incident also has
                # last_notified_at None, so without this it falls into the
                # "delivery failed, retry" branch below and pages on the very next
                # run — measured: seeding the 66-capability registry would have sent
                # the operator ~57 messages one cadence after a --seed-only run that was
                # specifically supposed to prevent exactly that. Seeding must
                # protect run 2, not only run 1. A genuine status CHANGE inside a
                # seeded incident still falls through and alerts (seed_status
                # differs), and a real delivery failure still retries because it
                # sets undelivered=True.
                self.save()
                return {"action": "suppressed_seed_baseline", "delivered": True,
                        "attempts": [], "incident": incident}
            if last_notified is None:
                # Open but never actually delivered (all channels failed last run).
                # Retrying is the whole point of not marking it notified.
                message_kind = incident["kind"]
            elif incident.get("notified_status") not in (None, to_status):
                # DOWN -> SUSPENDED inside one open incident is a *different* fact
                # (network vs account), and the remedy differs. That is news, not a
                # duplicate — suppressing it would hide the diagnosis.
                message_kind = incident["kind"]
            elif (now - last_notified).total_seconds() >= self.escalate_after:
                message_kind = "ONGOING"
            else:
                self.save()
                return {"action": "suppressed_duplicate", "delivered": True,
                        "attempts": [], "incident": incident}

        message = Message(
            capability, incident["from_status"], to_status,
            detail=incident.get("detail", ""),
            blast_radius=blast_radius or incident.get("blast_radius", ""),
            remedy=remedy or incident.get("remedy", ""),
            since=opened, kind=message_kind,
            ongoing_for=(now - opened).total_seconds(),
        )
        ok, attempts = self.deliver(message)
        if ok:
            incident["last_notified_at"] = iso(now)
            incident["notified_status"] = to_status
            incident["notify_count"] = int(incident.get("notify_count", 0)) + 1
        else:
            incident["delivery_failures"] = int(incident.get("delivery_failures", 0)) + 1
            incident["undelivered"] = True
        self.save()
        action = "escalated" if message_kind == "ONGOING" else "alerted"
        return {"action": action, "delivered": ok, "attempts": attempts,
                "incident": incident}

    # ---- dead-man's switch ----------------------------------------------

    def check_liveness(self, checker="integrations-probe",
                       heartbeat_file=DEFAULT_HEARTBEAT_FILE,
                       heartbeat_field=DEFAULT_HEARTBEAT_FIELD,
                       cadence_seconds=DEFAULT_CADENCE_SECONDS,
                       max_cadences=DEFAULT_MAX_CADENCES,
                       blast_radius=None, remedy=None):
        """Fire STALE_CHECKER if the checker's last-run stamp is too old.

        Reads one timestamp out of one file and shares no process, lock or import
        with the checker, so it still works when the checker is dead — which is the
        only condition under which it matters. Point an independent launchd job at
        `alert.py --check-liveness` and silence stops reading as health.

        A missing or unparseable heartbeat counts as stale. Favourable-absence is
        the exact bug this switch exists to kill."""
        now = self.clock()
        threshold = cadence_seconds * max_cadences
        stamp = None
        reason = None
        try:
            with open(heartbeat_file) as fh:
                blob = json.load(fh)
            stamp = parse_iso(blob.get(heartbeat_field) if isinstance(blob, dict) else None)
            if stamp is None:
                reason = f"heartbeat field {heartbeat_field} missing or unparseable"
        except OSError:
            reason = "heartbeat file unreadable"
        except ValueError:
            reason = "heartbeat file is not valid JSON"

        age = (now - stamp).total_seconds() if stamp else None
        stale = stamp is None or age > threshold
        capability = "checker:" + checker

        if not stale:
            result = self.send(capability, "STALE", "UP",
                               detail=f"last run {human_duration(age)} ago")
            result["stale"] = False
            result["age_seconds"] = age
            return result

        detail = reason or (f"last run {human_duration(age)} ago, threshold {human_duration(threshold)}")
        result = self.send(
            capability, "UP", "STALE",
            detail=detail,
            blast_radius=(blast_radius or
                          "ALL capability monitoring — nothing is being checked, so "
                          "an outage anywhere would now go unreported"),
            remedy=(remedy or
                    f"launchctl list | grep {checker} ; check its stderr log, then run it "
                    "once by hand"),
            kind="STALE_CHECKER",
        )
        result["stale"] = True
        result["age_seconds"] = age
        return result

    # ---- daily digest ----------------------------------------------------

    def digest(self, statuses=None, health_file=DEFAULT_HEARTBEAT_FILE, send=True):
        """One line, every day. Absence of alerts is only evidence if something
        positively says checking happened — otherwise a dead pipeline and a healthy
        estate produce identical silence.

        Returns (line, delivered)."""
        if statuses is None:
            statuses = read_health_statuses(health_file)
        total = len(statuses)
        bad = sorted((name, status) for name, status in statuses.items()
                     if not is_good(status))
        stamp = iso(self.clock())
        if total == 0:
            line = f"capmap digest {stamp}: NO CAPABILITIES CHECKED — the map is empty or " \
                   "unreadable, treat as red"
        elif not bad:
            line = f"capmap digest {stamp}: {total} capabilities checked, all green"
        else:
            listed = ", ".join(f"{name} {status}" for name, status in bad[:8])
            more = "" if len(bad) <= 8 else f" (+{len(bad) - 8} more)"
            line = (
                f"capmap digest {stamp}: {total} checked, {len(bad)} NOT green — "
                f"{listed}{more}"
            )
        line = _redact(line, self.secrets)

        delivered = True
        if send:
            message = Message("digest", "-", "DIGEST", detail=line,
                              blast_radius="n/a — daily proof-of-life",
                              remedy="none; this line proves checking ran",
                              kind="DIGEST")
            message.render = lambda secrets=None, _line=line: _line  # one line, verbatim
            delivered, _attempts = self.deliver(message)
        return line, delivered


def _json_safe(value):
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return str(value)


def read_health_statuses(path=DEFAULT_HEARTBEAT_FILE):
    """Read {name: status} from the probe's snapshot. Unreadable returns {} and the
    digest renders that as red, never as 'all green'."""
    try:
        with open(path) as fh:
            blob = json.load(fh)
    except (OSError, ValueError):
        return {}
    integrations = blob.get("integrations", {}) if isinstance(blob, dict) else {}
    out = {}
    for name, record in integrations.items():
        if isinstance(record, dict):
            out[name] = record.get("status", "UNKNOWN")
        else:
            out[name] = str(record)
    return out


# ------------------------------------------------------------------- construction

def is_armed(arm_file=ARM_FILE):
    return os.path.exists(arm_file)


def build_channels(env=None, target=None, email=None):
    """The live fallback chain, in order. A channel whose credential is absent is
    simply not built — a chain of three where two are unconfigured is still a chain,
    and the ledger records which ones existed at send time."""
    env = env if env is not None else load_env()
    target = target or env.get("CAPMAP_SLACK_TARGET") or os.environ.get("CAPMAP_SLACK_TARGET")
    email = email or env.get("CAPMAP_SLACK_EMAIL") or "owner@hooli.example"
    channels = []
    if env.get("SLACK_BOT_TOKEN"):
        channels.append(SlackDMChannel(env["SLACK_BOT_TOKEN"], target=target,
                                       email=email, team_id=env.get("SLACK_TEAM_ID")))
    if env.get("OPS_ALERT_SLACK_WEBHOOK_URL"):
        channels.append(SlackWebhookChannel(env["OPS_ALERT_SLACK_WEBHOOK_URL"]))
    if env.get("OMNI_NTFY_URL"):
        channels.append(NtfyChannel(env["OMNI_NTFY_URL"]))
    return channels


def build_alerter(dry_run=False, seed_only=False, env=None, live=False, **kwargs):
    """Assemble the production Alerter, honouring both safety gates.

    dry_run OR not-armed => the chain is replaced by a DryRunChannel that keeps a
    record of which live channels *would* have been used. There is exactly one
    place in this module where a real channel can be handed to an Alerter, and it
    is this function."""
    env = env if env is not None else load_env()
    live_channels = build_channels(env)
    armed = is_armed()
    if dry_run or not armed:
        channels = [DryRunChannel(wrapping=live_channels)]
    else:
        channels = live_channels
    alerter = Alerter(channels=channels, secrets=env, seed_only=seed_only, **kwargs)
    alerter.armed = armed
    alerter.dry_run = dry_run or not armed
    alerter.live_channels = live_channels
    return alerter


# ------------------------------------------------------- module-level convenience

def send(capability, from_status, to_status, detail="", blast_radius="", remedy="",
         dry_run=False, seed_only=False):
    return build_alerter(dry_run=dry_run, seed_only=seed_only).send(
        capability, from_status, to_status, detail=detail,
        blast_radius=blast_radius, remedy=remedy)


def check_liveness(dry_run=False, **kwargs):
    return build_alerter(dry_run=dry_run).check_liveness(**kwargs)


def digest(dry_run=False, **kwargs):
    return build_alerter(dry_run=dry_run).digest(**kwargs)


# ----------------------------------------------------------------------- CLI

def _print_plan(alerter):
    print("delivery plan (in order):")
    if not alerter.live_channels:
        print("  !! no live channel is configured — check connections.env")
    for index, channel in enumerate(alerter.live_channels, 1):
        plan = channel.plan() if hasattr(channel, "plan") else channel.name
        print(f"  {index}. {channel.name:<14} {plan}")
    print(f"armed: {alerter.armed} ({ARM_FILE})")
    print("mode : %s" % ("DRY RUN — nothing will be sent" if alerter.dry_run else "LIVE"))


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="capmap durable alert transport (Slack DM -> webhook -> ntfy)")
    parser.add_argument("--selftest-flip", metavar="NAME",
                        help="simulate NAME going UP->DOWN twice; proves one alert + "
                             "one suppression. Dry-run unless --live.")
    parser.add_argument("--selftest-deadman", action="store_true",
                        help="run the dead-man's switch. Dry-run unless --live.")
    parser.add_argument("--check-liveness", action="store_true",
                        help="production dead-man's switch run (for an independent "
                             "launchd job)")
    parser.add_argument("--digest", action="store_true", help="emit the daily one-liner")
    parser.add_argument("--seed-only", action="store_true",
                        help="establish baseline state without sending anything")
    parser.add_argument("--dry-run", action="store_true",
                        help="render exactly what would be sent, send nothing")
    parser.add_argument("--live", action="store_true",
                        help="allow a selftest to really deliver (requires --arm first)")
    parser.add_argument("--arm", action="store_true",
                        help="create the ARM file: permits real delivery to the operator")
    parser.add_argument("--status", action="store_true",
                        help="print the delivery plan and open incidents")
    parser.add_argument("--capability", default="capmap-selftest")
    parser.add_argument("--from-status", default="UP")
    parser.add_argument("--to-status", default="DOWN")
    parser.add_argument("--detail", default="")
    parser.add_argument("--blast-radius", default="")
    parser.add_argument("--remedy", default="")
    args = parser.parse_args(argv)

    if args.arm:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(ARM_FILE, "w") as fh:
            fh.write(f"armed_at={iso(now_utc())}\n")
        print(f"ARMED: {ARM_FILE} — real alerts to the operator are now permitted.")
        return 0

    # Selftests default to dry-run: a test must not be able to page a human.
    selftest = bool(args.selftest_flip) or args.selftest_deadman
    dry_run = args.dry_run or (selftest and not args.live)

    # ...and a selftest keeps its own incident state. A selftest flip of a REAL
    # capability name against the production incidents.json would leave that
    # capability marked "already open", which would suppress the next genuine
    # alert for it — the selftest would create the outage it is testing for.
    # This isolation must hold for --live TOO, which is the case the original
    # `and not args.live` guard missed. Measured in practice: a --live selftest flip
    # left `capmap-selftest` open in the PRODUCTION incidents.json, so the next run
    # of the same selftest was suppressed as a duplicate and reported SELFTEST FAIL
    # while the transport was in fact working perfectly. Worse, --selftest-deadman
    # --live left `checker:integrations-probe` open as STALE, which would have
    # SUPPRESSED A GENUINE DEAD-MAN ALERT — exactly the failure the comment above
    # predicts. Live delivery is still real (the DM is the point of the test); only
    # the bookkeeping is isolated, so a test can never mute production.
    state_kwargs = {}
    if selftest:
        state_kwargs = {"incidents_path": SELFTEST_DIR + "/incidents.json",
                        "ledger_path": SELFTEST_DIR + "/alerts.jsonl"}

    alerter = build_alerter(dry_run=dry_run, seed_only=args.seed_only, **state_kwargs)

    if args.status or not any([args.selftest_flip, args.selftest_deadman,
                               args.check_liveness, args.digest, args.seed_only]):
        _print_plan(alerter)
        state = alerter.state()
        print(f"open incidents: {len(state['open'])}")
        for name, incident in sorted(state["open"].items()):
            print(
                f"  {name:<28} {incident.get('to_status')} since "
                f"{incident.get('opened_at')} (notified {incident.get('notify_count', 0)}x)"
            )
        return 0

    if args.selftest_flip:
        name = args.selftest_flip
        _print_plan(alerter)
        sink = alerter.channels[0].sink if isinstance(alerter.channels[0], DryRunChannel) else None
        first = alerter.send(name, args.from_status, args.to_status,
                             detail=args.detail or "selftest flip",
                             blast_radius=args.blast_radius or
                             "selftest capability — no production impact",
                             remedy=args.remedy or "none; this is a transport selftest")
        second = alerter.send(name, args.to_status, args.to_status,
                              detail="selftest flip (repeat within dedupe window)")
        if sink:
            print("\n--- rendered message ---")
            for text in sink:
                print(text)
            print("--- end ---")
        print("\nfirst  : action={} delivered={}".format(first["action"], first["delivered"]))
        print("second : action={} delivered={}".format(second["action"], second["delivered"]))
        ok = first["action"] == "alerted" and second["action"] == "suppressed_duplicate"
        print("SELFTEST %s (expect alerted then suppressed_duplicate)"
              % ("PASS" if ok else "FAIL"))
        return 0 if ok else 1

    if args.selftest_deadman or args.check_liveness:
        result = alerter.check_liveness()
        if isinstance(alerter.channels[0], DryRunChannel) and alerter.channels[0].sink:
            print("--- rendered message ---")
            for text in alerter.channels[0].sink:
                print(text)
            print("--- end ---")
        print(json.dumps({"stale": result.get("stale"),
                          "age_seconds": result.get("age_seconds"),
                          "action": result["action"],
                          "delivered": result["delivered"]}, indent=1))
        return 0

    if args.digest:
        line, delivered = alerter.digest(send=not args.dry_run)
        print(line)
        if args.dry_run:
            print("(dry run — not sent)")
        elif not delivered:
            print(f"({UNDELIVERED_MARKER} — digest could not be delivered)")
            return 1
        return 0

    if args.seed_only:
        statuses = read_health_statuses()
        for name, status in sorted(statuses.items()):
            alerter.send(name, "UNKNOWN", status, detail="seed baseline")
        print(f"seeded {len(statuses)} capabilities into {alerter.incidents_path} (nothing sent)")
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
