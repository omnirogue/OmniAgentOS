from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from omniagentos.lab.contracts import EvalResult, EvalSplit
from omniagentos.lab.eval import grader as grader_module
from omniagentos.lab.eval.grader import MissingExpectedError, ProtectedGrader
from omniagentos.lab.eval.paths import PROTECTED_DB_ENV


def test_put_expected_then_score_outputs_grades_deterministically() -> None:
    grader = ProtectedGrader(":memory:")
    grader.put_expected("evc_1", {"value": "42"})
    grader.put_expected("evc_2", {"value": "7"})

    result = grader.score_outputs(
        "evs_1",
        "held_out",
        "challenger",
        {"evc_1": {"text": "42"}, "evc_2": {"text": "wrong"}},
    )

    assert result.arm == "challenger"
    assert result.suite_id == "evs_1"
    assert result.split == EvalSplit.HELD_OUT
    assert result.metrics["accuracy"] == 0.5
    assert result.per_case["evc_1"]["correct"] == 1.0
    assert result.per_case["evc_2"]["correct"] == 0.0
    # Grading never had experiment context — the caller (ProtectedEvaluator /
    # L04-campaign) is responsible for stamping experiment_id before persisting.
    assert result.experiment_id == ""


def test_score_outputs_never_serializes_expected_anywhere_in_the_result() -> None:
    grader = ProtectedGrader(":memory:")
    canary = "CANARY-3f9a7c2e-do-not-leak"
    grader.put_expected("evc_secret", {"value": canary})

    result = grader.score_outputs(
        "evs_1", "held_out", "challenger", {"evc_secret": {"text": "guess"}}
    )

    dumped = result.model_dump_json()
    assert canary not in dumped
    assert canary not in str(result.metrics)
    assert canary not in str(result.per_case)


def test_score_outputs_raises_missing_expected_for_unknown_case_id() -> None:
    grader = ProtectedGrader(":memory:")
    grader.put_expected("evc_known", {"value": "x"})
    with pytest.raises(MissingExpectedError):
        grader.score_outputs("evs_1", "held_out", "challenger", {"evc_unknown": {"text": "x"}})


def test_score_outputs_rejects_empty_outputs() -> None:
    grader = ProtectedGrader(":memory:")
    with pytest.raises(ValueError, match="empty"):
        grader.score_outputs("evs_1", "dev", "champion", {})


def test_put_expected_upserts_on_the_same_case_id() -> None:
    grader = ProtectedGrader(":memory:")
    grader.put_expected("evc_1", {"value": "first"})
    grader.put_expected("evc_1", {"value": "second"})
    result = grader.score_outputs("evs_1", "dev", "champion", {"evc_1": {"text": "second"}})
    assert result.per_case["evc_1"]["correct"] == 1.0


def test_in_memory_expected_is_ciphertext_and_still_scores() -> None:
    grader = ProtectedGrader(":memory:")
    canary = "CANARY-in-memory-at-rest"
    grader.put_expected("evc_secret", {"value": canary})

    row = grader._connection.execute(
        "SELECT expected_json FROM protected_expected WHERE case_id = ?", ("evc_secret",)
    ).fetchone()
    assert row is not None
    ciphertext = str(row["expected_json"])
    assert ciphertext.startswith("gAAAA")
    assert canary not in ciphertext

    result = grader.score_outputs(
        "evs_1", "held_out", "challenger", {"evc_secret": {"text": canary}}
    )
    assert result.per_case["evc_secret"]["correct"] == 1.0
    grader.close()


def test_put_expected_many_uses_one_worker_and_scores_all_cases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    grader = ProtectedGrader(str(tmp_path / "protected.db"))
    original_worker = grader._worker
    operations: list[str] = []

    def spy_worker(request: dict[str, object]) -> dict[str, object]:
        operations.append(str(request.get("operation")))
        return original_worker(request)

    monkeypatch.setattr(grader, "_worker", spy_worker)
    grader.put_expected_many({"evc_1": {"value": "a"}, "evc_2": {"value": "b"}})
    result = grader.score_outputs(
        "evs_1",
        "held_out",
        "challenger",
        {"evc_1": {"text": "a"}, "evc_2": {"text": "b"}},
    )

    assert operations == ["put_many", "score"]
    assert result.metrics["accuracy"] == 1.0


