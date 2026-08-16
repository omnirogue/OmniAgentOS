"""Stage D — Application, provenance, and self-evaluation."""

from __future__ import annotations

import json
import logging
import subprocess
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from omniagentos.contracts import default_db_path, utc_now_iso
from omniagentos.db.store import SqliteStore
from omniagentos.reflection.fingerprint import proposal_fingerprint
from omniagentos.reflection.guard import (
    ContentKind,
    authorise_write,
    coerce_payload,
    reflection_repo_root,
)
from omniagentos.reflection.kinds import (
    CONFIG_KINDS,
    DOCUMENT_KINDS,
    UNATTENDED_APPLY_KINDS,
    config_target_path,
    resolve_target_path,
    target_key,
)
from omniagentos.reflection.settlement import Settlement, classify_settlement, empty_artifacts
from omniagentos.runtime_paths import resolve_var_root

_LOG = logging.getLogger(__name__)


def retire_empty_apply_logs(
    log_dir: str | Path | None = None, pattern: str = "reliability-apply-*.log"
) -> list[Path]:
    """Return the apply logs the artifact rule retires (zero-byte = ungateable).

    The same rule that stops an empty briefing from being recorded as a success
    stops an empty apply log from being counted as evidence that anything was
    applied.  This CLASSIFIES ONLY — it never deletes and never signals.
    """
    # Package anchor (env_keys=(), environ={}): the reliability apply-log
    # writers are package-anchored; following OMNIAGENTOS_VAR would classify a
    # tree they never write (review B1).
    root = Path(log_dir) if log_dir else resolve_var_root(env_keys=(), environ={}, leaf=("log",))
    if not root.is_dir():
        return []
    return empty_artifacts(sorted(root.glob(pattern)))


def set_nested(data: dict[str, Any], keys: list[str], value: Any) -> None:
    """Set a deeply nested key inside a dictionary."""
    current = data
    for key in keys[:-1]:
        if key not in current or not isinstance(current[key], dict):
            current[key] = {}
        current = current[key]
    current[keys[-1]] = value


def apply_yaml_change(repo_root: Path, target: Any, proposed_val: Any) -> str:
    """Safely apply a YAML change at repo_root.

    THE HARD-STOP IS ENFORCED HERE, not (only) in ``validate_proposal``. The
    writer is the last point every caller passes through — ``apply_proposal``,
    and therefore ``POST /reflection/{id}/approve``, reaches this function with
    no validator anywhere on the path — so a refusal that lives upstream does
    not cover the human-approval route at all.

    The path is not computed here and then checked. It is OBTAINED from
    :func:`omniagentos.reflection.guard.authorise_write`, which refuses before
    it returns one; there is no other way to get an absolute path in this
    function, so there is no spelling that reaches the open() unchecked.
    """
    # Same reader the validator's hard-stop resolves through, so the file this
    # writes and the file that was checked cannot be different files.
    file_path = config_target_path(target)
    key_str = target_key(target)

    if not file_path:
        raise ValueError(f"Invalid target: {target}")

    # The payload is coerced BEFORE authorisation, so the guard examines the
    # object that will actually be applied rather than its wire form.
    val_parsed = coerce_payload(ContentKind.CONFIG, proposed_val)

    allowed = authorise_write(
        repo_root,
        file_path,
        content_kind=ContentKind.CONFIG,
        payload=val_parsed,
        key=key_str,
        confine_to="configs",
    )

    # `key_str` is guaranteed non-empty here: examine_payload refuses a keyless
    # config write, because replacing the whole file puts its contents beyond
    # any rule written about named keys. Enforce that invariant rather than
    # only documenting it -- if authorise_write ever stops refusing, this must
    # fail closed instead of writing to a key path spelled from `None`.
    if not key_str:
        raise ValueError(f"Refusing a keyless config write for target: {target}")

    try:
        data = yaml.safe_load(allowed.read_text()) if allowed.exists() else {}
    except Exception as exc:
        _LOG.error("Failed to parse YAML file %s: %s", allowed.canonical, exc)
        data = {}

    if not isinstance(data, dict):
        data = {}

    set_nested(data, key_str.split("."), val_parsed)

    written = allowed.write(yaml.safe_dump(data, sort_keys=True))
    _LOG.info("Successfully updated YAML file %s", written)
    return written


