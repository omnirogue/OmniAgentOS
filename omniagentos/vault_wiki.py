"""Flag-gated post-run wiki-update step: LLM-assisted vault knowledge extraction.

This module reads a completed run note and uses an LLM to decide whether the run
produced durable knowledge (new capability, gotcha, decision, failure pattern).
If so, it creates or updates a concept page in the vault and appends a back-link
to the run note. All exceptions are swallowed so wiki updates never block/fail runs.

Design intent:
- Runs with `OMNIAGENTOS_WIKI_UPDATE=1` trigger optional LLM review after the
  run note is written.
- Trivial runs (failures before artifacts, no meaningful outcome) are skipped
  via cheap heuristics to avoid LLM overhead.
- The LLM prompt passes run-note content + SCHEMA.md + instructions to create
  EXACTLY ONE concept note if durable knowledge was found.
- The LLM's file writes are constrained to vault_dir; any path traversal is
  rejected before write.
- The harness (not the LLM) appends a deterministic back-link to the run note's
  "## Extracted (auto)" section, ensuring bidirectional traceability.
- Failures are logged but never propagate; a wiki-update error is not a run error.

OPERATIONAL NOTE (multi-worker deployments): the LLM call (up to 120s) is now
wrapped in a liveness pump that emits heartbeats on a fixed cadence (1Hz) without
requiring the blocking operation to cooperate. The pump runs in a background timer
and is cancelled in a finally block, ensuring it cannot outlive the call. This
allows worker peer detection to remain observable during long LLM operations while
stale_s remains tunable. If workers still claim runs mid-finalization, increase
stale_s to accommodate your LLM latency distribution (e.g., stale_s=150 for
tail-latency coverage).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from omniagentos.path_containment import inode_relative_parts_anchored

LOG = logging.getLogger(__name__)


@contextmanager
def with_heartbeat(
    beat_fn: Callable[[], None], interval_s: float = 1.0
) -> Generator[None, None, None]:
    """Context manager that pumps a liveness beat on a fixed cadence around a blocking call.

    The beat function runs in a background timer thread and is pumped at the specified
    interval. The pump is cancelled in a finally block, ensuring it cannot outlive
    the context (no orphaned threads).

    Args:
        beat_fn: Callable invoked on each beat. Should be cheap and side-effect-free.
        interval_s: Interval in seconds between beats (default 1.0 Hz).

    Example:
        >>> beats = []
        >>> def record_beat():
        ...     beats.append(time.time())
        >>> with with_heartbeat(record_beat, interval_s=0.1):
        ...     time.sleep(0.3)
        >>> len(beats) >= 2  # At least 2 beats in 0.3s at 1/0.1 Hz
        True
    """
    timer: threading.Timer | None = None

    def heartbeat_loop() -> None:
        """Run the beat function and reschedule the next beat."""
        nonlocal timer
        try:
            beat_fn()
        except Exception:
            # Silently swallow beat exceptions so a transient error doesn't kill the pump
            pass
        finally:
            # Reschedule the next beat
            timer = threading.Timer(interval_s, heartbeat_loop)
            timer.daemon = True
            timer.start()

    try:
        # Start the initial beat
        timer = threading.Timer(interval_s, heartbeat_loop)
        timer.daemon = True
        timer.start()
        yield
    finally:
        # Cancel the pump in finally so it never outlives the context
        if timer is not None:
            timer.cancel()


def _is_trivial_run(run: dict[str, Any], artifacts: list[str] | None = None) -> bool:
    """Heuristic to skip runs that likely produced no durable knowledge.

    Returns True if:
    - Run failed before completing (state != COMPLETED)
    - No artifacts and no meaningful output_json (no work product)
    - No meaningful duration (< 1s)

    Args:
        run: Run dict from the database (keys from _RUN_COLUMNS)
        artifacts: List of artifact names from store.get_artifacts(run_id)
    """
    state = run.get("state")
    if state != "completed":
        return True

    artifacts_list = artifacts or []
    output_json_raw = run.get("output_json")
    # Handle output_json being a raw string from the database
    if isinstance(output_json_raw, str):
        output_json_raw = output_json_raw.strip()
        # Empty, null JSON, empty string, empty object/array are treated as no output
        if output_json_raw in ("", "null", '""', "{}", "[]"):
            output_json_raw = None

    if not artifacts_list and not output_json_raw:
        return True

    started = run.get("started_at")
    finished = run.get("finished_at")
    if started and finished:
        try:
            from datetime import datetime

            start_dt = datetime.fromisoformat(started.replace("Z", "+00:00"))
            finish_dt = datetime.fromisoformat(finished.replace("Z", "+00:00"))
            duration_s = (finish_dt - start_dt).total_seconds()
            if duration_s < 1:
                return True
        except (ValueError, AttributeError):
            pass

    return False


def _validate_vault_path(candidate: str, vault_dir: str) -> bool:
    """Validate that candidate path is within vault_dir (no traversal).

    Returns True if candidate resolves within vault_dir. Uses realpath to
    resolve .. and symlinks, so an escape fails closed (not in scope).
    """
    try:
        requested = Path(candidate)
        if requested.is_absolute() or ".." in requested.parts:
            return False
        vault_real = Path(vault_dir).resolve()
        candidate_real = (Path(vault_dir) / requested).resolve()
        return inode_relative_parts_anchored(candidate_real, vault_real) is not None
    except (ValueError, OSError):
        return False


def _append_run_note_backlink(run_note_file: Path, note_id: str) -> None:
    """Append a back-link to the run note in the "## Extracted (auto)" section.

    Appends "- [[<note_id>]]\n" to the run note's machine-owned section.
    If the section does not exist, creates it. Leaves "## Notes (human)" untouched.
    Failure to write the back-link is swallowed and logged; the already-written
    concept note is not lost.

    Args:
        run_note_file: Path to the run note file
        note_id: The ID of the concept note to link to
    """
    try:
        content = run_note_file.read_text(encoding="utf-8")
        extracted_section = "## Extracted (auto)"

        if extracted_section in content:
            # Append after the section header
            idx = content.find(extracted_section)
            # Find end of header line (next newline)
            eol = content.find("\n", idx)
            if eol != -1:
                insert_point = eol + 1
                backlink = f"- [[{note_id}]]\n"
                new_content = content[:insert_point] + backlink + content[insert_point:]
                run_note_file.write_text(new_content, encoding="utf-8")
                LOG.debug(f"Appended back-link to run note: {note_id}")
            else:
                LOG.warning("Could not find newline after Extracted section header")
        else:
            # Append the section before "## Notes (human)" if it exists, else at end
            notes_idx = content.find("## Notes (human)")
            if notes_idx != -1:
                # Insert before Notes section
                backlink = f"## Extracted (auto)\n\n- [[{note_id}]]\n\n"
                new_content = content[:notes_idx] + backlink + content[notes_idx:]
            else:
                # Append at end
                if not content.endswith("\n"):
                    content += "\n"
                content += f"\n## Extracted (auto)\n\n- [[{note_id}]]\n"
                new_content = content

            run_note_file.write_text(new_content, encoding="utf-8")
            LOG.debug(f"Created Extracted section and appended back-link: {note_id}")

    except Exception as e:
        # Swallow exception; back-link failure should not lose the concept note
        LOG.warning(f"Failed to append back-link to run note: {e}")


def maybe_update_wiki(
    run: dict[str, Any], vault_dir: str, artifacts: list[str] | None = None
) -> None:
    """Post-run wiki-update: extract durable knowledge from run note and vault.

    Behavior:
    - Returns immediately if OMNIAGENTOS_WIKI_UPDATE != "1" (default: off).
    - Skips trivial runs via _is_trivial_run heuristic.
    - Reads run note + SCHEMA.md and prompts LLM to create/update one concept note.
    - Validates all file paths to stay within vault_dir.
    - Appends a back-link to the run note's "## Extracted (auto)" section.
    - Swallows ALL exceptions; logs and returns cleanly.

    Args:
        run: Run dict from the database (includes state from _RUN_COLUMNS, no artifacts_list)
        vault_dir: Absolute path to the vault directory
        artifacts: List of artifact names from store.get_artifacts(run_id)
    """
    # Guard: flag must be explicitly set to enable.
    if os.environ.get("OMNIAGENTOS_WIKI_UPDATE") != "1":
        return

    try:
        # Guard: skip trivial runs.
        if _is_trivial_run(run, artifacts):
            LOG.debug(f"Skipping trivial run {run.get('id')}")
            return

        run_id_raw = run.get("id")
        note_path = run.get("vault_note_path")
        if not isinstance(run_id_raw, str) or not run_id_raw:
            LOG.debug("Run missing id; skipping wiki update")
            return
        run_id = run_id_raw
        if not isinstance(note_path, str) or not note_path:
            LOG.debug(f"Run {run_id} has no vault_note_path yet")
            return

        # Read the run note content.
        run_note_file = Path(vault_dir) / note_path
        if not run_note_file.exists():
            LOG.warning(f"Run note file not found: {run_note_file}")
            return

        run_note_content = run_note_file.read_text(encoding="utf-8")

        # Read SCHEMA.md for the LLM.
        schema_file = Path(vault_dir) / "SCHEMA.md"
        if not schema_file.exists():
            LOG.warning(f"SCHEMA.md not found at {schema_file}")
            return

        schema_content = schema_file.read_text(encoding="utf-8")

        # Invoke LLM to analyze run note and decide on knowledge extraction.
        concept_note_id = _invoke_wiki_update_llm(
            run_id, run_note_content, schema_content, vault_dir
        )

        # If a concept note was created, append a back-link to the run note.
        if concept_note_id:
            _append_run_note_backlink(run_note_file, concept_note_id)

    except Exception as e:
        # Swallow all exceptions so wiki failure never fails the run.
        LOG.exception(f"Wiki update failed for run {run.get('id')}: {e}")
        return


def _invoke_wiki_update_llm(
    run_id: str, run_note_content: str, schema_content: str, vault_dir: str
) -> str | None:
    """Call LLM to extract durable knowledge from run note and update vault.

    Prompts the LLM to:
    1. Analyze the run note for durable knowledge.
    2. Decide if a new concept note should be created (learning, failure, decision, etc.)
    3. If yes, create EXACTLY ONE note in the correct vault directory per SCHEMA.md.
    4. Return JSON with {created: bool, note_id: str | null, path: str | null}.

    The harness (not the LLM) appends the back-link to the run note.

    The blocking LLM call is wrapped in a heartbeat pump to maintain liveness
    observability during long operations (up to 120s). The pump is cancelled
    after the call exits (success or exception).

    Returns the note_id if a concept note was successfully created, else None.
    All file operations are validated; any path traversal is rejected before write.
    LLM output is validated against the frontmatter contract before persisting.
    """
    try:
        from omniagentos.adapters.registry import resolve_adapter
        from omniagentos.contracts import AgentInput, BudgetSpec, HarnessType, ResultStatus
    except ImportError as e:
        LOG.warning(f"Failed to import adapters: {e}")
        return None

    prompt = _wiki_update_prompt(run_note_content, schema_content, vault_dir)
    output_schema = {
        "type": "object",
        "properties": {
            "created": {"type": "boolean"},
            "note_id": {"type": ["string", "null"]},
            "path": {"type": ["string", "null"]},
            "note_content": {"type": ["string", "null"]},
            "reasoning": {"type": "string"},
        },
        "required": ["created", "note_id", "path", "note_content", "reasoning"],
    }

    # Harness/model are env-configurable. Default is cli-grok: wiki extraction is
    # auxiliary volume work, so it runs on the Grok subscription rather than
    # consuming Claude account capacity. Structured output is enforced by the
    # shared CliAdapter base (_structured_prompt/_validate_json), so any CLI
    # harness satisfies the JSON contract.
    harness_env = os.environ.get("OMNIAGENTOS_WIKI_HARNESS", "cli-grok")
    try:
        harness = HarnessType(harness_env)
    except ValueError:
        LOG.warning(f"Invalid OMNIAGENTOS_WIKI_HARNESS {harness_env!r}; using cli-grok")
        harness = HarnessType.CLI_GROK
    # Empty default lets each adapter apply its own default model (grok-4.5 for
    # cli-grok); set OMNIAGENTOS_WIKI_MODEL to pin one explicitly.
    model = os.environ.get("OMNIAGENTOS_WIKI_MODEL") or None

    try:
        adapter = resolve_adapter(harness)

        # Track heartbeats for observability during the blocking LLM call.
        beats: list[float] = []

        def beat() -> None:
            """Emit a liveness beat (timestamp, side-effect-free)."""
            beats.append(time.time())

        # Wrap the blocking LLM call in a heartbeat pump.
        with with_heartbeat(beat, interval_s=1.0):
            response = adapter.run(
                AgentInput(
                    run_id=f"wiki-{run_id}",
                    task_id=f"wiki-{run_id}",
                    prompt=prompt,
                    model=model,
                    output_schema=output_schema,
                    tools_allowed=[],
                    budget=BudgetSpec(wall_ms_max=120_000, max_turns=1),
                    metadata={"source": "vault_wiki_update"},
                )
            )
    except Exception as e:
        LOG.warning(f"LLM call failed for wiki update: {e}")
        return None

    if response.status != ResultStatus.OK:
        LOG.warning(f"LLM returned non-OK status: {response.status}")
        return None

    try:
        output = response.output_json
        if not isinstance(output, dict):
            LOG.warning(f"Invalid LLM output type: {type(output)}")
            return None

        if not output.get("created"):
            LOG.debug(f"LLM decided no knowledge extraction needed for {run_id}")
            return None

        note_id = output.get("note_id")
        note_path_rel = output.get("path")
        note_content = output.get("note_content")

        if not (
            isinstance(note_id, str)
            and note_id
            and isinstance(note_path_rel, str)
            and note_path_rel
            and isinstance(note_content, str)
            and note_content
        ):
            LOG.warning(f"LLM output missing required fields: {output}")
            return None

        # Validate path to prevent traversal.
        if not _validate_vault_path(note_path_rel, vault_dir):
            LOG.error(f"LLM returned invalid path (traversal attempt): {note_path_rel}")
            return None

        # Validate note_content: parse frontmatter and check NoteType validity.
        try:
            from omniagentos.contracts import NoteType
            from omniagentos.vault.frontmatter import parse_frontmatter

            frontmatter = parse_frontmatter(note_content)
            # Verify the type is a valid NoteType enum value
            try:
                NoteType(frontmatter.type)
            except ValueError:
                LOG.warning(
                    f"LLM produced note with invalid type '{frontmatter.type}' "
                    f"(not a valid NoteType). Not persisting."
                )
                return None
        except Exception as e:
            LOG.warning(f"LLM note_content failed frontmatter validation: {e}. Not persisting.")
            return None

        # Persist through the versioned vault write path (path confinement,
        # human-section merge, optional git autocommit). Raw write_text would
        # bypass recoverability and the vault contracts.
        from omniagentos.vault.write import write_note

        write_note(vault_dir, note_path_rel, note_content)
        LOG.info(f"Created vault note: {note_path_rel} (run {run_id})")

        # Return the note_id so the caller can append a back-link.
        return note_id

    except Exception as e:
        LOG.exception(f"Failed to write wiki update for {run_id}: {e}")
        return None


def _wiki_update_prompt(run_note_content: str, schema_content: str, vault_dir: str) -> str:
    """Construct the LLM prompt for vault knowledge extraction.

    Tells the LLM to:
    - Read the run note carefully
    - Decide if the run produced durable knowledge
    - If yes, create EXACTLY ONE concept note per SCHEMA.md rules
    - Return JSON with the note content, ID, and relative path

    The harness (not the LLM) is responsible for adding the back-link to the
    run note, so the prompt does not instruct the LLM to do so.
    """
    return f"""You are analyzing a run note to extract durable knowledge for the OmniAgentOS vault.

