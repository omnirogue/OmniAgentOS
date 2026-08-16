"""Brand-pack intake stays validated, scoped, and immutable to agents."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from omniagentos.brandpacks import BrandPackError, load_brand_pack, materialize_to_inputs


def _write_pack(root: Path) -> Path:
    root.mkdir()
    (root / "voice.md").write_text("Direct, specific, and warm.\n", encoding="utf-8")
    (root / "offer.json").write_text(
        json.dumps({"name": "Launch audit", "price": 500}), encoding="utf-8"
    )
    (root / "banned_claims.txt").write_text(
        "# Legal review required\nguaranteed results\n\nno-risk\n", encoding="utf-8"
    )
    return root


def test_load_brand_pack_parses_all_required_sources(tmp_path: Path) -> None:
    pack = load_brand_pack(_write_pack(tmp_path / "brand"))

    assert pack.voice_md == "Direct, specific, and warm.\n"
    assert pack.offer == {"name": "Launch audit", "price": 500}
    assert pack.banned_claims == ("guaranteed results", "no-risk")


def test_load_brand_pack_rejects_missing_required_file(tmp_path: Path) -> None:
    root = _write_pack(tmp_path / "brand")
    (root / "voice.md").unlink()

    with pytest.raises(BrandPackError, match="voice.md"):
        load_brand_pack(root)


def test_load_brand_pack_rejects_invalid_offer_json(tmp_path: Path) -> None:
    root = _write_pack(tmp_path / "brand")
    (root / "offer.json").write_text("{not json", encoding="utf-8")

    with pytest.raises(BrandPackError, match="invalid brand pack"):
        load_brand_pack(root)


def test_materialize_to_inputs_copies_read_only_files_under_scope(tmp_path: Path) -> None:
    source = _write_pack(tmp_path / "brand")
    pack = load_brand_pack(source)
    scope_root = tmp_path / "scope-root"

    destination = materialize_to_inputs(pack, scope_root, scope="run-42")

    assert destination == scope_root / "var" / "inputs" / "run-42" / "brand"
    for name in ("voice.md", "offer.json", "banned_claims.txt"):
        copied = destination / name
        assert copied.read_text(encoding="utf-8") == (source / name).read_text(encoding="utf-8")
        assert stat.S_IMODE(copied.stat().st_mode) == 0o444
    assert destination.resolve().is_relative_to(scope_root.resolve())


def test_materialize_rejects_unsafe_scope_component(tmp_path: Path) -> None:
    pack = load_brand_pack(_write_pack(tmp_path / "brand"))

    with pytest.raises(ValueError, match="unsafe path component"):
        materialize_to_inputs(pack, tmp_path / "scope-root", scope="../other")


def test_materialize_reprovisions_over_existing_0444_files(tmp_path: Path) -> None:
    """M-29: second provision must replace read-only copies, not soft-fail/no-op."""
    source = _write_pack(tmp_path / "brand-v1")
    pack_v1 = load_brand_pack(source)
    scope_root = tmp_path / "scope-root"

    destination = materialize_to_inputs(pack_v1, scope_root, scope="client-a")
    voice_path = destination / "voice.md"
    assert voice_path.read_text(encoding="utf-8") == "Direct, specific, and warm.\n"
    assert stat.S_IMODE(voice_path.stat().st_mode) == 0o444

    # Mutate the pack source and re-materialize over the existing 0444 targets.
    (source / "voice.md").write_text("Updated voice after review.\n", encoding="utf-8")
    (source / "offer.json").write_text(
        json.dumps({"name": "Launch audit", "price": 750}), encoding="utf-8"
    )
    pack_v2 = load_brand_pack(source)

    destination2 = materialize_to_inputs(pack_v2, scope_root, scope="client-a")
    assert destination2 == destination
    assert voice_path.read_text(encoding="utf-8") == "Updated voice after review.\n"
    assert json.loads((destination / "offer.json").read_text(encoding="utf-8")) == {
        "name": "Launch audit",
        "price": 750,
    }
    for name in ("voice.md", "offer.json", "banned_claims.txt"):
        assert stat.S_IMODE((destination / name).stat().st_mode) == 0o444


def test_brand_pack_file_symlinked_outside_pack_is_rejected(tmp_path):
    """A symlink inside a brand pack must not read arbitrary files: pack
    bodies are injected verbatim into prompts that reach external APIs."""
    import json as _json

    import pytest

    from omniagentos.brandpacks.pack import BrandPackError, load_brand_pack

    secret = tmp_path / "secret.txt"
    secret.write_text("SUPER-SECRET", encoding="utf-8")
    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "offer.json").write_text(_json.dumps({"name": "x"}), encoding="utf-8")
    (pack / "banned_claims.txt").write_text("# none\n", encoding="utf-8")
    (pack / "voice.md").symlink_to(secret)

    with pytest.raises(BrandPackError, match="outside its pack directory"):
        load_brand_pack(pack)


def test_project_memory_symlinked_outside_memories_root_is_ignored(tmp_path):
    """MEMORY.md escaping var/memories via symlink must yield no content."""
    from omniagentos.brandpacks.pack import _bounded_memory

    secret = tmp_path / "outside.md"
    secret.write_text("SUPER-SECRET", encoding="utf-8")
    mem_dir = tmp_path / "var" / "memories" / "proj"
    mem_dir.mkdir(parents=True)
    (mem_dir / "MEMORY.md").symlink_to(secret)

    assert _bounded_memory({"id": "proj"}, tmp_path) == ""


def test_project_memory_reads_real_file_within_root(tmp_path):
    from omniagentos.brandpacks.pack import _bounded_memory

    mem_dir = tmp_path / "var" / "memories" / "proj"
    mem_dir.mkdir(parents=True)
    (mem_dir / "MEMORY.md").write_text("real lesson", encoding="utf-8")

    assert _bounded_memory({"id": "proj"}, tmp_path) == "real lesson"
