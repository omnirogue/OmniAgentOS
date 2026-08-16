"""Harness adapters for extracting session transcripts across multiple provider CLIs.

Each adapter is a SourceAdapter supporting discover() and extract().
All reads are read-only, newest-first, and strictly size-capped.
"""

from __future__ import annotations

import json
import os
import re
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

import yaml

from omniagentos.contracts import estimate_tokens
from omniagentos.reflection.contracts import SourceDigest
from omniagentos.runtime_paths import (
    TOKEN_VAR_ENV_KEYS,
    resolve_sim_context_or_none,
    resolve_var_root,
)


class SourceAdapter(ABC):
    """Abstract base class for all CLI harness transcript adapters."""

    @abstractmethod
    def discover(self, window_start_epoch: float) -> list[Any]:
        """Find relevant session references within the given time window."""
        pass

    @abstractmethod
    def extract(self, ref: Any, byte_cap: int, token_cap: int) -> SourceDigest:
        """Extract and digest the transcript content for a session reference."""
        pass


# Hard ceilings for the optional taxonomy grep pass on oversized files.
# Prevents a multi-GB log from forcing finalize (or harvest) to scan to EOF
# when fewer than 100 taxonomy matches exist. Independent of content byte_cap
# so a small head/tail sample still allows a bounded full-file pattern pass.
_GREP_SCAN_LINE_CAP = 20_000
_GREP_SCAN_BYTE_CAP = 1 * 1024 * 1024
_GREP_MATCH_CAP = 100


