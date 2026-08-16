"""Scriptable process double for the real provider-exec seam.

The production :class:`ProviderSessionRunner` accepts ``process_factory`` and
``adapter_resolver`` collaborators.  This module reuses those seams: no
subprocess is launched, but provider-exec still owns session creation,
streaming, parsing, usage capture, and terminalization.
"""

from __future__ import annotations

import io
import json
import signal
import threading
import time
from dataclasses import dataclass
from typing import Any

from omniagentos.adapters.common import ParsedResponse


@dataclass(frozen=True)
class AttemptResult:
    """One canned provider executable result, consumed in launch order.

    ``raw_stdout`` bypasses the JSON payload builder (malformed-JSON scenarios).
    ``hang=True`` never yields stdout until the process is killed (timeout/cancel).
    """

    text: str = "done"
    returncode: int = 0
    input_tokens: int | None = 100
    output_tokens: int | None = 50
    cost_usd: float | None = 0.25
    raw_stdout: str | None = None
    hang: bool = False

    def stdout(self) -> str:
        if self.raw_stdout is not None:
            return self.raw_stdout
        usage: dict[str, Any] = {}
        if self.input_tokens is not None:
            usage["input_tokens"] = self.input_tokens
        if self.output_tokens is not None:
            usage["output_tokens"] = self.output_tokens
        payload: dict[str, Any] = {
            "text": self.text,
            "session_ref": "stub-session",
            "usage": usage,
        }
        if self.cost_usd is not None:
            payload["total_cost_usd"] = self.cost_usd
        return json.dumps(payload, sort_keys=True) + "\n"


@dataclass
class AttemptInterval:
    """Strict process interval used to measure actual concurrency."""

    launch_index: int
    model: str
    started_ns: int | None = None
    ended_ns: int | None = None


class StubAdapter:
    """Minimal adapter that keeps provider-exec's command/parse lifecycle real."""

    def __init__(self, provider: str) -> None:
        self.name = provider
        self.cli = f"stub-{provider}"

    def _command(self, agent_input: Any, prompt: str, session_ref: str | None) -> list[str]:
        del prompt, session_ref
        return [
            self.cli,
            "--model",
            str(agent_input.model or "default"),
            "--task-id",
            str(agent_input.task_id),
        ]

    def _sandboxed_launch(
        self,
        command: list[str],
        working_dir: str,
        extra_write_roots: list[str] | None = None,
        **kwargs: Any,
    ) -> list[str]:
        del working_dir, extra_write_roots, kwargs
        return command

    def _parse(self, stdout: str) -> ParsedResponse:
        decoded = json.loads(stdout)
        return ParsedResponse(
            text=str(decoded["text"]),
            session_ref=decoded.get("session_ref"),
            payload=decoded,
        )


class _ScriptedStdout:
    def __init__(
        self,
        process: _StubProcess,
        result: AttemptResult,
        interval: AttemptInterval,
        barrier: threading.Barrier | None,
    ) -> None:
        self._process = process
        self._result = result
        self._interval = interval
        self._barrier = barrier
        self._state = 0
        self._hang_release = threading.Event()

    def __iter__(self) -> _ScriptedStdout:
        return self

    def __next__(self) -> str:
        if self._state == 0:
            self._state = 1
            self._interval.started_ns = time.monotonic_ns()
            if self._barrier is not None:
                self._barrier.wait(timeout=3)
            if self._result.hang:
                # Block the provider reader until kill/stop; do not emit a body.
                self._hang_release.wait(timeout=30)
                self._state = 2
                if self._interval.ended_ns is None:
                    self._interval.ended_ns = time.monotonic_ns()
                raise StopIteration
            return self._result.stdout()
        if self._state == 1:
            self._state = 2
            self._interval.ended_ns = time.monotonic_ns()
            self._process.returncode = self._result.returncode
        raise StopIteration

    def stop(self, sig: int) -> None:
        if self._state < 2:
            self._state = 2
            self._interval.ended_ns = time.monotonic_ns()
        self._process.returncode = -sig
        self._hang_release.set()
        if self._barrier is not None:
            try:
                self._barrier.abort()
            except threading.BrokenBarrierError:
                pass