## Run Note

{run_note_content}

## Vault Schema

{schema_content}

## Task

Analyze the run note above. Determine if this run produced durable knowledge that should be captured as a concept note in the vault:
- A new learning or pattern discovered
- A failure or gotcha to document
- A decision made
- A capability or gotcha validation
- Any other durable insight

**STRICT RULES**:
1. Create EXACTLY ONE concept note if knowledge is found; zero notes otherwise.
2. Choose the correct note type (learning, failure, decision, benchmark, playbook, etc.) and directory per SCHEMA.md.
3. Use kebab-case IDs, e.g., "adapter-context-limits" or "sandbox-traversal-escape".
4. Follow the frontmatter contract exactly: id, type, discipline, created, source_run, confidence, status, supersedes.
5. The note body should have a title, a brief summary, and links to relevant vault pages via [[id-slug]] wikilinks.
6. Set source_run to the run ID from the note metadata.
7. Set confidence: high if the insight is validated by the run itself; medium if inferred.
8. Do NOT invent or guess; only capture what the run evidence clearly shows.

## Response Format

Return only valid JSON matching this schema:
{{
  "created": true|false,
  "note_id": "kebab-case-id or null",
  "path": "learnings/kebab-case-id.md or null (relative to vault)",
  "note_content": "full note markdown with frontmatter, or null",
  "reasoning": "brief explanation of why this knowledge is/isn't durable"
}}

If no durable knowledge is found, set created=false and all other fields to null.
If created=true, the path must be a relative path (e.g., "learnings/foo.md" or "failures/bar.md").
"""
