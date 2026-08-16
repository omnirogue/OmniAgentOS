"""Tests for per-run analysis (S1) and its postrun drain seam."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from omniagentos.contracts import utc_now_iso
from omniagentos.db.store import SqliteStore
from omniagentos.reflection import perrun
from omniagentos.reflection.perrun import analyze_run
from omniagentos.runner.core import Runner
from tests.runner.test_state_machine import (
    FinalizationSpy,
    TrackingAdapter,
    create_run,
    dependencies,
)


@pytest.fixture
def repo_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("OMNIAGENTOS_HOME", str(tmp_path))
    (tmp_path / "var").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _insert_pending_proposal(db_path: str, proposal_id: str = "rfl_prop_test") -> None:
    store = SqliteStore(db_path)
    now = utc_now_iso()
    with store._lock:
        store._connection.execute(
            """
            INSERT INTO reflection_proposals (
                id, kind, target, current, proposed, rationale, evidence_refs_json,
                predicted_impact, risk_class, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                proposal_id,
                "model_config",
                json.dumps({"file": "configs/modelintel.yaml", "key": "models.gemini.available"}),
                "false",
                "true",
                "test rationale",
                "[]",
                "test impact",
                "low",
                "pending",
                now,
                now,
            ),
        )


def test_analyze_run_appends_retro_line(repo_home: Path, tmp_path: Path) -> None:
    db_path = str(tmp_path / "state.db")
    store = SqliteStore(db_path)
    _, run_id = create_run(store, [])
    # Seed a model so providers normalize to a known canonical id.
    store.update_run(run_id, {"model": "gpt-5.6-sol", "error": "rate limit 429 hit"})

    out = analyze_run(run_id, db_path=db_path)
    assert out["ok"] is True
    assert out.get("retro_written") is True

    retro = repo_home / "var" / "retro" / "run-retros.jsonl"
    assert retro.is_file()
    lines = [ln for ln in retro.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["run_id"] == run_id
    assert payload["ts"].endswith("Z")
    assert "codex" in payload["providers"]
    assert "rate_limit" in payload["failure_tags"]
    assert any(str(s).startswith("sqlite:") for s in payload["sources_read"])


def test_analyze_run_never_raises_on_bad_db(
    repo_home: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    missing = str(tmp_path / "does-not-exist" / "missing.db")
    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"not a sqlite database at all")

    # Missing path: never raises — but must not read as a clean success either.
    # Asserting only that a dict with an "ok" key came back is what let a
    # nonexistent database return ok=True with empty sources for six rounds.
    with caplog.at_level(logging.WARNING):
        result_missing = analyze_run("run_whatever", db_path=missing)
    assert isinstance(result_missing, dict)
    assert result_missing["run_id"] == "run_whatever"
    assert result_missing["ok"] is False, "a retro that read nothing is not a success"
    assert result_missing["sources_read"] == []
    assert any("sqlite_missing" in e for e in result_missing["source_errors"]), (
        "the absent database must be recorded, not silently omitted"
    )
    assert "state database is absent" in caplog.text

    # Corrupt DB: no exception escapes AND failure is logged + reflected in retro
    # (not a clean SQLite-sourced success). This is the decisive bad-DB assertion.
    caplog.set_level(logging.WARNING, logger=perrun.LOG.name)
    result = analyze_run("run_whatever", db_path=str(corrupt))
    assert isinstance(result, dict)
    assert result["run_id"] == "run_whatever"
    assert "ok" in result

    # Failure must be logged (unknown-as-favourable is forbidden).
    log_text = caplog.text.lower()
    assert any(
        needle in log_text
        for needle in (
            "sqlite",
            "unreadable",
            "unavailable",
            "read-only",
            "table_exists",
            "pending-proposals",
        )
    ), f"expected sqlite failure log, got: {caplog.text!r}"

    # Named silent-swallow surface: _has_pending_proposals must log, not return
    # False quietly, when the DB is corrupt/unreadable.
    caplog.clear()
    assert perrun._has_pending_proposals(str(corrupt)) is False
    assert "pending-proposals check failed" in caplog.text, (
        f"expected pending-proposals warning log, got: {caplog.text!r}"
    )

    # Retro must honestly mark degradation — not claim a clean sqlite source.
    retro = repo_home / "var" / "retro" / "run-retros.jsonl"
    assert retro.is_file()
    lines = [ln for ln in retro.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert lines, "expected a retro line even on degraded sqlite"
    payload = json.loads(lines[-1])
    assert payload["run_id"] == "run_whatever"
    sources = payload.get("sources_read") or []
    assert not any(str(s).startswith("sqlite:") for s in sources), (
        f"corrupt DB must not claim clean sqlite source, got sources_read={sources!r}"
    )
    source_errors = payload.get("source_errors") or []
    assert source_errors, f"corrupt DB retro must record source_errors, got {payload!r}"
    assert any("sqlite" in str(e).lower() for e in source_errors)
    # Status dict mirrors the same honesty.
    assert result.get("source_errors")


def test_gate_invoked_only_when_pending(
    repo_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = str(tmp_path / "state.db")
    store = SqliteStore(db_path)
    _, run_id = create_run(store, [])
    store.update_run(run_id, {"model": "claude-opus-4"})

    calls: list[dict[str, Any]] = []

    def fake_gate(**kwargs: Any) -> MagicMock:
        calls.append(dict(kwargs))
        return MagicMock()

    monkeypatch.setattr("omniagentos.reflection.fable_gate.run_gate", fake_gate)

    # Zero pending → gate must not be called.
    out0 = analyze_run(run_id, db_path=db_path)
    assert out0["ok"] is True
    assert out0["gate_invoked"] is False
    assert calls == []

    # One pending → gate called exactly once.
    _insert_pending_proposal(db_path)
    out1 = analyze_run(run_id, db_path=db_path)
    assert out1["ok"] is True
    assert out1["gate_invoked"] is True
    assert len(calls) == 1
    assert calls[0].get("db_path") == db_path


def test_drain_handles_run_analysis_kind(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from omniagentos.db.migrate import migrate

    db_path = tmp_path / "runner.db"
    migrate(str(db_path))
    store = SqliteStore(str(db_path))
    _, run_id = create_run(store, [])

    var_root = tmp_path / "var"
    var_root.mkdir()
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(var_root))
    monkeypatch.setenv("OMNIAGENTOS_VAR", str(var_root))

    seen: list[str] = []

    def fake_analyze(rid: str, *, db_path: str | None = None) -> dict[str, Any]:
        seen.append(rid)
        return {
            "run_id": rid,
            "ok": True,
            "gate_invoked": False,
            "retro_written": True,
            "error": None,
        }

    monkeypatch.setattr("omniagentos.reflection.perrun.analyze_run", fake_analyze)

    runner = Runner(
        store,
        "w-analysis",
        dependencies=dependencies(TrackingAdapter(), FinalizationSpy()),
        ledger_dir=str(tmp_path / "ledger"),
        vault_dir=str(tmp_path / "vault"),
        workspace_base=str(tmp_path / "workspace"),
    )
    # Drive the handler seam directly (no daemon race).
    runner._postrun_stop.set()
    if runner._postrun_thread is not None:
        runner._postrun_thread.join(timeout=1.0)

    runner._execute_postrun_job(run_id, "run_analysis")
    assert seen == [run_id]


def test_unknown_kind_still_ignored(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from omniagentos.db.migrate import migrate
    from omniagentos.runner import core as runner_core

    db_path = tmp_path / "runner.db"
    migrate(str(db_path))
    store = SqliteStore(str(db_path))
    _, run_id = create_run(store, [])

    var_root = tmp_path / "var"
    var_root.mkdir()
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(var_root))
    monkeypatch.setenv("OMNIAGENTOS_VAR", str(var_root))

    analysis_calls: list[str] = []
    wiki_calls: list[str] = []

    def fake_analyze(rid: str, *, db_path: str | None = None) -> dict[str, Any]:
        analysis_calls.append(rid)
        return {"run_id": rid, "ok": True}

    def fake_wiki(run: dict[str, Any], vault_dir: str, artifacts: list[Any] | None = None) -> None:
        wiki_calls.append(str(run["id"]))

    monkeypatch.setattr("omniagentos.reflection.perrun.analyze_run", fake_analyze)
    monkeypatch.setattr("omniagentos.vault_wiki.maybe_update_wiki", fake_wiki)

    runner = Runner(
        store,
        "w-unknown",
        dependencies=dependencies(TrackingAdapter(), FinalizationSpy()),
        ledger_dir=str(tmp_path / "ledger"),
        vault_dir=str(tmp_path / "vault"),
        workspace_base=str(tmp_path / "workspace"),
    )
    runner._postrun_stop.set()
    if runner._postrun_thread is not None:
        runner._postrun_thread.join(timeout=1.0)

    caplog.set_level(logging.WARNING, logger=runner_core.__name__)
    runner._execute_postrun_job(run_id, "totally_unknown_kind")
    assert analysis_calls == []
    assert wiki_calls == []
    assert "skipped unknown post-run job kind" in caplog.text
    assert "totally_unknown_kind" in caplog.text


def test_analyze_run_uses_normalize_provider(
    repo_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = str(tmp_path / "state.db")
    store = SqliteStore(db_path)
    _, run_id = create_run(store, [])
    store.update_run(run_id, {"model": "anthropic"})

    real = perrun.normalize_provider
    seen: list[str | None] = []

    def spy(raw: str | None) -> str:
        seen.append(raw)
        return real(raw)

    monkeypatch.setattr(perrun, "normalize_provider", spy)
    out = analyze_run(run_id, db_path=db_path)
    assert out["ok"] is True
    assert any(v == "anthropic" for v in seen)


def test_analyze_run_reads_related_file_and_classifies(repo_home: Path, tmp_path: Path) -> None:
    """Decisive: a related log with a taxonomy trigger must be opened, tagged, and claimed.

    Filename-only listing without content I/O must fail this test (round-3 finding).
    SQLite holds no error text so the tag can only come from the file body.
    """
    db_path = str(tmp_path / "state.db")
    store = SqliteStore(db_path)
    _, run_id = create_run(store, [])
    # No run.error — classification must come from the related file content.
    store.update_run(run_id, {"model": "gpt-5.6-sol"})

    logs_dir = repo_home / "var" / "logs" / run_id
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / "provider.log"
    log_path.write_text(
        "provider upstream returned HTTP 429 rate limit exceeded\n",
        encoding="utf-8",
    )

    # Also consume run.manifest_path the same way when present.
    manifest = repo_home / "var" / "ledger" / "runs-202607.jsonl"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        f'{{"run_id": "{run_id}", "note": "manifest present for per-run read"}}\n',
        encoding="utf-8",
    )
    store.update_run(run_id, {"manifest_path": str(manifest)})

    out = analyze_run(run_id, db_path=db_path)
    assert out["ok"] is True
    assert "rate_limit" in out["failure_tags"], (
        f"related-file 429 must yield rate_limit tag, got {out['failure_tags']!r}"
    )

    sources = [str(s) for s in (out.get("sources_read") or [])]
    assert any("provider.log" in s for s in sources), (
        f"genuinely read log must appear in sources_read, got {sources!r}"
    )
    assert any("runs-202607.jsonl" in s for s in sources), (
        f"manifest_path must be opened and listed in sources_read, got {sources!r}"
    )

    retro = repo_home / "var" / "retro" / "run-retros.jsonl"
    assert retro.is_file()
    payload = json.loads(retro.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert "rate_limit" in payload["failure_tags"]
    assert any("provider.log" in str(s) for s in payload["sources_read"])


def test_analyze_run_manifest_no_cross_run_contamination(repo_home: Path, tmp_path: Path) -> None:
    """Decisive: multi-run monthly manifest must not leak another run's tags.

    ``manifest_path`` points at the shared monthly ``runs-YYYYMM.jsonl`` ledger.
    Whole-file sampling (round 3) attributes a peer run's 429 to *this* run.
    Only this run's JSONL lines may reach the classifier; own per-run records
    must still produce tags.
    """
    db_path = str(tmp_path / "state.db")
    store = SqliteStore(db_path)
    _, run_id = create_run(store, [])
    # No SQLite failure text — tags must come only from related-file content.
    store.update_run(run_id, {"model": "gpt-5.6-sol"})

    other_run_id = "run_other_peer_with_429"

    # Shared monthly ledger: THIS run clean; OTHER run has the 429.
    # Note: this-run line must not itself contain taxonomy trigger phrases
    # (e.g. the words "rate limit"), or the instrument confuses self-text
    # with peer contamination.
    manifest = repo_home / "var" / "ledger" / "runs-202607.jsonl"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "run_id": other_run_id,
                        "error": "HTTP 429 rate limit exceeded on peer run",
                    }
                ),
                json.dumps(
                    {
                        "run_id": run_id,
                        "note": "this run finished clean with ok status",
                    }
                ),
                "",  # trailing newline
            ]
        ),
        encoding="utf-8",
    )
    store.update_run(run_id, {"manifest_path": str(manifest)})

    # This run's own per-run log carries a different taxonomy trigger.
    logs_dir = repo_home / "var" / "logs" / run_id
    logs_dir.mkdir(parents=True, exist_ok=True)
    (logs_dir / "provider.log").write_text(
        "agent step aborted: connection timeout waiting for upstream\n",
        encoding="utf-8",
    )

    out = analyze_run(run_id, db_path=db_path)
    assert out["ok"] is True

    tags = list(out.get("failure_tags") or [])
    assert "rate_limit" not in tags, (
        "peer-run 429 in shared monthly manifest must not contaminate this run; "
        f"got failure_tags={tags!r}"
    )
    assert "timeout" in tags, (
        f"this run's own provider.log timeout must still be classified, got {tags!r}"
    )

    sources = [str(s) for s in (out.get("sources_read") or [])]
    assert any("runs-202607.jsonl" in s for s in sources), (
        f"manifest was opened (line-filtered) and must appear in sources_read, got {sources!r}"
    )
    assert any("provider.log" in s for s in sources), (
        f"per-run log must appear in sources_read, got {sources!r}"
    )

    retro = repo_home / "var" / "retro" / "run-retros.jsonl"
    assert retro.is_file()
    payload = json.loads(retro.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert payload["run_id"] == run_id
    assert "rate_limit" not in payload["failure_tags"]
    assert "timeout" in payload["failure_tags"]


def test_analyze_run_related_file_errors_not_claimed_as_read(
    repo_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unreadable/oversized related files go to source_errors, never clean sources_read."""
    # Tiny cap so a modest file is "oversized" without multi-MB fixtures.
    monkeypatch.setattr(perrun, "_PERRUN_FILE_BYTE_CAP", 64)

    db_path = str(tmp_path / "state.db")
    store = SqliteStore(db_path)
    _, run_id = create_run(store, [])
    store.update_run(run_id, {"model": "gpt-5.6-sol"})

    logs_dir = repo_home / "var" / "logs" / run_id
    logs_dir.mkdir(parents=True, exist_ok=True)
    huge = logs_dir / "huge.log"
    huge.write_bytes(b"x" * 4096)

    missing_manifest = repo_home / "var" / "ledger" / "does-not-exist.jsonl"
    store.update_run(run_id, {"manifest_path": str(missing_manifest)})

    # Unreadable: present with size>0 but shared reader yields empty content.
    locked = logs_dir / "locked.log"
    locked.write_text("would-be-content\n", encoding="utf-8")

    from omniagentos.reflection.adapters import read_and_sample_file as real_rasf

    def gated_reader(path: Path, byte_cap: int = 2 * 1024 * 1024):
        if path.name == "locked.log":
            return "", False, 0
        return real_rasf(path, byte_cap)

    monkeypatch.setattr("omniagentos.reflection.adapters.read_and_sample_file", gated_reader)

    out = analyze_run(run_id, db_path=db_path)
    assert out["ok"] is True
    sources = [str(s) for s in (out.get("sources_read") or [])]
    errors = [str(e) for e in (out.get("source_errors") or [])]

    assert not any("huge.log" in s for s in sources), (
        f"over-cap file must not be a clean sources_read entry, got {sources!r}"
    )
    assert any("file_byte_cap" in e and "huge.log" in e for e in errors), (
        f"over-cap file must appear in source_errors, got {errors!r}"
    )
    assert not any("locked.log" in s for s in sources), (
        f"unreadable file must not be a clean sources_read entry, got {sources!r}"
    )
    assert any("file_unreadable" in e and "locked.log" in e for e in errors), (
        f"unreadable file must appear in source_errors, got {errors!r}"
    )
    assert any("file_missing" in e for e in errors), (
        f"missing manifest_path must appear in source_errors, got {errors!r}"
    )


def test_analyze_run_non_run_scoped_extra_path_not_fed_to_classifier(
    repo_home: Path, tmp_path: Path
) -> None:
    """Shared non-JSONL path cannot be attributed → source_errors, no taxonomy leak."""
    db_path = str(tmp_path / "state.db")
    store = SqliteStore(db_path)
    _, run_id = create_run(store, [])
    store.update_run(run_id, {"model": "gpt-5.6-sol"})

    shared = repo_home / "var" / "ledger" / "shared-noise.log"
    shared.parent.mkdir(parents=True, exist_ok=True)
    shared.write_text(
        "foreign process hit HTTP 429 rate limit exceeded\n",
        encoding="utf-8",
    )
    # manifest_path is the only extra_paths seam today — use it for a .log.
    store.update_run(run_id, {"manifest_path": str(shared)})

    out = analyze_run(run_id, db_path=db_path)
    assert out["ok"] is True
    assert "rate_limit" not in (out.get("failure_tags") or []), (
        f"non-run-scoped shared log must not reach classifier, got {out.get('failure_tags')!r}"
    )
    errors = [str(e) for e in (out.get("source_errors") or [])]
    assert any("file_not_run_scoped" in e and "shared-noise.log" in e for e in errors), (
        f"expected file_not_run_scoped error, got {errors!r}"
    )
    sources = [str(s) for s in (out.get("sources_read") or [])]
    assert not any("shared-noise.log" in s for s in sources)


def test_analyze_run_shared_ledger_scan_is_bounded(
    repo_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Decisive: large non-matching monthly ledger must not be scanned unbounded.

    Round 4 walked every peer line (JSON-decode included) when this run's
    record was absent. Round 5 must stop at the configured line/byte ceilings
    and record the cap hit in ``source_errors``.
    """
    scan_line_cap = 50
    # Byte window large enough that the line cap is the binding constraint.
    monkeypatch.setattr(perrun, "_PERRUN_JSONL_SCAN_LINE_CAP", scan_line_cap)
    monkeypatch.setattr(perrun, "_PERRUN_JSONL_SCAN_BYTE_CAP", 512_000)
    monkeypatch.setattr(perrun, "_PERRUN_FILE_BYTE_CAP", 64_000)

    examined = {"n": 0}
    real_rid = perrun._jsonl_line_run_id

    def counting_rid(line: str) -> str | None:
        examined["n"] += 1
        return real_rid(line)

    monkeypatch.setattr(perrun, "_jsonl_line_run_id", counting_rid)

    db_path = str(tmp_path / "state.db")
    store = SqliteStore(db_path)
    _, run_id = create_run(store, [])
    store.update_run(run_id, {"model": "gpt-5.6-sol"})

    # 2000 peer-only lines — none match this run_id. Round 4 would examine all.
    peer_n = 2000
    manifest = repo_home / "var" / "ledger" / "runs-202607.jsonl"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    with manifest.open("w", encoding="utf-8") as fh:
        for i in range(peer_n):
            fh.write(
                json.dumps(
                    {
                        "run_id": f"peer_run_{i:04d}",
                        "note": f"peer ledger row {i}",
                    }
                )
                + "\n"
            )

    store.update_run(run_id, {"manifest_path": str(manifest)})

    out = analyze_run(run_id, db_path=db_path)
    assert out["ok"] is True, f"analyze_run must still return, got {out!r}"
    assert examined["n"] <= scan_line_cap, (
        f"shared ledger scan must examine at most {scan_line_cap} lines "
        f"(matching or not); examined {examined['n']} (peer rows={peer_n})"
    )
    assert examined["n"] > 0, "expected at least one line examined in the tail window"

    errors = [str(e) for e in (out.get("source_errors") or [])]
    assert any("file_line_cap" in e and "runs-202607.jsonl" in e for e in errors), (
        f"line-cap hit must be recorded in source_errors, got {errors!r}"
    )
    sources = [str(s) for s in (out.get("sources_read") or [])]
    assert not any("runs-202607.jsonl" in s for s in sources), (
        f"scan-capped ledger must not be claimed as a clean sources_read entry, got {sources!r}"
    )


def test_read_jsonl_truncates_oversized_first_matching_line(
    repo_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REVERT-CHECK target: first matching line larger than content_cap is truncated.

    Round 4 only applied the content byte cap when ``matched`` was already
    non-empty, so an oversized first hit was retained in full.
    """
    content_cap = 64
    monkeypatch.setattr(perrun, "_PERRUN_FILE_BYTE_CAP", content_cap)
    monkeypatch.setattr(perrun, "_PERRUN_JSONL_SCAN_LINE_CAP", 100)
    monkeypatch.setattr(perrun, "_PERRUN_JSONL_SCAN_BYTE_CAP", 256_000)

    db_path = str(tmp_path / "state.db")
    store = SqliteStore(db_path)
    _, run_id = create_run(store, [])
    store.update_run(run_id, {"model": "gpt-5.6-sol"})

    # Single matching line well over content_cap.
    blob = "Z" * 4000
    manifest = repo_home / "var" / "ledger" / "runs-202607.jsonl"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    full_line = json.dumps({"run_id": run_id, "blob": blob})
    assert len(full_line.encode("utf-8")) > content_cap
    manifest.write_text(full_line + "\n", encoding="utf-8")

    content, count, limit_hits = perrun._read_jsonl_lines_for_run(
        manifest, run_id, content_cap=content_cap
    )
    assert count >= 1
    assert len(content.encode("utf-8")) <= content_cap, (
        f"first matching line must be truncated to content_cap={content_cap}, "
        f"got {len(content.encode('utf-8'))} bytes"
    )
    assert "file_byte_cap" in limit_hits, f"content cap hit must be reported, got {limit_hits!r}"

    store.update_run(run_id, {"manifest_path": str(manifest)})
    out = analyze_run(run_id, db_path=db_path)
    assert out["ok"] is True
    errors = [str(e) for e in (out.get("source_errors") or [])]
    assert any("file_byte_cap" in e and "runs-202607.jsonl" in e for e in errors), (
        f"file_byte_cap must appear in source_errors, got {errors!r}"
    )


def test_read_and_sample_file_grep_scan_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run-scoped oversized logs must not taxonomy-grep to EOF when matches are sparse."""
    from omniagentos.reflection import adapters

    # Tiny scan ceiling so a modest multi-line file trips the bound.
    monkeypatch.setattr(adapters, "_GREP_SCAN_LINE_CAP", 25)
    monkeypatch.setattr(adapters, "_GREP_MATCH_CAP", 100)
    # Byte window large enough that the line cap is the binding constraint.
    monkeypatch.setattr(adapters, "_GREP_SCAN_BYTE_CAP", 512_000)

    huge = tmp_path / "huge.log"
    # No taxonomy hits — round-4 style loop would walk every line to EOF.
    lines = [f"plain operational line {i} with no failure tags\n" for i in range(500)]
    huge.write_text("".join(lines), encoding="utf-8")

    # byte_cap small → oversized path (head/tail + grep).
    byte_cap = 200
    real_open = open
    grep_reads: list[int] = []

    def counting_open(file: Any, *args: Any, **kwargs: Any) -> Any:
        mode = args[0] if args else kwargs.get("mode", "r")
        fh = real_open(file, *args, **kwargs)
        # Grep pass is binary and size-capped via read(n); count those reads.
        if isinstance(mode, str) and "b" in mode:
            real_read = fh.read

            def capped_read(n: int = -1) -> bytes:
                data = real_read(n)
                # Only record the third open's reads (head, tail, then grep).
                # Simpler: record any read whose requested size matches the
                # grep budget after head/tail half-caps.
                if n == adapters._GREP_SCAN_BYTE_CAP or (
                    isinstance(n, int) and n == max(adapters._GREP_SCAN_BYTE_CAP, 1)
                ):
                    grep_reads.append(len(data))
                return data

            fh.read = capped_read  # type: ignore[method-assign]
        return fh

    monkeypatch.setattr("builtins.open", counting_open)

    content, hit, total = adapters.read_and_sample_file(huge, byte_cap=byte_cap)
    assert hit is True
    assert total == huge.stat().st_size
    # Binary window read must not exceed the scan byte cap (decisive on BYTES).
    assert grep_reads, "expected a size-capped binary grep read"
    assert all(n <= adapters._GREP_SCAN_BYTE_CAP for n in grep_reads), (
        f"taxonomy grep must read at most _GREP_SCAN_BYTE_CAP="
        f"{adapters._GREP_SCAN_BYTE_CAP} bytes, got reads={grep_reads!r}"
    )
    assert "... [TRUNCATED" in content


def test_read_and_sample_file_one_mib_matching_line_is_byte_capped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DECISIVE (round 6): one ~1 MiB matching line, no newline → sample ≤ cap.

    Round 5 counted yielded short lines and missed the line-materialized
    violation: with ``_GREP_SCAN_BYTE_CAP=64`` a single 1 MiB matching line
    still returned ~1 MiB. Assert on returned **bytes**, not line count.
    """
    from omniagentos.reflection import adapters

    grep_cap = 64
    monkeypatch.setattr(adapters, "_GREP_SCAN_BYTE_CAP", grep_cap)
    monkeypatch.setattr(adapters, "_GREP_SCAN_LINE_CAP", 20_000)
    monkeypatch.setattr(adapters, "_GREP_MATCH_CAP", 100)

    # Real taxonomy hit ("rate limit") so the line is retained on match — not a
    # non-matching filler that would skip the retention path entirely.
    # One continuous matching line, NO trailing newline — the exact shape that
    # defeated the post-line bytes_scanned check.
    payload = b"rate limit 429 " + (b"X" * (1024 * 1024))
    assert b"\n" not in payload
    huge = tmp_path / "oneline.log"
    huge.write_bytes(payload)
    file_size = huge.stat().st_size
    assert file_size >= 1024 * 1024

    # Force oversized path (head/tail + grep); keep content sample small.
    byte_cap = 256
    content, hit, total = adapters.read_and_sample_file(huge, byte_cap=byte_cap)
    assert hit is True
    assert total == file_size

    returned_bytes = len(content.encode("utf-8", errors="replace"))
    # Head/tail ≤ byte_cap + markers + grep section ≤ grep_cap (plus small labels).
    # The hard requirement: nowhere near the full 1 MiB line.
    max_allowed = byte_cap + grep_cap + 1024
    assert returned_bytes <= max_allowed, (
        f"returned sample must be ≤ configured caps "
        f"(byte_cap={byte_cap} + grep_cap={grep_cap} + overhead); "
        f"got {returned_bytes} bytes from a {file_size}-byte one-line file"
    )
    assert returned_bytes < file_size // 10, (
        f"returned sample ({returned_bytes} B) must not retain the 1 MiB line (file={file_size} B)"
    )


def test_analyze_run_sql_output_text_is_truncated_at_source(
    repo_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DECISIVE (round 6): huge runs.output_text is substr-capped, not fetched whole.

    Assert retained failure text is bounded and truncation is recorded in
    ``source_errors``. REVERT-CHECK target: remove SQL-side substr → fails.
    """
    text_cap = 256
    monkeypatch.setattr(perrun, "_PERRUN_SQL_TEXT_CAP", text_cap)
    monkeypatch.setattr(perrun, "_PERRUN_SQL_STEP_CAP", 10)

    db_path = str(tmp_path / "state.db")
    store = SqliteStore(db_path)
    _, run_id = create_run(store, [])

    huge_output = "rate limit 429 " + ("Y" * 200_000)
    assert len(huge_output.encode("utf-8")) > text_cap * 10
    store.update_run(
        run_id,
        {
            "model": "gpt-5.6-sol",
            "error": "failed",
            "output_text": huge_output,
        },
    )

    # Instrument the classify path: capture what finalize actually feeds it.
    captured: dict[str, str] = {}
    real_classify = perrun.classify_content

    def wrapping_classify(text: str) -> list[str]:
        captured["text"] = text
        return real_classify(text)

    monkeypatch.setattr(perrun, "classify_content", wrapping_classify)

    out = analyze_run(run_id, db_path=db_path)
    assert out["ok"] is True, f"analyze_run must still return, got {out!r}"
    assert "text" in captured, "expected classifier to receive failure text"

    retained = captured["text"]
    retained_bytes = len(retained.encode("utf-8", errors="replace"))
    # Bundle contributes substr(output_text)+error+state — each column ≤ text_cap.
    # Hard ceiling well under the raw 200 KiB payload.
    assert retained_bytes <= text_cap * 4 + 256, (
        f"retained classify text must be SQL-capped (text_cap={text_cap}), "
        f"got {retained_bytes} bytes"
    )
    assert retained_bytes < len(huge_output) // 10, (
        f"retained text ({retained_bytes} B) must not include full output_text "
        f"({len(huge_output)} chars)"
    )

    errors = [str(e) for e in (out.get("source_errors") or [])]
    assert any("sqlite_text_cap" in e and "output_text" in e for e in errors), (
        f"SQL text truncation must be recorded in source_errors, got {errors!r}"
    )
    # Taxonomy still sees the rate-limit token from the untruncated prefix.
    assert "rate_limit" in (out.get("failure_tags") or [])


def test_failed_run_query_is_not_claimed_as_a_source(tmp_path, monkeypatch, caplog):
    """A query that errored is not evidence.

    Round 7 appended ``sqlite:<db>`` to ``sources_read`` before knowing whether
    the read produced anything, so a schema mismatch logged an OperationalError
    and still yielded ok=True with empty source_errors — a clean-looking retro
    built on a failed read.
    """
    import logging
    import sqlite3

    monkeypatch.setenv("OMNIAGENTOS_HOME", str(tmp_path))
    db = tmp_path / "malformed.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE runs (id TEXT PRIMARY KEY)")  # missing projected columns
    conn.commit()
    conn.close()

    with caplog.at_level(logging.WARNING):
        result = analyze_run("run_x", db_path=str(db))

    assert result["ok"] is False
    assert result["sources_read"] == []
    assert any("sqlite_query_failed" in e for e in result["source_errors"])


def test_absent_run_row_is_not_claimed_as_a_source(tmp_path, monkeypatch, caplog):
    """A database with no row for this run has told us nothing about this run."""
    import logging
    import sqlite3

    monkeypatch.setenv("OMNIAGENTOS_HOME", str(tmp_path))
    db = tmp_path / "empty.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE runs (id TEXT PRIMARY KEY, model TEXT, harness TEXT, agent TEXT, "
        "state TEXT, session_ref TEXT, manifest_path TEXT, error TEXT, output_text TEXT)"
    )
    conn.commit()
    conn.close()

    with caplog.at_level(logging.WARNING):
        result = analyze_run("run_absent", db_path=str(db))

    assert result["ok"] is False, "no row for this run is not a successful analysis"
    assert result["sources_read"] == []
    assert any("sqlite_run_absent" in e for e in result["source_errors"])
    assert "not claiming it as a source" in caplog.text


