#!/usr/bin/env python3
"""Agent Health Sentinel — one cheap, total pass over "is the fleet actually alive".

The signals this reads all already existed on this box; nothing assembled them,
so a dead Claude pool, a stopped scheduler tick, an unloaded launchd job and a
missing reflection briefing each stayed invisible until someone happened to
look at a different file. This is the assembler.

Fourteen checks, each TOTAL (a crash inside one becomes that check's ``fail``,
never the run's) and each producing ``ok | warn | fail`` plus a one-line evidence
string an operator can act on without opening anything else:

  api           GET ``/api/health`` on ``$OMNIAGENTOS_API_PORT`` — reachable, not
                degraded, worker heartbeat fresh.
  runner        the step-polling runtime worker (``python -m omniagentos.runner``,
                as ``scripts/launch-omniagentos.sh``'s ``_runner`` spawns it)
                is alive AND belongs to THIS checkout, not a sibling worktree.
  scheduler     ``var/log/routines.log``'s newest tick is < 15 min old (cadence
                is ~5 min), read from the log's own ``checked_at`` stamp rather
                than file mtime, so a log touched by a deprecation warning
                cannot fake a tick.
  slack_socket  BOTH halves of the hybrid Slack ingestion: the ``slack-socket``
                heartbeat row is fresh (a live process with a dead connection
                thread is exactly what ``KeepAlive`` cannot see), the ``slack``
                reconciliation sweep has run inside 3 of its own intervals, and
                the sweep caught nothing — ``created > 0`` there is proof, not a
                hint, that the socket missed messages.
  claude_pool   every account ENABLED in ``configs/accounts.yaml`` still has
                usable auth, judged from (1) its ``~/.claude-account-N``
                credential file's expiry and (2) the ``claude_accounts`` row the
                runtime itself last wrote. NO CLI is spawned and no LLM token is
                spent — see :func:`check_claude_pool` for why both sources are
                required and neither alone is sufficient.
  memory        ``var/memories/OmniAgentOS/MEMORY.md`` exists, is non-empty
                and is not stale; the memlife store root exists; the control-plane
                SQLite exists and its memlife tables are readable.
  reflection    ``<vault>/briefings/reflection-<today|yesterday>.md`` exists (25h
                window — the loop is nightly, so yesterday's is still current
                until tonight's lands). ``<vault>`` is resolved by
                :func:`resolve_briefings_dir`, i.e. the SAME chain the writer
                uses (``OMNIAGENTOS_VAULT_DIR`` → ``var/runtime/vault``),
                never a hardcoded ``<repo>/vault``.
  providers     ``var/provider-health.json`` parses and no provider is failing.
  launchd       every ``com.omniagentos.*`` plist INSTALLED in
                ``~/Library/LaunchAgents`` is loaded and last exited 0; every
                plist RENDERED under ``var/launchd/rendered`` is at least
                installed. This is the meta-watchdog: it is what notices that
                the watchdogs themselves are gone.
  disk          ``/System/Volumes/Data`` free space above the 50 GB floor.
  dashboard_build_freshness  every top-level page directory under
                ``dashboard/src/app`` resolves live on :3003 (not a 404 from a
                served build that predates its own source).
  gate_workspace whether the detached gate pin exists, is clean, and still
                points at ``main``'s current commit.
  revenue       the revenue collector's persisted ``revenue_source_status``
                fail-loud streak: FAIL when any source has been failing or
                unconfigured 2+ consecutive ET days, WARN below that.

Outputs (all additive, all under ``var/``):
  * ``var/health-sentinel/latest.json``   — full snapshot (atomic tmp+rename)
  * ``var/health-sentinel/ledger-YYYYMM.jsonl`` — one compact line per run
  * ``var/log/health-sentinel.log``       — human log
  * ``<vault>/briefings/health-ALERT-YYYY-MM-DD.md`` — written whenever a check
    FAILS, in the ``reflection-ALERT-*.md`` table style, beside the reflection
    briefings it is reporting on.

Alerting: any ``fail`` records a ``kind="alert"`` notification through
``omniagentos.notifications.service.record_notification`` (the one write seam)
with a date-scoped ``ref_id``. The macOS banner is deduped PER ISSUE PER DAY by
this script's own state file — ``record_notification`` pushes unconditionally
even on a dedupe, and at a 30-minute cadence that would be 48 identical banners
a day. ``warn`` is logged and lands in the snapshot but never banners.

NO LLM calls, no CLI spawns other than ``ps``/``launchctl``, no writes outside
``var/`` and ``vault/briefings/``. Every filesystem/DB/network read is guarded:
the moment this script would crash is exactly the moment its alert matters most,
so a locked SQLite, a dead API or a missing file degrades to a ``fail`` WITH
evidence, never to a traceback and no snapshot.
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = Path(__file__).resolve().parent

# The two consumer arms live beside this file. `scripts/*` is not an installed
# package (see launchd.py's docstring for why that idiom is deliberate here), so
# the sibling import is made explicit rather than depending on sys.path[0]
# happening to be the script dir — which is false whenever this module is
# imported by a test instead of executed.
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
import audit_checks as _audit  # noqa: E402
import blocked_sessions as _blocked  # noqa: E402
import mechanism_drift_detector as _mechanism_drift  # noqa: E402
from mechanism_registry import MECHANISM_REGISTRY_PATH, load_registry  # noqa: E402

STATE_DIR = REPO_ROOT / "var" / "health-sentinel"
LATEST_PATH = STATE_DIR / "latest.json"
ALERT_STATE_PATH = STATE_DIR / "alert-state.json"
REMEDY_LEDGER_PATH = STATE_DIR / "remedy_ledger.json"
LOG_PATH = REPO_ROOT / "var" / "log" / "health-sentinel.log"
HOLDS_PATH = REPO_ROOT / "HOLDS.yaml"

ACCOUNTS_YAML = REPO_ROOT / "configs" / "accounts.yaml"
ROUTINES_LOG = REPO_ROOT / "var" / "log" / "routines.log"
PROVIDER_HEALTH = REPO_ROOT / "var" / "provider-health.json"
MEMORY_MD = REPO_ROOT / "var" / "memories" / "OmniAgentOS" / "MEMORY.md"
MEMLIFE_STORE = REPO_ROOT / "var" / "memories" / "memlife"
RENDERED_LAUNCHD_DIR = REPO_ROOT / "var" / "launchd" / "rendered"
LAUNCH_AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"

LAUNCHD_PREFIX = "com.omniagentos."
DATA_VOLUME = "/System/Volumes/Data"

DASHBOARD_APP_DIR = REPO_ROOT / "dashboard" / "src" / "app"
DASHBOARD_TIMEOUT_SECONDS = 5.0
# Route-group and file entries under dashboard/src/app that are not themselves
# a browsable page (the API proxy group, dynamic/group segments, and stray
# top-level files) — never probed for build freshness.
_DASHBOARD_NON_PAGE_ENTRIES = frozenset({"api", "__tests__"})

OK = "ok"
WARN = "warn"
FAIL = "fail"
_RANK = {OK: 0, WARN: 1, FAIL: 2}
_OBSERVER_CONSUMERS = frozenset({"health_sentinel:check_mechanism_registry"})
_PROVIDER_OUTCOMES = frozenset(
    {
        "ok",
        "auth_error",
        "quota_exhausted",
        "transient_rate_limit",
        "overloaded",
        "harness_error",
        "unavailable",
    }
)

# --- thresholds -------------------------------------------------------------
API_TIMEOUT_SECONDS = 8.0
HEARTBEAT_WARN_SECONDS = 120.0
HEARTBEAT_FAIL_SECONDS = 300.0
TICK_WARN_SECONDS = 10 * 60
TICK_FAIL_SECONDS = 15 * 60
REFLECTION_WINDOW_HOURS = 25
MEMORY_WARN_AGE_DAYS = 7
MEMORY_FAIL_AGE_DAYS = 30
CREDENTIAL_EXPIRY_WARN_SECONDS = 3 * 86400
PROVIDER_SNAPSHOT_WARN_SECONDS = 36 * 3600
DISK_FAIL_BYTES = 50 * 1000**3  # 50 GB floor (decimal GB, as df/Finder report)
DISK_WARN_BYTES = 75 * 1000**3
SUBPROCESS_TIMEOUT_SECONDS = 15.0


# Kickstart allowlist: labels that health-sentinel is allowed to restart on failure
KICKSTART_ALLOWLIST = frozenset(
    (
        "com.omniagentos.api",
        "com.omniagentos.runner",
        "com.omniagentos.routines",
        "com.omniagentos.health-sentinel",
        # Both halves of the hybrid Slack ingestion. The SWEEP is on this list
        # for the same reason it is a FAIL above: a dead sweep silently removes
        # the only thing that repairs a socket gap, so it must come back.
        "com.omniagentos.comms-slack-socket",
        "com.omniagentos.comms-slack-sweep",
    )
)

# --------------------------------------------------------------------------- model


@dataclass
class CheckResult:
    """One check's verdict. ``evidence`` is the operator-facing one-liner."""

    name: str
    status: str
    evidence: str
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "evidence": self.evidence,
            "detail": self.detail,
        }


def _worst(*statuses: str) -> str:
    return max(statuses, key=lambda s: _RANK.get(s, 0)) if statuses else OK


# --------------------------------------------------------------------------- io helpers


def resolve_briefings_dir() -> Path:
    """``<vault>/briefings`` as the reflection WRITER resolves it, at call time.

    ``omniagentos.reflection.report`` writes the nightly briefing under
    ``contracts.default_vault_dir()``, which honours ``OMNIAGENTOS_VAULT_DIR``
    — and ``scripts/launch-env.sh`` (sourced by health-sentinel.sh, and baked
    into the reflection plists' ``EnvironmentVariables``) points that at
    ``$OMNIAGENTOS_VAR_DIR/vault``, i.e. ``var/runtime/vault``. The
    hardcoded ``REPO_ROOT / "vault"`` this replaces was therefore reading a
    DIFFERENT directory from the one the loop writes, so a reflection that had
    genuinely landed was reported ``fail``. Resolve through the same chain.

    The package import is lazy and guarded, per this script's own rule that the
    moment it would crash is the moment its alert matters most: if
    ``omniagentos`` cannot be imported, fall back to the first link of that same
    chain (the env var every launcher exports) and only then to the repo vault.
    """
    try:
        from omniagentos.contracts import default_vault_dir  # noqa: PLC0415

        return Path(default_vault_dir()) / "briefings"
    except Exception:  # noqa: BLE001 - the sentinel degrades, it does not die
        env_vault = os.environ.get("OMNIAGENTOS_VAULT_DIR")
        return (Path(env_vault) if env_vault else REPO_ROOT / "vault") / "briefings"


