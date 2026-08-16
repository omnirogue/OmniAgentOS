"""Tests for the operator CLI (``python -m omniagentos.prompts``).

The CLI is what ``system-prompts/README.md`` tells the operator to run, so the
commands in that document are asserted here: if a documented command stops
working, this fails rather than the operator discovering it.
"""

from __future__ import annotations

import pytest

from omniagentos.prompts.__main__ import main
from omniagentos.prompts.registry import repo_root


def test_check_reports_ok(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["check"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("OK — ")
    assert "role(s)" in out


def test_list_shows_every_role(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["list"]) == 0
    out = capsys.readouterr().out
    assert "job.implementer" in out
    assert "agent.opus-critic" in out
    assert "example.hello" in out


def test_list_live_hides_non_live_roles(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["list", "--live"]) == 0
    out = capsys.readouterr().out
    assert "job.implementer" in out
    assert "example.hello" not in out, "the worked example is not live"


def test_list_filters_by_kind(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["list", "--kind", "daemon"]) == 0
    out = capsys.readouterr().out
    assert "daemon.hygiene" in out
    assert "job.implementer" not in out


def test_list_rejects_an_unknown_kind() -> None:
    with pytest.raises(SystemExit):  # argparse choices
        main(["list", "--kind", "wizard"])


def test_show_prints_the_prompt_body(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["show", "job.implementer"]) == 0
    out = capsys.readouterr().out
    expected = (repo_root() / "vault" / "prompts" / "roles" / "implementer.md").read_text(
        encoding="utf-8"
    )
    assert out == expected, "the CLI must print the live file verbatim"


def test_show_meta_prints_provenance(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["show", "job.implementer", "--meta"]) == 0
    out = capsys.readouterr().out
    assert "owner:" in out
    assert "location:    repo" in out
    assert "consumer:" in out


def test_show_refuses_an_external_role_and_names_where_it_lives(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["show", "agent.opus-critic"]) == 1
    captured = capsys.readouterr()
    assert captured.out == "", "no prompt body may be printed for a by-reference role"
    assert "UnresolvablePromptError" in captured.err
    assert "~/.claude/agents/opus-critic.md" in captured.err


def test_show_refuses_an_embedded_role_and_names_the_constant(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["show", "dispatch.classifier"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "omniagentos/dispatch/gate.py::_LLM_PROMPT" in captured.err


def test_show_unknown_role_exits_nonzero_with_a_suggestion(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["show", "job.implementor"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "UnknownRoleError" in captured.err
    assert "job.implementer" in captured.err


def test_no_subcommand_is_an_error() -> None:
    with pytest.raises(SystemExit):
        main([])
