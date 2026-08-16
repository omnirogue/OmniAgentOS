"""Integrity tests for the sticky North Star mask-evidence registry."""

from __future__ import annotations

import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
import yaml

from scripts.northstar_cert import record_results as recorder
from scripts.northstar_cert.record_results import (
    CheckVerdict,
    JUnitOutcome,
    ManifestCheck,
    WriterEvidence,
    evaluate_checks,
    runnable_targets,
)

MANIFEST = Path("configs/northstar-cert/manifest.yaml")
WRITERS = Path("configs/northstar-cert/writers.yaml")


def _manifest() -> dict[str, object]:
    document = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def _check(check_id: str = "NSC-C99-PROOF") -> ManifestCheck:
    return ManifestCheck(
        id=check_id,
        capability="C-99",
        binding_type="pytest",
        target="tests/example.py::test_carrier",
        tier="t1",
        gate=True,
        requires=(),
        scope="scenario",
        provenance=("writer-registry-test",),
    )


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


def _history_repo(tmp_path: Path, *, token: str = "writer:historical") -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    config = repo / "configs/northstar-cert"
    config.mkdir(parents=True)
    manifest = config / "manifest.yaml"
    manifest.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "checks": [
                    {
                        "id": "NSC-C99-HISTORY",
                        "capability": "C-99",
                        "binding": {
                            "type": "pytest",
                            "target": "tests/example.py::test_carrier",
                        },
                        "tier": "t1",
                        "gate": True,
                        "requires": [token],
                        "scope": "scenario",
                        "provenance": ["fixture"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    registry = config / "writers.yaml"
    registry.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "tokens": {
                    token: {
                        "gates": ["NSC-C99-HISTORY"],
                        "witness": None,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    _git(repo, "init", "-b", "main")
    _git(repo, "add", "configs/northstar-cert/manifest.yaml", "configs/northstar-cert/writers.yaml")
    _git(
        repo,
        "-c",
        "user.name=NorthStar Test",
        "-c",
        "user.email=northstar@example.invalid",
        "commit",
        "-m",
        "seed writer history",
    )
    return manifest, registry


def test_writer_registry_is_a_complete_sticky_reverse_index() -> None:
    """All current colon-shaped masks are covered, while gates stay sticky.

    Deliberately do not require a registry gate to retain the token in the
    manifest: that assertion would make a bare deletion fail a meta-test, but
    would put the obligation back in the mutable line instead of proving that
    runtime adjudication remains safe after the deletion.
    """

    checks = _manifest()["checks"]
    assert isinstance(checks, list)
    check_ids = {item["id"] for item in checks}
    registry = recorder._load_writer_registry(WRITERS, repo_root=Path.cwd())

    unknown_gates = {
        token: sorted(set(entry.gates) - check_ids)
        for token, entry in registry.items()
        if set(entry.gates) - check_ids
    }
    assert not unknown_gates

    uncovered = {
        (item["id"], requirement)
        for item in checks
        for requirement in item["requires"]
        if recorder._is_mask_token(requirement)
        and (requirement not in registry or item["id"] not in registry[requirement].gates)
    }
    assert not uncovered
    assert registry, "writer registry must not degrade into a vacuous empty mapping"


def test_real_writer_registry_retains_every_historical_token() -> None:
    # This checkout is a plain directory export, not a git checkout (no
    # `.git` at the repo root) -- the estate's own release process cuts it
    # this way, so there is no commit history for `configs/northstar-cert/
    # {manifest,writers}.yaml` to validate the mask registry against here.
    # `_writer_registry_for_manifest` is estate-bound on real git history the
    # same way harness-audit and shim-patch lanes are estate-bound on real
    # dev-task artifacts; skip with the same explicit-reason discipline
    # rather than fail on a git error that names no actionable fix in this
    # checkout.
    if not (Path.cwd() / ".git").exists():
        pytest.skip(
            f"{Path.cwd()} is not a git checkout (no .git) -- the sticky "
            "writer registry's history validation has no commit history to "
            "check here"
        )
    registry = recorder._writer_registry_for_manifest(MANIFEST, repo_root=Path.cwd())
    assert registry, "history-validated writer gating must be active"


def test_near_duplicate_writer_tokens_remain_distinct() -> None:
    registry = recorder._load_writer_registry(WRITERS, repo_root=Path.cwd())
    for left, right in (
        ("writer:duplicate-detector", "writer:duplication-detector"),
        ("writer:loopqueue-priority", "writer:normalized-loopqueue-priority"),
        ("writer:queue-aging", "writer:aging-priority"),
    ):
        assert left in registry and right in registry and left != right


def test_witness_resolution_requires_a_real_ast_symbol(tmp_path: Path) -> None:
    source = tmp_path / "writer.py"
    source.write_text("class RealWriter:\n    enabled = True\n", encoding="utf-8")

    assert recorder._witness_resolves("writer.py", tmp_path)
    assert recorder._witness_resolves("writer.py::RealWriter", tmp_path)
    assert recorder._witness_resolves("writer.py::RealWriter.enabled", tmp_path)
    assert not recorder._witness_resolves("writer.py::MentionedOnlyInAComment", tmp_path)
    assert not recorder._witness_resolves("../writer.py::RealWriter", tmp_path)
    assert not recorder._witness_resolves(None, tmp_path)


def test_existence_only_injected_witness_cannot_satisfy_writer_gate(tmp_path: Path) -> None:
    """Reproduce the round-2 existence-only witness attack without the loader."""

    (tmp_path / "unrelated.py").write_text("def import_ok():\n    return True\n")
    check = _check()
    registry = {
        "writer:exact": WriterEvidence(
            gates=(check.id,),
            witness="unrelated.py::import_ok",
            proof=None,
        )
    }
    result = evaluate_checks(
        [check],
        [JUnitOutcome(check.target, "passed")],
        repo_root=tmp_path,
        writer_registry=registry,
    )[0]

    assert result.verdict is CheckVerdict.NOT_EVALUABLE
    assert result.reason == "no_writer_evidence:proof_absent:writer:exact"


def test_nsc_writer_proof_marker_must_name_the_exact_token(tmp_path: Path) -> None:
    proof = tmp_path / "proof.py"
    proof.write_text(
        "import pytest\n\n"
        "@pytest.mark.nsc_writer_proof('writer:exact')\n"
        "def test_exact():\n    pass\n\n"
        "@pytest.mark.nsc_writer_proof('writer:different')\n"
        "def test_wrong():\n    pass\n\n"
        "def test_unmarked():\n    pass\n",
        encoding="utf-8",
    )

    assert recorder._proof_declares_token("proof.py::test_exact", "writer:exact", tmp_path)
    assert not recorder._proof_declares_token("proof.py::test_wrong", "writer:exact", tmp_path)
    assert not recorder._proof_declares_token("proof.py::test_unmarked", "writer:exact", tmp_path)


def test_registry_loader_rejects_an_arbitrary_green_test_as_proof(tmp_path: Path) -> None:
    (tmp_path / "writer.py").write_text("class Writer:\n    pass\n", encoding="utf-8")
    (tmp_path / "proof.py").write_text("def test_unrelated():\n    pass\n", encoding="utf-8")
    registry_path = tmp_path / "writers.yaml"
    registry_path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "tokens": {
                    "writer:exact": {
                        "gates": ["NSC-C99-PROOF"],
                        "witness": "writer.py::Writer",
                        "proof": "proof.py::test_unrelated",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(recorder.CertificationError, match="does not self-declare"):
        recorder._load_writer_registry(registry_path, repo_root=tmp_path)


def test_registry_loader_rejects_bare_truthy_witness_assertion(tmp_path: Path) -> None:
    """``assert Witness`` is existence/truthiness, not capability evidence."""

    token = "writer:vacuous"
    (tmp_path / "writer.py").write_text("class Writer:\n    pass\n", encoding="utf-8")
    (tmp_path / "proof.py").write_text(
        "import pytest\n"
        "from writer import Writer\n\n"
        f"@pytest.mark.nsc_writer_proof({token!r})\n"
        "def test_vacuous():\n"
        "    assert Writer\n",
        encoding="utf-8",
    )
    registry_path = tmp_path / "writers.yaml"
    registry_path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "tokens": {
                    token: {
                        "gates": ["NSC-C99-PROOF"],
                        "witness": "writer.py::Writer",
                        "proof": "proof.py::test_vacuous",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(recorder.CertificationError, match="does not exercise witness"):
        recorder._load_writer_registry(registry_path, repo_root=tmp_path)


def test_witness_swap_to_unrelated_passing_test_does_not_flip_pass(tmp_path: Path) -> None:
    """An exact marker cannot launder a proof that exercises another writer."""

    token = "writer:target"
    (tmp_path / "target_writer.py").write_text("class TargetWriter:\n    pass\n")
    (tmp_path / "other_writer.py").write_text("class OtherWriter:\n    pass\n")
    (tmp_path / "proof.py").write_text(
        "import pytest\n"
        "from other_writer import OtherWriter\n\n"
        "@pytest.mark.nsc_writer_proof('writer:target')\n"
        "def test_other_writer_is_green():\n"
        "    assert OtherWriter\n",
        encoding="utf-8",
    )
    registry_path = tmp_path / "writers.yaml"
    registry_path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "tokens": {
                    token: {
                        "gates": ["NSC-C99-PROOF"],
                        "witness": "target_writer.py::TargetWriter",
                        "proof": "proof.py::test_other_writer_is_green",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(recorder.CertificationError, match="does not exercise witness"):
        recorder._load_writer_registry(
            registry_path,
            repo_root=tmp_path,
            manifest_targets={"NSC-C99-PROOF": "tests/example.py::test_carrier"},
        )


def test_bogus_unresolvable_witness_fails_closed_at_registry_load(tmp_path: Path) -> None:
    token = "writer:missing"
    (tmp_path / "proof.py").write_text(
        "import pytest\n"
        "@pytest.mark.nsc_writer_proof('writer:missing')\n"
        "def test_missing():\n"
        "    assert True\n",
        encoding="utf-8",
    )
    registry_path = tmp_path / "writers.yaml"
    registry_path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "tokens": {
                    token: {
                        "gates": ["NSC-C99-PROOF"],
                        "witness": "does_not_exist.py::MissingWriter",
                        "proof": "proof.py::test_missing",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(recorder.CertificationError, match="does not resolve"):
        recorder._load_writer_registry(registry_path, repo_root=tmp_path)


def test_writer_proof_must_be_distinct_from_every_gated_carrier(tmp_path: Path) -> None:
    token = "writer:distinct"
    (tmp_path / "feature.py").write_text("class Writer:\n    pass\n")
    (tmp_path / "proof.py").write_text(
        "import pytest\n"
        "from feature import Writer\n\n"
        "@pytest.mark.nsc_writer_proof('writer:distinct')\n"
        "def test_writer():\n"
        "    assert Writer.__name__ == 'Writer'\n",
        encoding="utf-8",
    )
    registry_path = tmp_path / "writers.yaml"
    registry_path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "tokens": {
                    token: {
                        "gates": ["NSC-C99-PROOF"],
                        "witness": "feature.py::Writer",
                        "proof": "proof.py::test_writer",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(recorder.CertificationError, match="aliases gated carrier"):
        recorder._load_writer_registry(
            registry_path,
            repo_root=tmp_path,
            manifest_targets={"NSC-C99-PROOF": "proof.py::test_writer[param]"},
        )


@pytest.mark.parametrize("token", ["writer:historical", "glue:historical"])
def test_registry_entry_deletion_fails_closed_against_git_history(
    tmp_path: Path, token: str
) -> None:
    manifest, registry_path = _history_repo(tmp_path, token=token)
    document = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    document["checks"][0]["requires"] = []
    manifest.write_text(yaml.safe_dump(document), encoding="utf-8")
    registry_path.write_text("version: 1\ntokens: {}\n", encoding="utf-8")

    with pytest.raises(recorder.CertificationError, match="deleted historical token/check entries"):
        recorder._writer_registry_for_manifest(manifest, repo_root=manifest.parents[2])


def _land_writer(repo: Path, manifest: Path, registry_path: Path) -> None:
    token = "writer:historical"
    (repo / "feature.py").write_text("class GenuineWriter:\n    enabled = True\n", encoding="utf-8")
    # A landed witness has to be consumed by REAL production code, not only its
    # proof — that is the in-band realness guard. Give GenuineWriter a genuine
    # production consumer under a shipped root so it is not a metric-shaped stub.
    # Committed, so shallow clones of this repo carry it too.
    (repo / "omniagentos").mkdir(exist_ok=True)
    (repo / "omniagentos" / "consumer.py").write_text(
        "from feature import GenuineWriter\n\n\ndef use() -> bool:\n"
        "    return GenuineWriter.enabled\n",
        encoding="utf-8",
    )
    (repo / "proof.py").write_text(
        "import pytest\n"
        "from feature import GenuineWriter\n\n"
        f"@pytest.mark.nsc_writer_proof({token!r})\n"
        "def test_genuine_writer():\n"
        "    assert GenuineWriter.enabled is True\n",
        encoding="utf-8",
    )
    document = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    document["checks"][0]["requires"] = []
    manifest.write_text(yaml.safe_dump(document), encoding="utf-8")
    registry_path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "tokens": {
                    token: {
                        "gates": ["NSC-C99-HISTORY"],
                        "witness": "feature.py::GenuineWriter",
                        "proof": "proof.py::test_genuine_writer",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    _git(
        repo,
        "add",
        "feature.py",
        "omniagentos/consumer.py",
        "proof.py",
        str(manifest.relative_to(repo)),
        str(registry_path.relative_to(repo)),
    )
    _git(
        repo,
        "-c",
        "user.name=NorthStar Test",
        "-c",
        "user.email=northstar@example.invalid",
        "commit",
        "-m",
        "land genuine writer",
    )


def _passing_junit(repo: Path) -> Path:
    root = ET.Element("testsuites")
    suite = ET.SubElement(root, "testsuite", tests="2", failures="0")
    ET.SubElement(suite, "testcase", file="tests/example.py", name="test_carrier")
    ET.SubElement(suite, "testcase", file="proof.py", name="test_genuine_writer")
    path = repo / "junit.xml"
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)
    return path


def test_detached_head_with_genuine_proof_reaches_certified(tmp_path: Path) -> None:
    manifest, registry_path = _history_repo(tmp_path)
    repo = manifest.parents[2]
    _land_writer(repo, manifest, registry_path)
    _git(repo, "checkout", "--detach")

    summary = recorder.record_results(
        manifest_path=manifest,
        junit_path=_passing_junit(repo),
        tier="t1",
        run_id="detached-legitimate",
        db_path=repo / "results.sqlite3",
        evidence_root=repo / "evidence",
        repo_root=repo,
        mission_unmapped=0,
        injection_registry_closed=True,
        live_portfolio_corroborated=True,
    )

    assert summary.writer_gating_active is True
    assert summary.results[0].verdict is CheckVerdict.PASS
    assert summary.verdict is recorder.RunVerdict.CERTIFIED


def test_shallow_clone_fetches_full_history_before_validation(tmp_path: Path) -> None:
    manifest, registry_path = _history_repo(tmp_path / "origin-fixture")
    origin = manifest.parents[2]
    _land_writer(origin, manifest, registry_path)
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "--depth=1", f"file://{origin}", str(clone))

    registry = recorder._writer_registry_for_manifest(
        clone / "configs/northstar-cert/manifest.yaml", repo_root=clone
    )

    assert registry
    assert _git(clone, "rev-parse", "--is-shallow-repository") == "false"


def test_unfetchable_shallow_history_requires_explicit_ack(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest, registry_path = _history_repo(tmp_path / "origin-fixture")
    origin = manifest.parents[2]
    _land_writer(origin, manifest, registry_path)
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "--depth=1", f"file://{origin}", str(clone))
    _git(clone, "remote", "remove", "origin")
    clone_manifest = clone / "configs/northstar-cert/manifest.yaml"

    monkeypatch.delenv(recorder.SHALLOW_HISTORY_ACK_ENV, raising=False)
    with pytest.raises(recorder.CertificationError, match="set NSCERT_ACK_SHALLOW_HISTORY=1"):
        recorder._writer_registry_for_manifest(clone_manifest, repo_root=clone)

    monkeypatch.setenv(recorder.SHALLOW_HISTORY_ACK_ENV, "1")
    assert recorder._writer_registry_for_manifest(clone_manifest, repo_root=clone)
    assert (
        "instrument_warning:mask_registry_history_incomplete:"
        "acknowledged_by=NSCERT_ACK_SHALLOW_HISTORY" in capsys.readouterr().err
    )


def test_runnable_targets_include_a_landed_writers_same_run_proof(tmp_path: Path) -> None:
    (tmp_path / "feature.py").write_text("class Writer:\n    pass\n", encoding="utf-8")
    # A real production consumer so ``Writer`` is not a stub the realness guard refuses.
    (tmp_path / "omniagentos").mkdir()
    (tmp_path / "omniagentos" / "consumer.py").write_text(
        "from feature import Writer\n\n\ndef use() -> type:\n    return Writer\n", encoding="utf-8"
    )
    (tmp_path / "proof.py").write_text(
        "import pytest\n"
        "from feature import Writer\n\n"
        "@pytest.mark.nsc_writer_proof('writer:exact')\n"
        "def test_exact():\n    assert Writer.__name__ == 'Writer'\n",
        encoding="utf-8",
    )
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "checks": [
                    {
                        "id": "NSC-C99-PROOF",
                        "capability": "C-99",
                        "binding": {
                            "type": "pytest",
                            "target": "tests/example.py::test_carrier",
                        },
                        "tier": "t1",
                        "gate": True,
                        "requires": [],
                        "scope": "scenario",
                        "provenance": ["fixture"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "writers.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "tokens": {
                    "writer:exact": {
                        "gates": ["NSC-C99-PROOF"],
                        "witness": "feature.py::Writer",
                        "proof": "proof.py::test_exact",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    registry = recorder._load_writer_registry(
        tmp_path / "writers.yaml",
        repo_root=tmp_path,
        manifest_targets={"NSC-C99-PROOF": "tests/example.py::test_carrier"},
    )
    assert runnable_targets(manifest, "t1", repo_root=tmp_path, writer_registry=registry) == [
        "tests/example.py::test_carrier",
        "proof.py::test_exact",
    ]


@pytest.mark.parametrize(
    ("proof_outcomes", "reason"),
    [
        ([], "no_writer_evidence:proof_not_executed:writer:exact"),
        (
            [JUnitOutcome("proof.py::test_exact", "failure")],
            "no_writer_evidence:proof_not_passing:writer:exact",
        ),
    ],
)
def test_optional_proof_must_appear_and_pass_in_the_same_junit(
    tmp_path: Path, proof_outcomes: list[JUnitOutcome], reason: str
) -> None:
    (tmp_path / "writer.py").write_text("class Writer:\n    pass\n", encoding="utf-8")
    (tmp_path / "proof.py").write_text(
        "import pytest\n"
        "from writer import Writer\n\n"
        "@pytest.mark.nsc_writer_proof('writer:exact')\n"
        "def test_exact():\n"
        "    assert Writer.__name__ == 'Writer'\n",
        encoding="utf-8",
    )
    check = _check()
    registry = {
        "writer:exact": WriterEvidence(
            gates=(check.id,),
            witness="writer.py::Writer",
            proof="proof.py::test_exact",
        )
    }
    outcomes = [JUnitOutcome(check.target, "passed"), *proof_outcomes]

    result = evaluate_checks([check], outcomes, repo_root=tmp_path, writer_registry=registry)[0]
    assert result.verdict is CheckVerdict.NOT_EVALUABLE
    assert result.reason == reason


# --------------------------------------------------------------- v3 per-pair


def _pair_repo(
    tmp_path: Path,
    *,
    token: str = "glue:historical-fold",
    landed: bool = False,
    proof_marker: str | None = None,
) -> tuple[Path, Path]:
    """A scratch repo whose glue token gates two checks, one optionally landed.

    The second check is what makes this a PER-PAIR fixture: every assertion
    about the landed one must leave the other exactly as masked as it was.
    """
    repo = tmp_path / "repo"
    config = repo / "configs/northstar-cert"
    config.mkdir(parents=True)

    def _check_entry(check_id: str, target: str) -> dict[str, object]:
        return {
            "id": check_id,
            "capability": "C-99",
            "binding": {"type": "pytest", "target": target},
            "tier": "t1",
            "gate": True,
            "requires": [token],
            "scope": "scenario",
            "provenance": ["fixture"],
        }

    manifest = config / "manifest.yaml"
    manifest.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "checks": [
                    _check_entry("NSC-C99-LANDED", "tests/example.py::test_landed_carrier"),
                    _check_entry("NSC-C99-MASKED", "tests/example.py::test_masked_carrier"),
                ],
            }
        ),
        encoding="utf-8",
    )
    (repo / "feature.py").write_text("class FoldLeg:\n    enabled = True\n", encoding="utf-8")
    # Real production consumer so FoldLeg is not a stub the realness guard refuses
    # (committed via ``add -A`` below). The witness must be exercised by shipped
    # code, not only by its own proof.
    (repo / "omniagentos").mkdir(exist_ok=True)
    (repo / "omniagentos" / "consumer.py").write_text(
        "from feature import FoldLeg\n\n\ndef use() -> bool:\n    return FoldLeg.enabled\n",
        encoding="utf-8",
    )
    marker = proof_marker if proof_marker is not None else f"{token}@NSC-C99-LANDED"
    (repo / "proof.py").write_text(
        "import pytest\n"
        "from feature import FoldLeg\n\n"
        f"@pytest.mark.nsc_writer_proof({marker!r})\n"
        "def test_fold_leg():\n"
        "    assert FoldLeg.enabled is True\n",
        encoding="utf-8",
    )
    evidence = (
        {"witness": "feature.py::FoldLeg", "proof": "proof.py::test_fold_leg"}
        if landed
        else {"witness": None, "proof": None}
    )
    registry = config / "writers.yaml"
    registry.write_text(
        yaml.safe_dump(
            {
                "version": 3,
                "tokens": {
                    token: {
                        "gates": {
                            "NSC-C99-LANDED": evidence,
                            "NSC-C99-MASKED": {"witness": None, "proof": None},
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    _git(repo, "init", "-b", "main")
    _git(repo, "add", "-A")
    _git(
        repo,
        "-c",
        "user.name=NorthStar Test",
        "-c",
        "user.email=northstar@example.invalid",
        "commit",
        "-m",
        "seed per-pair history",
    )
    return manifest, registry


def _pair_checks() -> list[ManifestCheck]:
    return [
        ManifestCheck(
            id=check_id,
            capability="C-99",
            binding_type="pytest",
            target=target,
            tier="t1",
            gate=True,
            requires=("glue:historical-fold",),
            scope="scenario",
            provenance=("fixture",),
        )
        for check_id, target in (
            ("NSC-C99-LANDED", "tests/example.py::test_landed_carrier"),
            ("NSC-C99-MASKED", "tests/example.py::test_masked_carrier"),
        )
    ]


def _pair_outcomes(*, proof_status: str = "passed") -> list[JUnitOutcome]:
    return [
        JUnitOutcome("tests/example.py::test_landed_carrier", "passed"),
        JUnitOutcome("tests/example.py::test_masked_carrier", "passed"),
        JUnitOutcome("proof.py::test_fold_leg", proof_status),
    ]


def _verdicts(manifest: Path, registry_path: Path, outcomes: list[JUnitOutcome]) -> dict[str, str]:
    repo = manifest.parents[2]
    registry = recorder._load_writer_registry(registry_path, repo_root=repo)
    results = evaluate_checks(
        _pair_checks(),
        outcomes,
        repo_root=repo,
        writer_registry=registry,
    )
    return {result.check.id: result.verdict.value for result in results}


def test_the_real_registry_is_v3_with_per_check_pairs_for_every_non_writer_token() -> None:
    document = yaml.safe_load(WRITERS.read_text(encoding="utf-8"))
    assert document["version"] >= recorder.PER_CHECK_REGISTRY_VERSION
    for token, entry in document["tokens"].items():
        if token.startswith(recorder.WRITER_REQUIREMENT_PREFIX):
            assert isinstance(entry["gates"], list), token
        else:
            assert isinstance(entry["gates"], dict), token
            assert set(entry) == {"gates"}, token


def test_null_pairs_mean_exactly_what_the_bare_list_meant(tmp_path: Path) -> None:
    """The migration is behaviour-preserving: a converted, unlanded pair masks
    its check with the same reason the list form produced."""
    manifest, registry_path = _pair_repo(tmp_path, landed=False)
    verdicts = _verdicts(manifest, registry_path, _pair_outcomes())
    assert verdicts == {"NSC-C99-LANDED": "NOT_EVALUABLE", "NSC-C99-MASKED": "NOT_EVALUABLE"}


def test_a_landed_pair_unmasks_only_its_own_check(tmp_path: Path) -> None:
    manifest, registry_path = _pair_repo(tmp_path, landed=True)
    verdicts = _verdicts(manifest, registry_path, _pair_outcomes())
    assert verdicts["NSC-C99-LANDED"] == "PASS"
    assert verdicts["NSC-C99-MASKED"] == "NOT_EVALUABLE", (
        "one landed leg must not discharge the other checks the token masks"
    )


def test_a_pair_proof_that_stops_passing_reverts_to_unsatisfied(tmp_path: Path) -> None:
    """No ratchet: evidence is re-established every run, never remembered."""
    manifest, registry_path = _pair_repo(tmp_path, landed=True)
    failing = _verdicts(manifest, registry_path, _pair_outcomes(proof_status="failure"))
    assert failing["NSC-C99-LANDED"] == "NOT_EVALUABLE"
    missing = _verdicts(
        manifest,
        registry_path,
        [outcome for outcome in _pair_outcomes() if "proof.py" not in outcome.nodeid],
    )
    assert missing["NSC-C99-LANDED"] == "NOT_EVALUABLE"


def test_a_pair_proof_that_vanishes_from_the_tree_masks_only_its_own_check(
    tmp_path: Path,
) -> None:
    """Evidence defects are PER PAIR: a vanished proof unsatisfies its own check
    with a named reason and never takes the file down with it."""
    manifest, registry_path = _pair_repo(tmp_path, landed=True)
    (manifest.parents[2] / "proof.py").unlink()

    registry = recorder._load_writer_registry(registry_path, repo_root=manifest.parents[2])

    pair = registry["glue:historical-fold"].pair_for("NSC-C99-LANDED")
    assert pair.defect and "does not self-declare" in pair.defect
    assert pair.declared and not pair.landed
    verdicts = _verdicts(manifest, registry_path, _pair_outcomes())
    assert verdicts == {"NSC-C99-LANDED": "NOT_EVALUABLE", "NSC-C99-MASKED": "NOT_EVALUABLE"}


def test_a_pair_proof_must_declare_the_pair_not_the_bare_token(tmp_path: Path) -> None:
    """A proof declaring only `glue:...` would read as evidence for every check
    that token masks -- a hundred of them, for one landed leg."""
    manifest, registry_path = _pair_repo(tmp_path, landed=True, proof_marker="glue:historical-fold")

    registry = recorder._load_writer_registry(registry_path, repo_root=manifest.parents[2])

    pair = registry["glue:historical-fold"].pair_for("NSC-C99-LANDED")
    assert pair.defect and "does not self-declare" in pair.defect
    verdicts = _verdicts(manifest, registry_path, _pair_outcomes())
    assert verdicts["NSC-C99-LANDED"] == "NOT_EVALUABLE"


def test_a_pair_may_not_be_half_landed(tmp_path: Path) -> None:
    manifest, registry_path = _pair_repo(tmp_path, landed=True)
    document = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
    document["tokens"]["glue:historical-fold"]["gates"]["NSC-C99-LANDED"]["proof"] = None
    registry_path.write_text(yaml.safe_dump(document), encoding="utf-8")

    registry = recorder._load_writer_registry(registry_path, repo_root=manifest.parents[2])

    pair = registry["glue:historical-fold"].pair_for("NSC-C99-LANDED")
    assert pair.defect == "witness and proof must be declared together"
    assert not pair.landed
    assert _verdicts(manifest, registry_path, _pair_outcomes())["NSC-C99-LANDED"] == (
        "NOT_EVALUABLE"
    )


def test_per_check_gates_require_the_v3_version_stamp(tmp_path: Path) -> None:
    manifest, registry_path = _pair_repo(tmp_path, landed=False)
    document = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
    document["version"] = 2
    registry_path.write_text(yaml.safe_dump(document), encoding="utf-8")
    with pytest.raises(recorder.CertificationError, match="require version 3"):
        recorder._load_writer_registry(registry_path, repo_root=manifest.parents[2])


def test_deleting_a_pair_from_the_map_fails_closed_against_git_history(tmp_path: Path) -> None:
    """The sticky invariant, per pair: pairs are never removed, only satisfied."""
    manifest, registry_path = _pair_repo(tmp_path, landed=False)
    document = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
    del document["tokens"]["glue:historical-fold"]["gates"]["NSC-C99-MASKED"]
    registry_path.write_text(yaml.safe_dump(document), encoding="utf-8")
    manifest_document = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    manifest_document["checks"] = manifest_document["checks"][:1]
    manifest.write_text(yaml.safe_dump(manifest_document), encoding="utf-8")

    with pytest.raises(recorder.CertificationError, match="deleted historical token/check entries"):
        recorder._writer_registry_for_manifest(manifest, repo_root=manifest.parents[2])


def test_history_reads_both_registry_shapes(tmp_path: Path) -> None:
    """A v2 list and a v3 map are both historical fact; a reader that knew only
    one would read every pair in the other as deleted."""
    listed = recorder._mask_obligations_in_document(
        yaml.safe_dump({"version": 2, "tokens": {"glue:x": {"gates": ["NSC-A", "NSC-B"]}}}),
        source="v2",
    )
    mapped = recorder._mask_obligations_in_document(
        yaml.safe_dump(
            {
                "version": 3,
                "tokens": {
                    "glue:x": {
                        "gates": {
                            "NSC-A": {"witness": None, "proof": None},
                            "NSC-B": {"witness": None, "proof": None},
                        }
                    }
                },
            }
        ),
        source="v3",
    )
    assert listed == mapped == {"glue:x": {"NSC-A", "NSC-B"}}


def test_runnable_targets_include_a_landed_pairs_proof(tmp_path: Path) -> None:
    manifest, registry_path = _pair_repo(tmp_path, landed=True)
    repo = manifest.parents[2]
    registry = recorder._load_writer_registry(registry_path, repo_root=repo)
    targets = runnable_targets(manifest, "t1", repo_root=repo, writer_registry=registry)
    assert "tests/example.py::test_landed_carrier" in targets
    assert "proof.py::test_fold_leg" in targets
    assert "tests/example.py::test_masked_carrier" not in targets


# ------------------------------------------------- evidence must be REAL code


# The realness guard now lives IN the grader. These tests exercise the SAME
# production-code function (``recorder._production_references(witness, repo_root)``)
# rather than a test-local copy, so the meta-test can never drift from what the
# grader actually enforces. ``repo_root`` replaces the old implicit cwd.
_production_references = recorder._production_references


def test_every_landed_witness_is_real_production_code() -> None:
    """A witness with no production presence is a metric-shaped stub.

    The gaming class this whole design exists to refuse: build a helper nobody
    calls, write a proof that feeds it its own fixture, and the check goes
    green while the capability does not exist. A landed witness has to be code
    the system actually runs.
    """
    registry = recorder._load_writer_registry(WRITERS, repo_root=Path.cwd())
    landed = [
        (token, pair) for token, entry in registry.items() for pair in entry.pairs if pair.landed
    ]
    assert landed, "the lane's pilot pair must be landed for this guard to mean anything"
    for token, pair in landed:
        references = _production_references(pair.witness, Path.cwd())
        # At least one USE outside the symbol's own definition. A recursive stub
        # calling itself, or a same-named helper in a module that never imported
        # the real one, both count as zero.
        assert references, f"{token}@{pair.check_id}: {pair.witness} has no production consumer"


def test_a_self_comparing_proof_exercises_nothing(tmp_path: Path) -> None:
    """`assert witness == witness` mentions the symbol twice and executes none
    of it; it is true for any object Python can bind."""
    (tmp_path / "feature.py").write_text(
        "def real_capability():\n    raise AssertionError('the proof must call this')\n",
        encoding="utf-8",
    )
    (tmp_path / "proof.py").write_text(
        "import pytest\n"
        "from feature import real_capability\n\n"
        "@pytest.mark.nsc_writer_proof('glue:fold@NSC-X')\n"
        "def test_vacuous():\n"
        "    assert real_capability == real_capability\n\n"
        "@pytest.mark.nsc_writer_proof('glue:fold@NSC-Y')\n"
        "def test_identity():\n"
        "    assert real_capability is real_capability\n",
        encoding="utf-8",
    )
    for node in ("proof.py::test_vacuous", "proof.py::test_identity"):
        assert not recorder._proof_exercises_witness(node, "feature.py::real_capability", tmp_path)


def test_calling_or_reading_the_witness_does_exercise_it(tmp_path: Path) -> None:
    """The green side: a call, and a non-call attribute read feeding an assert."""
    (tmp_path / "feature.py").write_text(
        "class Capability:\n"
        "    enabled = True\n\n"
        "    @staticmethod\n"
        "    def measure(value):\n"
        "        return value * 2\n",
        encoding="utf-8",
    )
    (tmp_path / "proof.py").write_text(
        "import pytest\n"
        "from feature import Capability\n\n"
        "@pytest.mark.nsc_writer_proof('glue:fold@NSC-CALL')\n"
        "def test_called():\n"
        "    assert Capability.measure(2) == 4\n\n"
        "@pytest.mark.nsc_writer_proof('glue:fold@NSC-ATTR')\n"
        "def test_attribute_read():\n"
        "    assert Capability.enabled is True\n\n"
        "@pytest.mark.nsc_writer_proof('glue:fold@NSC-SELF')\n"
        "def test_self_compare_plus_nothing_else():\n"
        "    assert Capability == Capability\n",
        encoding="utf-8",
    )
    exercises = recorder._proof_exercises_witness
    assert exercises("proof.py::test_called", "feature.py::Capability", tmp_path)
    assert exercises("proof.py::test_attribute_read", "feature.py::Capability", tmp_path)
    assert not exercises(
        "proof.py::test_self_compare_plus_nothing_else", "feature.py::Capability", tmp_path
    )


def test_the_realness_guard_is_not_laundered_by_self_reference(
    tmp_path: Path,
) -> None:
    """A witness that only calls itself has no consumer.

    Self-recursion is the cheapest fake caller: the symbol appears twice in the
    tree (its def, and the call inside it) and a naive count reads that as
    "defined and used". So does a same-named helper in a module that never
    imported the real one.
    """
    for directory in ("omniagentos", "scripts", "pipeline"):
        (tmp_path / directory).mkdir()
    (tmp_path / "omniagentos" / "stub.py").write_text(
        "def synthetic_witness():\n    return synthetic_witness()\n", encoding="utf-8"
    )
    (tmp_path / "omniagentos" / "impostor.py").write_text(
        # same NAME, never imported from the defining module
        "def synthetic_witness():\n    return 1\n\n\ndef caller():\n"
        "    return synthetic_witness()\n",
        encoding="utf-8",
    )
    (tmp_path / "omniagentos" / "real.py").write_text(
        "class RealWitness:\n    value = 1\n", encoding="utf-8"
    )
    (tmp_path / "omniagentos" / "consumer.py").write_text(
        "from omniagentos.real import RealWitness\n\n\ndef use():\n    return RealWitness.value\n",
        encoding="utf-8",
    )

    assert _production_references("omniagentos/stub.py::synthetic_witness", tmp_path) == []
    assert _production_references("synthetic_witness", tmp_path) == [], "bare-name mode too"
    assert _production_references("omniagentos/real.py::RealWitness", tmp_path), (
        "a genuine external consumer must still count"
    )


def test_a_statically_dead_call_exercises_nothing(tmp_path: Path) -> None:
    """`if False: witness()` is text, not execution.

    Full reachability analysis is out of scope (a proof can always hide a call
    behind a runtime condition) — this closes the constant-false shapes, and the
    residual is named in the writers.yaml header: each landed pair is Class-A
    reviewed, and that review is the backstop for what static analysis cannot
    decide.
    """
    (tmp_path / "feature.py").write_text(
        "class Capability:\n    enabled = True\n\n    @staticmethod\n"
        "    def measure(value):\n        return value * 2\n",
        encoding="utf-8",
    )
    (tmp_path / "proof.py").write_text(
        "import pytest\n"
        "from feature import Capability\n\n"
        "@pytest.mark.nsc_writer_proof('glue:fold@NSC-DEAD')\n"
        "def test_dead_branch():\n"
        "    if False:\n"
        "        assert Capability.measure(2) == 4\n"
        "    assert True\n\n"
        "@pytest.mark.nsc_writer_proof('glue:fold@NSC-DEAD0')\n"
        "def test_dead_zero_branch():\n"
        "    if 0:\n"
        "        assert Capability.enabled\n"
        "    assert True\n\n"
        "@pytest.mark.nsc_writer_proof('glue:fold@NSC-DEADELSE')\n"
        "def test_dead_else_branch():\n"
        "    if True:\n"
        "        assert True\n"
        "    else:\n"
        "        assert Capability.measure(2) == 4\n\n"
        "@pytest.mark.nsc_writer_proof('glue:fold@NSC-LIVE')\n"
        "def test_live_branch():\n"
        "    if True:\n"
        "        assert Capability.measure(2) == 4\n",
        encoding="utf-8",
    )
    exercises = recorder._proof_exercises_witness
    for dead in ("test_dead_branch", "test_dead_zero_branch", "test_dead_else_branch"):
        assert not exercises(f"proof.py::{dead}", "feature.py::Capability", tmp_path), dead
    assert exercises("proof.py::test_live_branch", "feature.py::Capability", tmp_path)


def test_attribute_tautology_over_the_witness_exercises_nothing(tmp_path: Path) -> None:
    """R6: ``Witness.attr == Witness.attr`` is a tautology, not a substantive read.

    A bare-reference check alone recognises only ``Witness``/``module.Witness`` as
    a bare ref, so a name-import PLUS an attribute read on both sides
    (``Capability.enabled == Capability.enabled``) slipped through as "state read
    into the assertion". It reads no state the comparison did not put on both
    sides. A comparison of two DIFFERENT witness attributes still counts.
    """
    (tmp_path / "feature.py").write_text(
        "class Capability:\n    enabled = True\n    other = False\n", encoding="utf-8"
    )
    (tmp_path / "proof.py").write_text(
        "import pytest\n"
        "from feature import Capability\n\n"
        "@pytest.mark.nsc_writer_proof('glue:fold@NSC-TAUT')\n"
        "def test_attr_tautology():\n"
        "    assert Capability.enabled == Capability.enabled\n\n"
        "@pytest.mark.nsc_writer_proof('glue:fold@NSC-DEEPTAUT')\n"
        "def test_deep_attr_tautology():\n"
        "    assert Capability.enabled is Capability.enabled\n\n"
        "@pytest.mark.nsc_writer_proof('glue:fold@NSC-DISTINCT')\n"
        "def test_distinct_attrs():\n"
        "    assert Capability.enabled != Capability.other\n",
        encoding="utf-8",
    )
    exercises = recorder._proof_exercises_witness
    assert not exercises("proof.py::test_attr_tautology", "feature.py::Capability", tmp_path)
    assert not exercises("proof.py::test_deep_attr_tautology", "feature.py::Capability", tmp_path)
    # Two DIFFERENT attributes is a real read of witness state, not a tautology.
    assert exercises("proof.py::test_distinct_attrs", "feature.py::Capability", tmp_path)


def test_a_witness_shadowed_by_a_fixture_parameter_is_not_credited(tmp_path: Path) -> None:
    """R6 shadow: a fixture parameter named like the witness is NOT the witness.

    ``def test(Capability): assert Capability.enabled`` reads the FIXTURE, not the
    imported production symbol, so it exercises nothing — while the same proof
    with the real import (no shadow) does.
    """
    (tmp_path / "feature.py").write_text(
        "class Capability:\n    enabled = True\n", encoding="utf-8"
    )
    (tmp_path / "proof.py").write_text(
        "import pytest\n"
        "from feature import Capability\n\n"
        "@pytest.mark.nsc_writer_proof('glue:fold@NSC-SHADOW')\n"
        "def test_shadowed(Capability):\n"
        "    assert Capability.enabled is True\n\n"
        "@pytest.mark.nsc_writer_proof('glue:fold@NSC-NOSHADOW')\n"
        "def test_unshadowed():\n"
        "    assert Capability.enabled is True\n",
        encoding="utf-8",
    )
    exercises = recorder._proof_exercises_witness
    assert not exercises("proof.py::test_shadowed", "feature.py::Capability", tmp_path)
    assert exercises("proof.py::test_unshadowed", "feature.py::Capability", tmp_path)


def test_statically_dead_non_bool_constants_and_ternaries_exercise_nothing(tmp_path: Path) -> None:
    """R8: the dead-branch prune covers every statically-falsy constant and IfExp.

    The old prune decided only ``bool``/``int`` conditions, so ``if "":``,
    ``if None:``, ``if []:``, ``if 0.0:`` and the ternary ``witness() if 0 else
    X`` were credited even though the arm holding the witness never runs. A witness
    reachable ONLY through statically-dead code must count as nothing; a live
    ternary arm still counts.
    """
    (tmp_path / "feature.py").write_text(
        "class Capability:\n    enabled = True\n\n    @staticmethod\n"
        "    def measure(value):\n        return value * 2\n",
        encoding="utf-8",
    )
    (tmp_path / "proof.py").write_text(
        "import pytest\n"
        "from feature import Capability\n\n"
        "@pytest.mark.nsc_writer_proof('glue:fold@NSC-EMPTYSTR')\n"
        "def test_empty_str():\n"
        '    if "":\n'
        "        assert Capability.measure(2) == 4\n"
        "    assert True\n\n"
        "@pytest.mark.nsc_writer_proof('glue:fold@NSC-NONE')\n"
        "def test_none():\n"
        "    if None:\n"
        "        assert Capability.enabled\n"
        "    assert True\n\n"
        "@pytest.mark.nsc_writer_proof('glue:fold@NSC-EMPTYLIST')\n"
        "def test_empty_list():\n"
        "    if []:\n"
        "        assert Capability.enabled\n"
        "    assert True\n\n"
        "@pytest.mark.nsc_writer_proof('glue:fold@NSC-ZEROFLOAT')\n"
        "def test_zero_float():\n"
        "    if 0.0:\n"
        "        assert Capability.enabled\n"
        "    assert True\n\n"
        "@pytest.mark.nsc_writer_proof('glue:fold@NSC-IFEXP')\n"
        "def test_dead_ternary():\n"
        "    assert (Capability.measure(2) if 0 else True)\n\n"
        "@pytest.mark.nsc_writer_proof('glue:fold@NSC-IFEXPLIVE')\n"
        "def test_live_ternary():\n"
        "    assert (Capability.measure(2) if 1 else True) == 4\n",
        encoding="utf-8",
    )
    exercises = recorder._proof_exercises_witness
    dead_cases = (
        "test_empty_str",
        "test_none",
        "test_empty_list",
        "test_zero_float",
        "test_dead_ternary",
    )
    for dead in dead_cases:
        assert not exercises(f"proof.py::{dead}", "feature.py::Capability", tmp_path), dead
    assert exercises("proof.py::test_live_ternary", "feature.py::Capability", tmp_path)


def test_a_stub_witness_with_no_production_consumer_is_refused(tmp_path: Path) -> None:
    """FIX (a) in-band: a witness the proof exercises but NOTHING in shipped code
    consumes is a metric-shaped stub — the grader records it as a pair defect and
    never PASS. A genuine witness with a real production consumer still lands."""
    for directory in ("omniagentos", "scripts", "pipeline"):
        (tmp_path / directory).mkdir()
    (tmp_path / "omniagentos" / "stub.py").write_text(
        "class SyntheticWriter:\n    value = 1\n", encoding="utf-8"
    )
    (tmp_path / "proof.py").write_text(
        "import pytest\n"
        "from omniagentos.stub import SyntheticWriter\n\n"
        "@pytest.mark.nsc_writer_proof('glue:x@NSC-C99-STUB')\n"
        "def test_stub():\n"
        "    assert SyntheticWriter.value == 1\n",
        encoding="utf-8",
    )
    registry_path = tmp_path / "writers.yaml"
    registry_path.write_text(
        yaml.safe_dump(
            {
                "version": 3,
                "tokens": {
                    "glue:x": {
                        "gates": {
                            "NSC-C99-STUB": {
                                "witness": "omniagentos/stub.py::SyntheticWriter",
                                "proof": "proof.py::test_stub",
                            }
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    registry = recorder._load_writer_registry(registry_path, repo_root=tmp_path)
    pair = registry["glue:x"].pair_for("NSC-C99-STUB")
    assert pair.defect and "no production reference" in pair.defect
    assert pair.declared and not pair.landed

    # Add a real production consumer: now the same witness is genuinely landed.
    (tmp_path / "omniagentos" / "consumer.py").write_text(
        "from omniagentos.stub import SyntheticWriter\n\n\n"
        "def use() -> int:\n    return SyntheticWriter.value\n",
        encoding="utf-8",
    )
    recorder._PRODUCTION_TREE_CACHE.clear()  # the tree changed within this test
    registry = recorder._load_writer_registry(registry_path, repo_root=tmp_path)
    landed_pair = registry["glue:x"].pair_for("NSC-C99-STUB")
    assert landed_pair.defect is None and landed_pair.landed


def test_a_writer_token_stub_witness_fails_closed_at_registry_load(tmp_path: Path) -> None:
    """The realness bar applies to token-wide (``writer:``) evidence too."""
    (tmp_path / "omniagentos").mkdir()
    (tmp_path / "omniagentos" / "stub.py").write_text(
        "class LoneWriter:\n    enabled = True\n", encoding="utf-8"
    )
    (tmp_path / "proof.py").write_text(
        "import pytest\n"
        "from omniagentos.stub import LoneWriter\n\n"
        "@pytest.mark.nsc_writer_proof('writer:lone')\n"
        "def test_lone():\n"
        "    assert LoneWriter.enabled is True\n",
        encoding="utf-8",
    )
    registry_path = tmp_path / "writers.yaml"
    registry_path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "tokens": {
                    "writer:lone": {
                        "gates": ["NSC-C99-PROOF"],
                        "witness": "omniagentos/stub.py::LoneWriter",
                        "proof": "proof.py::test_lone",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    recorder._PRODUCTION_TREE_CACHE.clear()
    with pytest.raises(recorder.CertificationError, match="has no production reference"):
        recorder._load_writer_registry(registry_path, repo_root=tmp_path)


def test_all_statically_dead_control_flow_shapes_exercise_nothing(tmp_path: Path) -> None:
    """R8 completeness: the dead-code prune is UNIFORM over the whole
    constant-decidable control-flow class, not an enumerated few.

    ``while False``, ``for _ in <empty>``, ``False and W()`` / ``True or W()`` and
    comprehensions over an empty source (or a constant-false filter) each hide a
    witness call in a provably-unreached subtree — the sibling shapes of the
    if/IfExp holes. Every one must count as nothing, while the data-dependent LIVE
    counterpart (a runtime loop or condition, a non-empty source) still counts:
    the prune drops the provably-dead, never a real live path.
    """
    (tmp_path / "feature.py").write_text(
        "class Capability:\n    enabled = True\n\n    @staticmethod\n"
        "    def measure(value):\n        return value * 2\n",
        encoding="utf-8",
    )
    dead = {
        "while_false": "    while False:\n        Capability()\n    assert True\n",
        "for_empty_list": "    for _ in []:\n        Capability()\n    assert True\n",
        "for_empty_tuple": "    for _ in ():\n        Capability()\n    assert True\n",
        "for_empty_str": "    for _ in '':\n        Capability()\n    assert True\n",
        "and_short_circuit": "    assert False and Capability()\n",
        "or_short_circuit": "    assert True or Capability()\n",
        "listcomp_empty": "    assert [Capability() for _ in []] == []\n",
        "dictcomp_empty": "    assert {i: Capability() for i in []} == {}\n",
        "genexp_empty": "    assert list(Capability() for _ in []) == []\n",
        "comp_false_filter": "    assert [Capability() for x in range(3) if False] == []\n",
        "if_false_boolop": "    if False and object():\n        Capability()\n    assert True\n",
    }
    live = {
        "for_runtime": "    for x in [1, 2]:\n        assert Capability.measure(x) == x * 2\n",
        "if_runtime": "    import os\n    if os.getpid():\n        assert Capability.enabled\n",
        "true_and": "    assert True and Capability.enabled\n",
        "listcomp_nonempty": "    assert [Capability.measure(x) for x in [2]] == [4]\n",
    }
    lines = ["import pytest", "from feature import Capability", ""]
    for name, body in {**dead, **live}.items():
        lines.append(f"@pytest.mark.nsc_writer_proof('glue:fold@NSC-{name}')")
        lines.append(f"def test_{name}():")
        lines.append(body.rstrip("\n"))
        lines.append("")
    (tmp_path / "proof.py").write_text("\n".join(lines), encoding="utf-8")

    exercises = recorder._proof_exercises_witness
    for name in dead:
        assert not exercises(f"proof.py::test_{name}", "feature.py::Capability", tmp_path), name
    for name in live:
        assert exercises(f"proof.py::test_{name}", "feature.py::Capability", tmp_path), name


def test_chained_comparison_and_assert_message_reachability(tmp_path: Path) -> None:
    """The last two short-circuit shapes: a chained comparison and an assert message.

    ``0 > 1 > witness()`` is ``(0 > 1) and (1 > witness())`` — ``0 > 1`` is false so
    ``witness()`` in the tail never runs; ``assert True, witness()`` evaluates its
    message only when the assertion FAILS, so a statically-true test makes the
    message dead. Both must count as nothing, while a reachable chain tail
    (``1 < 2 < witness()``) and a message on a failing/undecidable assert
    (``assert False, witness()`` — the message IS built) stay credited.
    """
    (tmp_path / "feature.py").write_text(
        "class Capability:\n    enabled = True\n\n    @staticmethod\n"
        "    def measure(value):\n        return value * 2\n",
        encoding="utf-8",
    )
    dead = {
        "chain_false_head": "    if 0 > 1 > Capability():\n        pass\n",
        # The if-BODY is dead because `0 > 1` settles the whole chain False even
        # though the tail is a RUNTIME call — the lazy fold must decide False (not
        # bail to undecidable and credit the body).
        "chain_false_body": "    if 0 > 1 > len([1]):\n        Capability()\n    assert True\n",
        "chain_false_assert": "    assert 0 > 1 > Capability.measure(0)\n",
        "chain_false_middle": "    if 1 < 5 < 2 < Capability():\n        pass\n",
        "assert_true_message": "    assert True, Capability.measure(0)\n    assert True\n",
        "assert_const_message": "    assert 1 == 1, Capability.measure(0)\n    assert True\n",
    }
    live = {
        "chain_reachable_tail": "    assert 1 < 2 < Capability.measure(2)\n",
        "chain_runtime_operand": "    x = 5\n    assert x < Capability.measure(2)\n",
        "assert_false_message": (
            "    try:\n        assert False, Capability.measure(2)\n"
            "    except AssertionError:\n        pass\n"
        ),
        "assert_runtime_message": (
            "    import os\n    assert os.getpid(), Capability.measure(2)\n"
        ),
    }
    lines = ["import pytest", "from feature import Capability", ""]
    for name, body in {**dead, **live}.items():
        lines.append(f"@pytest.mark.nsc_writer_proof('glue:fold@NSC-{name}')")
        lines.append(f"def test_{name}():")
        lines.append(body.rstrip("\n"))
        lines.append("")
    (tmp_path / "proof.py").write_text("\n".join(lines), encoding="utf-8")

    exercises = recorder._proof_exercises_witness
    for name in dead:
        assert not exercises(f"proof.py::test_{name}", "feature.py::Capability", tmp_path), name
    for name in live:
        assert exercises(f"proof.py::test_{name}", "feature.py::Capability", tmp_path), name


def test_constant_folding_closes_the_whole_operator_class(tmp_path: Path) -> None:
    """R8 general invariant: a condition decides IFF it is a PURE CONSTANT, and
    stays LIVE the instant any runtime reference appears — no operator enumeration.

    ``not True`` / ``not not False`` / ``1 == 0`` / ``2 < 1`` / ``1 - 1`` /
    ``~(-1)`` / ``x and False`` (always falsy) / ``5 in [1, 2]`` all fold to a dead
    branch; while a runtime name/call/subscript anywhere — ``if runtime`` /
    ``if fn()`` / ``if [witness()]`` (a Call in the test) / ``not (False and
    runtime)`` (folds to True → the body stays live) — keeps the witness credited.
    And a witness in the EVALUATED operand of a short circuit still runs:
    ``if witness() and False:`` credits the call even though the body is dead.
    """
    (tmp_path / "feature.py").write_text(
        "class Capability:\n    enabled = True\n\n    @staticmethod\n"
        "    def measure(value):\n        return value * 2\n",
        encoding="utf-8",
    )
    dead = {
        "not_true": "    if not True:\n        Capability()\n    assert True\n",
        "not_not_false": "    if not not False:\n        Capability()\n    assert True\n",
        "comp_not_true_filter": "    assert [Capability() for _ in [1] if not True] == []\n",
        "compare_eq": "    if 1 == 0:\n        Capability()\n    assert True\n",
        "compare_lt": "    if 2 < 1:\n        Capability()\n    assert True\n",
        "arithmetic": "    if 1 - 1:\n        Capability()\n    assert True\n",
        "invert": "    if ~(-1):\n        Capability()\n    assert True\n",
        "membership": "    if 5 in [1, 2]:\n        Capability()\n    assert True\n",
        "name_and_false": "    if bool([]) and False:\n        Capability()\n    assert True\n",
    }
    live = {
        "runtime_name": "    import os\n    if os.getpid():\n        Capability()\n",
        "runtime_call": "    if len([1, 2]):\n        Capability()\n",
        "witness_in_test": "    if [Capability()]:\n        assert True\n",
        "not_false_and_runtime": (
            "    import os\n    if not (False and os.getpid()):\n        Capability()\n"
        ),
        "arithmetic_live": "    if 1 + 1:\n        Capability()\n",
        "membership_live": "    if 1 in [1, 2]:\n        Capability()\n",
        "witness_short_circuit_operand": (
            "    if Capability.measure(2) == 4 and False:\n        assert True\n    assert True\n"
        ),
    }
    lines = ["import pytest", "from feature import Capability", ""]
    for name, body in {**dead, **live}.items():
        lines.append(f"@pytest.mark.nsc_writer_proof('glue:fold@NSC-{name}')")
        lines.append(f"def test_{name}():")
        lines.append(body.rstrip("\n"))
        lines.append("")
    (tmp_path / "proof.py").write_text("\n".join(lines), encoding="utf-8")

    exercises = recorder._proof_exercises_witness
    for name in dead:
        assert not exercises(f"proof.py::test_{name}", "feature.py::Capability", tmp_path), name
    for name in live:
        assert exercises(f"proof.py::test_{name}", "feature.py::Capability", tmp_path), name
