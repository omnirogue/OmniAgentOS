"""Run executor: one run = (family, world_seed, arm, model) x N sequential episodes.

Memory at episode t is the arm-filtered view of episodes 1..t-1 of the same run.
Every episode is appended to `episodes.jsonl` immediately; re-invoking the same
experiment id resumes by rebuilding memory from those lines, so a crashed
matrix continues without duplicating spend.
"""

from __future__ import annotations

import datetime as dt
import json
import random
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .client import CallResult, ModelSpec, OpenRouterClient
from .memory import EpisodeRecord, build_scripted_history, est_tokens, memory_block, sha
from .worlds import make_world

SYSTEM_PROMPT = (
    "You are an agent operating inside a simulated environment that is part of a "
    "learning experiment. Some environment behavior is undocumented and can only be "
    "learned from experience. If records of previous episodes are provided, study "
    "them: never resubmit an action that already failed — form a hypothesis about "
    "the undocumented rule behind each failure and try a systematically different "
    "variant; reuse what already worked. Always answer with a single JSON object "
    "and nothing else."
)

MAX_EPISODE_ERRORS = 2


@dataclass(frozen=True)
class RunSpec:
    family: str
    seed: int
    arm: str
    model: ModelSpec
    episodes: int = 16
    # Cross-model transfer (DESIGN.md §10 round 11): when set, the memory the
    # agent sees is the DONOR model's episode history for the same
    # (family, seed, arm), aligned episode-for-episode — the consumer's own
    # attempts are logged but never enter its memory.
    donor_alias: str | None = None

    @property
    def run_id(self) -> str:
        base = f"{self.family}-s{self.seed}-{self.arm}-{self.model.alias}"
        return f"{base}-don_{self.donor_alias}" if self.donor_alias else base


def _now() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _record_from_line(line: dict[str, Any]) -> EpisodeRecord:
    return EpisodeRecord(
        ep=line["ep"],
        task=line["task"],
        task_summary=line["task_summary"],
        task_text=line["task_text"],
        response_text=line["response"],
        action=line["action"],
        action_summary=line["action_summary"],
        parse_ok=line["parse_ok"],
        win=bool(line["win"]),
        code=line["code"],
        feedback=line["feedback"],
    )


def pick_shadow_world(family: str, seed: int, episodes: int, world):
    """Shadow world for the placebo arm, guaranteed to differ in hidden structure.

    A same-structure shadow leaks target-valid answers through the
    "content-free" control (HT-008: at seed 2 the fixed +1000 shadow drew
    identical trapcli quirks).
    """
    for offset in range(1000, 11000, 1000):
        candidate = make_world(family, seed + offset, episodes)
        if candidate.hidden_signature() != world.hidden_signature():
            return candidate
    raise RuntimeError(f"no distinct shadow world found for {family} seed {seed}")


def _user_prompt(memory_text: str, task_text: str) -> str:
    if memory_text:
        return f"{memory_text}\n\n=== CURRENT TASK ===\n{task_text}"
    return task_text


