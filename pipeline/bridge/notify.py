#!/usr/bin/env python3
"""Push transport for loop alerts (ntfy).

WHY THIS FILE EXISTS: the loops' durable alert record is ``ALERTS.md`` — a
file nobody watches without a live session. An alert that only lands there
never reaches the operator's phone. This module adds the SECOND leg of every
alert: if ``OMNI_NTFY_URL`` is set, POST the alert to it (ntfy) so it reaches
a phone with no session running.

DIVISION OF LABOUR (do not blur it): the callers (``bridge/claim.py``,
``bridge/advice_writer.py``, …) still write the ``ALERTS.md`` line
themselves — that is the durable log and it is never at risk from anything
here. This module ONLY does the push, and is called at the SAME single point
that just wrote the file line, so one alert = one file line + one push.

FAIL-SOFT BY CONTRACT — no path in this module may raise into the loop:

  * URL unset            -> return None, do nothing (the normal no-push case,
                            NOT a failure — nothing is logged).
  * DNS / connect error  -> swallow, append a one-line transport-failure note
  * timeout              -> swallow, append a one-line transport-failure note
  * non-2xx HTTP status  -> swallow, append a one-line transport-failure note

The transport-failure note goes into ``ALERTS.md`` itself (the same durable
log) so a silently dead push channel always leaves a visible trace — the one
failure mode a push transport must never have is disappearing without a word.
The alert line the caller already wrote is on disk before we are ever called,
so even a note-write failure cannot lose the primary alert.
"""

from __future__ import annotations

import os
import re
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

# Env var the whole estate agreed on (see run-loop.sh ROLE_ENV). The full
# value is ``https://ntfy.sh/<hard-to-guess-topic>``.
NTFY_ENV = "OMNI_NTFY_URL"

# Short by design: a slow push must never stall a loop iteration. On timeout
# we fall back to "ALERTS.md only" and record a transport-failure note.
DEFAULT_TIMEOUT_S = 5

# --- egress guardrail (operator-approved 2026-08-09) -------------------------
# ntfy is a PUBLIC relay: the durable ``ALERTS.md`` line the caller wrote keeps
# full detail, but what we PUSH is reduced here to a terse, secret-free,
# path-free, single-line summary. A leaked topic can then expose only a bounded
# operational line — never a secret, key, token, private key, or absolute
# filesystem path. This is defensive and fail-soft: on ANY error the body
# collapses to a safe placeholder rather than pushing raw content, and the
# result is always a single line so it can never smuggle extra ntfy headers.
_PUSH_MAX_BODY = 200
_PUSH_MAX_TITLE = 100
# Zero-width / bidi format chars: stripped first so a secret cannot be split
# (e.g. a zero-width space inside "sk-…") to evade the shape patterns below.
_ZERO_WIDTH_RE = re.compile("[\u200b-\u200f\u202a-\u202e\u2060\ufeff]")

# Secret-shaped tokens that must never leave the machine. Ordered longest/most
# specific first so a private-key block is caught before narrower patterns.
_SECRET_PATTERNS = (
    # PRIVATE KEY / PRIVATE KEY BLOCK (PGP armor) with any label words around it
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY[A-Z0-9 ]*-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY[A-Z0-9 ]*-----"),
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY[A-Z0-9 ]*-----[\s\S]*"),
    re.compile(r"eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"),  # JWT
    re.compile(r"sk[-_][A-Za-z0-9_\-]{8,}"),                               # openai-style (hyphen or underscore)
    re.compile(r"\b[A-Za-z]{2,8}_(?:live|test)_[A-Za-z0-9]{4,}"),          # stripe-style vendor keys (sk_live_/pk_test_/rk_live_…)
    re.compile(r"\bwhsec_[A-Za-z0-9]{6,}"),                                # stripe webhook signing secret
    re.compile(r"gh[pousr]_[A-Za-z0-9]{16,}"),                            # github PAT family
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{8,}"),                           # slack
    re.compile(r"(?<![A-Z0-9])A(?:KIA|SIA)[0-9A-Z]{16}"),                  # aws access key id
    re.compile(r"AIza[0-9A-Za-z_\-]{20,}"),                               # google api key
    re.compile(r"\bEAA[A-Za-z0-9]{20,}"),                                 # facebook
    re.compile(r"\bpit-[A-Za-z0-9-]{10,}"),                               # pipedream
)
# Generic high-entropy backstop: any 32+ char token is redacted UNLESS it is a
# pure-hex or pure-digit run (a git SHA / candidate id / numeric id — which the
# operator needs to act on the alert). AWS-secret-shaped and other unknown
# base64 tokens carry upper-case or symbols and so are caught here.
_HIGH_ENTROPY = re.compile(r"[A-Za-z0-9+/=_-]{32,}")