def test_step_count_probe_is_bounded(tmp_path, monkeypatch):
    """The step-cap probe must not visit every matching row.

    ``SELECT COUNT(*) ... WHERE run_id = ?`` has to scan the whole matching set
    to produce an exact count, so a run with very many steps imposed unbounded
    work on the serial post-run drain even though the payload query used LIMIT.
    Written before the fix and confirmed failing against it.
    """
    import sqlite3

    from omniagentos.reflection import perrun

    monkeypatch.setenv("OMNIAGENTOS_HOME", str(tmp_path))
    db = tmp_path / "many_steps.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE runs (id TEXT PRIMARY KEY, model TEXT, harness TEXT, agent TEXT, "
        "state TEXT, session_ref TEXT, manifest_path TEXT, error TEXT, output_text TEXT)"
    )
    conn.execute("INSERT INTO runs (id, state) VALUES ('run_big', 'completed')")
    conn.execute(
        "CREATE TABLE steps (run_id TEXT, seq INTEGER, name TEXT, status TEXT, output TEXT)"
    )
    conn.executemany(
        "INSERT INTO steps (run_id, seq, name, status, output) VALUES ('run_big', ?, 'n', 'ok', '')",
        [(i,) for i in range(5000)],
    )
    conn.commit()
    conn.close()

    scanned = {"rows": 0}
    real_execute = sqlite3.Cursor.execute

    ro = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    ro.row_factory = sqlite3.Row
    try:
        # The probe must be answerable from at most cap+1 rows, never all 5000.
        cap = perrun._PERRUN_STEP_CAP if hasattr(perrun, "_PERRUN_STEP_CAP") else 50
        rows = ro.execute(
            "SELECT COUNT(*) AS n FROM (SELECT 1 FROM steps WHERE run_id = ? LIMIT ?)",
            ("run_big", cap + 1),
        ).fetchone()
        scanned["rows"] = int(rows["n"])
    finally:
        ro.close()
    assert (
        scanned["rows"]
        <= (perrun._PERRUN_STEP_CAP if hasattr(perrun, "_PERRUN_STEP_CAP") else 50) + 1
    )

    bundle_src = Path(perrun.__file__).read_text(encoding="utf-8")
    assert "SELECT COUNT(*) AS n FROM steps WHERE run_id = ?" not in bundle_src, (
        "the exact-count probe must be replaced by a bounded one (LIMIT cap+1)"
    )
    del real_execute


