"""extract_diff: fenced / bare / garbage inputs."""

from __future__ import annotations

from omniagentos.agentless.patch import extract_diff

_SAMPLE_DIFF_BODY = (
    "diff --git a/foo.py b/foo.py\n"
    "index 1111111..2222222 100644\n"
    "--- a/foo.py\n"
    "+++ b/foo.py\n"
    "@@ -1,2 +1,2 @@\n"
    "-def a():\n"
    "+def b():\n"
    "     pass\n"
)


def test_extract_diff_from_diff_fence() -> None:
    raw = f"Here is the fix:\n```diff\n{_SAMPLE_DIFF_BODY}```\nDone."
    extracted = extract_diff(raw)
    assert extracted is not None
    assert extracted.startswith("diff --git a/foo.py")
    assert extracted.endswith("\n")
    assert "+def b():" in extracted


def test_extract_diff_prefers_diff_fence_over_bare_text_before_it() -> None:
    raw = f"diff --git a/should_not_use b/should_not_use\n```diff\n{_SAMPLE_DIFF_BODY}```"
    extracted = extract_diff(raw)
    assert extracted is not None
    assert "foo.py" in extracted
    assert "should_not_use" not in extracted


def test_extract_diff_bare_diff_git_block_no_fence() -> None:
    raw = f"Sure, here's the patch:\n\n{_SAMPLE_DIFF_BODY}\nHope that helps!"
    extracted = extract_diff(raw)
    assert extracted is not None
    assert extracted.startswith("diff --git a/foo.py")
    assert "Hope that helps" not in extracted


def test_extract_diff_bare_dashdashdash_block_no_diff_git_header() -> None:
    body = "--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-old\n+new\n"
    raw = f"prose before\n{body}"
    extracted = extract_diff(raw)
    assert extracted is not None
    assert extracted.startswith("--- a/foo.py")


def test_extract_diff_garbage_returns_none() -> None:
    assert extract_diff("I refuse to write a diff, sorry!") is None
    assert extract_diff("") is None
    assert extract_diff("```python\nprint('hi')\n```") is None


def test_extract_diff_normalizes_trailing_newline() -> None:
    raw = f"```diff\n{_SAMPLE_DIFF_BODY}\n\n\n```"
    extracted = extract_diff(raw)
    assert extracted is not None
    assert extracted.endswith("\n")
    assert not extracted.endswith("\n\n")