def write_document_change(repo_root: Path, kind: str, target: Any, proposed_val: str) -> str:
    """Append a document change at the path the SHARED resolver names.

    Fail closed, three times over:

    * a kind that is not a document kind is refused outright;
    * a document kind that resolves to no path is refused, instead of falling
      back to a default file; and
    * the resolved path is authorised against the hard-stop set BEFORE this
      function holds an absolute path at all.

    The old body ended in an else-branch that defaulted the path to the repo's
    AGENTS.md, so ANY kind the per-kind ``if`` did not recognise — ``skill``
    today, the next kind added tomorrow — was appended to the agents' own
    instruction file. That is not a hypothetical: this writer is reachable
    WITHOUT validation through the dashboard's ``POST /reflection/{id}/approve``
    route, which calls ``apply_proposal`` directly. The refusal has to live
    here, in the writer, not only in the validator — and the previous fix said
    exactly that in this docstring while still leaving the hard-stop set
    enforced only upstream. Saying where the boundary is does not put it there;
    :func:`omniagentos.reflection.guard.authorise_write` does.
    """
    if kind not in DOCUMENT_KINDS:
        raise ValueError(f"Not a document proposal kind: {kind!r}")

    file_path = resolve_target_path(kind, target)
    if not file_path:
        raise ValueError(
            f"{kind} proposal names no target and has no default document; "
            "refusing to write (fail closed)"
        )

    body = coerce_payload(ContentKind.DOCUMENT, proposed_val)
    allowed = authorise_write(
        repo_root,
        file_path,
        content_kind=ContentKind.DOCUMENT,
        payload=body,
        key=target_key(target),
    )

    # Append or create. A document already containing the text is left alone —
    # that is a no-op, not a refusal, so nothing is written and the canonical
    # path is still what gets reported.
    if allowed.exists():
        existing = allowed.read_text()
        if body not in existing:
            allowed.write(existing.rstrip() + "\n\n" + body.strip() + "\n")
    else:
        allowed.write(body.strip() + "\n")

    _LOG.info("Successfully wrote document change to %s", allowed.canonical)
    return allowed.canonical


def git_commit_change(repo_root: Path, file_path: str, summary: str) -> None:
    """Add and commit the change to main branch with metadata if on main."""
    try:
        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            cwd=str(repo_root),
            check=True,
        ).stdout.strip()

        if branch != "main":
            _LOG.info("Current branch is %s (not 'main'). Skipping git commit.", branch)
            return

        subprocess.run(["git", "add", file_path], cwd=str(repo_root), check=True)
        cmd = [
            "git",
            "commit",
            "-m",
            f"reflection: {summary}",
            "--author=reflection-loop <4580856+omniagentos-bot[bot]@users.noreply.github.com>",
        ]
        subprocess.run(cmd, cwd=str(repo_root), check=True)
        _LOG.info("Committed %s with reflection-loop author info.", file_path)
    except Exception as exc:
        _LOG.warning("Failed to commit change via git: %s", exc)


