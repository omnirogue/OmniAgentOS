import json
from pathlib import Path

import pytest

from omniagentos.connectors import Connector, ConnectorRegistry, Group
from omniagentos.connectors.inventory import (
    _classify_names,
    _grep_repo_for_names,
    _parse_env_file,
    _run_inventory,
    write_report,
)


@pytest.fixture
def fixture_env(tmp_path: Path) -> Path:
    path = tmp_path / "connections.env"
    path.write_text(
        "# ignored\nREGISTRY_KEY=secret123\nREPO_ONLY_KEY=https://secret.example\n"
        "LOCAL_KEY=personal\nexport EXPORTED_KEY=value\nEMPTY_KEY=\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def fixture_registry() -> ConnectorRegistry:
    return ConnectorRegistry(
        version=1,
        groups={"test": Group(label="Test")},
        connectors={
            "test": Connector(
                label="Test",
                group="test",
                env=["REGISTRY_KEY", "MISSING_REGISTRY_KEY"],
                capabilities={},
            )
        },
    )


def test_parse_env_file_basic_and_comments(fixture_env: Path) -> None:
    assert _parse_env_file(fixture_env) == frozenset(
        {"REGISTRY_KEY", "REPO_ONLY_KEY", "LOCAL_KEY", "EXPORTED_KEY", "EMPTY_KEY"}
    )


def test_parse_env_file_never_returns_values(fixture_env: Path) -> None:
    names = _parse_env_file(fixture_env)
    assert "secret123" not in names
    assert "https://secret.example" not in names


def test_grep_repo_for_names_and_exclusions(tmp_path: Path) -> None:
    (tmp_path / "source.py").write_text("REPO_ONLY_KEY = 1\n", encoding="utf-8")
    for directory in (".git", "__pycache__", ".venv"):
        hidden = tmp_path / directory
        hidden.mkdir()
        (hidden / "hidden.txt").write_text("HIDDEN_KEY\n", encoding="utf-8")
    assert _grep_repo_for_names({"REPO_ONLY_KEY", "HIDDEN_KEY"}, tmp_path) == frozenset(
        {"REPO_ONLY_KEY"}
    )


def test_grep_repo_for_names_fallback_parity_with_rg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The BSD/GNU `grep` fallback (used when `rg` is not on PATH) must return
    the exact same result as the normal `rg` path for the same tree.

    Includes a binary-file false-positive regression: a binary file whose
    CONTENT matches ``REPO_ONLY_KEY`` but whose PATH happens to contain a
    DIFFERENT search name (``BOGUS_PATH_KEY``) must not report that unrelated
    name as found. Without ``-I``, `grep -r` treats the matching binary file
    specially and prints a diagnostic line ("Binary file <path> matches")
    instead of the actual matched content -- and that diagnostic line
    contains the full file PATH, so naive text-based extraction against it
    would falsely attribute a match to any search name that merely happens to
    appear in the path (here, ``BOGUS_PATH_KEY``, which is not present
    anywhere in the file's actual bytes). ``-I`` skips binary files entirely,
    so neither name is reported for that file, matching `rg`'s behavior
    (`rg --only-matching` never surfaces path text as a content match)."""
    (tmp_path / "source.py").write_text("REPO_ONLY_KEY = 1\n", encoding="utf-8")
    for directory in (".git", "__pycache__", ".venv"):
        hidden = tmp_path / directory
        hidden.mkdir()
        (hidden / "hidden.txt").write_text("HIDDEN_KEY\n", encoding="utf-8")
    # A binary file whose CONTENT matches REPO_ONLY_KEY but whose PATH
    # contains an unrelated search name (BOGUS_PATH_KEY) that is NOT present
    # in the file's actual bytes.
    binary_dir = tmp_path / "assets"
    binary_dir.mkdir()
    (binary_dir / "BOGUS_PATH_KEY.bin").write_bytes(b"REPO_ONLY_KEY\x00binarystuff")

    names = {"REPO_ONLY_KEY", "HIDDEN_KEY", "BOGUS_PATH_KEY"}

    rg_result = _grep_repo_for_names(names, tmp_path)
    assert rg_result == frozenset({"REPO_ONLY_KEY"})

    monkeypatch.setattr("omniagentos.connectors.inventory.shutil.which", lambda _tool: None)
    grep_result = _grep_repo_for_names(names, tmp_path)
    assert grep_result == rg_result == frozenset({"REPO_ONLY_KEY"})


def test_classify_basic_and_exactly_once() -> None:
    report = _classify_names(
        frozenset({"IN_VAULT", "LOCAL_KEY"}),
        frozenset({"IN_VAULT", "MISSING_KEY"}),
        frozenset({"LOCAL_KEY", "REPO_KEY"}),
    )
    assert report.adopt.names == frozenset({"IN_VAULT", "MISSING_KEY"})
    assert report.declare.names == frozenset({"LOCAL_KEY", "REPO_KEY"})
    assert report.archive.names == frozenset()
    assert report.total_scanned == 4
    all_bucket_names = (
        report.adopt.names | report.declare.names | report.archive.names | report.dead.names
    )
    assert len(all_bucket_names) == report.total_scanned


def test_classify_overrides_and_archive_invariant() -> None:
    report = _classify_names(frozenset({"OLD_KEY"}), frozenset(), frozenset(), {"OLD_KEY": "DEAD"})
    assert report.dead.names == frozenset({"OLD_KEY"})
    invalid = _classify_names(
        frozenset({"KEY"}), frozenset(), frozenset({"KEY"}), {"KEY": "ARCHIVE"}
    )
    assert not invalid.is_valid
    assert invalid.archive.names == frozenset()


def test_run_and_write_report(
    tmp_path: Path, fixture_env: Path, fixture_registry: ConnectorRegistry
) -> None:
    (tmp_path / "source.py").write_text("REPO_ONLY_KEY\n", encoding="utf-8")
    report = _run_inventory(fixture_env, fixture_registry, tmp_path)
    output = write_report(report, tmp_path)
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert output.parent == tmp_path / "var"
    assert payload["total_names_scanned"] == report.total_scanned
    assert set(payload["details"]) == {"ADOPT", "DECLARE", "ARCHIVE", "DEAD"}
    serialized = output.read_text(encoding="utf-8")
    assert "secret123" not in serialized
    assert "https://secret.example" not in serialized


def test_default_report_follows_the_runtime_var_root(
    tmp_path: Path,
    fixture_env: Path,
    fixture_registry: ConnectorRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An implicit report destination belongs to this process, not the checkout."""
    runtime_root = tmp_path / "isolated-runtime"
    monkeypatch.setenv("OMNIAGENTOS_VAR", str(runtime_root))
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(runtime_root))
    report = _run_inventory(fixture_env, fixture_registry, tmp_path)

    output = write_report(report)

    assert output.parent == runtime_root
    assert output.is_file()
