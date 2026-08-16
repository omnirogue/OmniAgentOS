"""localize() on a small fixture tree: task-mentioned file should rank first."""

from __future__ import annotations

from pathlib import Path

from omniagentos.agentless.localize import localize


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def test_localize_ranks_task_mentioned_file_first(tmp_path: Path) -> None:
    _write(
        tmp_path / "pkg" / "billing.py",
        "def charge_customer(amount):\n"
        "    return amount * 1.0\n\n\n"
        "class BillingError(Exception):\n"
        "    pass\n",
    )
    _write(
        tmp_path / "pkg" / "unrelated.py",
        "def totally_unrelated_helper():\n"
        "    return 42\n\n\n"
        "class SomeOtherThing:\n"
        "    def method(self):\n"
        "        return 1\n",
    )
    _write(
        tmp_path / "pkg" / "__init__.py",
        "from pkg.billing import charge_customer\nfrom pkg.unrelated import totally_unrelated_helper\n",
    )

    result = localize(str(tmp_path), "Fix a bug in pkg/billing.py: charge_customer overcharges")

    assert result.focus_files, "expected at least one focus file"
    assert result.focus_files[0] == "pkg/billing.py"
    assert "pkg/billing.py" in result.repo_map


def test_localize_focus_terms_bias_ranking_toward_mentioned_symbol(tmp_path: Path) -> None:
    _write(
        tmp_path / "a.py",
        "def rarely_referenced_thing():\n    return None\n",
    )
    _write(
        tmp_path / "b.py",
        "def compute_discount(price):\n    return price * 0.9\n",
    )
    _write(
        tmp_path / "c.py",
        "from a import rarely_referenced_thing\nfrom b import compute_discount\n"
        "def orchestrate():\n    rarely_referenced_thing()\n    compute_discount(1)\n",
    )

    # Task never mentions a file path, only a symbol name -> focus_terms path.
    result = localize(str(tmp_path), "compute_discount returns the wrong percentage")

    top_symbol_names = [s.name for s in result.top_symbols[:5]]
    assert "compute_discount" in top_symbol_names


def test_localize_returns_symbol_refs_with_line_numbers(tmp_path: Path) -> None:
    _write(
        tmp_path / "mod.py",
        "def first():\n    pass\n\n\ndef second():\n    pass\n",
    )
    result = localize(str(tmp_path), "fix second() in mod.py")
    assert any(s.name == "second" and s.line == 5 for s in result.top_symbols)


def test_localize_empty_repo_returns_empty_result(tmp_path: Path) -> None:
    result = localize(str(tmp_path), "fix something")
    assert result.focus_files == []
    assert result.top_symbols == []
    assert result.repo_map == ""


def test_localize_respects_max_files(tmp_path: Path) -> None:
    for i in range(10):
        _write(tmp_path / f"m{i}.py", f"def f{i}():\n    return {i}\n")
    result = localize(str(tmp_path), "generic task mentioning nothing specific", max_files=3)
    assert len(result.focus_files) <= 3