def apply_proposal(
    proposal_id: str,
    db_path: str | None = None,
) -> dict[str, Any] | None:
    """Apply a single approved/eligible proposal."""
    db = db_path or default_db_path()
    store = SqliteStore(db)

    with store._lock:
        row = store._connection.execute(
            "SELECT * FROM reflection_proposals WHERE id = ?", (proposal_id,)
        ).fetchone()

        if not row:
            _LOG.error("Proposal %s not found in database", proposal_id)
            return None

        p_id = row["id"]
        kind = row["kind"]
        target = row["target"]
        row["current"]
        proposed = row["proposed"]
        row["rationale"]
        row["status"]

        # Parse JSON keys if dicts
        try:
            target_parsed = json.loads(target)
        except Exception:
            target_parsed = target

        try:
            proposed_parsed = json.loads(proposed)
        except Exception:
            proposed_parsed = proposed

    # The same root reader the validator uses, so a proposal cannot be checked
    # in one tree and written in another.
    repo_root = reflection_repo_root()
    file_changed = ""

    # Apply based on kind
    try:
        if kind in CONFIG_KINDS:
            file_changed = apply_yaml_change(repo_root, target_parsed, proposed_parsed)
        elif kind in DOCUMENT_KINDS:
            proposed_str = str(proposed_parsed)
            file_changed = write_document_change(repo_root, kind, target_parsed, proposed_str)
        else:
            raise ValueError(f"Unsupported proposal kind: {kind}")

        # Git commit (if on main)
        git_commit_change(repo_root, file_changed, f"applied {kind} for {p_id}")

        # Record outcome and transition proposal status
        outcome_id = f"rfl_out_{uuid.uuid4().hex[:12]}"
        now = utc_now_iso()

        with store._lock:
            # Transition proposal status to 'promoted'
            store._connection.execute(
                "UPDATE reflection_proposals SET status = 'promoted', updated_at = ? WHERE id = ?",
                (now, p_id),
            )

            # Insert outcome row
            store._connection.execute(
                """
                INSERT INTO reflection_outcomes (
                    id, proposal_id, kind, target, applied_at, outcome_metrics_json, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    outcome_id,
                    p_id,
                    kind,
                    str(target_parsed),
                    now,
                    "{}",
                    "monitored",
                    now,
                    now,
                ),
            )

        # Settle on the file the apply actually produced.  A change that wrote
        # nothing (or wrote an empty file) is not a success — it is ungateable,
        # and it says so instead of being recorded as one.
        settlement = classify_settlement(repo_root / file_changed if file_changed else None)
        if settlement is Settlement.OK:
            _LOG.info("Successfully applied proposal %s and wrote outcome %s", p_id, outcome_id)
        else:
            _LOG.warning(
                "Applied proposal %s but its artifact settled '%s': %s",
                p_id,
                settlement.value,
                file_changed or "<no file>",
            )
        return {
            "proposal_id": p_id,
            "outcome_id": outcome_id,
            "kind": kind,
            "file_changed": file_changed,
            "settlement": settlement.value,
            "status": "success" if settlement is Settlement.OK else settlement.value,
        }

    except Exception as exc:
        _LOG.error("Failed to apply proposal %s: %s", p_id, exc)
        return {
            "proposal_id": p_id,
            "kind": kind,
            "status": "failed",
            "error": str(exc),
        }


def auto_apply_eligible(
    db_path: str | None = None,
    accepted_fingerprints: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Auto-apply only eligible kinds: lessons, brief_templates, model_config availability flags.

    ``accepted_fingerprints`` maps proposal id -> content fingerprint for the
    proposals the validate stage accepted this run. ``None`` means no validated
    set was provided — nothing is applied (fail closed): bulk apply must always
    sit downstream of validation. A pending row whose current content no longer
    matches its validated fingerprint is skipped (content changed after
    validation). Singular human-approved applies go through ``apply_proposal``
    directly.
    """
    if accepted_fingerprints is None:
        _LOG.warning(
            "auto_apply_eligible called without a validated proposal set; applying nothing (fail closed)."
        )
        return []

    db = db_path or default_db_path()
    store = SqliteStore(db)

    accepted = {str(key): str(value) for key, value in accepted_fingerprints.items()}
    eligible_proposals = []
    with store._lock:
        rows = store._connection.execute(
            "SELECT id, kind, target, proposed FROM reflection_proposals WHERE status = 'pending'"
        ).fetchall()

        for row in rows:
            p_id = row["id"]
            kind = row["kind"]
            target = row["target"]
            expected_fp = accepted.get(str(p_id))
            if expected_fp is None:
                continue
            row_fp = proposal_fingerprint(str(kind), str(target), str(row["proposed"]))
            if row_fp != expected_fp:
                _LOG.warning(
                    "proposal %s content changed after validation; skipping (fail closed).",
                    p_id,
                )
                continue

            # v1 auto-apply rules:
            # 1. UNATTENDED_APPLY_KINDS (lessons, brief_templates) — a strict
            #    subset of DOCUMENT_KINDS on purpose, so adding a document kind
            #    does not silently grant it unattended write access.
            # 2. model_config availability flags (check if target key has 'available')
            is_eligible = False
            if kind in UNATTENDED_APPLY_KINDS:
                is_eligible = True
            elif kind == "model_config":
                target_lower = str(target).lower()
                if "available" in target_lower or "availability" in target_lower:
                    is_eligible = True

            if is_eligible:
                eligible_proposals.append(p_id)

    applied_results = []
    for p_id in eligible_proposals:
        res = apply_proposal(p_id, db_path=db)
        if res:
            applied_results.append(res)

    return applied_results


def score_outcomes(db_path: str | None = None) -> dict[str, Any]:
    """Score yesterday's outcomes, check for regression, file rollbacks on regression."""
    db = db_path or default_db_path()
    store = SqliteStore(db)

    monitored_outcomes = []
    with store._lock:
        rows = store._connection.execute(
            "SELECT * FROM reflection_outcomes WHERE status = 'monitored'"
        ).fetchall()
        for r in rows:
            monitored_outcomes.append(dict(r))

    scored = []
    rollbacks_filed = []

    for outcome in monitored_outcomes:
        out_id = outcome["id"]
        prop_id = outcome["proposal_id"]
        outcome["kind"]
        target = outcome["target"]

        # 1. Fetch original proposal
        with store._lock:
            p_row = store._connection.execute(
                "SELECT * FROM reflection_proposals WHERE id = ?", (prop_id,)
            ).fetchone()

        if not p_row:
            continue

        p_current = p_row["current"]
        p_proposed = p_row["proposed"]
        p_row["rationale"]
        p_risk_class = p_row["risk_class"]

        # 2. Check for regression (Mock regression checking / metrics lookup)
        # In a real system, we'd query table swarm_runs/swarm_attempts to find runs executed after
        # outcome["applied_at"] vs runs executed before.
        # Let's check for any runs with state == 'failed' within the last 24h.
        # If the success rate dropped, flag as regression.
        # For our mock/test, we'll mark as success unless a special flag or test condition triggers regression.
        has_regression = False

        # Query actual runs to see if there is any failure after applied_at
        with store._lock:
            recent_fails = store._connection.execute(
                "SELECT COUNT(*) FROM swarm_runs WHERE status = 'failed' AND finished_at > ?",
                (outcome["applied_at"],),
            ).fetchone()
            fail_count = recent_fails[0] if recent_fails else 0
            if fail_count > 5:  # Arbitrary threshold to trigger rollback
                has_regression = True

        now = utc_now_iso()
        if has_regression:
            # Transition outcome status to regression
            with store._lock:
                store._connection.execute(
                    "UPDATE reflection_outcomes SET status = 'regression', updated_at = ? WHERE id = ?",
                    (now, out_id),
                )

            # File rollback proposal
            rollback_id = f"rfl_prop_rollback_{prop_id}"
            with store._lock:
                # Check if rollback proposal already filed
                exists = store._connection.execute(
                    "SELECT 1 FROM reflection_proposals WHERE id = ?", (rollback_id,)
                ).fetchone()

                if not exists:
                    store._connection.execute(
                        """
                        INSERT INTO reflection_proposals (
                            id, kind, target, current, proposed, rationale, evidence_refs_json, predicted_impact, risk_class, status, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            rollback_id,
                            "rollback",
                            target,
                            p_proposed,  # Reverting proposed back to current
                            p_current,
                            f"Rollback of proposal {prop_id} due to detected performance regression.",
                            json.dumps([prop_id]),
                            "Restores performance to baseline.",
                            p_risk_class,
                            "pending",
                            now,
                            now,
                        ),
                    )
                    _LOG.warning(
                        "Regression detected for outcome %s! Filed rollback proposal %s.",
                        out_id,
                        rollback_id,
                    )
                    rollbacks_filed.append(rollback_id)
        else:
            # Mark as successful
            with store._lock:
                store._connection.execute(
                    "UPDATE reflection_outcomes SET status = 'success', updated_at = ? WHERE id = ?",
                    (now, out_id),
                )
            scored.append(out_id)

    return {
        "scored_outcomes": scored,
        "rollbacks_filed": rollbacks_filed,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    try:
        _LOG.info(
            "Standalone bulk auto-apply is disabled (fail closed): proposals are applied "
            "only via the validated nightly runner (python -m omniagentos.reflection.runner) "
            "or singular human approval through the reflection API."
        )
        _LOG.info("Running outcomes evaluation...")
        score_res = score_outcomes()
        _LOG.info("Outcomes evaluation finished: %s", score_res)
    except Exception as exc:
        _LOG.error("Apply runner failed: %s", exc)
