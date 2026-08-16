#!/usr/bin/env python3
"""Blind Pairwise Code Quality Judge.

Automated benchmark evaluation comparing 3 implementations:
- Swarm (Sol + Gemini + Grok)
- Grok 4.5 Standalone
- GPT-5.6 Sol Standalone

Spawns 3 independent expert judges:
1. Grok 4.5 (xAI API)
2. Gemini 3.1 Pro (Google/LiteLLM proxy)
3. GPT-4o (OpenAI API)

Executes pairwise matchups (A vs B, B vs C, A vs C) for each task.
"""

from __future__ import annotations

import json
import os
import random
import sys
import time
from pathlib import Path
from typing import Any

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def load_env_keys() -> dict[str, str]:
    """Load API credentials from ~/.config/omni/connections.env securely."""
    env_file = Path("/Users/youruser/.config/omni/connections.env")
    keys = {}
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("export "):
                line = line[len("export ") :]
            if "=" in line:
                k, v = line.split("=", 1)
                keys[k.strip()] = v.strip().strip("'\"")
    return keys


KEYS = load_env_keys()


def get_api_key(name: str) -> str | None:
    return os.environ.get(name) or KEYS.get(name)


def call_grok_45(prompt: str) -> str:
    key = get_api_key("XAI_API_KEY")
    if not key:
        return "ERROR: XAI_API_KEY not configured"
    try:
        res = requests.post(
            "https://api.x.ai/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": "grok-4.5",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.0,
            },
            timeout=120,
        )
        res.raise_for_status()
        return res.json()["choices"][0]["message"]["content"]
    except Exception as exc:  # noqa: BLE001
        return f"Grok API call failed: {exc}"


def call_gemini_31_pro(prompt: str) -> str:
    try:
        res = requests.post(
            "http://127.0.0.1:4000/v1/chat/completions",
            headers={"Authorization": "Bearer sk-local-litellm-proxy-secure"},
            json={
                "model": "gemini-3.1-pro",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.0,
            },
            timeout=120,
        )
        res.raise_for_status()
        return res.json()["choices"][0]["message"]["content"]
    except Exception as exc:  # noqa: BLE001
        return f"Gemini API call failed: {exc}"


def call_gpt_4o(prompt: str) -> str:
    key = get_api_key("OPENAI_API_KEY")
    if not key:
        return "ERROR: OPENAI_API_KEY not configured"
    try:
        res = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": "gpt-4o",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.0,
            },
            timeout=120,
        )
        res.raise_for_status()
        return res.json()["choices"][0]["message"]["content"]
    except Exception as exc:  # noqa: BLE001
        return f"OpenAI API call failed: {exc}"


def read_project_code(directory: Path, patterns: list[str]) -> str:
    """Load matching code files in a directory into a structured text snippet."""
    code_parts = []
    for pattern in patterns:
        for p in sorted(directory.glob(pattern)):
            if p.is_file() and p.name not in {"PLAN.md", "WORKBOOK.md"}:
                try:
                    content = p.read_text(encoding="utf-8")
                    code_parts.append(f"=== FILE: {p.relative_to(directory)} ===\n{content}\n")
                except Exception:  # noqa: BLE001
                    pass
    return "\n".join(code_parts)


def evaluate_matchup(
    project_name: str,
    brief: str,
    name_a: str,
    code_a: str,
    name_b: str,
    code_b: str,
) -> dict[str, Any]:
    print(f"\nEvaluating {name_a} vs {name_b}...")

    # Randomize to prevent position bias
    is_a_first = random.choice([True, False])
    code_1 = code_a if is_a_first else code_b
    code_2 = code_b if is_a_first else code_a

    prompt = f"""You are an expert, independent software engineer and code evaluator.
You must perform a strict, objective, and BLIND pairwise comparison of two implementations of this brief:

TASK BRIEF:
{brief}

--------------------------------------------------------------------------------
IMPLEMENTATION 1:
{code_1}

--------------------------------------------------------------------------------
IMPLEMENTATION 2:
{code_2}

--------------------------------------------------------------------------------
Evaluate both implementations strictly on:
1. Completeness: Did they implement all requested features? Are there any 'TODO' placeholders or incomplete modules?
2. Code Quality & Architecture: Is the code clean, well-structured, modular, and optimized?
3. Aesthetics & User Experience (UX) (if applicable): Are animations, styling, and layouts modern? If not applicable, focus on system robustness.

Provide your evaluation in this JSON format (surround with standard JSON codeblock):
{{
  "evaluation": {{
    "implementation_1": {{
      "pros": ["...", "..."],
      "cons": ["...", "..."],
      "completeness_score": 1-10,
      "quality_score": 1-10,
      "ux_score": 1-10
    }},
    "implementation_2": {{
      "pros": ["...", "..."],
      "cons": ["...", "..."],
      "completeness_score": 1-10,
      "quality_score": 1-10,
      "ux_score": 1-10
    }}
  }},
  "reasoning": "A detailed, technical analysis comparing both implementations.",
  "winner": "Implementation 1" or "Implementation 2" or "Tie"
}}
"""

    judges = {
        "Grok 4.5": call_grok_45,
        "Gemini 3.1 Pro": call_gemini_31_pro,
        "GPT-4o": call_gpt_4o,
    }

    votes = []

    for judge_name, caller in judges.items():
        t0 = time.time()
        print(f"  > Calling {judge_name} ... ", end="", flush=True)
        response = caller(prompt)
        t1 = time.time()
        print(f"done ({int(t1 - t0)}s)")

        winner = "Error"
        try:
            start = response.find("{")
            end = response.rfind("}")
            verdict = json.loads(response[start : end + 1])
            raw_winner = verdict.get("winner", "Tie")

            if raw_winner == "Implementation 1":
                winner = name_a if is_a_first else name_b
            elif raw_winner == "Implementation 2":
                winner = name_b if is_a_first else name_a
            else:
                winner = "Tie"
        except Exception:  # noqa: BLE001
            if "Implementation 1" in response:
                winner = name_a if is_a_first else name_b
            elif "Implementation 2" in response:
                winner = name_b if is_a_first else name_a
            else:
                winner = "Tie"

        votes.append({"judge": judge_name, "winner": winner})

    # Tally votes for this matchup
    a_votes = sum(1 for v in votes if v["winner"] == name_a)
    b_votes = sum(1 for v in votes if v["winner"] == name_b)

    matchup_winner = "Tie"
    if a_votes > b_votes:
        matchup_winner = name_a
    elif b_votes > a_votes:
        matchup_winner = name_b

    print(f"  Matchup Winner: {matchup_winner} ({a_votes} vs {b_votes})")
    return {"matchup": f"{name_a} vs {name_b}", "winner": matchup_winner, "votes": votes}


