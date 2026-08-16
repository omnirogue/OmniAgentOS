"""Read-only quota telemetry for provider accounts — how much budget is LEFT.

The complement to ``omniagentos.routing.limit_state``, which is *reactive*: it
learns an account is exhausted only by being refused, then cools it. This module
is *predictive* — it reads the quota snapshot each CLI already caches on disk, so
the fleet can see "82% of the weekly window is gone" BEFORE a single spawn fails.

Strictly read-only: no network, no writes, no DB. Every collector is total —
a missing/corrupt/older-format file yields ``available=False`` with a ``reason``,
never an exception, because a usage panel must degrade to "unknown", never to a
500 or (worse) a confident wrong number.

Sources, per provider:

  claude  ``{config_dir}/.claude.json`` (or ``{config_dir}.json`` for the default
          ``~/.claude``, whose json is ``~/.claude.json`` — same dual layout
          ``service._read_account_email`` handles) -> ``cachedUsageUtilization``.
          Its ``utilization.limits[]`` is the authoritative list and carries all
          three windows the operator cares about: ``session`` (5h), ``weekly_all``,
          and ``weekly_scoped`` (per-model, e.g. Fable). Older CLIs predate
          ``limits[]``; we fall back to the ``five_hour``/``seven_day`` scalars.
          ``extra_usage`` is the paid-overage credit pool (minor units).
  codex   newest ``~/.codex/sessions/**/rollout-*.jsonl`` -> the LAST
          ``token_count`` event's ``rate_limits`` block (``primary``/``secondary``
          windows keyed by ``window_minutes``; 10080 = weekly, <=1440 = session).

grok / gemini / kimi / qwen expose no cached quota-window telemetry on disk.
They report
``available=False`` with a reason rather than a fabricated zero; observed-cooldown
state from ``limit_state`` remains their only honest signal.

EVERY snapshot is a CACHE, refreshed only when that CLI last ran. ``fetched_at`` /
``age_seconds`` / ``stale`` are part of the contract precisely so a caller can
never present a day-old number as live.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from omniagentos.accounts.service import detect_config_dirs

# Providers with no on-disk quota telemetry, and why (surfaced to the operator so
# "unknown" reads as a known gap, not a bug).
UNSUPPORTED_PROVIDERS: dict[str, str] = {
    "grok": "the grok CLI writes no usage telemetry to ~/.grok",
    "gemini": "the gemini CLI writes no usage telemetry to ~/.gemini",
    "kimi": "~/.kimi-code stores OAuth credentials only, no usage telemetry",
    "qwen": "~/.qwen stores sessions and auth configuration, not quota-window telemetry",
}

# A snapshot older than this is flagged stale. Both CLIs refresh their cache on
# every turn, so an account in active rotation stays well inside the window; a
# breach means the account has simply been idle.
STALE_AFTER_SECONDS = 3600.0

# Codex rolls one JSONL per session; only the newest few can hold the latest
# snapshot, and scanning every historical session on each page load is waste.
_CODEX_FILES_TO_SCAN = 6

WindowKind = Literal["session", "weekly_all", "weekly_scoped"]
Severity = Literal["normal", "warning", "critical"]

# Matches the severities the claude CLI itself stamps on its limits[] entries, so
# a derived severity (codex) and a reported one (claude) mean the same thing.
_WARNING_AT = 80.0
_CRITICAL_AT = 95.0


def severity_for(percent: float) -> Severity:
    """Derive a severity for providers that report a bare percentage."""
    if percent >= _CRITICAL_AT:
        return "critical"
    if percent >= _WARNING_AT:
        return "warning"
    return "normal"


class UsageWindow(BaseModel):
    """One rate-limit window's consumption — the unit the UI renders as a bar."""

    kind: WindowKind
    label: str  # human label, e.g. "Session (5h)" / "Weekly · Fable"
    percent: float  # 0-100 consumed
    severity: Severity
    resets_at: str | None = None  # UTC ISO8601
    window_minutes: int | None = None
    scope_model: str | None = None  # weekly_scoped only, e.g. "Fable"
    is_active: bool = False  # provider says this window is the binding one


