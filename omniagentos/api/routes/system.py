"""System transparency API — the self-describing surface for OmniAgentOS.

One system: agents (who) × skills (how) × loops (improvers). Every panel here
reads an EDITABLE file on disk; this module never invents state, it reflects the
files that define the estate:

  * ``ARCHI.md`` + ``docs/architecture/system-map.mmd``  (morning archi job)
  * ``~/.claude/agents/*.md``                             (subagent definitions)
  * ``~/.claude/skills/*/SKILL.md``                       (skill library)
  * ``~/Library/LaunchAgents/com.omniagentos.*.plist``    (improver/reviewer loops)
  * ``var/improvement-log.jsonl``                         (what the loops changed)

All reads are whitelisted to exactly these locations — there is no
arbitrary-path parameter anywhere. Every GET is session-token-gated (the
prompts and configs are recon-sensitive) exactly like the reliability routes.
Mutations (``PATCH /api/system/agents/{name}``; ``POST
/api/system/spend-breaker/reset``, the Steward spend-circuit-breaker un-trip —
see api/middleware/chokepoint.py) are gated by the app-level
``require_session_token`` dependency; ``patch_agent`` additionally rails
itself to the agents directory with no path traversal.

Path resolution goes through the module-level ``_repo_root``/``_claude_dir``/
``_launch_agents_dir``/``_home_git_dir`` helpers so tests can monkeypatch them at
a ``tmp_path``; handlers always call them (never cache) for that reason.
"""

from __future__ import annotations

import json
import os
import plistlib
import re
import subprocess
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from omniagentos.api.deps import StoreDep
from omniagentos.api.routes.control import _authorized, fail
from omniagentos.path_containment import inode_relative_parts

router = APIRouter(prefix="/api/system", tags=["system"])


# ── path resolution (monkeypatchable in tests) ──────────────────────────────


def _repo_root() -> Path:
    """Best-effort repo root: walk up to the dir containing ARCHI.md or .git."""
    override = os.environ.get("OMNIAGENTOS_REPO_ROOT")
    if override:
        return Path(override)
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "ARCHI.md").exists() or (parent / ".git").exists():
            return parent
    return here.parents[3]


def _claude_dir() -> Path:
    override = os.environ.get("OMNIAGENTOS_CLAUDE_DIR")
    return Path(override) if override else (Path.home() / ".claude")


def _launch_agents_dir() -> Path:
    override = os.environ.get("OMNIAGENTOS_LAUNCHAGENTS_DIR")
    return Path(override) if override else (Path.home() / "Library" / "LaunchAgents")


def _home_git_dir() -> Path:
    """The home repo the curator/UI commit agent-config edits into (audit trail)."""
    override = os.environ.get("OMNIAGENTOS_HOME_GIT_DIR")
    return Path(override) if override else Path.home()


# ── small file helpers ──────────────────────────────────────────────────────


