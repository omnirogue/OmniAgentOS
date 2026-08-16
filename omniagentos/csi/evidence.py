"""Evidence packet compilers per routine (read-only)."""

from __future__ import annotations

import re
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

from omniagentos.contracts import utc_now_iso
from omniagentos.csi.config import CsiConfig, load_csi_config
from omniagentos.csi.models import EvidencePacket

_ROOT = Path(__file__).resolve().parents[2]

# Routines that have offline evidence + planner support.
IMPLEMENTED_ROUTINES = frozenset(
    {
        "design",
        "self_learning",
        "routing",
        "skills",
        "tool_calls",
        "speed",
        "quality",
        "tech_debt",
    }
)


def _git_sha() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(_ROOT),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return (out.stdout or "").strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _list_worktrees() -> list[str]:
    try:
        out = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=str(_ROOT),
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        paths = []
        for line in (out.stdout or "").splitlines():
            if line.startswith("worktree "):
                paths.append(line.split(" ", 1)[1].strip())
        return paths
    except (OSError, subprocess.SubprocessError):
        return []


def _skills_snapshot() -> list[dict[str, Any]]:
    for skills_dir in (_ROOT / "vault" / "skills", _ROOT / "var" / "skills"):
        if not skills_dir.is_dir():
            continue
        out: list[dict[str, Any]] = []
        for path in sorted(skills_dir.rglob("SKILL.md"))[:50]:
            try:
                rel = str(path.relative_to(_ROOT))
            except ValueError:
                rel = str(path)
            out.append({"path": rel, "name": path.parent.name})
        return out
    return []


def _memory_snapshot(db_path: str | None) -> list[dict[str, Any]]:
    if not db_path or not Path(db_path).is_file():
        return []
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        rows: list[dict[str, Any]] = []
        if "memory_entries" in tables:
            for r in conn.execute(
                "SELECT id, kind, title FROM memory_entries ORDER BY created_at DESC LIMIT 30"
            ).fetchall():
                rows.append(dict(r))
        conn.close()
        return rows
    except sqlite3.Error:
        return []


def _scan_ledger_keywords(keywords: tuple[str, ...]) -> list[dict[str, Any]]:
    ledger = _ROOT / "ledger" / "sessions"
    if not ledger.is_dir():
        return []
    counts: dict[str, int] = {}
    for path in sorted(ledger.glob("*.jsonl"))[-40:]:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")[-8000:]
        except OSError:
            continue
        lower = text.lower()
        for key in keywords:
            if key.lower() in lower:
                counts[key] = counts.get(key, 0) + 1
    return [
        {"signal": k, "count": n, "id": f"ledger:{k}"}
        for k, n in sorted(counts.items(), key=lambda kv: -kv[1])
        if n >= 2
    ]


def _repeat_failures_from_ledger() -> list[dict[str, Any]]:
    return _scan_ledger_keywords(
        ("TypeError", "AssertionError", "FAILED", "timeout", "permission denied")
    )


def _dashboard_ux_signals() -> list[dict[str, Any]]:
    """Design routine: TODO/FIXME/EmptyState density under dashboard/src."""
    dash = _ROOT / "dashboard" / "src"
    if not dash.is_dir():
        return []
    todo_n = empty_n = files = 0
    samples: list[str] = []
    for path in dash.rglob("*.tsx"):
        files += 1
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        n_todo = len(re.findall(r"\b(TODO|FIXME|HACK)\b", text))
        n_empty = len(re.findall(r"EmptyState|empty state|placeholder", text, re.I))
        todo_n += n_todo
        empty_n += n_empty
        if (n_todo or n_empty) and len(samples) < 8:
            samples.append(str(path.relative_to(_ROOT)))
    signals: list[dict[str, Any]] = []
    if todo_n >= 3:
        signals.append(
            {
                "signal": "dashboard_todo_fixme",
                "count": todo_n,
                "id": "design:todo",
                "samples": samples,
            }
        )
    if empty_n >= 5:
        signals.append(
            {
                "signal": "empty_or_placeholder_ui",
                "count": empty_n,
                "id": "design:empty",
                "samples": samples,
            }
        )
    if files and not signals:
        # Still surface inventory so routine is not always empty-thin
        signals.append(
            {
                "signal": "dashboard_inventory",
                "count": files,
                "id": "design:inventory",
            }
        )
    return signals