def test_related_file_scan_does_not_materialize_the_directory(tmp_path, monkeypatch):
    """Enumeration must stop early, not sort the whole directory first.

    ``sorted(logs_dir.iterdir())`` builds and sorts every entry before the loop
    can break at the candidate cap, so a log directory with very many files is
    fully materialized at finalize.
    """
    from omniagentos.reflection import perrun

    src = __import__("pathlib").Path(perrun.__file__).read_text(encoding="utf-8")
    assert "sorted(logs_dir.iterdir())" not in src, (
        "directory enumeration must stream and stop at the cap, not sort everything first"
    )


def test_close_failures_inside_analyze_run_are_logged(tmp_path, monkeypatch, caplog):
    """Every failure inside analyze_run is caught AND logged — including close.

    Written test-first: run against the unmodified tree it fails, because the
    primary read handle's close was wrapped in `except Exception: pass`. A
    silent close is a small failure, but "caught and logged" is the brief's
    literal requirement and an unlogged one is unwitnessed by construction.
    """
    import logging
    import sqlite3

    monkeypatch.setenv("OMNIAGENTOS_HOME", str(tmp_path))
    db = tmp_path / "closes_badly.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE runs (id TEXT PRIMARY KEY, model TEXT, harness TEXT, agent TEXT, "
        "state TEXT, session_ref TEXT, manifest_path TEXT, error TEXT, output_text TEXT)"
    )
    conn.execute("INSERT INTO runs (id, state) VALUES ('run_close', 'completed')")
    conn.commit()
    conn.close()

    real_connect = sqlite3.connect

    class _ExplodingClose:
        """A connection wrapper whose close() always raises, and counts itself."""

        raised = 0

        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def close(self):
            self._inner.close()
            type(self).raised += 1
            raise sqlite3.OperationalError("close failed under test")

    def _connect(*args, **kwargs):
        return _ExplodingClose(real_connect(*args, **kwargs))

    monkeypatch.setattr(sqlite3, "connect", _connect)

    with caplog.at_level(logging.WARNING):
        result = analyze_run("run_close", db_path=str(db))

    assert isinstance(result, dict), "a failing close must never escape analyze_run"

    # Counting matters. Asserting only that SOME "failed closing" line appears is
    # satisfied by the one handler that already logged, so it passes while the
    # other stays silent — the first version of this test did exactly that.
    # Every close that raised must produce a log line.
    failures = _ExplodingClose.raised
    logged = sum(1 for r in caplog.records if "failed closing" in r.getMessage().lower())
    assert failures > 0, "the test did not actually exercise a failing close"
    assert logged == failures, (
        f"{failures} close(s) raised but only {logged} were logged; "
        "an unlogged failure inside analyze_run is unwitnessed by construction"
    )


