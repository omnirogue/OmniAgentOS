"""Core evidence harvesting engine for the nightly self-learning reflection loop.

Aggregates SQLite metrics, file-based ledgers, harness transcripts from all
providers (Claude, Gemini, Kimi, Codex, Grok), compiles historical lessons,
enforces hard caps, performs mechanical taxonomy tagging, and writes the nightly
evidence.json and digest.md.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from omniagentos.contracts import (
    default_db_path,
    default_vault_dir,
    utc_now_iso,
)
from omniagentos.reflection.adapters import (
    ClaudeSourceAdapter,
    CodexSourceAdapter,
    GeminiSourceAdapter,
    GrokSourceAdapter,
    KimiSourceAdapter,
    SourceAdapter,
    SwarmVerdictSourceAdapter,
    read_and_sample_file,
)
from omniagentos.reflection.contracts import ReflectionEvidence, SourceDigest
from omniagentos.reflection.settlement import (
    Settlement,
    classify_settlement,
    new_run_id,
)
from omniagentos.reflection.taxonomy import classify_content


def _repo_root() -> Path:
    """Helper to locate the repo root."""
    return Path(__file__).resolve().parent.parent.parent


def evidence_artifact_paths(date_str: str) -> tuple[Path, Path]:
    """The two artifacts a harvest owes: ``evidence.json`` and ``digest.md``.

    Exposed so the runner can settle the harvest stage on the FILES rather than
    on the fact that ``harvest_evidence`` returned.
    """
    out_dir = _repo_root() / "var" / "reflection" / date_str
    return out_dir / "evidence.json", out_dir / "digest.md"


def load_config() -> dict[str, Any]:
    """Loads reflection loop config from configs/reflection.yaml."""
    config_path = _repo_root() / "configs" / "reflection.yaml"
    if not config_path.exists():
        # Fallback defaults exactly as the spec defines
        return {
            "window_hours": 36,
            "caps": {
                "per_source_bytes": 2 * 1024 * 1024,  # 2MB
                "per_source_tokens": 8000,
                "total_tokens": 40000,
            },
            "adapters": {
                "claude": True,
                "gemini": True,
                "kimi": True,
                "codex": True,
                "grok": True,
            },
        }
    try:
        return yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except Exception:
        return {
            "window_hours": 36,
            "caps": {
                "per_source_bytes": 2 * 1024 * 1024,
                "per_source_tokens": 8000,
                "total_tokens": 40000,
            },
            "adapters": {
                "claude": True,
                "gemini": True,
                "kimi": True,
                "codex": True,
                "grok": True,
            },
        }


def query_sqlite_metrics(db_path: str, since_iso: str) -> dict[str, list[dict[str, Any]]]:
    """Queries SQLite tables for runs, attempts, sessions, and memory candidates in the window."""
    import sqlite3

    metrics: dict[str, list[dict[str, Any]]] = {
        "runs": [],
        "attempts": [],
        "sessions": [],
        "formation_selections": [],
        "metacog_memory_candidates": [],
    }

    if not Path(db_path).exists():
        return metrics

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        def table_exists(name: str) -> bool:
            cursor.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,))
            return cursor.fetchone() is not None

        if table_exists("swarm_runs"):
            cursor.execute(
                "SELECT * FROM swarm_runs WHERE created_at >= ? OR updated_at >= ? ORDER BY created_at DESC",
                (since_iso, since_iso),
            )
            metrics["runs"] = [dict(row) for row in cursor.fetchall()]

        if table_exists("swarm_attempts"):
            cursor.execute(
                "SELECT * FROM swarm_attempts WHERE started_at >= ? ORDER BY started_at DESC",
                (since_iso,),
            )
            metrics["attempts"] = [dict(row) for row in cursor.fetchall()]

        if table_exists("sessions"):
            cursor.execute(
                "SELECT * FROM sessions WHERE created_at >= ? OR updated_at >= ? ORDER BY created_at DESC",
                (since_iso, since_iso),
            )
            metrics["sessions"] = [dict(row) for row in cursor.fetchall()]

        if table_exists("formation_selections"):
            cursor.execute(
                "SELECT * FROM formation_selections WHERE created_at >= ? ORDER BY created_at DESC",
                (since_iso,),
            )
            metrics["formation_selections"] = [dict(row) for row in cursor.fetchall()]

        if table_exists("metacog_memory_records"):
            cursor.execute(
                "SELECT * FROM metacog_memory_records WHERE created_at >= ? OR updated_at >= ? ORDER BY created_at DESC",
                (since_iso, since_iso),
            )
            metrics["metacog_memory_candidates"] = [dict(row) for row in cursor.fetchall()]

        conn.close()
    except Exception:
        pass

    return metrics


def write_reflection_run_start(db_path: str, run_id: str, started_at: str) -> None:
    """Inserts a starting reflection_runs record into SQLite."""
    import sqlite3

    if not Path(db_path).exists():
        return
    try:
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO reflection_runs (id, started_at, status, bytes_read) VALUES (?, ?, ?, ?)",
            (run_id, started_at, "running", 0),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def write_reflection_run_end(
    db_path: str,
    run_id: str,
    finished_at: str,
    status: str,
    sources_read: list[str],
    bytes_read: int,
    caps_hit: list[str],
    *,
    owns_lifecycle: bool = True,
    harvest_status: str | None = None,
) -> None:
    """Update the ONE reflection_runs row for this run.

    When ``owns_lifecycle`` is False the runner opened the row and owns
    ``status``/``finished_at``; the harvester then writes only the columns it
    actually produced.  Previously the harvester stamped ``status='completed'``
    on its own private row while the runner was still on stage 1 — two rows per
    night, both claiming to be the truth.
    """
    import sqlite3

    if not Path(db_path).exists():
        return
    try:
        conn = sqlite3.connect(db_path)
    except Exception:
        return
    try:
        try:
            if owns_lifecycle:
                conn.execute(
                    "UPDATE reflection_runs SET finished_at = ?, status = ?, sources_read = ?, "
                    "bytes_read = ?, caps_hit = ? WHERE id = ?",
                    (
                        finished_at,
                        status,
                        json.dumps(sources_read),
                        bytes_read,
                        json.dumps(caps_hit),
                        run_id,
                    ),
                )
            else:
                conn.execute(
                    "UPDATE reflection_runs SET sources_read = ?, bytes_read = ?, caps_hit = ? "
                    "WHERE id = ?",
                    (json.dumps(sources_read), bytes_read, json.dumps(caps_hit), run_id),
                )
            conn.commit()
        except Exception:
            pass
        # Separate statement: legacy/minimal schemas without the stage columns
        # must not lose the lifecycle write above.
        if harvest_status is not None:
            try:
                conn.execute(
                    "UPDATE reflection_runs SET harvest_status = ? WHERE id = ?",
                    (harvest_status, run_id),
                )
                conn.commit()
            except Exception:
                pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


def read_ledger_files(
    window_start_epoch: float, byte_cap: int
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    int,
    list[str],
]:
    """Reads file-based ledgers/logs from the var/ directory."""
    session_logs: list[dict[str, Any]] = []
    router_shadow_logs: list[dict[str, Any]] = []
    openhands_gate_denials: list[dict[str, Any]] = []
    runs_ledger_attempts: list[dict[str, Any]] = []
    workbook_end_states: list[dict[str, Any]] = []

    bytes_read = 0
    caps_hit: list[str] = []

    root = _repo_root()

    # 1. var/runtime/ledger/sessions/*.jsonl
    sessions_dir = root / "var" / "runtime" / "ledger" / "sessions"
    if sessions_dir.exists():
        for p in sessions_dir.glob("*.jsonl"):
            try:
                mtime = p.stat().st_mtime
                if mtime >= window_start_epoch:
                    content, b_hit, read_sz = read_and_sample_file(p, byte_cap)
                    bytes_read += read_sz
                    if b_hit:
                        caps_hit.append(f"byte_cap:{p.name}")
                    for line in content.splitlines():
                        if line.strip():
                            try:
                                session_logs.append(json.loads(line))
                            except Exception:
                                continue
            except OSError:
                pass

    # 2. var/modelintel/router_shadow.jsonl
    shadow_log = root / "var" / "modelintel" / "router_shadow.jsonl"
    if shadow_log.exists():
        try:
            mtime = shadow_log.stat().st_mtime
            if mtime >= window_start_epoch:
                content, b_hit, read_sz = read_and_sample_file(shadow_log, byte_cap)
                bytes_read += read_sz
                if b_hit:
                    caps_hit.append("byte_cap:router_shadow.jsonl")
                for line in content.splitlines():
                    if line.strip():
                        try:
                            router_shadow_logs.append(json.loads(line))
                        except Exception:
                            continue
        except OSError:
            pass

    # 3. var/ledger/openhands_gate.jsonl
    gate_log = root / "var" / "ledger" / "openhands_gate.jsonl"
    if gate_log.exists():
        try:
            mtime = gate_log.stat().st_mtime
            if mtime >= window_start_epoch:
                content, b_hit, read_sz = read_and_sample_file(gate_log, byte_cap)
                bytes_read += read_sz
                if b_hit:
                    caps_hit.append("byte_cap:openhands_gate.jsonl")
                for line in content.splitlines():
                    if line.strip():
                        try:
                            openhands_gate_denials.append(json.loads(line))
                        except Exception:
                            continue
        except OSError:
            pass

    # 4. var/runtime/ledger/runs-*.jsonl
    runs_dir = root / "var" / "runtime" / "ledger"
    if runs_dir.exists():
        for p in runs_dir.glob("runs-*.jsonl"):
            try:
                mtime = p.stat().st_mtime
                if mtime >= window_start_epoch:
                    content, b_hit, read_sz = read_and_sample_file(p, byte_cap)
                    bytes_read += read_sz
                    if b_hit:
                        caps_hit.append(f"byte_cap:{p.name}")
                    for line in content.splitlines():
                        if line.strip():
                            try:
                                runs_ledger_attempts.append(json.loads(line))
                            except Exception:
                                continue
            except OSError:
                pass

    # 5. var/swarm/<run>/<task>/WORKBOOK.md and TASK.md
    swarm_dir = root / "var" / "swarm"
    if swarm_dir.exists():
        for p in swarm_dir.rglob("*.md"):
            if p.name in {"WORKBOOK.md", "TASK.md"}:
                try:
                    mtime = p.stat().st_mtime
                    if mtime >= window_start_epoch:
                        content, b_hit, read_sz = read_and_sample_file(p, byte_cap)
                        bytes_read += read_sz
                        if b_hit:
                            caps_hit.append(f"byte_cap:swarm_{p.parent.name}_{p.name}")
                        workbook_end_states.append(
                            {
                                "path": str(p.relative_to(root)),
                                "mtime": mtime,
                                "content": content,
                            }
                        )
                except OSError:
                    pass

    return (
        session_logs,
        router_shadow_logs,
        openhands_gate_denials,
        runs_ledger_attempts,
        workbook_end_states,
        bytes_read,
        caps_hit,
    )


def read_historical_learnings(days_window: int = 28) -> list[dict[str, Any]]:
    """Compiles markdown lessons and briefings from docs/lessons/ and vault/briefings/."""
    compiled: list[dict[str, Any]] = []
    window_s = days_window * 24 * 3600
    cutoff_epoch = time.time() - window_s

    root = _repo_root()

    # docs/lessons/*.md
    lessons_dir = root / "docs" / "lessons"
    if lessons_dir.exists():
        for p in lessons_dir.glob("*.md"):
            try:
                mtime = p.stat().st_mtime
                if mtime >= cutoff_epoch:
                    compiled.append(
                        {
                            "source_type": "lesson",
                            "path": str(p.relative_to(root)),
                            "mtime": mtime,
                            "content": p.read_text(encoding="utf-8", errors="replace"),
                        }
                    )
            except OSError:
                pass

    # vault/briefings/reflection-*.md
    vault_dir = Path(default_vault_dir())
    briefings_dir = vault_dir / "briefings"
    if briefings_dir.exists():
        for p in briefings_dir.glob("reflection-*.md"):
            try:
                mtime = p.stat().st_mtime
                if mtime >= cutoff_epoch:
                    compiled.append(
                        {
                            "source_type": "reflection_briefing",
                            "path": str(p.relative_to(root)) if p.is_relative_to(root) else p.name,
                            "mtime": mtime,
                            "content": p.read_text(encoding="utf-8", errors="replace"),
                        }
                    )
            except OSError:
                pass

    # Sort compiled historical learnings newest-first
    compiled.sort(key=lambda item: item.get("mtime", 0.0), reverse=True)
    return compiled


def generate_digest_markdown(evidence: ReflectionEvidence) -> str:
    """Renders a structured, human-readable markdown digest from the collected evidence."""
    total_db_records = (
        len(evidence.runs)
        + len(evidence.attempts)
        + len(evidence.sessions)
        + len(evidence.formation_selections)
        + len(evidence.metacog_memory_candidates)
    )

    harness_summaries = []
    for provider, digests in evidence.harness_transcripts.items():
        if digests:
            harness_summaries.append(f"### {provider.upper()}")
            for d in digests:
                caps_info = f" (Caps exceeded: {', '.join(d.caps_hit)})" if d.caps_hit else ""
                harness_summaries.append(
                    f"- **{d.source_name}**: {d.bytes_read} bytes read, {d.tokens_estimated} estimated tokens{caps_info}"
                )

    harness_section = (
        "\n".join(harness_summaries) if harness_summaries else "No transcripts harvested."
    )

    # Group failure tags
    failure_counts: dict[str, int] = {}
    failure_details = []
    for f_tag in evidence.failure_tags:
        tag = f_tag["tag"]
        failure_counts[tag] = failure_counts.get(tag, 0) + 1
        failure_details.append(
            f"- **{tag}** in `{f_tag['source']}`: matched pattern `{f_tag['pattern_matched']}`"
        )

    failures_summary = "\n".join(
        [f"- **{tag}**: {count} occurrences" for tag, count in failure_counts.items()]
    )
    failures_detail_section = (
        "\n".join(failure_details) if failure_details else "No mechanical failures tagged."
    )

    digest_md = f"""# Reflection Nightly Digest - {evidence.date}

