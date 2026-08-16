# Table Renderer worked examples

This document details byte-exact outputs for `render_table` to clarify padding, joining, alignment, and trailing whitespace removal.

## Example 1: Right-aligned columns

**Inputs:**
* `headers`: `["Name", "Age", "City"]`
* `rows`: `[["Alice", "24", "New York"], ["Bob", "300", "Paris"]]`
* `align`: `"lrr"` (Left alignment for Name, Right alignment for Age and City)

**Calculation:**
1. Column widths:
   * Column 0: `max(len("Name"), len("Alice"), len("Bob")) = 5`
   * Column 1: `max(len("Age"), len("24"), len("300")) = 3`
   * Column 2: `max(len("City"), len("New York"), len("Paris")) = 8`
2. Padded cells:
   * Header: `["Name ", "Age", "    City"]`
   * Row 1: `["Alice", " 24", "New York"]`
   * Row 2: `["Bob  ", "300", "   Paris"]`
3. Joined lines (joined with `" | "`):
   * Header: `"Name  | Age |     City"` (length 22)
   * Row 1: `"Alice |  24 | New York"` (length 22)
   * Row 2: `"Bob   | 300 |    Paris"` (length 22)
4. Longest line is 22 characters, so separator is 22 `-` characters.

**Output (22 characters wide per line, no trailing spaces):**
```
Name  | Age |     City
----------------------
Alice |  24 | New York
Bob   | 300 |    Paris
```

---

## Example 2: Left-aligned columns with trailing whitespace stripping

**Inputs:**
* `headers`: `["Name", "Age", "City"]`
* `rows`: `[["Alice", "24", "New York"], ["Bob", "300", "Paris"]]`
* `align`: `"lll"` (or `""` which defaults to `"lll"`)

**Calculation:**
1. Column widths: `[5, 3, 8]`
2. Padded cells:
   * Header: `["Name ", "Age", "City    "]`
   * Row 1: `["Alice", "24 ", "New York"]`
   * Row 2: `["Bob  ", "300", "Paris   "]`
3. Joined and stripped lines:
   * Header unstripped: `"Name  | Age | City    "` (length 22) -> Stripped: `"Name  | Age | City"` (length 18)
   * Row 1 unstripped: `"Alice | 24  | New York"` (length 22) -> Stripped: `"Alice | 24  | New York"` (length 22)
   * Row 2 unstripped: `"Bob   | 300 | Paris   "` (length 22) -> Stripped: `"Bob   | 300 | Paris"` (length 19)
4. Longest rendered line (after stripping) is 22 characters, so separator is 22 `-` characters.

**Output (lines have different lengths after stripping, max is 22):**
```
Name  | Age | City
----------------------
Alice | 24  | New York
Bob   | 300 | Paris
```