class AccountCredits(BaseModel):
    """Paid-overage / credit balance, where the provider exposes one.

    ``used``/``limit`` are in the provider's MINOR units (claude sends cents with
    ``decimal_places=2``); ``used_amount``/``limit_amount`` are the major-unit
    values for display, so no caller has to rediscover the divisor.
    """

    enabled: bool = False
    used: int | None = None
    limit: int | None = None
    used_amount: float | None = None
    limit_amount: float | None = None
    percent: float | None = None
    currency: str | None = None
    # None = scale unknown (missing/unparseable). Never serialize unknown as 0 —
    # 0 is a measured scale that invents major units via 10**0.
    decimal_places: int | None = None
    balance: str | None = None  # codex-style opaque balance
    unlimited: bool = False
    disabled_reason: str | None = None


class ProviderUsage(BaseModel):
    """A quota snapshot for one provider account (or one account-less provider)."""

    provider: str
    account_key: str | None = None  # config_dir for claude; CODEX_HOME for codex
    email: str | None = None
    plan: str | None = None
    available: bool = False
    reason: str | None = None  # why unavailable — always set when not available
    windows: list[UsageWindow] = Field(default_factory=list)
    credits: AccountCredits | None = None
    fetched_at: str | None = None  # when the CLI captured it (UTC ISO8601)
    age_seconds: float | None = None
    stale: bool = False
    source: str | None = None  # file the snapshot came from

    @property
    def worst(self) -> UsageWindow | None:
        """The window closest to exhaustion — what a compact row should show."""
        return max(self.windows, key=lambda w: w.percent) if self.windows else None


def _now() -> datetime:
    return datetime.now(UTC)


