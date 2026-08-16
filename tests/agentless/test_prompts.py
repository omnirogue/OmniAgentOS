"""build_patch_prompt: stable/task ordering, byte-stable reuse, windowed content."""

from __future__ import annotations

from omniagentos.agentless.contracts import LocalizationResult, SymbolRef
from omniagentos.agentless.prompts import _windowed_content, build_patch_prompt
from omniagentos.promptshape.segments import render, stable_prefix


def _loc(focus_files: list[str], symbols: list[SymbolRef] | None = None) -> LocalizationResult:
    return LocalizationResult(
        repo_dir="/repo",
        focus_files=focus_files,
        top_symbols=symbols or [],
        repo_map="repo_map_contents_here",
    )


def test_build_patch_prompt_stable_before_task() -> None:
    loc = _loc(["a.py"])
    files = {"a.py": "print('hello')\n"}
    segments = build_patch_prompt(loc, files, "fix the bug")
    rendered = render(segments)
    assert rendered.endswith("Task:\nfix the bug")
    assert rendered.index("repo_map_contents_here") < rendered.index("fix the bug")
    assert "print('hello')" in rendered


def test_build_patch_prompt_byte_identical_stable_prefix_across_two_tasks() -> None:
    loc = _loc(["a.py"])
    files = {"a.py": "x = 1\n"}
    segments_1 = build_patch_prompt(loc, files, "task variant one")
    segments_2 = build_patch_prompt(loc, files, "a totally different task variant two")
    assert stable_prefix(segments_1) == stable_prefix(segments_2)


def test_build_patch_prompt_skips_missing_file_contents() -> None:
    loc = _loc(["missing.py"])
    segments = build_patch_prompt(loc, {}, "task")
    labels = [s.label for s in segments]
    assert not any(label.startswith("file:") for label in labels)


def test_windowed_content_short_file_included_fully_with_line_numbers() -> None:
    content = "\n".join(f"line{i}" for i in range(1, 11))
    out = _windowed_content("a.py", content, [])
    assert "line1" in out
    assert "line10" in out
    assert "    1  line1" in out


def test_windowed_content_long_file_windows_around_symbols_with_elision() -> None:
    total_lines = 1000
    content = "\n".join(f"line{i}" for i in range(1, total_lines + 1))
    symbols = [SymbolRef(rel_path="big.py", name="target", line=500, signature="def target():")]
    out = _windowed_content("big.py", content, symbols)

    # window is [440, 560]; content outside that (e.g. line 1, line 900) is elided.
    assert "line500" in out
    assert "line440" in out
    assert "line560" in out
    assert "line1\n" not in out and not out.startswith("    1  line1")
    assert "line900" not in out
    assert "⋮" in out
    # true line numbers preserved
    assert "  500  line500" in out


def test_windowed_content_merges_overlapping_windows() -> None:
    total_lines = 1000
    content = "\n".join(f"L{i}" for i in range(1, total_lines + 1))
    symbols = [
        SymbolRef(rel_path="f.py", name="s1", line=100, signature="def s1():"),
        SymbolRef(
            rel_path="f.py", name="s2", line=140, signature="def s2():"
        ),  # overlaps s1's window
    ]
    out = _windowed_content("f.py", content, symbols)

    # windows [40,160] and [80,200] overlap and must merge into one contiguous
    # [40,200] block: everything in between (e.g. L100..L140) present with NO
    # elision marker interrupting it, and exactly 2 elisions at the outer edges
    # (before L40 and after L200, since the file runs 1..300).
    assert "L100" in out and "L140" in out and "L40" in out and "L200" in out
    between = out[out.index("  100  L100") : out.index("  140  L140")]
    assert "⋮" not in between
    assert out.count("⋮") == 2
    assert "L1\n" not in out  # line 1 is outside the merged window, elided
    assert "L300" not in out  # line 300 is outside the merged window, elided
