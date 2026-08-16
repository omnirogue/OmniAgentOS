#!/usr/bin/env python3
"""Provider & Account Sentinel — nightly 22:30 provider-truth refresh.

Runs 30 minutes before the 23:00 fable-curator (ARCHI.md "Scheduling") so
every overnight job starts from a fresh provider-health read instead of
yesterday's guess. Each pass, in order:

1. **Provider doctor** — live-launch the four non-Claude CLI providers
   (codex, grok, gemini, kimi) via
   ``omniagentos.swarm.provider_exec.ProviderSessionRunner().provider_doctor()``
   and add non-live default rows for the Fireworks and Moonshot paid HTTP
   providers before persisting the full result to ``var/provider-health.json``
   (``{ts, results}``, atomic tmp+rename). Claude is the fifth provider in
   the roster but is deliberately never live-doctored here: it runs through
   a different session path (SessionSupervisor, not ProviderSessionRunner),
   and spawning it would itself BE the LLM call item 5 of the build brief
   forbids. Its health rides the read-only usage snapshot in step 2 instead.
2. **Usage refresh** — read ``omniagentos.accounts.usage.collect_all()``.
   That module is strictly read-only (no network, no writes, no DB of its
   own) by design, so "refreshing" it means calling it AFTER step 1: codex's
   on-disk quota cache is freshened as a direct side effect of doctor's live
   codex spawn, and every consumer (this sentinel, the accounts dashboard,
   fable-curator's account picker) reads the same live collector — no
   parallel cache is invented here.
3. **Alerting** — for each provider/account that (a) fails doctor with an
   auth-shaped error, (b) shows a session window below the configured
   remaining-percent floor, or (c) has failed doctor for N consecutive
   nights (N = policy ``consecutive_fail_nights``, compared against archived
   ``var/provider-health/<date>.json`` snapshots), record a ``kind="alert"``
   notification — deduped per (provider, issue) per calendar night via a
   date-scoped ``ref_id`` (record_notification's own unread-ref dedupe then
   does the rest). Only an AUTH-SHAPED failure (never a transient one, and
   never condition (c) alone) additionally calls
   ``accounts.service.mark_status(account_id, "error", ...)`` — and only
   when policy ``disable_on_auth_failure`` is true.
4. **Curator handoff** — write a 3-line human summary to
   ``var/provider-health-latest-summary.txt`` (fable-curator's account
   picker context can see it) and append one line to
   ``var/improvement-log.jsonl`` (``improver: "provider-sentinel"``).

NO LLM calls of this script's own: every CLI subprocess spawned above is
``provider_doctor``'s own LIVE auth/process check (its documented job,
"never part of the unit-test suite") — nothing here opens a Claude/Fable
session for narration, unlike fable-curator's optional live-agent pass.

Runtime policy (session_remaining_alert_pct, consecutive_fail_nights,
disable_on_auth_failure) is parsed from the fenced ```yaml policy:``` block
in ``prompt.md`` on every run — see :func:`load_policy`.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = Path(__file__).resolve().parent

DEFAULT_HEALTH_PATH = REPO_ROOT / "var" / "provider-health.json"
DEFAULT_ARCHIVE_DIR = REPO_ROOT / "var" / "provider-health"
DEFAULT_SUMMARY_PATH = REPO_ROOT / "var" / "provider-health-latest-summary.txt"
DEFAULT_IMPROVEMENT_LOG = REPO_ROOT / "var" / "improvement-log.jsonl"
DEFAULT_LOG_PATH = REPO_ROOT / "var" / "log" / "provider-sentinel.log"
DEFAULT_PROMPT_PATH = SCRIPT_DIR / "prompt.md"

# ProviderSessionRunner.provider_doctor()'s own SUPPORTED_PROVIDERS. "claude"
# is the fifth provider in the overall roster but is intentionally excluded
# here -- see the module docstring.
LIVE_DOCTOR_PROVIDERS = ("codex", "grok", "gemini", "kimi")
PAID_SNAPSHOT_PROVIDERS = ("fireworks", "moonshot")
DOCTOR_PROVIDERS = (*LIVE_DOCTOR_PROVIDERS, *PAID_SNAPSHOT_PROVIDERS)

LOG = logging.getLogger("provider_sentinel")

DEFAULT_POLICY: dict[str, Any] = {
    "session_remaining_alert_pct": 10.0,
    "consecutive_fail_nights": 2,
    "disable_on_auth_failure": True,
}

_YAML_BLOCK_RE = re.compile(r"```ya?ml\s*\n(.*?)```", re.DOTALL)


# --------------------------------------------------------------------------- policy


def load_policy(
    prompt_path: Path = DEFAULT_PROMPT_PATH, *, logger: logging.Logger = LOG
) -> dict[str, Any]:
    """Parse the fenced ```yaml policy:``` block from ``prompt.md``.

    Falls back to :data:`DEFAULT_POLICY` -- per FIELD, not just wholesale --
    on any missing file, missing block, YAML syntax error, missing ``policy``
    mapping, or a wrong-typed individual field. Every fallback is logged;
    none of them raise. A dashboard edit to ``prompt.md`` genuinely changes
    tonight's behavior; a BROKEN edit degrades to the shipped defaults
    instead of crashing the run.
    """
    policy = dict(DEFAULT_POLICY)
    try:
        text = prompt_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("policy: could not read %s (%s); using defaults", prompt_path, exc)
        return policy

    match = _YAML_BLOCK_RE.search(text)
    if match is None:
        logger.warning("policy: no fenced yaml block found in %s; using defaults", prompt_path)
        return policy

    try:
        parsed = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        logger.warning("policy: yaml parse error in %s (%s); using defaults", prompt_path, exc)
        return policy

    if not isinstance(parsed, dict) or not isinstance(parsed.get("policy"), dict):
        logger.warning("policy: no 'policy' mapping in %s; using defaults", prompt_path)
        return policy

    raw = parsed["policy"]

    pct = raw.get("session_remaining_alert_pct", policy["session_remaining_alert_pct"])
    if isinstance(pct, (int, float)) and not isinstance(pct, bool) and 0 <= pct <= 100:
        policy["session_remaining_alert_pct"] = float(pct)
    else:
        logger.warning(
            "policy: invalid session_remaining_alert_pct=%r; using default %r",
            pct,
            policy["session_remaining_alert_pct"],
        )

    nights = raw.get("consecutive_fail_nights", policy["consecutive_fail_nights"])
    if isinstance(nights, int) and not isinstance(nights, bool) and nights >= 1:
        policy["consecutive_fail_nights"] = nights
    else:
        logger.warning(
            "policy: invalid consecutive_fail_nights=%r; using default %r",
            nights,
            policy["consecutive_fail_nights"],
        )

    disable = raw.get("disable_on_auth_failure", policy["disable_on_auth_failure"])
    if isinstance(disable, bool):
        policy["disable_on_auth_failure"] = disable
    else:
        logger.warning(
            "policy: invalid disable_on_auth_failure=%r; using default %r",
            disable,
            policy["disable_on_auth_failure"],
        )

    return policy


# ---------------------------------------------------------------------- persistence


def atomic_write_json(path: Path, data: Any) -> None:
    """Write ``data`` as JSON to ``path`` via tmp-file-then-``os.replace``.

    A crash or exception mid-write leaves the ORIGINAL file untouched (the
    tmp file lives in the same directory so ``os.replace`` is an atomic
    same-filesystem rename, never a partial write observable by a reader)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _date_from_iso(ts: str) -> str | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return None


