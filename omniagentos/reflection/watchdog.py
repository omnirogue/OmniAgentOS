"""Watchdog (the fixer for the fixer) for the nightly reflection loop (Lane R3)."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from omniagentos.contracts import default_db_path, default_vault_dir, utc_now_iso
from omniagentos.reflection.runner import STAGES, load_reflection_config, run_reflection_loop
from omniagentos.reflection.settlement import (
    AcceptanceFloor,
    Settlement,
    acceptance_floor,
    classify_settlement,
    run_settlement,
)
from omniagentos.reflection.validate import is_hard_stop
from omniagentos.vault.paths import reflection_alert_relpath, reflection_briefing_relpath

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Alert cards carry an "Occurrences: N (first: ..., last: ...)" line so a
# refreshed card (see ``file_board_alert``) can recover its running count
# instead of restarting it at 1 on every check, plus a "Condition: <hash>"
# line (see ``_condition_key``) that is the card's REAL dedupe identity.
#
# Both regexes are matched with ``re.MULTILINE`` because these lines live
# inside a longer description, but ``reason`` -- attacker/log-controlled
# text interpolated into that description BEFORE either structured line --
# can itself contain text that matches one of these patterns. Whichever line
# WE actually appended is always the LAST match in the string (nothing
# user-controlled follows it), so both extractor helpers below take the last
# match, never ``.search()``'s leftmost-first: that is what keeps a forged
# "Occurrences: 41 (...)" or "Condition: ..." line inside ``reason`` from
# hijacking the parser (the admitted proposal's occurrence-metadata-
# confusion falsifier).
_OCCURRENCE_RE = re.compile(r"^Occurrences: (\d+) \(first: (\S+), last: \S+\)$", re.MULTILINE)
_CONDITION_RE = re.compile(r"^Condition: ([0-9a-f]{16})$", re.MULTILINE)


def _condition_key(reason: str) -> str:
    """Stable identity for a watchdog CONDITION, independent of the display title.

    ``file_board_alert`` truncates its human-readable title to 120
    characters. Two distinct reasons whose first 117 characters happen to be
    identical (e.g. "Briefing file does not exist: .../reflection-2026-08-
    08.md" vs. the same path for "...-08-09.md") must never be treated as
    the same condition just because their titles truncate alike -- this key
    is derived from the FULL, untruncated reason instead, and is what both
    the writer's dedupe lookup and the backlog cleanup script's grouping key
    on (see the admitted proposal's truncated-title-collision falsifier).
    """
    return hashlib.sha256(reason.encode("utf-8")).hexdigest()[:16]


def _extract_condition_key(description: str) -> str | None:
    """The card's OWN condition key (see the anchoring note above ``_OCCURRENCE_RE``)."""
    matches = list(_CONDITION_RE.finditer(description))
    return matches[-1].group(1) if matches else None


def _extract_occurrence_info(description: str) -> tuple[int, str] | None:
    """(occurrences, first_seen) from the card's OWN structured line, or None."""
    matches = list(_OCCURRENCE_RE.finditer(description))
    if not matches:
        return None
    match = matches[-1]
    return int(match.group(1)), match.group(2)


def _alert_description(
    reason: str, occurrences: int, first_seen: str, last_seen: str, condition_key: str
) -> str:
    return (
        "The reflection watchdog has identified a critical failure in the "
        "nightly self-repair loop, and the automatic re-run attempt has also failed.\n\n"
        f"Reason for failure: {reason}\n\n"
        f"Occurrences: {occurrences} (first: {first_seen}, last: {last_seen})\n\n"
        f"Condition: {condition_key}\n\n"
        "Please investigate the logs."
    )


def parse_iso(dt_str: str | None) -> datetime | None:
    """Safely parse ISO datetime string."""
    if not dt_str:
        return None
    try:
        return datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
    except Exception:
        # Fallback formatting
        for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(dt_str.split(".")[0], fmt)
            except ValueError:
                continue
    return None


# Run-level status is written in exactly two places: runner.py:66
# (create_run -> "running") and runner.py:448/452 (finish_run ->
# "completed"/"failed"). Live census on production state (2026-08-11):
# failed 1714, completed 4 -- no other value observed.
#
# PERMITS an UNGATEABLE verdict -- deliberately NARROW and fail-closed in
# THIS direction: a status NOT in this set can never buy an exclusion by
# being unlisted, it falls straight through to FAILED. "running" = the
# night is still in flight. "completed" = the runner itself folded the
# stages and found nothing to fail on (runner.py:448 maps its own
# UNGATEABLE fold to "completed"), so trusting the stage-fold classifier
# here is safe.
_RUN_STATUS_PERMITS_UNGATEABLE: frozenset[str] = frozenset({"running", "completed"})

# TERMINAL statuses -- narrow in the OTHER direction, also fail-closed: a
# status not listed here is treated as still IN FLIGHT, so the watchdog
# declines to auto re-run (and so re-enter) a loop whose state it cannot
# positively read as finished.
_RUN_STATUS_TERMINAL: frozenset[str] = frozenset({"completed", "failed"})


def _settle_with_status_override(stage_settlement: Settlement, status: str) -> Settlement:
    """Apply the run-status override to a stage-fold settlement.

    Pure and DB-independent on purpose: the ``reflection_runs.status`` column
    carries a ``CHECK (status IN ('running','completed','failed'))``
    constraint, so an "unrecognised" status can never actually be stored --
    but the FAIL-CLOSED shape of this rule (enumerate what is PERMITTED,
    never what is FORBIDDEN) is a safety property in its own right and must
    hold even if that constraint is ever loosened. A stage fold that is not
    UNGATEABLE is returned unchanged; only an UNGATEABLE fold can be
    overridden, and only to FAILED, never to OK.
    """
    if stage_settlement is Settlement.UNGATEABLE and status not in _RUN_STATUS_PERMITS_UNGATEABLE:
        return Settlement.FAILED
    return stage_settlement


@dataclass(frozen=True)
class NightVerdict:
    """The night's three-valued judgment, produced in exactly one place.

    ``settlement`` is the raw stage-fold classifier (``run_settlement``)
    PLUS the run-status override documented on
    ``_RUN_STATUS_PERMITS_UNGATEABLE``: an UNGATEABLE stage fold is
    trusted only when the run row's own ``status`` is in that narrow
    permit set, so a positive run-level failure assertion (``status``
    outside the permit set -- e.g. ``'failed'``, which ``runner.py``
    sets only from an ``except`` branch) always outranks an unrecorded
    stage. This is what keeps a genuinely crashed night alerting even
    though its stage fold looks identical to an in-flight night's.
    """

    settlement: Settlement
    detail: str
    in_flight: bool
    floor: AcceptanceFloor


class ReflectionWatchdog:
    """Monitors the nightly reflection runs and performs self-healing or alerting."""

    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = db_path or default_db_path()

    def check_last_run(self, date_str: str, sla_seconds: float = 1800.0) -> tuple[bool, str]:
        """(a) Verify last night's reflection_runs row exists, completed, and finished within SLA."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            # Query runs started on the given date
            rows = conn.execute(
                "SELECT * FROM reflection_runs WHERE started_at LIKE ? ORDER BY started_at DESC",
                (f"{date_str}%",),
            ).fetchall()
            if not rows:
                return False, f"No reflection run found starting on {date_str}"

            last_run = rows[0]
            status = last_run["status"]
            if status != "completed":
                return False, f"Last reflection run status is '{status}' (expected 'completed')"

            start = parse_iso(last_run["started_at"])
            end = parse_iso(last_run["finished_at"])
            if not start or not end:
                return False, "Run timestamps are invalid or missing"

            duration = (end - start).total_seconds()
            if duration > sla_seconds:
                return False, f"Run took {duration:.1f}s, exceeding SLA of {sla_seconds}s"

            return True, ""
        except Exception as exc:
            return False, f"Database query failed: {exc}"
        finally:
            conn.close()

    def check_context_budget_caps(
        self, date_str: str, max_bytes: int = 10485760
    ) -> tuple[bool, str]:
        """(b) Verify evidence and digest files exist and respect maximum size budget caps."""
        # An explicit OMNIAGENTOS_REFLECTION_DIR always wins (tests, drills,
        # relocated runtimes); otherwise resolve the dated dir under the repo.
        override = os.environ.get("OMNIAGENTOS_REFLECTION_DIR")
        ref_dir = Path(override) if override else REPO_ROOT / "var" / "reflection" / date_str
        evidence_path = ref_dir / "evidence.json"
        digest_path = ref_dir / "digest.md"

        if not evidence_path.exists():
            return False, f"Evidence file does not exist: {evidence_path}"
        if not digest_path.exists():
            return False, f"Digest file does not exist: {digest_path}"

        evidence_size = evidence_path.stat().st_size
        digest_size = digest_path.stat().st_size

        if evidence_size > max_bytes:
            return (
                False,
                f"Evidence file size ({evidence_size} bytes) exceeds budget cap of {max_bytes} bytes",
            )
        if digest_size > max_bytes:
            return (
                False,
                f"Digest file size ({digest_size} bytes) exceeds budget cap of {max_bytes} bytes",
            )

        return True, ""

    def briefing_path(self, date_str: str) -> Path:
        """Where last night's briefing IS — resolved exactly as the writer resolves it.

        ``reflection.report`` writes to ``default_vault_dir()`` +
        ``reflection_briefing_relpath()``; anchoring the reader on
        ``REPO_ROOT / "vault"`` instead was the split brain: the launchd jobs
        run with ``OMNIAGENTOS_VAULT_DIR=$OMNIAGENTOS_VAR_DIR/vault`` (see
        scripts/launch-env.sh), so a briefing the loop had genuinely written to
        ``var/runtime/vault/briefings/`` was scored a failed night and an
        ALERT was filed into a second, unread ``<repo>/vault``. Both halves of
        the path — root and relpath — now come from the one shared source.
        """
        return Path(default_vault_dir()) / reflection_briefing_relpath(date_str)

    def check_briefing_written(self, date_str: str) -> tuple[bool, str]:
        """(c) Verify the briefing file was written to vault AND is non-empty.

        Uses the one shared classifier: a zero-byte briefing is not a briefing.
        ``required=True`` — the loop owed us this file, so its absence is a
        genuine failure, not merely an ungateable result.
        """
        briefing_path = self.briefing_path(date_str)
        settlement = classify_settlement(briefing_path, required=True)
        if settlement is Settlement.OK:
            return True, ""
        if not briefing_path.exists():
            return False, f"Briefing file does not exist: {briefing_path}"
        return False, f"Briefing file is empty (0 bytes): {briefing_path}"

    def classify_night(self, date_str: str, floor: float = 1.0) -> NightVerdict:
        """Produce the night's three-valued judgment in exactly one place.

        ``check_run_settlement`` and ``acceptance_floor_for`` are thin
        wrappers around this method (kept alive with their original public
        signatures so existing symbols/tests can still call them). This is
        the ONE query, and the ONE place the run-status override
        (``_RUN_STATUS_PERMITS_UNGATEABLE`` / ``_RUN_STATUS_TERMINAL``) is
        applied, so the two wrappers can never drift out of agreement.
        """
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT * FROM reflection_runs WHERE started_at LIKE ? "
                "ORDER BY started_at DESC LIMIT 1",
                (f"{date_str}%",),
            ).fetchone()
        except Exception as exc:
            return NightVerdict(
                Settlement.FAILED,
                f"Database query failed: {exc}",
                False,
                acceptance_floor([], floor),
            )
        finally:
            conn.close()

        if row is None:
            return NightVerdict(
                Settlement.UNGATEABLE,
                f"No reflection run recorded for {date_str}",
                False,
                acceptance_floor([], floor),
            )

        keys = row.keys()
        stages = {
            stage: (row[f"{stage}_status"] if f"{stage}_status" in keys else None)
            for stage in STAGES
        }
        stage_settlement = run_settlement(stages.values())
        stage_detail = ", ".join(
            f"{stage}={value or 'unrecorded'}" for stage, value in stages.items()
        )
        status = str(row["status"] or "")
        settlement = _settle_with_status_override(stage_settlement, status)

        if settlement is Settlement.FAILED and stage_settlement is Settlement.UNGATEABLE:
            detail = f"run {row['id']} status='{status}' overrides an ungateable stage fold ({stage_detail})"
        elif settlement is Settlement.OK:
            detail = ""
        else:
            detail = f"run {row['id']} settled '{settlement.value}' ({stage_detail})"

        in_flight = status not in _RUN_STATUS_TERMINAL
        floor_value = acceptance_floor(stages.values(), floor)
        return NightVerdict(settlement, detail, in_flight, floor_value)

    def check_run_settlement(self, date_str: str) -> tuple[Settlement, str]:
        """Fold the recorded per-stage settlements of the night's run.

        Returns the explicit three-valued outcome so callers can EXCLUDE an
        ungateable night from the acceptance floor instead of scoring it
        against the loop. Thin wrapper over ``classify_night``.
        """
        verdict = self.classify_night(date_str)
        return verdict.settlement, verdict.detail

    def acceptance_floor_for(self, date_str: str, floor: float = 1.0) -> AcceptanceFloor:
        """Acceptance floor over the night's stage settlements.

        Ungateable stages are removed from numerator AND denominator by
        construction, so they can never be silently counted as unfavourable.
        Thin wrapper over ``classify_night``.
        """
        return self.classify_night(date_str, floor).floor

    def check_proposals_valid(self, date_str: str) -> tuple[bool, str]:
        """(d) Verify database and local file proposals are schema-valid."""
        ref_dir = REPO_ROOT / "var" / "reflection" / date_str
        proposals_file = ref_dir / "proposals.json"

        # Check local JSON file first if it exists
        if proposals_file.exists():
            try:
                import json

                with open(proposals_file, encoding="utf-8") as f:
                    proposals = json.load(f)
                if not isinstance(proposals, list):
                    return False, f"Proposals file does not contain a list: {proposals_file}"
                for p in proposals:
                    for k in ["id", "kind", "target", "current", "proposed"]:
                        if k not in p:
                            return False, f"Proposal missing schema key '{k}': {p}"
            except Exception as exc:
                return False, f"Failed to parse proposals file: {exc}"

        # Check database proposals table if it exists
        conn = sqlite3.connect(self.db_path)
        try:
            # Check if reflection_proposals table exists
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='reflection_proposals'"
            )
            if cursor.fetchone():
                rows = conn.execute(
                    "SELECT * FROM reflection_proposals WHERE created_at LIKE ?",
                    (f"{date_str}%",),
                ).fetchall()
                # Validate retrieved rows
                for r in rows:
                    # Rows are typically Row objects or tuples
                    try:
                        # Simple checks if keys/fields exist
                        if "id" not in r.keys() or "kind" not in r.keys():
                            return False, "Database proposals table has invalid schema"
                    except AttributeError:
                        pass
        except Exception as exc:
            return False, f"Database proposals validation failed: {exc}"
        finally:
            conn.close()

        return True, ""

    def check_git_hard_stops(self) -> tuple[bool, str]:
        """(e) Verify that no reflection commit touched any hard-stop file."""
        try:
            # git log --since="24 hours ago" --author="reflection-loop" --name-only --oneline
            res = subprocess.run(
                [
                    "git",
                    "log",
                    "--since=24 hours ago",
                    "--author=reflection-loop",
                    "--name-only",
                    "--oneline",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if res.returncode != 0:
                # If git command failed (e.g. not in a git repo, which might be true in tests), skip
                return True, ""

            lines = res.stdout.splitlines()
            touched_files = set()
            for line in lines:
                line = line.strip()
                if not line or " " in line:  # Skip oneline log prefix
                    continue
                touched_files.add(line)

            for fp in touched_files:
                if is_hard_stop(fp):
                    return (
                        False,
                        f"hard-stop violation: touched forbidden file '{fp}' in recent commits",
                    )

            return True, ""
        except Exception as exc:
            # Fail closed on actual execution failure of the git command if it exists but failed
            return False, f"Git log check failed to execute: {exc}"

    def run_all_checks(
        self, date_str: str, *, include_run_scoped: bool = True
    ) -> list[tuple[str, bool, str]]:
        """Run all verification checks for a given date.

        ``include_run_scoped=False`` excludes the four checks that read the
        night's OWN run row -- (a) SLA & Run Completion, (b) Context Budget
        Caps, (c) Morning Briefing File, (d) Proposals Schema. Callers pass
        this when the night is UNGATEABLE (never ran / still in flight), so
        an unrecorded or in-flight run stops being scored as a failure.
        (e) Git Hard-Stop Boundaries is run-INDEPENDENT (it greps recent git
        log, not the night's run row) and ALWAYS runs, so an ungateable
        night can never also suppress the hard-stop boundary check.
        """
        results: list[tuple[str, bool, str]] = []

        if include_run_scoped:
            config = load_reflection_config()
            limits = config.get("limits") or {}
            max_bytes = limits.get("max_context_size_bytes", 10485760)

            # (a) Check last run duration and completion
            ok, err = self.check_last_run(date_str)
            results.append(("SLA & Run Completion", ok, err))

            # (b) Check context budget caps
            ok, err = self.check_context_budget_caps(date_str, max_bytes)
            results.append(("Context Budget Caps", ok, err))

            # (c) Check morning briefing written
            ok, err = self.check_briefing_written(date_str)
            results.append(("Morning Briefing File", ok, err))

            # (d) Check proposals schema validation
            ok, err = self.check_proposals_valid(date_str)
            results.append(("Proposals Schema", ok, err))

        # (e) Check git hard stops -- run-independent, always runs.
        ok, err = self.check_git_hard_stops()
        results.append(("Git Hard-Stop Boundaries", ok, err))

        return results

    def file_board_alert(self, reason: str) -> bool:
        """File (or refresh) an urgent board card for the reflection loop being broken.

        The loop being broken is a CONDITION (one stuck/failing run), not a
        discrete event: the watchdog runs on a schedule, so an unconditional
        insert here files one new "urgent" card per invocation for as long as
        the condition persists — 1,709 identical open rows from one writer,
        measured live (see the admitted proposal this fixes,
        sha256:7244d0761fed...). Idempotent per condition instead: look up the
        one OPEN reflection card already reporting this exact condition (the
        dedupe key is ``_condition_key(reason)``, a hash of the FULL reason —
        NOT the truncated display title, and NOT discipline alone, so a
        genuinely different reason still gets its own card even when its
        title happens to truncate the same way) and refresh its occurrence
        count / last-seen timestamp in place rather than minting a duplicate.
        Insert only when no such card is open.

        ``board_tasks`` has no DB-level uniqueness to lean on, so the
        lookup-then-insert below is reconciled at the application level
        immediately after an insert: two overlapping invocations can both
        observe "no open card yet" and both insert before either commits,
        which would otherwise re-create the exact duplicate-minting bug this
        method exists to fix.

        Returns True when the alert was filed or refreshed, False when the
        write could not be confirmed (e.g. the existing card vanished
        between lookup and update) — callers must not report success on a
        bare call that ``update_board_task`` reported as a miss.
        """
        try:
            from omniagentos.collab.contracts import BoardTask, BoardTaskStatus
            from omniagentos.collab.store import CollabStore

            collab_store = CollabStore(self.db_path)
            condition_key = _condition_key(reason)
            title = f"reflection loop broken: {reason}"
            if len(title) > 120:
                title = title[:117] + "..."
            now = utc_now_iso()

            def _open_cards_for_condition() -> list[dict]:
                return [
                    task
                    for task in collab_store.list_board_tasks(status=BoardTaskStatus.OPEN.value)
                    if task.get("discipline") == "reflection"
                    and _extract_condition_key(str(task.get("description") or "")) == condition_key
                ]

            existing = next(iter(_open_cards_for_condition()), None)

            if existing is not None:
                info = _extract_occurrence_info(str(existing.get("description") or ""))
                if info is not None:
                    occurrences, first_seen = info
                else:
                    # A pre-dedupe card (no occurrence line yet): start counting
                    # from here rather than refusing to refresh it.
                    occurrences, first_seen = 1, str(existing.get("created_at") or now)
                updated = collab_store.update_board_task(
                    existing["id"],
                    {
                        "description": _alert_description(
                            reason, occurrences + 1, first_seen, now, condition_key
                        )
                    },
                    expect_status=BoardTaskStatus.OPEN.value,
                )
                if not updated:
                    print(
                        f"Failed to refresh alert card (ID: {existing['id']}): it was no "
                        "longer OPEN (deleted or transitioned concurrently). The condition "
                        "is still live, so a later invocation will self-heal.",
                        file=sys.stderr,
                    )
                    return False
                print(
                    f"Watchdog refreshed existing alert card "
                    f"(ID: {existing['id']}, occurrence #{occurrences + 1})"
                )
                return True

            board_task = BoardTask(
                title=title,
                description=_alert_description(reason, 1, now, now, condition_key),
                status=BoardTaskStatus.OPEN,
                priority="urgent",
                discipline="reflection",
            )
            collab_store.create_board_task(board_task)

            # Collision-safe re-read (see the docstring): if an overlapping
            # invocation raced us and also inserted a card for this same
            # condition, cancel every row but a deterministic winner (oldest
            # created_at, ties broken by id) so both invocations converge on
            # the SAME survivor instead of leaving two open cards.
            duplicates = _open_cards_for_condition()
            if len(duplicates) > 1:
                duplicates.sort(
                    key=lambda task: (str(task.get("created_at") or ""), str(task["id"]))
                )
                winner = duplicates[0]
                for loser in duplicates[1:]:
                    collab_store.update_board_task(
                        loser["id"],
                        {"status": BoardTaskStatus.CANCELLED.value},
                        expect_status=BoardTaskStatus.OPEN.value,
                    )
                if winner["id"] != board_task.id:
                    print(
                        "Watchdog lost a concurrent-insert race for this condition; "
                        f"folded its own card (ID: {board_task.id}) into the winner "
                        f"(ID: {winner['id']})"
                    )
                    return True

            print(f"Watchdog created an urgent alert card on the board (ID: {board_task.id})")
            return True
        except Exception as exc:
            print(f"Failed to file board alert card: {exc}", file=sys.stderr)
            return False

    def write_alert_briefing(
        self, date_str: str, reason: str, check_results: list[tuple[str, bool, str]]
    ) -> None:
        """Write a detailed failure report to vault/briefings/reflection-ALERT-<date>.md.

        Same vault as ``briefing_path()`` (and as the writer), so the alert
        lands beside the briefing whose absence it is reporting instead of in a
        second vault nobody reads.
        """
        alert_path = Path(default_vault_dir()) / reflection_alert_relpath(date_str)
        alert_path.parent.mkdir(parents=True, exist_ok=True)

        report_lines = [
            f"# REFLECTION LOOP CRITICAL ALERT - {date_str}",
            "",
            "The reflection loop watchdog has identified critical failures in last night's run,",
            "and the automatic self-healing re-run attempt was either exhausted or failed.",
            "",
            "## Failure Reason",
            f"**{reason}**",
            "",
            "## Watchdog Inspection Results",
            "",
            "| Check Name | Status | Message / Error |",
            "| :--- | :---: | :--- |",
        ]

        for name, ok, err in check_results:
            status_icon = "✅ PASS" if ok else "❌ FAIL"
            report_lines.append(f"| {name} | {status_icon} | {err or 'No errors'} |")

        report_lines.extend(
            [
                "",
                "## Actions Taken",
                "1. Triggered exactly one automatic re-run attempt.",
                "2. Logged failures and generated this alert briefing.",
                "3. Dispatched an urgent alert card to the operations board.",
                "",
                "## Recommended Next Steps",
                "- Verify the sqlite database state (`var/omniagentos.db`).",
                "- Inspect the log outputs from the nightly launchd daemon.",
                "- Verify credentials, API tokens, and model configurations.",
            ]
        )

        try:
            alert_path.write_text("\n".join(report_lines), encoding="utf-8")
            print(f"Watchdog wrote critical alert briefing to: {alert_path}")
        except Exception as exc:
            print(f"Failed to write alert briefing: {exc}", file=sys.stderr)

    def get_run_count(self, date_str: str) -> int:
        """Count the number of reflection runs executed today."""
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM reflection_runs WHERE started_at LIKE ?",
                (f"{date_str}%",),
            ).fetchone()
            return row[0] if row else 0
        except Exception:
            return 0
        finally:
            conn.close()


def _print_verdict(date_str: str, verdict: NightVerdict) -> None:
    floor = verdict.floor
    print(
        f"Watchdog: night settlement={verdict.settlement.value.upper()} for {date_str} "
        f"({verdict.detail or 'all recorded stages ok'}); "
        f"acceptance floor ok={floor.ok} failed={floor.failed} "
        f"ungateable={floor.ungateable} gateable={floor.gateable} meets={floor.meets}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Reflection Loop Watchdog")
    parser.add_argument(
        "--date",
        type=str,
        default=None,
        help="Target date for checking (YYYY-MM-DD). Defaults to today's date.",
    )
    args = parser.parse_args()

    date_str = args.date or datetime.now(UTC).strftime("%Y-%m-%d")
    watchdog = ReflectionWatchdog()

    print(f"Watchdog starting inspection for date: {date_str}")

    # Settle the night's three-valued outcome FIRST. An UNGATEABLE night
    # (never ran / still in flight, and NOT overridden by a positive
    # run-status failure assertion -- see _RUN_STATUS_PERMITS_UNGATEABLE)
    # excludes the four run-scoped checks from scoring; the run-INDEPENDENT
    # git hard-stop check always runs regardless.
    verdict = watchdog.classify_night(date_str)
    gateable = verdict.settlement is not Settlement.UNGATEABLE
    _print_verdict(date_str, verdict)

    results = watchdog.run_all_checks(date_str, include_run_scoped=gateable)
    failed_checks = [name for name, ok, _ in results if not ok]

    if failed_checks:
        first_failure_reason = next(err for _, ok, err in results if not ok)
        print(f"Watchdog: Checks failed! {', '.join(failed_checks)}")
        print(f"First failure: {first_failure_reason}")

        # Check run count to see if we already tried a re-run. Never
        # re-enter the loop while a night is still IN FLIGHT -- run_
        # reflection_loop carries no lock (runner.py), so re-entering a
        # live run is a concurrency hazard, not a recovery attempt.
        run_count = watchdog.get_run_count(date_str)
        if run_count < 2 and not verdict.in_flight:
            print(
                f"Attempting exactly ONE observe-only auto re-run of the reflection loop (run count is {run_count})..."
            )
            try:
                # A watchdog is a recovery path: it may re-run the loop to
                # restore health signals, but it must never be the
                # invocation that applies config writes or git commits —
                # that belongs to the validated nightly path under its own
                # observe_only configuration.
                run_reflection_loop(observe_only=True)
                print(
                    "Auto re-run finished. Re-classifying and re-running watchdog verification checks..."
                )
                verdict = watchdog.classify_night(date_str)
                gateable = verdict.settlement is not Settlement.UNGATEABLE
                _print_verdict(date_str, verdict)
                new_results = watchdog.run_all_checks(date_str, include_run_scoped=gateable)
                new_failed = [name for name, ok, _ in new_results if not ok]
                results = new_results
                failed_checks = new_failed
                if new_failed:
                    first_failure_reason = next(err for _, ok, err in new_results if not ok)
                    print(
                        f"Watchdog: Re-run failed to recover. Remaining failing checks: {', '.join(new_failed)}"
                    )
                elif gateable:
                    print("Watchdog: Auto re-run recovered the loop! All checks pass now.")
                    sys.exit(0)
            except Exception as exc:
                first_failure_reason = f"Auto re-run execution threw error: {exc}"
                print(first_failure_reason, file=sys.stderr)

        if failed_checks:
            print("Watchdog: Recovery attempt failed or exhausted. Raising alerts...")
            watchdog.write_alert_briefing(date_str, first_failure_reason, results)
            watchdog.file_board_alert(first_failure_reason)
            sys.exit(1)

    # UNGATEABLE is tested BEFORE the healthy branch -- and it must NEVER
    # fall through to it. Converting "could not grade" into "passed" is the
    # favourable-absence defect inverted.
    if not gateable:
        print(
            f"Watchdog: night settlement is UNGATEABLE ({verdict.detail}) -- excluded from "
            "the acceptance floor. Skipping the CRITICAL alert; exiting 2 (could not run)."
        )
        sys.exit(2)

    print("Watchdog: All checks passed. Reflection loop healthy.")
    sys.exit(0)


if __name__ == "__main__":
    main()