def display_path(path: Path) -> str:
    """Repo-relative when it can be, absolute otherwise.

    ``Path.relative_to`` RAISES for a path outside the repo, and the vault is
    relocatable via ``OMNIAGENTOS_VAULT_DIR``, so evidence strings must not
    assume containment.
    """
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _age(seconds: float) -> str:
    """Compact human age: 42s / 7m / 3h12m / 4d."""
    seconds = max(0.0, seconds)
    if seconds < 90:
        return f"{seconds:.0f}s"
    minutes = seconds / 60
    if minutes < 90:
        return f"{minutes:.0f}m"
    hours = int(minutes // 60)
    if hours < 48:
        rem = int(minutes - hours * 60)
        return f"{hours}h{rem:02d}m"
    return f"{hours // 24}d"


def atomic_write(path: Path, text: str) -> None:
    """tmp-file-then-``os.replace`` so a reader never sees a partial snapshot."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _read_json(path: Path) -> Any:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _parse_iso(value: str) -> datetime | None:
    """Parse the ISO shapes this codebase emits (``...Z``, ``...+00:00``, naive)."""
    text = (value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _run(argv: list[str]) -> tuple[int, str]:
    """Run a tiny system command; never raise. Returns ``(rc, stdout)``."""
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return 127, f"{type(exc).__name__}: {exc}"
    return proc.returncode, proc.stdout


def _readonly_connect(db_path: Path) -> sqlite3.Connection:
    """Open the control-plane DB READ-ONLY with a short busy timeout.

    Read-only URI mode means this sentinel can never itself take a write lock on
    the DB it is inspecting, and ``timeout`` bounds the wait when someone else
    holds one — "SQLite is briefly locked" must produce a verdict, not a hang.
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def _db_path() -> Path:
    """The control-plane SQLite, honouring ``OMNIAGENTOS_DB`` like launch-env.sh."""
    env = os.environ.get("OMNIAGENTOS_DB")
    if env:
        return Path(env)
    var_dir = os.environ.get("OMNIAGENTOS_VAR_DIR")
    root = Path(var_dir) if var_dir else REPO_ROOT / "var" / "runtime"
    return root / "state.sqlite3"


# --------------------------------------------------------------------------- checks


def check_api() -> CheckResult:
    """``GET /api/health`` — reachable, ``status != degraded``, heartbeat fresh.

    The route returns ``{status, version, db, worker:{alive,last_beat_at}, event_hub}``
    (``omniagentos/api/routes/control.py``). Unreachable, non-200, unparseable,
    ``status="degraded"``, ``db=false`` and a heartbeat older than
    ``HEARTBEAT_FAIL_SECONDS`` are all failures: each one means work is not
    moving, which is the whole point of the check.
    """
    port = os.environ.get("OMNIAGENTOS_API_PORT", "8485")
    url = f"http://127.0.0.1:{port}/api/health"
    try:
        with urllib.request.urlopen(url, timeout=API_TIMEOUT_SECONDS) as response:  # noqa: S310
            code = response.getcode()
            raw = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return CheckResult(
            "api", FAIL, f"{url} returned HTTP {exc.code}", {"url": url, "http_status": exc.code}
        )
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return CheckResult(
            "api",
            FAIL,
            f"API unreachable at {url} ({type(exc).__name__}: {exc})",
            {"url": url, "error": str(exc)},
        )

    try:
        payload = json.loads(raw)
    except ValueError:
        return CheckResult(
            "api", FAIL, f"{url} returned non-JSON body ({len(raw)} bytes)", {"url": url}
        )
    if not isinstance(payload, dict):
        return CheckResult("api", FAIL, f"{url} returned a non-object body", {"url": url})

    status = str(payload.get("status") or "unknown")
    db_ok = bool(payload.get("db"))
    worker = payload.get("worker") if isinstance(payload.get("worker"), dict) else {}
    beat_raw = str(worker.get("last_beat_at") or "")
    beat_at = _parse_iso(beat_raw)
    beat_age = (_now() - beat_at).total_seconds() if beat_at else None
    detail: dict[str, Any] = {
        "url": url,
        "http_status": code,
        "status": status,
        "db": db_ok,
        "version": payload.get("version"),
        "worker_alive": bool(worker.get("alive")),
        "last_beat_at": beat_raw or None,
        "heartbeat_age_seconds": beat_age,
    }

    problems: list[str] = []
    verdict = OK
    if code != 200:
        problems.append(f"HTTP {code}")
        verdict = _worst(verdict, FAIL)
    if status != "ok":
        problems.append(f"status={status}")
        verdict = _worst(verdict, FAIL)
    if not db_ok:
        problems.append("db=false")
        verdict = _worst(verdict, FAIL)
    if beat_age is None:
        problems.append("no worker heartbeat")
        verdict = _worst(verdict, FAIL)
    elif beat_age > HEARTBEAT_FAIL_SECONDS:
        problems.append(f"heartbeat {_age(beat_age)} old")
        verdict = _worst(verdict, FAIL)
    elif beat_age > HEARTBEAT_WARN_SECONDS:
        problems.append(f"heartbeat {_age(beat_age)} old")
        verdict = _worst(verdict, WARN)

    if verdict == OK:
        evidence = f"API ok on :{port} (v{payload.get('version')}, heartbeat {_age(beat_age or 0)})"
    else:
        evidence = f"API on :{port} — " + "; ".join(problems)
    return CheckResult("api", verdict, evidence, detail)


def _process_table() -> list[tuple[int, str]]:
    """``(pid, full command line)`` for every process, via ``ps -Awwo``.

    ``-ww`` matters: without it macOS truncates the command to the terminal
    width and the ``-m omniagentos.runner`` suffix — the only thing that
    identifies the runner — is exactly what gets cut off.
    """
    rc, out = _run(["/bin/ps", "-Awwo", "pid=,command="])
    if rc != 0:
        return []
    rows: list[tuple[int, str]] = []
    for line in out.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        pid_text, _, command = stripped.partition(" ")
        try:
            rows.append((int(pid_text), command.strip()))
        except ValueError:
            continue
    return rows


def check_runner() -> CheckResult:
    """The step-polling runtime worker is alive AND is THIS checkout's.

    ``scripts/launch-omniagentos.sh``'s ``_runner`` execs
    ``$ROOT/.venv/bin/python -m omniagentos.runner``, so the repo root is part of
    the command line. A sibling checkout (``~/OmniAgentOS-main`` and the swarm
    worktrees are both real on this box) running its own runner must NOT be
    allowed to read as this fleet's runner being up — that is precisely how a
    dead runner stays invisible while runs queue forever.
    """
    processes = _process_table()
    if not processes:
        return CheckResult("runner", FAIL, "could not enumerate processes (ps failed)", {})

    root = str(REPO_ROOT)
    mine: list[dict[str, Any]] = []
    foreign: list[dict[str, Any]] = []
    for pid, command in processes:
        if "omniagentos.runner" not in command:
            continue
        entry = {"pid": pid, "command": command[:300]}
        (mine if root in command else foreign).append(entry)

    detail = {"repo_root": root, "matched": mine, "foreign": foreign}
    if mine:
        pids = ", ".join(str(p["pid"]) for p in mine)
        return CheckResult("runner", OK, f"runner alive (pid {pids})", detail)
    if foreign:
        pids = ", ".join(str(p["pid"]) for p in foreign)
        return CheckResult(
            "runner",
            FAIL,
            f"no runner for {root}; {len(foreign)} runner(s) belong to another checkout "
            f"(pid {pids}) — queued runs will never move",
            detail,
        )
    return CheckResult(
        "runner",
        FAIL,
        "no `python -m omniagentos.runner` process — queued runs will never move "
        "(start: scripts/launch-omniagentos.sh runner)",
        detail,
    )


def _last_tick(path: Path, *, tail_bytes: int = 262144) -> tuple[datetime | None, str | None]:
    """Newest ``checked_at`` in ``routines.log``, read from the file's tail.

    The log interleaves JSON tick records with plain deprecation-warning lines,
    so mtime alone is not evidence of a tick; only a parseable record with a
    ``checked_at`` counts.
    """
    try:
        size = path.stat().st_size
        with open(path, "rb") as handle:
            if size > tail_bytes:
                handle.seek(size - tail_bytes)
                handle.readline()  # discard the partial first line
            chunk = handle.read().decode("utf-8", "replace")
    except OSError as exc:
        return None, f"{type(exc).__name__}: {exc}"

    for line in reversed(chunk.splitlines()):
        text = line.strip()
        if not text.startswith("{"):
            continue
        try:
            record = json.loads(text)
        except ValueError:
            continue
        if isinstance(record, dict) and record.get("checked_at"):
            stamp = _parse_iso(str(record["checked_at"]))
            if stamp is not None:
                return stamp, None
    return None, "no parseable tick record in the log tail"


def check_scheduler() -> CheckResult:
    """``var/log/routines.log``'s newest tick is younger than 15 minutes."""
    if not ROUTINES_LOG.exists():
        return CheckResult(
            "scheduler",
            FAIL,
            f"{ROUTINES_LOG.relative_to(REPO_ROOT)} does not exist — the routines tick has "
            "never run here",
            {"path": str(ROUTINES_LOG)},
        )
    stamp, error = _last_tick(ROUTINES_LOG)
    if stamp is None:
        return CheckResult(
            "scheduler",
            FAIL,
            f"no scheduler tick found in {ROUTINES_LOG.name} ({error})",
            {"path": str(ROUTINES_LOG), "error": error},
        )
    age = (_now() - stamp).total_seconds()
    detail = {"path": str(ROUTINES_LOG), "last_tick_at": _iso(stamp), "age_seconds": age}
    if age > TICK_FAIL_SECONDS:
        return CheckResult(
            "scheduler",
            FAIL,
            f"scheduler tick is {_age(age)} old (last {_iso(stamp)}; cadence ~5m, floor 15m) "
            "— routines are not firing",
            detail,
        )
    if age > TICK_WARN_SECONDS:
        return CheckResult(
            "scheduler", WARN, f"scheduler tick is {_age(age)} old (last {_iso(stamp)})", detail
        )
    return CheckResult("scheduler", OK, f"scheduler ticked {_age(age)} ago", detail)


def _load_enabled_claude_accounts() -> tuple[list[dict[str, Any]], str | None]:
    """Accounts marked ``enabled: true`` under ``providers.claude`` in accounts.yaml.

    ``discover_glob`` is deliberately NOT expanded: the config comments state that
    every non-authenticating profile is listed explicitly-and-disabled so the glob
    cannot silently re-admit it, and this check must honour that same intent.
    """
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - venv always has pyyaml
        return [], f"pyyaml unavailable ({exc})"
    try:
        data = yaml.safe_load(ACCOUNTS_YAML.read_text(encoding="utf-8"))
    except (OSError, Exception) as exc:  # noqa: BLE001 - yaml raises its own tree
        return [], f"could not read {ACCOUNTS_YAML.name} ({type(exc).__name__}: {exc})"
    if not isinstance(data, dict):
        return [], f"{ACCOUNTS_YAML.name} is not a mapping"
    providers = data.get("providers")
    claude = providers.get("claude") if isinstance(providers, dict) else None
    accounts = claude.get("accounts") if isinstance(claude, dict) else None
    if not isinstance(accounts, list):
        return [], f"{ACCOUNTS_YAML.name} has no providers.claude.accounts list"
    enabled: list[dict[str, Any]] = []
    for entry in accounts:
        if not isinstance(entry, dict):
            continue
        if entry.get("enabled", True) is not True:
            continue
        config_dir = entry.get("config_dir")
        if not config_dir:
            continue
        enabled.append(
            {
                "id": str(entry.get("id") or config_dir),
                "config_dir": str(Path(str(config_dir)).expanduser()),
            }
        )
    return enabled, None


def _credential_verdict(config_dir: Path) -> tuple[str, str, dict[str, Any]]:
    """Judge one ``CLAUDE_CONFIG_DIR`` from its on-disk OAuth credential only.

    Reads ``{config_dir}/.credentials.json`` -> ``claudeAiOauth`` and looks at the
    two stamps the CLI writes there. NOTHING is spawned and no secret value is
    read out of the file — only the two integer expiry stamps and the
    subscription label.

    Observed truth on this machine (2026-08-01): a healthy profile carries
    ``expiresAt: 0`` plus a FUTURE ``refreshTokenExpiresAt`` (the CLI refreshes
    the access token on demand); the two profiles the operator had already
    live-verified as dead carry a PAST ``expiresAt`` and no
    ``refreshTokenExpiresAt`` at all. So a past access-token expiry is only
    damning when there is no refresh token to redeem it with.
    """
    path = config_dir / ".credentials.json"
    if not config_dir.is_dir():
        return FAIL, "config dir missing", {"config_dir": str(config_dir)}
    if not path.exists():
        return FAIL, "no .credentials.json (never logged in / logged out)", {"path": str(path)}
    try:
        data = _read_json(path)
    except (OSError, ValueError) as exc:
        return FAIL, f"credentials unreadable ({type(exc).__name__})", {"error": str(exc)}
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    if not isinstance(oauth, dict):
        return FAIL, "credentials have no claudeAiOauth block", {"path": str(path)}

    now = time.time()

    def _seconds_left(key: str) -> float | None:
        raw = oauth.get(key)
        if not isinstance(raw, (int, float)) or isinstance(raw, bool) or raw <= 0:
            return None
        return float(raw) / 1000.0 - now

    access_left = _seconds_left("expiresAt")
    refresh_left = _seconds_left("refreshTokenExpiresAt")
    detail = {
        "path": str(path),
        "subscription": oauth.get("subscriptionType"),
        "access_token_seconds_left": access_left,
        "refresh_token_seconds_left": refresh_left,
        "credentials_mtime": _iso(datetime.fromtimestamp(path.stat().st_mtime, UTC)),
    }

    if refresh_left is not None:
        if refresh_left <= 0:
            return FAIL, f"refresh token expired {_age(-refresh_left)} ago", detail
        if refresh_left < CREDENTIAL_EXPIRY_WARN_SECONDS:
            return WARN, f"refresh token expires in {_age(refresh_left)}", detail
        return OK, f"credentials valid ({_age(refresh_left)} of refresh left)", detail
    if access_left is None:
        return WARN, "credentials carry no usable expiry stamp", detail
    if access_left <= 0:
        return FAIL, f"access token expired {_age(-access_left)} ago, no refresh token", detail
    return OK, f"credentials valid ({_age(access_left)} of access left)", detail


def _db_account_rows() -> tuple[dict[str, dict[str, Any]], str | None]:
    """``claude_accounts`` rows keyed by resolved ``config_dir``.

    This is the runtime's OWN verdict — ``accounts.service.mark_status`` writes
    ``status='error'`` with the exact adapter/CLI failure text — so it is the
    cheapest possible live probe: it was already paid for by a real spawn.
    """
    path = _db_path()
    if not path.exists():
        return {}, f"control-plane DB missing at {path}"
    try:
        conn = _readonly_connect(path)
    except sqlite3.Error as exc:
        return {}, f"{type(exc).__name__}: {exc}"
    try:
        rows = conn.execute(
            "SELECT id, label, config_dir, enabled, status, status_detail, updated_at, "
            "paused_until, cooldown_until FROM claude_accounts "
            "WHERE provider = 'claude' AND config_dir IS NOT NULL"
        ).fetchall()
    except sqlite3.Error as exc:
        return {}, f"{type(exc).__name__}: {exc}"
    finally:
        conn.close()

    table: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = str(Path(str(row["config_dir"])).expanduser())
        table[key] = {
            "id": row["id"],
            "label": row["label"],
            "enabled": bool(row["enabled"]),
            "status": row["status"],
            "status_detail": (row["status_detail"] or "")[:200] or None,
            "updated_at": row["updated_at"],
            "paused_until": row["paused_until"],
            "cooldown_until": row["cooldown_until"],
        }
    return table, None


def check_claude_pool() -> CheckResult:
    """Every Claude account the pool is allowed to pick still has usable auth.

    TWO independent sources, because on this box they DISAGREE and each alone
    lies in a different direction:

    * the credential file says a profile is fine whenever its refresh token is
      still dated in the future — but a server-side revocation
      (``401 OAuth access token has been revoked``) leaves that file untouched,
      so disk alone reports a revoked account as healthy;
    * the ``claude_accounts`` row carries the real 401 the runtime hit, but it is
      only as fresh as the last spawn that touched that profile, so the DB alone
      can be silent about a profile nobody has used since it broke.

    A profile is DEAD if EITHER source condemns it. The check fails when any
    yaml-enabled account is dead, and separately warns when the yaml pool and the
    DB rotation disagree about ``enabled`` — that divergence is itself how
    "agents lost their API access" happens quietly.
    """
    accounts, config_error = _load_enabled_claude_accounts()
    if config_error:
        return CheckResult("claude_pool", FAIL, config_error, {"error": config_error})
    if not accounts:
        return CheckResult(
            "claude_pool",
            FAIL,
            f"no Claude account is enabled in {ACCOUNTS_YAML.name} — the pool is empty",
            {"pool_size": 0},
        )

    db_rows, db_error = _db_account_rows()
    results: list[dict[str, Any]] = []
    dead: list[str] = []
    degraded: list[str] = []
    diverged: list[str] = []
    disagreements: list[dict[str, Any]] = []
    yaml_config_dirs = {str(account["config_dir"]) for account in accounts}
    now = _now()

    def _active_window(row: dict[str, Any], field: str) -> datetime | None:
        if not row.get("enabled"):
            return None
        until = _parse_iso(str(row.get(field) or ""))
        return until if until is not None and until > now else None

    for account in accounts:
        config_dir = Path(account["config_dir"])
        cred_status, cred_reason, cred_detail = _credential_verdict(config_dir)
        row = db_rows.get(str(config_dir))
        if (
            cred_status == FAIL
            and "no .credentials.json" in cred_reason
            and row is not None
            and str(row.get("status") or "") == "ok"
        ):
            # Keychain-backed logins never write .credentials.json; the runtime's
            # own mark_status row is the better witness for those profiles
            # (live-probe verified 2026-08-01: account-5 authenticates with no
            # credential file on disk).
            cred_status = OK
            cred_reason = "keychain-auth (no credential file; runtime row ok)"
        # (severity, text) pairs so the evidence line can quote the reason that
        # actually condemned the account, not merely the last one appended.
        graded: list[tuple[str, str]] = [(cred_status, cred_reason)]

        if row is not None:
            db_status = str(row.get("status") or "unknown")
            if db_status == "error":
                graded.append(
                    (FAIL, f"runtime marked error: {row.get('status_detail') or 'no detail'}")
                )
            elif db_status == "rate_limited":
                graded.append((WARN, "runtime marked rate_limited"))
            if not row.get("enabled"):
                diverged.append(account["id"])
                graded.append((WARN, "disabled in claude_accounts (yaml says enabled)"))
                disagreements.append(
                    {"kind": "db_disabled", "id": account["id"], "config_dir": str(config_dir)}
                )
            paused_until = _active_window(row, "paused_until")
            if paused_until is not None:
                graded.append((WARN, f"runtime paused until {_iso(paused_until)}"))
                disagreements.append(
                    {
                        "kind": "db_paused",
                        "id": account["id"],
                        "config_dir": str(config_dir),
                        "until": _iso(paused_until),
                    }
                )
            cooldown_until = _active_window(row, "cooldown_until")
            if cooldown_until is not None:
                graded.append((WARN, f"runtime cooling until {_iso(cooldown_until)}"))
                disagreements.append(
                    {
                        "kind": "db_cooling",
                        "id": account["id"],
                        "config_dir": str(config_dir),
                        "until": _iso(cooldown_until),
                    }
                )
        elif not db_error:
            graded.append((WARN, "no claude_accounts row — never registered with the spawner"))
            disagreements.append(
                {"kind": "db_missing", "id": account["id"], "config_dir": str(config_dir)}
            )

        status = _worst(*(severity for severity, _ in graded))
        reasons = [text for _, text in graded]
        worst_reason = next((text for severity, text in graded if severity == status), reasons[0])

        entry = {
            "id": account["id"],
            "config_dir": str(config_dir),
            "status": status,
            "worst_reason": worst_reason,
            "reasons": reasons,
            "credentials": cred_detail,
            "db_row": row,
        }
        results.append(entry)
        if status == FAIL:
            dead.append(account["id"])
        elif status == WARN:
            degraded.append(account["id"])

    for config_dir, row in db_rows.items():
        if not row.get("enabled") or config_dir in yaml_config_dirs:
            continue
        account_id = str(row.get("id") or config_dir)
        disagreements.append({"kind": "config_missing", "id": account_id, "config_dir": config_dir})
        for window_field, kind in (
            ("paused_until", "db_paused"),
            ("cooldown_until", "db_cooling"),
        ):
            until = _active_window(row, window_field)
            if until is not None:
                disagreements.append(
                    {
                        "kind": kind,
                        "id": account_id,
                        "config_dir": config_dir,
                        "until": _iso(until),
                    }
                )

    live = [entry["id"] for entry in results if entry["status"] == OK]
    detail: dict[str, Any] = {
        "pool_size": len(accounts),
        "live": live,
        "degraded": degraded,
        "dead": dead,
        "db_divergence": diverged,
        "disagreements": disagreements,
        "db_error": db_error,
        "accounts": results,
    }

    if dead:
        why = "; ".join(
            f"{entry['id']}: {entry['worst_reason']}"
            for entry in results
            if entry["status"] == FAIL
        )
        return CheckResult(
            "claude_pool",
            FAIL,
            f"{len(dead)}/{len(accounts)} enabled Claude accounts are DEAD; "
            f"live: {', '.join(live) or 'NONE'} — {why}",
            detail,
        )
    if degraded or disagreements or db_error:
        bits = []
        if degraded:
            bits.append(f"{len(degraded)} degraded ({', '.join(degraded)})")
        if diverged:
            bits.append(f"{len(diverged)} config/DB enabled-state disagreement(s)")
        if disagreements:
            kinds = ", ".join(sorted({str(item["kind"]) for item in disagreements}))
            bits.append(f"{len(disagreements)} disagreement(s): {kinds}")
        if db_error:
            bits.append(f"claude_accounts unreadable: {db_error}")
        return CheckResult(
            "claude_pool",
            WARN,
            f"Claude pool {len(live)}/{len(accounts)} clean — " + "; ".join(bits),
            detail,
        )
    return CheckResult(
        "claude_pool",
        OK,
        f"Claude pool healthy: {len(live)}/{len(accounts)} accounts ({', '.join(live)})",
        detail,
    )


def check_memory() -> CheckResult:
    """Durable memory is present: MEMORY.md, the memlife store, the memlife tables."""
    problems: list[str] = []
    verdict = OK
    detail: dict[str, Any] = {}

    try:
        stat = MEMORY_MD.stat()
    except OSError as exc:
        problems.append(f"MEMORY.md unreadable at {MEMORY_MD} ({type(exc).__name__})")
        verdict = FAIL
        detail["memory_md"] = {"path": str(MEMORY_MD), "error": str(exc)}
    else:
        age = time.time() - stat.st_mtime
        detail["memory_md"] = {
            "path": str(MEMORY_MD),
            "bytes": stat.st_size,
            "mtime": _iso(datetime.fromtimestamp(stat.st_mtime, UTC)),
            "age_seconds": age,
        }
        if stat.st_size == 0:
            problems.append("MEMORY.md is EMPTY")
            verdict = FAIL
        elif age < -3600:
            problems.append(f"MEMORY.md mtime is {_age(-age)} in the FUTURE (clock/copy fault)")
            verdict = _worst(verdict, WARN)
        elif age > MEMORY_FAIL_AGE_DAYS * 86400:
            problems.append(f"MEMORY.md untouched for {_age(age)}")
            verdict = _worst(verdict, FAIL)
        elif age > MEMORY_WARN_AGE_DAYS * 86400:
            problems.append(f"MEMORY.md untouched for {_age(age)}")
            verdict = _worst(verdict, WARN)

    store = Path(os.environ.get("OMNIAGENTOS_MEMLIFE_STORE") or MEMLIFE_STORE)
    detail["memlife_store"] = {"path": str(store), "exists": store.is_dir()}
    if not store.is_dir():
        problems.append(f"memlife store missing at {store}")
        verdict = _worst(verdict, FAIL)

    db_file = _db_path()
    db_detail: dict[str, Any] = {"path": str(db_file), "exists": db_file.exists()}
    if not db_file.exists():
        problems.append(f"control-plane DB missing at {db_file}")
        verdict = FAIL
    else:
        db_detail["bytes"] = db_file.stat().st_size
        try:
            conn = _readonly_connect(db_file)
            try:
                counts = {
                    name: conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
                    for name in ("memlife_candidates", "memlife_lessons")
                }
            finally:
                conn.close()
        except sqlite3.Error as exc:
            problems.append(f"memlife tables unreadable ({type(exc).__name__}: {exc})")
            verdict = _worst(verdict, FAIL)
        else:
            db_detail["counts"] = counts
            if counts.get("memlife_candidates", 0) == 0:
                problems.append("memlife_candidates is empty")
                verdict = _worst(verdict, WARN)
    detail["control_plane_db"] = db_detail

    if verdict == OK:
        size = detail["memory_md"]["bytes"]
        age = detail["memory_md"]["age_seconds"]
        return CheckResult(
            "memory",
            OK,
            f"memory intact (MEMORY.md {size}B, touched {_age(age)} ago; "
            f"memlife store + tables present)",
            detail,
        )
    return CheckResult("memory", verdict, "; ".join(problems), detail)


def check_reflection() -> CheckResult:
    """A reflection briefing exists for today or yesterday (25h window)."""
    today = _now().date()
    yesterday = today - timedelta(days=1)
    briefings = resolve_briefings_dir()
    candidates = [briefings / f"reflection-{day.isoformat()}.md" for day in (today, yesterday)]
    found = [path for path in candidates if path.exists() and path.stat().st_size > 0]
    detail = {
        "window_hours": REFLECTION_WINDOW_HOURS,
        "briefings_dir": str(briefings),
        "looked_for": [str(path) for path in candidates],
        "found": [str(path) for path in found],
    }
    if found:
        newest = max(found, key=lambda p: p.stat().st_mtime)
        age = time.time() - newest.stat().st_mtime
        return CheckResult(
            "reflection", OK, f"reflection briefing {newest.name} present ({_age(age)} old)", detail
        )

    alerts = sorted(briefings.glob("reflection-ALERT-*.md")) if briefings.is_dir() else []
    if alerts:
        detail["latest_alert"] = str(alerts[-1])
    return CheckResult(
        "reflection",
        FAIL,
        f"no reflection briefing for {today} or {yesterday} in "
        f"{display_path(briefings)} — the nightly loop did not produce one"
        + (f" (latest watchdog alert: {alerts[-1].name})" if alerts else ""),
        detail,
    )


def _provider_status_problem(status: Any) -> str | None:
    if not isinstance(status, dict):
        return "status entry is not a mapping"
    ok = status.get("ok")
    if not isinstance(ok, bool):
        return "ok is missing or not boolean"
    if "outcome" in status:
        outcome = status["outcome"]
        if not isinstance(outcome, str) or outcome not in _PROVIDER_OUTCOMES:
            return f"unknown outcome {outcome!r}"
        if ok is True and outcome != "ok":
            return f"contradictory ok=true with outcome={outcome}"
        if ok is False and outcome == "ok":
            return "contradictory ok=false with outcome=ok"
    for status_field in ("clean_exit", "kill_within_5s"):
        if status_field in status and not isinstance(status[status_field], bool):
            return f"{status_field} is not boolean"
    if "stream_events" in status:
        stream_events = status["stream_events"]
        if (
            not isinstance(stream_events, int)
            or isinstance(stream_events, bool)
            or stream_events < 0
        ):
            return "stream_events is not a non-negative integer"
    return None


def check_providers() -> CheckResult:
    """``var/provider-health.json`` parses and no provider is failing doctor."""
    if not PROVIDER_HEALTH.exists():
        return CheckResult(
            "providers",
            FAIL,
            f"{PROVIDER_HEALTH.relative_to(REPO_ROOT)} missing — provider-sentinel has never "
            "written a snapshot",
            {"path": str(PROVIDER_HEALTH)},
        )
    try:
        payload = _read_json(PROVIDER_HEALTH)
    except (OSError, ValueError) as exc:
        return CheckResult(
            "providers",
            FAIL,
            f"{PROVIDER_HEALTH.name} unreadable ({type(exc).__name__}: {exc})",
            {"path": str(PROVIDER_HEALTH), "error": str(exc)},
        )
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, dict):
        return CheckResult(
            "providers", FAIL, f"{PROVIDER_HEALTH.name} has no results mapping", {"payload": "bad"}
        )

    stamp = _parse_iso(str(payload.get("ts") or ""))
    snapshot_age = (_now() - stamp).total_seconds() if stamp else None
    failing: list[str] = []
    reasons: dict[str, str] = {}
    malformed: dict[str, str] = {}
    for key, status in sorted(results.items()):
        problem = _provider_status_problem(status)
        if problem is not None:
            malformed[key] = problem
        if not isinstance(status, dict) or not isinstance(status.get("ok"), bool):
            continue
        if status.get("ok"):
            continue
        failing.append(key)
        why = []
        if status.get("outcome"):
            why.append(f"outcome={status['outcome']}")
        if status.get("clean_exit") is False:
            why.append("clean_exit=false")
        if status.get("stream_events") == 0:
            why.append("no stream events")
        if status.get("kill_within_5s") is False:
            why.append("did not die within 5s")
        if status.get("error"):
            why.append(str(status["error"])[:160])
        if problem is not None:
            why.append(f"unparseable: {problem}")
        reasons[key] = ", ".join(why) or "doctor reported not-ok"

    detail = {
        "path": str(PROVIDER_HEALTH),
        "snapshot_ts": payload.get("ts"),
        "snapshot_age_seconds": snapshot_age,
        "total": len(results),
        "failing": failing,
        "reasons": reasons,
        "malformed": malformed,
    }
    if failing:
        summary = "; ".join(f"{key} ({reasons[key]})" for key in failing)
        return CheckResult(
            "providers",
            FAIL,
            f"{len(failing)}/{len(results)} providers failing doctor: {summary}",
            detail,
        )
    if malformed:
        summary = "; ".join(f"{key} ({reason})" for key, reason in malformed.items())
        stale = (
            f"; snapshot is {_age(snapshot_age)} old"
            if snapshot_age is not None and snapshot_age > PROVIDER_SNAPSHOT_WARN_SECONDS
            else ""
        )
        return CheckResult(
            "providers",
            WARN,
            f"provider snapshot has {len(malformed)} unparseable status entry: {summary}{stale}",
            detail,
        )
    if snapshot_age is not None and snapshot_age > PROVIDER_SNAPSHOT_WARN_SECONDS:
        return CheckResult(
            "providers",
            WARN,
            f"all {len(results)} providers ok but the snapshot is {_age(snapshot_age)} old "
            "(provider-sentinel may not be running)",
            detail,
        )
    return CheckResult("providers", OK, f"all {len(results)} providers ok", detail)