def evaluate_project(
    project_name: str,
    brief: str,
    dir_swarm: Path,
    dir_grok: Path,
    dir_sol: Path,
    file_patterns: list[str],
) -> None:
    print("\n" + "=" * 70)
    print(f"📊 EVALUATING PROJECT: {project_name}")
    print("=" * 70 + "\n")

    code_swarm = read_project_code(dir_swarm, file_patterns)
    code_grok = read_project_code(dir_grok, file_patterns)
    code_sol = read_project_code(dir_sol, file_patterns)

    if not code_swarm:
        print(f"Error: Swarm files missing in {dir_swarm}", file=sys.stderr)
        return
    if not code_grok:
        print(f"Error: Grok files missing in {dir_grok}", file=sys.stderr)
        return
    if not code_sol:
        print(f"Error: Sol files missing in {dir_sol}", file=sys.stderr)
        return

    # Pairwise 1: Swarm vs Grok
    res_1 = evaluate_matchup(project_name, brief, "Swarm", code_swarm, "Grok Standalone", code_grok)

    # Pairwise 2: Swarm vs Sol
    res_2 = evaluate_matchup(project_name, brief, "Swarm", code_swarm, "Sol Standalone", code_sol)

    # Pairwise 3: Grok vs Sol
    res_3 = evaluate_matchup(
        project_name, brief, "Grok Standalone", code_grok, "Sol Standalone", code_sol
    )

    # Tournament Tally
    wins = {"Swarm": 0, "Grok Standalone": 0, "Sol Standalone": 0, "Tie": 0}
    wins[res_1["winner"]] += 1
    wins[res_2["winner"]] += 1
    wins[res_3["winner"]] += 1

    print(f"\n--- OVERALL TOURNAMENT RESULTS FOR {project_name.upper()} ---")
    for name, w in wins.items():
        if name != "Tie":
            print(f"🏆 {name}: {w} Matchup Wins")


def main() -> None:
    # 1. Digital Clock
    evaluate_project(
        "Digital Clock",
        "Build a stunningly beautiful, highly interactive Digital Clock Generator in HTML, CSS, and vanilla JS. Neon themes, CSS animations, 12h/24h toggles, calendar, stopwatch.",
        Path("/Users/youruser/Desktop/DigitalClock_Swarm"),
        Path("/Users/youruser/Desktop/DigitalClock_Grok"),
        Path("/Users/youruser/Desktop/DigitalClock_Sol"),
        ["*.html", "*.css", "*.js"],
    )

    # 2. GameTest (10 2D Arcade Games)
    evaluate_project(
        "10 2D Arcade Games",
        "Build 10 high-quality 2D arcade games inside the GameTest project directory on the desktop. Snake, Pong, Brick Breaker, Asteroids, Tetris, Space Invaders, Flappy Bird, Pac-Man, Frogger, Lunar Lander.",
        Path("/Users/youruser/Desktop/GameTest_Swarm"),
        Path("/Users/youruser/Desktop/GameTest_Grok"),
        Path("/Users/youruser/Desktop/GameTest_Sol"),
        ["*.html", "*.css", "*.js"],
    )

    # 3. Docker DevOps
    evaluate_project(
        "Docker DevOps Stack",
        "Create a Docker-compose orchestrated environment containing a Redis cache, a Postgres database, a Python FastAPI worker, and a Nginx reverse proxy. Include health checks and a script to simulate high load.",
        Path("/Users/youruser/Desktop/DockerDevOps_Swarm"),
        Path("/Users/youruser/Desktop/DockerDevOps_Grok"),
        Path("/Users/youruser/Desktop/DockerDevOps_Sol"),
        ["docker-compose*.yml", "Dockerfile*", "*.py", "*.conf", "*.sh"],
    )


if __name__ == "__main__":
    main()
