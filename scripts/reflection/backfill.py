#!/usr/bin/env python3
"""One-shot historical backfill for the reflection loop.

Reads ALL transcript history available on disk (per-provider CLI stores),
week by week, distills each week into a MACHINE-READABLE lessons JSON via
the analyst model, and consolidates everything into one lessons document.

Raw transcript text is used transiently in memory and NEVER persisted —
each weekly output contains only metadata, counts, and distilled lessons
(operator directive 2026-07-26).

Usage: uv run python scripts/reflection/backfill.py [--start 2026-01-01]
Outputs: var/reflection/backfill/<YYYY-Www>.json  +  docs/lessons/<date>-history-backfill.md
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from omniagentos.reflection.adapters import (  # noqa: E402
    ClaudeSourceAdapter,
    CodexSourceAdapter,
    GeminiSourceAdapter,
    GrokSourceAdapter,
    KimiSourceAdapter,
)

OUT_DIR = REPO_ROOT / "var" / "reflection" / "backfill"
BYTE_CAP = 512 * 1024  # per-source raw read (transient)
TOKEN_CAP = 3000  # per-source digest tokens (transient)
BUNDLE_CHAR_CAP = 50_000  # per-week LLM input
REFS_PER_PROVIDER = 30  # newest-first cap per provider per week
LLM_TIMEOUT_S = 150

ADAPTERS = {
    "claude": ClaudeSourceAdapter(),
    "gemini": GeminiSourceAdapter(),
    "kimi": KimiSourceAdapter(),
    "codex": CodexSourceAdapter(),
    "grok": GrokSourceAdapter(),
}


def _ref_mtime(ref: Any) -> float | None:
    """Best-effort mtime for the heterogeneous ref types the adapters return."""
    candidates: list[str | Path] = []
    if isinstance(ref, Path):
        candidates = [ref]
    elif isinstance(ref, tuple):
        candidates = [x for x in ref if isinstance(x, (str, Path))]
    elif isinstance(ref, dict):
        candidates = [v for v in ref.values() if isinstance(v, (str, Path))]
    for c in candidates:
        try:
            p = Path(c)
            if p.exists():
                return p.stat().st_mtime
        except OSError:
            continue
    return None


def _distill(week_label: str, bundle: str) -> dict | None:
    prompt = (
        "You are a reflection analyst distilling agent-transcript digests into lessons.\n"
        f"Week: {week_label}. Below are transient digests from multiple AI-agent CLIs.\n"
        "Respond with ONLY a JSON object in a ```json codeblock, schema:\n"
        '{"week_summary": "<2-3 sentences>",\n'
        ' "failure_patterns": [{"tag": "<kebab>", "example": "<1 line>"}],\n'
        ' "lessons": [{"claim": "<what happened, 1 line>", "generalized_fix": "<the rule>",'
        ' "fix_target": "<config/brief/adapter/policy area>", "confidence": "low|medium|high"}],\n'
        ' "notable": ["<1-line notable events>"]}\n'
        "3-10 lessons max, only genuinely generalizable ones. Do not use any tools.\n"
        "--- DIGESTS ---\n" + bundle[:BUNDLE_CHAR_CAP]
    )
    for attempt in (1, 2):
        try:
            # start_new_session + killpg: subprocess.run(timeout=) cannot reach
            # the CLI's grandchildren, which hold the stdout pipe and hang this
            # batch job. No approval mode: pure text generation, no tools.
            proc = subprocess.Popen(
                ["gemini", "-m", "gemini-3.6-flash", "-p", prompt],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=str(OUT_DIR),
                start_new_session=True,
            )
            try:
                out, _err = proc.communicate(timeout=LLM_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    # Best effort: group gone or unsignalable; kill the leader.
                    proc.kill()
                try:
                    # Bounded reap — never let the reap itself hang the job.
                    proc.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    with contextlib.suppress(Exception):
                        # Reap the SIGKILLed leader without reading pipes.
                        proc.wait(timeout=2)
                raise
            m = re.search(r"```json\s*(\{.*?\})\s*```", out, re.DOTALL)
            raw = m.group(1) if m else out.strip()
            data = json.loads(raw)
            if isinstance(data, dict) and "lessons" in data:
                return data
        except Exception as exc:  # noqa: BLE001 - resilient batch job
            print(f"    distill attempt {attempt} failed: {exc}", flush=True)
            time.sleep(3)
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2026-01-01")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    start = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=UTC)
    # Align to Monday
    start -= timedelta(days=start.weekday())
    now = datetime.now(UTC)

    all_lessons: list[dict] = []
    week_files: list[str] = []
    week = start
    while week < now:
        week_end = week + timedelta(days=7)
        label = f"{week.isocalendar().year}-W{week.isocalendar().week:02d}"
        out_path = OUT_DIR / f"{label}.json"
        if out_path.exists():  # resumable
            try:
                prev = json.loads(out_path.read_text())
                all_lessons.extend(prev.get("distilled", {}).get("lessons", []) or [])
                week_files.append(out_path.name)
                week = week_end
                continue
            except Exception:  # noqa: BLE001
                pass
        s_epoch, e_epoch = week.timestamp(), week_end.timestamp()
        counts: dict[str, int] = {}
        bytes_read = 0
        parts: list[str] = []
        for name, adapter in ADAPTERS.items():
            try:
                refs = adapter.discover(s_epoch)
            except Exception as exc:  # noqa: BLE001
                print(f"  {label} {name}: discover failed: {exc}", flush=True)
                continue
            in_window = []
            for r in refs:
                mt = _ref_mtime(r)
                if mt is None or mt < e_epoch:
                    in_window.append((mt or 0, r))
            in_window.sort(key=lambda t: t[0], reverse=True)
            picked = [r for _, r in in_window[:REFS_PER_PROVIDER]]
            counts[name] = len(picked)
            for r in picked:
                try:
                    d = adapter.extract(r, BYTE_CAP, TOKEN_CAP)
                    bytes_read += d.bytes_read
                    if d.summary_or_sample.strip():
                        parts.append(f"### [{name}] {d.source_name}\n{d.summary_or_sample}")
                except Exception:  # noqa: BLE001
                    continue
        record: dict = {
            "week": label,
            "window": [week.strftime("%Y-%m-%d"), week_end.strftime("%Y-%m-%d")],
            "session_counts": counts,
            "bytes_read_transient": bytes_read,
            "generated_at": datetime.now(UTC).isoformat(),
        }
        bundle = "\n\n".join(parts)
        if not bundle.strip():
            record["empty"] = True
        else:
            distilled = _distill(label, bundle)
            record["distilled"] = distilled or {"error": "llm_distill_failed"}
            if distilled:
                all_lessons.extend(distilled.get("lessons", []) or [])
        # NOTE: `parts`/`bundle` (raw-ish samples) are intentionally NOT written.
        out_path.write_text(json.dumps(record, indent=2) + "\n")
        week_files.append(out_path.name)
        print(
            f"  {label}: sessions={sum(counts.values())} lessons={len((record.get('distilled') or {}).get('lessons', []) or [])}",
            flush=True,
        )
        week = week_end

    # Consolidate
    today = now.strftime("%Y-%m-%d")
    doc = REPO_ROOT / "docs" / "lessons" / f"{today}-history-backfill.md"
    seen: set[str] = set()
    unique: list[dict] = []
    for les in all_lessons:
        key = (les.get("claim") or "").strip().lower()[:120]
        if key and key not in seen:
            seen.add(key)
            unique.append(les)
    body = [
        f"# History backfill — distilled lessons ({today})",
        "",
        f"One-shot backfill over all transcript history on disk (weekly buckets in `var/reflection/backfill/`, {len(week_files)} weeks). Raw transcripts were read transiently and NOT stored — each week persists only counts + distilled lessons (operator directive).",
        "",
        "```json",
        json.dumps(unique, indent=2),
        "```",
        "",
    ]
    doc.write_text("\n".join(body))
    print(f"CONSOLIDATED: {len(unique)} unique lessons -> {doc}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
