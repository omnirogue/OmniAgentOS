from __future__ import annotations


def render_template(template: str, context: dict[str, object]) -> str:
    """Run the complete pipeline: scan -> build_tree -> render."""
    raise NotImplementedError("Not implemented")


def safe_render(template: str, context: dict[str, object]) -> str:
    """Run the pipeline but catch TemplateError, ParseError, or RenderError
    and return '<error: {message}>' using the exception's own message.
    """
    raise NotImplementedError("Not implemented")