class _StubProcess:
    def __init__(
        self,
        *,
        pid: int,
        result: AttemptResult,
        interval: AttemptInterval,
        barrier: threading.Barrier | None,
    ) -> None:
        self.pid = pid
        self.stdin = io.StringIO()
        # Hang attempts stay non-terminal until stop()/killpg.
        self.returncode: int | None = None if result.hang else None
        self.stdout = _ScriptedStdout(self, result, interval, barrier)
        if not result.hang:
            # returncode is assigned when stdout is fully drained.
            self.returncode = None

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        return self.returncode if self.returncode is not None else 1

    def stop(self, sig: int = signal.SIGTERM) -> None:
        self.stdout.stop(sig)


class ScriptedProcessFactory:
    """Thread-safe per-attempt provider process script."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._results: list[AttemptResult] = []
        self._barrier_size = 0
        self._barrier: threading.Barrier | None = None
        self._processes: dict[int, _StubProcess] = {}
        self.intervals: list[AttemptInterval] = []

    def script(self, results: list[AttemptResult], *, barrier_size: int = 0) -> None:
        with self._lock:
            self._results = list(results)
            self._barrier_size = max(0, int(barrier_size))
            self._barrier = (
                threading.Barrier(self._barrier_size) if self._barrier_size > 1 else None
            )
            self._processes.clear()
            self.intervals.clear()

    def __call__(self, command: list[str], **kwargs: Any) -> _StubProcess:
        del kwargs
        with self._lock:
            if not self._results:
                raise AssertionError(f"provider stub script exhausted for command {command!r}")
            result = self._results.pop(0)
            launch_index = len(self.intervals)
            try:
                model = command[command.index("--model") + 1]
            except (ValueError, IndexError):
                model = "unknown"
            interval = AttemptInterval(launch_index=launch_index, model=model)
            self.intervals.append(interval)
            pid = 70_000 + launch_index
            barrier = self._barrier if launch_index < self._barrier_size else None
            process = _StubProcess(
                pid=pid,
                result=result,
                interval=interval,
                barrier=barrier,
            )
            self._processes[pid] = process
            return process

    def killpg(self, pgid: int, sig: int) -> None:
        with self._lock:
            process = self._processes.get(pgid)
        if process is None:
            raise ProcessLookupError(pgid)
        process.stop(sig)

    def pgid_alive(self, pgid: int) -> bool:
        with self._lock:
            process = self._processes.get(pgid)
            return process is not None and process.poll() is None

    @property
    def remaining(self) -> int:
        with self._lock:
            return len(self._results)

    def strict_max_overlap(self) -> int:
        """Maximum strict overlap, with interval ends ordered before starts."""
        events: list[tuple[int, int]] = []
        with self._lock:
            intervals = list(self.intervals)
        for interval in intervals:
            if interval.started_ns is None or interval.ended_ns is None:
                continue
            events.append((interval.started_ns, 1))
            events.append((interval.ended_ns, -1))
        active = high_water = 0
        for _, delta in sorted(events, key=lambda event: (event[0], event[1])):
            active += delta
            high_water = max(high_water, active)
        return high_water


def full_usage(text: str = "done") -> AttemptResult:
    return AttemptResult(text=text, input_tokens=100, output_tokens=50, cost_usd=0.25)


def token_only(text: str = "done") -> AttemptResult:
    return AttemptResult(text=text, input_tokens=100, output_tokens=50, cost_usd=None)


def hang(text: str = "hanging") -> AttemptResult:
    """Provider process that never completes until killed (timeout/cancel)."""
    del text
    return AttemptResult(hang=True)


def malformed_json(text: str = "not-json{{{") -> AttemptResult:
    """Stdout that is not valid JSON; StubAdapter._parse will raise."""
    return AttemptResult(raw_stdout=text + "\n", returncode=0)


__all__ = [
    "AttemptInterval",
    "AttemptResult",
    "ScriptedProcessFactory",
    "StubAdapter",
    "full_usage",
    "hang",
    "malformed_json",
    "token_only",
]