def _formation_selection_signals(db_path: str | None) -> list[dict[str, Any]]:
    if not db_path or not Path(db_path).is_file():
        # also try calibration DB
        for p in (
            _ROOT / "var" / "calibration" / "ladder.db",
            _ROOT / "var" / "csi" / "csi.db",
        ):
            if p.is_file():
                db_path = str(p)
                break
    if not db_path or not Path(db_path).is_file():
        return []
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        out: list[dict[str, Any]] = []
        if "formation_selections" in tables:
            n = conn.execute("SELECT COUNT(*) AS c FROM formation_selections").fetchone()["c"]
            low = conn.execute(
                "SELECT COUNT(*) AS c FROM formation_selections WHERE low_confidence=1"
            ).fetchone()["c"]
            if n:
                out.append(
                    {
                        "signal": "formation_selections",
                        "count": int(n),
                        "id": "routing:formations",
                    }
                )
            if low:
                out.append(
                    {
                        "signal": "formation_low_confidence",
                        "count": int(low),
                        "id": "routing:low_conf",
                    }
                )
        if "cbm_allocations" in tables:
            n = conn.execute("SELECT COUNT(*) AS c FROM cbm_allocations").fetchone()["c"]
            if n:
                out.append({"signal": "cbm_allocations", "count": int(n), "id": "routing:cbm"})
        conn.close()
        return out
    except sqlite3.Error:
        return []


def _base_packet(
    *,
    routine: str,
    window: int,
    cfg: CsiConfig,
    traces: list[dict[str, Any]],
    metrics: dict[str, Any],
    gaps: list[str],
    thin: bool,
    memories: list[dict[str, Any]] | None = None,
    skills: list[dict[str, Any]] | None = None,
    repeats: list[dict[str, Any]] | None = None,
) -> EvidencePacket:
    return EvidencePacket(
        routine=routine,
        window_days=window,
        codebase_sha=_git_sha(),
        generated_at=utc_now_iso(),
        traces=traces,
        metrics=metrics,
        control_metrics={
            "window": "prior_equal_period_placeholder",
            "note": "control window filled once CSI outcomes exist",
        },
        open_incidents=[],
        active_worktrees=_list_worktrees(),
        frozen_surfaces=list(cfg.frozen_surfaces),
        observe_only_areas=[],
        existing_memories=memories or [],
        existing_skills=skills or [],
        repeat_failures=repeats or [],
        evidence_gaps=gaps,
        thin_evidence=thin,
    )


def compile_self_learning_packet(
    *,
    config: CsiConfig | None = None,
    db_path: str | None = None,
    window_days: int | None = None,
) -> EvidencePacket:
    return compile_packet("self_learning", config=config, db_path=db_path, window_days=window_days)


