"""End-to-end: scan a tmp corpus through the CLI and check the artifacts."""

from __future__ import annotations

import json
from pathlib import Path

from omniagentos.tracelab.__main__ import main


def _fixture_corpus(tmp_path: Path) -> Path:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    records = [
        {
            "type": "user",
            "uuid": "u1",
            "parentUuid": None,
            "sessionId": "s1",
            "message": {"role": "user", "content": "task"},
        },
        {
            "type": "assistant",
            "uuid": "a1",
            "parentUuid": "u1",
            "sessionId": "s1",
            "message": {
                "role": "assistant",
                "model": "claude-fable-5",
                "content": [
                    {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}
                ],
            },
        },
        {
            "type": "user",
            "uuid": "u2",
            "parentUuid": "a1",
            "sessionId": "s1",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}],
            },
        },
    ]
    (corpus / "session.jsonl").write_text("\n".join(json.dumps(r) for r in records))
    return corpus


def test_scan_cli_writes_artifacts(tmp_path: Path, capsys: object) -> None:
    corpus = _fixture_corpus(tmp_path)
    out = tmp_path / "out"
    code = main(["scan", "--corpus", str(corpus), "--out", str(out)])
    assert code == 0
    reports = list(out.glob("report-*.md"))
    assert len(reports) == 1
    assert (out / "hypotheses.json").exists()
    summary = json.loads((out / "mining-summary.json").read_text())
    assert summary["n_traces"] == 1
    assert summary["dataset_counts"] == {"claude-code": 1}


def test_overlapping_corpus_roots_do_not_double_count(tmp_path: Path) -> None:
    from omniagentos.tracelab.scan import scan_corpus

    corpus = _fixture_corpus(tmp_path)
    traces = list(scan_corpus([corpus, corpus, tmp_path]))
    assert len(traces) == 1
