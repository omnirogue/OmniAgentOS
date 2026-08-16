"""Decisive tests for the system arm (production-shape memory stack).

Uses the real gen.py fixture world + the real omniagentos memory stack over a
scratch sqlite DB — hermetic (tmp dirs, no network, no live DB).
"""

from __future__ import annotations

import importlib.util
import random
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


gen = _load("memcert_gen_sysarm_test", REPO_ROOT / "scripts" / "memcert" / "gen.py")

sys.path.insert(0, str(REPO_ROOT / "scripts" / "memcert"))
sys.path.insert(0, str(REPO_ROOT))
import arms as arms_mod  # noqa: E402
import system_arm  # noqa: E402

SEED = 77


@pytest.fixture(scope="module")
def world_dir(tmp_path_factory):
    world = gen.generate_world(SEED, scale="S", split="dev")
    out = tmp_path_factory.mktemp("sysarm") / f"w{SEED}"
    world.write_fixtures(out, run_uuid="sysarm-test-uuid")
    return out


@pytest.fixture(scope="module")
def world(world_dir):
    return gen.generate_world(SEED, scale="S", split="dev")


def test_system_arm_builds_and_respects_budget(world_dir, world) -> None:
    item = world.items()[0]
    ctx = arms_mod.build_context("system", world_dir, item, 12000, random.Random(0))
    assert ctx.arm == "system"
    assert ctx.context_block
    assert len(ctx.context_block) // 4 <= int(12000 * 1.15)
    assert ctx.meta["sources"] == ["memory.assemble", "transcript"]


def test_system_arm_contains_recent_session_content(world_dir, world) -> None:
    sessions = sorted((world_dir / "sessions").glob("*.jsonl"))
    newest_text = sessions[-1].read_text(encoding="utf-8").splitlines()[1]
    item = world.items()[0]
    ctx = arms_mod.build_context("system", world_dir, item, 12000, random.Random(0))
    # At least some content from the newest session must be present.
    import json as _json

    rec = _json.loads(newest_text)
    fragment = rec["message"]["content"][0]["text"][:40]
    assert fragment in ctx.context_block


def test_system_arm_never_reads_live_db(world_dir, world) -> None:
    # The scratch store must live under the tempdir, never var/runtime.
    key = system_arm._world_key(world_dir)
    store, _scope = system_arm._STORE_CACHE[key]
    assert "runtime" not in str(store._path if hasattr(store, "_path") else store)


def test_system_arm_is_deterministic_per_world(world_dir, world) -> None:
    item = world.items()[3]
    a = arms_mod.build_context("system", world_dir, item, 12000, random.Random(0))
    b = arms_mod.build_context("system", world_dir, item, 12000, random.Random(0))
    assert a.context_block == b.context_block


def test_canary_never_leaks_into_system_context(world_dir, world) -> None:
    item = world.items()[0]
    ctx = arms_mod.build_context("system", world_dir, item, 12000, random.Random(0))
    assert "MEMCERT-CANARY" not in ctx.context_block
