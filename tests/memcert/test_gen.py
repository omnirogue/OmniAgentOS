"""Decisive tests for the memcert fixture generator (scripts/memcert/gen.py).

Hermetic: no network, no wall clock in fixture content (virtual 2027 dates),
deterministic under fixed seeds, tmp_path only.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
GEN_PATH = REPO_ROOT / "scripts" / "memcert" / "gen.py"
CORE_PATH = REPO_ROOT / "scripts" / "memcert" / "core.py"

SEED = 42
RUN_UUID = "test-run-uuid-0001"
FORBIDDEN_KEYS = {"value", "aliases", "stale_values", "answer_spec"}


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # dataclass resolution needs the module reachable via sys.modules.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


gen = _load("memcert_gen_under_test", GEN_PATH)
core = _load("memcert_core_under_test", CORE_PATH)


@pytest.fixture(scope="module")
def world():
    return gen.generate_world(SEED, scale="S", split="dev")


@pytest.fixture(scope="module")
def fixture_dir(world, tmp_path_factory):
    out = tmp_path_factory.mktemp("memcert-fixtures") / f"w{SEED}"
    world.write_fixtures(out, run_uuid=RUN_UUID)
    return out


def _tree_hashes(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def _all_keys(obj):
    if isinstance(obj, dict):
        for key, val in obj.items():
            yield key
            yield from _all_keys(val)
    elif isinstance(obj, list):
        for val in obj:
            yield from _all_keys(val)


# 1. Determinism: same (seed, scale, split, run_uuid) -> byte-identical tree.
def test_same_seed_scale_split_is_byte_identical(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    gen.generate_world(SEED, scale="S", split="dev").write_fixtures(a, run_uuid=RUN_UUID)
    gen.generate_world(SEED, scale="S", split="dev").write_fixtures(b, run_uuid=RUN_UUID)
    hashes_a = _tree_hashes(a)
    assert hashes_a == _tree_hashes(b)
    # 8 main + 6 distractor sessions + items.jsonl + world-meta.json
    assert len(hashes_a) == 16


# 2. Different seeds produce different worlds.
def test_different_seeds_differ(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    gen.generate_world(SEED, scale="S", split="dev").write_fixtures(a, run_uuid=RUN_UUID)
    gen.generate_world(SEED + 1, scale="S", split="dev").write_fixtures(b, run_uuid=RUN_UUID)
    assert _tree_hashes(a) != _tree_hashes(b)


# 3. Canary is the first line of every emitted file.
def test_canary_first_line_in_every_file(fixture_dir):
    files = [p for p in fixture_dir.rglob("*") if p.is_file()]
    assert len(files) == 16
    expected = core.canary_line(RUN_UUID)
    for path in files:
        first = path.read_text(encoding="utf-8").splitlines()[0]
        assert expected in first, path
        if path.suffix == ".jsonl":
            record = json.loads(first)
            assert record["type"] == "canary"
            assert record["text"] == expected


# 4. items.jsonl leaks no answers: forbidden keys absent everywhere, and no
#    D-axis current-value string appears anywhere in the file.
def test_items_jsonl_leaks_no_answers(world, fixture_dir):
    text = (fixture_dir / "items.jsonl").read_text(encoding="utf-8")
    records = [json.loads(line) for line in text.splitlines()[1:]]
    assert records
    for record in records:
        keys = set(_all_keys(record))
        assert FORBIDDEN_KEYS.isdisjoint(keys), record["item_id"]
    d_items = [i for i in world.items() if i.axis == "D"]
    assert d_items
    for item in d_items:
        current = item.answer_spec.value
        assert isinstance(current, str) and current
        assert current not in text, item.item_id


# 5. Every axis A-H present; levels 1-3 populated for every axis except G.
def test_every_axis_and_level_populated(world):
    items = world.items()
    assert {i.axis for i in items} == set("ABCDEFGH")
    for axis in "ABCDEFH":
        levels = {i.level for i in items if i.axis == axis}
        assert levels == {1, 2, 3}, axis
        assert sum(1 for i in items if i.axis == axis) == 8, axis
    assert sum(1 for i in items if i.axis == "G") == 6


# 6. Axis invariants: D/F stale_values non-empty; E abstain; H params with
#    tool+args; B requires a >=2-session join.
def test_axis_answer_spec_invariants(world):
    for item in world.items():
        spec = item.answer_spec
        if item.axis in ("D", "F"):
            assert spec.stale_values, item.item_id
        if item.axis == "E":
            assert spec.kind == "abstain", item.item_id
        if item.axis == "H":
            assert spec.kind == "params", item.item_id
            assert set(spec.value) == {"tool", "args"}, item.item_id
            assert spec.value["tool"] and spec.value["args"], item.item_id
        if item.axis == "B":
            assert len(set(item.session_scope)) >= 2, item.item_id


# 7. G items carry all three lesson routings; placebo token-matched ±10%.
def test_g_items_carry_three_lesson_routings(world):
    g_items = [i for i in world.items() if i.axis == "G"]
    assert len(g_items) == 6
    for item in g_items:
        overrides = item.arm_overrides
        assert set(overrides) == {"lessons_real", "lessons_placebo", "lessons_shuffled"}
        real = overrides["lessons_real"]
        placebo = overrides["lessons_placebo"]
        shuffled = overrides["lessons_shuffled"]
        assert len(real) == len(placebo) == len(shuffled) >= 1
        for r, p in zip(real, placebo, strict=True):
            assert abs(len(p) - len(r)) <= 0.10 * len(r), item.item_id
        assert shuffled != real, item.item_id
        assert not set(shuffled) & set(real), item.item_id


# 8. approx_tokens within the scale-S corpus budget.
def test_scale_s_token_budget(world):
    assert 15_000 <= world.approx_tokens() <= 60_000


# 9. --emit-answers refuses any path inside a git checkout (exit 2).
def test_emit_answers_refuses_checkout_path(tmp_path):
    target = REPO_ROOT / "var" / "memcert-answers-must-never-exist.jsonl"
    proc = subprocess.run(
        [
            sys.executable,
            str(GEN_PATH),
            "--seed", "7",
            "--out", str(tmp_path / "fx"),
            "--emit-answers", str(target),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 2, proc.stderr
    assert "refused" in proc.stderr.lower()
    assert not target.exists()


def test_emit_answers_writes_specs_outside_checkouts(tmp_path):
    answers = tmp_path / "protected" / "answers.jsonl"
    proc = subprocess.run(
        [
            sys.executable,
            str(GEN_PATH),
            "--seed", "7",
            "--out", str(tmp_path / "fx"),
            "--emit-answers", str(answers),
            "--run-uuid", RUN_UUID,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    lines = answers.read_text(encoding="utf-8").splitlines()
    assert core.canary_line(RUN_UUID) in lines[0]
    rows = [json.loads(line) for line in lines[1:]]
    assert rows
    assert all("answer_spec" in row and "item_id" in row for row in rows)


# 10. Grade-time re-derivation: two generations yield identical answer specs.
def test_answer_specs_rederive_identically(world):
    again = gen.generate_world(SEED, scale="S", split="dev")
    first = {i.item_id: i.answer_spec.to_json() for i in world.items()}
    second = {i.item_id: i.answer_spec.to_json() for i in again.items()}
    assert first == second
    assert {i.item_id: i.public_json() for i in world.items()} == {
        i.item_id: i.public_json() for i in again.items()
    }


# -- shape and construction invariants ------------------------------------


def test_session_lines_match_transcript_shape(fixture_dir):
    session_files = sorted((fixture_dir / "sessions").glob("*.jsonl"))
    assert len(session_files) == 14
    for path in session_files:
        body = path.read_text(encoding="utf-8").splitlines()[1:]
        assert 12 <= len(body) <= 30, path
        for line in body:
            entry = json.loads(line)
            assert entry["type"] in ("user", "assistant")
            message = entry["message"]
            assert message["role"] == entry["type"]
            block = message["content"][0]
            assert block["type"] == "text"
            assert isinstance(block["text"], str) and block["text"]
            assert entry["timestamp"].startswith("2027-")


def test_world_meta_stores_seed_hash_not_raw_seed(fixture_dir):
    meta = json.loads((fixture_dir / "world-meta.json").read_text(encoding="utf-8"))
    assert meta["seed_sha256"] == hashlib.sha256(str(SEED).encode()).hexdigest()
    assert "seed" not in meta
    assert meta["scale"] == "S"
    assert meta["split"] == "dev"
    assert meta["counts"]["sessions"] == 14
    assert meta["virtual_dates"]["start"].startswith("2027-")


def test_item_ids_cluster_and_split_stamp(world):
    items = world.items()
    ids = [i.item_id for i in items]
    assert len(ids) == len(set(ids))
    for item in items:
        assert re.fullmatch(rf"MEM-{item.axis}{item.level}-\d\d-w{SEED}", item.item_id)
        assert item.cluster_id == f"world-w{SEED}"
        assert item.split == "dev"


def test_cert_split_ids_never_carry_the_raw_seed():
    """The seed is the answer key (DESIGN §4/§7): cert items.jsonl must expose
    only a seed-hash tag, or any arm reading it can re-derive every answer."""
    cert = gen.generate_world(SEED, scale="S", split="cert")
    sha10 = hashlib.sha256(str(SEED).encode("utf-8")).hexdigest()[:10]
    for item in cert.items():
        assert str(SEED) not in item.item_id
        assert str(SEED) not in item.cluster_id
        assert re.fullmatch(rf"MEM-{item.axis}{item.level}-\d\d-wh{sha10}", item.item_id)
        assert item.cluster_id == f"world-wh{sha10}"
        assert item.split == "cert"


_STOPWORDS = {
    "the", "a", "an", "for", "of", "to", "is", "at", "on", "in", "by", "its",
    "with", "who", "which", "what",
}


def test_a_probes_share_no_content_words_with_source(world):
    """NoLiMa control: MEM-A probes are paraphrased away from the source
    sentence; overlap is stopwords and world proper nouns only."""
    a_items = [i for i in world.items() if i.axis == "A"]
    assert len(a_items) == 8
    for item in a_items:
        source = world.audit[item.item_id]["source_phrasing"]
        proper = {w.lower() for w in re.findall(r"[A-Z][a-z]+", source)}
        source_words = {w for w in re.findall(r"[a-z]+", source.lower())}
        question_words = {w for w in re.findall(r"[a-z]+", item.question.lower())}
        overlap = (question_words & source_words) - _STOPWORDS - proper
        assert not overlap, (item.item_id, overlap)