def read_and_sample_file(path: Path, byte_cap: int = 2 * 1024 * 1024) -> tuple[str, bool, int]:
    """Read a file up to byte_cap. If oversized, samples head/tail and greps for errors.

    The taxonomy grep pass is bounded by ``_GREP_SCAN_LINE_CAP`` lines examined
    and ``_GREP_SCAN_BYTE_CAP`` bytes **read** (binary window; matching or not).
    It never materializes a whole line larger than that budget, and never walks
    to EOF on a huge file just because matches are sparse. A single matching
    line with no trailing newline is truncated to the remaining budget.
    """
    if not path.exists():
        return "", False, 0
    stat = path.stat()
    total_bytes = stat.st_size
    if total_bytes <= byte_cap:
        try:
            return path.read_text(encoding="utf-8", errors="replace"), False, total_bytes
        except OSError:
            return "", False, 0

    # Oversized file: sample head/tail
    half_cap = max(byte_cap // 2, 1)
    try:
        with open(path, "rb") as f:
            head_bytes = f.read(half_cap)
            f.seek(-half_cap, 2)
            tail_bytes = f.read(half_cap)
    except OSError:
        return "", False, 0

    head_text = head_bytes.decode("utf-8", errors="replace")
    tail_text = tail_bytes.decode("utf-8", errors="replace")

    # Grep for failure patterns — bounded scan, never EOF on sparse matches.
    from omniagentos.reflection.taxonomy import TAXONOMY_PATTERNS

    patterns: list[str] = []
    for tag_pats in TAXONOMY_PATTERNS.values():
        patterns.extend(tag_pats)
    combined_regex = re.compile("|".join(patterns), re.IGNORECASE)

    # Bound by BYTES READ, not by lines. Text-mode line iteration would
    # materialize an entire newline-free / minified line before any cap check,
    # then retain a matching line whole — defeating _GREP_SCAN_BYTE_CAP. Read a
    # hard-capped binary window first so no single line can exceed the budget.
    matching_lines: list[str] = []
    grep_byte_budget = max(_GREP_SCAN_BYTE_CAP, 1)
    try:
        with open(path, "rb") as f:
            raw_window = f.read(grep_byte_budget)
        window_text = raw_window.decode("utf-8", errors="replace")
        # splitlines(keepends=False): a file with no newline at all is one part
        # whose length is already bounded by the binary read above.
        window_lines = window_text.splitlines()
        retained_match_bytes = 0
        for line_no, line in enumerate(window_lines, 1):
            if line_no > _GREP_SCAN_LINE_CAP:
                break
            if len(matching_lines) >= _GREP_MATCH_CAP:
                break
            if retained_match_bytes >= grep_byte_budget:
                break
            if not line:
                continue
            if not combined_regex.search(line):
                continue
            prefix = f"Line {line_no}: "
            stripped = line.strip()
            prefix_bytes = len(prefix.encode("utf-8", errors="replace"))
            remain = grep_byte_budget - retained_match_bytes - prefix_bytes
            if remain <= 0:
                break
            encoded = stripped.encode("utf-8", errors="replace")
            if len(encoded) > remain:
                stripped = encoded[:remain].decode("utf-8", errors="replace")
                entry = f"{prefix}{stripped}…[line_truncated]"
            else:
                entry = f"{prefix}{stripped}"
            matching_lines.append(entry)
            retained_match_bytes += len(entry.encode("utf-8", errors="replace"))
    except OSError:
        pass

    grep_section = ""
    if matching_lines:
        grep_section = "\n\n[ERRORS GREPPED FROM TRUNCATED CONTENT]\n" + "\n".join(matching_lines)

    sampled_text = f"{head_text}\n... [TRUNCATED {total_bytes - byte_cap} BYTES] ...\n{tail_text}{grep_section}"
    return sampled_text, True, total_bytes


def enforce_token_cap(text: str, token_cap: int = 8000) -> tuple[str, bool]:
    """Clamps a string to approximate token_cap using chars // 4 estimation."""
    est = estimate_tokens(text)
    if est <= token_cap:
        return text, False
    char_limit = token_cap * 4
    truncated = (
        text[:char_limit]
        + f"\n... [TRUNCATED DUE TO SOURCE DIGEST TOKEN CAP ({token_cap} tokens)] ..."
    )
    return truncated, True


class ClaudeSourceAdapter(SourceAdapter):
    """Adapter for Claude Code: ~/.claude*/projects/**/*.jsonl."""

    def discover(self, window_start_epoch: float) -> list[Path]:
        discovered: list[Path] = []
        config_dirs: list[Path] = []

        # Try loading config_dirs from configs/accounts.yaml
        accounts_yaml = Path("configs/accounts.yaml")
        if accounts_yaml.exists():
            try:
                data = yaml.safe_load(accounts_yaml.read_text(encoding="utf-8"))
                accounts = data.get("providers", {}).get("claude", {}).get("accounts", [])
                for acc in accounts:
                    c_dir = acc.get("config_dir")
                    if c_dir:
                        path_resolved = Path(os.path.expanduser(c_dir))
                        if path_resolved.exists():
                            config_dirs.append(path_resolved)
            except Exception:
                pass

        # Fallback / expansion of standard ~/.claude* directories
        home = Path.home()
        for p in home.glob(".claude*"):
            if p.is_dir() and p not in config_dirs:
                config_dirs.append(p)

        # Glob projects
        for c_dir in config_dirs:
            for jsonl_file in c_dir.glob("projects/**/*.jsonl"):
                try:
                    mtime = jsonl_file.stat().st_mtime
                    if mtime >= window_start_epoch:
                        discovered.append(jsonl_file)
                except OSError:
                    pass

        # Sort newest-first
        discovered.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
        return discovered

    def extract(self, ref: Path, byte_cap: int, token_cap: int) -> SourceDigest:
        content, byte_cap_hit, total_bytes = read_and_sample_file(ref, byte_cap)
        final_text, token_cap_hit = enforce_token_cap(content, token_cap)

        caps_hit: list[str] = []
        if byte_cap_hit:
            caps_hit.append("byte_cap")
        if token_cap_hit:
            caps_hit.append("token_cap")

        return SourceDigest(
            source_name=f"claude:{ref.relative_to(Path.home()) if ref.is_relative_to(Path.home()) else ref.name}",
            bytes_read=total_bytes,
            tokens_estimated=estimate_tokens(final_text),
            caps_hit=caps_hit,
            summary_or_sample=final_text,
        )


class GeminiSourceAdapter(SourceAdapter):
    """Adapter for Gemini CLI: ~/.gemini/tmp/** session logs."""

    def discover(self, window_start_epoch: float) -> list[Path]:
        discovered: list[Path] = []
        gemini_dir = Path.home() / ".gemini" / "tmp"
        if not gemini_dir.exists():
            return []

        for p in gemini_dir.rglob("*"):
            if p.is_file():
                try:
                    mtime = p.stat().st_mtime
                    if mtime >= window_start_epoch:
                        discovered.append(p)
                except OSError:
                    pass

        discovered.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
        return discovered

    def extract(self, ref: Path, byte_cap: int, token_cap: int) -> SourceDigest:
        content, byte_cap_hit, total_bytes = read_and_sample_file(ref, byte_cap)
        final_text, token_cap_hit = enforce_token_cap(content, token_cap)

        caps_hit: list[str] = []
        if byte_cap_hit:
            caps_hit.append("byte_cap")
        if token_cap_hit:
            caps_hit.append("token_cap")

        return SourceDigest(
            source_name=f"gemini:{ref.name}",
            bytes_read=total_bytes,
            tokens_estimated=estimate_tokens(final_text),
            caps_hit=caps_hit,
            summary_or_sample=final_text,
        )


class KimiSourceAdapter(SourceAdapter):
    """Adapter for Kimi CLI: ~/.kimi-code session logs and transcripts."""

    def discover(self, window_start_epoch: float) -> list[dict[str, Any]]:
        discovered: list[dict[str, Any]] = []
        kimi_home = Path.home() / ".kimi-code"
        session_index = kimi_home / "session_index.jsonl"
        if not session_index.exists():
            return []

        try:
            with open(session_index, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        entry = json.loads(line)
                        session_dir_raw = entry.get("sessionDir")
                        if not session_dir_raw:
                            continue
                        # Resolve path
                        session_dir = Path(os.path.expanduser(session_dir_raw))
                        if not session_dir.is_absolute():
                            session_dir = kimi_home / session_dir

                        state_json = session_dir / "state.json"
                        if state_json.exists():
                            mtime = state_json.stat().st_mtime
                            if mtime >= window_start_epoch:
                                entry["resolved_dir"] = session_dir
                                entry["mtime"] = mtime
                                discovered.append(entry)
                    except Exception:
                        continue
        except OSError:
            pass

        # Sort newest-first
        discovered.sort(key=lambda e: e.get("mtime", 0), reverse=True)
        return discovered

    def extract(self, ref: dict[str, Any], byte_cap: int, token_cap: int) -> SourceDigest:
        session_dir = ref["resolved_dir"]
        session_id = ref.get("sessionId", "unknown")
        ref.get("workDir", "")

        aggregated: list[str] = []
        total_bytes = 0
        caps_hit: list[str] = []

        # Read state.json
        state_json = session_dir / "state.json"
        state_data = {}
        if state_json.exists():
            try:
                state_bytes = state_json.read_text(encoding="utf-8", errors="replace")
                total_bytes += len(state_bytes)
                state_data = json.loads(state_bytes)
                title = state_data.get("title", "")
                last_prompt = state_data.get("lastPrompt", "")
                aggregated.append(
                    f"KIMI SESSION: {session_id}\nTITLE: {title}\nLAST PROMPT: {last_prompt}"
                )
                # Verbatim sub-agent briefs
                agents = state_data.get("agents", {})
                if agents:
                    aggregated.append("SUB-AGENTS:")
                    for a_id, a_info in agents.items():
                        swarm_item = a_info.get("swarmItem", {})
                        aggregated.append(f"  - Agent {a_id}: {json.dumps(swarm_item)}")
            except Exception:
                pass

        # Parse wire.jsonl (transcripts, turns, usage.record events)
        wire_files = list(session_dir.glob("agents/*/wire.jsonl"))
        usage_total = {"tokens": 0, "cost": 0.0}
        transcript_turns: list[str] = []

        for wire in wire_files:
            try:
                stat = wire.stat()
                total_bytes += stat.st_size
                with open(wire, encoding="utf-8", errors="replace") as f:
                    for line in f:
                        if not line.strip():
                            continue
                        try:
                            event = json.loads(line)
                            event_type = event.get("type")
                            # Accumulate usage
                            if event_type == "usage" or "usage" in event:
                                u_rec = event.get("usage", {})
                                usage_total["tokens"] += u_rec.get("input_tokens", 0) + u_rec.get(
                                    "output_tokens", 0
                                )
                                usage_total["cost"] += float(u_rec.get("cost_usd") or 0.0)

                            # Capture text/turn content
                            actor = event.get("actor") or event.get("role") or "agent"
                            text = event.get("text") or event.get("content") or ""
                            tool_use = event.get("tool_use") or event.get("tool")
                            if text or tool_use:
                                item = f"[{actor}]"
                                if tool_use:
                                    item += f" Tool: {json.dumps(tool_use)}"
                                if text:
                                    item += f" {text}"
                                transcript_turns.append(item)
                        except Exception:
                            continue
            except OSError:
                pass

        if usage_total["tokens"] > 0 or usage_total["cost"] > 0.0:
            aggregated.append(
                f"USAGE: {usage_total['tokens']} tokens, Cost USD: {usage_total['cost']:.6f}"
            )

        if transcript_turns:
            aggregated.append("TRANSCRIPT:")
            aggregated.extend(transcript_turns[:200])  # limit turns before caps

        # Read output.log for verdicts
        output_logs = list(session_dir.glob("agents/main/tasks/*/output.log"))
        for log in output_logs:
            try:
                log_content, b_hit, l_bytes = read_and_sample_file(
                    log, 50 * 1024
                )  # 50KB cap for log
                total_bytes += l_bytes
                aggregated.append(f"\nOUTPUT LOG ({log.name}):\n{log_content}")
            except OSError:
                pass

        # Join output
        full_text = "\n".join(aggregated)
        if total_bytes > byte_cap:
            caps_hit.append("byte_cap")
            # Trucate to byte_cap
            full_text = full_text[:byte_cap] + "\n... [TRUNCATED DUE TO BYTE CAP] ..."

        final_text, token_cap_hit = enforce_token_cap(full_text, token_cap)
        if token_cap_hit:
            caps_hit.append("token_cap")

        return SourceDigest(
            source_name=f"kimi:{session_id}",
            bytes_read=total_bytes,
            tokens_estimated=estimate_tokens(final_text),
            caps_hit=caps_hit,
            summary_or_sample=final_text,
        )


class CodexSourceAdapter(SourceAdapter):
    """Adapter for Codex CLI: ~/.codex/sessions/**."""

    def discover(self, window_start_epoch: float) -> list[Path]:
        discovered: list[Path] = []
        codex_dir = Path.home() / ".codex" / "sessions"
        if not codex_dir.exists():
            return []

        for p in codex_dir.rglob("*"):
            if p.is_file():
                try:
                    mtime = p.stat().st_mtime
                    if mtime >= window_start_epoch:
                        discovered.append(p)
                except OSError:
                    pass

        discovered.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
        return discovered

    def extract(self, ref: Path, byte_cap: int, token_cap: int) -> SourceDigest:
        content, byte_cap_hit, total_bytes = read_and_sample_file(ref, byte_cap)
        final_text, token_cap_hit = enforce_token_cap(content, token_cap)

        caps_hit: list[str] = []
        if byte_cap_hit:
            caps_hit.append("byte_cap")
        if token_cap_hit:
            caps_hit.append("token_cap")

        return SourceDigest(
            source_name=f"codex:{ref.name}",
            bytes_read=total_bytes,
            tokens_estimated=estimate_tokens(final_text),
            caps_hit=caps_hit,
            summary_or_sample=final_text,
        )


class GrokSourceAdapter(SourceAdapter):
    """Adapter for Grok CLI home directories (~/.grok, ~/.config/grok, ~/.grok-code)."""

    def discover(self, window_start_epoch: float) -> list[tuple[str, Path]]:
        discovered: list[tuple[str, Path]] = []
        candidates = {
            "grok": Path.home() / ".grok",
            "grok-config": Path.home() / ".config" / "grok",
            "grok-code": Path.home() / ".grok-code",
        }

        for name, p in candidates.items():
            if p.exists() and p.is_dir():
                # We can glob inside these for any logs/transcripts within the 36h window
                for file_path in p.rglob("*"):
                    if file_path.is_file():
                        try:
                            mtime = file_path.stat().st_mtime
                            if mtime >= window_start_epoch:
                                discovered.append((name, file_path))
                        except OSError:
                            pass

        discovered.sort(
            key=lambda item: item[1].stat().st_mtime if item[1].exists() else 0, reverse=True
        )
        return discovered

    def extract(self, ref: tuple[str, Path], byte_cap: int, token_cap: int) -> SourceDigest:
        name, file_path = ref
        content, byte_cap_hit, total_bytes = read_and_sample_file(file_path, byte_cap)
        final_text, token_cap_hit = enforce_token_cap(content, token_cap)

        caps_hit: list[str] = []
        if byte_cap_hit:
            caps_hit.append("byte_cap")
        if token_cap_hit:
            caps_hit.append("token_cap")

        return SourceDigest(
            source_name=f"grok:{name}:{file_path.name}",
            bytes_read=total_bytes,
            tokens_estimated=estimate_tokens(final_text),
            caps_hit=caps_hit,
            summary_or_sample=final_text,
        )


class SwarmVerdictSourceAdapter(SourceAdapter):
    """Adapter for the pump-lane swarm outcome records under ``var/swarm``.

    2026-07-29 finding: the lane -> verdict -> rework pumps write the day's
    orchestration outcomes (cross-lineage reviewer verdicts in
    ``sol-verdicts/``, lane states in ``fleet-ledger.jsonl``, standing-role
    findings in ``role-log.jsonl``) as files nothing harvested — an entire
    swarm day was invisible to the nightly digest because the SQLite tables it
    reads stayed empty. This adapter closes that hole.

    Repo-local rather than a CLI home directory; honours
    ``OMNIAGENTOS_SWARM_DIR`` (tests) and defaults to ``<repo>/var/swarm``.
    It reads ONLY the curated outcome files — it must never descend into
    ``clones/`` (full per-lane repo clones, ~150k files/day).
    """

    LEDGER_NAMES = ("role-log.jsonl", "fleet-ledger.jsonl", "integration-ledger.jsonl")

    def _swarm_dir(self) -> Path:
        # An explicit OMNIAGENTOS_SWARM_DIR always wins, sim or not (same rule as
        # sessions/token.py's explicit override: the defect is the silent implicit
        # default, never a deliberate assignment).
        override = os.environ.get("OMNIAGENTOS_SWARM_DIR")
        if override:
            return Path(override)

        # Under a campaign the swarm dir joins the isolated set: it follows the
        # campaign var root instead of reading the operator checkout's verdicts.
        # It resolves rather than raising, because nothing in the repo SETS
        # OMNIAGENTOS_SWARM_DIR — a hard failure here would abort every
        # reflection harvest under simulation (harvest.py calls discover()
        # unguarded).
        if resolve_sim_context_or_none() is not None:
            return resolve_var_root(env_keys=TOKEN_VAR_ENV_KEYS, leaf=("swarm",))

        # Production is byte-identical to the historical path and deliberately
        # ignores VAR/VAR_DIR: the launchers always export them, and honouring
        # them here would move discovery to <VAR_DIR>/swarm while every producer
        # (swarm/planner.py, land-approved-lanes.py, plan-consolidator.sh) still
        # writes <repo>/var/swarm — i.e. zero verdicts found.
        return Path(__file__).resolve().parents[2] / "var" / "swarm"

    def discover(self, window_start_epoch: float) -> list[Path]:
        base = self._swarm_dir()
        if not base.is_dir():
            return []
        discovered: list[Path] = []
        verdict_dir = base / "sol-verdicts"
        if verdict_dir.is_dir():
            for p in verdict_dir.iterdir():
                if not p.is_file():
                    continue
                try:
                    if p.stat().st_mtime >= window_start_epoch:
                        discovered.append(p)
                except OSError:
                    pass
        for name in self.LEDGER_NAMES:
            p = base / name
            try:
                if p.is_file() and p.stat().st_mtime >= window_start_epoch:
                    discovered.append(p)
            except OSError:
                pass
        discovered.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
        return discovered

    def extract(self, ref: Path, byte_cap: int, token_cap: int) -> SourceDigest:
        content, byte_cap_hit, total_bytes = read_and_sample_file(ref, byte_cap)
        final_text, token_cap_hit = enforce_token_cap(content, token_cap)

        caps_hit: list[str] = []
        if byte_cap_hit:
            caps_hit.append("byte_cap")
        if token_cap_hit:
            caps_hit.append("token_cap")

        label = (
            f"swarm:sol-verdicts/{ref.name}"
            if ref.parent.name == "sol-verdicts"
            else f"swarm:{ref.name}"
        )
        return SourceDigest(
            source_name=label,
            bytes_read=total_bytes,
            tokens_estimated=estimate_tokens(final_text),
            caps_hit=caps_hit,
            summary_or_sample=final_text,
        )