def _redact_high_entropy(s: str) -> str:
    def repl(m: re.Match) -> str:
        tok = m.group(0)
        if re.fullmatch(r"[0-9a-f]+", tok) or re.fullmatch(r"[0-9]+", tok):
            return tok  # SHA / hash / numeric id — keep so the alert stays actionable
        return "[redacted]"

    return _HIGH_ENTROPY.sub(repl, s)
# key=value / key: value secrets (password=, token:, api_key=, bearer …). The
# optional quotes around the separator catch JSON/dict shapes too — a loop that
# alerts json.dumps(err_dict) produces "password":"letmein", which a bare
# `\s*[=:]\s*` would miss.
_KV_SECRET = re.compile(
    r"(?i)(pass(?:word)?|passwd|secret|token|api[_-]?key|apikey|auth|bearer|credential|priv(?:ate)?[_-]?key)"
    r"([\"']?\s*[=:]\s*[\"']?)(\S+)"
)
# Soft labels (token/auth) are ordinary prose words AND secret labels. Redact
# from the label to end-of-line IFF a credential-shaped token appears anywhere
# after it — robust to ANY phrasing/verb because it detects the secret by SHAPE,
# not position. So 'auth failed for provider openai' / 'token bucket empty'
# survive, while 'token set to <secret>' / 'the token is now <secret>' do not.
# (The hard labels password/secret/api_key/… are handled unconditionally by
# _KV_HARD above; only token/auth need this shape-aware middle ground.)
_SOFT_LABEL_RE = re.compile(r"(?i)\b(token|auth)\b(\s+\S.*)$")
_VALUE_PREFIX = re.compile(r"(?i)^(sk|pk|rk|ak|live|test|api|key|tok|bearer|xox[baprs]|gh[pousr]|eyj|aws|pit|whsec)[-_]?")


def _looks_secretish(val: str) -> bool:
    """True if a bare token following a secret label looks like a credential
    rather than an ordinary word (so 'auth failed' stays readable)."""
    if _VALUE_PREFIX.match(val):
        return True
    if len(val) >= 20:
        return True
    has_digit = any(c.isdigit() for c in val)
    has_alpha = any(c.isalpha() for c in val)
    internal_upper = any(c.isupper() for c in val[1:])
    return (has_digit and has_alpha) or internal_upper


def _soft_label_repl(m: re.Match) -> str:
    tail = m.group(2)
    if any(_looks_secretish(t.strip("\"'.,;:()[]{}")) for t in tail.split()):
        return f"{m.group(1)} [redacted]"
    return m.group(0)


# HARD secret labels: the token right after the label IS the value regardless of
# how it looks, so redact it unconditionally (any =/:/whitespace separator).
# Kept separate from the heuristic _KV_SPACE so 'auth failed' / 'token bucket'
# (soft labels) stay readable while 'password letmein' does not.
_KV_HARD = re.compile(
    r"(?i)\b(pass(?:word)?|passwd|secret|api[_-]?key|apikey|priv(?:ate)?[_-]?key|credential|bearer)\b"
    r"([\"']?\s*[=:]\s*[\"']?|\s+).*$"
)


def _kv_hard_repl(m: re.Match) -> str:
    # A hard secret label means "a secret follows", so redact EVERYTHING after the
    # label to end-of-line (fail-closed). Structural, not per-case: this closes
    # 'password letmein', 'password is X', 'password: was Y' and every other
    # "label + filler + value" shape that a single-token capture leaked.
    return f"{m.group(1)}{m.group(2)}[redacted]"


# A URL of ANY scheme (https, postgres, redis, mongodb+srv, amqp, ftp, ssh…) may
# carry a topic/token in path/query, or user:pass@ credentials in the authority.
# Collapse to the BARE HOST via _url_repl, which drops the whole userinfo (up to
# the LAST '@', so multi-'@' and '/'-containing passwords cannot survive) plus
# the port, path and query.
_URL_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9+.\-]*://([^\s'\"]*)")


def _url_repl(m: re.Match) -> str:
    authority = m.group(1).split("/", 1)[0]   # up to first '/': drop path+query
    host = authority.rsplit("@", 1)[-1]       # drop ALL userinfo (up to last '@')
    host = host.split(":", 1)[0]              # drop port
    return host or "[redacted]"


