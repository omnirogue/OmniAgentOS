"""GET /api/ledger/claims and GET /api/ledger/tail — read-only relays over the
estate session-ledger CLI (``~/.omniagentos/ops/bin/ledger``), feeding the kanban
board's open-claims header strip and per-card event-timeline drawer.

Contract authority: ``~/.omniagentos/ops/session-ledger/PLAN.md`` + ``EVENTS.md`` (the
session-ledger integration brief, 2026-08-04). HARD RULES from that brief:

  * SHELL the CLI via ``subprocess`` with an argv LIST — never ``shell=True``,
    never a string built by interpolating a query param into a command line.
    Every query parameter is validated against a conservative allowlist
    BEFORE it is placed in argv.
  * NEVER parse ``ledger*.jsonl`` directly. The CLI owns the exclusive lock,
    the ``(at, id)`` ordering, and correction-attachment (a `note` correcting
    an earlier row is stitched onto it as ``_corrections`` by the CLI itself,
    see ``ledger.py::attach_corrections``) — re-implementing any of that here
    would be a second, drifting copy of the CLI's own logic.
  * NEVER pass ``--owner``. The owner-only (the operator-visibility) stream is a
    physically separate file, ingested only by coordinator contexts, and
    must never leave this Mac via the API. Neither route ever passes
    ``--owner`` — ``ledger tail`` therefore stays restricted to the generic
    stream by the CLI's own default. ``ledger claims`` is called with NO
    owner-related flag at all (pre-merge review decision, 2026-08-04): the
    CLI's own default already shows owner-HELD surfaces (with any attached
    correction text redacted server-side by the CLI itself, see
    ``ledger.py::cmd_claims``) rather than omitting them, because silently
    hiding a owner-held claim would present that surface as FREE to a
    coordinator checking who holds what — a worse outcome than showing the
    (redacted) fact that it is held. This module never re-derives or
    re-redacts anything; it relays exactly what the CLI printed.
  * subprocess timeout: 10s for ``tail``, 3s for ``claims`` (its own,
    shorter constant — a locked ledger stalling this app's sync request
    threadpool at 10s per poll, when the board polls claims every 10s, is a
    starvation vector `ledger tail` does not share). A nonzero exit, a
    timeout, a subprocess that could not even start (binary missing/
    unreadable, a resource exhaustion), an exit-0 stdout line that is not
    valid JSON, or a line that IS valid JSON but not an object (``null``,
    ``[1,2]``, a bare number — every real ledger row is a JSON object), all
    answer 503 carrying a diagnostic — never an empty 200 and never a bare
    500 (silence, or an unhandled crash, must not look like an empty
    ledger).

Auth: both routes are session-token-gated even though most GETs in this app
are public by convention — see ``_is_session_ledger_read`` in
``omniagentos.api.main`` (deliberate, per the integration brief's S1: these
relay cross-project claim/session history off the machine's estate-wide
ledger, the same recon tier as the other gated read namespaces).
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from typing import Any

from fastapi import APIRouter, Query

from omniagentos.api.services import fail

LOG = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ledger", tags=["ledger"])

# Absolute path, exactly as named by the integration brief and mirrored by
# lane-done-check's own `ledger append` shell-out — never resolved via PATH.
_LEDGER_BIN = "/Users/youruser/Work/Ops/bin/ledger"
_SUBPROCESS_TIMEOUT_S = 10
_CLAIMS_TIMEOUT_S = 3
_MAX_N = 200

# Test-only root override. Unset in production, where the CLI's own default
# root (~/.omniagentos/ops/session-ledger) applies unchanged — this seam exists
# solely so tests/api/test_ledger_routes.py's live-CLI E2E test can point a
# REAL (unmocked) subprocess call at a scratch directory instead of ever
# appending to or reading the real estate ledger. Read fresh on every call
# (never cached at import time) so a test can monkeypatch the env var
# per-test via `monkeypatch.setenv`.
_LEDGER_ROOT_ENV = "OMNIAGENTOS_LEDGER_ROOT"

# Conservative allowlist charset for the project/event/agent filters: letters,
# digits, and the punctuation actually used by ledger identifiers (path-like
# projects such as "OmniAgentOS/dashboard", agent lineages such as
# "human:owner", event names such as "branch_ready"). This never reaches a
# shell — argv only — but the brief requires every param validated before it
# reaches argv regardless, so nothing skips this allowlist.
_FILTER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$"
_REF_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_]{1,64}$")
_REF_MAX_VALUE_LEN = 256
# C0 (\x00-\x1f) AND DEL/C1 (\x7f-\x9f) — a bare `.match()` against a
# `^...$`-anchored pattern is not sufficient on its own: `$` matches just
# before a TRAILING newline, not strictly end-of-string, so a key like
# "task\n" would slip past `_REF_KEY_PATTERN.match(...)` with the newline
# intact. This scans the WHOLE ref string (key AND value) before any
# partitioning, so no control character — wherever it sits — ever reaches
# argv; `_REF_KEY_PATTERN` is additionally checked with `fullmatch` (which
# requires consuming the entire key) as defense in depth.
_CONTROL_CHAR = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _ledger_root() -> str | None:
    """The ``--root`` override for this call, or ``None`` in production."""
    return os.environ.get(_LEDGER_ROOT_ENV) or None


def _warn_if_root_override_is_set() -> None:
    """Called once, at module import time (below) — routes are imported once
    per process at app startup, so this fires at most once per server
    lifetime, never once per request. A LEAKED ``OMNIAGENTOS_LEDGER_ROOT``
    (e.g. carried over from a shell that ran the test suite) must not
    silently redirect production ledger reads to a scratch directory
    without so much as a log line."""
    value = os.environ.get(_LEDGER_ROOT_ENV)
    if value:
        LOG.warning(
            "%s is set (%r) -- session-ledger reads in THIS PROCESS are "
            "pointed at a NON-PRODUCTION root, not ~/.omniagentos/ops/session-ledger. "
            "This variable exists solely for the live-CLI E2E test in "
            "tests/api/test_ledger_routes.py; if this warning appears outside "
            "a test run, a leaked env var is silently redirecting production "
            "ledger reads.",
            _LEDGER_ROOT_ENV,
            value,
        )


_warn_if_root_override_is_set()


def _run_ledger(args: list[str], *, timeout: int = _SUBPROCESS_TIMEOUT_S) -> list[dict[str, Any]]:
    """Shell the ledger CLI and parse its newline-delimited JSON stdout.

    ``ledger tail``/``ledger claims`` each print one JSON object per line
    (see ``ledger.py::cmd_tail``/``cmd_claims``) — the only ledger data this
    module ever touches is that stdout, never the on-disk ``ledger*.jsonl``
    shards.

    Every real-world failure mode — a timeout, the subprocess machinery
    itself refusing to start (binary missing/unreadable, a resource
    exhaustion such as EMFILE), a nonzero exit, or an exit-0 stdout line
    that is not valid JSON (a CLI regression, not ledger emptiness) —
    resolves to 503, never an uncaught 500 and never an empty 200.
    """
    argv = [_LEDGER_BIN]
    root = _ledger_root()
    if root:
        argv += ["--root", root]
    argv += args
    try:
        proc = subprocess.run(  # noqa: S603 - argv list, no shell, absolute binary
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        fail(503, "ledger_unavailable", f"ledger {args[0]} timed out after {timeout}s")
    except (subprocess.SubprocessError, OSError) as exc:
        # FileNotFoundError (binary missing) and PermissionError are both
        # OSError subclasses; subprocess.SubprocessError covers the rest of
        # the subprocess module's own failure taxonomy. A broken CLI relay
        # is a 503, exactly like a timeout or a nonzero exit — never a bare
        # 500 from an uncaught exception.
        fail(503, "ledger_unavailable", f"ledger {args[0]} could not run: {exc}")
    if proc.returncode != 0:
        stderr_line = next(
            (line for line in proc.stderr.splitlines() if line.strip()),
            f"ledger {args[0]} exited {proc.returncode}",
        )
        fail(503, "ledger_unavailable", stderr_line)
    rows: list[dict[str, Any]] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            # exit 0 but a stdout line that is not valid JSON is a CLI
            # regression (e.g. a stray diagnostic printed to stdout instead
            # of stderr), not an empty ledger — 503, not a silent drop.
            fail(503, "ledger_unavailable", f"unparseable ledger {args[0]} output: {line[:120]!r}")
        if not isinstance(row, dict):
            # Valid JSON that is NOT an object ("null", "[1,2]", "42", ...)
            # parses cleanly and would otherwise sail through as a row --
            # every real ledger row is a JSON object, so this is the same
            # CLI-regression class as unparseable JSON, not a shape this
            # module quietly reshapes or drops.
            fail(503, "ledger_unavailable", f"non-object ledger {args[0]} row: {line[:120]!r}")
        rows.append(row)
    return rows


def _validate_ref(ref: str) -> str:
    """``--ref`` filters are ``key=value`` (see ``ledger tail --ref``)."""
    if _CONTROL_CHAR.search(ref):
        fail(422, "validation", f"ref must not contain control characters: {ref!r}")
    key, sep, value = ref.partition("=")
    if (
        not sep
        or not _REF_KEY_PATTERN.fullmatch(key)
        or not value
        or len(value) > _REF_MAX_VALUE_LEN
    ):
        fail(422, "validation", f"ref must be key=value with a plain key: {ref!r}")
    return ref


@router.get("/claims")
def ledger_claims() -> list[dict[str, Any]]:
    """Every OPEN claim across every project — the coordinator's first-check,
    now the board header's open-claims strip. No owner-related flag at all (see
    the module docstring): owner-held surfaces are shown, redacted, never
    silently omitted. A dedicated, SHORTER timeout than `tail` (see
    `_CLAIMS_TIMEOUT_S`) — this route is polled every 10s by every open
    board tab, so a locked ledger must fail fast rather than piling up
    threadpool workers."""
    return _run_ledger(["claims"], timeout=_CLAIMS_TIMEOUT_S)


@router.get("/tail")
def ledger_tail(
    project: str | None = Query(default=None, pattern=_FILTER_PATTERN),
    event: str | None = Query(default=None, pattern=_FILTER_PATTERN),
    agent: str | None = Query(default=None, pattern=_FILTER_PATTERN),
    ref: str | None = Query(default=None, max_length=_REF_MAX_VALUE_LEN + 65),
    n: int = Query(default=20, ge=1, le=_MAX_N),
) -> list[dict[str, Any]]:
    """Filtered ledger tail, newest-last (see ``ledger tail``). The card
    drawer calls this with ``ref=task=<task_id>`` to render one card's event
    timeline."""
    args = ["tail", "-n", str(n)]
    if project:
        args += ["--project", project]
    if event:
        args += ["--event", event]
    if agent:
        args += ["--agent", agent]
    if ref:
        args += ["--ref", _validate_ref(ref)]
    return _run_ledger(args)
