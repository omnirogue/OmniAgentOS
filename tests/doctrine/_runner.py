"""Shared pytest subprocess runner used by revert and counterfeit helpers.

Never pipes pytest through ``tail`` (or any other consumer that replaces the
exit code). Captures full stdout/stderr so failure text is preserved verbatim.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class PytestRun:
    """One invocation of a named pytest node."""

    nodeid: str
    returncode: int
    stdout: str
    stderr: str
    command: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return self.returncode == 0

    @property
    def failed(self) -> bool:
        return self.returncode != 0

    @property
    def combined_output(self) -> str:
        parts = [self.stdout or "", self.stderr or ""]
        return "\n".join(p for p in parts if p).rstrip()

    def failure_text(self) -> str:
        """Verbatim failure body for reports (stdout then stderr)."""
        text = self.combined_output
        if not text:
            return (
                f"(no output; pytest exit {self.returncode} for nodeid={self.nodeid!r}; "
                f"cmd={' '.join(self.command)!r})"
            )
        return text


def repo_root() -> Path:
    """Resolve the checkout root from this file's location (tests/doctrine/)."""
    return Path(__file__).resolve().parents[2]


def run_pytest_node(
    nodeid: str,
    *,
    cwd: Path | None = None,
    extra_args: list[str] | None = None,
    env: dict[str, str] | None = None,
    timeout: float = 120.0,
) -> PytestRun:
    """Run a single pytest nodeid and return the full result.

    Invokes ``python -m pytest`` directly — never via a shell pipeline — so the
    process exit code is pytest's own.
    """
    work = Path(cwd) if cwd is not None else repo_root()
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        nodeid,
        "-vv",
        "--tb=short",
        "--no-header",
        "-p",
        "no:cacheprovider",
    ]
    if extra_args:
        cmd.extend(extra_args)

    run_env = os.environ.copy()
    if env:
        run_env.update(env)
    # Keep doctrine self-runs off the operator DB even if a parent shell exported one.
    run_env.setdefault("OMNIAGENTOS_DB", str(work / ".doctrine-tmp-db" / "omniagentos.db"))

    completed = subprocess.run(
        cmd,
        cwd=str(work),
        capture_output=True,
        text=True,
        env=run_env,
        timeout=timeout,
        check=False,
    )
    return PytestRun(
        nodeid=nodeid,
        returncode=completed.returncode,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
        command=tuple(cmd),
    )