def compile_packet(
    routine_id: str,
    *,
    config: CsiConfig | None = None,
    db_path: str | None = None,
    window_days: int | None = None,
) -> EvidencePacket:
    """Compile evidence for any implemented routine."""
    cfg = config or load_csi_config()
    rid = routine_id.replace("-", "_")
    if rid not in IMPLEMENTED_ROUTINES:
        raise ValueError(f"routine_not_implemented:{rid}")
    spec = cfg.routine(rid)
    window = int(window_days or (spec.window_days if spec else 7))
    skills = _skills_snapshot()
    memories = _memory_snapshot(db_path)

    if rid == "self_learning":
        repeats = _repeat_failures_from_ledger()
        gaps: list[str] = []
        if not skills:
            gaps.append("no_skills_on_disk")
        if not memories:
            gaps.append("no_memory_rows_or_table")
        if not repeats:
            gaps.append("no_repeat_failure_signals_in_recent_ledgers")
        thin = len(repeats) == 0 and len(skills) < 3
        return _base_packet(
            routine=rid,
            window=window,
            cfg=cfg,
            traces=[{"kind": "repeat_failure", **r} for r in repeats],
            metrics={
                "skills_count": len(skills),
                "memories_count": len(memories),
                "repeat_failure_signals": len(repeats),
                "window_days": window,
            },
            gaps=gaps,
            thin=thin,
            memories=memories,
            skills=skills,
            repeats=repeats,
        )

    if rid == "design":
        signals = _dashboard_ux_signals()
        gaps = [] if signals else ["no_dashboard_ux_signals"]
        # Inventory alone is thin; actionable signals (todo/empty) are not
        thin = not any(
            s.get("signal") in {"dashboard_todo_fixme", "empty_or_placeholder_ui"} for s in signals
        )
        return _base_packet(
            routine=rid,
            window=window,
            cfg=cfg,
            traces=[{"kind": "ux_signal", **s} for s in signals],
            metrics={
                "ux_signals": len(signals),
                "window_days": window,
                **{s["signal"]: s["count"] for s in signals},
            },
            gaps=gaps,
            thin=thin,
            skills=skills,
            # reuse repeat_failures field for "actionable signals" for planner
            repeats=[
                s
                for s in signals
                if s.get("signal") in {"dashboard_todo_fixme", "empty_or_placeholder_ui"}
            ],
        )

    if rid == "routing":
        signals = _formation_selection_signals(db_path)
        gaps = [] if signals else ["no_formation_or_cbm_telemetry"]
        thin = len(signals) < 1
        return _base_packet(
            routine=rid,
            window=window,
            cfg=cfg,
            traces=[{"kind": "routing_signal", **s} for s in signals],
            metrics={"routing_signals": len(signals), "window_days": window},
            gaps=gaps,
            thin=thin,
            skills=skills,
            repeats=signals,
        )

    if rid == "skills":
        gaps = [] if skills else ["no_skills_on_disk"]
        # Many skills → opportunity to merge/retire; few → capture gaps
        thin = len(skills) == 0
        repeats = (
            [{"signal": "skill_catalog_size", "count": len(skills), "id": "skills:n"}]
            if skills
            else []
        )
        if len(skills) >= 15:
            repeats.append(
                {
                    "signal": "large_skill_catalog",
                    "count": len(skills),
                    "id": "skills:large",
                }
            )
        return _base_packet(
            routine=rid,
            window=window,
            cfg=cfg,
            traces=[{"kind": "skill", **s} for s in skills[:20]],
            metrics={"skills_count": len(skills), "window_days": window},
            gaps=gaps,
            thin=thin,
            skills=skills,
            repeats=repeats if not thin else [],
        )

    if rid == "tool_calls":
        signals = _scan_ledger_keywords(
            ("ToolplaneError", "not_allowed", "tool_error", "timeout", "rate_limit")
        )
        gaps = [] if signals else ["no_tool_error_signals"]
        thin = not signals
        return _base_packet(
            routine=rid,
            window=window,
            cfg=cfg,
            traces=[{"kind": "tool_signal", **s} for s in signals],
            metrics={"tool_signals": len(signals), "window_days": window},
            gaps=gaps,
            thin=thin,
            skills=skills,
            repeats=signals,
        )

    if rid == "speed":
        signals = _scan_ledger_keywords(
            ("slow", "latency", "TIMEOUT", "timeout", "deadline", "rate limited")
        )
        gaps = [] if signals else ["no_latency_signals"]
        thin = not signals
        return _base_packet(
            routine=rid,
            window=window,
            cfg=cfg,
            traces=[{"kind": "speed_signal", **s} for s in signals],
            metrics={"speed_signals": len(signals), "window_days": window},
            gaps=gaps,
            thin=thin,
            skills=skills,
            repeats=signals,
        )

    if rid == "quality":
        signals = _scan_ledger_keywords(
            ("AssertionError", "FAILED", "regression", "flaky", "TypeError")
        )
        gaps = [] if signals else ["no_quality_failure_signals"]
        thin = not signals
        return _base_packet(
            routine=rid,
            window=window,
            cfg=cfg,
            traces=[{"kind": "quality_signal", **s} for s in signals],
            metrics={"quality_signals": len(signals), "window_days": window},
            gaps=gaps,
            thin=thin,
            skills=skills,
            repeats=signals,
        )

    if rid == "tech_debt":
        # Simple density of TODO/FIXME in omniagentos (read-only scan, capped)
        todo = 0
        samples: list[str] = []
        pkg = _ROOT / "omniagentos"
        if pkg.is_dir():
            for path in list(pkg.rglob("*.py"))[:200]:
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                n = len(re.findall(r"\b(TODO|FIXME|XXX)\b", text))
                if n:
                    todo += n
                    if len(samples) < 6:
                        samples.append(str(path.relative_to(_ROOT)))
        signals = []
        if todo >= 5:
            signals.append(
                {
                    "signal": "code_todo_fixme",
                    "count": todo,
                    "id": "debt:todo",
                    "samples": samples,
                }
            )
        gaps = [] if signals else ["no_todo_debt_signals"]
        thin = not signals
        return _base_packet(
            routine=rid,
            window=window,
            cfg=cfg,
            traces=[{"kind": "debt_signal", **s} for s in signals],
            metrics={"todo_fixme": todo, "window_days": window},
            gaps=gaps,
            thin=thin,
            skills=skills,
            repeats=signals,
        )

    raise ValueError(f"routine_not_implemented:{rid}")


__all__ = [
    "IMPLEMENTED_ROUTINES",
    "compile_packet",
    "compile_self_learning_packet",
]
