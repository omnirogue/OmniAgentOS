"""The approval-boundary measurement harness has to be trustworthy itself.

``scripts/approval_corpus_probe.py`` is the evidence behind an INVERTED security
rule: it is what says a boundary costs 0.23% of real traffic and un-parks nothing.
A probe that silently measures the wrong tree, or that quietly counts a crash as a
pass, produces a clean-looking number for a broken boundary -- which is worse than
no number at all. These tests pin the three properties the number depends on.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

_PROBE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "approval_corpus_probe.py"


def _load_probe() -> Any:
    spec = importlib.util.spec_from_file_location("approval_corpus_probe", _PROBE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


probe = _load_probe()


def _transcript(path: Path, commands: list[str]) -> None:
    lines = []
    for index, command in enumerate(commands):
        lines.append(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": f"toolu_{index}",
                                "name": "Bash",
                                "input": {"command": command, "description": "x"},
                            }
                        ],
                    },
                }
            )
        )
    # Noise the harvester must ignore: a non-Bash tool, and an unparseable line.
    lines.append(
        json.dumps(
            {
                "message": {
                    "content": [
                        {"type": "tool_use", "name": "Read", "input": {"file_path": "/etc/hosts"}}
                    ]
                }
            }
        )
    )
    lines.append("{not json at all")
    path.write_text("\n".join(lines) + "\n")


def test_extract_harvests_distinct_bash_commands_and_hashes_the_population(tmp_path: Path) -> None:
    """A corpus without a manifest is an unauditable number by another name."""
    root = tmp_path / "projects" / "proj-a"
    root.mkdir(parents=True)
    _transcript(root / "a.jsonl", ["ls -la", "echo hi", "ls -la"])
    _transcript(root / "b.jsonl", ["echo hi", "git status"])
    out = tmp_path / "corpus.jsonl"

    rc = probe.cmd_extract(
        probe.argparse.Namespace(
            transcripts=[str(tmp_path / "projects")], project_filter=None, out=str(out)
        )
    )

    assert rc == 0
    harvested = sorted(json.loads(line)["command"] for line in out.read_text().splitlines())
    assert harvested == ["echo hi", "git status", "ls -la"]
    manifest = json.loads(Path(str(out) + ".manifest.json").read_text())
    assert manifest["distinct_commands"] == 3
    assert manifest["transcript_files"] == 2
    assert len(manifest["corpus_sha256"]) == 64


def test_the_probe_refuses_to_measure_a_tree_it_did_not_import(tmp_path: Path) -> None:
    """THE named trap: ~90 worktrees plus a machine-wide PYTHONPATH pin.

    A probe that shrugs and measures whatever ``import omniagentos`` happened to
    find reports "no change" for a fix it never loaded. That has already happened
    once on this estate, so an unattributable tree must RAISE, not degrade.
    """
    empty_tree = tmp_path / "not-a-checkout"
    empty_tree.mkdir()

    with pytest.raises(SystemExit) as excinfo:
        probe._pin_tree(empty_tree)

    assert "REFUSING TO MEASURE" in str(excinfo.value)


def test_pinning_the_real_tree_succeeds_and_reports_that_tree() -> None:
    tree = Path(__file__).resolve().parents[2]
    origin = probe._pin_tree(tree)
    assert tree in Path(origin).parents


def _measurement(
    entries: dict[str, bool], triggers: dict[str, str] | None = None
) -> dict[str, Any]:
    triggers = triggers or {}
    return {
        "results": {
            command: {"approved": approved, "trigger": triggers.get(command, "")}
            for command, approved in entries.items()
        }
    }


def test_compare_names_both_directions_and_never_hides_an_un_park() -> None:
    """An un-park is the one number that can never be traded away."""
    base = _measurement({"safe": True, "risky": True, "already": False, "was-parked": False})
    head = _measurement(
        {"safe": True, "risky": False, "already": False, "was-parked": True},
        triggers={"risky": "unrecognised-money-action"},
    )

    report = probe._compare(base, head, samples=5)

    assert report["newly_parked"] == 1
    assert report["newly_parked_samples"] == ["risky"]
    assert report["newly_parked_triggers"] == {"unrecognised-money-action": 1}
    assert report["un_parked"] == 1
    assert report["un_parked_samples"] == ["was-parked"]
    assert report["base_parked"] == 2
    assert report["head_parked"] == 2


def test_compare_exits_non_zero_when_something_un_parks(tmp_path: Path) -> None:
    """A harness read only by its exit code must still fail on a safety regression."""
    base = tmp_path / "base.json"
    head = tmp_path / "head.json"
    base.write_text(json.dumps(_measurement({"cmd": False})))
    head.write_text(json.dumps(_measurement({"cmd": True})))

    assert probe.cmd_compare(probe.argparse.Namespace(base=base, head=head, samples=3)) == 1

    head.write_text(json.dumps(_measurement({"cmd": False})))
    assert probe.cmd_compare(probe.argparse.Namespace(base=base, head=head, samples=3)) == 0


def test_a_classifier_crash_counts_as_parked_not_as_skipped() -> None:
    """Unknown is not a favourable value here either: a crash must not read as approve."""
    probe._WORKER_STATE.clear()

    class _Boom:
        def __init__(self, **_: Any) -> None:
            pass

    def _explode(_request: Any) -> Any:
        raise RuntimeError("classifier exploded")

    probe._WORKER_STATE.update(
        {"resolve": _explode, "request": _Boom, "action_class": "consequential"}
    )
    try:
        command, approved, trigger = probe._judge("rm -rf /")
    finally:
        probe._WORKER_STATE.clear()

    assert command == "rm -rf /"
    assert approved is False
    assert trigger == ""