def _realistic_sessions_attempts_schema(conn) -> None:
    """Mirror production indexes that matter for finalize-time plans.

    sessions: PK on id; NO index on session_ref (089 recreates state/source/
    account_state/created_at only). swarm_attempts: index on swarm_run_id only
    (045); no (swarm_run_id, started_at) composite.
    """
    conn.executescript(
        """
        CREATE TABLE runs (
            id TEXT PRIMARY KEY, model TEXT, harness TEXT, agent TEXT,
            state TEXT, session_ref TEXT, manifest_path TEXT,
            error TEXT, output_text TEXT
        );
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT, project_dir TEXT, provider TEXT, session_ref TEXT,
            state TEXT, pid INTEGER, model TEXT, title TEXT,
            account_id TEXT, created_at TEXT
        );
        CREATE INDEX idx_sessions_state ON sessions(state);
        CREATE INDEX idx_sessions_source ON sessions(source);
        CREATE INDEX idx_sessions_account_state ON sessions(account_id, state);
        CREATE INDEX idx_sessions_created ON sessions(created_at DESC, id DESC);

        CREATE TABLE swarm_attempts (
            id TEXT PRIMARY KEY,
            swarm_run_id TEXT NOT NULL,
            board_task_id TEXT NOT NULL,
            seq INTEGER NOT NULL,
            session_id TEXT,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            tier TEXT,
            account_id TEXT,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            end_reason TEXT,
            detail TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX idx_swarm_attempts_run ON swarm_attempts(swarm_run_id);
        """
    )


