"""Registry-truth launch sync and fail-loud startup assertions."""

from __future__ import annotations

import hashlib
import logging
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

from omniagentos import connectors
from omniagentos.connectors import ConnectorError, compute_registry_hash, load_registry
from omniagentos.simgate import (
    SimGateError,
    assert_registry_truth,
    assert_startup_coherence,
)

ROOT = Path(__file__).resolve().parents[1]
REPO_REGISTRY = ROOT / "configs" / "connectors.yaml"
LAUNCH_ENV = ROOT / "scripts" / "launch-env.sh"


@pytest.fixture(autouse=True)
def _clear_registry_cache() -> Iterator[None]:
    load_registry.cache_clear()
    yield
    load_registry.cache_clear()


def _copy_registry(runtime_root: Path) -> Path:
    runtime_root.mkdir(parents=True, exist_ok=True)
    runtime_registry = runtime_root / "connectors.yaml"
    runtime_registry.write_bytes(REPO_REGISTRY.read_bytes())
    return runtime_registry


def test_compute_registry_hash_uses_exact_file_bytes(tmp_path: Path) -> None:
    registry = tmp_path / "registry.yaml"
    content = b"version: 1\n# exact bytes matter\n"
    registry.write_bytes(content)

    assert compute_registry_hash(registry) == hashlib.sha256(content).hexdigest()


def test_launch_sync_and_registry_truth_resolve_current_capabilities(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    runtime_root = tmp_path / "runtime"
    runtime_registry = runtime_root / "connectors.yaml"
    runtime_root.mkdir()
    runtime_registry.write_text("version: stale\n", encoding="utf-8")
    environment = {
        "HOME": os.environ["HOME"],
        "PATH": os.environ["PATH"],
        "OMNIAGENTOS_VAR_DIR": str(runtime_root),
    }
    probe = f'''
set -eu
cd /
. "{LAUNCH_ENV}"
. "{LAUNCH_ENV}"
test -r "$OMNIAGENTOS_VAR_DIR/connectors.yaml"
'''

    result = subprocess.run(
        ["bash", "-c", probe],
        env=environment,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert runtime_registry.is_file()
    assert runtime_registry.read_bytes() == REPO_REGISTRY.read_bytes()

    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(runtime_root))
    with caplog.at_level(logging.INFO, logger="omniagentos.simgate"):
        assert_startup_coherence()

    registry = connectors.load_registry()
    assert registry.capability("jira.read").callable_now
    assert registry.capability("replicate.generate").callable_now
    expected_hash = compute_registry_hash(REPO_REGISTRY)
    assert expected_hash in caplog.text
    assert "loaded_sha256=" in caplog.text
    assert "expected_sha256=" in caplog.text


def test_registry_truth_refuses_divergent_runtime_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_root = tmp_path / "runtime"
    runtime_registry = _copy_registry(runtime_root)
    raw = yaml.safe_load(runtime_registry.read_text(encoding="utf-8"))
    del raw["connectors"]["jira"]["capabilities"]["jira.read"]
    runtime_registry.write_text(yaml.safe_dump(raw), encoding="utf-8")
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(runtime_root))

    loaded_hash = compute_registry_hash(runtime_registry)
    expected_hash = compute_registry_hash(REPO_REGISTRY)
    with pytest.raises(SimGateError, match="registry hash mismatch") as error:
        assert_registry_truth()

    assert loaded_hash in str(error.value)
    assert expected_hash in str(error.value)


def test_registry_truth_refuses_malformed_yaml_as_a_startup_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Malformed runtime YAML refuses, and it refuses in the startup vocabulary.

    A raw ``ConnectorError`` escaping here would skip
    ``assert_startup_coherence``'s ``SimGateError`` handler, so the operator
    would get a bare parser error with no "REFUSING TO START:" prefix and no
    indication that the API declined to boot. The cause is still chained, so
    nothing about the parse failure is lost.
    """
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    (runtime_root / "connectors.yaml").write_text(
        "invalid: yaml: [ unclosed", encoding="utf-8"
    )
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(runtime_root))

    with pytest.raises(SimGateError, match="Unable to read connector registry") as error:
        assert_registry_truth()
    assert isinstance(error.value.__cause__, ConnectorError)


def test_registry_truth_refuses_a_missing_runtime_copy_and_names_the_fix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Absence is not harmless: the runtime var dir IS the registry lookup path.

    ``connectors._default_registry_path()`` resolves the registry under
    ``$OMNIAGENTOS_VAR_DIR``, so a var dir with no ``connectors.yaml`` makes
    every capability lookup fail at request time. Refusing at boot is the whole
    point, and the refusal has to name the thing that mints the copy or the
    operator has no move.
    """
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(runtime_root))

    with pytest.raises(SimGateError, match="runtime copy is missing") as error:
        assert_registry_truth()
    assert "launch-env.sh" in str(error.value)
    assert str(runtime_root / "connectors.yaml") in str(error.value)


def test_startup_coherence_refuses_registry_hash_mismatch(
    tmp_path: Path,
) -> None:
    runtime_root = tmp_path / "runtime"
    runtime_registry = _copy_registry(runtime_root)
    runtime_registry.write_bytes(runtime_registry.read_bytes() + b"\n# counterfeit\n")
    environment = os.environ.copy()
    environment["OMNIAGENTOS_VAR_DIR"] = str(runtime_root)
    environment.pop("OMNIAGENTOS_SIM_MODE", None)

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from omniagentos.simgate import assert_startup_coherence; "
            "assert_startup_coherence()",
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "REFUSING TO START" in result.stderr
    assert "registry hash mismatch" in result.stderr
