"""compress() behavior: off/basic modes, diff/patch exemption, llmlingua2 fallback."""

from __future__ import annotations

import pytest

from omniagentos.promptshape.compress import compress


def test_default_mode_off_returns_text_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OMNIAGENTOS_COMPRESS", raising=False)
    text = "line1\nline1\nline1\nline1\nline1\n"
    assert compress(text, kind="log") == text


def test_env_off_explicit_returns_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIAGENTOS_COMPRESS", "off")
    text = "\x1b[31mred text\x1b[0m"
    assert compress(text, kind="log") == text


def test_basic_strips_ansi_escapes() -> None:
    raw = "\x1b[1;32mPASSED\x1b[0m test_foo"
    out = compress(raw, kind="log", mode="basic")
    assert "\x1b" not in out
    assert "PASSED" in out
    assert "test_foo" in out


def test_basic_collapses_more_than_three_identical_lines() -> None:
    raw = "\n".join(["repeated line"] * 6)
    out = compress(raw, kind="log", mode="basic")
    assert out.count("repeated line") == 1
    assert "[repeated 6 times]" in out


def test_basic_does_not_collapse_three_or_fewer_identical_lines() -> None:
    raw = "\n".join(["same"] * 3)
    out = compress(raw, kind="log", mode="basic")
    assert "repeated" not in out
    assert out == raw


def test_basic_folds_traceback_keeping_first_2_last_4_frames_and_exception_line() -> None:
    frames = [f'  File "mod{i}.py", line {i}, in func{i}' for i in range(1, 11)]
    raw = "Traceback (most recent call last):\n" + "\n".join(frames) + "\nValueError: boom"
    out = compress(raw, kind="log", mode="basic")
    assert "Traceback (most recent call last):" in out
    assert "ValueError: boom" in out
    # first 2 and last 4 frames survive verbatim
    for i in [1, 2, 7, 8, 9, 10]:
        assert f"mod{i}.py" in out
    # middle frames (3..6) are elided
    for i in [3, 4, 5, 6]:
        assert f"mod{i}.py" not in out
    assert "elided" in out


def test_basic_leaves_short_traceback_untouched() -> None:
    raw = 'Traceback (most recent call last):\n  File "a.py", line 1, in f\nValueError: x'
    out = compress(raw, kind="log", mode="basic")
    assert out == raw


def test_basic_collapses_blank_line_runs() -> None:
    raw = "a\n\n\n\n\nb"
    out = compress(raw, kind="log", mode="basic")
    assert "\n\n\n" not in out
    assert "a" in out and "b" in out


def test_diff_kind_never_compressed_even_in_basic_mode() -> None:
    raw = "\x1b[31m" + "\n".join(["+same line"] * 10) + "\x1b[0m"
    out = compress(raw, kind="diff", mode="basic")
    assert out == raw


def test_patch_kind_never_compressed_even_via_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIAGENTOS_COMPRESS", "basic")
    raw = "\n".join(["-old line"] * 8)
    out = compress(raw, kind="patch")
    assert out == raw


def test_env_var_selects_basic_mode_when_mode_arg_omitted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIAGENTOS_COMPRESS", "basic")
    raw = "\x1b[1mstyled\x1b[0m"
    out = compress(raw, kind="log")
    assert "\x1b" not in out


def test_llmlingua2_mode_falls_back_to_basic_when_package_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # llmlingua is not a repo dependency (stdlib-only rule) so ImportError is the
    # real, expected path here — this asserts the fallback, not a mocked one.
    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "llmlingua":
            raise ImportError("no module named llmlingua")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", fake_import)
    raw = "\x1b[31mred\x1b[0m\n" + "\n".join(["dup"] * 5)
    out = compress(raw, kind="log", mode="llmlingua2")
    # Fell back to 'basic': ANSI stripped and repeat run collapsed.
    assert "\x1b" not in out
    assert "[repeated 5 times]" in out


def test_llmlingua2_mode_falls_back_when_compressor_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys
    import types

    fake_module = types.ModuleType("llmlingua")

    class _BoomCompressor:
        def compress_prompt(self, text: str) -> dict[str, str]:
            raise RuntimeError("boom")

    fake_module.PromptCompressor = _BoomCompressor  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "llmlingua", fake_module)
    raw = "\n".join(["dup"] * 5)
    out = compress(raw, kind="log", mode="llmlingua2")
    assert "[repeated 5 times]" in out
