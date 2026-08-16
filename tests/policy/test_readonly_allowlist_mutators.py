"""Mechanical gates: sips_out_refused, readonly_allowlist_no_mutators."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from omniagentos.contracts import ActionClass
from omniagentos.policy import shell as shell_policy
from omniagentos.policy.shell import _READ_ONLY_SIMPLE, classify_shell

PROJECT = "/tmp/policy-proj"


def test_readonly_allowlist_no_unconditional_sips() -> None:
    # Gate: readonly_allowlist_no_mutators / counterfeit readonly-mutator-allowlisted
    assert "sips" not in _READ_ONLY_SIMPLE
    assert "identify" not in _READ_ONLY_SIMPLE
    source = Path(shell_policy.__file__).read_text(encoding="utf-8")
    # Pin the frozenset literal does not list sips as simple member via AST
    module = ast.parse(source)
    found = None
    for node in module.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "_READ_ONLY_SIMPLE":
                    found = node.value
    assert found is not None
    # evaluate constants in the frozenset call
    simple_src = ast.get_source_segment(source, found) or ""
    assert '"sips"' not in simple_src and "'sips'" not in simple_src
    assert '"identify"' not in simple_src and "'identify'" not in simple_src


@pytest.mark.parametrize(
    "command",
    [
        "sips --out out.png foo.png",
        "sips -o out.png foo.png",
        "sips -s format jpeg foo.png --out out.jpg",
        "sips -z 100 100 foo.png",
        "sips -c 100 100 foo.png",
        "sips -r 90 foo.png",
        "sips -f horizontal foo.png",
        "sips -x profile.icc foo.png",
        "sips -X desc tag.bin profile.icc",
        "sips -e profile.icc foo.png",
        "sips -m profile.icc foo.png",
        "sips --unknown-query foo.png",
    ],
)
def test_sips_out_refused(command: str) -> None:
    # Gate: sips_out_refused / counterfeit sips-out-autoapproved
    assert classify_shell(command, PROJECT) is not ActionClass.READ_ONLY
    assert classify_shell(command, PROJECT) is ActionClass.IRREVERSIBLE


@pytest.mark.parametrize(
    "command",
    [
        "sips -g all foo.png",
        "sips -g pixelWidth foo.png",
        "sips --getProperty all foo.png",
        "sips --getProperty=pixelWidth foo.png",
        "sips -g all -1 foo.png",
        "sips -1 foo.png",
        "sips --verify profile.icc",
    ],
)
def test_sips_query_still_read_only(command: str) -> None:
    assert classify_shell(command, PROJECT) is ActionClass.READ_ONLY


def test_identify_write_not_read_only() -> None:
    assert classify_shell("identify -write out.png in.png", PROJECT) is not ActionClass.READ_ONLY
    assert classify_shell("identify +write out.png in.png", PROJECT) is not ActionClass.READ_ONLY
    assert (
        classify_shell("identify -write-mask mask.png in.png", PROJECT)
        is not ActionClass.READ_ONLY
    )
    assert classify_shell("identify in.png", PROJECT) is ActionClass.READ_ONLY
    assert classify_shell("identify -verbose in.png", PROJECT) is ActionClass.READ_ONLY
    assert (
        classify_shell("identify -format '%w x %h' in.png", PROJECT)
        is ActionClass.READ_ONLY
    )


def test_file_compile_still_irreversible() -> None:
    assert classify_shell("file foo.png", PROJECT) is ActionClass.READ_ONLY
    assert classify_shell("file -C -m magic", PROJECT) is ActionClass.IRREVERSIBLE
    assert classify_shell("file --compile -m magic", PROJECT) is ActionClass.IRREVERSIBLE