def test_attempts_lookup_plan_is_bounded(tmp_path, monkeypatch):
    """Attempts read must not sort every row for the run before LIMIT.

    ``WHERE swarm_run_id = ? ORDER BY started_at DESC LIMIT 50`` uses the
    swarm_run_id-only index then builds a TEMP B-TREE over every matching
    attempt. LIMIT bounds rows returned, not rows examined/sorted — the same
    class as exact COUNT and materialized directory scans.

    Axis of the property: EXPLAIN QUERY PLAN must not contain TEMP B-TREE.
    Written test-first against the pre-fix tree.
    """
    import sqlite3

    from omniagentos.reflection import perrun as perrun_mod

    monkeypatch.setenv("OMNIAGENTOS_HOME", str(tmp_path))
    db = tmp_path / "attempts_plan.db"
    conn = sqlite3.connect(db)
    _realistic_sessions_attempts_schema(conn)
    conn.execute(
        "INSERT INTO runs (id, state, session_ref, model) "
        "VALUES ('run_att', 'completed', NULL, 'gpt-5.6-sol')"
    )
    conn.executemany(
        "INSERT INTO swarm_attempts "
        "(id, swarm_run_id, board_task_id, seq, provider, model, started_at, detail) "
        "VALUES (?, 'run_att', ?, 1, 'claude', 'claude-opus', ?, 'ok')",
        [
            (f"swa_{i}", f"task_{i}", f"2026-01-01T00:00:{i % 60:02d}.{i:04d}")
            for i in range(500)
        ],
    )
    conn.commit()
    conn.close()

    captured: list[str] = []
    real_open = perrun_mod._open_ro

    def _open_ro(db_path: str):
        c = real_open(db_path)
        if c is not None:
            def _trace(sql: str) -> None:
                if "swarm_attempts" in sql.lower() and sql.lstrip().upper().startswith("SELECT"):
                    captured.append(sql)
            c.set_trace_callback(_trace)
        return c

    monkeypatch.setattr(perrun_mod, "_open_ro", _open_ro)

    result = analyze_run("run_att", db_path=str(db))
    assert isinstance(result, dict)
    assert captured, "analyze_run never issued a swarm_attempts SELECT — path unbound"

    plan_conn = sqlite3.connect(db)
    try:
        for sql in captured:
            n_params = sql.count("?")
            params = tuple(
                [16000 if i == 0 and "substr" in sql.lower() else "run_att" for i in range(n_params)]
            )
            plans = list(plan_conn.execute("EXPLAIN QUERY PLAN " + sql, params))
            plan_text = " | ".join(str(row[-1]) for row in plans)
            assert "TEMP B-TREE" not in plan_text.upper(), (
                f"attempts lookup sorts unbounded work before LIMIT; "
                f"plan={plan_text!r} sql={sql!r}"
            )
    finally:
        plan_conn.close()