def test_corrupt_ciphertext_surfaces_as_missing_expected_integrity_error(
    tmp_path: Path,
) -> None:
    path = tmp_path / "protected.db"
    grader = ProtectedGrader(str(path))
    grader.put_expected("evc_1", {"value": "secret"})
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "UPDATE protected_expected SET expected_json = ? WHERE case_id = ?",
            ("corrupt-ciphertext", "evc_1"),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(MissingExpectedError):
        grader.score_outputs("evs_1", "held_out", "challenger", {"evc_1": {"text": "secret"}})
    grader.close()


def test_two_graders_pointed_at_different_files_are_fully_isolated(tmp_path: Path) -> None:
    """Structural proof that ProtectedGrader instances do not share state
    beyond the file each is explicitly given — a grader for suite A never
    sees suite B's held-out answers even though both run in the same process."""
    path_a = tmp_path / "a" / "eval_protected.db"
    path_b = tmp_path / "b" / "eval_protected.db"
    grader_a = ProtectedGrader(str(path_a))
    grader_b = ProtectedGrader(str(path_b))

    grader_a.put_expected("evc_shared_id", {"value": "answer-in-a"})
    # grader_b never received put_expected for this case id at all.
    with pytest.raises(MissingExpectedError):
        grader_b.score_outputs(
            "evs_1", "held_out", "challenger", {"evc_shared_id": {"text": "guess"}}
        )

    assert path_a.exists()
    assert path_b.exists()
    assert path_a != path_b
    grader_a.close()
    grader_b.close()


def test_protected_db_file_is_created_owner_only_permissions(tmp_path: Path) -> None:
    path = tmp_path / "eval_protected.db"
    grader = ProtectedGrader(str(path))
    grader.put_expected("evc_1", {"value": "x"})
    mode = path.stat().st_mode & 0o777
    assert mode == 0o600
    grader.close()


def test_close_never_deletes_explicit_protected_path_directory(tmp_path: Path) -> None:
    protected_dir = tmp_path / "caller-owned"
    protected_dir.mkdir()
    marker = protected_dir / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    grader = ProtectedGrader(str(protected_dir / "protected.db"))

    grader.close()
    grader.close()

    assert protected_dir.is_dir()
    assert marker.read_text(encoding="utf-8") == "keep"
    assert (protected_dir / "protected.db").exists()


def test_close_never_deletes_env_override_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    protected_dir = tmp_path / "deployer-owned"
    protected_dir.mkdir()
    marker = protected_dir / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    protected_path = protected_dir / "persistent.db"
    monkeypatch.setenv(PROTECTED_DB_ENV, str(protected_path))
    grader = ProtectedGrader()

    grader.close()
    grader.close()

    assert protected_dir.is_dir()
    assert marker.read_text(encoding="utf-8") == "keep"
    assert protected_path.exists()


def _result(
    arm: str,
    split: EvalSplit,
    metrics: dict[str, float],
    per_case: dict[str, dict[str, float]] | None = None,
) -> EvalResult:
    return EvalResult(
        experiment_id="exp_1",
        arm=arm,
        suite_id="evs_1",
        suite_version=1,
        split=split,
        metrics=metrics,
        per_case=per_case or {},
    )


def test_audit_metric_jump_flags_a_large_absolute_jump() -> None:
    grader = ProtectedGrader(":memory:")
    champ = _result("champion", EvalSplit.DEV, {"accuracy": 0.5})
    chal = _result("challenger", EvalSplit.DEV, {"accuracy": 0.55})
    assert grader.audit_metric_jump("exp_1", champ, chal) == []

    chal_big_jump = _result("challenger", EvalSplit.DEV, {"accuracy": 1.0})
    flags = grader.audit_metric_jump("exp_1", champ, chal_big_jump)
    assert any(flag.startswith("metric_jump:accuracy:") for flag in flags)


def test_audit_metric_jump_flags_suspicious_perfect_held_out_score() -> None:
    grader = ProtectedGrader(":memory:")
    champ = _result("champion", EvalSplit.HELD_OUT, {"accuracy": 0.4})
    chal = _result(
        "challenger",
        EvalSplit.HELD_OUT,
        {"accuracy": 1.0},
        per_case={f"evc_{i}": {"correct": 1.0} for i in range(5)},
    )
    flags = grader.audit_metric_jump("exp_1", champ, chal)
    assert any("suspicious_perfect" in flag for flag in flags)
    assert any("zero_variance_perfect" in flag for flag in flags)