class EpisodeLog:
    """Thread-safe append-only JSONL log shared by all runs of an experiment."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.Lock()
        # run_id -> gen -> ep -> line. Generations exist so an invalid run
        # (>MAX_EPISODE_ERRORS) can be rerun in full without rewriting the
        # append-only log (HT-005).
        self.done: dict[str, dict[int, dict[int, dict[str, Any]]]] = {}
        if path.exists():
            # Repair in BYTES before decoding: a kill can tear the tail inside
            # a multibyte UTF-8 sequence, and read_text() would raise before
            # the newline repair ever ran, blocking resume entirely (HT-003
            # reopened). errors="replace" garbles only the torn fragment,
            # which json.loads then skips like any other torn line.
            raw_bytes = path.read_bytes()
            if raw_bytes and not raw_bytes.endswith(b"\n"):
                with path.open("ab") as f:
                    f.write(b"\n")
            raw_text = raw_bytes.decode("utf-8", errors="replace")
            for raw in raw_text.splitlines():
                if not raw.strip():
                    continue
                try:
                    line = json.loads(raw)
                except ValueError:
                    continue  # torn tail from a killed run; the episode reruns
                gen = int(line.get("gen", 1))
                self.done.setdefault(line["run_id"], {}).setdefault(gen, {})[line["ep"]] = line

    def completed(self, run_id: str) -> tuple[int, dict[int, dict[str, Any]]]:
        """Latest generation for the run whose episodes should be reused.

        If the newest generation is already invalid (>MAX_EPISODE_ERRORS error
        episodes), the run must be redone in full: return the next generation
        number with no reusable episodes (the pre-registered
        full-rerun-on-invalidation rule).
        """
        gens = self.done.get(run_id, {})
        if not gens:
            return 1, {}
        gen = max(gens)
        eps = gens[gen]
        errors = sum(1 for line in eps.values() if line.get("code") == "ERROR")
        if errors > MAX_EPISODE_ERRORS:
            return gen + 1, {}
        return gen, eps

    def append(self, line: dict[str, Any]) -> None:
        data = json.dumps(line, sort_keys=True, ensure_ascii=False)
        with self.lock:
            with self.path.open("a") as f:
                f.write(data + "\n")
                f.flush()


def load_donor_records(
    donor_log_path: Path, donor_run_id: str
) -> list[EpisodeRecord]:
    """Latest-generation records of a donor run, for cross-model transfer."""
    donor_log = EpisodeLog(donor_log_path)
    _, eps = donor_log.completed(donor_run_id)
    return [
        _record_from_line(line)
        for ep, line in sorted(eps.items())
        if line.get("code") != "ERROR"
    ]


def run_one(
    spec: RunSpec,
    exp_id: str,
    log: EpisodeLog,
    client: OpenRouterClient | None,
    dry_run: bool = False,
    donor_records: list[EpisodeRecord] | None = None,
) -> dict[str, Any]:
    world = make_world(spec.family, spec.seed, spec.episodes)
    shadow = None
    if spec.arm == "placebo":
        shadow_world = pick_shadow_world(spec.family, spec.seed, spec.episodes, world)
        shadow = build_scripted_history(
            shadow_world, spec.episodes - 1, f"placebo:{spec.family}:{spec.seed}"
        )
    dry_rng = random.Random(f"dry:{spec.run_id}")
    gen, existing = log.completed(spec.run_id)
    records: list[EpisodeRecord] = []
    failed_signatures: set[str] = set()
    stats = {"errors": 0, "cost_usd": 0.0, "mem_tokens": 0, "calls": 0}
    wins: list[bool | None] = []
    repeats: list[bool] = []

    for ep in range(1, spec.episodes + 1):
        task = world.task(ep)
        if ep in existing:
            line = existing[ep]
            if line.get("code") == "ERROR":
                stats["errors"] += 1
                wins.append(None)
                repeats.append(False)
                continue
            rec = _record_from_line(line)
            records.append(rec)
            wins.append(rec.win)
            repeats.append(bool(line.get("repeat_failure")))
            if not rec.win and rec.parse_ok:
                failed_signatures.add(rec.action_summary)
            stats["mem_tokens"] += est_tokens(line.get("memory_text", "") or "")
            continue

        mem_source = records
        if donor_records is not None:
            # Aligned prefix: at episode t the consumer sees the donor's
            # episodes 1..t-1, exactly as the donor itself would have.
            mem_source = [r for r in donor_records if r.ep < ep]
        memory_text = memory_block(spec.arm, spec.family, mem_source, task, shadow)
        task_text = world.render_task(task)
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _user_prompt(memory_text, task_text)},
        ]

        if dry_run:
            action = world.scripted_action(task, dry_rng)
            display = {k: v for k, v in action.items() if k != "tokens"}
            result = CallResult(ok=True, text=json.dumps(display))
        else:
            assert client is not None
            result = client.chat(spec.model, messages)
        stats["calls"] += 1
        stats["cost_usd"] += result.cost_usd
        stats["mem_tokens"] += est_tokens(memory_text)

        base = {
            "exp_id": exp_id,
            "run_id": spec.run_id,
            "family": spec.family,
            "seed": spec.seed,
            "arm": spec.arm,
            "model": spec.model.alias,
            "model_id": spec.model.model_id,
            "ep": ep,
            "gen": gen,
            "ts": _now(),
            "task": task,
            "task_summary": world.task_summary(task),
            "task_text": task_text,
            "memory_sha": sha(memory_text) if memory_text else None,
            "memory_chars": len(memory_text),
            "memory_text": memory_text,
            "chance": task.get("chance"),
            "usage": {
                "prompt_tokens": result.prompt_tokens,
                "completion_tokens": result.completion_tokens,
            },
            "cost_usd": round(result.cost_usd, 8),
            "latency_s": result.latency_s,
            "provider": result.provider,
            "attempts": result.attempts,
        }

        if not result.ok:
            stats["errors"] += 1
            wins.append(None)
            repeats.append(False)
            log.append(
                base
                | {
                    "response": "",
                    "action": None,
                    "action_summary": "",
                    "parse_ok": False,
                    "win": None,
                    "code": "ERROR",
                    "feedback": "",
                    "repeat_failure": False,
                    "error": result.error,
                    "status": result.status,
                }
            )
            continue

        action = world.parse_action(result.text)
        verdict = world.verify(task, action)
        summary = world.action_summary(action)
        repeat = bool(action) and not verdict.win and summary in failed_signatures
        if action and not verdict.win:
            failed_signatures.add(summary)
        rec = EpisodeRecord(
            ep=ep,
            task=task,
            task_summary=world.task_summary(task),
            task_text=task_text,
            response_text=result.text,
            action=action,
            action_summary=summary,
            parse_ok=action is not None,
            win=verdict.win,
            code=verdict.code,
            feedback=verdict.feedback,
        )
        records.append(rec)
        wins.append(verdict.win)
        repeats.append(repeat)
        log.append(
            base
            | {
                "response": result.text,
                "action": action,
                "action_summary": summary,
                "parse_ok": action is not None,
                "win": verdict.win,
                "code": verdict.code,
                "feedback": verdict.feedback,
                "repeat_failure": repeat,
                "error": None,
            }
        )

    valid = [w for w in wins if w is not None]
    # Late window = the second half of the run (episodes 9-16 at 16 episodes,
    # 17-32 at 32 — round-9 pre-registration).
    late = [w for w in wins[spec.episodes // 2 :] if w is not None]
    return {
        "exp_id": exp_id,
        "run_id": spec.run_id,
        "family": spec.family,
        "seed": spec.seed,
        "arm": spec.arm,
        "model": spec.model.alias,
        "episodes": spec.episodes,
        "errors": stats["errors"],
        "valid": len(valid),
        "wins": sum(valid),
        "success": round(sum(valid) / len(valid), 4) if valid else None,
        "late_success": round(sum(late) / len(late), 4) if late else None,
        "repeat_failures": sum(repeats),
        "mem_tokens_mean": round(stats["mem_tokens"] / max(spec.episodes, 1), 1),
        "cost_usd": round(stats["cost_usd"], 6),
        "invalid": stats["errors"] > MAX_EPISODE_ERRORS,
    }
