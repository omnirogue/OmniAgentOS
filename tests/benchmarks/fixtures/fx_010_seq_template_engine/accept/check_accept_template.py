"""FROZEN acceptance check for fx_010_seq_template_engine

This file is copied into the agent's workspace AFTER they have finished.
Do not modify or weaken this test suite.
"""

from __future__ import annotations

import os

from engine import render_template, safe_render
from tmpl_parse import Block, ParseError, build_tree
from tmpl_render import RenderError, render
from tmpl_scan import Node, TemplateError, scan


def test_scan_basic():
    # Literal text only
    nodes = scan("Hello world { not tag")
    assert nodes == [Node("text", "Hello world { not tag")]

    # Single tag and spacing
    nodes = scan("{{ name }}")
    assert nodes == [Node("var", "name")]

    # Multi tag with adjacent layout
    nodes = scan("{{x}}{{y}}")
    assert nodes == [Node("var", "x"), Node("var", "y")]

    # If, else, end tags
    nodes = scan("{% if show %}{{ val }}{% else %}{% end %}")
    assert nodes == [Node("if", "show"), Node("var", "val"), Node("else", ""), Node("end", "")]


def test_scan_errors():
    # Unterminated var tag
    try:
        scan("Hello {{ world")
        raise AssertionError("Should raise TemplateError")
    except TemplateError as e:
        assert "6" in str(e)  # starts at index 6

    # Unterminated control tag
    try:
        scan("Hello {% if x")
        raise AssertionError("Should raise TemplateError")
    except TemplateError as e:
        assert "6" in str(e)  # starts at index 6

    # Invalid variable name
    try:
        scan("{{ a-b }}")
        raise AssertionError("Should raise TemplateError")
    except TemplateError as e:
        assert "0" in str(e)

    # Invalid control tag format
    try:
        scan("{% if %}")
        raise AssertionError("Should raise TemplateError")
    except TemplateError as e:
        assert "0" in str(e)

    try:
        scan("{% invalid_kw %}")
        raise AssertionError("Should raise TemplateError")
    except TemplateError as e:
        assert "0" in str(e)


def test_parse_basic():
    nodes = [Node("text", "Hi "), Node("var", "name"), Node("text", ".")]
    tree = build_tree(nodes)
    assert tree.kind == "root"
    assert len(tree.body) == 3
    assert tree.body[0] == Block("text", "Hi ", (), ())
    assert tree.body[1] == Block("var", "name", (), ())
    assert tree.body[2] == Block("text", ".", (), ())


def test_parse_nesting():
    nodes = [
        Node("if", "cond"),
        Node("text", "true_part"),
        Node("else", ""),
        Node("text", "false_part"),
        Node("end", ""),
    ]
    tree = build_tree(nodes)
    assert tree.kind == "root"
    assert len(tree.body) == 1
    if_block = tree.body[0]
    assert if_block.kind == "if"
    assert if_block.value == "cond"
    assert if_block.body == (Block("text", "true_part", (), ()),)
    assert if_block.orelse == (Block("text", "false_part", (), ()),)


def test_parse_errors():
    # Else with no open if
    try:
        build_tree([Node("else", "")])
        raise AssertionError("Should raise ParseError")
    except ParseError as e:
        assert isinstance(e, ParseError)

    # End with no open if
    try:
        build_tree([Node("end", "")])
        raise AssertionError("Should raise ParseError")
    except ParseError as e:
        assert isinstance(e, ParseError)

    # Multiple else tags
    try:
        build_tree([Node("if", "c"), Node("else", ""), Node("else", ""), Node("end", "")])
        raise AssertionError("Should raise ParseError")
    except ParseError as e:
        assert isinstance(e, ParseError)

    # Unclosed if
    try:
        build_tree([Node("if", "c"), Node("text", "inside")])
        raise AssertionError("Should raise ParseError")
    except ParseError as e:
        assert isinstance(e, ParseError)


def test_render_basic():
    # Simple variables and text
    tree = Block(
        "root",
        "",
        (
            Block("text", "Hello, ", (), ()),
            Block("var", "user", (), ()),
            Block("text", "!", (), ()),
        ),
        (),
    )
    context = {"user": "Alice"}
    result = render(tree, context)
    assert result == "Hello, Alice!"


def test_render_missing_variable():
    tree = Block("root", "", (Block("var", "missing_var", (), ()),), ())
    try:
        render(tree, {})
        raise AssertionError("Should raise RenderError")
    except RenderError as e:
        # Check that RenderError is a KeyError and its message contains the variable name
        assert isinstance(e, KeyError)
        assert "missing_var" in str(e)


def test_render_missing_condition():
    # {% if missing_cond %}true{% else %}false{% end %}
    # If cond is missing, it should evaluate to False rather than raising an error
    tree = Block(
        "root",
        "",
        (
            Block(
                "if",
                "missing_cond",
                (Block("text", "true", (), ()),),
                (Block("text", "false", (), ()),),
            ),
        ),
        (),
    )
    result = render(tree, {})
    assert result == "false"


def test_end_to_end_welcome():
    # Read the welcome template file
    # We resolve path relative to this file's folder, but in the workspace,
    # the templates folder is in the same directory as the module if they run from workspace root.
    # Let's try multiple lookup paths.
    possible_paths = [
        "templates/welcome.tmpl",
        "seed/templates/welcome.tmpl",
        os.path.join(os.path.dirname(__file__), "..", "seed", "templates", "welcome.tmpl"),
    ]
    template_content = ""
    for path in possible_paths:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                template_content = f.read()
            break

    assert template_content, "welcome.tmpl could not be loaded"

    # Render as admin
    ctx_admin = {"username": "Bob", "is_admin": True}
    res_admin = render_template(template_content, ctx_admin)
    assert "Bob" in res_admin
    assert "administrator" in res_admin
    assert "regular user" not in res_admin

    # Render as regular user
    ctx_user = {"username": "Charlie", "is_admin": False}
    res_user = render_template(template_content, ctx_user)
    assert "Charlie" in res_user
    assert "regular user" in res_user
    assert "administrator" not in res_user

    # Render with missing is_admin (should default to False)
    ctx_missing = {"username": "Dave"}
    res_missing = render_template(template_content, ctx_missing)
    assert "Dave" in res_missing
    assert "regular user" in res_missing
    assert "administrator" not in res_missing


def test_safe_render_errors():
    # Scan/TemplateError
    res1 = safe_render("{{ unclosed", {})
    assert res1.startswith("<error:")
    assert res1.endswith(">")
    assert "0" in res1

    # ParseError
    res2 = safe_render("{% else %}", {})
    assert res2.startswith("<error:")
    assert res2.endswith(">")

    # RenderError
    res3 = safe_render("{{ missing_var }}", {})
    assert res3.startswith("<error:")
    assert res3.endswith(">")
    assert "missing_var" in res3


def test_arbitrary_nesting():
    # Deeply nested structures
    template = (
        "{% if outer %}"
        "Outer True. "
        "{% if inner %}"
        "Inner True."
        "{% else %}"
        "Inner False."
        "{% end %}"
        "{% else %}"
        "Outer False."
        "{% end %}"
    )

    # Outer True, Inner True
    assert render_template(template, {"outer": True, "inner": True}) == "Outer True. Inner True."
    # Outer True, Inner False
    assert render_template(template, {"outer": True, "inner": False}) == "Outer True. Inner False."
    # Outer False
    assert render_template(template, {"outer": False}) == "Outer False."
