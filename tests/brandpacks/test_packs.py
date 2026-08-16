"""Brand pack load + materialize (Grok pack.py API)."""

from __future__ import annotations

from pathlib import Path

from omniagentos.brandpacks import load_brand_pack, materialize_to_inputs


def test_load_and_materialize(tmp_path: Path) -> None:
    pack_dir = tmp_path / "brand"
    pack_dir.mkdir()
    (pack_dir / "voice.md").write_text("No em-dashes.\n", encoding="utf-8")
    (pack_dir / "offer.json").write_text('{"name": "Pro"}\n', encoding="utf-8")
    (pack_dir / "banned_claims.txt").write_text("# comment\nguaranteed\n", encoding="utf-8")
    pack = load_brand_pack(pack_dir)
    assert "em-dashes" in pack.voice_md
    assert pack.offer["name"] == "Pro"
    assert pack.banned_claims == ("guaranteed",)
    dest = materialize_to_inputs(pack, tmp_path, scope="scope1")
    assert dest.is_dir()
    assert (dest / "voice.md").is_file()
