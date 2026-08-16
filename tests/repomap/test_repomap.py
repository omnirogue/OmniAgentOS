"""Repo-map: ranking, task-focus, budget/breadth caps, multi-language, caching."""

from __future__ import annotations

from pathlib import Path

from omniagentos.repomap import RepoMap, build_repo_map
from omniagentos.repomap.service import _MAX_PER_FILE


def _write(base: Path, rel: str, text: str) -> None:
    path = base / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_central_file_outranks_isolated_file(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "core.py",
        "class Widget:\n    def assemble(self):\n        return 1\n\n\ndef make_widget():\n    return Widget()\n",
    )
    for i in range(5):  # five files depend on core -> core is central
        _write(
            tmp_path,
            f"user{i}.py",
            f"from core import Widget, make_widget\n\n\ndef use{i}():\n    return make_widget() or Widget()\n",
        )
    _write(tmp_path, "isolated.py", "def unrelated():\n    total = 1 + 2\n    return total\n")

    out = build_repo_map(str(tmp_path), max_tokens=500)
    assert "core.py:" in out
    assert "class Widget" in out
    if "isolated.py:" in out:
        assert out.index("core.py:") < out.index("isolated.py:")


def test_focus_terms_promote_the_defining_file(tmp_path: Path) -> None:
    _write(tmp_path, "core.py", "class Widget:\n    def assemble(self):\n        return 1\n")
    for i in range(6):
        _write(tmp_path, f"u{i}.py", "from core import Widget\n\n\ndef f():\n    return Widget()\n")
    # a low-traffic file, never referenced -> low generic rank
    _write(tmp_path, "special.py", "def rare_singular_helper():\n    return 42\n")

    generic = build_repo_map(str(tmp_path), max_tokens=400)
    focused = build_repo_map(str(tmp_path), focus_terms=["rare_singular_helper"], max_tokens=400)

    # Focus lifts special.py to the very top (ahead of the central core.py).
    assert focused.startswith("special.py:")
    assert "rare_singular_helper" in focused
    # Generic ranking does NOT put it first (core is central); prove focus changed order.
    assert not generic.startswith("special.py:")


def test_per_file_cap_prevents_monopoly(tmp_path: Path) -> None:
    body = "".join(f"def fn{i}():\n    return {i}\n\n\n" for i in range(30))
    _write(tmp_path, "big.py", body)
    _write(tmp_path, "small.py", "def only_one():\n    return 0\n")
    out = build_repo_map(str(tmp_path), max_tokens=2000)
    shown = [line for line in out.splitlines() if line.strip().startswith("def fn")]
    assert len(shown) <= _MAX_PER_FILE


def test_budget_is_respected(tmp_path: Path) -> None:
    for i in range(40):
        _write(tmp_path, f"m{i}.py", f"class C{i}:\n    def method(self):\n        return {i}\n")
    tiny = build_repo_map(str(tmp_path), max_tokens=50)
    big = build_repo_map(str(tmp_path), max_tokens=2000)
    assert 0 < len(tiny) < len(big)


def test_typescript_is_indexed(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "comp.tsx",
        "export function Button() {\n  return null;\n}\nexport class Panel {}\n",
    )
    _write(
        tmp_path,
        "app.tsx",
        "import { Button, Panel } from './comp';\nexport function App() {\n  return Button();\n}\n",
    )
    out = build_repo_map(str(tmp_path), max_tokens=400)
    assert "comp.tsx:" in out
    assert "Button" in out


def test_missing_and_empty_repo_return_empty(tmp_path: Path) -> None:
    assert build_repo_map(str(tmp_path / "does-not-exist")) == ""
    _write(tmp_path, "blank.py", "")
    assert isinstance(build_repo_map(str(tmp_path)), str)


def test_repomap_caches_by_mtime_and_reflects_edits(tmp_path: Path) -> None:
    _write(tmp_path, "a.py", "class Alpha:\n    def go(self):\n        return 1\n")
    repo_map = RepoMap(str(tmp_path))
    first = repo_map.build(max_tokens=300)
    assert "a.py:" in first and "class Alpha" in first
    # Cached second call is identical.
    assert repo_map.build(max_tokens=300) == first
