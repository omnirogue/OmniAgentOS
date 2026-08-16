"""Pins Lane A: on `import jsonschema` failure, all three tools name a
conforming interpreter (or say plainly that none was found) instead of just
failing, and none of them change their exit code or hardcode a project path
to do it.

Before this file, the documented invocation (`python3 bridge/file_proposal.py
...` / `python3 bridge/validate_envelope.py ...`) failed 100% of the time on
this host because no system interpreter has `jsonschema` installed, and the
failure message named no remedy — an agent following CONTRACT.md verbatim had
no way to get unstuck. `bridge/validate_envelope.py`'s own `main()` also
imported `jsonschema` BEFORE the guarded `_build_validator()` call, so the
tool crashed with an uncaught `ModuleNotFoundError` (exit 1) instead of its
documented exit 2 — that ordering bug is pinned here too.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parent.parent
BRIDGE = PKG / "bridge"

# BOTH interpreters come from shared session fixtures in conftest.py, and
# NEITHER is a module-level mark:
#
#   * `bare_interpreter` must BOTH lack jsonschema AND be >= 3.11, or the tools
#     die on `from datetime import UTC` before the remedy this file pins —
#     exactly what happened on the twin host (system python 3.9) and falsely
#     rejected every remote-gated train on 2026-08-10.
#   * `conforming_interpreter` is this suite's own interpreter, proven to have
#     jsonschema. Its absence FAILS the tests that need it (a suite running
#     without a locked dependency is a broken environment, not an excused one),
#     it does not skip them.
#
# The prerequisites are attached per test rather than to the module because
# TestNoHardcodedVenvPath only reads source text: it needs no interpreter pair,
# and a module-level mark silently took those three cases off the board on any
# host where the pair was unavailable — the exact class of unrun-but-green the
# fixtures above exist to end.


def _run(interpreter: str, script: Path, args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [interpreter, str(script), *args],
        capture_output=True, text=True, env=env, cwd=str(PKG),
    )


@pytest.fixture
def draft(tmp_path: Path) -> Path:
    p = tmp_path / "draft.json"
    p.write_text(json.dumps({
        "contract": "v1.1", "kind": "proposal", "title": "t",
        "created_at": "2026-08-08T00:00:00Z",
        "producer": {"role": "planner", "actor": "x", "lineage": "anthropic"},
        "paths": ["foo.py"],
        "payload": {},
    }))
    return p


@pytest.fixture
def env_with_workdir(monkeypatch, tmp_path: Path, conforming_interpreter: str) -> dict[str, str]:
    import os
    env = dict(os.environ)
    env.pop("VIRTUAL_ENV", None)
    workdir = tmp_path / "loop-workdir"
    interpreter = workdir / ".venv" / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    # A wrapper script, not a symlink: a bare symlink into an empty dir makes
    # Python fall back to the RESOLVED binary's own base environment (no
    # pyvenv.cfg beside the symlink), so whether jsonschema resolves depended
    # on the HOST's base install — true locally by pyenv accident, false on
    # the twin. Exec-ing the conforming interpreter via its own path keeps its
    # real venv context on every host.
    interpreter.write_text(f"#!/bin/sh\nexec {conforming_interpreter!r} \"$@\"\n")
    interpreter.chmod(0o755)
    env["LOOP_WORKDIR"] = str(workdir)
    return env


class TestImportErrorNamesRemedy:
    def test_validate_envelope_names_a_conforming_interpreter(self, draft, env_with_workdir, bare_interpreter):
        result = _run(bare_interpreter, BRIDGE / "validate_envelope.py", [str(draft)], env_with_workdir)
        assert result.returncode == 2, result.stderr
        assert "jsonschema is not importable" in result.stderr
        named = [tok for tok in result.stderr.replace(":", " ").split() if Path(tok).exists()]
        assert named, f"no existing path named in remedy: {result.stderr!r}"
        check = subprocess.run([named[0], "-c", "import jsonschema"], capture_output=True)
        assert check.returncode == 0, f"named interpreter {named[0]!r} does not have jsonschema"

    def test_file_proposal_check_names_a_conforming_interpreter(self, draft, env_with_workdir, bare_interpreter):
        result = _run(bare_interpreter, BRIDGE / "file_proposal.py", ["--check", str(draft)], env_with_workdir)
        # Ruling #4 (2026-08-09): jsonschema-missing is COULD NOT RUN = exit 2,
        # now identical to validate_envelope.py (was exit 3 under the old scheme).
        assert result.returncode == 2, result.stderr
        assert "jsonschema is not importable" in result.stderr
        named = [tok for tok in result.stderr.replace(":", " ").split() if Path(tok).exists()]
        assert named, f"no existing path named in remedy: {result.stderr!r}"
        check = subprocess.run([named[0], "-c", "import jsonschema"], capture_output=True)
        assert check.returncode == 0, f"named interpreter {named[0]!r} does not have jsonschema"

    def test_integration_schema_validate_names_a_conforming_interpreter(self, env_with_workdir, bare_interpreter):
        code = (
            "import sys; sys.path.insert(0, '.')\n"
            "from pathlib import Path\n"
            "from bridge.integration import _schema_validate\n"
            "status, msg = _schema_validate({}, Path('schema'))\n"
            "print(status); print(msg)\n"
        )
        result = subprocess.run([bare_interpreter, "-c", code], capture_output=True, text=True,
                                env=env_with_workdir, cwd=str(PKG))
        assert result.returncode == 0, result.stderr
        lines = result.stdout.strip().splitlines()
        assert lines[0] == "unavailable"
        msg = lines[1]
        named = [tok for tok in msg.replace(":", " ").split() if Path(tok).exists()]
        assert named, f"no existing path named in remedy: {msg!r}"
        check = subprocess.run([named[0], "-c", "import jsonschema"], capture_output=True)
        assert check.returncode == 0, f"named interpreter {named[0]!r} does not have jsonschema"

    def test_no_remedy_when_nothing_probed(self, draft, bare_interpreter):
        """With $VIRTUAL_ENV and $LOOP_WORKDIR both unset, the tool must say so
        plainly rather than silently naming a wrong or missing path."""
        import os
        env = dict(os.environ)
        env.pop("VIRTUAL_ENV", None)
        env.pop("LOOP_WORKDIR", None)
        result = _run(bare_interpreter, BRIDGE / "validate_envelope.py", [str(draft)], env)
        assert result.returncode == 2
        assert "VIRTUAL_ENV" in result.stderr and "LOOP_WORKDIR" in result.stderr


class TestExitCodesUnchanged:
    def test_validate_envelope_exit_code_still_2(self, draft, env_with_workdir, bare_interpreter):
        result = _run(bare_interpreter, BRIDGE / "validate_envelope.py", [str(draft)], env_with_workdir)
        assert result.returncode == 2

    def test_file_proposal_exit_code_couldnotrun_is_2(self, draft, env_with_workdir, bare_interpreter):
        result = _run(bare_interpreter, BRIDGE / "file_proposal.py", ["--check", str(draft)], env_with_workdir)
        # Ruling #4: could-not-run (jsonschema missing) is exit 2, not 3.
        assert result.returncode == 2


class TestNoHardcodedVenvPath:
    @pytest.mark.parametrize("relpath", [
        "bridge/file_proposal.py",
        "bridge/validate_envelope.py",
        "bridge/integration.py",
    ])
    def test_no_hardcoded_absolute_or_project_path(self, relpath):
        text = (PKG / relpath).read_text()
        assert "/Users/" not in text, f"{relpath} hardcodes an absolute /Users/ path"
        assert "OmniAgentOS/.venv" not in text, (
            f"{relpath} hardcodes a sibling project's venv path — ThreeLoops must "
            "discover an interpreter via $VIRTUAL_ENV / $LOOP_WORKDIR, not assume one repo"
        )
