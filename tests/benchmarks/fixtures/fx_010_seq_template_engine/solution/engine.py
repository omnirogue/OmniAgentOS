from __future__ import annotations

from tmpl_parse import ParseError, build_tree
from tmpl_render import RenderError, render
from tmpl_scan import TemplateError, scan


def render_template(template: str, context: dict[str, object]) -> str:
    """Run the complete pipeline: scan -> build_tree -> render."""
    nodes = scan(template)
    tree = build_tree(nodes)
    return render(tree, context)


def safe_render(template: str, context: dict[str, object]) -> str:
    """Run the pipeline but catch TemplateError, ParseError, or RenderError
    and return '<error: {message}>' using the exception's own message.
    """
    try:
        return render_template(template, context)
    except (TemplateError, ParseError, RenderError) as e:
        return f"<error: {str(e)}>"