# Credential authority (schemeless) "[user]:[pass]@host" -> redact the WHOLE
# userinfo up to the LAST '@' and keep only the bare host. One structural rule
# (not per-case): the ':' requirement leaves plain "user@host" emails alone; the
# `(?:@[^\s@]*)*` segment absorbs a password that itself contains '@' (so nothing
# after the first '@' leaks); an empty user (":pass@host", redis-style) still
# matches. Scheme'd URLs are already collapsed by _URL_RE above.
_CRED_AUTHORITY_RE = re.compile(r"(?<![\w@:/])[^\s@]*:[^\s@]*(?:@[^\s@]*)*@([^\s/@:]+)")


def _cred_repl(m: re.Match) -> str:
    return m.group(1)  # keep only the host, drop all userinfo
# Any absolute filesystem path (NOT a root allowlist) -> keep only the basename.
# The lookbehind avoids prose like "and/or" / "read/write" (a real path is not
# preceded by a word char); >=2 segments are required so a lone "/etc" token in
# prose is left alone. DIRECTORY segments allow internal single spaces so real
# macOS paths ("Application Support", "Google Drive") don't leak their structure
# past the first space; the final basename stops at the first space so trailing
# prose after the path isn't swallowed. Covers /Applications /Library /usr …
_ABS_PATH_RE = re.compile(r"(?<!\w)/(?:[\w.\-]+(?: [\w.\-]+)*/)+[\w.\-]+")


def _abs_path_repl(m: re.Match) -> str:
    p = m.group(0).rstrip("/")
    base = p.rsplit("/", 1)[-1] or "path"
    return f".../{base}"


def _terse(text: object, *, max_len: int = _PUSH_MAX_BODY, extra: object = None) -> str:
    """Reduce arbitrary alert text to a single-line, secret-free, path-free,
    length-capped summary that is safe to POST to a public relay and safe to
    place in a single ntfy header. NEVER raises — on any failure it returns a
    conservative placeholder rather than risk pushing raw content."""
    try:
        s = "" if text is None else str(text)
        # First non-empty line only: anything on a later line is dropped, so a
        # secret split below the first line never reaches the relay.
        first = ""
        for line in s.splitlines():
            if line.strip():
                first = line.strip()
                break
        s = first or s.strip()
        # Bound the working length. The pushed body is capped to 200 chars and
        # every redaction below only ever SHRINKS the string, so processing more
        # than this is pointless — and it keeps the whole pipeline strictly
        # linear (a huge alert line cannot drive quadratic regex backtracking).
        # Truncation only DROPS trailing content; it can never leak it.
        s = s[:2000]
        # Strip zero-width / bidi format chars so a secret can't be split
        # (e.g. a zero-width space inside "sk-…") to slip past the patterns below.
        s = _ZERO_WIDTH_RE.sub("", s)
        # Explicitly redact the live push topic (env value + the actual target)
        # even when mentioned bare (no scheme, short) — belt-and-braces beyond
        # the _URL_RE host collapse.
        for _t in (os.environ.get(NTFY_ENV, "").strip(), str(extra or "").strip()):
            if not _t:
                continue
            s = re.sub(re.escape(_t), "[redacted]", s, flags=re.IGNORECASE)
            _bare = _t.rstrip("/").rsplit("/", 1)[-1]
            if len(_bare) >= 8:
                # also catch a TRUNCATED or different-case mention: the topic's
                # 8-char lead followed by any topic-ish run.
                s = re.sub(re.escape(_bare[:8]) + r"[A-Za-z0-9\-]*", "[redacted]", s, flags=re.IGNORECASE)
            elif len(_bare) >= 4:
                s = re.sub(re.escape(_bare), "[redacted]", s, flags=re.IGNORECASE)
        # Redact secrets BEFORE structural collapse (a secret may sit in a path
        # or URL); the collapses below only ever remove more.
        for pat in _SECRET_PATTERNS:
            s = pat.sub("[redacted]", s)
        s = _KV_SECRET.sub(lambda m: f"{m.group(1)}{m.group(2)}[redacted]", s)
        s = _KV_HARD.sub(_kv_hard_repl, s)
        s = _SOFT_LABEL_RE.sub(_soft_label_repl, s)
        # Structural reductions run BEFORE the generic high-entropy backstop:
        # collapse URLs -> host, strip credential authorities, reduce paths ->
        # basename first, so a long path/URL run is not swallowed whole by
        # _HIGH_ENTROPY (whose charset includes '/') before the path-aware
        # redactor can see it — which would leave the trailing structure exposed.
        if "://" in s:
            s = _URL_RE.sub(_url_repl, s)
        if "@" in s:
            s = _CRED_AUTHORITY_RE.sub(_cred_repl, s)
        if "/" in s:
            s = _ABS_PATH_RE.sub(_abs_path_repl, s)
        # High-entropy backstop LAST: catch any bare secret token that survived
        # the shape / label / structural passes above (base64 secrets keep '/').
        s = _redact_high_entropy(s)
        # Collapse any residual CR/LF (header-injection defence) and cap length.
        s = s.replace("\r", " ").replace("\n", " ").strip()
        if len(s) > max_len:
            s = s[: max_len - 1].rstrip() + "…"
        return s
    except Exception:  # noqa: BLE001 — the guardrail is fail-soft like the rest of this module
        return "[alert suppressed]"


