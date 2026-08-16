"""Versioned prompts used by the knowledge extraction pipeline."""

from importlib.resources import files

_EXTRACT_PROMPTS = {"v1": "extract-v1.md"}


def get_extraction_prompt(content: str, version: str = "v1") -> str:
    """Load pinned extraction prompt by version."""
    try:
        filename = _EXTRACT_PROMPTS[version]
    except KeyError as exc:
        raise ValueError(f"unknown extraction prompt version: {version}") from exc
    template = files(__package__).joinpath(filename).read_text(encoding="utf-8")
    # The JSON example intentionally contains literal braces, so only the explicit
    # content placeholder is interpolated rather than applying ``str.format`` to it.
    return template.replace("{content}", content)
