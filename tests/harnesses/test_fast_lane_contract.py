"""Static contract for the exploratory parallel pytest lane."""

from __future__ import annotations

import re
import shlex
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OPT_IN_MARKERS = (
    "live_cli",
    "perf",
    "live_ollama",
    "live",
    "counterfeit_gate",
    "feature_health",
    # The outcome-bound probe drives the DEPLOYED stack over HTTP and fails (never
    # skips) when it is down, so it is opt-in for the same reason `live` is.
    "e2e",
    # cef7ac6f introduced observational LiveSim against the running system; it
    # is intentionally excluded from both default and fast lanes.
    "livesim",
)


def _parse_makefile_variables(makefile_text: str) -> dict[str, str]:
    """Parse Makefile variable definitions (NAME := value and NAME ?= value)."""
    variables: dict[str, str] = {}
    for line in makefile_text.splitlines():
        # Match both := and ?= assignments
        match = re.match(r'(\w+)\s*(:=|\?=)\s*(.+)$', line.strip())
        if match:
            name, _, value = match.groups()
            variables[name] = value.strip()
    return variables


def _substitute_makefile_variables(text: str, variables: dict[str, str]) -> str:
    """Substitute $(NAME) with values from the variables dict."""
    result = text
    for name, value in variables.items():
        result = result.replace(f"$({name})", value)
    return result


def _marker_expression(command: str) -> str:
    tokens = shlex.split(command.replace("\\\n", " "))
    positions = [index for index, token in enumerate(tokens) if token == "-m"]
    assert len(positions) == 1
    return tokens[positions[0] + 1]


def _make_recipe(target: str) -> str:
    makefile_text = (ROOT / "Makefile").read_text(encoding="utf-8")
    lines = makefile_text.splitlines()
    start = lines.index(f"{target}:") + 1
    recipe: list[str] = []
    for line in lines[start:]:
        if line.startswith("\t"):
            recipe.append(line.removeprefix("\t"))
            continue
        if not line.strip():
            continue
        break
    raw_recipe = "\n".join(recipe)

    # Parse and substitute Makefile variables
    variables = _parse_makefile_variables(makefile_text)
    return _substitute_makefile_variables(raw_recipe, variables)


def test_fast_lane_preserves_every_default_opt_in_exclusion() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    default_addopts = config["tool"]["pytest"]["ini_options"]["addopts"]
    assert _marker_expression(default_addopts) == f"not ({' or '.join(OPT_IN_MARKERS)})"

    fast_expression = _marker_expression(_make_recipe("test-fast"))
    expected = ("smoke", *OPT_IN_MARKERS)
    assert fast_expression == f"not ({' or '.join(expected)})"


def test_fast_lane_keeps_parallel_and_directory_quarantine_contract() -> None:
    recipe = _make_recipe("test-fast")
    tokens = shlex.split(recipe.replace("\\\n", " "))

    # Verify -n is a positive integer not exceeding 16 (P-core cap on this box)
    worker_index = tokens.index("-n")
    worker_count = int(tokens[worker_index + 1])
    assert 1 <= worker_count <= 16, f"Worker count {worker_count} is outside valid range [1, 16]"

    assert tokens[tokens.index("--dist") + 1] == "worksteal"
    for path in ("tests/simharness", "tests/counterfeits", "tests/longhaul"):
        assert f"--ignore={path}" in tokens