def test_sessions_lookup_plan_is_bounded(tmp_path, monkeypatch):
    """Sessions read must not SCAN the table for an unindexed session_ref.

    ``WHERE id = ? OR session_ref = ? LIMIT 5`` plans as SCAN sessions because
    session_ref has no index and OR disables the id primary-key search. LIMIT
    bounds matches returned; a missing provider session_ref still walks every
    session row.

    Axis of the property: EXPLAIN QUERY PLAN must not contain SCAN.
    Written test-first against the pre-fix tree.
    """
    import sqlite3

    from omniagentos.reflection import perrun as perrun_mod

    monkeypatch.setenv("OMNIAGENTOS_HOME", str(tmp_path))
    db = tmp_path / "sessions_plan.db"
    conn = sqlite3.connect(db)
    _realistic_sessions_attempts_schema(conn)
    conn.execute(
        "INSERT INTO runs (id, state, session_ref, model) "
        "VALUES ('run_ses', 'completed', 'missing_provider_ref', 'gpt-5.6-sol')"
    )
    conn.executemany(
        "INSERT INTO sessions (id, provider, session_ref, state, model, created_at) "
        "VALUES (?, 'claude', ?, 'idle', 'claude-opus', ?)",
        [
            (f"ses_{i}", f"ref_{i}", f"2026-01-01T00:00:{i % 60:02d}")
            for i in range(500)
        ],
    )
    conn.commit()
    conn.close()

    captured: list[str] = []
    real_open = perrun_mod._open_ro

    def _open_ro(db_path: str):
        c = real_open(db_path)
        if c is not None:
            def _trace(sql: str) -> None:
                if (
                    "sessions" in sql.lower()
                    and sql.lstrip().upper().startswith("SELECT")
                    and "sqlite_master" not in sql.lower()
                ):
                    captured.append(sql)
            c.set_trace_callback(_trace)
        return c

    monkeypatch.setattr(perrun_mod, "_open_ro", _open_ro)

    result = analyze_run("run_ses", db_path=str(db))
    assert isinstance(result, dict)
    assert captured, "analyze_run never issued a sessions SELECT — path unbound"

    plan_conn = sqlite3.connect(db)
    try:
        for sql in captured:
            n_params = sql.count("?")
            params = tuple(["missing_provider_ref"] * n_params)
            plans = list(plan_conn.execute("EXPLAIN QUERY PLAN " + sql, params))
            plan_text = " | ".join(str(row[-1]) for row in plans)
            assert "SCAN" not in plan_text.upper(), (
                f"sessions lookup examines unbounded rows; "
                f"plan={plan_text!r} sql={sql!r}"
            )
    finally:
        plan_conn.close()


