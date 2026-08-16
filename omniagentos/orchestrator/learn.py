"""Stage 5 — learn (fan-out, fire-and-forget).

On each executor session completion the orchestrator triggers the EXISTING learners:

* the session-transcript -> skills path (``selfimprove.curator.curate_sessions``), and
* a vault/brain write (``vault.write_note``) recording the completed unit.

These already exist; this module just INVOKES them, fire-and-forget, so a slow or
failing learner never blocks the orchestration. The default hook runs them on a daemon
thread; a test injects a synchronous recording hook to assert they fired.
"""

from __future__ import annotations

import logging
import threading

from omniagentos.contracts import NoteType, VaultFrontmatter, utc_now_iso
from omniagentos.orchestrator.contracts import LearnEvent
from omniagentos.vault.frontmatter import render_frontmatter
from omniagentos.vault.paths import _safe_filename_id

LOG = logging.getLogger(__name__)


def _curate(vault_dir: str) -> None:
    try:
        from omniagentos.selfimprove.curator import curate_sessions

        curate_sessions(vault_dir=vault_dir)
    except Exception:  # noqa: BLE001 -- a learner fault must never surface to the loop.
        LOG.debug("orchestrator curate_sessions failed", exc_info=True)


def _brain_write(event: LearnEvent) -> None:
    if not event.session_id:
        return
    try:
        from omniagentos.vault import write_note

        note_id = f"orchestration-session-{_safe_filename_id(event.session_id)}"
        fm = VaultFrontmatter(
            id=note_id,
            type=NoteType.LEARNING,
            created=utc_now_iso(),
            status="active",
        )
        body = (
            f"# Orchestration session {event.session_id}\n\n"
            f"Task: {event.task_title}\n\n"
            f"## Outcome\n\n{(event.output_text or '(monitored session)').strip()}\n"
        )
        write_note(
            event.vault_dir,
            f"orchestration/sessions/{note_id}.md",
            render_frontmatter(fm) + "\n" + body,
        )
    except Exception:  # noqa: BLE001
        LOG.debug("orchestrator brain write failed", exc_info=True)


def run_learners(event: LearnEvent) -> None:
    """Synchronously invoke the learners for a completed session (errors swallowed)."""
    _brain_write(event)
    _curate(event.vault_dir)


def default_learn_hook(event: LearnEvent) -> None:
    """Fire-and-forget: run the learners on a daemon thread so the loop never waits."""
    thread = threading.Thread(
        target=run_learners, args=(event,), name="orchestrator-learn", daemon=True
    )
    thread.start()


__all__ = ["default_learn_hook", "run_learners"]
