"""Write SHA-bound certification evidence for status/doc generation.

This module never runs tests and never infers success from directory presence.
``scripts/certify-omniagentos.sh`` calls it only after pytest returns and after the
post-run git provenance check.  Claim evidence is emitted only when a named
behavioral test path was part of that successful invocation and contains at
least one test module.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = "omniagentos.certification-evidence.v1"
PYTEST_INVENTORY_SCHEMA = "omniagentos.pytest-inventory.v2"
CLAIM_TEST_PATHS: dict[str, tuple[str, ...]] = {
    "Decision Center": ("tests/decision_center", "tests/decision"),
    "TaskContract": ("tests/taskcontract",),
    "Toolplane": ("tests/toolplane",),
    "fan-in": ("tests/fanin",),
    "isolation": ("tests/scope/test_isolation_drill.py",),
    "P12": ("tests/p12",),
}
EXPECTED_MARKER = (
    "not (live_cli or perf or live_ollama or live or counterfeit_gate or feature_health or e2e)"
)
FORBIDDEN_MARKERS = {
    "live_cli",
    "perf",
    "live_ollama",
    "live",
    "counterfeit_gate",
    "feature_health",
    "e2e",
}


def _contains_behavioral_tests(repo_root: Path, relative_path: str) -> bool:
    path = repo_root / relative_path
    if path.is_file():
        return path.name.startswith("test_") and path.suffix == ".py"
    if path.is_dir():
        return any(candidate.is_file() for candidate in path.rglob("test_*.py"))
    return False


def read_passed_testcases(path: Path) -> tuple[set[str], dict[str, int]]:
    """Return non-skipped, non-failed testcase identifiers from pytest JUnit XML."""
    try:
        root = ET.parse(path).getroot()  # noqa: S314 - local pytest artifact
    except (OSError, ET.ParseError):
        return set(), {"tests": 0, "passed": 0, "skipped": 0, "failed": 0}

    passed: set[str] = set()
    counts = {"tests": 0, "passed": 0, "skipped": 0, "failed": 0}
    for testcase in root.iter("testcase"):
        counts["tests"] += 1
        if testcase.find("skipped") is not None:
            counts["skipped"] += 1
            continue
        if testcase.find("failure") is not None or testcase.find("error") is not None:
            counts["failed"] += 1
            continue
        counts["passed"] += 1
        classname = testcase.get("classname", "")
        file_path = testcase.get("file", "")
        if classname:
            passed.add(classname)
        if file_path:
            passed.add(file_path.removesuffix(".py").replace("/", "."))
    return passed, counts


def _test_path_executed(relative_path: str, passed_testcases: set[str]) -> bool:
    marker = relative_path.removesuffix(".py").replace("/", ".")
    return any(
        testcase == marker or testcase.startswith(f"{marker}.") for testcase in passed_testcases
    )


def _nodeid_file(nodeid: str) -> str:
    return nodeid.split("::", 1)[0]


def _nodeid_belongs_to_path(nodeid: str, relative_path: str) -> bool:
    node_file = _nodeid_file(nodeid)
    if relative_path.endswith(".py"):
        return node_file == relative_path
    prefix = relative_path.rstrip("/")
    return node_file == prefix or node_file.startswith(f"{prefix}/")


def _nodeid_list(value: object) -> list[str] | None:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        return None
    return [str(item) for item in value]


def _deselected_nodeids_dict(value: object) -> dict[str, list[str]] | None:
    if not isinstance(value, dict):
        return None
    for k, v in value.items():
        if not isinstance(k, str) or not k:
            return None
        if not isinstance(v, list) or not v:
            return None
        if not all(isinstance(item, str) and item in FORBIDDEN_MARKERS for item in v):
            return None
        if v != sorted(list(set(v))):
            return None
    return {str(k): [str(i) for i in v] for k, v in value.items()}


def _selection_errors(mode: str, payload: dict[str, object]) -> list[str]:
    """Reject any run whose executed surface could have been narrowed."""
    selection = payload.get("selection")
    if not isinstance(selection, dict):
        return [f"{mode} inventory has no selection block"]
    errors: list[str] = []

    if selection.get("keyword") != "":
        errors.append(f"{mode} run used a keyword selector")
    if selection.get("markexpr") != EXPECTED_MARKER:
        errors.append(f"{mode} run used an invalid markexpr (expected exact offline marker)")

    for option in ("deselect", "ignore", "ignore_glob"):
        if selection.get(option) != []:
            errors.append(f"{mode} run used a {option} selector")
    if selection.get("maxfail") != 0:
        errors.append(f"{mode} run used maxfail (partial execution)")
    if selection.get("last_failed") is not False or selection.get("failed_first") is not False:
        errors.append(f"{mode} run used a cached-failure selector")
    if selection.get("collect_only") is not (mode == "expected"):
        errors.append(f"{mode} run has the wrong collect-only mode")
    return errors


def _environment_errors(mode: str, payload: dict[str, object]) -> list[str]:
    """Reject any run that could have loaded a third-party selector plugin."""
    environment = payload.get("environment")
    if not isinstance(environment, dict):
        return [f"{mode} inventory has no environment block"]
    errors: list[str] = []
    if environment.get("plugin_autoload_disabled") is not True:
        errors.append(f"{mode} run allowed pytest plugin autoloading")
    if environment.get("pytest_addopts") != "":
        errors.append(f"{mode} run inherited PYTEST_ADDOPTS")
    if environment.get("pytest_plugins") != "":
        errors.append(f"{mode} run inherited PYTEST_PLUGINS")
    if environment.get("plugin_dists") != []:
        errors.append(f"{mode} run loaded external pytest plugin distributions")
    return errors


def _producer_errors(mode: str, payload: dict[str, object], repo_root: Path) -> list[str]:
    """Reject an inventory that was not produced inside the certified tree."""
    producer = payload.get("producer")
    if not isinstance(producer, dict):
        return [f"{mode} inventory has no producer block"]
    errors: list[str] = []
    plugin_file = producer.get("plugin_file")
    if not isinstance(plugin_file, str) or not plugin_file:
        errors.append(f"{mode} inventory does not name its producing plugin")
    elif not Path(plugin_file).is_relative_to(repo_root):
        errors.append(f"{mode} inventory was produced by a plugin outside the certified tree")
    invocation_dir = producer.get("invocation_dir")
    if not isinstance(invocation_dir, str) or Path(invocation_dir) != repo_root:
        errors.append(f"{mode} run was invoked outside the certified tree")
    return errors


def _read_inventory(
    path: Path, expected_mode: str, repo_root: Path
) -> tuple[dict[str, object] | None, list[str]]:
    try:
        if path.stat().st_size <= 0:
            return None, [f"{expected_mode} inventory is empty"]
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, [f"{expected_mode} inventory is unreadable: {type(exc).__name__}"]
    if not isinstance(payload, dict):
        return None, [f"{expected_mode} inventory is not an object"]
    if payload.get("schema") != PYTEST_INVENTORY_SCHEMA:
        return None, [f"{expected_mode} inventory schema is invalid"]
    if payload.get("mode") != expected_mode:
        return None, [f"{expected_mode} inventory mode is invalid"]
    if payload.get("pytest_exit_code") != 0:
        return None, [f"{expected_mode} pytest exit code is not zero"]

    collected = _nodeid_list(payload.get("collected_nodeids"))
    selected = _nodeid_list(payload.get("selected_nodeids"))
    deselected = _deselected_nodeids_dict(payload.get("deselected_nodeids"))
    outcomes = payload.get("outcomes")
    if collected is None or selected is None or deselected is None:
        return None, [f"{expected_mode} inventory node IDs are invalid"]
    if not isinstance(outcomes, dict) or not all(
        isinstance(key, str) and key and value in {"passed", "skipped", "failed"}
        for key, value in outcomes.items()
    ):
        return None, [f"{expected_mode} inventory outcomes are invalid"]

    errors: list[str] = []
    selection = payload.get("selection")

    # The pre-deselection inventory is the only trustworthy surface, but
    # the exact canonical offline marker is permitted to deselect tests.
    markexpr = selection.get("markexpr", "") if isinstance(selection, dict) else ""

    for nodeid, reasons in deselected.items():
        if not reasons:
            errors.append(
                f"{expected_mode} inventory node ID {nodeid} is deselected "
                f"but carries no fixed forbidden markers"
            )

    if deselected and markexpr != EXPECTED_MARKER:
        errors.append(f"{expected_mode} run deselected {len(deselected)} tests")
    if Counter(selected) + Counter(deselected.keys()) != Counter(collected):
        errors.append(f"{expected_mode} run collected node count != selected + deselected")
    if set(selected).intersection(set(deselected.keys())):
        errors.append(f"{expected_mode} run selected and deselected sets are not disjoint")
    if Counter(selected) != Counter(collected) and markexpr != EXPECTED_MARKER:
        errors.append(f"{expected_mode} run selected fewer tests than it collected")
    errors.extend(_selection_errors(expected_mode, payload))
    errors.extend(_environment_errors(expected_mode, payload))
    errors.extend(_producer_errors(expected_mode, payload, repo_root))
    if errors:
        return None, errors
    return payload, []


def compare_inventories(
    *,
    expected_path: Path,
    execution_path: Path,
    executed_paths: list[str],
    repo_root: Path,
) -> tuple[bool, dict[str, int], dict[str, int], dict[str, int], set[str], list[str]]:
    """Validate complete, selected offline surface against trusted collection.

    Includes independently attested fixed-marker exclusions.

    Returns ``(complete, expected_by_path, actual_by_path, test_counts,
    passed_testcases, errors)``.  Every requested path must own at least one
    unique test, collection and execution node IDs must match exactly, and
    every selected offline test must have one passing outcome, with all deselections
    independently attested as fixed forbidden markers.
    """
    repo_root = repo_root.resolve()
    errors: list[str] = []
    expected, read_errors = _read_inventory(expected_path, "expected", repo_root)
    errors.extend(read_errors)
    execution, read_errors = _read_inventory(execution_path, "execution", repo_root)
    errors.extend(read_errors)
    if expected is None or execution is None:
        return (
            False,
            {},
            {},
            {"tests": 0, "passed": 0, "skipped": 0, "failed": 0},
            set(),
            errors,
        )

    expected_nodeids = expected["collected_nodeids"]
    execution_nodeids = execution["collected_nodeids"]
    expected_selected = expected["selected_nodeids"]
    execution_selected = execution["selected_nodeids"]
    expected_deselected = expected["deselected_nodeids"]
    execution_deselected = execution["deselected_nodeids"]
    outcomes = execution["outcomes"]
    assert isinstance(expected_nodeids, list)
    assert isinstance(execution_nodeids, list)
    assert isinstance(expected_selected, list)
    assert isinstance(execution_selected, list)
    assert isinstance(expected_deselected, dict)
    assert isinstance(execution_deselected, dict)
    assert isinstance(outcomes, dict)

    if not expected_nodeids:
        errors.append("expected collection contains zero tests")
    if len(set(expected_nodeids)) != len(expected_nodeids):
        errors.append("expected collection contains duplicate node IDs")
    if Counter(expected_nodeids) != Counter(execution_nodeids):
        errors.append("execution collection differs from expected collection (partial/deselected)")
    if Counter(expected_selected) != Counter(execution_selected):
        errors.append("execution selection differs from expected selection")
    if expected_deselected != execution_deselected:
        errors.append(
            "execution deselection differs from expected deselection (IDs or reasons mismatch)"
        )
    if set(outcomes) != set(execution_selected):
        errors.append("execution outcomes do not cover every selected node ID")

    expected_by_path = dict.fromkeys(executed_paths, 0)
    actual_by_path = dict.fromkeys(executed_paths, 0)
    for nodeid in expected_selected:
        owners = [path for path in executed_paths if _nodeid_belongs_to_path(nodeid, path)]
        if len(owners) != 1:
            errors.append(f"expected node ID has {len(owners)} requested-path owners: {nodeid}")
            continue
        expected_by_path[owners[0]] += 1
    for nodeid in execution_selected:
        owners = [path for path in executed_paths if _nodeid_belongs_to_path(nodeid, path)]
        if len(owners) == 1 and outcomes.get(nodeid) == "passed":
            actual_by_path[owners[0]] += 1

    for path in executed_paths:
        expected_count = expected_by_path[path]
        actual_count = actual_by_path[path]
        if expected_count <= 0:
            errors.append(f"requested test path selected zero tests: {path}")
        if actual_count != expected_count:
            errors.append(
                f"requested test path was not fully passed: {path} "
                f"(expected={expected_count}, passed={actual_count})"
            )

    counts = {"tests": len(execution_selected), "passed": 0, "skipped": 0, "failed": 0}
    passed_testcases: set[str] = set()
    for nodeid in execution_selected:
        outcome = outcomes.get(nodeid)
        if outcome in counts:
            counts[outcome] += 1
        if outcome == "passed":
            passed_testcases.add(_nodeid_file(nodeid).removesuffix(".py").replace("/", "."))
    if counts["skipped"] or counts["failed"]:
        errors.append("certification execution contains skipped or failed tests")
    if counts["passed"] != counts["tests"]:
        errors.append("not every selected certification test passed")

    return (
        not errors,
        expected_by_path,
        actual_by_path,
        counts,
        passed_testcases,
        errors,
    )


def build_manifest(
    *,
    repo_root: Path,
    repository_sha: str,
    pytest_exit_code: int,
    clean_tree: bool,
    executed_paths: list[str],
    passed_testcases: set[str],
    test_counts: dict[str, int] | None = None,
    suite_complete: bool = False,
    expected_test_counts: dict[str, int] | None = None,
    actual_test_counts: dict[str, int] | None = None,
    inventory_errors: list[str] | None = None,
) -> dict[str, object]:
    """Build a fail-closed evidence manifest without writing it."""
    executed = set(executed_paths)
    counts = test_counts or {}
    expected_counts = expected_test_counts or {}
    actual_counts = actual_test_counts or {}
    errors = inventory_errors or []
    count_maps_complete = (
        len(executed) == len(executed_paths)
        and set(expected_counts) == executed
        and set(actual_counts) == executed
        and all(type(value) is int and value > 0 for value in expected_counts.values())
        and expected_counts == actual_counts
        and sum(expected_counts.values()) == counts.get("tests")
    )
    run_passed = (
        pytest_exit_code == 0
        and clean_tree
        and re.fullmatch(r"[0-9a-f]{40}", repository_sha) is not None
        and suite_complete
        and not errors
        and count_maps_complete
        and counts.get("tests", 0) > 0
        and counts.get("passed") == counts.get("tests")
        and counts.get("skipped") == 0
        and counts.get("failed") == 0
    )
    claims: dict[str, dict[str, object]] = {}
    for claim, candidates in CLAIM_TEST_PATHS.items():
        evidence = [
            path
            for path in candidates
            if path in executed
            and _contains_behavioral_tests(repo_root, path)
            and _test_path_executed(path, passed_testcases)
        ]
        claims[claim] = {
            "status": "passed" if run_passed and evidence else "not_certified",
            "evidence": evidence if run_passed else [],
        }
    return {
        "schema": SCHEMA,
        "source": "scripts/certify-omniagentos.sh",
        "repository_sha": repository_sha,
        "clean_tree": clean_tree,
        "pytest_exit_code": pytest_exit_code,
        "certified": run_passed,
        "generated_at": datetime.now(UTC).isoformat(),
        "executed_paths": executed_paths,
        "suite_complete": suite_complete,
        "expected_test_counts": expected_counts,
        "actual_test_counts": actual_counts,
        "inventory_errors": errors,
        "test_counts": counts,
        "claims": claims,
    }


def write_manifest(path: Path, manifest: dict[str, object]) -> None:
    """Atomically replace an ignored runtime evidence file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--repository-sha", required=True)
    parser.add_argument("--pytest-exit-code", required=True, type=int)
    parser.add_argument("--clean-tree", choices=("true", "false"), required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--junit-xml", required=True, type=Path)
    parser.add_argument("--expected-inventory", required=True, type=Path)
    parser.add_argument("--execution-inventory", required=True, type=Path)
    parser.add_argument("--executed-path", action="append", default=[])
    args = parser.parse_args()

    (
        suite_complete,
        expected_test_counts,
        actual_test_counts,
        test_counts,
        passed_testcases,
        inventory_errors,
    ) = compare_inventories(
        expected_path=args.expected_inventory,
        execution_path=args.execution_inventory,
        executed_paths=args.executed_path,
        repo_root=args.repo_root,
    )
    junit_passed, junit_counts = read_passed_testcases(args.junit_xml)
    if not args.junit_xml.is_file() or args.junit_xml.stat().st_size <= 0:
        inventory_errors.append("JUnit XML output is missing or empty")
        suite_complete = False
    junit_covers_inventory = all(
        any(
            marker == junit_marker or junit_marker.startswith(f"{marker}.")
            for junit_marker in junit_passed
        )
        for marker in passed_testcases
    )
    if junit_counts != test_counts or not junit_covers_inventory:
        inventory_errors.append("JUnit XML outcomes do not match trusted execution inventory")
        suite_complete = False

    manifest = build_manifest(
        repo_root=args.repo_root,
        repository_sha=args.repository_sha,
        pytest_exit_code=args.pytest_exit_code,
        clean_tree=args.clean_tree == "true",
        executed_paths=args.executed_path,
        passed_testcases=passed_testcases,
        test_counts=test_counts,
        suite_complete=suite_complete,
        expected_test_counts=expected_test_counts,
        actual_test_counts=actual_test_counts,
        inventory_errors=inventory_errors,
    )
    write_manifest(args.output, manifest)
    if manifest["certified"] is not True:
        print(
            "certification evidence rejected: " + "; ".join(inventory_errors),
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
