"""FROZEN acceptance check for fx_007_seq_config_pipeline

This file is copied in after the agent finishes, so the agent cannot weaken it.
"""

from __future__ import annotations

import contextlib
import io
import os
import tempfile

from cli import main
from iniparse import IniError, parse_ini
from resolve import CircularReference, MissingReference, resolve


def test_parse_ini_basic():
    text = """
    # This is a comment
    ; This is also a comment

    [database]
    host = localhost
    port = 5432

    [api]
    url = http://${database.host}:${database.port}/v1
    timeout = 30

    [empty]

    [database]
    # Reopening database to overwrite/add keys
    # duplicate key, last wins
    user = admin
    port = 5433
    """
    res = parse_ini(text)
    assert res == {
        "database": {
            "host": "localhost",
            "port": "5433",
            "user": "admin",
        },
        "api": {
            "url": "http://${database.host}:${database.port}/v1",
            "timeout": "30",
        },
        "empty": {},
    }


def test_parse_ini_inline_comments():
    text = """
    [sec]
    ; This is a full-line comment that should be ignored
    motd = hello # world
    semi = foo ; bar
    """
    res = parse_ini(text)
    assert res == {
        "sec": {
            "motd": "hello # world",
            "semi": "foo ; bar",
        }
    }


def test_parse_ini_errors():
    # Key-value before any section header
    try:
        parse_ini("key = value")
        raise AssertionError("Should have raised IniError")
    except IniError as e:
        assert "Line 1" in str(e) or "1" in str(e)

    # Non-blank, non-comment, non-header line with no '='
    bad_text = """[sec]
    key = val
    invalid_line_here
    """
    try:
        parse_ini(bad_text)
        raise AssertionError("Should have raised IniError")
    except IniError as e:
        assert "Line 3" in str(e) or "3" in str(e)


def test_resolve_basic():
    config = {
        "database": {
            "host": "localhost",
            "port": "5432",
        },
        "api": {
            "url": "http://${database.host}:${database.port}/v1",
            "timeout": "30",
        },
    }
    res = resolve(config)
    assert res == {
        "database": {
            "host": "localhost",
            "port": "5432",
        },
        "api": {
            "url": "http://localhost:5432/v1",
            "timeout": "30",
        },
    }


def test_resolve_literal_dollar():
    config = {
        "prices": {
            "item1": "$100",
            "item2": "${prices.item1} is the cost",
        }
    }
    res = resolve(config)
    assert res == {
        "prices": {
            "item1": "$100",
            "item2": "$100 is the cost",
        }
    }


def test_resolve_cycles():
    config = {
        "sec": {
            "a": "${sec.b}",
            "b": "${sec.a}",
        }
    }
    try:
        resolve(config)
        raise AssertionError("Should have raised CircularReference")
    except CircularReference as e:
        msg = str(e)
        assert "sec.a" in msg or "sec.b" in msg


def test_resolve_missing():
    # Referencing non-existent section or key
    config = {
        "sec": {
            "a": "${other_sec.b}",
        }
    }
    try:
        resolve(config)
        raise AssertionError("Should have raised MissingReference")
    except MissingReference:
        pass


def test_resolve_no_mutation():
    config = {
        "sec": {
            "a": "val",
            "b": "${sec.a}",
        }
    }
    import copy

    config_copy = copy.deepcopy(config)
    resolve(config)
    assert config == config_copy


def test_cli_main_success():
    text = """[app]
    env = production
    title = My App (${app.env})
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        ini_path = os.path.join(tmpdir, "config.ini")
        with open(ini_path, "w", encoding="utf-8") as f:
            f.write(text)

        f_out = io.StringIO()
        with contextlib.redirect_stdout(f_out):
            ret = main([ini_path])

        assert ret == 0
        output_lines = f_out.getvalue().strip().splitlines()
        assert output_lines == [
            "app.env=production",
            "app.title=My App (production)",
        ]


def test_cli_main_missing_arg():
    f_out = io.StringIO()
    with contextlib.redirect_stdout(f_out):
        ret = main([])
    assert ret == 2
    assert "usage: cli.py <config-path>" in f_out.getvalue()


def test_cli_main_missing_file():
    f_out = io.StringIO()
    with contextlib.redirect_stdout(f_out):
        ret = main(["non_existent_file_path.ini"])
    assert ret == 2
    assert "error: cannot read non_existent_file_path.ini" in f_out.getvalue()


def test_cli_main_error_propagation():
    # Ini parsing error
    text_bad_ini = """
    key = val
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        ini_path = os.path.join(tmpdir, "bad.ini")
        with open(ini_path, "w", encoding="utf-8") as f:
            f.write(text_bad_ini)

        f_out = io.StringIO()
        with contextlib.redirect_stdout(f_out):
            ret = main([ini_path])
        assert ret == 1
        assert "error:" in f_out.getvalue()

    # Circular ref error
    text_circular = """[sec]
    a = ${sec.b}
    b = ${sec.a}
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        ini_path = os.path.join(tmpdir, "circular.ini")
        with open(ini_path, "w", encoding="utf-8") as f:
            f.write(text_circular)

        f_out = io.StringIO()
        with contextlib.redirect_stdout(f_out):
            ret = main([ini_path])
        assert ret == 1
        assert "error:" in f_out.getvalue()
