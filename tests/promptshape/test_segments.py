"""Segment ordering + byte-stable-prefix tests (lost-in-the-middle discipline)."""

from __future__ import annotations

from omniagentos.promptshape.segments import Segment, render, stable_prefix


def test_render_orders_stable_first_bulk_middle_task_last() -> None:
    segments = [
        Segment(kind="task", label="task", text="TASK"),
        Segment(kind="bulk", label="bulk1", text="BULK1"),
        Segment(kind="stable", label="sys", text="SYS"),
        Segment(kind="bulk", label="bulk2", text="BULK2"),
        Segment(kind="stable", label="map", text="MAP"),
    ]
    rendered = render(segments)
    # stable segments (input order) first, then bulk (input order), then task last.
    assert rendered == "SYS\n\nMAP\n\nBULK1\n\nBULK2\n\nTASK"


def test_render_task_always_last_even_if_declared_first() -> None:
    segments = [
        Segment(kind="task", label="task", text="do the thing"),
        Segment(kind="stable", label="sys", text="system prompt"),
    ]
    rendered = render(segments)
    assert rendered.endswith("do the thing")
    assert rendered.index("system prompt") < rendered.index("do the thing")


def test_stable_prefix_is_true_prefix_of_full_render() -> None:
    segments = [
        Segment(kind="stable", label="a", text="AAA"),
        Segment(kind="stable", label="b", text="BBB"),
        Segment(kind="bulk", label="c", text="CCC"),
        Segment(kind="task", label="d", text="DDD"),
    ]
    prefix = stable_prefix(segments)
    full = render(segments)
    assert prefix == "AAA\n\nBBB"
    assert full.startswith(prefix)


def test_stable_prefix_byte_identical_across_two_builds_with_different_bulk_task() -> None:
    """Two batches with the SAME stable content but different bulk/task must
    produce a byte-identical stable_prefix (what a provider prompt cache keys on),
    even though the bulk/task content differs between builds."""

    def build(bulk_text: str, task_text: str) -> list[Segment]:
        return [
            Segment(kind="stable", label="sys", text="system: fixed instructions"),
            Segment(kind="stable", label="map", text="repo map: A B C"),
            Segment(kind="bulk", label="ctx", text=bulk_text),
            Segment(kind="task", label="task", text=task_text),
        ]

    first = build("bulk-variant-1", "fix bug #1")
    second = build("totally-different-bulk-content", "fix bug #2, unrelated task")

    assert stable_prefix(first) == stable_prefix(second)
    assert stable_prefix(first) == "system: fixed instructions\n\nrepo map: A B C"


def test_render_is_deterministic_no_timestamps_or_randomness() -> None:
    segments = [
        Segment(kind="stable", label="sys", text="fixed"),
        Segment(kind="task", label="task", text="do X"),
    ]
    assert render(segments) == render(segments)