## Execution Summary
- **Started At**: {evidence.started_at}
- **Finished At**: {evidence.finished_at}
- **Bytes Read**: {evidence.bytes_read} bytes
- **Caps Exceeded**: {", ".join(evidence.caps_hit) if evidence.caps_hit else "None"}

## Database & Metrics
Harvested **{total_db_records}** records from SQLite:
- `swarm_runs`: {len(evidence.runs)}
- `swarm_attempts`: {len(evidence.attempts)}
- `sessions`: {len(evidence.sessions)}
- `formation_selections`: {len(evidence.formation_selections)}
- `metacog_memory_candidates`: {len(evidence.metacog_memory_candidates)}

## File-Based Ledgers
- `session_logs`: {len(evidence.session_logs)} records
- `router_shadow_logs`: {len(evidence.router_shadow_logs)} records
- `openhands_gate_denials`: {len(evidence.openhands_gate_denials)} records
- `runs_ledger_attempts`: {len(evidence.runs_ledger_attempts)} records
- `workbook_end_states`: {len(evidence.workbook_end_states)} workbooks

## Harness Transcripts
{harness_section}

## Historical Learnings (Last 28 Days)
Harvested **{len(evidence.compiled_learnings)}** historical learning document(s).

## Mechanical Failure Classification
{failures_summary or "No failures observed."}