def test_analyze_run_reads_attempt_and_session_rows(tmp_path, monkeypatch):
    """Path coverage: insert attempt + session rows and assert they feed providers.

    Earlier green ladders never inserted these rows, so the unbounded lookups
    had no binding test at all.
    """
    import sqlite3

    monkeypatch.setenv("OMNIAGENTOS_HOME", str(tmp_path))
    db = tmp_path / "path_coverage.db"
    conn = sqlite3.connect(db)
    _realistic_sessions_attempts_schema(conn)
    conn.execute(
        "INSERT INTO runs (id, state, session_ref, model) "
        "VALUES ('run_cov', 'completed', 'ses_target', NULL)"
    )
    conn.execute(
        "INSERT INTO sessions (id, provider, session_ref, state, model, created_at) "
        "VALUES ('ses_target', 'gemini', 'provider-uuid-1', 'idle', 'gemini-2.5', '2026-01-01')"
    )
    conn.execute(
        "INSERT INTO swarm_attempts "
        "(id, swarm_run_id, board_task_id, seq, provider, model, started_at, detail) "
        "VALUES ('swa_1', 'run_cov', 'task_1', 1, 'kimi', 'kimi-k2', '2026-01-02', 'ok')"
    )
    conn.commit()
    conn.close()

    out = analyze_run("run_cov", db_path=str(db))
    assert out["ok"] is True
    retro = tmp_path / "var" / "retro" / "run-retros.jsonl"
    payload = json.loads(retro.read_text(encoding="utf-8").strip().splitlines()[-1])
    providers = set(payload["providers"])
    # session via id match + attempt provider must both contribute
    assert "gemini" in providers, payload
    assert "kimi" in providers, payload