def _launchctl_table() -> tuple[dict[str, tuple[str, str]], str | None]:
    """``label -> (pid, last_exit_status)`` from ``launchctl list``."""
    rc, out = _run(["/bin/launchctl", "list"])
    if rc != 0:
        return {}, f"launchctl list failed (rc={rc})"
    table: dict[str, tuple[str, str]] = {}
    for line in out.splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        table[parts[2].strip()] = (parts[0].strip(), parts[1].strip())
    return table, None


def check_launchd() -> CheckResult:
    """Meta-watchdog: the scheduled jobs themselves are installed, loaded and exiting 0.

    Expected-set construction, deliberately in two tiers:

    * INSTALLED — every ``com.omniagentos.*.plist`` actually present in
      ``~/Library/LaunchAgents``. Installed-but-not-loaded, or loaded with a
      nonzero last exit status, is a FAIL: the operator asked for that job.
    * RENDERED — every plist this repo has rendered into
      ``var/launchd/rendered``. Rendered-but-not-installed is a WARN, because the
      repo's own installers are render-only by convention. It is included
      because on 2026-08-01 the INSTALLED set was EMPTY while sixteen jobs were
      rendered — an installed-only expectation would have reported a perfectly
      healthy silence over a completely unloaded fleet.
    """
    loaded, launchctl_error = _launchctl_table()
    installed = {
        path.stem: path
        for path in sorted(LAUNCH_AGENTS_DIR.glob(f"{LAUNCHD_PREFIX}*.plist"))
        if path.stem.startswith(LAUNCHD_PREFIX)
    }
    rendered = {
        path.stem
        for path in sorted(RENDERED_LAUNCHD_DIR.glob(f"{LAUNCHD_PREFIX}*.plist"))
        if path.stem.startswith(LAUNCHD_PREFIX)
    }
    loaded_ours = {
        label: value for label, value in loaded.items() if label.startswith(LAUNCHD_PREFIX)
    }

    not_loaded = sorted(label for label in installed if label not in loaded_ours)
    bad_exit: dict[str, str] = {}
    bad_exit_running: list[str] = []
    signal_restarted: dict[str, str] = {}
    for label, (pid, status) in sorted(loaded_ours.items()):
        if status in ("0", "-"):
            continue
        alive = pid not in (None, "", "-")
        try:
            signal_exit = int(status) < 0
        except ValueError:
            signal_exit = False
        if alive and signal_exit:
            # A KeepAlive daemon that is running NOW with a negative last exit
            # was signal-terminated and relaunched — the normal aftermath of
            # `launchctl kickstart -k` or an operator kill, not a crash loop.
            # A POSITIVE exit with a live PID is a respawn after a real crash
            # and stays a FAIL.
            signal_restarted[label] = status
        else:
            bad_exit[label] = status
            if alive:
                bad_exit_running.append(label)
    not_installed = sorted(rendered - set(installed))

    detail = {
        "launch_agents_dir": str(LAUNCH_AGENTS_DIR),
        "installed": sorted(installed),
        "loaded": sorted(loaded_ours),
        "rendered": sorted(rendered),
        "installed_not_loaded": not_loaded,
        "nonzero_last_exit": bad_exit,
        # Which of those are still RUNNING.  A failed job that launchd has
        # already respawned needs its wedged instance killed (`kickstart -k`);
        # one that is down must only be started (`kickstart`).  See
        # _apply_kickstart.
        "nonzero_last_exit_running": sorted(bad_exit_running),
        "signal_restarted_running": signal_restarted,
        "rendered_not_installed": not_installed,
        "launchctl_error": launchctl_error,
        # This is populated by _apply_kickstart after the check.  Keeping it
        # present even on a healthy pass gives snapshots a stable audit shape.
        "heal_attempts": [],
    }

    if launchctl_error:
        return CheckResult("launchd", FAIL, launchctl_error, detail)

    problems: list[str] = []
    verdict = OK
    if not_loaded:
        problems.append(f"{len(not_loaded)} installed but NOT loaded ({', '.join(not_loaded)})")
        verdict = FAIL
    if bad_exit:
        problems.append(
            f"{len(bad_exit)} exited nonzero ("
            + ", ".join(f"{label}={status}" for label, status in bad_exit.items())
            + ")"
        )
        verdict = FAIL
    if signal_restarted:
        problems.append(
            f"{len(signal_restarted)} signal-restarted but running ("
            + ", ".join(f"{label}={status}" for label, status in signal_restarted.items())
            + ")"
        )
        verdict = _worst(verdict, WARN)
    if not_installed:
        problems.append(
            f"{len(not_installed)} rendered but never installed into ~/Library/LaunchAgents "
            f"({', '.join(not_installed)})"
        )
        verdict = _worst(verdict, WARN)

    if verdict == OK:
        return CheckResult(
            "launchd",
            OK,
            f"{len(loaded_ours)}/{len(installed)} {LAUNCHD_PREFIX}* jobs loaded, all last-exit 0",
            detail,
        )
    return CheckResult(
        "launchd", verdict, f"{LAUNCHD_PREFIX}* jobs — " + "; ".join(problems), detail
    )


