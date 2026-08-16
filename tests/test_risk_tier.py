"""Tiered verification — `bridge.risk_tier` classifier + its `gate_loop` wiring.

Two layers under test:

  * the pure classifier (`risk_tier.classify`): fail-closed HIGH/LOW from the
    REAL changed paths, with the two narrow LOW carve-outs (additive-only schema
    field, single small script) gated behind an explicit attestation, and the
    `OMNIAGENTOS_TIERED_VERIFY` kill switch forcing HIGH when unset;
  * the lander wiring (`GateLoop.load_candidates`): a LOW candidate lands on a
    signed, receipt-verified merge-gate PASS on its OWN tip (recording a
    synthetic `mechanical-gate` verdict); the gate stays the floor so a LOW
    candidate with no verified PASS receipt does NOT land; a HIGH candidate is
    still refused without a genuine cross-lineage verdict; and with the switch
    off a would-be-LOW candidate again requires that verdict.

Hermetic: every wiring test builds a throwaway git repo in tmp_path; no network,
no real merge-gate, no twin.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PIPELINE = REPO_ROOT / "pipeline"
if str(PIPELINE) not in sys.path:
    sys.path.insert(0, str(PIPELINE))

from bridge import risk_tier  # noqa: E402
from bridge.gate_loop import GateLoop  # noqa: E402

HIGH = risk_tier.HIGH
LOW = risk_tier.LOW


# --------------------------------------------------------------- pure classifier


def test_docs_and_tests_only_classify_low_when_enabled():
    assert risk_tier.classify(
        ["docs/readme.md", "tests/test_widget.py", "pipeline/tests/test_x.py"],
        enabled=True) == LOW


def test_kill_switch_forces_high_for_everything():
    # Default (switch off) — a diff that would be LOW is HIGH.
    assert risk_tier.classify(["docs/readme.md"], enabled=False) == HIGH
    # Read from the environment: only the exact string "1" enables tiering.
    assert risk_tier.classify(["docs/readme.md"], env={}) == HIGH
    assert risk_tier.classify(["docs/readme.md"],
                              env={"OMNIAGENTOS_TIERED_VERIFY": "0"}) == HIGH
    assert risk_tier.classify(["docs/readme.md"],
                              env={"OMNIAGENTOS_TIERED_VERIFY": "true"}) == HIGH
    assert risk_tier.classify(["docs/readme.md"],
                              env={"OMNIAGENTOS_TIERED_VERIFY": "1"}) == LOW


def test_empty_or_unreadable_diff_is_high():
    assert risk_tier.classify([], enabled=True) == HIGH
    assert risk_tier.classify(None, enabled=True) == HIGH
    assert risk_tier.classify(["   "], enabled=True) == HIGH


def test_ambiguous_unknown_path_is_high():
    # Not docs, not tests, not an attested carve-out => conservative HIGH.
    assert risk_tier.classify(["omniagentos/widgets/render.py"], enabled=True) == HIGH
    assert risk_tier.classify(["some/brand/new/thing.rs"], enabled=True) == HIGH
    # One unknown path poisons an otherwise-mechanical set.
    assert risk_tier.classify(
        ["docs/readme.md", "omniagentos/widgets/render.py"], enabled=True) == HIGH


@pytest.mark.parametrize("path", [
    "pipeline/bridge/integration.py",       # main-writer / lander core
    "pipeline/bridge/gate_loop.py",
    "pipeline/bridge/land_detect.py",       # land path
    "scripts/merge-gate.sh",                # the gate
    "gates/reachability.py",                # the gate / reachability
    "omniagentos/db/migrations/007_add.sql",  # db migration
    "omniagentos/policy/approvals.py",      # approvals
    "services/auth/session.py",             # auth (word)
    "services/billing/stripe_charge.py",    # money (word)
    "configs/security/keys.yaml",           # secrets/permissions
    "pipeline/prompts/system.txt",          # prompts
    "system-prompts/reviewer.md",           # prompts (a .md, but a prompt)
    "docs/PROMPT-reviewer.md",              # PROMPT-* basename
    "ARCHI.md",                             # architecture
    "ARCHI.json",
])
def test_high_surfaces_classify_high(path):
    assert risk_tier.classify([path], enabled=True) == HIGH


def test_schema_is_high_unless_additive_attestation_covers_it():
    schema = "schema/order.schema.json"
    assert risk_tier.classify([schema], enabled=True) == HIGH
    assert risk_tier.classify([schema], enabled=True,
                              attested_additive_schema=[schema]) == LOW
    # contracts/ and *.schema.json suffix are both schema surfaces.
    assert risk_tier.classify(["contracts/api.json"], enabled=True) == HIGH
    assert risk_tier.classify(["pipeline/schema/envelope.schema.json"], enabled=True,
                              attested_additive_schema=["pipeline/schema/envelope.schema.json"]
                              ) == LOW


def test_attestation_never_downgrades_a_hard_high_surface():
    # A migration is HARD-HIGH; attesting it as schema OR script cannot save it.
    mig = "omniagentos/db/migrations/007.sql"
    assert risk_tier.classify([mig], enabled=True,
                              attested_additive_schema=[mig],
                              attested_scripts=[mig]) == HIGH
    # gate_loop.py likewise.
    core = "pipeline/bridge/gate_loop.py"
    assert risk_tier.classify([core], enabled=True, attested_scripts=[core]) == HIGH


def test_single_small_attested_script_is_low_but_two_are_high():
    s1, s2 = "scripts/tidy_reports.py", "scripts/rotate_logs.sh"
    assert risk_tier.classify([s1], enabled=True, attested_scripts=[s1]) == LOW
    assert risk_tier.classify([s1], enabled=True) == HIGH  # unattested script => HIGH
    assert risk_tier.classify([s1, s2], enabled=True,
                              attested_scripts=[s1, s2]) == HIGH  # "a SINGLE small script"


# ------------------------------------ BLOCKER 1: superset of the old risky net
#
# Safety guarantee: a candidate the OLD policy (`review_policy`) would have
# required a cross-lineage verdict for must NEVER be attested-LOW here. So the
# hard-HIGH nets must be a superset of `review_policy`'s risky nets, EXCEPT the
# deliberate schema/contracts attestation carve-out (§2).
from bridge import review_policy  # noqa: E402


def test_hard_high_words_superset_of_review_policy_risky_words():
    # The set relationship, asserted mechanically so the two nets cannot drift.
    assert set(review_policy._RISKY_PATH_WORDS) <= set(risk_tier._HARD_HIGH_WORDS)


@pytest.mark.parametrize("word", sorted(review_policy._RISKY_PATH_WORDS))
def test_every_risky_word_forces_high_even_on_a_would_be_low_path(word):
    # A test file (tests/) and an attested script both classify LOW without a
    # risky word; carrying one of the old policy's risky words must flip them to
    # HIGH — this is exactly the `tests/test_gate_foo.py` / `scripts/policy_*`
    # regression the reopened word net caused.
    test_path = f"tests/test_{word}.py"
    assert risk_tier.classify([test_path], enabled=True) == HIGH, word
    script_path = f"scripts/{word}_check.py"
    assert risk_tier.classify([script_path], enabled=True,
                              attested_scripts=[script_path]) == HIGH, word


def test_hard_high_exact_superset_of_review_policy_risky_exact():
    assert set(review_policy._RISKY_EXACT) <= set(risk_tier._HARD_HIGH_EXACT)
    for path in review_policy._RISKY_EXACT:
        assert risk_tier.classify([path], enabled=True) == HIGH, path


def test_review_policy_prefixes_are_high_except_schema_carveout():
    # A `.md` under a risky prefix would be a bounded-mechanical doc (LOW) but for
    # the prefix; the schema/contracts prefixes are the intended attestation
    # carve-out (§2) and stay downgradable, everything else must be HIGH.
    carveout = set(risk_tier._RISKY_PREFIXES_SCHEMA_CARVEOUT)
    for pref in review_policy._RISKY_PREFIXES:
        if pref in carveout:
            continue
        p = f"{pref}notes.md"
        assert risk_tier.classify([p], enabled=True) == HIGH, pref
    # And the carve-out still works: an attested additive schema field is LOW.
    assert risk_tier.classify(["schema/order.schema.json"], enabled=True,
                              attested_additive_schema=["schema/order.schema.json"]) == LOW


# ---------------------------- BLOCKER 2: self-green-lighting test surfaces


def test_test_named_file_outside_tests_dir_is_not_auto_low():
    # A `test_*.py` basename ANYWHERE was auto-mechanical, so arbitrary code could
    # ride a test-naming convention into LOW. Now only files inside a `tests/`
    # DIRECTORY are bounded-mechanical; elsewhere the name earns no free pass.
    assert risk_tier.classify(["scripts/test_deploy.py"], enabled=True) == HIGH
    assert risk_tier.classify(["omniagentos/api/test_helpers.py"], enabled=True) == HIGH
    assert risk_tier.classify(["some/dir/thing_test.py"], enabled=True) == HIGH
    # A genuine tests/ file stays mechanical / LOW.
    assert risk_tier.classify(["tests/test_real.py"], enabled=True) == LOW
    assert risk_tier.classify(["pipeline/tests/unit/test_x.py"], enabled=True) == LOW


@pytest.mark.parametrize("path", [
    "tests/conftest.py", "conftest.py", "tests/unit/conftest.py",
    "pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml",
])
def test_test_harness_files_are_hard_high_even_inside_tests(path):
    # These DEFINE what "PASS" means (pytest hooks/fixtures, gate config); a
    # candidate that edits one controls its own gate verdict, so no attestation
    # and no `tests/` location may make them LOW.
    assert risk_tier.classify([path], enabled=True) == HIGH
    assert risk_tier.classify([path], enabled=True, attested_scripts=[path]) == HIGH


@pytest.mark.parametrize("path", [
    "docs/prompt-foo.md", "archi.md", "Prompt-Guide.md", "Archi.json",
    "some/dir/prompt-notes.md", "PROMPT-x.md", "ARCHI.md",
])
def test_prompt_and_archi_basename_checks_are_case_insensitive(path):
    # `prompt-foo.md` / `archi.md` (lowercase) used to slip past the
    # case-SENSITIVE basename check and downgrade to a doc => LOW.
    assert risk_tier.classify([path], enabled=True) == HIGH


# ------------------------------------------------------------------ git helpers


def _git(repo: Path, *args: str) -> str:
    p = subprocess.run(["git", "-C", str(repo), *args],
                       capture_output=True, text=True, check=False)
    if p.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {p.stderr or p.stdout}")
    return p.stdout.strip()


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "t@example.invalid")
    _git(path, "config", "user.name", "tester")
    (path / "README.md").write_text("baseline\n", encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "baseline")
    return path


def _commit_files(repo: Path, branch: str, start: str, files: dict[str, str]) -> str:
    """Create `branch` at `start`, write every file, commit. Returns the tip."""
    _git(repo, "checkout", "-q", "-B", branch, start)
    for rel, content in files.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", f"{branch}: {', '.join(files)}")
    tip = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "main")
    return tip


def _write_candidate(loops_root: Path, ident_hex: str, branch: str, base: str,
                     tip: str, paths: list[str], *,
                     producer_lineage: str = "anthropic",
                     verdict_lineage: str | None = None,
                     extra: dict | None = None) -> str:
    ident = f"sha256:{ident_hex}"
    art: dict = {
        "contract": "v1.1", "id": ident, "kind": "candidate",
        "title": f"cand {branch}", "created_at": "2026-08-11T00:00:00Z",
        "producer": {"role": "implementer", "actor": "impl@x",
                     "lineage": producer_lineage},
        "base_sha": base, "head_sha": tip, "branch": branch, "paths": paths,
        "evidence": [{"claim": "built", "verified_by": "execution",
                      "command": "pytest", "exit_code": 0}],
        "payload": {"resolves": "x"},
    }
    if verdict_lineage is not None:
        art["verdicts"] = [{"lineage": verdict_lineage, "model": "m",
                            "reviewed_sha": tip, "verdict": "approve"}]
    if extra:
        art.update(extra)
    cdir = loops_root / "candidates"
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / f"sha256_{ident_hex}.json").write_text(json.dumps(art), encoding="utf-8")
    return ident


def _make_loop(loops_root: Path, repo: Path, gate_ws: Path) -> GateLoop:
    (loops_root / "state").mkdir(parents=True, exist_ok=True)
    return GateLoop(loops_root, repo, gate_ws=gate_ws, remote=None, push=False,
                    python=sys.executable)


def _evidence_receipt_path(gate_ws: Path, tip: str) -> Path:
    """The §0 candidate-bound receipt path `_low_tier_gate_pass` reads."""
    ws = str(gate_ws)
    shared_root = ws[:-len("-gate")] if ws.endswith("-gate") else ws
    return (Path(shared_root) / "var" / "gate-evidence"
            / "records" / "merge-gate" / f"{tip}.json")


def _write_gate_pass_receipt(gate_ws: Path, tip: str, payload: dict | None = None) -> Path:
    r = _evidence_receipt_path(gate_ws, tip)
    r.parent.mkdir(parents=True, exist_ok=True)
    r.write_text(json.dumps(payload or {"signed": True, "candidate_sha": tip}),
                 encoding="utf-8")
    return r


@pytest.fixture
def env_on(monkeypatch):
    monkeypatch.setenv("OMNIAGENTOS_TIERED_VERIFY", "1")


# ------------------------------------------------------------ load_candidates wiring


def _low_candidate_repo(tmp_path: Path):
    """A docs + test + additive-schema candidate (attested) and its harness."""
    repo = _init_repo(tmp_path / "repo")
    base = _git(repo, "rev-parse", "HEAD")
    schema = "schema/order.schema.json"
    tip = _commit_files(repo, "low", base, {
        "docs/notes.md": "notes\n",
        "tests/test_low.py": "def test_ok():\n    assert True\n",
        schema: '{"type":"object"}\n',
    })
    loops_root = tmp_path / "loops"
    _write_candidate(loops_root, "1" * 64, "low", base, tip,
                     ["docs/notes.md", "tests/test_low.py", schema],
                     extra={"additive_schema_paths": [schema]})
    gate_ws = tmp_path / "wtree-gate"
    return repo, loops_root, gate_ws, tip


def test_low_candidate_with_signed_gate_pass_lands_with_synthetic_verdict(tmp_path, env_on):
    repo, loops_root, gate_ws, tip = _low_candidate_repo(tmp_path)
    receipt = _write_gate_pass_receipt(gate_ws, tip)
    loop = _make_loop(loops_root, repo, gate_ws)

    cands = loop.load_candidates(set())
    assert len(cands) == 1
    verdicts = cands[0].art.get("verdicts")
    assert isinstance(verdicts, list) and len(verdicts) == 1
    v = verdicts[0]
    assert v["lineage"] == "mechanical-gate"
    assert v["by"] == "merge-gate"
    assert v["reviewed_sha"] == tip
    assert v["receipt"] == hashlib.sha256(receipt.read_bytes()).hexdigest()


def test_low_candidate_without_gate_pass_does_not_land(tmp_path, env_on):
    # Gate is the FLOOR: LOW risk with no signed PASS receipt on its tip never lands.
    repo, loops_root, gate_ws, tip = _low_candidate_repo(tmp_path)
    loop = _make_loop(loops_root, repo, gate_ws)   # no receipt written

    assert loop.load_candidates(set()) == []
    assert any("LOW tier but" in line for line in loop.lines)


def test_low_candidate_with_failing_gate_receipt_does_not_land(tmp_path, env_on):
    repo, loops_root, gate_ws, tip = _low_candidate_repo(tmp_path)
    _write_gate_pass_receipt(gate_ws, tip, {"signed": True, "rc": 1})  # a FAILED run
    loop = _make_loop(loops_root, repo, gate_ws)

    assert loop.load_candidates(set()) == []
    assert any("LOW tier but" in line for line in loop.lines)


def test_high_candidate_is_refused_without_cross_lineage_verdict(tmp_path, env_on):
    repo = _init_repo(tmp_path / "repo")
    base = _git(repo, "rev-parse", "HEAD")
    tip = _commit_files(repo, "high", base,
                        {"pipeline/bridge/integration.py": "x = 1\n"})
    loops_root = tmp_path / "loops"
    _write_candidate(loops_root, "2" * 64, "high", base, tip,
                     ["pipeline/bridge/integration.py"])  # no verdict
    gate_ws = tmp_path / "wtree-gate"
    # Even a signed gate PASS on the tip must NOT rescue a HIGH surface.
    _write_gate_pass_receipt(gate_ws, tip)
    loop = _make_loop(loops_root, repo, gate_ws)

    assert loop.load_candidates(set()) == []
    assert any("cross-lineage build verdict" in line for line in loop.lines)


def test_high_migration_candidate_with_cross_lineage_verdict_is_eligible(tmp_path, env_on):
    repo = _init_repo(tmp_path / "repo")
    base = _git(repo, "rev-parse", "HEAD")
    tip = _commit_files(repo, "mig", base,
                        {"omniagentos/db/migrations/007_add.sql": "ALTER TABLE t;\n"})
    loops_root = tmp_path / "loops"
    _write_candidate(loops_root, "3" * 64, "mig", base, tip,
                     ["omniagentos/db/migrations/007_add.sql"],
                     producer_lineage="anthropic", verdict_lineage="openai")
    gate_ws = tmp_path / "wtree-gate"
    loop = _make_loop(loops_root, repo, gate_ws)

    cands = loop.load_candidates(set())
    assert len(cands) == 1
    # No synthetic mechanical-gate verdict was injected on the HIGH path.
    assert all(v.get("lineage") != "mechanical-gate"
               for v in cands[0].art.get("verdicts", []))


def test_kill_switch_low_candidate_still_requires_cross_lineage_verdict(tmp_path, monkeypatch):
    # Switch OFF: the very candidate that lands LOW with the switch on is instead
    # routed through the pre-tiering check, where schema/ is a risky surface and a
    # signed gate PASS is NOT accepted in place of a cross-lineage verdict.
    monkeypatch.setenv("OMNIAGENTOS_TIERED_VERIFY", "0")
    repo, loops_root, gate_ws, tip = _low_candidate_repo(tmp_path)
    _write_gate_pass_receipt(gate_ws, tip)  # present, but irrelevant when OFF
    loop = _make_loop(loops_root, repo, gate_ws)

    assert loop.load_candidates(set()) == []
    assert any("cross-lineage build verdict" in line for line in loop.lines)


def test_kill_switch_off_low_candidate_lands_with_genuine_verdict(tmp_path, monkeypatch):
    # And with the switch OFF, the same schema candidate DOES land once it carries
    # a real cross-lineage verdict — exact pre-tiering behaviour, no synthetic one.
    monkeypatch.delenv("OMNIAGENTOS_TIERED_VERIFY", raising=False)
    repo = _init_repo(tmp_path / "repo")
    base = _git(repo, "rev-parse", "HEAD")
    schema = "schema/order.schema.json"
    tip = _commit_files(repo, "low", base, {schema: '{"type":"object"}\n'})
    loops_root = tmp_path / "loops"
    _write_candidate(loops_root, "4" * 64, "low", base, tip, [schema],
                     producer_lineage="anthropic", verdict_lineage="openai",
                     extra={"additive_schema_paths": [schema]})
    gate_ws = tmp_path / "wtree-gate"
    loop = _make_loop(loops_root, repo, gate_ws)

    cands = loop.load_candidates(set())
    assert len(cands) == 1
    assert all(v.get("lineage") != "mechanical-gate"
               for v in cands[0].art.get("verdicts", []))


# --------------------------- MINOR: receipt negative-checks fail CLOSED
#
# The gate-PASS receipt that authorizes waiving cross-lineage review must be read
# fail-closed: any shape we cannot positively read as a green run is a fail. The
# earlier checks were type-fragile (`isinstance(rc, int)` let a string `"1"` slip;
# a blocklist of four failure strings let every other `result` value slip).


def test_low_candidate_string_rc_receipt_does_not_land(tmp_path, env_on):
    repo, loops_root, gate_ws, tip = _low_candidate_repo(tmp_path)
    _write_gate_pass_receipt(gate_ws, tip, {"signed": True, "rc": "1"})  # string, not int
    loop = _make_loop(loops_root, repo, gate_ws)

    assert loop.load_candidates(set()) == []
    assert any("LOW tier but" in line for line in loop.lines)


def test_low_candidate_unknown_result_receipt_does_not_land(tmp_path, env_on):
    repo, loops_root, gate_ws, tip = _low_candidate_repo(tmp_path)
    _write_gate_pass_receipt(gate_ws, tip, {"signed": True, "result": "timeout"})
    loop = _make_loop(loops_root, repo, gate_ws)

    assert loop.load_candidates(set()) == []
    assert any("LOW tier but" in line for line in loop.lines)


def test_low_candidate_mismatched_candidate_sha_does_not_land(tmp_path, env_on):
    repo, loops_root, gate_ws, tip = _low_candidate_repo(tmp_path)
    _write_gate_pass_receipt(gate_ws, tip,
                             {"signed": True, "candidate_sha": "0" * 40})  # not the tip
    loop = _make_loop(loops_root, repo, gate_ws)

    assert loop.load_candidates(set()) == []
    assert any("LOW tier but" in line for line in loop.lines)


def test_low_candidate_with_explicit_pass_receipt_lands(tmp_path, env_on):
    # Positive control: a receipt that POSITIVELY declares a green run still lands.
    repo, loops_root, gate_ws, tip = _low_candidate_repo(tmp_path)
    _write_gate_pass_receipt(gate_ws, tip, {
        "signed": True, "rc": 0, "result": "pass", "candidate_sha": tip})
    loop = _make_loop(loops_root, repo, gate_ws)

    cands = loop.load_candidates(set())
    assert len(cands) == 1
    assert cands[0].art["verdicts"][0]["lineage"] == "mechanical-gate"
