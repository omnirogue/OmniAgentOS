"""Adversarial contracts for the coordinator-only promotion finalizer."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import subprocess
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import omniagentos.integration.promote as promote
from omniagentos.contracts import utc_now_iso
from omniagentos.integration.promote import (
    ProducedReceipt,
    PromotionFinalizer,
    PromotionRefusal,
    PromotionRequest,
)
from omniagentos.scheduler.gate_evidence import (
    SCHEMA,
    GateEvidence,
    GateEvidenceStore,
    binding_digest,
    workspace_digest_for,
)
from omniagentos.scheduler.gate_runner import GateRunRequest

_CANDIDATE_REF = "refs/heads/promotion/p0-test"


def _git(repo: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if check:
        assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _config(path: Path, *, mode: str = "enforce", pause_s: int = 1) -> Path:
    body = {
        "integration": {
            "branch_prefix": "integration/batch",
            "protected_branches": ["main"],
            "batch": {
                "state_file": "var/integration/current-batch.json",
                "worktree_root": "var/integration/worktrees",
            },
            "roles": {
                "coder": {
                    "harness": "cli-grok",
                    "model": "grok-4.5",
                    "effort": "high",
                    "can_merge_to_main": False,
                },
                "lane_reviewer": {
                    "harness": "cli-codex",
                    "model": "gpt-5.6-sol",
                    "effort": "high",
                    "can_merge_to_main": False,
                },
                "aggregate_reviewer": {
                    "harness": "cli-claude",
                    "model": "claude-opus-5",
                    "effort": "high",
                    "can_merge_to_main": False,
                },
            },
            "reviewer_lineage_required": "anthropic",
            "verdicts": {"prose_fallback": True},
            "promotion": {
                "mode": mode,
                "pause_s": pause_s,
                "gate_targets": ["tests/gate"],
            },
        }
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(body, sort_keys=False), encoding="utf-8")
    return path


def _write_authorship(
    path: Path,
    *,
    key: bytes,
    candidate_sha: str,
    records: list[dict[str, str]] | None = None,
) -> Path:
    payload: dict[str, Any] = {
        "schema": promote._AUTHORSHIP_SCHEMA,
        "candidate_sha": candidate_sha,
        "authorships": records or [{"role": "coder", "model": "gpt-5.6-sol", "family": "openai"}],
    }
    payload["signature"] = hmac.new(key, _canonical(payload), "sha256").hexdigest()
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    return path


def _write_review(
    path: Path,
    *,
    model: str,
    family: str,
    candidate_sha: str,
    authorship_families: tuple[str, ...] = ("openai",),
    verdict: str = "APPROVE",
    structured: bool = True,
) -> Path:
    lines = [
        f"VERDICT: {verdict}",
        f"Candidate-Sha: {candidate_sha}",
        f"Authorship-Families: {','.join(authorship_families)}",
    ]
    if structured:
        lines.extend([f"Reviewer-Model: {model}", f"Reviewer-Family: {family}"])
    else:
        lines.append("This was not reviewed by Claude, Opus, Fable, or Kimi.")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _repo(
    tmp_path: Path,
    *,
    mode: str = "enforce",
) -> tuple[Path, str, str, Path, Path, tuple[Path, Path], bytes, Ed25519PrivateKey]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Test Coordinator")
    _git(repo, "config", "user.email", "coordinator@example.invalid")
    (repo / ".gitignore").write_text("var/*\n", encoding="utf-8")
    (repo / "ARCHI.md").write_text("old architecture\n", encoding="utf-8")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    (repo / "omniagentos").mkdir()
    (repo / "omniagentos" / "__init__.py").write_text('"""Fixture package."""\n', encoding="utf-8")
    (repo / "tests" / "gate").mkdir(parents=True)
    (repo / "tests" / "gate" / "test_gate.py").write_text(
        "def test_gate():\n    assert True\n",
        encoding="utf-8",
    )
    config = _config(repo / "configs" / "integration.yaml", mode=mode)
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    main_sha = _git(repo, "rev-parse", "HEAD")

    _git(repo, "switch", "-c", "integration")
    (repo / "product.txt").write_text("integrated\n", encoding="utf-8")
    _git(repo, "add", "product.txt")
    _git(repo, "commit", "-m", "integration")
    integration_sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "switch", "main")

    evidence_root = repo / "var" / "gate-evidence"
    evidence_root.mkdir(parents=True)
    signing_key = os.urandom(32)
    key_path = evidence_root / "signing.key"
    key_path.write_bytes(signing_key)
    key_path.chmod(0o600)
    operator_key = Ed25519PrivateKey.generate()
    public = operator_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    (evidence_root / "operator-authorization.pub").write_bytes(public)

    authorship = _write_authorship(
        tmp_path / "authorship.json",
        key=signing_key,
        candidate_sha=integration_sha,
    )
    reviews = (
        _write_review(
            tmp_path / "opus-review.md",
            model="claude-opus-5",
            family="anthropic",
            candidate_sha=integration_sha,
        ),
        _write_review(
            tmp_path / "kimi-review.md",
            model="kimi-k3",
            family="moonshot",
            candidate_sha=integration_sha,
        ),
    )
    assert _git(repo, "status", "--porcelain=v1", "--untracked-files=all") == ""
    return (
        repo,
        main_sha,
        integration_sha,
        config,
        authorship,
        reviews,
        signing_key,
        operator_key,
    )


def _request(
    repo: Path,
    main_sha: str,
    integration_sha: str,
    config: Path,
    authorship: Path,
    reviews: tuple[Path, ...],
    *,
    enforce: bool,
) -> PromotionRequest:
    evidence_root = repo / "var" / "gate-evidence"
    return PromotionRequest(
        repo=repo,
        expected_main_sha=main_sha,
        integration_sha=integration_sha,
        authorship_manifest=authorship,
        aggregate_reviews=reviews,
        config_path=config,
        evidence_root=evidence_root,
        lock_path=evidence_root / "locks" / "promotion.lock",
        candidate_ref=_CANDIDATE_REF,
        enforce=enforce,
    )


def _write_reports_and_operator(
    tmp_path: Path,
    request: PromotionRequest,
    operator_key: Ed25519PrivateKey,
    *,
    start: datetime | None = None,
) -> PromotionRequest:
    first_time = start or datetime.now(UTC) - timedelta(seconds=5)
    first = PromotionFinalizer(clock=lambda: first_time).run(replace(request, enforce=False))
    second = PromotionFinalizer(clock=lambda: first_time + timedelta(seconds=2)).run(
        replace(request, enforce=False)
    )
    assert first.report_receipt is not None and second.report_receipt is not None
    first_path = tmp_path / "report-1.json"
    second_path = tmp_path / "report-2.json"
    first_path.write_text(json.dumps(first.report_receipt), encoding="utf-8")
    second_path.write_text(json.dumps(second.report_receipt), encoding="utf-8")
    report_ids = [
        first.report_receipt["report_id"],
        second.report_receipt["report_id"],
    ]
    authorization: dict[str, Any] = {
        "schema": promote._OPERATOR_SCHEMA,
        "authorized": True,
        "subject_digest": first.subject_digest,
        "report_ids": report_ids,
        "operator_id": "owner",
        "issued_at": promote._utc_text(first_time + timedelta(seconds=3)),
        "nonce": "a" * 32,
    }
    authorization["signature"] = base64.b64encode(
        operator_key.sign(_canonical(authorization))
    ).decode()
    auth_path = tmp_path / "operator-authorization.json"
    auth_path.write_text(json.dumps(authorization), encoding="utf-8")
    return replace(
        request,
        report_receipts=(first_path, second_path),
        operator_authorization=auth_path,
        enforce=True,
    )


def _make_evidence(
    request: GateRunRequest,
    store: GateEvidenceStore,
    *,
    overrides: dict[str, Any] | None = None,
) -> GateEvidence:
    values = dict(overrides or {})
    command = str(values.pop("command", request.gate_config["command"]))
    targets = tuple(values.pop("targets", command.split()[1:]))
    iteration = int(values.pop("iteration", request.iteration))
    routine_id = str(values.pop("routine_id", request.routine_id))
    run_id = str(values.pop("run_id", request.run_id))
    gate_type = str(values.pop("gate_type", request.gate_type))
    workspace_digest = str(values.pop("workspace_digest", workspace_digest_for(request.workspace)))
    candidate_sha = str(values.pop("candidate_sha", request.candidate_sha))
    merge_base_sha = str(values.pop("merge_base_sha", request.merge_base_sha))
    now = utc_now_iso()
    started_at = str(values.pop("started_at", now))
    finished_at = str(values.pop("finished_at", now))
    evidence = GateEvidence(
        schema=str(values.pop("schema", SCHEMA)),
        routine_id=routine_id,
        run_id=run_id,
        iteration=iteration,
        gate_type=gate_type,
        command=command,
        targets=targets,
        workspace_digest=workspace_digest,
        binding_digest=binding_digest(
            routine_id=routine_id,
            run_id=run_id,
            iteration=iteration,
            gate_type=gate_type,
            command=command,
            targets=targets,
            workspace_digest=workspace_digest,
            candidate_sha=candidate_sha,
            merge_base_sha=merge_base_sha,
        ),
        tool=str(values.pop("tool", "pytest")),
        tool_version="9.0.0",
        exit_code=0,
        checks_collected=1,
        checks_passed=1,
        checks_skipped=0,
        checks_failed=0,
        started_at=started_at,
        finished_at=finished_at,
        nonce=secrets_token(),
        workspace_sha=str(values.pop("workspace_sha", request.candidate_sha)),
        workspace_tree_clean=True,
        interpreter="/usr/bin/python3",
        interpreter_version="3.12.0",
        node_inventory_digest=hashlib.sha256(b"gate").hexdigest(),
        deselected_count=0,
        candidate_sha=candidate_sha,
        merge_base_sha=merge_base_sha,
    )
    assert not values
    return store.record(evidence)


def secrets_token() -> str:
    return os.urandom(16).hex()


def _receipt_producer(
    *,
    overrides: dict[str, Any] | None = None,
    produced_new: bool = True,
    contract_override: str | None = None,
):
    def produce(
        request: GateRunRequest,
        store: GateEvidenceStore,
        contract_digest: str,
    ) -> ProducedReceipt:
        evidence = _make_evidence(request, store, overrides=overrides)
        return ProducedReceipt(
            evidence=evidence,
            request_contract_digest=contract_override or contract_digest,
            produced_new=produced_new,
        )

    return produce


def _passing_gate(repo: Path, candidate_ref: str, receipt_path: Path) -> str:
    assert _git(repo, "rev-parse", f"{candidate_ref}^{{commit}}")
    assert receipt_path.is_file()
    return f"MERGE GATE: PASS — {candidate_ref} is safe to merge\n"


def _archdocs_commit(worktree: Path) -> str:
    (worktree / "ARCHI.md").write_text("refreshed architecture\n", encoding="utf-8")
    _git(worktree, "add", "ARCHI.md")
    _git(worktree, "commit", "-m", "archi-morning: refresh map + diagram")
    return _git(worktree, "rev-parse", "HEAD")


def _ready(
    tmp_path: Path,
    *,
    mode: str = "enforce",
    archdocs_runner=_archdocs_commit,
    receipt_producer=None,
    checkpoint=None,
) -> tuple[PromotionFinalizer, PromotionRequest, Path, str, str]:
    repo, main, integration, config, authorship, reviews, _, operator_key = _repo(
        tmp_path,
        mode=mode,
    )
    request = _request(
        repo,
        main,
        integration,
        config,
        authorship,
        reviews,
        enforce=True,
    )
    request = _write_reports_and_operator(tmp_path, request, operator_key)
    finalizer = PromotionFinalizer(
        receipt_producer=receipt_producer or _receipt_producer(),
        merge_gate_runner=_passing_gate,
        archdocs_runner=archdocs_runner,
        checkpoint=checkpoint,
        clock=lambda: datetime.now(UTC),
    )
    return finalizer, request, repo, main, integration


def _assert_prepublication_refusal(repo: Path, main_sha: str, request: PromotionRequest) -> None:
    assert _git(repo, "rev-parse", "refs/heads/main") == main_sha
    assert _git(repo, "rev-parse", "HEAD") == main_sha
    assert _git(repo, "status", "--porcelain=v1", "--untracked-files=all") == ""
    assert not any((request.evidence_root / "promotion-reports").glob("*/finalized.json"))


def test_report_mode_is_filesystem_and_git_read_only(tmp_path: Path) -> None:
    repo, main, integration, config, authorship, reviews, _, _ = _repo(
        tmp_path,
        mode="report",
    )
    request = _request(
        repo,
        main,
        integration,
        config,
        authorship,
        reviews,
        enforce=False,
    )
    refs_before = _git(repo, "show-ref")
    files_before = sorted(str(path.relative_to(repo)) for path in repo.rglob("*"))
    index_before = (repo / ".git" / "index").read_bytes()
    index_mtime_before = (repo / ".git" / "index").stat().st_mtime_ns
    result = PromotionFinalizer().run(request)
    files_after = sorted(str(path.relative_to(repo)) for path in repo.rglob("*"))
    assert result.status == "report"
    assert result.report_receipt is not None
    assert refs_before == _git(repo, "show-ref")
    assert files_before == files_after
    assert (repo / ".git" / "index").read_bytes() == index_before
    assert (repo / ".git" / "index").stat().st_mtime_ns == index_mtime_before
    assert not request.lock_path.exists()


def test_default_archdocs_rehearsal_refuses_before_main_mutation(tmp_path: Path) -> None:
    finalizer, request, repo, main, _ = _ready(
        tmp_path,
        archdocs_runner=promote._default_archdocs_runner,
    )
    with pytest.raises(PromotionRefusal):
        finalizer.run(request)
    _assert_prepublication_refusal(repo, main, request)


def test_environment_override_cannot_supply_human_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    finalizer, request, repo, main, _ = _ready(tmp_path, mode="report")
    monkeypatch.setenv("OMNIAGENTOS_AUTO_PROMOTE", "enforce")
    with pytest.raises(PromotionRefusal, match="environment promotion override"):
        finalizer.run(request)
    _assert_prepublication_refusal(repo, main, request)


def test_two_report_cycles_pause_and_operator_signature_are_required(tmp_path: Path) -> None:
    repo, main, integration, config, authorship, reviews, _, _ = _repo(tmp_path)
    request = _request(
        repo,
        main,
        integration,
        config,
        authorship,
        reviews,
        enforce=True,
    )
    with pytest.raises(PromotionRefusal, match="two clean report-cycle"):
        PromotionFinalizer().run(request)


def test_report_cycles_must_satisfy_configured_pause(tmp_path: Path) -> None:
    repo, main, integration, config, authorship, reviews, _, _ = _repo(tmp_path)
    request = _request(
        repo,
        main,
        integration,
        config,
        authorship,
        reviews,
        enforce=False,
    )
    observed = datetime.now(UTC) - timedelta(seconds=2)
    first = PromotionFinalizer(clock=lambda: observed).run(request)
    second = PromotionFinalizer(clock=lambda: observed).run(request)
    assert first.report_receipt is not None and second.report_receipt is not None
    paths = (tmp_path / "same-time-a.json", tmp_path / "same-time-b.json")
    paths[0].write_text(json.dumps(first.report_receipt), encoding="utf-8")
    paths[1].write_text(json.dumps(second.report_receipt), encoding="utf-8")
    with pytest.raises(PromotionRefusal, match="configured pause"):
        PromotionFinalizer().run(replace(request, enforce=True, report_receipts=paths))


def test_operator_authorization_signature_is_binding(tmp_path: Path) -> None:
    finalizer, request, repo, main, _ = _ready(tmp_path)
    assert request.operator_authorization is not None
    body = json.loads(request.operator_authorization.read_text(encoding="utf-8"))
    body["signature"] = base64.b64encode(b"x" * 64).decode()
    request.operator_authorization.write_text(json.dumps(body), encoding="utf-8")
    with pytest.raises(PromotionRefusal, match="signature is invalid"):
        finalizer.run(request)
    _assert_prepublication_refusal(repo, main, request)


def test_success_publishes_exact_terminal_sha_with_one_cas(tmp_path: Path) -> None:
    transitions: list[tuple[str, str]] = []

    def checkpoint(name: str, request: PromotionRequest, _promotion: str, final: str) -> None:
        if name == "publication_boundary":
            transitions.append((_git(request.repo, "rev-parse", "refs/heads/main"), final))

    finalizer, request, repo, main, integration = _ready(tmp_path, checkpoint=checkpoint)
    result = finalizer.run(request)
    assert result.status == "finalized"
    assert transitions == [(main, result.final_main_sha)]
    assert _git(repo, "rev-parse", "refs/heads/main") == result.final_main_sha
    assert _git(repo, "show", "-s", "--format=%P", result.code_main_sha).split() == [
        main,
        result.promotion_sha,
    ]
    assert _git(repo, "show", "-s", "--format=%P", result.final_main_sha).split() == [
        result.code_main_sha
    ]
    assert _git(repo, "merge-base", "--is-ancestor", integration, result.final_main_sha) == ""
    assert _git(repo, "status", "--porcelain=v1", "--untracked-files=all") == ""
    assert _git(repo, "rev-parse", "--verify", request.candidate_ref, check=False) == ""


def test_main_publication_cas_refuses_exact_boundary_race(tmp_path: Path) -> None:
    racer: list[str] = []

    def checkpoint(name: str, request: PromotionRequest, _promotion: str, _final: str) -> None:
        if name == "before_main_cas":
            racer.append(
                _git(
                    request.repo,
                    "commit-tree",
                    _git(request.repo, "rev-parse", f"{request.expected_main_sha}^{{tree}}"),
                    "-p",
                    request.expected_main_sha,
                    "-m",
                    "racer",
                )
            )
            _git(
                request.repo,
                "update-ref",
                "refs/heads/main",
                racer[0],
                request.expected_main_sha,
            )

    finalizer, request, repo, _, _ = _ready(tmp_path, checkpoint=checkpoint)
    with pytest.raises(PromotionRefusal, match="authoritative main CAS refused"):
        finalizer.run(request)
    assert _git(repo, "rev-parse", "refs/heads/main") == racer[0]
    assert not (repo / ".git" / "MERGE_HEAD").exists()


@pytest.mark.parametrize(
    ("label", "overrides", "produced_new", "contract_override", "message"),
    [
        ("wrong_iteration", {"iteration": 99}, True, None, "receipt iteration"),
        (
            "wrong_command",
            {"command": "pytest tests/other", "targets": ("tests/other",)},
            True,
            None,
            "receipt command",
        ),
        ("wrong_targets", {"targets": ("tests/other",)}, True, None, "receipt targets"),
        (
            "wrong_workspace",
            {"workspace_digest": "f" * 64},
            True,
            None,
            "receipt workspace",
        ),
        ("wrong_tool", {"tool": "not-pytest"}, True, None, "receipt tool"),
        ("wrong_config_digest", {}, True, "f" * 64, "contract digest"),
        (
            "stale_receipt",
            {
                "started_at": "2000-01-01T00:00:00Z",
                "finished_at": "2000-01-01T00:00:01Z",
            },
            True,
            None,
            "predates",
        ),
        (
            "replayed_run_subject",
            {"run_id": "e" * 40},
            True,
            None,
            "receipt run",
        ),
        ("replayed", {}, False, None, "not freshly produced"),
    ],
)
def test_signed_receipt_mismatch_refuses_before_publication(
    tmp_path: Path,
    label: str,
    overrides: dict[str, Any],
    produced_new: bool,
    contract_override: str | None,
    message: str,
) -> None:
    finalizer, request, repo, main, _ = _ready(
        tmp_path,
        receipt_producer=_receipt_producer(
            overrides=overrides,
            produced_new=produced_new,
            contract_override=contract_override,
        ),
    )
    with pytest.raises(PromotionRefusal, match=message):
        finalizer.run(request)
    _assert_prepublication_refusal(repo, main, request)


@pytest.mark.parametrize(
    "kind",
    [
        "reset_to_base",
        "reparent",
        "extra_commit",
        "drop_product",
        "modify_non_oracle",
    ],
)
def test_archdocs_history_and_tree_counterfeits_refuse(
    tmp_path: Path,
    kind: str,
) -> None:
    original: dict[str, str] = {}

    def counterfeit(worktree: Path) -> str:
        code = _git(worktree, "rev-parse", "HEAD")
        original["code"] = code
        if kind == "reset_to_base":
            base = _git(worktree, "rev-list", "--max-parents=0", "HEAD")
            _git(worktree, "reset", "--hard", base)
        elif kind == "reparent":
            parent = _git(worktree, "rev-parse", f"{code}^")
            _git(worktree, "reset", "--hard", parent)
        elif kind == "extra_commit":
            _git(worktree, "commit", "--allow-empty", "-m", "unapproved middle commit")
        elif kind == "drop_product":
            (worktree / "product.txt").unlink()
            _git(worktree, "add", "-u")
        elif kind == "modify_non_oracle":
            (worktree / "base.txt").write_text("tampered\n", encoding="utf-8")
            _git(worktree, "add", "base.txt")
        (worktree / "ARCHI.md").write_text(f"{kind}\n", encoding="utf-8")
        _git(worktree, "add", "ARCHI.md")
        _git(worktree, "commit", "-m", "archi counterfeit")
        return _git(worktree, "rev-parse", "HEAD")

    finalizer, request, repo, main, _ = _ready(tmp_path, archdocs_runner=counterfeit)
    with pytest.raises(PromotionRefusal):
        finalizer.run(request)
    _assert_prepublication_refusal(repo, main, request)


def test_lock_inode_replacement_refuses_before_main_cas(tmp_path: Path) -> None:
    def checkpoint(name: str, request: PromotionRequest, _promotion: str, _final: str) -> None:
        if name == "gate_passed":
            replacement = request.lock_path.with_suffix(".replacement")
            request.lock_path.rename(replacement)
            request.lock_path.write_text("replacement\n", encoding="utf-8")

    finalizer, request, repo, main, _ = _ready(tmp_path, checkpoint=checkpoint)
    with pytest.raises(PromotionRefusal, match="canonical lock inode changed"):
        finalizer.run(request)
    _assert_prepublication_refusal(repo, main, request)


def test_index_interlock_excludes_concurrent_archi_morning_git_mutation(
    tmp_path: Path,
) -> None:
    attempted: list[subprocess.CompletedProcess[str]] = []

    def checkpoint(name: str, request: PromotionRequest, _promotion: str, _final: str) -> None:
        if name == "gate_passed":
            attempted.append(
                subprocess.run(
                    ["git", "add", "ARCHI.md"],
                    cwd=request.repo,
                    capture_output=True,
                    text=True,
                    check=False,
                )
            )

    finalizer, request, repo, _, _ = _ready(tmp_path, checkpoint=checkpoint)
    result = finalizer.run(request)
    assert result.status == "finalized"
    assert len(attempted) == 1
    assert attempted[0].returncode != 0
    assert "index.lock" in attempted[0].stderr
    assert _git(repo, "status", "--porcelain=v1", "--untracked-files=all") == ""


@pytest.mark.parametrize("replacement", ["unlink", "rename"])
def test_lock_path_replacement_cannot_split_exclusion(
    tmp_path: Path,
    replacement: str,
) -> None:
    finalizer, request, repo, main, _ = _ready(tmp_path)
    del finalizer
    lock = promote._StableLock.acquire(request, repo)
    try:
        displaced = request.lock_path.with_suffix(".displaced")
        if replacement == "rename":
            request.lock_path.rename(displaced)
        else:
            request.lock_path.unlink()
        request.lock_path.write_text("replacement\n", encoding="utf-8")
        with pytest.raises(PromotionRefusal, match="another promotion holds"):
            promote._StableLock.acquire(request, repo)
    finally:
        warnings = lock.release()
    assert any("replaced" in warning or "foreign" in warning for warning in warnings)
    assert _git(repo, "rev-parse", "refs/heads/main") == main


def test_enforce_rejects_alternate_lock_and_evidence_root(tmp_path: Path) -> None:
    finalizer, request, repo, main, _ = _ready(tmp_path)
    alternate = tmp_path / "alternate-root"
    alternate.mkdir()
    with pytest.raises(PromotionRefusal, match="canonical repository promotion lock"):
        finalizer.run(
            replace(
                request,
                evidence_root=alternate,
                lock_path=alternate / "promotion.lock",
            )
        )
    _assert_prepublication_refusal(repo, main, request)


def test_enforce_rejects_alternate_lock_path_alone(tmp_path: Path) -> None:
    finalizer, request, repo, main, _ = _ready(tmp_path)
    with pytest.raises(PromotionRefusal, match="canonical repository promotion lock"):
        finalizer.run(replace(request, lock_path=tmp_path / "different.lock"))
    _assert_prepublication_refusal(repo, main, request)


def test_enforce_rejects_symlinked_lock_parent(tmp_path: Path) -> None:
    finalizer, request, repo, main, _ = _ready(tmp_path)
    locks = request.evidence_root / "locks"
    external = tmp_path / "external-locks"
    external.mkdir()
    locks.symlink_to(external, target_is_directory=True)
    with pytest.raises(PromotionRefusal, match="canonical lock parent is unsafe"):
        finalizer.run(request)
    _assert_prepublication_refusal(repo, main, request)


def test_candidate_import_requires_inode_containment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    inside = candidate / "omniagentos" / "__init__.py"
    inside.parent.mkdir()
    inside.write_text("", encoding="utf-8")
    outside = tmp_path / "operator-checkout" / "omniagentos" / "__init__.py"
    outside.parent.mkdir(parents=True)
    outside.write_text("", encoding="utf-8")

    def report_import(path: Path) -> None:
        monkeypatch.setattr(
            promote,
            "_run",
            lambda *_args, **_kwargs: subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=f"{path}\n",
                stderr="",
            ),
        )

    report_import(inside)
    promote._assert_candidate_import(candidate)

    report_import(outside)
    with pytest.raises(PromotionRefusal, match="imports outside candidate tree"):
        promote._assert_candidate_import(candidate)

    linked = candidate / "linked.py"
    linked.symlink_to(outside)
    report_import(linked)
    with pytest.raises(PromotionRefusal, match="imports outside candidate tree"):
        promote._assert_candidate_import(candidate)


def test_missing_trust_key_is_not_bootstrapped(tmp_path: Path) -> None:
    repo, main, integration, config, authorship, reviews, _, _ = _repo(tmp_path)
    key = repo / "var" / "gate-evidence" / "signing.key"
    key.unlink()
    request = _request(
        repo,
        main,
        integration,
        config,
        authorship,
        reviews,
        enforce=False,
    )
    with pytest.raises(PromotionRefusal, match="pre-existing"):
        PromotionFinalizer().run(request)
    assert not key.exists()


def test_prose_lineage_marker_cannot_authorize(tmp_path: Path) -> None:
    repo, main, integration, config, authorship, reviews, _, _ = _repo(tmp_path)
    prose = _write_review(
        tmp_path / "prose.md",
        model="claude-opus-5",
        family="anthropic",
        candidate_sha=integration,
        structured=False,
    )
    request = _request(
        repo,
        main,
        integration,
        config,
        authorship,
        (prose, reviews[1]),
        enforce=False,
    )
    with pytest.raises(PromotionRefusal, match="structured trailers"):
        PromotionFinalizer().run(request)


def test_same_family_approval_is_non_authorizing(tmp_path: Path) -> None:
    repo, main, integration, config, _, reviews, key, _ = _repo(tmp_path)
    authorship = _write_authorship(
        tmp_path / "anthropic-authorship.json",
        key=key,
        candidate_sha=integration,
        records=[{"role": "coder", "model": "claude-opus-5", "family": "anthropic"}],
    )
    same = _write_review(
        tmp_path / "same.md",
        model="claude-opus-5",
        family="anthropic",
        candidate_sha=integration,
        authorship_families=("anthropic",),
    )
    outside = _write_review(
        tmp_path / "outside.md",
        model="kimi-k3",
        family="moonshot",
        candidate_sha=integration,
        authorship_families=("anthropic",),
    )
    request = _request(
        repo,
        main,
        integration,
        config,
        authorship,
        (same, outside),
        enforce=False,
    )
    with pytest.raises(PromotionRefusal, match="same-family"):
        PromotionFinalizer().run(request)


def test_rework_family_joins_complete_authorship_set(tmp_path: Path) -> None:
    repo, main, integration, config, _, _, key, _ = _repo(tmp_path)
    families = ("anthropic", "openai")
    authorship = _write_authorship(
        tmp_path / "rework-authorship.json",
        key=key,
        candidate_sha=integration,
        records=[
            {"role": "coder", "model": "gpt-5.6-sol", "family": "openai"},
            {"role": "coder_rework", "model": "claude-opus-5", "family": "anthropic"},
        ],
    )
    opus = _write_review(
        tmp_path / "opus-same.md",
        model="claude-opus-5",
        family="anthropic",
        candidate_sha=integration,
        authorship_families=families,
    )
    kimi = _write_review(
        tmp_path / "kimi-outside.md",
        model="kimi-k3",
        family="moonshot",
        candidate_sha=integration,
        authorship_families=families,
    )
    request = _request(
        repo,
        main,
        integration,
        config,
        authorship,
        (opus, kimi),
        enforce=False,
    )
    with pytest.raises(PromotionRefusal, match="same-family"):
        PromotionFinalizer().run(request)


def test_sensitive_candidate_requires_two_distinct_outside_families(tmp_path: Path) -> None:
    repo, main, integration, config, authorship, _, _, _ = _repo(tmp_path)
    one = _write_review(
        tmp_path / "opus-a.md",
        model="claude-opus-5",
        family="anthropic",
        candidate_sha=integration,
    )
    duplicate = _write_review(
        tmp_path / "opus-b.md",
        model="claude-fable-5",
        family="anthropic",
        candidate_sha=integration,
    )
    request = _request(
        repo,
        main,
        integration,
        config,
        authorship,
        (one, duplicate),
        enforce=False,
    )
    with pytest.raises(PromotionRefusal, match="two distinct"):
        PromotionFinalizer().run(request)


def test_same_family_reject_remains_binding(tmp_path: Path) -> None:
    repo, main, integration, config, authorship, reviews, _, _ = _repo(tmp_path)
    reject = _write_review(
        tmp_path / "reject.md",
        model="gpt-5.6-sol",
        family="openai",
        candidate_sha=integration,
        verdict="REJECT",
    )
    request = _request(
        repo,
        main,
        integration,
        config,
        authorship,
        (reject, *reviews),
        enforce=False,
    )
    with pytest.raises(PromotionRefusal, match="binding review rejects"):
        PromotionFinalizer().run(request)


def test_review_candidate_sha_mismatch_refuses(tmp_path: Path) -> None:
    repo, main, integration, config, authorship, reviews, _, _ = _repo(tmp_path)
    wrong = _write_review(
        tmp_path / "wrong-sha.md",
        model="claude-opus-5",
        family="anthropic",
        candidate_sha="f" * 40,
    )
    request = _request(
        repo,
        main,
        integration,
        config,
        authorship,
        (wrong, reviews[1]),
        enforce=False,
    )
    with pytest.raises(PromotionRefusal, match="different candidate SHA"):
        PromotionFinalizer().run(request)


def test_candidate_hijack_root_cause_is_not_masked_by_cleanup(tmp_path: Path) -> None:
    foreign: list[str] = []

    def checkpoint(name: str, request: PromotionRequest, _promotion: str, _final: str) -> None:
        if name == "receipt_verified":
            foreign.append(request.integration_sha)
            _git(request.repo, "update-ref", request.candidate_ref, request.integration_sha)

    finalizer, request, repo, main, _ = _ready(tmp_path, checkpoint=checkpoint)
    with pytest.raises(
        PromotionRefusal,
        match=r"^candidate ref moved: expected .* found",
    ):
        finalizer.run(request)
    assert _git(repo, "rev-parse", request.candidate_ref) == foreign[0]
    assert _git(repo, "rev-parse", "refs/heads/main") == main


def test_gate_failure_cleans_candidate_without_global_worktree_prune(tmp_path: Path) -> None:
    def reject_gate(_repo: Path, _ref: str, _receipt: Path) -> str:
        raise PromotionRefusal("synthetic gate refusal")

    finalizer, request, repo, main, _ = _ready(tmp_path)
    finalizer.merge_gate_runner = reject_gate
    before = _git(repo, "worktree", "list", "--porcelain")
    with pytest.raises(PromotionRefusal, match="synthetic gate refusal"):
        finalizer.run(request)
    assert _git(repo, "rev-parse", "refs/heads/main") == main
    assert _git(repo, "worktree", "list", "--porcelain") == before
    assert _git(repo, "rev-parse", "--verify", request.candidate_ref, check=False) == ""


def test_post_publication_state_failure_is_truthful_and_nonthrowing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    finalizer, request, repo, main, _ = _ready(tmp_path)
    original = promote._atomic_idempotent

    def fail_terminal(path: Path, body: bytes) -> None:
        if path.name in {"finalized.json", "published_recovery_required.json"}:
            raise OSError("synthetic terminal receipt failure")
        original(path, body)

    monkeypatch.setattr(promote, "_atomic_idempotent", fail_terminal)
    result = finalizer.run(request)
    assert result.status == "published_recovery_required"
    assert _git(repo, "rev-parse", "refs/heads/main") != main
    assert _git(repo, "rev-parse", request.candidate_ref) == result.promotion_sha
    assert any("terminal state artifact publication failed" in item for item in result.warnings)
    prepared = request.evidence_root / "promotion-reports" / result.promotion_sha / "prepared.json"
    assert prepared.is_file()


def test_post_publication_worktree_reset_failure_returns_recovery_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    finalizer, request, repo, main, _ = _ready(tmp_path)
    original = promote._run

    def fail_reset(
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str] | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        if argv[:3] == ["git", "reset", "--hard"]:
            return subprocess.CompletedProcess(argv, 1, "", "synthetic reset failure")
        return original(argv, cwd=cwd, env=env, check=check)

    monkeypatch.setattr(promote, "_run", fail_reset)
    result = finalizer.run(request)
    assert result.status == "published_recovery_required"
    assert _git(repo, "rev-parse", "refs/heads/main") != main
    assert _git(repo, "rev-parse", request.candidate_ref) == result.promotion_sha
    assert not (repo / ".git" / "MERGE_HEAD").exists()
    assert any("worktree needs recovery" in warning for warning in result.warnings)


def test_success_report_retains_complete_audit_identity(tmp_path: Path) -> None:
    finalizer, request, _, _, _ = _ready(tmp_path)
    result = finalizer.run(request)
    prepared_path = (
        request.evidence_root / "promotion-reports" / result.promotion_sha / "prepared.json"
    )
    prepared = json.loads(prepared_path.read_text(encoding="utf-8"))
    assert prepared["status"] == "prepared"
    assert prepared["config_sha256"] == result.config_sha256
    assert prepared["authorship_sha256"] == result.authorship_sha256
    assert prepared["review_sha256"] == list(result.review_sha256)
    assert prepared["authorship_families"] == list(result.authorship_families)
    assert prepared["reviewer_families"] == list(result.reviewer_families)
    assert prepared["report_cycle_ids"] == list(result.report_cycle_ids)
    assert prepared["operator_authorization_sha256"]
    assert (
        Path(prepared["merge_gate_output_path"])
        .read_text(encoding="utf-8")
        .startswith("MERGE GATE: PASS")
    )
    artifact_dir = prepared_path.parent
    assert json.loads((artifact_dir / "published.json").read_text())["status"] == "published"
    assert json.loads((artifact_dir / "finalized.json").read_text())["status"] == "finalized"
