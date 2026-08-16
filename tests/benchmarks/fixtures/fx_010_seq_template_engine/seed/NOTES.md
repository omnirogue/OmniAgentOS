# Sequential Template Engine Syntax Notes

This template engine processes templates using a strictly-sequential multi-stage pipeline:
1. Scanning: Breaks template strings into lexical `Node` tokens.
2. Parsing: Transforms flat list of nodes into a nested tree structured as `Block` blocks.
3. Rendering: Evaluates the block tree within a context dictionary to produce a rendered string.

## Tag Reference

### Variables
Syntax: `{{ variable_name }}`
Renders the string representation of `context['variable_name']`. If the variable is missing from the context, a `RenderError` is raised.

### Conditionals
Syntax:
```
{% if condition_name %}
  ... true content ...
{% else %}
  ... false content ...
{% end %}
```
Note that `{% else %}` is optional. A missing condition name evaluates to `False` (does not raise an error).

## Syntax Constraints
- Tag names must match `[A-Za-z0-9_]+`.
- Spaces around names within tags are stripped.
- Any syntax or parsing errors raise specialized exception classes with clear messages.
