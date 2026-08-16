"""Run the project's own test suite against an applied candidate — the verifier.

This is the step that makes Agentless compute-optimal: a test suite is a cheap,
objective oracle, so instead of asking a model (or a human) to judge N candidate
patches, we just run the tests and let the ones that fail eliminate themselves
(:mod:`omniagentos.agentless.select` then only has to choose among passers).
"""

from __future__ import annotations

import shlex
import subprocess
import time

from omniagentos.promptshape.compress import compress

_TAIL_CHARS = 4000


def run_tests(workdir: str, test_cmd: str, timeout_s: int) -> tuple[int | None, str, float]:
    """Run parsed ``test_cmd`` argv in ``workdir``; return (returncode, tail, seconds).

    stdout+stderr are merged. On timeout, returncode is None and the tail notes
    the timeout. The tail is capped to the last 4000 chars and passed through
    ``promptshape.compress(kind='log')`` so callers can hand it straight to a
    downstream prompt without re-shrinking it themselves."""
    argv = shlex.split(test_cmd, posix=True)
    if not argv:
        raise ValueError("test_cmd must contain an executable")
    started = time.monotonic()
    try:
        result = subprocess.run(
            argv,
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        elapsed = time.monotonic() - started
        output = (result.stdout or "") + (result.stderr or "")
        tail = compress(output[-_TAIL_CHARS:], kind="log")
        return result.returncode, tail, elapsed
    except subprocess.TimeoutExpired as exc:
        elapsed = time.monotonic() - started
        partial = ((exc.stdout or "") if isinstance(exc.stdout, str) else "") + (
            (exc.stderr or "") if isinstance(exc.stderr, str) else ""
        )
        note = f"\n[TIMEOUT after {timeout_s}s running: {test_cmd}]\n"
        tail = compress((partial[-_TAIL_CHARS:] + note), kind="log")
        return None, tail, elapsed