def test_audit_metric_jump_does_not_flag_dev_split_perfection() -> None:
    """The near-perfect/zero-variance leak heuristics are HELD_OUT-specific —
    a challenger nailing the (candidate-visible) DEV set is unremarkable."""
    grader = ProtectedGrader(":memory:")
    champ = _result("champion", EvalSplit.DEV, {"accuracy": 0.4})
    chal = _result(
        "challenger",
        EvalSplit.DEV,
        {"accuracy": 1.0},
        per_case={f"evc_{i}": {"correct": 1.0} for i in range(5)},
    )
    flags = grader.audit_metric_jump("exp_1", champ, chal)
    # the absolute-jump check still fires (0.6 >= 0.5) regardless of split...
    assert any(flag.startswith("metric_jump:accuracy:") for flag in flags)
    # ...but the held-out-only leak signatures must not.
    assert not any("suspicious_perfect" in flag for flag in flags)
    assert not any("zero_variance_perfect" in flag for flag in flags)


def test_audit_metric_jump_no_flags_for_a_modest_genuine_improvement() -> None:
    grader = ProtectedGrader(":memory:")
    champ = _result(
        "champion",
        EvalSplit.HELD_OUT,
        {"accuracy": 0.6},
        per_case={"evc_1": {"correct": 1.0}, "evc_2": {"correct": 0.0}},
    )
    chal = _result(
        "challenger",
        EvalSplit.HELD_OUT,
        {"accuracy": 0.7},
        per_case={"evc_1": {"correct": 1.0}, "evc_2": {"correct": 1.0}, "evc_3": {"correct": 0.0}},
    )
    assert grader.audit_metric_jump("exp_1", champ, chal) == []


def test_audit_metric_jump_skips_metrics_missing_from_either_arm() -> None:
    grader = ProtectedGrader(":memory:")
    champ = _result("champion", EvalSplit.DEV, {"accuracy": 0.5, "only_champion": 0.9})
    chal = _result("challenger", EvalSplit.DEV, {"accuracy": 0.5, "only_challenger": 0.9})
    assert grader.audit_metric_jump("exp_1", champ, chal) == []


def test_audit_metric_jump_ignores_arbitrary_scale_metrics() -> None:
    """A latency/complexity swing is not a '0.5 jump' in the accuracy sense —
    the heuristic is scoped to [0, 1]-bounded (quality-shaped) values only;
    a held-out leak can inflate a correctness score, never shrink latency."""
    grader = ProtectedGrader(":memory:")
    champ = _result(
        "champion", EvalSplit.DEV, {"accuracy": 0.5, "latency_ms": 200.0, "complexity": 10.0}
    )
    chal = _result(
        "challenger", EvalSplit.DEV, {"accuracy": 0.5, "latency_ms": 900.0, "complexity": 40.0}
    )
    assert grader.audit_metric_jump("exp_1", champ, chal) == []


