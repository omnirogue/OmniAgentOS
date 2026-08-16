from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _fake_uv(tmp_path: Path) -> tuple[Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "uv-calls"
    executable = bin_dir / "uv"
    executable.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$*\" >> {calls!s}\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return bin_dir, calls


def test_make_bench_refuses_an_implicit_arm_before_invoking_uv(tmp_path: Path) -> None:
    bin_dir, calls = _fake_uv(tmp_path)
    result = subprocess.run(
        ["make", "bench"],
        cwd=ROOT,
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "BENCH_ARM is required" in result.stderr
    assert not calls.exists()


def test_make_bench_uses_frozen_fixture_capture_with_explicit_arm(tmp_path: Path) -> None:
    bin_dir, calls = _fake_uv(tmp_path)
    result = subprocess.run(
        ["make", "bench", "BENCH_ARM=oracle", "BENCH_ARGS=--limit 2"],
        cwd=ROOT,
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    invocation = calls.read_text(encoding="utf-8")
    assert (
        "run python -m scripts.benchmarks.capture_baseline --arm oracle --limit 2" in invocation
    )
    assert "omniagentos.harnesses.bench" not in invocation
    assert "devtasks" not in invocation