def _read_json(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """Read a JSON object from disk, distinguishing *why* it is absent.

    Returns ``(data, error)``:
    - ``(dict, None)`` on a parseable object
    - ``(None, "missing")`` when the file is not present
    - ``(None, "unreadable")`` on OSError (permissions / IO)
    - ``(None, "corrupt")`` on JSON parse failure or non-object root

    Collapsing missing and corrupt into a bare ``None`` (and later the same
    reason as a present-but-empty object) is the governing
    missing-source-as-empty defect.
    """
    if not path.is_file():
        return None, "missing"
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except OSError:
        return None, "unreadable"
    except ValueError:
        return None, "corrupt"
    if not isinstance(data, dict):
        return None, "corrupt"
    return data, None


def _age(fetched_at: datetime, now: datetime | None = None) -> tuple[float, bool]:
    age = max(0.0, ((now or _now()) - fetched_at).total_seconds())
    return age, age > STALE_AFTER_SECONDS


def _as_float(value: Any) -> float | None:
    # bool is a subclass of int; float(False)==0.0 / float(True)==1.0 would turn
    # unparseable JSON booleans into a confident consumption number (the governing
    # non-result-as-favourable class). Reject them before coercion.
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result else None  # reject NaN


def _as_int(value: Any) -> int | None:
    """Strict integer parse — booleans are not credit/minute quantities."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


# --------------------------------------------------------------------------- claude


def _claude_json_candidates(config_dir: str) -> list[Path]:
    """Both layouts, in the order ``service._read_account_email`` probes them."""
    base = Path(config_dir)
    return [base / ".claude.json", Path(f"{config_dir}.json")]


def _claude_window(entry: dict[str, Any]) -> UsageWindow | None:
    """One ``utilization.limits[]`` entry -> a window (None if unusable)."""
    kind = str(entry.get("kind") or "")
    if kind not in ("session", "weekly_all", "weekly_scoped"):
        return None
    percent = _as_float(entry.get("percent"))
    if percent is None:
        return None

    scope_model: str | None = None
    scope = entry.get("scope")
    if isinstance(scope, dict):
        model = scope.get("model")
        if isinstance(model, dict):
            name = model.get("display_name")
            scope_model = str(name) if name else None

    if kind == "session":
        label = "Session (5h)"
    elif kind == "weekly_all":
        label = "Weekly"
    else:
        label = f"Weekly · {scope_model}" if scope_model else "Weekly (scoped)"

    reported = entry.get("severity")
    severity: Severity = (
        reported if reported in ("normal", "warning", "critical") else severity_for(percent)
    )
    resets_at = entry.get("resets_at")
    return UsageWindow(
        kind=kind,  # type: ignore[arg-type]
        label=label,
        percent=percent,
        severity=severity,
        resets_at=str(resets_at) if resets_at else None,
        window_minutes=300 if kind == "session" else 10080,
        scope_model=scope_model,
        is_active=bool(entry.get("is_active")),
    )


def _claude_legacy_windows(utilization: dict[str, Any]) -> list[UsageWindow]:
    """Pre-``limits[]`` fallback: the ``five_hour`` / ``seven_day`` scalars."""
    windows: list[UsageWindow] = []
    for key, kind, label, minutes in (
        ("five_hour", "session", "Session (5h)", 300),
        ("seven_day", "weekly_all", "Weekly", 10080),
    ):
        block = utilization.get(key)
        if not isinstance(block, dict):
            continue
        percent = _as_float(block.get("utilization"))
        if percent is None:
            continue
        resets_at = block.get("resets_at")
        windows.append(
            UsageWindow(
                kind=kind,  # type: ignore[arg-type]
                label=label,
                percent=percent,
                severity=severity_for(percent),
                resets_at=str(resets_at) if resets_at else None,
                window_minutes=minutes,
            )
        )
    return windows


def _claude_credits(utilization: dict[str, Any]) -> AccountCredits | None:
    extra = utilization.get("extra_usage")
    if not isinstance(extra, dict):
        return None
    used = _as_int(extra.get("used_credits"))
    limit = _as_int(extra.get("monthly_limit"))
    places = _as_int(extra.get("decimal_places"))
    # Scale is three-valued: a real int is known; None means unknown (missing or
    # unparseable). Unknown must not invent major units via 10**0 — that is a
    # confident dollar figure from an unmeasured scale (governing class). And
    # the scale field itself must stay None, never 0 (0 is a known scale).
    if places is None:
        decimals: int | None = None
        used_amount: float | None = None
        limit_amount: float | None = None
    else:
        decimals = places
        divisor = float(10**decimals)
        used_amount = used / divisor if used is not None else None
        limit_amount = limit / divisor if limit is not None else None

    percent = _as_float(extra.get("utilization"))
    if percent is None and used is not None and limit is not None and limit > 0:
        percent = used / limit * 100.0

    return AccountCredits(
        enabled=bool(extra.get("is_enabled")),
        used=used,
        limit=limit,
        used_amount=used_amount,
        limit_amount=limit_amount,
        percent=percent,
        currency=str(extra.get("currency")) if extra.get("currency") else None,
        decimal_places=decimals,
        disabled_reason=(
            str(extra.get("disabled_reason")) if extra.get("disabled_reason") else None
        ),
    )


def collect_claude(config_dir: str, *, now: datetime | None = None) -> ProviderUsage:
    """The cached quota snapshot for one Claude config dir.

    Probes both json layouts and keeps going until one actually carries a
    ``cachedUsageUtilization`` — the default ``~/.claude`` has a ``.claude.json``
    INSIDE it that holds no usage, with the real payload at ``~/.claude.json``, so
    stopping at the first parseable file would silently report the wrong thing.
    """
    usage = ProviderUsage(provider="claude", account_key=config_dir)
    checked: list[Path] = []
    # Per-path status for the final reason string — missing / unreadable /
    # present-but-empty must not collapse to one message.
    path_status: list[tuple[Path, str]] = []

    for path in _claude_json_candidates(config_dir):
        checked.append(path)
        data, read_err = _read_json(path)
        if data is None:
            # Three-valued: read_err is the classification. Never bare-truthiness
            # fold None into "missing" (unknown read failure ≠ absent file).
            if read_err is None:
                path_status.append((path, "unreadable"))
            else:
                path_status.append((path, read_err))
            continue
        cached = data.get("cachedUsageUtilization")
        if not isinstance(cached, dict):
            # File present and parseable but no usable utilization payload.
            path_status.append((path, "empty"))
            continue
        utilization = cached.get("utilization")
        if not isinstance(utilization, dict):
            # Present cachedUsageUtilization with missing/null/non-object
            # utilization is present-but-empty — never "missing" (file exists).
            path_status.append((path, "empty"))
            continue

        fetched_ms = _as_float(cached.get("fetchedAtMs"))
        if fetched_ms is not None:
            fetched = datetime.fromtimestamp(fetched_ms / 1000.0, UTC)
            usage.fetched_at = fetched.isoformat()
            usage.age_seconds, usage.stale = _age(fetched, now)
        else:
            # Age unknown — never default-claim "live" (stale=False is the model
            # default and would present an untimestamped cache as fresh).
            usage.stale = True

        limits = utilization.get("limits")
        if isinstance(limits, list):
            windows = [
                window
                for entry in limits
                if isinstance(entry, dict) and (window := _claude_window(entry)) is not None
            ]
        else:
            windows = []
        usage.windows = windows or _claude_legacy_windows(utilization)
        usage.credits = _claude_credits(utilization)
        usage.source = str(path)

        account = data.get("oauthAccount")
        if isinstance(account, dict):
            email = account.get("emailAddress") or account.get("email")
            usage.email = str(email) if email else None

        if not usage.windows:
            usage.reason = f"no usable limit windows in {path}"
            return usage
        usage.available = True
        return usage

    usage.reason = _claude_unavailable_reason(path_status, checked)
    return usage


def _claude_unavailable_reason(path_status: list[tuple[Path, str]], checked: list[Path]) -> str:
    """Explain *why* no usage was collected — never one string for every failure mode.

    Candidates are a pair of layouts; one may be missing while the other is
    present-but-empty. Prefer the strongest present-file diagnosis so a real
    empty ``.claude.json`` is never reported as "missing" just because the
    sibling ``.json`` path does not exist.
    """
    if not path_status and not checked:
        return "no usage cache candidates"
    by_status: dict[str, list[Path]] = {}
    for path, status in path_status:
        by_status.setdefault(status, []).append(path)

    def _join(paths: list[Path]) -> str:
        return " or ".join(str(p) for p in paths)

    # Priority: empty (file present, no utilization) > unreadable/corrupt > missing.
    if "empty" in by_status:
        return f"empty usage cache (no utilization): {_join(by_status['empty'])}"
    bad = by_status.get("unreadable", []) + by_status.get("corrupt", [])
    if bad:
        return f"unreadable usage cache: {_join(bad)}"
    if "missing" in by_status:
        return f"missing usage cache: {_join(by_status['missing'])}"
    detail = "; ".join(f"{p} ({status})" for p, status in path_status)
    return f"no usable usage cache: {detail}"


# ---------------------------------------------------------------------------- codex


def _codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path(os.path.expanduser("~")) / ".codex")


def _codex_window(block: Any) -> UsageWindow | None:
    """A ``rate_limits.primary``/``secondary`` block -> a window."""
    if not isinstance(block, dict):
        return None
    percent = _as_float(block.get("used_percent"))
    if percent is None:
        return None

    raw_minutes = block.get("window_minutes")
    minutes = _as_int(raw_minutes)
    # Duration is the only kind discriminator. Unknown/unparseable minutes must
    # not fall through to a manufactured weekly_all window (None is not "long").
    if minutes is None:
        return None
    # Codex keys windows by duration rather than name: a day or less is the
    # rolling session allowance, anything longer is the weekly pool.
    if minutes <= 1440:
        kind: WindowKind = "session"
        hours = max(1, round(minutes / 60))
        label = f"Session ({hours}h)"
    else:
        kind = "weekly_all"
        label = "Weekly"

    resets_at: str | None = None
    epoch = _as_float(block.get("resets_at"))
    if epoch is not None:
        resets_at = datetime.fromtimestamp(epoch, UTC).isoformat()

    return UsageWindow(
        kind=kind,
        label=label,
        percent=percent,
        severity=severity_for(percent),
        resets_at=resets_at,
        window_minutes=minutes,
    )


def _codex_credits(block: Any) -> AccountCredits | None:
    if not isinstance(block, dict):
        return None
    balance = block.get("balance")
    return AccountCredits(
        enabled=bool(block.get("has_credits")),
        unlimited=bool(block.get("unlimited")),
        balance=str(balance) if balance is not None else None,
    )


def _newest_codex_rollouts(home: Path, limit: int) -> list[Path]:
    try:
        files = list(home.glob("sessions/**/rollout-*.jsonl"))
    except OSError:
        return []
    scored: list[tuple[float, Path]] = []
    for path in files:
        try:
            scored.append((path.stat().st_mtime, path))
        except OSError:
            continue
    scored.sort(key=lambda item: item[0], reverse=True)
    return [path for _, path in scored[:limit]]


def _last_rate_limits(path: Path) -> tuple[dict[str, Any], str | None] | None:
    """The final ``rate_limits`` payload in a rollout, with its event timestamp.

    Scanned forward keeping the last hit: a rollout is append-only, so the last
    occurrence is the freshest, and the cheap ``in`` guard skips the JSON parse
    for the overwhelming majority of lines (tool calls, messages, reasoning).
    """
    found: tuple[dict[str, Any], str | None] | None = None
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                if '"rate_limits"' not in line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                payload = event.get("payload")
                if not isinstance(payload, dict):
                    continue
                limits = payload.get("rate_limits")
                if isinstance(limits, dict):
                    timestamp = event.get("timestamp")
                    found = (limits, str(timestamp) if timestamp else None)
    except OSError:
        return None
    return found


def collect_codex(*, now: datetime | None = None) -> ProviderUsage:
    """The quota snapshot Codex left in its most recent session rollout."""
    home = _codex_home()
    usage = ProviderUsage(provider="codex", account_key=str(home))

    rollouts = _newest_codex_rollouts(home, _CODEX_FILES_TO_SCAN)
    if not rollouts:
        usage.reason = f"no session rollouts under {home / 'sessions'}"
        return usage

    for path in rollouts:
        found = _last_rate_limits(path)
        if found is None:
            continue
        limits, timestamp = found

        windows = [
            window
            for block in (limits.get("primary"), limits.get("secondary"))
            if (window := _codex_window(block)) is not None
        ]
        if not windows:
            continue

        usage.windows = windows
        usage.credits = _codex_credits(limits.get("credits"))
        usage.source = str(path)
        plan = limits.get("plan_type")
        usage.plan = str(plan) if plan else None
        age_known = False
        if timestamp:
            usage.fetched_at = timestamp
            try:
                fetched = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            except ValueError:
                fetched = None
            if fetched is not None:
                if fetched.tzinfo is None:
                    fetched = fetched.replace(tzinfo=UTC)
                usage.age_seconds, usage.stale = _age(fetched, now)
                age_known = True
        if not age_known:
            # Missing or unparseable event time — do not claim the snapshot is live.
            usage.stale = True
            if not timestamp:
                usage.fetched_at = None
        usage.available = True
        return usage

    usage.reason = f"no rate_limits event in the {len(rollouts)} newest rollouts under {home}"
    return usage


# ------------------------------------------------------------------------ aggregate


def collect_all(*, now: datetime | None = None) -> list[ProviderUsage]:
    """Every provider's snapshot: one row per detected Claude config dir, one for
    codex, and one explicitly-unavailable row per telemetry-less provider."""
    snapshots = [collect_claude(config_dir, now=now) for config_dir, _ in detect_config_dirs()]
    snapshots.append(collect_codex(now=now))
    snapshots.extend(
        ProviderUsage(provider=provider, reason=reason)
        for provider, reason in UNSUPPORTED_PROVIDERS.items()
    )
    return snapshots