def check_mechanism_registry(
    *, registry_path: Path = MECHANISM_REGISTRY_PATH
) -> CheckResult:
    """Verify freshness, consumers, and launchd reality for every registered mechanism.

    Disabled mechanisms retain the required ``freshness_SLA`` field with a
    ``null`` value.  That explicitly records that no current output is expected
    while parked; a numeric SLA means the output must exist and be fresh whether
    or not the entry has a launchd label.
    """
    try:
        entries, error = load_registry(registry_path)
        if error:
            return CheckResult("mechanism_registry", FAIL, error, {"path": str(registry_path)})

        now = _now().timestamp()
        stale: list[str] = []
        missing_output: list[str] = []
        skewed: list[str] = []
        consumerless: list[str] = []
        freshness_checked = 0
        for entry in entries:
            mechanism_id = entry["id"]
            consumers = [
                value.strip()
                for value in entry["named_consumer"]
                if value.strip() and value.strip() not in _OBSERVER_CONSUMERS
            ]
            if not consumers:
                consumerless.append(mechanism_id)
            sla = entry["freshness_SLA"]
            if sla is None:
                continue
            output = Path(entry["expected_output_path"])
            try:
                age = now - output.stat().st_mtime
            except OSError:
                missing_output.append(
                    f"{mechanism_id} (output missing: {output} — dead mechanism OR wrong registry entry)"
                )
                continue
            freshness_checked += 1
            if age < -3600:
                skewed.append(f"{mechanism_id} (output {_age(-age)} in the future; clock/copy fault)")
            elif age > float(sla):
                stale.append(f"{mechanism_id} (output {_age(age)} old; SLA {_age(float(sla))})")

        loaded, launchctl_error = _launchctl_table()
        drifts: list[dict[str, Any]] = []
        drift_error: str | None = None
        if not launchctl_error:
            drifts, drift_error = _mechanism_drift.detect_drift(
                registry_path=registry_path, loaded_labels=set(loaded), entries=entries
            )

        def _bucket(name: str, values: list[str]) -> str:
            count = len(values)
            return (
                f"{name} {count}: "
                + ", ".join(values[:5])
                + (f" (+{count - 5} more)" if count > 5 else "")
            )

        problems: list[str] = []
        verdict = OK
        if stale:
            problems.append(_bucket("stale", stale))
            verdict = _worst(verdict, FAIL)
        if missing_output:
            problems.append(_bucket("missing output", missing_output))
            verdict = _worst(verdict, FAIL)
        if skewed:
            problems.append(_bucket("future-skewed", skewed))
            verdict = _worst(verdict, WARN)
        if consumerless:
            problems.append(_bucket("zero real consumers", consumerless))
            verdict = _worst(verdict, WARN)
        if drifts:
            drift_descriptions = [
                f"{item['id']} (launchd={'enabled' if item['launchd_enabled'] else 'disabled'}, "
                f"registry={'enabled' if item['registry_enabled'] else 'disabled'})"
                for item in drifts
            ]
            problems.append(_bucket("launchd drift", drift_descriptions))
            verdict = _worst(verdict, FAIL)
        if launchctl_error:
            problems.append(launchctl_error)
            verdict = _worst(verdict, FAIL)
        if drift_error:
            problems.append(f"could not compare registry with launchd ({drift_error})")
            verdict = _worst(verdict, FAIL)
        labeled_total = sum(entry["launchd_label"] is not None for entry in entries)
        labeled_matched = labeled_total - len(drifts) if not (launchctl_error or drift_error) else 0
        detail = {
            "path": str(registry_path),
            "entry_count": len(entries),
            "freshness_checked": freshness_checked,
            "stale": stale,
            "missing_output": missing_output,
            "skewed": skewed,
            "consumerless": consumerless,
            "drifts": drifts,
            "labeled_total": labeled_total,
            "labeled_matched": labeled_matched,
        }
        if problems:
            return CheckResult("mechanism_registry", verdict, "; ".join(problems), detail)
        return CheckResult(
            "mechanism_registry",
            OK,
            f"{len(entries)} registered; {freshness_checked}/{len(entries)} freshness-checked; "
            f"{labeled_matched}/{labeled_total} launchd-labeled matched",
            detail,
        )
    except Exception as exc:  # noqa: BLE001 - this check is a total sentinel boundary
        return CheckResult(
            "mechanism_registry",
            FAIL,
            f"mechanism registry check failed ({type(exc).__name__}: {exc})",
            {"path": str(registry_path)},
        )


def check_disk() -> CheckResult:
    """Free space on ``/System/Volumes/Data`` above the 50 GB floor."""
    try:
        usage = shutil.disk_usage(DATA_VOLUME)
    except OSError as exc:
        return CheckResult(
            "disk", FAIL, f"could not stat {DATA_VOLUME} ({type(exc).__name__}: {exc})", {}
        )
    free_gb = usage.free / 1000**3
    detail = {
        "volume": DATA_VOLUME,
        "free_bytes": usage.free,
        "free_gb": round(free_gb, 1),
        "total_gb": round(usage.total / 1000**3, 1),
        "floor_gb": DISK_FAIL_BYTES / 1000**3,
    }
    if usage.free < DISK_FAIL_BYTES:
        return CheckResult(
            "disk", FAIL, f"{DATA_VOLUME} has {free_gb:.1f} GB free (floor 50 GB)", detail
        )
    if usage.free < DISK_WARN_BYTES:
        return CheckResult(
            "disk", WARN, f"{DATA_VOLUME} has {free_gb:.1f} GB free (floor 50 GB)", detail
        )
    return CheckResult("disk", OK, f"{DATA_VOLUME} has {free_gb:.1f} GB free", detail)


# Mirrors omniagentos.revenue.collect._CONSECUTIVE_FAIL_THRESHOLD. Kept as an
# independent literal rather than an import: this script reads every other
# subsystem's state by raw SQL/file, deliberately never importing the
# omniagentos package, so a crash inside an unrelated import is never how the
# sentinel discovers a dead system (see module docstring).
_REVENUE_FAIL_THRESHOLD = 2


def check_revenue() -> CheckResult:
    """The revenue collector's fail-loud data-quality streak, read directly.

    ``omniagentos.revenue.collect.collect_day`` persists one row per
    (vertical, source) in ``revenue_source_status`` every real (non-dry-run)
    pass: 'ok', 'failed', or 'unconfigured', plus a running consecutive-day
    failure streak (an unconfigured source counts exactly like a failing one
    -- see that module for why). This is the second half of the fail-loud
    contract: the collector can no longer both go RED and stay invisible,
    because THIS check turns a persisted RED streak into a failing sentinel
    check independent of whatever the collector's own exit code was.
    """
    try:
        with _readonly_connect(_db_path()) as conn:
            rows = conn.execute(
                "SELECT status_key, vertical, source, last_day, last_status, "
                "last_message, consecutive_failures, updated_at "
                "FROM revenue_source_status ORDER BY status_key ASC"
            ).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return CheckResult(
                "revenue",
                WARN,
                "revenue_source_status table does not exist yet — the collector has "
                "not completed a real (non-dry-run) pass since this check was added",
                {},
            )
        return CheckResult("revenue", FAIL, f"could not read revenue_source_status: {exc}", {})
    except sqlite3.Error as exc:
        return CheckResult("revenue", FAIL, f"could not read revenue_source_status: {exc}", {})

    if not rows:
        return CheckResult(
            "revenue", WARN, "revenue collector has never recorded a source status", {}
        )

    sources = [dict(row) for row in rows]
    detail: dict[str, Any] = {"threshold": _REVENUE_FAIL_THRESHOLD, "sources": sources}
    red = sorted(
        row["status_key"] for row in rows if row["consecutive_failures"] >= _REVENUE_FAIL_THRESHOLD
    )
    warn = sorted(
        row["status_key"]
        for row in rows
        if 0 < row["consecutive_failures"] < _REVENUE_FAIL_THRESHOLD
    )
    if red:
        return CheckResult(
            "revenue",
            FAIL,
            f"{len(red)} revenue source(s) failing {_REVENUE_FAIL_THRESHOLD}+ consecutive "
            f"days: {', '.join(red)}",
            detail,
        )
    if warn:
        return CheckResult(
            "revenue",
            WARN,
            f"{len(warn)} revenue source(s) failed today (below the "
            f"{_REVENUE_FAIL_THRESHOLD}-day threshold): {', '.join(warn)}",
            detail,
        )
    return CheckResult("revenue", OK, f"{len(rows)} revenue source(s) all healthy", detail)


