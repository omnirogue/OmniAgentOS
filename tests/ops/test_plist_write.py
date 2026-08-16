from __future__ import annotations

from pathlib import Path

import pytest

from scripts.lib.plist_write import PlistWriteError, write_plist_atomic

VALID_PLIST = """<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">
<plist version=\"1.0\"><dict><key>Label</key><string>com.example.test</string></dict></plist>
"""


def test_json_stub_is_refused_without_touching_existing_plist(tmp_path: Path) -> None:
    target = tmp_path / "com.example.test.plist"
    target.write_text(VALID_PLIST, encoding="utf-8")

    with pytest.raises(PlistWriteError, match="invalid plist"):
        write_plist_atomic(target, '{"Hour":2,"Minute":30}')

    assert target.read_text(encoding="utf-8") == VALID_PLIST
    assert list(tmp_path.glob(".com.example.test.plist.*.tmp")) == []


def test_valid_plist_replaces_target_via_sibling_temp_and_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import scripts.lib.plist_write as plist_write

    target = tmp_path / "com.example.test.plist"
    target.write_text("old-but-valid", encoding="utf-8")
    replaced: list[tuple[Path, Path]] = []
    real_replace = plist_write.os.replace

    def observe_replace(source: Path, destination: Path) -> None:
        replaced.append((Path(source), Path(destination)))
        real_replace(source, destination)

    monkeypatch.setattr(plist_write.os, "replace", observe_replace)
    write_plist_atomic(target, VALID_PLIST)

    assert target.read_text(encoding="utf-8") == VALID_PLIST
    assert replaced and replaced[0][0].parent == target.parent
    assert replaced[0][0].name.startswith(f".{target.name}.")
    assert replaced[0][1] == target