def test_protected_path_cannot_alias_shared_database_or_visible_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Graft #1: Isolation guard prevents protected DB from aliasing shared
    database or living inside vault/ledger directories."""
    shared = tmp_path / "omniagentos.db"
    vault = tmp_path / "vault"
    ledger = tmp_path / "ledger"
    monkeypatch.setenv("OMNIAGENTOS_DB", str(shared))
    monkeypatch.setenv("OMNIAGENTOS_VAULT_DIR", str(vault))
    monkeypatch.setenv("OMNIAGENTOS_LEDGER_DIR", str(ledger))

    # Should reject if protected path equals shared DB path
    with pytest.raises(ValueError, match="separate"):
        ProtectedGrader(str(shared))

    # Should reject if protected path is inside vault
    with pytest.raises(ValueError, match="vault or ledger"):
        ProtectedGrader(str(vault / "eval.db"))

    # Should reject if protected path is inside ledger
    with pytest.raises(ValueError, match="vault or ledger"):
        ProtectedGrader(str(ledger / "eval.db"))


def test_isolation_uses_actual_shared_paths_not_process_defaults(tmp_path: Path) -> None:
    actual_shared = tmp_path / "deployment" / "custom.db"
    with pytest.raises(ValueError, match="separate from the shared database"):
        ProtectedGrader(
            str(actual_shared),
            shared_db_path=str(actual_shared),
            shared_vault_dir=str(tmp_path / "custom-vault"),
            shared_ledger_dir=str(tmp_path / "custom-ledger"),
        )


def _isolation_probe(tmp_path: Path) -> ProtectedGrader:
    grader = object.__new__(ProtectedGrader)
    grader._path = str(tmp_path / "protected" / "eval.db")
    grader._shared_db_path = str(tmp_path / "shared" / "omniagentos.db")
    grader._shared_vault_dir = str(tmp_path / "vault")
    grader._shared_ledger_dir = str(tmp_path / "ledger")
    return grader


def test_isolation_rejects_unknown_database_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    grader = _isolation_probe(tmp_path)
    monkeypatch.setattr(grader_module, "inode_paths_equal", lambda *_args: None)

    with pytest.raises(ValueError, match="separate from the shared database"):
        grader._validate_isolation()


def test_isolation_rejects_unknown_visible_root_containment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    grader = _isolation_probe(tmp_path)
    monkeypatch.setattr(grader_module, "inode_paths_equal", lambda *_args: False)
    monkeypatch.setattr(
        grader_module,
        "inode_path_is_within_anchored",
        lambda *_args: None,
        raising=False,
    )

    with pytest.raises(ValueError, match="vault or ledger"):
        grader._validate_isolation()


def test_isolation_allows_positively_different_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    grader = _isolation_probe(tmp_path)
    monkeypatch.setattr(grader_module, "inode_paths_equal", lambda *_args: False)
    monkeypatch.setattr(
        grader_module,
        "inode_path_is_within_anchored",
        lambda *_args: False,
        raising=False,
    )

    grader._validate_isolation()


def test_protected_grader_scores_out_of_process_and_returns_no_expected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Graft #2: Verify that expected values never enter the parent process
    address space. The parent process MUST NOT open the protected SQLite file."""
    grader = ProtectedGrader(str(tmp_path / "protected" / "eval.db"))
    grader.put_expected("case-a", {"value": "correct"})
    grader.put_expected("case-b", {"value": "right"})

    def parent_must_not_open_sqlite(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError(
            "parent process opened protected SQLite — expected must not enter parent!"
        )

    # Monkeypatch sqlite3.connect to raise if called in the parent process
    monkeypatch.setattr(sqlite3, "connect", parent_must_not_open_sqlite)

    # Score outputs: this must complete successfully via the subprocess worker
    # WITHOUT the parent ever calling sqlite3.connect()
    result = grader.score_outputs(
        "suite_eval",
        "held_out",
        "challenger",
        {
            "case-a": {"text": "correct"},
            "case-b": {"text": "wrong"},
        },
    )

    # Verify correctness of the scoring
    assert result.metrics == {"accuracy": 0.5, "mean_score": 0.5}
    assert result.per_case == {
        "case-a": {"correct": 1.0, "score": 1.0},
        "case-b": {"correct": 0.0, "score": 0.0},
    }
    # Verify the expected value never leaked into the result
    dumped = result.model_dump_json()
    assert "correct" not in dumped or "expected" not in dumped
    # Verify file permissions are strict (0o600)
    assert (tmp_path / "protected" / "eval.db").stat().st_mode & 0o777 == 0o600


def test_protected_db_sidecar_files_also_have_owner_only_permissions(tmp_path: Path) -> None:
    """Graft #3: Chmod the -wal and -shm sidecar files 0o600 as well."""
    path = tmp_path / "protected.db"
    grader = ProtectedGrader(str(path))
    grader.put_expected("evc_1", {"value": "x"})
    # Trigger WAL creation if it hasn't been already
    grader.score_outputs("suite", "dev", "test", {"evc_1": {"text": "x"}})

    # Check main file permissions
    main_mode = path.stat().st_mode & 0o777
    assert main_mode == 0o600

    # Check sidecar files if they exist (WAL mode creates them)
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{path}{suffix}")
        if sidecar.exists():
            sidecar_mode = sidecar.stat().st_mode & 0o777
            assert sidecar_mode == 0o600, (
                f"{suffix} file should be 0o600 but got {oct(sidecar_mode)}"
            )
