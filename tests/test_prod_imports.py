"""H5 guard: the ASGI app must import with only [project].dependencies installed.

`omniagentos.api.main` transitively imports `httpx` (via
`omniagentos.api.routes.voice` -> `omniagentos.voice.service` ->
`omniagentos.voice.{elevenlabs,xai}`, and via
`omniagentos.api.routes.briefings` -> `omniagentos.briefing.run` ->
`omniagentos.steward.notify`). httpx used to live only in
`[dependency-groups].dev`, so a `uv run --no-dev ...` production install (or any
install that skips the dev group) would raise `ModuleNotFoundError: httpx` the
moment the API process started. This test is a regular pytest run (which does
have the dev group installed) and so does not itself prove the `--no-dev` case —
it is a smoke guard that `omniagentos.api.main` keeps importing cleanly. The real
`--no-dev` proof is a separate, explicit command documented in the RB-deploy
session's validation notes:

    uv run --no-dev python -c "import omniagentos.api.main"

which only succeeds now that httpx is a `[project].dependencies` entry (H5 fix
in pyproject.toml). If httpx were ever moved back to dev-only, that command
would fail with ModuleNotFoundError while this in-process test would still pass
(the dev group is present during `pytest`) — so treat the `--no-dev` command as
the authoritative guard and this test as a fast regression tripwire for the
import graph itself.
"""

from __future__ import annotations


def test_api_main_imports() -> None:
    import omniagentos.api.main as main_module

    assert main_module.app is not None