def _iso_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _append_transport_note(loops_root: Path | str, msg: str, source: str) -> None:
    """Record a one-line transport-failure note in ALERTS.md. Best-effort:
    if even this write fails, swallow it — the PRIMARY alert line is already
    on disk (the caller wrote it before calling us), so nothing is lost by
    not recording that the push failed. Never raises."""
    try:
        with open(Path(loops_root) / "ALERTS.md", "a", encoding="utf-8") as fh:
            fh.write(f"- {_iso_now()} {source}: {msg}\n")
    except OSError:
        pass


def push_alert(
    loops_root: Path | str,
    title: str,
    body: str,
    *,
    source: str = "notify",
    priority: str | int | None = None,
    tags: str | list[str] | None = None,
    url: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> int | None:
    """POST one alert to ``OMNI_NTFY_URL`` (ntfy) if it is set.

    Returns the HTTP status code on a 2xx response, ``None`` in every other
    case (URL unset, network error, timeout, or a non-2xx status). NEVER
    raises — fail-soft is the entire point of this function. On a genuine
    failure (URL set but the POST did not succeed) a one-line
    transport-failure note is appended to ALERTS.md so the dead channel is
    visible; an unset URL is the normal no-push path and logs nothing.

    ``title`` -> ntfy ``X-Title`` header (the phone notification title).
    ``body``  -> the POST body (the notification text).
    ``priority`` -> optional ``X-Priority`` (1..5). ``tags`` -> optional
    ``X-Tags`` (comma-joined if a list).
    """
    target = url if url is not None else os.environ.get(NTFY_ENV, "")
    target = (target or "").strip()
    if not target:
        # Unset URL is not a failure — it is the default state before the
        # operator subscribes. Do nothing, quietly.
        return None

    # Egress guardrail (operator-approved 2026-08-09): the pushed body/title are
    # reduced to a terse, secret-free, path-free, single-line summary. The
    # caller's full ALERTS.md line is untouched — only what leaves the machine
    # is minimised. Every header value is single-line, so none can inject.
    data = _terse(body, extra=target).encode("utf-8")
    req = urllib.request.Request(target, data=data, method="POST")
    if title:
        req.add_header("X-Title", _terse(title, max_len=_PUSH_MAX_TITLE, extra=target))
    if priority is not None:
        req.add_header("X-Priority", _terse(priority, max_len=12, extra=target))
    if tags:
        tags_str = tags if isinstance(tags, str) else ",".join(str(t) for t in tags)
        req.add_header("X-Tags", _terse(tags_str, max_len=_PUSH_MAX_TITLE, extra=target))

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, "status", None)
            if status is None:
                status = resp.getcode()
        if isinstance(status, int) and 200 <= status < 300:
            return status
        _append_transport_note(
            loops_root,
            f"ntfy push returned HTTP {status} — alert is recorded in ALERTS.md "
            f"but did NOT reach the push channel",
            source,
        )
        return None
    except Exception as exc:  # noqa: BLE001 — fail-soft: NOTHING may raise into the loop
        # Any transport-layer problem (DNS, connect refused, timeout, TLS,
        # a malformed URL) lands here. The alert is already durably in
        # ALERTS.md; we only note that the phone leg did not go through.
        _append_transport_note(
            loops_root,
            f"ntfy push failed ({type(exc).__name__}) — alert is recorded in "
            f"ALERTS.md but did NOT reach the push channel",
            source,
        )
        return None
