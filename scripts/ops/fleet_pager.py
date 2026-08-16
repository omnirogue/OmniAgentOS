#!/usr/bin/env python3
"""Page on launchd signal exits and unexpectedly disappeared OmniAgentOS jobs.

This is a read-only observer: it invokes only ``launchctl list`` and writes its
own state below ``var/fleet-pager``.  Schedule it from cron or launchd at the
desired polling interval; it never loads, unloads, or kickstarts a job.
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

LAUNCHCTL_COMMAND = ("/bin/launchctl", "list")
LAUNCHD_PREFIX = "com.omniagentos."
STATE_VERSION = 1
PAGER_DIR = REPO_ROOT / "var" / "fleet-pager"
STATE_PATH = PAGER_DIR / "state.json"
ALERT_STATE_PATH = PAGER_DIR / "alert-state.json"
SUBPROCESS_TIMEOUT_SECONDS = 15
DEDUPLICATION_WINDOW = timedelta(minutes=5)
EVENT_SIGNAL = "signal"
EVENT_DISAPPEARED = "disappeared"

LOGGER = logging.getLogger(__name__)
NotificationRecorder = Callable[..., str | None]


@dataclass(frozen=True)
class AlertEvent:
    """One condition that warrants an on-call page."""

    event_type: str
    unit: str
    pid: str
    last_exit_status: str
    previous_last_exit_status: str

    @property
    def key(self) -> str:
        """Return the de-duplication key for this exact condition."""
        if self.event_type == EVENT_SIGNAL:
            return f"{self.event_type}:{self.unit}:{self.last_exit_status}"
        return f"{self.event_type}:{self.unit}"


def parse_launchctl_list(
    output: str, *, prefix: str = LAUNCHD_PREFIX
) -> dict[str, tuple[str, str]]:
    """Return prefixed ``label -> (pid, last_exit_status)`` launchctl rows."""
    units: dict[str, tuple[str, str]] = {}
    for line in output.splitlines()[1:]:
        columns = line.split("\t")
        if len(columns) < 3:
            continue
        label = columns[2].strip()
        if label.startswith(prefix):
            units[label] = (columns[0].strip(), columns[1].strip())
    return units


def read_launchctl_list() -> dict[str, tuple[str, str]] | None:
    """Read launchctl's table, returning ``None`` if its subprocess failed."""
    try:
        result = subprocess.run(
            LAUNCHCTL_COMMAND,
            capture_output=True,
            text=True,
            check=False,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        LOGGER.error("launchctl list failed: %s", exc)
        return None
    if result.returncode != 0:
        LOGGER.error(
            "launchctl list failed with rc=%s: %s", result.returncode, result.stderr.strip()
        )
        return None
    return parse_launchctl_list(result.stdout)


def empty_state() -> dict[str, Any]:
    """Return an empty, JSON-serializable pager state document."""
    return {"version": STATE_VERSION, "units": {}}


def state_for_units(units: dict[str, tuple[str, str]], *, seen_at: datetime) -> dict[str, Any]:
    """Build a durable state snapshot from a launchctl table."""
    timestamp = seen_at.isoformat()
    return {
        "version": STATE_VERSION,
        "units": {
            label: {"pid": pid, "last_exit_status": status, "seen_at": timestamp}
            for label, (pid, status) in sorted(units.items())
        },
    }


def load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    """Load a state document, treating absent or malformed data as empty."""
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except (OSError, json.JSONDecodeError) as exc:
        LOGGER.error("could not read %s: %s", path, exc)
        return default
    if not isinstance(loaded, dict):
        LOGGER.error("could not read %s: top-level JSON must be an object", path)
        return default
    return loaded


def save_json(path: Path, payload: dict[str, Any]) -> None:
    """Atomically persist a small JSON state document."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as stream:
        json.dump(payload, stream, indent=2, sort_keys=True)
        stream.write("\n")
        temporary_path = Path(stream.name)
    temporary_path.replace(path)


def is_negative_signal(status: str) -> bool:
    """Whether a launchctl last-exit status is a negative signal number."""
    try:
        return int(status) < 0
    except (TypeError, ValueError):
        return False


def detect_alerts(
    previous_state: dict[str, Any], current_units: dict[str, tuple[str, str]]
) -> list[AlertEvent]:
    """Find signal transitions and units absent from the previous snapshot."""
    previous_units = previous_state.get("units", {})
    if not isinstance(previous_units, dict):
        previous_units = {}
    events = _detect_signal_alerts(previous_units, current_units)
    events.extend(_detect_disappearance_alerts(previous_units, current_units))
    return events


def _detect_signal_alerts(
    previous_units: dict[str, Any], current_units: dict[str, tuple[str, str]]
) -> list[AlertEvent]:
    events: list[AlertEvent] = []
    for label, (pid, status) in current_units.items():
        previous = previous_units.get(label)
        prior_status = previous.get("last_exit_status", "-") if isinstance(previous, dict) else "-"
        if isinstance(previous, dict) and is_negative_signal(status) and status != prior_status:
            events.append(AlertEvent(EVENT_SIGNAL, label, pid, status, str(prior_status)))
    return events


def _detect_disappearance_alerts(
    previous_units: dict[str, Any], current_units: dict[str, tuple[str, str]]
) -> list[AlertEvent]:
    events: list[AlertEvent] = []
    for label, previous in previous_units.items():
        if label in current_units or not isinstance(previous, dict):
            continue
        events.append(
            AlertEvent(
                EVENT_DISAPPEARED,
                label,
                str(previous.get("pid", "-")),
                str(previous.get("last_exit_status", "-")),
                str(previous.get("last_exit_status", "-")),
            )
        )
    return events


def should_emit(event: AlertEvent, alert_state: dict[str, Any], *, now: datetime) -> bool:
    """Return whether the same condition has not paged within five minutes."""
    timestamps = alert_state.get("alerts", {})
    recorded = timestamps.get(event.key) if isinstance(timestamps, dict) else None
    if not isinstance(recorded, str):
        return True
    try:
        previous_time = datetime.fromisoformat(recorded)
    except ValueError:
        return True
    if previous_time.tzinfo is None:
        previous_time = previous_time.replace(tzinfo=UTC)
    return now - previous_time >= DEDUPLICATION_WINDOW


def mark_emitted(event: AlertEvent, alert_state: dict[str, Any], *, now: datetime) -> None:
    """Record the successful page time for the event's unit and condition."""
    alerts = alert_state.setdefault("alerts", {})
    if not isinstance(alerts, dict):
        alert_state["alerts"] = alerts = {}
    alerts[event.key] = now.isoformat()
    alert_state["version"] = STATE_VERSION


def _event_title(event: AlertEvent) -> str:
    if event.event_type == EVENT_SIGNAL:
        return f"{event.unit} exited with signal {event.last_exit_status}"
    return f"{event.unit} disappeared from launchctl"


def _event_body(event: AlertEvent) -> str:
    if event.event_type == EVENT_SIGNAL:
        return (
            f"pid={event.pid}; last exit changed from {event.previous_last_exit_status} "
            f"to {event.last_exit_status}."
        )
    return f"last seen pid={event.pid}; last exit status={event.last_exit_status}."


def _notification_recorder() -> NotificationRecorder | None:
    """Import the optional notification seam without making observation fatal."""
    try:
        from omniagentos.notifications.service import record_notification
    except Exception as exc:  # noqa: BLE001 - pager must keep its baseline
        LOGGER.error("record_notification import failed: %s: %s", type(exc).__name__, exc)
        return None
    return record_notification


def emit_alert(
    event: AlertEvent, *, now: datetime, recorder: NotificationRecorder | None = None
) -> bool:
    """Persist a high-severity notification for an event, returning success."""
    notifier = recorder or _notification_recorder()
    if notifier is None:
        return False
    try:
        notifier(
            kind="alert",
            title=_event_title(event),
            body=_event_body(event),
            severity="high",
            ref_type="fleet_pager",
            ref_id=f"{event.unit}:{now.date().isoformat()}",
        )
    except Exception as exc:  # noqa: BLE001 - notification failures are not fatal
        LOGGER.error("could not record fleet pager alert for %s: %s", event.unit, exc)
        return False
    return True


def run(
    *,
    now: datetime | None = None,
    state_path: Path = STATE_PATH,
    alert_state_path: Path = ALERT_STATE_PATH,
    recorder: NotificationRecorder | None = None,
) -> list[AlertEvent]:
    """Poll, diff, emit non-duplicate pages, then persist the new baseline."""
    observed_at = now or datetime.now(UTC)
    current_units = read_launchctl_list()
    if current_units is None:
        return []
    previous_state = load_json(state_path, empty_state())
    alert_state = load_json(alert_state_path, {"version": STATE_VERSION, "alerts": {}})
    emitted: list[AlertEvent] = []
    for event in detect_alerts(previous_state, current_units):
        if should_emit(event, alert_state, now=observed_at) and emit_alert(
            event, now=observed_at, recorder=recorder
        ):
            mark_emitted(event, alert_state, now=observed_at)
            emitted.append(event)
    try:
        save_json(alert_state_path, alert_state)
        save_json(state_path, state_for_units(current_units, seen_at=observed_at))
    except OSError as exc:
        LOGGER.error("fleet pager state write failed: %s", exc)
    return emitted


def main() -> int:
    """Run one polling pass."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verbose", action="store_true", help="enable INFO logging")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)
    run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
