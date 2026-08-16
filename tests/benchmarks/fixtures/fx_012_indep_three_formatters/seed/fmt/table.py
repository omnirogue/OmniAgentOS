"""
Fixed-width text table renderer.
"""

from __future__ import annotations


def render_table(headers: list[str], rows: list[list[str]], align: str = "") -> str:
    """
    Renders a fixed-width text table where column width is the longest cell in that column.
    """
    raise NotImplementedError("TODO: implement render_table")
