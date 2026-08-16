"""
Fixed-width text table renderer.
"""

from __future__ import annotations


def render_table(headers: list[str], rows: list[list[str]], align: str = "") -> str:
    """
    Renders a fixed-width text table where column width is the longest cell in that column.
    """
    if not headers:
        raise ValueError("Headers cannot be empty")

    n = len(headers)

    # Check all rows have the same length as headers
    for row in rows:
        if len(row) != n:
            raise ValueError("Row length does not match header count")

    # Parse align
    if align == "":
        align = "l" * n
    elif len(align) != n or any(c not in ("l", "r") for c in align):
        raise ValueError("Invalid align string")

    # Calculate column widths
    widths = []
    for i in range(n):
        max_w = len(headers[i])
        for row in rows:
            max_w = max(max_w, len(row[i]))
        widths.append(max_w)

    # Helper to pad a cell
    def format_cell(val: str, width: int, alignment: str) -> str:
        if alignment == "l":
            return val.ljust(width)
        else:
            return val.rjust(width)

    # Render header
    header_cells = [format_cell(headers[i], widths[i], align[i]) for i in range(n)]
    header_line = " | ".join(header_cells).rstrip()

    # Render rows
    rendered_rows = []
    for row in rows:
        row_cells = [format_cell(row[i], widths[i], align[i]) for i in range(n)]
        rendered_rows.append(" | ".join(row_cells).rstrip())

    # Calculate separator width: the maximum length of any rendered line (header or rows)
    all_lines = [header_line] + rendered_rows
    sep_width = max(len(line) for line in all_lines)
    separator = "-" * sep_width

    # Assemble the table
    table_lines = [header_line, separator] + rendered_rows
    return "\n".join(table_lines)