def _dashboard_page_routes() -> list[str]:
    """Top-level browsable routes under ``dashboard/src/app`` (dir has ``page.tsx``).

    Only top-level segments are enumerated — deliberately shallow, matching
    ``tests/feature_health/tier3/test_production_surface.py``'s own sweep list —
    so this stays a cheap sentinel-cadence check, not a full route crawl.
    Dynamic (``[slug]``) and route-group (``(group)``) directories are excluded:
    neither is a fixed, directly-GET-able path.
    """
    if not DASHBOARD_APP_DIR.is_dir():
        return []
    routes: list[str] = []
    for entry in sorted(DASHBOARD_APP_DIR.iterdir()):
        if not entry.is_dir():
            continue
        name = entry.name
        if name in _DASHBOARD_NON_PAGE_ENTRIES:
            continue
        if name.startswith("[") or name.startswith("(") or name.startswith("_"):
            continue
        if (entry / "page.tsx").exists():
            routes.append(f"/{name}")
    return routes


def check_dashboard_build_freshness() -> CheckResult:
    """The served :3003 Next.js build must not predate ``dashboard/src/app``'s pages.

    2026-08-11: ``/team`` and ``/control-plane`` 404'd live for an unknown
    stretch because the dashboard source grew those pages but the running
    process was never rebuilt/restarted — nothing noticed until a manual tier3
    sweep did. This check is the tripwire: for every top-level page directory
    that exists in the SOURCE tree, GET the equivalent live route and fail if
    it 404s. An unreachable dashboard (down for a deploy, box asleep) is a
    WARN, not a FAIL — that is a different, already-visible failure mode; this
    check exists specifically to catch a build silently lagging its own source
    while the process itself looks perfectly healthy.
    """
    routes = _dashboard_page_routes()
    detail: dict[str, Any] = {"app_dir": str(DASHBOARD_APP_DIR), "routes_checked": routes}
    if not routes:
        return CheckResult(
            "dashboard_build_freshness",
            WARN,
            f"no page routes found under {DASHBOARD_APP_DIR} — nothing to check",
            detail,
        )

    port = os.environ.get("OMNIAGENTOS_DASHBOARD_PORT", "3003")
    stale: list[dict[str, Any]] = []
    unreachable: str | None = None
    for route in routes:
        url = f"http://127.0.0.1:{port}{route}"
        try:
            with urllib.request.urlopen(url, timeout=DASHBOARD_TIMEOUT_SECONDS) as response:  # noqa: S310
                code = response.getcode()
        except urllib.error.HTTPError as exc:
            code = exc.code
        except (urllib.error.URLError, OSError, ValueError) as exc:
            unreachable = f"{url} unreachable ({type(exc).__name__}: {exc})"
            break
        if code == 404:
            stale.append({"route": route, "http_status": code})

    detail["stale_routes"] = stale
    detail["unreachable"] = unreachable

    if unreachable is not None:
        return CheckResult(
            "dashboard_build_freshness",
            WARN,
            f"dashboard unreachable on :{port} — {unreachable}",
            detail,
        )
    if stale:
        names = ", ".join(item["route"] for item in stale)
        return CheckResult(
            "dashboard_build_freshness",
            FAIL,
            f"served build on :{port} is stale — 404 for page(s) present in source: {names}",
            detail,
        )
    return CheckResult(
        "dashboard_build_freshness",
        OK,
        f"served build on :{port} matches source — {len(routes)} page route(s) all resolve",
        detail,
    )


GATE_SETTLEMENT_STREAK = 3


def check_gate_settlement() -> CheckResult:
    """Persistent evidence-free routine settlement must be VISIBLE, never punitive.

    Settlement itself deliberately treats absence of gate evidence as neutral
    (NULL/NULL) — punishing absence is the non-result-as-unfavourable defect
    class that auto-paused this repo's routines four times on 2026-07-31. The
    flip side is that absence then produces no alarm at all: 254/254 vacuous
    settlements went unnoticed for days, and a poisoned/dirty gate workspace
    would silence the acceptance floor forever. This check is the alarm half:

    * No usable ``<repo>-gate`` workspace → WARN naming the bootstrap step
      (expected state until ``scripts/gate-workspace.sh`` has been run).
    * Usable workspace but an ACTIVE routine's last ``GATE_SETTLEMENT_STREAK``
      settled runs are all evidence-free → FAIL: evidence was expected and
      never produced (probe-vs-settlement drift, launch env not reloaded, or a
      workspace that only LOOKS usable).
    """
    workspace = REPO_ROOT.parent / f"{REPO_ROOT.name}-gate"
    probe_reason: str | None = None
    if not workspace.is_dir():
        probe_reason = "workspace does not exist (run scripts/gate-workspace.sh)"
    else:
        rev_rc, _ = _run(["git", "-C", str(workspace), "rev-parse", "HEAD"])
        stat_rc, stat_out = _run(
            ["git", "-C", str(workspace), "status", "--porcelain=v1", "--untracked-files=all"]
        )
        if rev_rc != 0:
            probe_reason = "not a usable git checkout"
        elif stat_rc != 0:
            probe_reason = "git status failed (corrupt checkout?)"
        elif stat_out.strip():
            probe_reason = "workspace is dirty — probe refuses it, settlements stay evidence-free"

    detail: dict[str, Any] = {
        "workspace": str(workspace),
        "workspace_usable": probe_reason is None,
        "probe_reason": probe_reason,
        "streak_threshold": GATE_SETTLEMENT_STREAK,
    }

    starved: list[str] = []
    judged: list[str] = []
    try:
        with _readonly_connect(_db_path()) as conn:
            routines = conn.execute(
                "SELECT id, name FROM routines WHERE status = 'active'"
            ).fetchall()
            for routine in routines:
                runs = conn.execute(
                    "SELECT gate_passed, accepted FROM routine_runs"
                    " WHERE routine_id = ? AND finished_at IS NOT NULL"
                    " ORDER BY created_at DESC LIMIT ?",
                    (routine["id"], GATE_SETTLEMENT_STREAK),
                ).fetchall()
                if len(runs) < GATE_SETTLEMENT_STREAK:
                    continue
                if all(r["gate_passed"] is None and r["accepted"] is None for r in runs):
                    starved.append(routine["name"])
                else:
                    judged.append(routine["name"])
    except sqlite3.Error as exc:
        return CheckResult("gate_settlement", FAIL, f"could not read settlements: {exc}", detail)

    detail["starved"] = starved
    detail["recently_judged"] = judged

    if probe_reason is not None:
        return CheckResult(
            "gate_settlement",
            WARN,
            f"no usable gate workspace — {probe_reason}; "
            f"{len(starved)} active routine(s) settling without evidence",
            detail,
        )
    if starved:
        return CheckResult(
            "gate_settlement",
            FAIL,
            f"gate workspace is usable but {len(starved)} active routine(s) produced "
            f"{GATE_SETTLEMENT_STREAK}+ consecutive evidence-free settlements "
            f"({', '.join(starved)}) — evidence expected, never produced",
            detail,
        )
    return CheckResult(
        "gate_settlement",
        OK,
        f"gate workspace usable; {len(judged)} active routine(s) recently judged on real evidence",
        detail,
    )


def _gate_workspace_path() -> Path:
    """Configured gate workspace, matching ``scripts/gate-workspace.sh``."""
    configured = os.environ.get("OMNIAGENTOS_GATE_WORKSPACE")
    return Path(configured) if configured else Path(f"{REPO_ROOT}-gate")


def check_gate_workspace_staleness() -> CheckResult:
    """Expose a clean but stale gate pin before it makes gate evidence misleading.

    The gate runner correctly refuses a dirty workspace.  A detached workspace
    that is clean but behind ``main`` is more subtle: it can still produce
    perfectly valid evidence for the wrong source revision.  This check is
    read-only and treats every git failure as an operator-visible unknown
    state, never as a crash.
    """
    workspace = _gate_workspace_path()
    detail: dict[str, Any] = {"workspace": str(workspace)}
    if not workspace.is_dir():
        return CheckResult(
            "gate_workspace_staleness",
            WARN,
            "gate workspace not yet pinned",
            detail,
        )

    workspace_rc, workspace_out = _run(["git", "-C", str(workspace), "rev-parse", "HEAD"])
    workspace_sha = workspace_out.strip()
    if workspace_rc != 0 or not workspace_sha:
        detail["error"] = "could not resolve workspace HEAD"
        return CheckResult(
            "gate_workspace_staleness",
            WARN,
            "gate workspace is not a usable git checkout",
            detail,
        )
    detail["workspace_sha"] = workspace_sha

    status_rc, status_out = _run(
        ["git", "-C", str(workspace), "status", "--porcelain=v1", "--untracked-files=all"]
    )
    if status_rc != 0:
        detail["error"] = "git status failed"
        return CheckResult(
            "gate_workspace_staleness",
            WARN,
            "could not determine whether gate workspace is dirty",
            detail,
        )
    if status_out.strip():
        return CheckResult(
            "gate_workspace_staleness",
            FAIL,
            "gate workspace is dirty (has uncommitted changes)",
            detail,
        )

    main_rc, main_out = _run(
        ["git", "-C", str(workspace), "rev-parse", "--verify", "main^{commit}"]
    )
    main_sha = main_out.strip()
    if main_rc != 0 or not main_sha:
        detail["error"] = "could not resolve main"
        return CheckResult(
            "gate_workspace_staleness",
            WARN,
            "could not determine gate workspace staleness because main is not reachable",
            detail,
        )
    detail["main_sha"] = main_sha

    if workspace_sha == main_sha:
        return CheckResult(
            "gate_workspace_staleness", OK, "gate workspace is current with main", detail
        )

    ancestor_rc, _ = _run(
        ["git", "-C", str(workspace), "merge-base", "--is-ancestor", workspace_sha, main_sha]
    )
    if ancestor_rc == 0:
        count_rc, count_out = _run(
            ["git", "-C", str(workspace), "rev-list", "--count", f"{workspace_sha}..{main_sha}"]
        )
        try:
            commits_behind = int(count_out.strip()) if count_rc == 0 else None
        except ValueError:
            commits_behind = None
        if commits_behind is not None:
            detail["commits_behind"] = commits_behind
            evidence = (
                f"gate workspace is behind main by {commits_behind} commits; "
                "rerun `scripts/gate-workspace.sh main` to advance"
            )
        else:
            detail["error"] = "could not count commits behind main"
            evidence = (
                "gate workspace is behind main; rerun `scripts/gate-workspace.sh main` to advance"
            )
        return CheckResult("gate_workspace_staleness", WARN, evidence, detail)

    if ancestor_rc == 1:
        detail["error"] = "workspace HEAD is not an ancestor of main"
        return CheckResult(
            "gate_workspace_staleness",
            WARN,
            "gate workspace does not match main history; rerun `scripts/gate-workspace.sh main` to advance",
            detail,
        )

    detail["error"] = "could not compare workspace HEAD with main"
    return CheckResult(
        "gate_workspace_staleness",
        WARN,
        "could not determine gate workspace staleness",
        detail,
    )


SLACK_SOCKET_SOURCE = "slack-socket"
SLACK_POLLER_SOURCE = "slack"
#: 5x the client's 60s heartbeat. Below this it would alarm on one slow write.
SLACK_HEARTBEAT_FAIL_SECONDS = 300.0
#: 3x the sweep's 300s cadence.
SLACK_SWEEP_FAIL_SECONDS = 900.0
#: How recently a reconciliation catch still counts as "the socket is missing things".
SLACK_RECONCILE_WINDOW_SECONDS = 3600.0
#: Slack cycles a Socket Mode connection roughly hourly (``refresh_requested``),
#: so 1-2 disconnects inside one 1800s sentinel interval is HOUSEKEEPING.
SLACK_DISCONNECT_WARN_DELTA = 2
#: Four or more in one interval is a flap: every gap loses thread replies the
#: sweep can never backfill, and the row otherwise reads perfectly healthy.
SLACK_DISCONNECT_FAIL_DELTA = 4
#: Monotonic counters this check watermarks between runs. The sweep runs every
#: 300s and this sentinel every 1800s, so ANY last-value field is overwritten ~5
#: times out of 6 before it is ever read. A watermark cannot miss an event
#: between two observations; a last-value field structurally can.
SLACK_WATERMARK_PATH = STATE_DIR / "slack-watermark.json"
#: Mirrors ``omniagentos.comms.sockets.slack.SLOW_WRITE_MS`` — this script is
#: standalone (stdlib + sqlite3 only) and never imports the package, so the
#: number is repeated rather than shared. Used only in the message text.
SLACK_SLOW_WRITE_MS = 1000
_SLACK_WATERMARKED = ("reconciled_total", "disconnects", "store_failures", "store_slow_writes")


def _slack_watermark(path: Path) -> dict[str, int]:
    try:
        data = _read_json(path)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    marks: dict[str, int] = {}
    for key in _SLACK_WATERMARKED:
        try:
            marks[key] = int(data.get(key))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
    return marks


