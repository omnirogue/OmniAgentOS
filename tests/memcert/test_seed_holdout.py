"""Decisive tests for scripts/memcert/seed_holdout.py (cert-seed rotation).

The cert seed is the suite's answer key (DESIGN §4/§7): these tests pin the
out-of-checkout refusal, ensure-idempotence, hash-only disclosure, and
reveal-on-retirement semantics.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "memcert" / "seed_holdout.py"


def _load():
    spec = importlib.util.spec_from_file_location("memcert_seed_holdout", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_ensure_is_idempotent_and_never_prints_the_seed(tmp_path: Path) -> None:
    mod = _load()
    first = mod.ensure(tmp_path / "state", "2026-W33")
    second = mod.ensure(tmp_path / "state", "2026-W33")
    assert first["seed_sha256"] == second["seed_sha256"]
    assert "seed" not in first and "seed" not in second
    on_disk = json.loads((tmp_path / "state" / "cert-seed-2026-W33.json").read_text())
    assert hashlib.sha256(str(on_disk["seed"]).encode()).hexdigest() == first["seed_sha256"]


def test_seed_file_is_owner_only(tmp_path: Path) -> None:
    mod = _load()
    mod.ensure(tmp_path / "state", "2026-W33")
    mode = (tmp_path / "state" / "cert-seed-2026-W33.json").stat().st_mode & 0o777
    assert mode == 0o600


def test_rotate_reveals_retired_and_mints_fresh(tmp_path: Path) -> None:
    mod = _load()
    old = mod.ensure(tmp_path / "state", "2026-W33")
    out = mod.rotate(tmp_path / "state", "2026-W34")
    assert [r["seed_sha256"] for r in out["retired"]] == [old["seed_sha256"]]
    retired_rows = [
        json.loads(line)
        for line in (tmp_path / "state" / "retired.jsonl").read_text().splitlines()
    ]
    # Reveal on retirement: the raw seed graduates to dev-split material.
    assert retired_rows[0]["seed"] is not None
    assert out["current"]["seed_sha256"] != old["seed_sha256"]
    assert not (tmp_path / "state" / "cert-seed-2026-W33.json").exists()


def test_in_checkout_state_dir_is_refused_exit_2(tmp_path: Path) -> None:
    inside = REPO_ROOT / "var" / "memcert-test-should-never-exist"
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "ensure", "--state-dir", str(inside)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 2
    assert "REFUSED" in proc.stderr
    assert not inside.exists()