def archive_and_persist(
    new_results: dict[str, dict[str, Any]],
    *,
    health_path: Path = DEFAULT_HEALTH_PATH,
    archive_dir: Path = DEFAULT_ARCHIVE_DIR,
    ts: str | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Archive whatever ``var/provider-health.json`` currently holds (last
    run's snapshot) to ``var/provider-health/<its-own-date>.json``, then
    atomically overwrite it with tonight's ``{ts, results}``.

    Returns ``(previous_payload, new_payload)`` -- ``previous_payload`` is
    ``None`` on the first-ever run. The archive write is idempotent per
    calendar day (a same-night re-run never clobbers an already-archived
    prior night)."""
    ts = ts or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    previous = _read_json(health_path)
    if previous is not None:
        prev_date = _date_from_iso(str(previous.get("ts") or "")) or date.today().isoformat()
        archive_path = archive_dir / f"{prev_date}.json"
        if not archive_path.exists():
            atomic_write_json(archive_path, previous)

    payload: dict[str, Any] = {"ts": ts, "results": new_results}
    atomic_write_json(health_path, payload)
    return previous, payload


# ---------------------------------------------------------------------- classification


def doctor_error_text(status: dict[str, Any]) -> str:
    """Combined exception and terminal-probe error text for one doctor key.

    A process exception remains in ``error``; a completed probe's real terminal
    error is now preserved separately in ``probe_error``.  Keep both signals
    available to the shared auth-shape table rather than degrading the latter
    back into a bare ``clean_exit`` boolean.
    """
    from omniagentos.swarm.provider_exec import provider_doctor_error_text

    return provider_doctor_error_text(status)


def classify_doctor_failure(provider: str, status: dict[str, Any]) -> str | None:
    """``'auth_error' | 'quota_exhausted' | 'transient_rate_limit' |
    'overloaded' | None`` for one ``provider_doctor()`` status entry.

    STRUCTURED-FIRST (docs/architecture/longhaul.md): only ever meaningful on
    a key whose own ``ok`` is already ``False`` -- ``provider_doctor``'s
    ``ok`` is a structural AND of stream/clean-exit/kill-timing checks, so a
    genuinely clean run is never misclassified by a coincidental phrase.
    Reuses ``longhaul.limits.classify_limit_text``, the SAME table
    ``provider_exec``'s own terminal-classification path (WP2 four-class
    table) uses for these same non-claude CLIs, so a phrase means the same
    thing here as it does mid-run."""
    if status.get("ok"):
        return None
    from omniagentos.longhaul.limits import classify_limit_text

    text = doctor_error_text(status)
    if not text:
        return None
    return classify_limit_text(provider, text)


def consecutive_fail_streak(
    key: str,
    *,
    tonight_ok: bool,
    archive_dir: Path,
    today: date,
    nights_needed: int,
) -> bool:
    """True iff ``key`` is ``ok=False`` tonight AND was ``ok=False`` on each
    of the ``nights_needed - 1`` immediately preceding nights, per the
    archived ``var/provider-health/<date>.json`` snapshots.

    A missing or unreadable archive for any required night breaks the streak
    conservatively -- this never asserts a repeat it cannot prove from disk."""
    if tonight_ok or nights_needed <= 0:
        return False
    if nights_needed == 1:
        return True
    for offset in range(1, nights_needed):
        day = today - timedelta(days=offset)
        archived = _read_json(archive_dir / f"{day.isoformat()}.json")
        if archived is None:
            return False
        results = archived.get("results")
        if not isinstance(results, dict):
            return False
        entry = results.get(key)
        if not isinstance(entry, dict) or entry.get("ok") is not False:
            return False
    return True


def low_session_windows(usages: list[Any], *, threshold_pct: float) -> list[tuple[Any, Any]]:
    """``(usage, window)`` pairs whose ``session`` window has less than
    ``threshold_pct`` remaining (``100 - window.percent``)."""
    hits: list[tuple[Any, Any]] = []
    for usage in usages:
        if not getattr(usage, "available", False):
            continue
        for window in usage.windows:
            if window.kind != "session":
                continue
            if (100.0 - window.percent) < threshold_pct:
                hits.append((usage, window))
    return hits


# --------------------------------------------------------------------------- alerts


@dataclass(frozen=True)
class Alert:
    provider: str
    account_id: str | None
    issue: str
    title: str
    body: str
    severity: str = "warning"
    mark_error: bool = False  # accounts.service.mark_status("error") side effect


def evaluate_alerts(
    *,
    doctor_results: dict[str, dict[str, Any]],
    previous_results: dict[str, Any] | None,
    usages: list[Any],
    policy: dict[str, Any],
    archive_dir: Path,
    today: date,
) -> list[Alert]:
    """The three alert conditions (a) auth-shaped doctor failure, (b) low
    session budget, (c) N-consecutive-nights doctor failure -- against
    tonight's doctor results and usage snapshot. ``previous_results`` is
    accepted for symmetry with the archive-based streak check but the streak
    itself is read from disk (:func:`consecutive_fail_streak`), not from
    this in-memory value, so a caller that only has the archive still gets
    identical behavior."""
    del previous_results  # streak comes from the archive directory itself
    alerts: list[Alert] = []
    nights_needed = int(policy.get("consecutive_fail_nights", 2))

    for key, status in doctor_results.items():
        if not isinstance(status, dict):
            continue
        provider = str(status.get("provider") or key.split(":", 1)[0])
        account_id = status.get("account_id")
        ok = bool(status.get("ok"))

        if not ok:
            limit_class = classify_doctor_failure(provider, status)
            if limit_class == "auth_error":
                alerts.append(
                    Alert(
                        provider=provider,
                        account_id=account_id,
                        issue="auth_failure",
                        title=f"Provider health: {provider} auth failure",
                        body=(
                            f"{key} failed provider_doctor with an auth-shaped "
                            f"error: {doctor_error_text(status)[:300]}"
                        ),
                        mark_error=bool(policy.get("disable_on_auth_failure", True)),
                    )
                )

        if consecutive_fail_streak(
            key,
            tonight_ok=ok,
            archive_dir=archive_dir,
            today=today,
            nights_needed=nights_needed,
        ):
            alerts.append(
                Alert(
                    provider=provider,
                    account_id=account_id,
                    issue="doctor_repeat_failure",
                    title=f"Provider health: {provider} doctor failing {nights_needed} nights running",
                    body=(
                        f"{key} has failed provider_doctor for {nights_needed} consecutive nights."
                    ),
                    mark_error=False,  # (c) alone never disables -- only auth-shape does
                )
            )

    threshold = float(policy.get("session_remaining_alert_pct", 10.0))
    for usage, window in low_session_windows(usages, threshold_pct=threshold):
        label = usage.email or usage.account_key or "default"
        alerts.append(
            Alert(
                provider=usage.provider,
                account_id=None,  # usage.py has no claude_accounts row id, only a config_dir/email
                issue="session_budget_low",
                title=f"Provider health: {usage.provider} session budget low",
                body=(
                    f"{usage.provider} account {label}: session window at "
                    f"{window.percent:.1f}% used ({100.0 - window.percent:.1f}% "
                    f"remaining, floor {threshold:.0f}%)."
                ),
                mark_error=False,
            )
        )
    return alerts


def emit_alerts(
    alerts: list[Alert],
    *,
    today: date,
    db_path: str | None = None,
) -> list[str]:
    """Persist each alert as a ``kind="alert"`` notification, deduped per
    (provider, issue) per calendar night via a date-scoped ``ref_id`` (so
    ``record_notification``'s own unread-ref dedupe naturally scopes to
    "one alert per night", not "one alert forever until read"). Auth-shaped
    alerts additionally call ``accounts.service.mark_status(..., "error")``
    when ``Alert.mark_error`` and a real account id are both present."""
    from omniagentos.accounts.service import mark_status
    from omniagentos.notifications.service import record_notification

    emitted: list[str] = []
    for alert in alerts:
        ref_id = (
            f"{alert.provider}:{alert.account_id or 'default'}:{alert.issue}:{today.isoformat()}"
        )
        notification_id = record_notification(
            kind="alert",
            title=alert.title,
            body=alert.body,
            severity=alert.severity,
            ref_type="provider_health",
            ref_id=ref_id,
            payload={
                "provider": alert.provider,
                "account_id": alert.account_id,
                "issue": alert.issue,
            },
            db_path=db_path,
        )
        if notification_id is not None:
            emitted.append(notification_id)
            LOG.warning("alert: %s", alert.title)
        if alert.mark_error and alert.account_id:
            try:
                mark_status(alert.account_id, "error", alert.body[:500], db_path=db_path)
            except Exception:  # noqa: BLE001 - alerting must not crash the run
                LOG.exception("mark_status(error) failed for account %s", alert.account_id)
    return emitted


# ---------------------------------------------------------------------- curator handoff


def write_summary(
    *,
    doctor_results: dict[str, dict[str, Any]],
    usages: list[Any],
    alerts: list[Alert],
    ts: str,
    summary_path: Path = DEFAULT_SUMMARY_PATH,
) -> None:
    """3-line human summary fable-curator's account picker context can see."""
    total = len(doctor_results)
    ok_count = sum(1 for s in doctor_results.values() if isinstance(s, dict) and s.get("ok"))
    fail_keys = sorted(
        k for k, s in doctor_results.items() if isinstance(s, dict) and not s.get("ok")
    )
    line1 = f"Provider health @ {ts}: doctor {ok_count}/{total} ok" + (
        f"; failing: {', '.join(fail_keys)}" if fail_keys else ""
    )

    usage_bits: list[str] = []
    for usage in usages:
        if not getattr(usage, "available", False):
            continue
        worst = usage.worst
        if worst is None:
            continue
        label = usage.email or usage.account_key or usage.provider
        usage_bits.append(f"{usage.provider}:{label} {worst.kind}={worst.percent:.0f}%")
    line2 = "Usage: " + ("; ".join(usage_bits) if usage_bits else "no telemetry available")

    if alerts:
        issues = ", ".join(sorted({f"{a.provider}:{a.issue}" for a in alerts}))
        line3 = f"Alerts tonight: {len(alerts)} ({issues})"
    else:
        line3 = "Alerts tonight: 0"

    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(line1 + "\n" + line2 + "\n" + line3 + "\n", encoding="utf-8")


def append_improvement_log(
    *,
    ts: str,
    notes: str,
    log_path: Path = DEFAULT_IMPROVEMENT_LOG,
) -> None:
    """Append one JSONL line (never rewrite), matching the shape fable-
    curator's own improvement-log entries use (``ts``, ``improver``,
    ``account_used``, ``changes``, ``notes``, ``report_path``)."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    entry: dict[str, Any] = {
        "ts": ts,
        "improver": "provider-sentinel",
        "account_used": None,
        "changes": [],
        "notes": notes,
        "report_path": None,
    }
    with open(log_path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, sort_keys=True) + "\n")


# --------------------------------------------------------------------------------- run


def _static_paid_provider_health() -> dict[str, dict[str, Any]]:
    """Return explicit non-live rows until paid HTTP probes are wired safely."""

    return {
        provider: {
            "provider": provider,
            "account_id": None,
            "ok": False,
            "live_check": False,
            "outcome": "unprobed",
            "error": "no non-billing health probe is wired",
        }
        for provider in PAID_SNAPSHOT_PROVIDERS
    }


def _configure_logging(log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(log_path)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)


def run(
    *,
    health_path: Path = DEFAULT_HEALTH_PATH,
    archive_dir: Path = DEFAULT_ARCHIVE_DIR,
    summary_path: Path = DEFAULT_SUMMARY_PATH,
    improvement_log_path: Path = DEFAULT_IMPROVEMENT_LOG,
    prompt_path: Path = DEFAULT_PROMPT_PATH,
    db_path: str | None = None,
) -> dict[str, Any]:
    """The full nightly pass: doctor -> usage -> alert -> curator handoff."""
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    today = datetime.now(UTC).date()

    policy = load_policy(prompt_path)
    LOG.info("policy: %s", policy)

    from omniagentos.swarm.provider_exec import ProviderSessionRunner

    runner = ProviderSessionRunner(db_path=db_path)
    LOG.info("provider doctor: starting live check across %s", DOCTOR_PROVIDERS)
    doctor_results = runner.provider_doctor(LIVE_DOCTOR_PROVIDERS)
    doctor_results.update(_static_paid_provider_health())
    LOG.info("provider doctor: done (%d keys)", len(doctor_results))
    for key, status in sorted(doctor_results.items()):
        LOG.info("provider doctor: %s -> %s", key, status)

    previous, _ = archive_and_persist(
        doctor_results, health_path=health_path, archive_dir=archive_dir, ts=ts
    )

    from omniagentos.accounts.usage import collect_all

    usages = collect_all()
    LOG.info("usage: collected %d provider snapshot(s)", len(usages))

    alerts = evaluate_alerts(
        doctor_results=doctor_results,
        previous_results=previous,
        usages=usages,
        policy=policy,
        archive_dir=archive_dir,
        today=today,
    )
    emitted = emit_alerts(alerts, today=today, db_path=db_path)
    LOG.info(
        "alerts: %d evaluated, %d emitted (%d deduped)",
        len(alerts),
        len(emitted),
        len(alerts) - len(emitted),
    )

    write_summary(
        doctor_results=doctor_results,
        usages=usages,
        alerts=alerts,
        ts=ts,
        summary_path=summary_path,
    )

    ok_count = sum(1 for s in doctor_results.values() if isinstance(s, dict) and s.get("ok"))
    notes = (
        f"doctor {ok_count}/{len(doctor_results)} ok; "
        f"{len(alerts)} alert(s) evaluated, {len(emitted)} emitted"
    )
    append_improvement_log(ts=ts, notes=notes, log_path=improvement_log_path)

    return {
        "ts": ts,
        "doctor_results": doctor_results,
        "usages": usages,
        "alerts": alerts,
        "emitted_notification_ids": emitted,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Provider & Account Sentinel nightly pass")
    parser.add_argument("--log-path", default=str(DEFAULT_LOG_PATH))
    args = parser.parse_args(argv)
    _configure_logging(Path(args.log_path))
    try:
        result = run()
    except Exception:
        LOG.exception("provider-sentinel run failed")
        return 1
    LOG.info("provider-sentinel run complete: ts=%s", result["ts"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