def check_slack_socket(*, watermark_path: Path = SLACK_WATERMARK_PATH) -> CheckResult:
    """The hybrid Slack ingestion — BOTH halves, because either alone is a defect.

    Socket Mode does not replay events missed while disconnected, so the socket
    is only safe behind the reconciliation sweep. That makes the SWEEP's death
    worse than the socket's: a dead socket with a live sweep degrades to 5-minute
    latency, while a dead sweep with a live socket silently removes the only
    mechanism that repairs a gap. Both are FAIL, and the sweep's message says so.

    A reconciliation catch is not a heuristic. Dedupe is a hard DB constraint
    (``UNIQUE(source, external_id)``), so in steady state behind a healthy socket
    every sweep insert is an IGNORE and ``created`` is 0. A non-zero count is a
    CONSEQUENCE: the socket missed those messages.

    WHY THIS READS A WATERMARK AND NOT THE LAST SWEEP'S COUNT
    ``reconciled_last_count`` is rewritten by EVERY sweep, including clean ones.
    The sweep runs at 300s and this sentinel at 1800s, so a real catch is visible
    for one sixth of one observation period — five catches in six would be erased
    before anyone looked, and the alarm most worth having would be the one that
    almost never fires. Monotonic ``reconciled_total`` is compared against a
    watermark persisted between runs instead, which cannot miss an event that
    happened entirely between two observations. The same argument applies to
    ``disconnects`` (a socket flapping every 20s inside the supervisor's 30s
    grace never sets status=error and would otherwise read perfectly healthy),
    to ``store_failures``, and to ``store_slow_writes``.

    Deliberately NOT claimed here: a quiet reconciliation counter does not prove
    the socket is alive. ``conversations.history`` never returns thread replies,
    so a reply lost during an outage can never be caught by the sweep. The
    heartbeat, not the counter, is the liveness signal.
    """
    detail: dict[str, Any] = {"db": str(_db_path())}
    rows: dict[str, sqlite3.Row] = {}
    try:
        with _readonly_connect(_db_path()) as conn:
            for row in conn.execute(
                "SELECT name, kind, status, config_json, last_poll_at, last_error"
                " FROM comms_sources WHERE name IN (?, ?)",
                (SLACK_SOCKET_SOURCE, SLACK_POLLER_SOURCE),
            ).fetchall():
                rows[str(row["name"])] = row
    except sqlite3.Error as exc:
        return CheckResult("slack_socket", FAIL, f"could not read comms_sources: {exc}", detail)

    socket_row = rows.get(SLACK_SOCKET_SOURCE)
    poller_row = rows.get(SLACK_POLLER_SOURCE)
    if socket_row is None and poller_row is None:
        # Neither half exists: the feature is not deployed. Not a failure of a
        # thing that was never installed — but never silence, either.
        return CheckResult(
            "slack_socket",
            WARN,
            "slack ingestion not deployed (no slack/slack-socket comms_sources rows) — "
            "run scripts/scheduler/install-comms-slack.sh",
            detail,
        )

    now = datetime.now(UTC)
    problems: list[str] = []
    verdict = OK

    def _age_seconds(row: sqlite3.Row | None) -> float | None:
        stamp = _parse_iso(str((row["last_poll_at"] if row is not None else "") or ""))
        return None if stamp is None else (now - stamp).total_seconds()

    # -- layer 1: the socket's own heartbeat --------------------------------
    socket_age = _age_seconds(socket_row)
    detail["socket_status"] = str(socket_row["status"]) if socket_row is not None else None
    detail["socket_heartbeat_age_s"] = socket_age
    if socket_row is None:
        verdict = FAIL
        problems.append("slack-socket row missing — the push client has never run")
    elif str(socket_row["status"]) == "pending_setup":
        verdict = verdict if _RANK[verdict] >= _RANK[WARN] else WARN
        problems.append(f"slack-socket credentials absent ({socket_row['last_error']})")
    elif str(socket_row["status"]) == "error":
        verdict = FAIL
        problems.append(f"slack-socket status=error: {socket_row['last_error']}")
    if socket_age is None and socket_row is not None:
        verdict = FAIL
        problems.append("slack-socket has no heartbeat timestamp")
    elif socket_age is not None and socket_age > SLACK_HEARTBEAT_FAIL_SECONDS:
        verdict = FAIL
        problems.append(
            f"slack-socket heartbeat is {_age(socket_age)} old "
            f"(>{SLACK_HEARTBEAT_FAIL_SECONDS:.0f}s) — live process, dead connection thread"
        )

    # -- the sweep, which is what makes a dead socket survivable -------------
    sweep_age = _age_seconds(poller_row)
    detail["sweep_age_s"] = sweep_age
    if poller_row is None or sweep_age is None:
        verdict = FAIL
        problems.append(
            "the reconciliation sweep has never run — a socket without it loses "
            "every message it misses, permanently"
        )
    elif sweep_age > SLACK_SWEEP_FAIL_SECONDS:
        verdict = FAIL
        problems.append(
            f"the reconciliation SWEEP is {_age(sweep_age)} stale "
            f"(>{SLACK_SWEEP_FAIL_SECONDS:.0f}s) — worse than a dead socket, because it "
            "is what makes a dead socket survivable"
        )

    # -- the sweep's own coverage -------------------------------------------
    config: dict[str, Any] = {}
    if poller_row is not None:
        try:
            parsed = json.loads(str(poller_row["config_json"] or "{}"))
            config = parsed if isinstance(parsed, dict) else {}
        except (TypeError, ValueError):
            config = {}
    socket_config: dict[str, Any] = {}
    if socket_row is not None:
        try:
            parsed_socket = json.loads(str(socket_row["config_json"] or "{}"))
            socket_config = parsed_socket if isinstance(parsed_socket, dict) else {}
        except (TypeError, ValueError):
            socket_config = {}

    detail["sweep_status"] = str(poller_row["status"]) if poller_row is not None else None
    if poller_row is not None and str(poller_row["status"]) == "error":
        verdict = FAIL
        problems.append(f"the reconciliation SWEEP is failing: {poller_row['last_error']}")

    if poller_row is not None and "member_channels" in config:
        member_channels = int(config.get("member_channels") or 0)
        detail["member_channels"] = member_channels
        detail["channels_swept"] = int(config.get("channels_swept") or 0)
        if member_channels == 0:
            verdict = FAIL
            problems.append(
                "the reconciliation sweep covers ZERO channels (the bot is a member of no "
                "public channel) — the socket's safety net is vacuous"
            )
    channel_error_count = int(config.get("channel_error_count") or 0)
    detail["channel_error_count"] = channel_error_count
    if channel_error_count:
        verdict = verdict if _RANK[verdict] >= _RANK[WARN] else WARN
        errors = config.get("channel_errors")
        sample = "; ".join(str(item) for item in errors[:3]) if isinstance(errors, list) else ""
        problems.append(
            f"the sweep could not read {channel_error_count} channel(s) — those are "
            f"unreconciled: {sample}"
        )

    # -- monotonic counters, compared against the previous run ---------------
    current = {
        "reconciled_total": int(config.get("reconciled_total") or 0),
        "disconnects": int(socket_config.get("disconnects") or 0),
        "store_failures": int(socket_config.get("store_failures") or 0),
        "store_slow_writes": int(socket_config.get("store_slow_writes") or 0),
    }
    previous = _slack_watermark(watermark_path)
    detail.update(current)
    detail["reconciled_last_count"] = int(config.get("reconciled_last_count") or 0)
    detail["reconciled_last_at"] = config.get("reconciled_last_at") or ""
    detail["last_disconnect_reason"] = str(socket_config.get("last_disconnect_reason") or "")
    detail["store_latency_ms_max"] = int(socket_config.get("store_latency_ms_max") or 0)
    detail["watermark"] = dict(previous)

    def _delta(key: str) -> int:
        # A first run has no baseline: record it, never alarm on history that
        # accumulated before this check existed.
        return 0 if key not in previous else max(0, current[key] - previous[key])

    caught_at = _parse_iso(str(config.get("reconciled_last_at") or ""))
    reconciled_delta = _delta("reconciled_total")
    if reconciled_delta:
        verdict = FAIL
        problems.append(
            f"reconciliation caught {reconciled_delta} message(s) the socket missed since the "
            f"last check (total {current['reconciled_total']}) — the socket is dropping traffic"
        )
    elif (
        caught_at is not None
        and (now - caught_at).total_seconds() <= SLACK_RECONCILE_WINDOW_SECONDS
        and int(config.get("reconciled_total") or 0) > 0
    ):
        # Belt and braces: the watermark alarms once, this keeps the fact
        # visible for the window even after the watermark has caught up.
        verdict = FAIL
        problems.append(
            "reconciliation caught messages the socket missed "
            f"{_age((now - caught_at).total_seconds())} ago"
        )

    disconnect_delta = _delta("disconnects")
    if disconnect_delta >= SLACK_DISCONNECT_FAIL_DELTA:
        verdict = FAIL
        problems.append(
            f"the socket disconnected {disconnect_delta}x since the last check — it is FLAPPING, "
            f"and every gap loses thread replies the sweep cannot backfill "
            f"(last reason: {detail['last_disconnect_reason'] or 'unknown'})"
        )
    elif disconnect_delta >= SLACK_DISCONNECT_WARN_DELTA:
        verdict = verdict if _RANK[verdict] >= _RANK[WARN] else WARN
        problems.append(
            f"the socket disconnected {disconnect_delta}x since the last check "
            f"(last reason: {detail['last_disconnect_reason'] or 'unknown'})"
        )

    store_failure_delta = _delta("store_failures")
    if store_failure_delta:
        verdict = FAIL
        problems.append(
            f"{store_failure_delta} socket message(s) failed to store since the last check — "
            "those are lost unless the sweep re-reads them, and it never re-reads thread replies"
        )
    slow_write_delta = _delta("store_slow_writes")
    if slow_write_delta:
        verdict = verdict if _RANK[verdict] >= _RANK[WARN] else WARN
        problems.append(
            f"{slow_write_delta} store write(s) took over {SLACK_SLOW_WRITE_MS}ms since the last "
            "check "
            f"(max {detail['store_latency_ms_max']}ms) — ack latency is at risk under contention"
        )

    try:
        atomic_write(
            watermark_path,
            json.dumps({**current, "ts": _iso(now)}, indent=2, sort_keys=True) + "\n",
        )
    except OSError as exc:
        detail["watermark_write_error"] = str(exc)
        verdict = verdict if _RANK[verdict] >= _RANK[WARN] else WARN
        problems.append(f"could not persist the slack watermark ({exc}) — deltas are unreliable")

    if problems:
        return CheckResult("slack_socket", verdict, "; ".join(problems), detail)
    return CheckResult(
        "slack_socket",
        OK,
        f"slack socket heartbeat {_age(socket_age or 0.0)} old, sweep {_age(sweep_age or 0.0)} old "
        f"over {detail.get('member_channels', '?')} member channel(s), nothing reconciled",
        detail,
    )


CHECKS: tuple[tuple[str, Any], ...] = (
    ("api", check_api),
    ("runner", check_runner),
    ("scheduler", check_scheduler),
    ("gate_settlement", check_gate_settlement),
    ("gate_workspace_staleness", check_gate_workspace_staleness),
    ("slack_socket", check_slack_socket),
    ("claude_pool", check_claude_pool),
    ("memory", check_memory),
    ("reflection", check_reflection),
    ("providers", check_providers),
    ("launchd", check_launchd),
    ("mechanism_registry", check_mechanism_registry),
    ("disk", check_disk),
    ("dashboard_build_freshness", check_dashboard_build_freshness),
    ("revenue", check_revenue),
)


def _apply_kickstart(launchd_check: CheckResult) -> None:
    """Heal failed allowlisted launchd jobs and record every attempt.

    ``kickstart`` is only meaningful for a loaded job.  An installed plist
    absent from ``launchctl list`` must instead be bootstrapped into this GUI
    domain.  Healing is deliberately best-effort: a failed command is recorded
    in the check detail but never prevents the sentinel from publishing its
    snapshot and alert.

    ``-k`` is reserved for a job that is RUNNING.  ``-k`` means "kill the
    current instance first"; against a KeepAlive job that is DOWN it races
    launchd's own respawn and SIGTERMs the instance launchd just started, so the
    service never gets past startup.  On 2026-08-05 that is what the sentinel
    did to ``com.omniagentos.api`` on every pass: the heal recorded a 10s
    timeout because ``kickstart -k`` blocks on the kill-and-respawn it provoked,
    and the resulting ``exit=-15`` fed straight back into ``nonzero_last_exit``
    on the next pass.  A down job only ever needs to be STARTED.
    """
    if launchd_check.status != FAIL:
        return  # No failures to remedy

    detail = launchd_check.detail or {}
    launchd_check.detail = detail
    heal_attempts = detail.setdefault("heal_attempts", [])
    not_loaded = detail.get("installed_not_loaded", [])
    bad_exit = detail.get("nonzero_last_exit", {})
    bad_exit_running = set(detail.get("nonzero_last_exit_running", ()))
    loaded_ours = set(detail.get("loaded", []))

    # This cannot occur in a single launchctl snapshot.  Treat it as an
    # assertion failure rather than silently selecting one verb: it means the
    # check detail changed underneath us (or a caller supplied inconsistent
    # state), and the operator needs to see that fact.
    state_conflicts = sorted(set(not_loaded) & loaded_ours)
    if state_conflicts:
        detail["state_assertion"] = (
            "FAIL: installed_not_loaded also present in loaded jobs: " + ", ".join(state_conflicts)
        )
        detail["state_conflicts"] = state_conflicts
        launchd_check.status = FAIL
        if "state assertion failed" not in launchd_check.evidence:
            launchd_check.evidence += "; state assertion failed: " + ", ".join(state_conflicts)

    def _cap(value: str | None) -> str:
        """Keep audit records useful without allowing command output to bloat snapshots."""
        text = value or ""
        return text if len(text) <= 500 else text[:497] + "..."

    def _is_transient(exc: BaseException) -> bool:
        if not isinstance(exc, OSError):
            return False
        if exc.errno in (errno.EAGAIN, errno.EBUSY, errno.EINTR):
            return True
        message = str(exc).lower()
        return "temporarily unavailable" in message or "resource busy" in message

    uid = os.getuid()
    jobs_to_heal: list[tuple[str, str]] = []

    # An installed-but-unloaded job needs bootstrap; kickstart is a silent
    # no-op in that state.  Skip assertion-conflicted jobs above until the next
    # check has a coherent snapshot.
    for label in not_loaded:
        if label in KICKSTART_ALLOWLIST and label not in state_conflicts:
            jobs_to_heal.append((label, "bootstrap"))

    # A job with a nonzero last exit is already loaded, so kickstart applies.
    # Which FORM of kickstart depends on whether it is running: a live instance
    # that keeps failing has to be killed first (``-k``), a job that is down
    # must only be started.  ``-k`` on a down KeepAlive job kills the instance
    # launchd is concurrently respawning.  A state conflict never receives two
    # contradictory verbs in the same pass.
    for label in bad_exit.keys():
        if label in KICKSTART_ALLOWLIST and label not in state_conflicts:
            running = label in bad_exit_running
            jobs_to_heal.append((label, "kickstart-kill" if running else "kickstart"))

    if not jobs_to_heal:
        return  # Nothing allowed to restart

    # A label should normally occur in only one input set.  Preserve bootstrap
    # priority if a malformed detail happens to place it in both.
    commands: dict[str, str] = {}
    for label, verb in jobs_to_heal:
        commands.setdefault(label, verb)
    for label in sorted(commands):
        verb = commands[label]
        plist = LAUNCH_AGENTS_DIR / f"{label}.plist"
        if verb == "bootstrap":
            argv = ["launchctl", "bootstrap", f"gui/{uid}", str(plist)]
        elif verb == "kickstart-kill":
            argv = ["launchctl", "kickstart", "-k", f"gui/{uid}/{label}"]
        else:
            argv = ["launchctl", "kickstart", f"gui/{uid}/{label}"]
        for attempt in (1, 2):
            try:
                result = subprocess.run(
                    argv,
                    capture_output=True,
                    text=True,
                    errors="replace",
                    timeout=10,
                )
            except Exception as exc:  # noqa: BLE001 - healing must not crash the sentinel
                heal_attempts.append(
                    {
                        "label": label,
                        "status": "error",
                        "verb": verb,
                        "rc": None,
                        "stdout": "",
                        "stderr": "",
                        "error": str(exc),
                        "attempt": attempt,
                    }
                )
                if attempt == 1 and _is_transient(exc):
                    time.sleep(0.1)
                    continue
                break

            entry: dict[str, Any] = {
                "label": label,
                "status": "ok" if result.returncode == 0 else "error",
                "verb": verb,
                "rc": result.returncode,
                "stdout": _cap(result.stdout),
                "stderr": _cap(result.stderr),
                "attempt": attempt,
            }
            if result.returncode != 0:
                # A concurrent launchctl operation can win this race.  A fresh
                # list distinguishes that benign outcome from a failed heal.
                loaded, _error = _launchctl_table()
                if label in loaded:
                    entry["status"] = "already_running"
            heal_attempts.append(entry)
            break


