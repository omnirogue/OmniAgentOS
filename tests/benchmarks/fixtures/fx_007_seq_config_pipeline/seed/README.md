# Sequence Config Pipeline

This workspace contains a three-stage sequence configuration pipeline:
1. `iniparse.py`: Parses raw INI format text into a structured dictionary.
2. `resolve.py`: Resolves textual `${section.key}` references dynamically.
3. `cli.py`: A CLI interface to coordinate parsing, resolution, and formatted output.

## Sample Config Format
Configurations are written in a standard INI format. Sections are defined via `[section_name]` and key-value pairs are defined via `key = value`. Lines starting with `#` or `;` are ignored as comments.

Placeholders are defined as `${section_name.key_name}` and can be used within values.