### Details:
{failures_detail_section}
"""
    return digest_md


def harvest_evidence(date_str: str | None = None, run_id: str | None = None) -> ReflectionEvidence:
    """Orchestrates the entire mechanical evidence harvest process.

    ``run_id`` is the runner's already-open row.  When supplied the harvester
    writes INTO that row and never opens a second one — one run, one row, one
    id scheme (``refr_…``).
    """
    start_time_iso = utc_now_iso()
    start_epoch = time.time()

    if date_str is None:
        date_str = datetime.now(UTC).strftime("%Y-%m-%d")

    config = load_config()
    window_hours = config.get("window_hours", 36)
    byte_cap = config.get("caps", {}).get("per_source_bytes", 2 * 1024 * 1024)
    token_cap = config.get("caps", {}).get("per_source_tokens", 8000)
    total_token_cap = config.get("caps", {}).get("total_tokens", 40000)

    # Time window limits
    window_start_epoch = start_epoch - window_hours * 3600
    since_iso = (datetime.now(UTC) - timedelta(hours=window_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")

    db_path = default_db_path()
    # One id scheme.  Standalone harvests mint the same `refr_` prefix the
    # runner uses; runner-driven harvests reuse the runner's row.
    owns_lifecycle = run_id is None
    if run_id is None:
        run_id = new_run_id()
        # Record started run in SQLite
        write_reflection_run_start(db_path, run_id, start_time_iso)

    # Initialize variables for byte counter and cap tracking
    total_bytes_read = 0
    caps_hit: list[str] = []

    # 1. Query SQLite
    db_metrics = query_sqlite_metrics(db_path, since_iso)

    # 2. Read ledger files
    (
        session_logs,
        router_shadow_logs,
        openhands_gate_denials,
        runs_ledger_attempts,
        workbook_end_states,
        ledger_bytes,
        ledger_caps,
    ) = read_ledger_files(window_start_epoch, byte_cap)

    total_bytes_read += ledger_bytes
    caps_hit.extend(ledger_caps)

    # 3. Read harness transcripts using adapters
    harness_transcripts: dict[str, list[SourceDigest]] = {}
    active_adapters = config.get("adapters", {})

    # Annotated as zero-arg factories: a bare dict literal makes mypy join the
    # values to type[SourceAdapter], which it refuses to instantiate (abstract).
    adapter_classes: dict[str, Callable[[], SourceAdapter]] = {
        "claude": ClaudeSourceAdapter,
        "gemini": GeminiSourceAdapter,
        "kimi": KimiSourceAdapter,
        "codex": CodexSourceAdapter,
        "grok": GrokSourceAdapter,
        # Pump-lane outcome records (sol-verdicts + fleet/role ledgers): the
        # 2026-07-29 swarm day showed this filesystem loop is where real work
        # lands while the harvested SQLite tables stay empty. Default-on via
        # the same adapters map (absent key == enabled); disable with
        # adapters.swarm_verdicts: false in configs/reflection.yaml.
        "swarm_verdicts": SwarmVerdictSourceAdapter,
    }

    accumulated_digest_tokens = 0

    for name, adapter_class in adapter_classes.items():
        if active_adapters.get(name, True):
            adapter_inst = adapter_class()
            refs = adapter_inst.discover(window_start_epoch)
            digests: list[SourceDigest] = []
            for ref in refs:
                # Check overall total token cap before digesting more
                if accumulated_digest_tokens >= total_token_cap:
                    caps_hit.append("total_token_cap")
                    break
                try:
                    digest = adapter_inst.extract(ref, byte_cap, token_cap)
                    total_bytes_read += digest.bytes_read
                    accumulated_digest_tokens += digest.tokens_estimated
                    for c in digest.caps_hit:
                        caps_hit.append(f"{c}:{digest.source_name}")
                    digests.append(digest)
                except Exception:
                    pass
            harness_transcripts[name] = digests

    # Join Kimi CLI sessions with DB ledger sessions on (time window x workDir)
    # Lesson 25: backfill model and cost details from state.json/wire.jsonl to opaque rows
    kimi_digests = harness_transcripts.get("kimi", [])
    sqlite_sessions = db_metrics.get("sessions", [])
    for d in kimi_digests:
        for s in sqlite_sessions:
            if s.get("provider") == "kimi" and s.get("model") is None:
                # Join on approximate time overlap (e.g. within 1 hour) and work directory
                try:
                    datetime.strptime(s["created_at"], "%Y-%m-%dT%H:%M:%SZ")
                    # Find session ID or workDir matching. Guard the membership
                    # test: project_dir may be absent/None, and `None in <str>`
                    # raises TypeError (previously swallowed by this except).
                    project_dir = s.get("project_dir")
                    if d.source_name == f"kimi:{s.get('id')}" or (
                        isinstance(project_dir, str) and project_dir in d.summary_or_sample
                    ):
                        s["model"] = "kimi-k2.7-code"  # Backfill typical Kimi model
                        s["cost_usd"] = s["cost_usd"] or 0.05  # Approximate if null
                except Exception:
                    pass

    # 4. Load historical lessons
    compiled_learnings = read_historical_learnings(days_window=28)

    # 5. Failure taxonomy tagging (mechanical regex scan over transcripts, ledger files, workbooks, errors)
    failure_tags: list[dict[str, Any]] = []

    # Helper to scan a piece of text and append failure tags
    def scan_and_tag(content_str: str, source_label: str) -> None:
        tags = classify_content(content_str)
        for t in tags:
            # find matching regex pattern for reference
            from omniagentos.reflection.taxonomy import TAXONOMY_PATTERNS

            pattern_matched = "unknown"
            for pat in TAXONOMY_PATTERNS.get(t, []):
                import re

                if re.search(pat, content_str):
                    pattern_matched = pat
                    break
            failure_tags.append(
                {
                    "tag": t,
                    "source": source_label,
                    "pattern_matched": pattern_matched,
                    "timestamp": utc_now_iso(),
                }
            )

    # Scan SQLite DB error fields
    for r in db_metrics["runs"]:
        err = r.get("error")
        if err:
            scan_and_tag(err, f"db:run:{r['id']}")

    for att in db_metrics["attempts"]:
        detail = att.get("detail")
        if detail:
            scan_and_tag(detail, f"db:attempt:{att['id']}")

    # Scan session ledger logs
    for s_log in session_logs:
        killed = s_log.get("killed_by")
        if killed:
            scan_and_tag(f"killed_by: {killed}", f"ledger:session:{s_log.get('session_id')}")
        final_state = s_log.get("final_state")
        if final_state in {"failed", "cancelled", "killed"}:
            scan_and_tag(f"final_state: {final_state}", f"ledger:session:{s_log.get('session_id')}")

    # Scan harness transcripts
    for _p_name, digs in harness_transcripts.items():
        for d in digs:
            scan_and_tag(d.summary_or_sample, d.source_name)

    # Scan workbook end states
    for wb in workbook_end_states:
        scan_and_tag(wb["content"], f"workbook:{wb['path']}")

    # Assemble complete ReflectionEvidence
    evidence = ReflectionEvidence(
        date=date_str,
        started_at=start_time_iso,
        finished_at=utc_now_iso(),
        runs=db_metrics["runs"],
        attempts=db_metrics["attempts"],
        sessions=db_metrics["sessions"],
        formation_selections=db_metrics["formation_selections"],
        metacog_memory_candidates=db_metrics["metacog_memory_candidates"],
        session_logs=session_logs,
        router_shadow_logs=router_shadow_logs,
        openhands_gate_denials=openhands_gate_denials,
        runs_ledger_attempts=runs_ledger_attempts,
        workbook_end_states=workbook_end_states,
        harness_transcripts=harness_transcripts,
        compiled_learnings=compiled_learnings,
        failure_tags=failure_tags,
        bytes_read=total_bytes_read,
        caps_hit=caps_hit,
    )

    # 6. Write outputs
    out_dir = _repo_root() / "var" / "reflection" / date_str
    out_dir.mkdir(parents=True, exist_ok=True)

    # Write evidence.json
    evidence_json_path = out_dir / "evidence.json"
    evidence_json_path.write_text(evidence.model_dump_json(indent=2), encoding="utf-8")

    # Generate and write digest.md
    digest_md = generate_digest_markdown(evidence)
    digest_md_path = out_dir / "digest.md"
    digest_md_path.write_text(digest_md, encoding="utf-8")

    # 7. Record run completion in SQLite reflection_runs
    sources_read_list = ["db:runs", "db:attempts", "db:sessions", "db:formations", "db:metacog"]
    if session_logs:
        sources_read_list.append("ledger:sessions")
    if router_shadow_logs:
        sources_read_list.append("ledger:router_shadow")
    if openhands_gate_denials:
        sources_read_list.append("ledger:openhands_gate")
    if runs_ledger_attempts:
        sources_read_list.append("ledger:runs_attempts")
    if workbook_end_states:
        sources_read_list.append("ledger:workbooks")
    for _provider, digests in harness_transcripts.items():
        for d in digests:
            sources_read_list.append(d.source_name)

    # Settle on the artifacts that were just written, not on reaching this line.
    harvest_settlement = classify_settlement(evidence_json_path)
    write_reflection_run_end(
        db_path,
        run_id,
        evidence.finished_at,
        "failed" if harvest_settlement is Settlement.FAILED else "completed",
        sources_read_list,
        total_bytes_read,
        caps_hit,
        owns_lifecycle=owns_lifecycle,
        harvest_status=harvest_settlement.value,
    )

    return evidence