def _held_launchd_labels(*, holds_path: Path = HOLDS_PATH) -> set[str] | None:
    """Return labels explicitly held by active entries in ``HOLDS.yaml``.

    HOLDS.yaml currently has no launchd-specific field, so it intentionally
    yields no holds.  The check point is ready for a future active hold with a
    ``launchd_label`` or ``launchd_labels`` field; unrelated path scopes must
    never implicitly block (or authorize) a daemon operation.

    Returns ``None`` when HOLDS.yaml exists but cannot be parsed: an
    unreadable holds file must neither crash the sentinel nor read as "no
    holds" — the caller treats ``None`` as hold-everything.
    """
    try:
        import yaml  # noqa: PLC0415 - optional at this standalone-script boundary

        payload = yaml.safe_load(holds_path.read_text(encoding="utf-8"))
    except (ImportError, OSError, ValueError):
        return set()
    except Exception:  # noqa: BLE001 - yaml.YAMLError is not a ValueError; a malformed
        # HOLDS.yaml must not take down the sentinel run, and it must fail CLOSED
        # for consent, not open.
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("holds"), list):
        return set()

    labels: set[str] = set()
    for hold in payload["holds"]:
        if not isinstance(hold, dict) or hold.get("status") != "ACTIVE":
            continue
        single = hold.get("launchd_label")
        if isinstance(single, str) and single:
            labels.add(single)
        many = hold.get("launchd_labels")
        if isinstance(many, list):
            labels.update(str(label) for label in many if isinstance(label, str) and label)
    return labels


def _remedy_command(label: str, failure_class: str) -> str:
    """The exact operator command for one launchd detector state."""
    installed_plist = LAUNCH_AGENTS_DIR / f"{label}.plist"
    if failure_class == "installed_not_loaded":
        return f"launchctl bootstrap gui/{os.getuid()} {shlex.quote(str(installed_plist))}"
    rendered_plist = RENDERED_LAUNCHD_DIR / f"{label}.plist"
    return (
        f"cp {shlex.quote(str(rendered_plist))} {shlex.quote(str(installed_plist))} && "
        f"launchctl bootstrap gui/{os.getuid()} {shlex.quote(str(installed_plist))}"
    )


def _load_remedy_ledger(path: Path) -> dict[str, dict[str, Any]]:
    """Load the small signature ledger; a corrupt file starts a fresh ledger."""
    try:
        payload = _read_json(path)
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {
        signature: entry
        for signature, entry in payload.items()
        if isinstance(signature, str) and isinstance(entry, dict)
    }


def _save_remedy_ledger(path: Path, ledger: dict[str, dict[str, Any]]) -> None:
    atomic_write(path, json.dumps(ledger, indent=2, sort_keys=True) + "\n")


def _record_launchd_remedies(
    launchd_check: CheckResult,
    *,
    now: datetime | None = None,
    ledger_path: Path | None = None,
    holds_path: Path = HOLDS_PATH,
) -> None:
    """File launchd failures and optionally bootstrap unloaded installed plists.

    This is deliberately separate from :func:`_apply_kickstart`: that existing
    allowlisted failure healer remains unconditional, while this ledger is
    report-only unless ``OMNIAGENTOS_SENTINEL_AUTOREMEDY=1`` is explicitly set.
    """
    detail = launchd_check.detail or {}
    launchd_check.detail = detail
    now = now or _now()
    today = now.date().isoformat()
    ledger_path = ledger_path or REMEDY_LEDGER_PATH
    ledger = _load_remedy_ledger(ledger_path)
    held_labels = _held_launchd_labels(holds_path=holds_path)
    holds_unreadable = held_labels is None
    if holds_unreadable:
        held_labels = set()
    # Unreadable holds fail CLOSED: remedies are still FILED, but nothing may
    # execute while the hold list cannot be trusted.
    autoremedy = (
        os.environ.get("OMNIAGENTOS_SENTINEL_AUTOREMEDY") == "1" and not holds_unreadable
    )
    open_remedies: list[dict[str, Any]] = []
    autoremedy_attempts: list[dict[str, Any]] = []

    failures = [
        (label, "installed_not_loaded")
        for label in detail.get("installed_not_loaded", [])
        if isinstance(label, str)
    ] + [
        (label, "rendered_not_installed")
        for label in detail.get("rendered_not_installed", [])
        if isinstance(label, str)
    ]
    for label, failure_class in sorted(failures):
        signature = f"{label}:{failure_class}"
        entry = ledger.get(signature, {})
        first_seen = str(entry.get("first_seen") or _iso(now))
        first_seen_at = _parse_iso(first_seen) or now
        entry.update(
            {
                "label": label,
                "class": failure_class,
                "first_seen": first_seen,
                "last_seen": _iso(now),
                "days_recurring": max(1, (now.date() - first_seen_at.date()).days + 1),
                "last_filed_date": entry.get("last_filed_date"),
                "last_autoremedy_date": entry.get("last_autoremedy_date"),
                "last_autoremedy_rc": entry.get("last_autoremedy_rc"),
            }
        )
        filed_today = entry["last_filed_date"] != today
        if filed_today:
            entry["last_filed_date"] = today
        ledger[signature] = entry
        command = _remedy_command(label, failure_class)
        open_remedies.append(
            {
                "signature": signature,
                "label": label,
                "class": failure_class,
                "command": command,
                "first_seen": first_seen,
                "days_recurring": entry["days_recurring"],
                "filed_today": filed_today,
            }
        )

        # Rendered plists are report-only forever.  Existing kickstart healing
        # above is intentionally unrelated to this opt-in remediation path.
        if (
            not autoremedy
            or failure_class != "installed_not_loaded"
            or label in held_labels
            or entry["last_autoremedy_date"] == today
        ):
            continue

        argv = ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(LAUNCH_AGENTS_DIR / f"{label}.plist")]
        try:
            result = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=SUBPROCESS_TIMEOUT_SECONDS,
                check=False,
            )
            rc = result.returncode
            stderr = result.stderr or ""
        except (OSError, subprocess.SubprocessError) as exc:
            rc = 127
            stderr = f"{type(exc).__name__}: {exc}"
        entry["last_autoremedy_date"] = today
        entry["last_autoremedy_rc"] = rc
        autoremedy_attempts.append(
            {"signature": signature, "label": label, "rc": rc, "stderr": stderr}
        )

    detail["open_remedies"] = open_remedies
    detail["autoremedy_attempts"] = autoremedy_attempts
    try:
        _save_remedy_ledger(ledger_path, ledger)
    except OSError as exc:
        detail["remedy_ledger_error"] = str(exc)


def run_checks() -> list[CheckResult]:
    """Run every check; a check that raises becomes its own ``fail``, never the run's."""
    results: list[CheckResult] = []
    for name, fn in CHECKS:
        started = time.monotonic()
        try:
            result = fn()
        except Exception as exc:  # noqa: BLE001 - a sentinel must never crash
            result = CheckResult(
                name,
                FAIL,
                f"check crashed: {type(exc).__name__}: {exc}",
                {"exception": f"{type(exc).__name__}: {exc}"},
            )
        result.detail = dict(result.detail or {})
        result.detail["duration_seconds"] = round(time.monotonic() - started, 3)
        results.append(result)
    return results


# --------------------------------------------------------------------------- outputs


def build_snapshot(results: list[CheckResult], *, started_at: datetime, duration: float) -> dict:
    counts = {OK: 0, WARN: 0, FAIL: 0}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
    overall = _worst(*(result.status for result in results)) if results else OK
    return {
        "ts": _iso(started_at),
        "overall": overall,
        "counts": counts,
        "duration_seconds": round(duration, 3),
        "repo_root": str(REPO_ROOT),
        "db_path": str(_db_path()),
        "failing": [r.name for r in results if r.status == FAIL],
        "warning": [r.name for r in results if r.status == WARN],
        "checks": [result.as_dict() for result in results],
        "alerts": {"banner_fired": [], "notifications": [], "briefing": None},
    }


def append_ledger(snapshot: dict, *, ledger_dir: Path = STATE_DIR) -> Path:
    """One compact JSONL line per run, per month."""
    stamp = _parse_iso(str(snapshot.get("ts") or "")) or _now()
    path = ledger_dir / f"ledger-{stamp.strftime('%Y%m')}.jsonl"
    entry = {
        "ts": snapshot["ts"],
        "overall": snapshot["overall"],
        "counts": snapshot["counts"],
        "failing": snapshot["failing"],
        "warning": snapshot["warning"],
        "duration_seconds": snapshot["duration_seconds"],
        "evidence": {c["name"]: c["evidence"] for c in snapshot["checks"]},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, sort_keys=True) + "\n")
    return path


def log_lines(snapshot: dict, *, log_path: Path = LOG_PATH) -> None:
    """Append the human log — one header line plus one line per check."""
    icons = {OK: "OK  ", WARN: "WARN", FAIL: "FAIL"}
    lines = [
        f"{snapshot['ts']} health-sentinel overall={snapshot['overall']} "
        f"ok={snapshot['counts'].get(OK, 0)} warn={snapshot['counts'].get(WARN, 0)} "
        f"fail={snapshot['counts'].get(FAIL, 0)} in {snapshot['duration_seconds']}s"
    ]
    for check in snapshot["checks"]:
        lines.append(
            f"{snapshot['ts']}   [{icons.get(check['status'], '????')}] "
            f"{check['name']}: {check['evidence']}"
        )
        if check["name"] == "launchd":
            for attempt in check.get("detail", {}).get("autoremedy_attempts", []):
                lines.append(
                    f"{snapshot['ts']}   [AUTO] launchd {attempt['signature']}: "
                    f"rc={attempt['rc']} stderr={attempt['stderr']}"
                )
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def _load_alert_state(today: date) -> set[str]:
    """Issues already bannered TODAY. A new day starts from empty."""
    try:
        data = _read_json(ALERT_STATE_PATH)
    except (OSError, ValueError):
        return set()
    if not isinstance(data, dict) or data.get("date") != today.isoformat():
        return set()
    fired = data.get("fired")
    return set(map(str, fired)) if isinstance(fired, list) else set()


def _save_alert_state(today: date, fired: set[str]) -> None:
    atomic_write(
        ALERT_STATE_PATH,
        json.dumps({"date": today.isoformat(), "fired": sorted(fired)}, indent=2) + "\n",
    )


def _open_remedies_markdown(snapshot: dict) -> str:
    """Render the separately-filed launchd remedies for the daily alert."""
    launchd = next((check for check in snapshot["checks"] if check["name"] == "launchd"), None)
    if not launchd:
        return "- No launchd remedies are open."
    remedies = launchd.get("detail", {}).get("open_remedies", [])
    if not remedies:
        return "- No launchd remedies are open."
    rows = "\n".join(
        "| {label} | {failure_class} | `{command}` | {first_seen} | {days} |".format(
            label=remedy["label"],
            failure_class=remedy["class"],
            command=remedy["command"],
            first_seen=remedy["first_seen"],
            days=remedy["days_recurring"],
        )
        for remedy in remedies
    )
    attempts = launchd.get("detail", {}).get("autoremedy_attempts", [])
    outcomes = "\n".join(
        f"- AUTOREMEDY `{attempt['signature']}`: rc={attempt['rc']}; "
        f"stderr={attempt['stderr'] or '(none)'}"
        for attempt in attempts
    )
    return (
        "These remedies are awaiting operator action or `OMNIAGENTOS_SENTINEL_AUTOREMEDY=1`. "
        "Rendered-not-installed jobs remain operator-only.\n\n"
        "| Label | Class | Exact remedy command | First seen | Days recurring |\n"
        "| :--- | :--- | :--- | :--- | ---: |\n"
        f"{rows}"
        + (f"\n\n### AUTOREMEDY Outcomes\n{outcomes}" if outcomes else "")
    )