def _read_text(path: Path) -> str | None:
    """Read a text file, returning None if absent/unreadable (never raises)."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return None


def _tail_lines(path: Path, n: int) -> list[str]:
    text = _read_text(path)
    if text is None:
        return []
    lines = text.splitlines()
    return lines[-n:] if n > 0 else lines


# ── frontmatter parsing (defensive) ─────────────────────────────────────────

_MODEL_FAMILIES = ("haiku", "sonnet", "opus", "fable")
_EFFORTS = ("low", "medium", "high", "xhigh")


def _split_frontmatter(text: str) -> dict[str, Any] | None:
    """Split a ``---``-delimited frontmatter block off the front of ``text``.

    Returns ``{opener, fm, closer, body}`` where ``fm`` is the list of raw
    frontmatter lines (newlines preserved) and ``body`` is the byte-exact
    remainder after the closing delimiter. Returns None when there is no
    well-formed block.
    """
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        return None
    for i in range(1, len(lines)):
        if lines[i].rstrip("\r\n") == "---":
            return {
                "opener": lines[0],
                "fm": lines[1:i],
                "closer": lines[i],
                "body": "".join(lines[i + 1 :]),
            }
    return None


def _parse_fm_dict(fm_lines: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in fm_lines:
        line = raw.rstrip("\r\n")
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        if key and key not in out:
            out[key] = value.strip()
    return out


def _first_paragraph(body: str) -> str:
    """First non-empty, non-heading paragraph of a markdown body."""
    para: list[str] = []
    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            if para:
                break
            continue
        if line.startswith("#") or line.startswith("---"):
            if para:
                break
            continue
        para.append(line)
    return " ".join(para).strip()


# Known delegated (writer) model lineages, most specific first, for body
# detection when there is no explicit `delegated_model:` frontmatter key.
_DELEGATED_TOKENS = (
    "gpt-5.6-sol",
    "gpt-5.6",
    "sol",
    "terra",
    "luna",
    "grok",
    "gemini",
    "kimi",
)


def _detect_delegated_from_body(body: str) -> str | None:
    """Best-effort: sniff the writer model token from the agent's body.

    Prefers an explicit ``-m <model>`` / ``--model <model>`` invocation line,
    then falls back to a known lineage word appearing anywhere in the body.
    """
    inv = re.search(
        r"(?:-m|--model|model_reasoning_effort=|-c\s+model=)\s*['\"]?([A-Za-z0-9.\-]+)", body
    )
    if inv:
        token = inv.group(1).lower()
        if any(t in token for t in _DELEGATED_TOKENS) or token in _DELEGATED_TOKENS:
            return token
    low = body.lower()
    for token in _DELEGATED_TOKENS:
        if re.search(rf"\b{re.escape(token)}\b", low):
            return token
    return None


def _parse_agent(path: Path) -> dict[str, Any]:
    """Parse one ``~/.claude/agents/*.md`` file into the roster shape."""
    text = _read_text(path) or ""
    split = _split_frontmatter(text)
    fm = _parse_fm_dict(split["fm"]) if split else {}
    body = split["body"] if split else text

    tools_raw = fm.get("tools", "")
    tools = [t.strip() for t in re.split(r"[,\s]+", tools_raw) if t.strip()] if tools_raw else []

    description = fm.get("description") or _first_paragraph(body)

    delegated_fm = fm.get("delegated_model")
    delegated_model: str | None
    if delegated_fm:
        delegated_model, delegated_editable = delegated_fm, True
    else:
        delegated_model, delegated_editable = _detect_delegated_from_body(body), False

    return {
        "name": fm.get("name") or path.stem,
        "model": fm.get("model") or None,
        "effort": fm.get("effort") or None,
        "category": fm.get("category") or "uncategorized",
        "delegated_model": delegated_model,
        "delegated_model_editable": delegated_editable,
        "tools": tools,
        "tools_count": len(tools),
        "description": description,
        "body_md": body,
        "path": str(path),
    }


def _load_agents() -> list[dict[str, Any]]:
    agents_dir = _claude_dir() / "agents"
    if not agents_dir.is_dir():
        return []
    out = []
    for path in sorted(agents_dir.glob("*.md")):
        try:
            out.append(_parse_agent(path))
        except Exception:  # noqa: BLE001 — one bad file never sinks the roster
            continue
    return out


# ── last-activated (best-effort, coarse by design) ──────────────────────────


def _conn(store: Any) -> Any:
    """Raw sqlite connection when the store is SQLite-backed, else None."""
    return getattr(store, "_connection", None)


def _agent_db_tokens(agent: dict[str, Any]) -> set[str]:
    """Substring tokens used to fuzzily map DB model strings to this agent.

    Coarse ON PURPOSE (the spec asks to map sessions.model + swarm_attempts.model
    to agent defs via their frontmatter/delegated model names): a `model: opus`
    agent matches any session whose model contains "opus". `last_activated_source`
    carries the exact matched string so the UI can be honest about coarseness.
    """
    toks: set[str] = set()
    fam = (agent.get("model") or "").strip().lower()
    if fam:
        toks.add(fam)
    dm = (agent.get("delegated_model") or "").strip().lower()
    if dm:
        toks.add(dm)
        alpha = re.findall(r"[a-z]{3,}", dm)
        if alpha:
            toks.add(alpha[-1])  # lineage word, e.g. gpt-5.6-sol -> sol
    return {t for t in toks if len(t) >= 3}


def _db_model_maxts(conn: Any) -> list[tuple[str, str, str]]:
    """(model_lower, iso_ts, source_label) pairs — max ts per distinct model."""
    rows: list[tuple[str, str, str]] = []
    if conn is None:
        return rows
    for sql, source in (
        (
            "SELECT model AS m, MAX(created_at) AS t FROM sessions "
            "WHERE model IS NOT NULL AND model != '' GROUP BY model",
            "session",
        ),
        (
            "SELECT model AS m, MAX(started_at) AS t FROM swarm_attempts "
            "WHERE model IS NOT NULL AND model != '' GROUP BY model",
            "attempt",
        ),
    ):
        try:
            for row in conn.execute(sql).fetchall():
                m, t = row["m"], row["t"]
                if m and t:
                    rows.append((str(m).lower(), str(t), f"{source}:{m}"))
        except Exception:  # noqa: BLE001 — missing table/store → no signal
            continue
    return rows


def _fusion_run_mentions() -> list[tuple[str, str]]:
    """(iso_mtime, lowercased_blob) per ~/.claude/fusion/runs entry (bounded)."""
    runs_dir = _claude_dir() / "fusion" / "runs"
    if not runs_dir.is_dir():
        return []
    out: list[tuple[str, str]] = []
    budget = 2_000_000  # cap total bytes scanned across all runs
    for entry in sorted(runs_dir.iterdir()):
        try:
            mtime = _iso(entry.stat().st_mtime)
        except OSError:
            continue
        blob = entry.name.lower()
        if entry.is_dir():
            for f in sorted(entry.rglob("*")):
                if budget <= 0:
                    break
                if f.is_file() and f.suffix.lower() in {".md", ".json", ".txt", ".log"}:
                    chunk = _read_text(f)
                    if chunk:
                        chunk = chunk[:20000]
                        blob += " " + chunk.lower()
                        budget -= len(chunk)
        out.append((mtime, blob))
    return out


def _iso(epoch: float) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(epoch, tz=UTC).isoformat().replace("+00:00", "Z")


def _compute_last_activated(
    agents: list[dict[str, Any]], store: Any, log_entries: list[dict[str, Any]]
) -> None:
    """Annotate each agent in-place with `last_activated` + `last_activated_source`."""
    db_models = _db_model_maxts(_conn(store))
    fusion = _fusion_run_mentions()

    # Pre-flatten improvement-log entries to (ts, blob) for name matching.
    log_blobs: list[tuple[str, str]] = []
    for entry in log_entries:
        ts = entry.get("ts")
        if not isinstance(ts, str):
            continue
        try:
            blob = json.dumps(entry, default=str).lower()
        except (TypeError, ValueError):
            blob = str(entry).lower()
        log_blobs.append((ts, blob))

    for agent in agents:
        name = (agent.get("name") or "").lower()
        tokens = _agent_db_tokens(agent)
        candidates: list[tuple[str, str]] = []  # (ts, source)

        for model_low, ts, source in db_models:
            if any(tok in model_low for tok in tokens):
                candidates.append((ts, source))

        if name:
            for mtime, blob in fusion:
                if name in blob:
                    candidates.append((mtime, "fusion-run"))
            for ts, blob in log_blobs:
                if name in blob:
                    candidates.append((ts, "improvement-log"))

        if candidates:
            best = max(candidates, key=lambda c: c[0])
            agent["last_activated"] = best[0]
            agent["last_activated_source"] = best[1]
        else:
            agent["last_activated"] = None
            agent["last_activated_source"] = None


# ── skills ──────────────────────────────────────────────────────────────────


def _load_skills() -> list[dict[str, Any]]:
    skills_dir = _claude_dir() / "skills"
    if not skills_dir.is_dir():
        return []
    out = []
    for sub in sorted(p for p in skills_dir.iterdir() if p.is_dir()):
        skill_md = sub / "SKILL.md"
        if not skill_md.is_file():
            continue
        text = _read_text(skill_md) or ""
        split = _split_frontmatter(text)
        fm = _parse_fm_dict(split["fm"]) if split else {}
        out.append(
            {
                "name": fm.get("name") or sub.name,
                "description": fm.get("description")
                or _first_paragraph(split["body"] if split else text),
                "category": fm.get("category") or "uncategorized",
                "path": str(skill_md),
            }
        )
    return out


# ── improvement log ─────────────────────────────────────────────────────────


def _improvement_log_entries(limit: int) -> list[dict[str, Any]]:
    """Last ``limit`` parsed lines of var/improvement-log.jsonl (malformed skipped)."""
    path = _repo_root() / "var" / "improvement-log.jsonl"
    text = _read_text(path)
    if text is None:
        return []
    entries: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict):
            entries.append(obj)
    return entries[-limit:] if limit > 0 else entries


# ── loop-agent (improver/reviewer) discovery ────────────────────────────────

_IMPROVER_HINTS = (
    "curator",
    "optimizer",
    "improve",
    "review",
    "critic",
    "reliability",
    "selfimprove",
    "reviewer",
    "judge",
    "learn",
    "evolve",
)

# Improver-adjacent "guard" jobs: they protect the estate (backups/snapshots)
# rather than curate it. Discovered and surfaced read-only (no editable prompt),
# distinct from the infra jobs (api/dashboard/runner/sessions) that stay hidden.
_GUARD_HINTS = (
    "backup",
    "snapshot",
    "guard",
)


def _humanize_schedule(plist: dict[str, Any]) -> str:
    if "StartInterval" in plist:
        try:
            secs = int(plist["StartInterval"])
        except (TypeError, ValueError):
            return "interval"
        if secs % 3600 == 0:
            return f"every {secs // 3600}h"
        if secs % 60 == 0:
            return f"every {secs // 60}m"
        return f"every {secs}s"
    cal = plist.get("StartCalendarInterval")
    if cal is not None:
        entries = cal if isinstance(cal, list) else [cal]
        times: list[str] = []
        for e in entries:
            if not isinstance(e, dict):
                continue
            h, m = e.get("Hour"), e.get("Minute")
            if h is not None:
                times.append(f"{int(h):02d}:{int(m):02d}" if m is not None else f"{int(h):02d}:00")
            elif m is not None:
                times.append(f"hourly at :{int(m):02d}")
        if times:
            return "daily at " + ", ".join(times)
    if plist.get("RunAtLoad"):
        return "at load"
    return "on demand"


def _script_from_args(args: Any) -> tuple[str | None, str]:
    """(.sh path or None, human command string) from ProgramArguments."""
    if not isinstance(args, list):
        return None, str(args or "")
    strings = [str(a) for a in args]
    sh = next((s for s in strings if s.endswith(".sh")), None)
    # The interesting command is usually the last argument of `sh -lc "<cmd>"`.
    command = strings[-1] if strings else ""
    return sh, command


def _next_fire(plist: dict[str, Any], last_run_iso: str | None) -> str | None:
    """Best-effort next-fire estimate as a local-tz ISO string (None if unknowable).

    Interval jobs project from the last observed run (or now) forward to the next
    future slot; calendar jobs take the soonest future occurrence of any of their
    Hour/Minute triggers. This is an estimate for the UI, not a launchd oracle.
    """
    from datetime import datetime, timedelta

    now = datetime.now().astimezone()

    if "StartInterval" in plist:
        try:
            secs = int(plist["StartInterval"])
        except (TypeError, ValueError):
            return None
        if secs <= 0:
            return None
        base = now
        if last_run_iso:
            try:
                base = datetime.fromisoformat(last_run_iso.replace("Z", "+00:00")).astimezone()
            except ValueError:
                base = now
        nxt = base + timedelta(seconds=secs)
        if nxt <= now:
            missed = int((now - base).total_seconds() // secs) + 1
            nxt = base + timedelta(seconds=secs * missed)
        return nxt.isoformat()

    cal = plist.get("StartCalendarInterval")
    if cal is not None:
        entries = cal if isinstance(cal, list) else [cal]
        candidates: list[datetime] = []
        for e in entries:
            if not isinstance(e, dict):
                continue
            hour, minute = e.get("Hour"), e.get("Minute")
            if hour is None and minute is None:
                continue
            target_minute = int(minute) if minute is not None else 0
            if hour is not None:
                cand = now.replace(hour=int(hour), minute=target_minute, second=0, microsecond=0)
                if cand <= now:
                    cand = cand + timedelta(days=1)
            else:  # hourly at :MM
                cand = now.replace(minute=target_minute, second=0, microsecond=0)
                if cand <= now:
                    cand = cand + timedelta(hours=1)
            candidates.append(cand)
        if candidates:
            return min(candidates).isoformat()
    return None


def _last_word(s: str) -> str:
    words = re.findall(r"[a-z0-9]+", s.lower())
    return words[-1] if words else ""


def _improver_matches_label(improver: str | None, suffix: str) -> bool:
    """Fuzzy: does an improvement-log `improver` field belong to this loop `suffix`?

    Matches on equality, containment either way, or a shared trailing lineage word
    (e.g. improver "curator" ↔ suffix "fable-curator").
    """
    if not improver:
        return False
    a = improver.strip().lower()
    b = suffix.strip().lower()
    if not a or not b:
        return False
    if a == b or a in b or b in a:
        return True
    la, lb = _last_word(a), _last_word(b)
    return bool(la) and la == lb


def _recent_activity_for(
    suffix: str, log_entries: list[dict[str, Any]], limit: int
) -> list[dict[str, Any]]:
    """Newest-first slice of the improvement log attributed to this improver."""
    matched = [e for e in log_entries if _improver_matches_label(e.get("improver"), suffix)]
    matched.sort(key=lambda e: e.get("ts") or "", reverse=True)
    return matched[:limit] if limit > 0 else matched


def _find_prompt_md(label_suffix: str, sh_path: str | None, repo_root: Path) -> Path | None:
    candidate_dirs: list[Path] = []
    if sh_path:
        p = Path(sh_path)
        if not p.is_absolute():
            p = repo_root / sh_path
        candidate_dirs.append(p.parent)
    candidate_dirs.append(repo_root / "scripts" / label_suffix)
    seen: set[Path] = set()
    for d in candidate_dirs:
        if d in seen or not d.is_dir():
            continue
        seen.add(d)
        for pattern in ("prompt*.md", "*-prompt.md", "*_prompt.md"):
            for f in sorted(d.glob(pattern)):
                return f
    return None


def _discover_loops() -> list[dict[str, Any]]:
    """Enumerate com.omniagentos.* launchd jobs that curate, review, or guard the estate.

    A job qualifies as an **improver** when it either has an adjacent ``prompt*.md``
    (the strongest signal of an LLM curation/review loop — this catches the nightly
    fleet: golden-suite, backlog-executor, hygiene, provider-sentinel, plus any
    user-created reviewer) or its label matches a known improver keyword
    (curator/optimizer/reliability/…). A job qualifies as a **guard** when its label
    matches a guard keyword (backup/snapshot/guard) but it carries no prompt —
    surfaced read-only so the estate's protective loops (github-backup) are visible
    but not editable. Infra jobs (api/dashboard/runner/sessions/banking/…) match
    neither and stay hidden.

    Each entry carries ``kind`` ("improver"|"guard"), ``editable`` (has a prompt
    file), a best-effort ``next_fire`` estimate, and ``recent_activity`` — this
    improver's last 5 improvement-log entries (matched by improver ≈ label suffix).
    """
    la_dir = _launch_agents_dir()
    repo_root = _repo_root()
    if not la_dir.is_dir():
        return []
    log_entries = _improvement_log_entries(0)
    loops: list[dict[str, Any]] = []
    for plist_path in sorted(la_dir.glob("com.omniagentos.*.plist")):
        try:
            with plist_path.open("rb") as fh:
                data = plistlib.load(fh)
        except Exception:  # noqa: BLE001 — malformed plist → skip that job
            continue
        if not isinstance(data, dict):
            continue
        label = str(data.get("Label") or plist_path.stem)
        suffix = label.split("com.omniagentos.", 1)[-1]
        suffix_low = suffix.lower()
        sh_path, command = _script_from_args(data.get("ProgramArguments"))

        prompt_file = _find_prompt_md(suffix, sh_path, repo_root)
        has_prompt = prompt_file is not None
        is_improver = has_prompt or any(h in suffix_low for h in _IMPROVER_HINTS)
        is_guard = (not is_improver) and any(h in suffix_low for h in _GUARD_HINTS)
        if not is_improver and not is_guard:
            continue
        kind = "improver" if is_improver else "guard"

        log_path = data.get("StandardOutPath") or data.get("StandardErrorPath")
        log_p = Path(log_path) if log_path else (repo_root / "var" / "log" / f"{suffix}.log")
        last_run_guess = None
        if log_p.is_file():
            try:
                last_run_guess = _iso(log_p.stat().st_mtime)
            except OSError:
                last_run_guess = None

        loops.append(
            {
                "label": label,
                "suffix": suffix,
                "kind": kind,
                "editable": has_prompt,
                "schedule_human": _humanize_schedule(data),
                "next_fire": _next_fire(data, last_run_guess),
                "script_path": sh_path or command or None,
                "prompt_path": str(prompt_file) if prompt_file else None,
                "prompt_md": _read_text(prompt_file) if prompt_file else None,
                "log_path": str(log_p) if log_p.is_file() else None,
                "last_log_tail": _tail_lines(log_p, 40),
                "last_run_guess": last_run_guess,
                "recent_activity": _recent_activity_for(suffix, log_entries, 5),
            }
        )
    return loops


def _curator_reports(limit: int) -> list[dict[str, Any]]:
    reports_dir = _claude_dir() / "curator-reports"
    if not reports_dir.is_dir():
        return []
    files = sorted(
        (p for p in reports_dir.iterdir() if p.is_file()),
        key=lambda p: p.name,
    )
    out = []
    for path in files[-limit:] if limit > 0 else files:
        text = _read_text(path) or ""
        first_lines = [ln for ln in text.splitlines() if ln.strip()][:3]
        out.append(
            {
                "filename": path.name,
                "path": str(path),
                "first_lines": first_lines,
            }
        )
    return out


# ── agent activity timeline ─────────────────────────────────────────────────


def _resolve_agent_tokens(agent_name: str) -> tuple[set[str], str]:
    """(db match tokens, canonical name) for a filter agent, from the roster."""
    for agent in _load_agents():
        if (agent.get("name") or "").lower() == agent_name.lower():
            return _agent_db_tokens(agent), (agent.get("name") or agent_name).lower()
    return {agent_name.lower()} if len(agent_name) >= 3 else set(), agent_name.lower()


def _date_of(ts: str | None) -> str | None:
    if not ts or not isinstance(ts, str):
        return None
    return ts[:10]


def _agent_activity(
    store: Any, agent: str | None, day: str | None, limit: int
) -> list[dict[str, Any]]:
    tokens: set[str] = set()
    name_low = ""
    if agent:
        tokens, name_low = _resolve_agent_tokens(agent)

    def model_matches(model: str | None) -> bool:
        if not agent:
            return True
        if not model:
            return False
        low = model.lower()
        return any(tok in low for tok in tokens)

    rows: list[dict[str, Any]] = []
    conn = _conn(store)

    if conn is not None:
        try:
            for r in conn.execute(
                "SELECT id, created_at, model, state, title, effort FROM sessions "
                "WHERE created_at IS NOT NULL ORDER BY created_at DESC LIMIT 2000"
            ).fetchall():
                if day and _date_of(r["created_at"]) != day:
                    continue
                if not model_matches(r["model"]):
                    continue
                rows.append(
                    {
                        "ts": r["created_at"],
                        "source": "session",
                        "model": r["model"],
                        "effort": r["effort"],
                        "status": r["state"],
                        "summary": r["title"] or "",
                        "ref_id": r["id"],
                    }
                )
        except Exception:  # noqa: BLE001
            pass
        try:
            for r in conn.execute(
                "SELECT id, started_at, provider, model, effort, end_reason, board_task_id "
                "FROM swarm_attempts WHERE started_at IS NOT NULL "
                "ORDER BY started_at DESC LIMIT 2000"
            ).fetchall():
                if day and _date_of(r["started_at"]) != day:
                    continue
                if not model_matches(r["model"]):
                    continue
                rows.append(
                    {
                        "ts": r["started_at"],
                        "source": "attempt",
                        "model": r["model"],
                        "provider": r["provider"],
                        "effort": r["effort"],
                        "status": r["end_reason"] or "running",
                        "summary": r["board_task_id"] or "",
                        "ref_id": r["id"],
                    }
                )
        except Exception:  # noqa: BLE001
            pass

    for entry in _improvement_log_entries(0):
        ts = entry.get("ts")
        if not isinstance(ts, str):
            continue
        if day and _date_of(ts) != day:
            continue
        if agent:
            try:
                blob = json.dumps(entry, default=str).lower()
            except (TypeError, ValueError):
                blob = str(entry).lower()
            if name_low and name_low not in blob and not any(tok in blob for tok in tokens):
                continue
        raw_changes = entry.get("changes")
        changes = raw_changes if isinstance(raw_changes, list) else []
        kinds = sorted(
            {str(c.get("kind")) for c in changes if isinstance(c, dict) and c.get("kind")}
        )
        rows.append(
            {
                "ts": ts,
                "source": "improvement",
                "improver": entry.get("improver"),
                "status": ",".join(kinds) if kinds else "log",
                "summary": entry.get("notes") or "",
                "change_count": len(changes),
            }
        )

    rows.sort(key=lambda r: r.get("ts") or "", reverse=True)
    return rows[:limit] if limit > 0 else rows


# ── endpoints ───────────────────────────────────────────────────────────────


class SpendBreakerResetResponse(BaseModel):
    tripped: bool
    cleared: bool


@router.post("/spend-breaker/reset")
def reset_spend_breaker(
    _: Annotated[None, Depends(_authorized)] = None,
) -> SpendBreakerResetResponse:
    """Un-trip the Steward spend circuit breaker (see api/middleware/chokepoint.py).

    ``_authorized`` here is defense-in-depth: POST is ALREADY gated app-wide by
    ``require_session_token`` (main.py), and the chokepoint middleware only lets
    a request to this exact path fall through to routing at all -- while
    tripped, every OTHER mutating request is refused before it ever reaches a
    route/dependency. Both checks resolve the same underlying session token
    (``omniagentos.sessions.token.verify_token``), so a tokenless caller is
    rejected 401 regardless of which check runs; this is not two independent
    secrets, just two enforcement points so the token requirement is provable
    at the route itself, not only inferred from middleware wiring.

    Clearing is idempotent: calling this when the breaker is already clear (or
    when no trip has ever happened) succeeds and reports ``tripped: false``.
    """
    del _
    from omniagentos.api.middleware.chokepoint import clear_breaker_state

    cleared = clear_breaker_state()
    if not cleared:
        fail(500, "spend_breaker_reset_failed", "failed to clear spend circuit breaker state")
    return SpendBreakerResetResponse(tripped=False, cleared=True)


@router.get("/map")
def get_map(_: Annotated[None, Depends(_authorized)] = None) -> dict[str, Any]:
    """ARCHI.md text + the mermaid system-map source + the ARCHI generation stamp."""
    del _
    repo_root = _repo_root()
    archi_md = _read_text(repo_root / "ARCHI.md")
    diagram_mmd = _read_text(repo_root / "docs" / "architecture" / "system-map.mmd")
    diagram_md = _read_text(repo_root / "docs" / "architecture" / "system-map.md")

    generated_at = None
    if archi_md:
        try:
            from omniagentos.archdocs.staleness import parse_stamp_comment

            stamp = parse_stamp_comment(archi_md)
            if stamp is not None:
                generated_at = stamp.generated_at
        except Exception:  # noqa: BLE001
            generated_at = None

    return {
        "archi_md": archi_md,
        "archi_path": str(repo_root / "ARCHI.md"),
        "diagram_mmd": diagram_mmd,
        "diagram_md": diagram_md,
        "diagram_path": str(repo_root / "docs" / "architecture" / "system-map.mmd"),
        "generated_at": generated_at,
    }


@router.get("/agents")
def get_agents(
    store: StoreDep,
    _: Annotated[None, Depends(_authorized)] = None,
) -> dict[str, Any]:
    """Roster from ~/.claude/agents/*.md with best-effort last-activated stamps."""
    del _
    agents = _load_agents()
    try:
        _compute_last_activated(agents, store, _improvement_log_entries(0))
    except Exception:  # noqa: BLE001 — activation is a nicety, never a hard failure
        for agent in agents:
            agent.setdefault("last_activated", None)
            agent.setdefault("last_activated_source", None)
    categories = sorted({a.get("category") or "uncategorized" for a in agents})
    return {"agents": agents, "categories": categories, "agents_dir": str(_claude_dir() / "agents")}


@router.get("/skills")
def get_skills(_: Annotated[None, Depends(_authorized)] = None) -> dict[str, Any]:
    """Skill library from ~/.claude/skills/*/SKILL.md (list only, no bodies)."""
    del _
    skills = _load_skills()
    categories = sorted({s.get("category") or "uncategorized" for s in skills})
    return {"skills": skills, "categories": categories, "skills_dir": str(_claude_dir() / "skills")}


@router.get("/improvers")
def get_improvers(_: Annotated[None, Depends(_authorized)] = None) -> dict[str, Any]:
    """The improver/reviewer loop roster + the shared improvement log."""
    del _
    return {
        "loops": _discover_loops(),
        "improvement_log": _improvement_log_entries(30),
        "curator_reports": _curator_reports(5),
        "optimizer_playbook_tail": (
            (lambda t: "\n".join(t.splitlines()[-60:]) if t else None)(
                _read_text(_repo_root() / "vault" / "swarm" / "playbook.md")
            )
        ),
        "improvement_log_path": str(_repo_root() / "var" / "improvement-log.jsonl"),
    }


class ImproverPromptPatch(BaseModel):
    """New full content for an improver's editable prompt file. No path field —
    the target is resolved server-side from launchd discovery, never the client."""

    content: str


_PROMPT_MAX_BYTES = 64 * 1024


def _git_commit_repo(file_path: Path, message: str) -> bool:
    """Best-effort: add + commit the edited prompt file in the OmniAgentOS repo."""
    repo = _repo_root()
    try:
        add = subprocess.run(
            ["git", "-C", str(repo), "add", str(file_path)],
            capture_output=True,
            timeout=15,
        )
        if add.returncode != 0:
            return False
        commit = subprocess.run(
            ["git", "-C", str(repo), "commit", "-m", message, "--", str(file_path)],
            capture_output=True,
            timeout=15,
        )
        return commit.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


@router.patch("/improvers/{label}/prompt")
def patch_improver_prompt(
    label: str,
    body: ImproverPromptPatch,
    _: Annotated[None, Depends(_authorized)] = None,
) -> dict[str, Any]:
    """Overwrite the discovered prompt file for improver ``label`` + commit.

    Rails: the target path is resolved SERVER-SIDE from launchd discovery (never
    from the request body — there is no client-supplied path anywhere); the file
    must already exist; content is capped at 64KB; the write is committed to the
    OmniAgentOS repo audit trail. Guard entries and improvers without a prompt
    file are read-only (409). Unknown labels 404.
    """
    del _
    loop = next((cand for cand in _discover_loops() if cand["label"] == label), None)
    if loop is None:
        fail(404, "not_found", "improver not found", {"label": label})

    prompt_path = loop.get("prompt_path")
    if not loop.get("editable") or not prompt_path:
        fail(409, "read_only", "this improver has no editable prompt file", {"label": label})

    target = Path(prompt_path)
    if not target.is_file():
        fail(404, "not_found", "prompt file no longer exists on disk", {"label": label})

    content = body.content
    size = len(content.encode("utf-8"))
    if size > _PROMPT_MAX_BYTES:
        fail(413, "too_large", f"prompt exceeds {_PROMPT_MAX_BYTES // 1024}KB cap", {"bytes": size})

    try:
        target.write_text(content, encoding="utf-8")
    except OSError as exc:
        fail(500, "internal", f"could not write prompt file: {exc}", {"label": label})

    committed = _git_commit_repo(target, f"system-ui: improver prompt edit {label}")
    return {"label": label, "prompt_path": str(target), "committed": committed, "bytes": size}


@router.get("/agent-activity")
def get_agent_activity(
    store: StoreDep,
    agent: str | None = Query(default=None),
    day: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    limit: int = Query(default=100, ge=1, le=1000),
    _: Annotated[None, Depends(_authorized)] = None,
) -> dict[str, Any]:
    """Merged per-agent timeline across sessions, swarm_attempts, improvement log."""
    del _
    return {"activity": _agent_activity(store, agent, day, limit)}


class AgentConfigPatch(BaseModel):
    """Whitelisted editable frontmatter fields only. Body is never touched."""

    model: str | None = None
    effort: str | None = None
    category: str | None = None
    delegated_model: str | None = None


_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_CATEGORY_RE = re.compile(r"^[\w .+/&-]{1,64}$")
_DELEGATED_RE = re.compile(r"^[A-Za-z0-9.+/_-]{1,64}$")


def _rewrite_frontmatter(text: str, updates: dict[str, str]) -> str:
    """Return ``text`` with ``updates`` applied to its frontmatter; body byte-exact.

    Existing keys are updated in place (all other frontmatter lines, including
    comments and untouched keys, are preserved verbatim); whitelisted keys not
    yet present are appended just before the closing delimiter.
    """
    split = _split_frontmatter(text)
    if split is None:
        raise ValueError("no frontmatter block")
    remaining = dict(updates)
    new_fm: list[str] = []
    for raw in split["fm"]:
        line = raw.rstrip("\r\n")
        key = line.partition(":")[0].strip() if ":" in line else None
        if key is not None and key in remaining:
            newline = raw[len(line) :] or "\n"
            new_fm.append(f"{key}: {remaining.pop(key)}{newline}")
        else:
            new_fm.append(raw)
    for key, value in remaining.items():
        new_fm.append(f"{key}: {value}\n")
    return split["opener"] + "".join(new_fm) + split["closer"] + split["body"]


def _git_commit_home(file_path: Path, message: str) -> bool:
    """Best-effort: force-add + commit the edited file in the home repo audit trail."""
    home = _home_git_dir()
    try:
        add = subprocess.run(
            ["git", "-C", str(home), "add", "-f", str(file_path)],
            capture_output=True,
            timeout=15,
        )
        if add.returncode != 0:
            return False
        commit = subprocess.run(
            ["git", "-C", str(home), "commit", "-m", message, "--", str(file_path)],
            capture_output=True,
            timeout=15,
        )
        return commit.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


@router.patch("/agents/{name}")
def patch_agent(
    name: str,
    body: AgentConfigPatch,
    store: StoreDep,
    _: Annotated[None, Depends(_authorized)] = None,
) -> dict[str, Any]:
    """Rewrite whitelisted frontmatter keys of ~/.claude/agents/<name>.md + commit.

    Rails: ``name`` is validated against the roster with no path traversal; only
    ``model``/``effort``/``category``/``delegated_model`` may change; the body is
    preserved byte-exact; ``delegated_model`` is editable ONLY when the file
    already carries it as a frontmatter key (body-detected values stay read-only).
    """
    del _
    if not _NAME_RE.match(name):
        fail(422, "validation", "invalid agent name", {"name": name})

    agents_dir = (_claude_dir() / "agents").resolve()
    target = (agents_dir / f"{name}.md").resolve()
    # Path-traversal rail: the resolved target must live directly in agents_dir.
    if inode_relative_parts(target.parent, agents_dir) != () or not target.is_file():
        fail(404, "not_found", "agent not found", {"name": name})

    text = _read_text(target)
    if text is None:
        fail(404, "not_found", "agent file unreadable", {"name": name})

    split = _split_frontmatter(text)
    if split is None:
        fail(422, "validation", "agent file has no frontmatter block", {"name": name})
    fm = _parse_fm_dict(split["fm"])

    updates: dict[str, str] = {}
    if body.model is not None:
        if body.model not in _MODEL_FAMILIES:
            fail(
                422,
                "validation",
                "model must be one of haiku|sonnet|opus|fable",
                {"model": body.model},
            )
        updates["model"] = body.model
    if body.effort is not None:
        if body.effort not in _EFFORTS:
            fail(
                422,
                "validation",
                "effort must be one of low|medium|high|xhigh",
                {"effort": body.effort},
            )
        updates["effort"] = body.effort
    if body.category is not None:
        cat = body.category.strip()
        if not _CATEGORY_RE.match(cat):
            fail(422, "validation", "invalid category", {"category": body.category})
        updates["category"] = cat
    if body.delegated_model is not None:
        if "delegated_model" not in fm:
            fail(
                409,
                "read_only",
                "delegated_model is body-detected for this agent and not editable; "
                "add a delegated_model frontmatter key to make it editable",
                {"name": name},
            )
        dm = body.delegated_model.strip()
        if not _DELEGATED_RE.match(dm):
            fail(
                422,
                "validation",
                "invalid delegated_model",
                {"delegated_model": body.delegated_model},
            )
        updates["delegated_model"] = dm

    if not updates:
        fail(422, "validation", "no editable fields provided", None)

    try:
        new_text = _rewrite_frontmatter(text, updates)
    except ValueError as exc:
        fail(422, "validation", str(exc), {"name": name})

    try:
        target.write_text(new_text, encoding="utf-8")
    except OSError as exc:
        fail(500, "internal", f"could not write agent file: {exc}", {"name": name})

    committed = _git_commit_home(target, f"system-ui: agent config edit {name}")

    return {"agent": _parse_agent(target), "committed": committed, "changed": sorted(updates)}


# ── Observatory delta ─ "since you were gone" counts ─────────────────────


_DELTA_MAX_DAYS = 30  # server-side cap; longer ranges deep-link to full pages


@router.get("/delta")
def get_delta(
    store: StoreDep,
    since: str = Query(
        ...,
        description="ISO-8601 timestamp; counts returned cover [since, now].",
    ),
    _: Annotated[None, Depends(_authorized)] = None,
) -> dict[str, Any]:
    """Observatory "since you were gone" aggregate (pinned contract, §B).

    Returns the number of notable events that happened between ``since`` and
    now across the surfaces the cockpit pulse tiles summarize. The server
    clamps the range to the last 30 days regardless of client input — longer
    ranges are better served by the full pages (/pulse, /improvements, …).

    Response shape (pinned contract):

        {
          "since": ISO,
          "skills_updated": int,
          "improvements_decided": int,
          "loops_run": int,
          "tasks_completed": int,
          "chats_active": int,
        }

    Each count is computed independently and degrades to ``0`` on a missing
    or renamed table — one broken table must never blank the whole strip.
    """
    from datetime import UTC, datetime, timedelta

    del _
    try:
        since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
    except ValueError:
        fail(422, "validation", "since must be a valid ISO-8601 timestamp", {"since": since})

    now = datetime.now(UTC)
    floor = now - timedelta(days=_DELTA_MAX_DAYS)
    if since_dt < floor:
        since_dt = floor
    since_iso = since_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    conn = _conn(store)

    def _count(sql: str, params: tuple[Any, ...] = ()) -> int:
        try:
            row = conn.execute(sql, params).fetchone()
        except Exception:  # noqa: BLE001 — missing table degrades to 0
            return 0
        return int(row[0] or 0) if row is not None else 0

    skills_updated = _count("SELECT COUNT(*) FROM skills WHERE updated_at >= ?", (since_iso,))
    improvements_decided = _count(
        "SELECT COUNT(*) FROM improvements WHERE updated_at >= ?", (since_iso,)
    )
    loops_run = _count("SELECT COUNT(*) FROM routine_runs WHERE created_at >= ?", (since_iso,))
    # "completed" covers the terminal-good state of board_tasks (done, archived);
    # in_progress/in_review transitions happen constantly and aren't user-facing.
    tasks_completed = _count(
        "SELECT COUNT(*) FROM board_tasks WHERE updated_at >= ? AND status IN ('done', 'archived')",
        (since_iso,),
    )
    chats_active = _count("SELECT COUNT(*) FROM chats WHERE status = 'active'")

    return {
        "since": since_iso,
        "skills_updated": skills_updated,
        "improvements_decided": improvements_decided,
        "loops_run": loops_run,
        "tasks_completed": tasks_completed,
        "chats_active": chats_active,
    }


__all__ = ["router"]