def write_alert_briefing(snapshot: dict, *, today: date, briefings_dir: Path | None = None) -> Path:
    """``vault/briefings/health-ALERT-YYYY-MM-DD.md``, in the reflection-ALERT table style.

    Rewritten on every failing run so the file always reflects the CURRENT state
    rather than the first failure of the day; the banner, not the file, is what
    is deduped.

    ``briefings_dir`` defaults to :func:`resolve_briefings_dir` at CALL time
    (not to a constant bound at import time), so the health alert lands in the
    same vault as the reflection briefing and its ALERT sibling.
    """
    briefings_dir = briefings_dir or resolve_briefings_dir()
    icons = {OK: "✅ PASS", WARN: "⚠️ WARN", FAIL: "❌ FAIL"}
    failing = [c for c in snapshot["checks"] if c["status"] == FAIL]
    warning = [c for c in snapshot["checks"] if c["status"] == WARN]

    rows = "\n".join(
        f"| {check['name']} | {icons.get(check['status'], '?')} | {check['evidence']} |"
        for check in snapshot["checks"]
    )
    remedies = {
        "api": "Start the API: `scripts/launch-omniagentos.sh api` (port $OMNIAGENTOS_API_PORT).",
        "runner": "Start the worker: `scripts/launch-omniagentos.sh runner`.",
        "scheduler": "The routines tick is not firing — check the runner and "
        "`var/log/routines.log`.",
        "claude_pool": "Re-login the dead profiles: "
        "`CLAUDE_CONFIG_DIR=~/.claude-account-N claude /login`, then reconcile "
        "`configs/accounts.yaml` with the `claude_accounts` table.",
        "memory": "Durable memory is missing/stale — verify `var/memories/` and the "
        "control-plane SQLite before running anything that writes memory.",
        "reflection": "The nightly reflection loop produced no briefing — see "
        "`var/log/reflection-nightly.log` and the latest `reflection-ALERT-*.md`.",
        "providers": "Run `scripts/provider-sentinel/provider-sentinel.sh` and inspect "
        "`var/provider-health.json`; re-login the failing CLI.",
        "launchd": "Install/load the missing jobs: render with the job's own "
        "`install*.sh`, then `launchctl bootstrap gui/$(id -u) "
        "~/Library/LaunchAgents/<label>.plist`.",
        "mechanism_registry": "Check the durable record at "
        "`/Users/youruser/Work/Ops/mechanism-registry/` and run "
        "`scripts/health-sentinel/mechanism_drift_detector.py` to inspect launchd drift.",
        "disk": "Free space on /System/Volumes/Data is below the 50 GB floor.",
    }
    steps = (
        "\n".join(
            f"- **{check['name']}** — {remedies.get(check['name'], 'Investigate.')}"
            for check in failing
        )
        or "- No failing checks."
    )

    body = f"""# AGENT HEALTH SENTINEL ALERT - {today.isoformat()}

The health sentinel found {len(failing)} failing check(s) and {len(warning)} warning(s)
at {snapshot["ts"]}. This file is rewritten on every failing pass, so it always shows
the CURRENT state; macOS banners are deduped per issue per day.

## Failure Summary
**{", ".join(check["name"] for check in failing) or "none"}**

## Check Results

| Check Name | Status | Evidence |
| :--- | :---: | :--- |
{rows}

## Recommended Next Steps
{steps}

## OPEN REMEDIES
{_open_remedies_markdown(snapshot)}

## Where To Look
- Snapshot: `var/health-sentinel/latest.json`
- History: `var/health-sentinel/ledger-{today.strftime("%Y%m")}.jsonl`
- Log: `var/log/health-sentinel.log`
- Run by hand: `scripts/health-sentinel/health-sentinel.sh`
"""
    path = briefings_dir / f"health-ALERT-{today.isoformat()}.md"
    atomic_write(path, body)
    return path


def emit_alerts(snapshot: dict, *, today: date, db_path: str | None = None) -> dict[str, Any]:
    """Persist a notification per failing check; banner only the day's first sighting.

    ``record_notification``'s own dedupe scopes on an UNREAD ``(ref_type, ref_id)``
    row, which we date-scope exactly as provider-sentinel does — but its push
    fires whether or not the row deduped, so at a 30-minute cadence the banner
    dedupe has to live here.
    """
    failing = [check for check in snapshot["checks"] if check["status"] == FAIL]
    outcome: dict[str, Any] = {"banner_fired": [], "notifications": [], "errors": []}
    if not failing:
        return outcome

    try:
        from omniagentos.notifications.service import record_notification
    except Exception as exc:  # noqa: BLE001 - alerting must never break the snapshot
        outcome["errors"].append(f"import record_notification failed: {type(exc).__name__}: {exc}")
        return outcome

    already = _load_alert_state(today)
    fired = set(already)
    for check in failing:
        name = check["name"]
        first_today = name not in already
        try:
            notification_id = record_notification(
                kind="alert",
                title=f"Agent health: {name} FAILING",
                body=check["evidence"],
                severity="high",
                ref_type="health_sentinel",
                ref_id=f"{name}:{today.isoformat()}",
                payload={
                    "check": name,
                    "status": check["status"],
                    "evidence": check["evidence"],
                    "snapshot": str(LATEST_PATH),
                },
                db_path=db_path,
                push=first_today,
            )
        except Exception as exc:  # noqa: BLE001
            outcome["errors"].append(f"{name}: {type(exc).__name__}: {exc}")
            continue
        if notification_id:
            outcome["notifications"].append(notification_id)
        if first_today:
            outcome["banner_fired"].append(name)
            fired.add(name)

    if fired != already:
        try:
            _save_alert_state(today, fired)
        except OSError as exc:
            outcome["errors"].append(f"alert-state write failed: {exc}")
    return outcome


# --------------------------------------------------------------------------------- run


def run(*, db_path: str | None = None, quiet: bool = False) -> dict[str, Any]:
    """One full pass: check -> snapshot -> ledger -> log -> briefing -> alert."""
    started_at = _now()
    started = time.monotonic()
    results = run_checks()

    # Existing, unconditional allowlisted heal path for nonzero exits (and its
    # historical installed-not-loaded bootstrap).  It remains independent of
    # the ledger-based, explicitly opt-in AUTOREMEDY path below.
    for result in results:
        if result.name == "launchd":
            _apply_kickstart(result)
            _record_launchd_remedies(result, now=started_at)
            break

    snapshot = build_snapshot(results, started_at=started_at, duration=time.monotonic() - started)

    # Persist the snapshot BEFORE alerting: a broken notification path must never
    # cost the operator the evidence.
    atomic_write(LATEST_PATH, json.dumps(snapshot, indent=2, sort_keys=True) + "\n")
    try:
        append_ledger(snapshot)
    except OSError as exc:
        snapshot.setdefault("errors", []).append(f"ledger append failed: {exc}")
    try:
        log_lines(snapshot)
    except OSError as exc:
        snapshot.setdefault("errors", []).append(f"log append failed: {exc}")

    today = started_at.date()
    launchd = next((check for check in snapshot["checks"] if check["name"] == "launchd"), {})
    has_open_remedies = bool(launchd.get("detail", {}).get("open_remedies"))
    if snapshot["failing"] or has_open_remedies:
        try:
            briefing = write_alert_briefing(snapshot, today=today)
            snapshot["alerts"]["briefing"] = str(briefing)
        except OSError as exc:
            snapshot.setdefault("errors", []).append(f"briefing write failed: {exc}")
        if snapshot["failing"]:
            alerts = emit_alerts(snapshot, today=today, db_path=db_path)
            snapshot["alerts"].update(alerts)
        atomic_write(LATEST_PATH, json.dumps(snapshot, indent=2, sort_keys=True) + "\n")

    if not quiet:
        icons = {OK: "OK  ", WARN: "WARN", FAIL: "FAIL"}
        print(
            f"health-sentinel {snapshot['ts']} overall={snapshot['overall']} "
            f"(ok={snapshot['counts'].get(OK, 0)} warn={snapshot['counts'].get(WARN, 0)} "
            f"fail={snapshot['counts'].get(FAIL, 0)}) in {snapshot['duration_seconds']}s"
        )
        for check in snapshot["checks"]:
            print(f"  [{icons.get(check['status'], '????')}] {check['name']}: {check['evidence']}")
        if snapshot["alerts"].get("briefing"):
            print(f"  briefing: {snapshot['alerts']['briefing']}")
        if snapshot["alerts"].get("banner_fired"):
            print(f"  bannered: {', '.join(snapshot['alerts']['banner_fired'])}")
    return snapshot


def _print_blocked_summary(report: dict[str, Any]) -> None:
    """One operator-facing screen for ``--watch-blocked``."""
    if report.get("error"):
        print(f"blocked-sessions: {report['error']}")
        return
    if "replay_at_index" in report:
        verdict = report.get("verdict") or {}
        print(
            f"blocked-sessions replay {Path(str(report['transcript'])).name} "
            f"@{report['replay_at_index']} blocked={report.get('blocked')} "
            f"minutes={report.get('minutes_blocked')} tool={verdict.get('tool_name')}"
        )
        print(f"  reason: {verdict.get('reason')}")
        return
    if "gaps" in report:
        print(
            f"blocked-sessions gap-scan: {report['total_gaps']} gap(s) > {report['gap_minutes']}m; "
            f"{report['turn_duration_preceded']} preceded by turn_duration; "
            f"{report['assistant_tool_use_preceded']} by assistant/tool_use; "
            f"{len(report['flagged'])} flagged; "
            f"p90(answered tool_use gap)={report['assistant_tool_use_gap_p90_minutes']}m"
        )
        return
    dispatch = report.get("dispatch") or {}
    print(
        f"blocked-sessions {report['ts']} scanned={report['scanned']} "
        f"candidates={len(report.get('candidates', []))} blocked={len(report.get('blocked', []))} "
        f"emitted={len(dispatch.get('emitted', []))} deduped={len(dispatch.get('suppressed', []))} "
        f"push_armed={report.get('push_armed')} in {report['duration_seconds']}s"
    )
    for entry in report.get("blocked", []):
        alert = entry.get("alert") or {}
        print(
            f"  [BLOCKED] {alert.get('account')} {alert.get('sessionId', '')[:8]} "
            f"{alert.get('tool_name')} {alert.get('minutes_blocked')}m {alert.get('cwd')}"
        )


def _print_audit_summary(report: dict[str, Any]) -> None:
    """One operator-facing screen for ``--audit``."""
    icons = {OK: "OK  ", WARN: "WARN", FAIL: "FAIL"}
    counts = report.get("counts", {})
    print(
        f"audit {report['ts']} overall={report['overall']} "
        f"(ok={counts.get(OK, 0)} warn={counts.get(WARN, 0)} fail={counts.get(FAIL, 0)}) "
        f"in {report['duration_seconds']}s [read-only]"
    )
    for check in report.get("checks", []):
        print(f"  [{icons.get(check['status'], '????')}] {check['id']}: {check['evidence']}")
    if report.get("machinery_error"):
        print(f"  MACHINERY: {report['machinery_error']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Agent health sentinel — one check cycle")
    parser.add_argument("--quiet", action="store_true", help="suppress stdout summary")
    parser.add_argument("--json", action="store_true", help="print the snapshot as JSON")
    parser.add_argument(
        "--fail-exit",
        action="store_true",
        help="exit 1 when any check fails (default: always exit 0 so launchd sees a clean run)",
    )
    parser.add_argument("--db-path", default=None, help="override the control-plane DB path")
    # The two CONSUMERS. Until these landed the sentinel was a detector with no
    # consumer: it reported `reflection FAILED` 115 runs out of 115 and did
    # nothing. Both arms are separate SLOs and deliberately separate schedules —
    # --watch-blocked runs on its own 300s label, --audit is on-demand. The
    # sentinel's own 1800s label is NOT touched: coupling a 5-minute stall
    # detector to a 30-minute drift audit means a slow audit starves detection.
    _blocked.add_arguments(parser)
    _audit.add_arguments(parser)
    parser.add_argument(
        "--no-push",
        action="store_true",
        default=True,
        help="never deliver a notification (DEFAULT; alerts are recorded to the alert log instead)",
    )
    parser.add_argument(
        "--arm-push",
        action="store_true",
        help="CONSEQUENTIAL: actually deliver blocked-session notifications (they leave the machine)",
    )
    args = parser.parse_args(argv)
    if args.arm_push:
        args.no_push = False

    if args.watch_blocked:
        code, report = _blocked.run_watch_blocked(args)
        if args.json:
            print(json.dumps(report, indent=2, sort_keys=True, default=str))
        elif not args.quiet:
            _print_blocked_summary(report)
        return code

    if args.audit:
        code, report = _audit.run_audit_cli(args)
        if args.json:
            print(json.dumps(report, indent=2, sort_keys=True, default=str))
        elif not args.quiet:
            _print_audit_summary(report)
        return code

    try:
        snapshot = run(db_path=args.db_path, quiet=args.quiet or args.json)
    except Exception as exc:  # noqa: BLE001 - last resort; still say something useful
        print(f"health-sentinel: run failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(snapshot, indent=2, sort_keys=True))
    if args.fail_exit and snapshot["overall"] == FAIL:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
