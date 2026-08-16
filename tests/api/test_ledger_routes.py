"""GET /api/ledger/claims and GET /api/ledger/tail -- the session-ledger relay
feeding the board header's open-claims strip and the card drawer's event
timeline (session-ledger integration brief, 2026-08-04).

Contract authority: ``~/.omniagentos/ops/session-ledger/PLAN.md`` + ``EVENTS.md``.
These tests pin the brief's hard rules mechanically rather than by claim:
argv-built subprocess calls (never ``shell=True``, never a string built from
a query param), ``--owner`` never reaching the CLI, a 503 (never a bare 500 or
an empty 200) on every CLI failure mode, and the app-level session-token
gate (S1). Extended per the 2026-08-04 pre-merge review: full subprocess
failure taxonomy, ref validation over the whole string (not just the
value), a claims-specific timeout, the owner-held-claim decision, and a
scratch-root E2E that never touches the real estate ledger.
"""

from __future__ import annotations

import ast
import asyncio
import json
import logging
import os
import subprocess
from pathlib import Path

import httpx
import pytest

from omniagentos.api.routes import session_ledger
from omniagentos.sessions import token

# A real board task id, read from the LIVE control plane (GET /api/board,
# 127.0.0.1:8485, current main) on 2026-08-04 while building this lane --
# pinned as a constant rather than fetched live on every test run so this
# suite stays fast and hermetic. Any real board task id round-trips the same
# way; this one just proves the drawer's exact query shape against a real
# card rather than a fabricated string.
_REAL_BOARD_TASK_ID = "btk_de7776a180774a038cfb"


def _completed(args: list[str], returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=args, returncode=returncode, stdout=stdout, stderr=stderr)


# --- relay behaviour (subprocess mocked) ----------------------------------


def test_ledger_claims_relays_cli_rows_and_passes_no_owner_related_flag_at_all(
    asgi_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pre-merge review decision (2026-08-04): claims is invoked with NO
    owner-related flag -- not `--owner`, not `--no-owner`. The CLI's own default
    already shows owner-held surfaces (see the dedicated redaction test
    below); the never-pass---owner HARD RULE is untouched (no owner flag is
    passed at all, so `--owner` is trivially never among them)."""
    calls: list[list[str]] = []
    row = {
        "project": "OmniAgentOS",
        "surface": "repoA",
        "session": "s1",
        "agent": "fable",
        "since": "2026-08-04T10:00:00.000000+00:00",
        "lease_until": None,
        "stale": False,
        "id": "a" * 64,
        "visibility": "project",
    }
    stdout = json.dumps(row) + "\n"

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return _completed(args, 0, stdout, "")

    monkeypatch.setattr(session_ledger.subprocess, "run", fake_run)
    monkeypatch.delenv(session_ledger._LEDGER_ROOT_ENV, raising=False)

    response = asyncio.run(asgi_client.get("/api/ledger/claims"))

    assert response.status_code == 200
    assert response.json() == [row]
    assert len(calls) == 1
    assert calls[0] == [session_ledger._LEDGER_BIN, "claims"]
    assert "--owner" not in calls[0]
    assert "--no-owner" not in calls[0]


def test_ledger_claims_uses_its_own_shorter_timeout(
    asgi_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A locked ledger stalling this app's sync request threadpool at the
    same 10s as `tail` is a starvation vector `claims` does not share (the
    board polls it every 10s) -- its own, shorter constant."""
    seen_timeout: list[object] = []

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen_timeout.append(kwargs.get("timeout"))
        return _completed(args, 0, "", "")

    monkeypatch.setattr(session_ledger.subprocess, "run", fake_run)

    asyncio.run(asgi_client.get("/api/ledger/claims"))

    assert session_ledger._CLAIMS_TIMEOUT_S == 3
    assert seen_timeout == [3]


def test_ledger_tail_keeps_the_10s_timeout(
    asgi_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen_timeout: list[object] = []

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen_timeout.append(kwargs.get("timeout"))
        return _completed(args, 0, "", "")

    monkeypatch.setattr(session_ledger.subprocess, "run", fake_run)

    asyncio.run(asgi_client.get("/api/ledger/tail"))

    assert session_ledger._SUBPROCESS_TIMEOUT_S == 10
    assert seen_timeout == [10]


def test_ledger_claims_shows_a_owner_held_claim_as_held_never_absent(
    asgi_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLI's own default (called with no owner flag at all) shows a
    owner-held surface with only its attached correction TEXT redacted --
    hiding the surface entirely would present it as FREE. This module never
    re-derives or re-redacts anything; it must relay the row verbatim,
    including `visibility: "owner"`, never filtering it back out."""
    row = {
        "project": "Personal",
        "surface": "taxes-2026",
        "session": "human:owner",
        "agent": "human:owner",
        "since": "2026-08-04T10:00:00.000000+00:00",
        "lease_until": None,
        "stale": False,
        "id": "b" * 64,
        "visibility": "owner",
        "_corrections": [
            {
                "id": "c" * 64,
                "summary": "[owner-stream correction — ledger tail --owner]",
                "at": "2026-08-04T11:00:00.000000+00:00",
            }
        ],
    }
    stdout = json.dumps(row) + "\n"

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return _completed(args, 0, stdout, "")

    monkeypatch.setattr(session_ledger.subprocess, "run", fake_run)

    response = asyncio.run(asgi_client.get("/api/ledger/claims"))

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["visibility"] == "owner"
    assert body[0]["project"] == "Personal"
    assert body[0]["surface"] == "taxes-2026"
    assert body[0]["session"] == "human:owner"
    assert body[0]["_corrections"][0]["summary"] == "[owner-stream correction — ledger tail --owner]"


def test_ledger_tail_builds_argv_from_every_filter_and_never_passes_owner(
    asgi_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return _completed(args, 0, "", "")

    monkeypatch.setattr(session_ledger.subprocess, "run", fake_run)
    monkeypatch.delenv(session_ledger._LEDGER_ROOT_ENV, raising=False)

    response = asyncio.run(
        asgi_client.get(
            "/api/ledger/tail",
            params={
                "project": "OmniAgentOS",
                "event": "note",
                "agent": "fable",
                "ref": f"task={_REAL_BOARD_TASK_ID}",
                "n": 50,
            },
        )
    )

    assert response.status_code == 200
    assert response.json() == []
    assert len(calls) == 1
    assert calls[0] == [
        session_ledger._LEDGER_BIN,
        "tail",
        "-n",
        "50",
        "--project",
        "OmniAgentOS",
        "--event",
        "note",
        "--agent",
        "fable",
        "--ref",
        f"task={_REAL_BOARD_TASK_ID}",
    ]
    assert "--owner" not in calls[0]


def test_ledger_tail_defaults_n_and_omits_absent_filters(
    asgi_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return _completed(args, 0, "", "")

    monkeypatch.setattr(session_ledger.subprocess, "run", fake_run)
    monkeypatch.delenv(session_ledger._LEDGER_ROOT_ENV, raising=False)

    response = asyncio.run(asgi_client.get("/api/ledger/tail"))

    assert response.status_code == 200
    assert calls[0] == [session_ledger._LEDGER_BIN, "tail", "-n", "20"]


def test_ledger_tail_parses_multiple_jsonl_rows_newest_last_order_preserved(
    asgi_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLI already emits rows in (at, id) order; the route must not
    reorder or drop the ``_corrections`` the CLI attaches."""
    stdout = (
        '{"id":"' + "1" * 64 + '","at":"2026-08-04T10:00:00.000000+00:00",'
        '"agent":"fable","event":"note","project":"t-smoke","summary":"first"}\n'
        '{"id":"' + "2" * 64 + '","at":"2026-08-04T10:00:01.000000+00:00",'
        '"agent":"fable","event":"note","project":"t-smoke","summary":"second",'
        '"_corrections":[{"id":"' + "3" * 64 + '","summary":"actually...","at":"2026-08-04T10:00:02.000000+00:00"}]}\n'
    )

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return _completed(args, 0, stdout, "")

    monkeypatch.setattr(session_ledger.subprocess, "run", fake_run)

    response = asyncio.run(asgi_client.get("/api/ledger/tail"))

    assert response.status_code == 200
    body = response.json()
    assert [row["id"] for row in body] == ["1" * 64, "2" * 64]
    assert body[1]["_corrections"] == [
        {"id": "3" * 64, "summary": "actually...", "at": "2026-08-04T10:00:02.000000+00:00"}
    ]


# --- root injection (test isolation seam; production default unchanged) ---


def test_ledger_tail_passes_root_flag_when_the_env_var_override_is_set(
    asgi_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[list[str]] = []

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return _completed(args, 0, "", "")

    monkeypatch.setattr(session_ledger.subprocess, "run", fake_run)
    monkeypatch.setenv(session_ledger._LEDGER_ROOT_ENV, str(tmp_path))

    asyncio.run(asgi_client.get("/api/ledger/tail"))

    # --root is a TOP-LEVEL ledger.py option and must precede the
    # subcommand: `ledger --root <dir> tail ...`, never `ledger tail --root
    # <dir> ...` (argparse subparsers do not accept it after the subcommand).
    assert calls[0] == [session_ledger._LEDGER_BIN, "--root", str(tmp_path), "tail", "-n", "20"]


def test_ledger_claims_omits_root_flag_by_default(
    asgi_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return _completed(args, 0, "", "")

    monkeypatch.setattr(session_ledger.subprocess, "run", fake_run)
    monkeypatch.delenv(session_ledger._LEDGER_ROOT_ENV, raising=False)

    asyncio.run(asgi_client.get("/api/ledger/claims"))

    assert calls[0] == [session_ledger._LEDGER_BIN, "claims"]
    assert "--root" not in calls[0]


# --- validation (never reaches argv) --------------------------------------


@pytest.mark.parametrize("n", [0, 201, -1])
def test_ledger_tail_rejects_n_outside_bounds(asgi_client: httpx.AsyncClient, n: int) -> None:
    response = asyncio.run(asgi_client.get("/api/ledger/tail", params={"n": n}))
    assert response.status_code == 422


@pytest.mark.parametrize("field", ["project", "event", "agent"])
@pytest.mark.parametrize("value", ["; rm -rf ~", "a b", "a\nb", "$(whoami)", ""])
def test_ledger_tail_rejects_bad_filter_charset(
    asgi_client: httpx.AsyncClient, field: str, value: str
) -> None:
    response = asyncio.run(asgi_client.get("/api/ledger/tail", params={field: value}))
    assert response.status_code == 422


@pytest.mark.parametrize(
    "ref",
    [
        "no-equals-sign",
        "=novalue",
        "bad key=value",
        "task=" + "x" * 300,
        "task=\nnewline",
        # A trailing newline slips past a bare `.match()` against a
        # `^...$`-anchored pattern: `$` matches just before a trailing
        # newline, not strictly end-of-string. `task\n` alone would have
        # passed the OLD key check. Now scanned as a whole-string control
        # character AND checked with `fullmatch` (defense in depth).
        "task\n=x",
        # DEL (0x7f), in the key.
        f"ta{chr(0x7f)}sk=x",
        # A C1 control character (0x9f), in the value.
        f"task=x{chr(0x9f)}",
    ],
)
def test_ledger_tail_rejects_malformed_ref(asgi_client: httpx.AsyncClient, ref: str) -> None:
    response = asyncio.run(asgi_client.get("/api/ledger/tail", params={"ref": ref}))
    assert response.status_code == 422


def test_ledger_tail_never_reaches_subprocess_on_validation_failure(
    asgi_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    called = False

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal called
        called = True
        return _completed(args, 0, "", "")

    monkeypatch.setattr(session_ledger.subprocess, "run", fake_run)

    response = asyncio.run(asgi_client.get("/api/ledger/tail", params={"n": 999}))

    assert response.status_code == 422
    assert called is False


# --- failure handling: every mode is 503, never a bare 500, never 200 -----


def test_ledger_claims_503_on_nonzero_exit_carries_stderr_line(
    asgi_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return _completed(args, 2, "", "refused: root surface\nmore detail\n")

    monkeypatch.setattr(session_ledger.subprocess, "run", fake_run)

    response = asyncio.run(asgi_client.get("/api/ledger/claims"))

    assert response.status_code == 503
    body = response.json()
    assert body["error"]["code"] == "ledger_unavailable"
    assert body["error"]["message"] == "refused: root surface"


def test_ledger_tail_503_on_timeout(asgi_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd=args, timeout=10)

    monkeypatch.setattr(session_ledger.subprocess, "run", fake_run)

    response = asyncio.run(asgi_client.get("/api/ledger/tail"))

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "ledger_unavailable"


def test_ledger_claims_never_returns_an_empty_200_on_cli_refusal(
    asgi_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Silence must not look like an empty ledger: an all-blank stderr on a
    nonzero exit still 503s, never falls through to `[]`/200."""

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return _completed(args, 1, "", "")

    monkeypatch.setattr(session_ledger.subprocess, "run", fake_run)

    response = asyncio.run(asgi_client.get("/api/ledger/claims"))

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "ledger_unavailable"


@pytest.mark.parametrize(
    "exc",
    [
        FileNotFoundError("[Errno 2] No such file or directory: 'ledger'"),
        PermissionError("[Errno 13] Permission denied: 'ledger'"),
        OSError("[Errno 24] Too many open files"),
    ],
    ids=["binary-missing", "permission-denied", "resource-exhaustion"],
)
def test_ledger_tail_503_when_the_subprocess_cannot_even_start(
    asgi_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, exc: OSError
) -> None:
    """FileNotFoundError/PermissionError/OSError are all raised by
    `subprocess.run` itself (before there is any CompletedProcess at all)
    when the binary is missing, unreadable, or the process table/fd table
    is exhausted. Previously uncaught -> a bare 500; the brief's 503 class
    covers this exactly like a timeout or a nonzero exit."""

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise exc

    monkeypatch.setattr(session_ledger.subprocess, "run", fake_run)

    response = asyncio.run(asgi_client.get("/api/ledger/tail"))

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "ledger_unavailable"


def test_ledger_claims_503_when_the_subprocess_cannot_even_start(
    asgi_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("[Errno 2] No such file or directory: 'ledger'")

    monkeypatch.setattr(session_ledger.subprocess, "run", fake_run)

    response = asyncio.run(asgi_client.get("/api/ledger/claims"))

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "ledger_unavailable"


def test_ledger_tail_503_on_unparseable_stdout_despite_exit_zero(
    asgi_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Probe: the CLI exits 0 (success) but a stray diagnostic line ("warn:
    rescan") lands on stdout instead of stderr -- a CLI regression, not an
    empty ledger. json.loads on that line must not become an uncaught
    500; it is exactly the same 503 class as every other CLI failure."""

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return _completed(args, 0, "warn: rescan\n", "")

    monkeypatch.setattr(session_ledger.subprocess, "run", fake_run)

    response = asyncio.run(asgi_client.get("/api/ledger/tail"))

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "ledger_unavailable"


def test_ledger_tail_503_on_unparseable_stdout_mixed_with_valid_rows(
    asgi_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same probe, but AFTER a genuinely valid row -- proving the whole
    response still refuses rather than silently returning a partial/
    truncated row list as if it were the complete tail."""
    stdout = json.dumps({"id": "a" * 64, "summary": "ok"}) + "\nwarn: rescan\n"

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return _completed(args, 0, stdout, "")

    monkeypatch.setattr(session_ledger.subprocess, "run", fake_run)

    response = asyncio.run(asgi_client.get("/api/ledger/tail"))

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "ledger_unavailable"


@pytest.mark.parametrize("bad_line", ["null", "[1,2]"])
def test_ledger_tail_503_on_valid_json_that_is_not_an_object(
    asgi_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, bad_line: str
) -> None:
    """The one 500 left after the JSONDecodeError fix: `json.loads("null")`
    and `json.loads("[1,2]")` both parse CLEANLY (no JSONDecodeError) but
    are not row objects -- every real ledger row is a JSON object. Before
    this check these would have sailed through `rows.append(...)` as a
    malformed "row" the frontend never expects."""
    stdout = f"{bad_line}\n"

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return _completed(args, 0, stdout, "")

    monkeypatch.setattr(session_ledger.subprocess, "run", fake_run)

    response = asyncio.run(asgi_client.get("/api/ledger/tail"))

    assert response.status_code == 503
    body = response.json()
    assert body["error"]["code"] == "ledger_unavailable"
    assert bad_line in body["error"]["message"]


def test_ledger_tail_503_on_non_object_json_mixed_with_valid_rows(
    asgi_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Probe exactly as specified: stdout='null\\n[1,2]\\n' (mixed with a
    genuinely valid row) still refuses the whole response."""
    stdout = json.dumps({"id": "a" * 64, "summary": "ok"}) + "\nnull\n[1,2]\n"

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return _completed(args, 0, stdout, "")

    monkeypatch.setattr(session_ledger.subprocess, "run", fake_run)

    response = asyncio.run(asgi_client.get("/api/ledger/tail"))

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "ledger_unavailable"


# --- startup-time warning: a leaked root override must never be silent ----


def test_warns_at_import_time_when_the_root_override_is_set(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """`_warn_if_root_override_is_set()` is the exact call made once, at
    module-import time, below its own definition -- calling it directly
    here tests precisely what fires at process startup without needing to
    force a module reload (which would desync the already-registered
    FastAPI route objects from a freshly re-executed module namespace)."""
    monkeypatch.setenv(session_ledger._LEDGER_ROOT_ENV, "/tmp/somewhere-not-production")

    with caplog.at_level(logging.WARNING, logger=session_ledger.__name__):
        session_ledger._warn_if_root_override_is_set()

    assert any(
        session_ledger._LEDGER_ROOT_ENV in record.getMessage()
        and "/tmp/somewhere-not-production" in record.getMessage()
        for record in caplog.records
    )


def test_does_not_warn_when_the_root_override_is_unset(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv(session_ledger._LEDGER_ROOT_ENV, raising=False)

    with caplog.at_level(logging.WARNING, logger=session_ledger.__name__):
        session_ledger._warn_if_root_override_is_set()

    assert not caplog.records


# --- app-level session-token gate (real_auth) ------------------------------


@pytest.mark.real_auth
class TestLedgerRoutesAreGated:
    @pytest.fixture
    def token_header(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, str]:
        monkeypatch.setattr(token, "TOKEN_PATH", tmp_path / "sessions-token")
        return {"X-Session-Token": token.load_or_create_token()}

    def test_claims_401_without_token(self, asgi_client: httpx.AsyncClient) -> None:
        response = asyncio.run(asgi_client.get("/api/ledger/claims"))
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "unauthorized"

    def test_tail_401_without_token(self, asgi_client: httpx.AsyncClient) -> None:
        response = asyncio.run(asgi_client.get("/api/ledger/tail"))
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "unauthorized"

    def test_claims_forbidden_to_machine_token_but_reachable_to_asserted_principal(
        self,
        asgi_client: httpx.AsyncClient,
        token_header: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            session_ledger.subprocess,
            "run",
            lambda args, **kwargs: _completed(args, 0, "", ""),
        )
        response = asyncio.run(asgi_client.get("/api/ledger/claims", headers=token_header))
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "system_principal_forbidden"

        operator_response = asyncio.run(
            asgi_client.get(
                "/api/ledger/claims",
                headers={**token_header, "X-Omni-Authenticated-Principal": "human:operator"},
            )
        )
        assert operator_response.status_code == 200
        assert operator_response.json() == []

    def test_tail_forbidden_to_machine_token_but_reachable_to_asserted_principal(
        self,
        asgi_client: httpx.AsyncClient,
        token_header: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            session_ledger.subprocess,
            "run",
            lambda args, **kwargs: _completed(args, 0, "", ""),
        )
        response = asyncio.run(asgi_client.get("/api/ledger/tail", headers=token_header))
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "system_principal_forbidden"

        operator_response = asyncio.run(
            asgi_client.get(
                "/api/ledger/tail",
                headers={**token_header, "X-Omni-Authenticated-Principal": "human:operator"},
            )
        )
        assert operator_response.status_code == 200
        assert operator_response.json() == []

    def test_the_unrelated_public_ledger_route_stays_public_under_the_real_gate(
        self, asgi_client: httpx.AsyncClient
    ) -> None:
        """Only /api/ledger/claims and /api/ledger/tail are gated -- the
        pre-existing GET /api/ledger (unrelated cognitive-flow manifest read,
        routes.control.ledger) must stay reachable without a token, proving
        this is a literal two-path match and not a blanket gate over the
        whole /api/ledger/* tree."""
        response = asyncio.run(asgi_client.get("/api/ledger"))
        assert response.status_code == 200


# --- isolation: never the raw JSONL, never --owner ---------------------------


def test_ledger_routes_never_open_files_or_pass_owner() -> None:
    """Isolation grep (brief hard rule): the route module must SHELL the CLI
    and never itself read ``ledger*.jsonl`` or pass ``--owner`` -- the CLI owns
    the lock/ordering/correction-attachment, and the owner-only stream must
    never leave this Mac via the API. A committed grep, not a claim.

    Checks are AST/code-shape based rather than whole-file substring
    matches, on purpose: this module's own docstrings must legitimately
    name ``--owner`` and ``ledger*.jsonl`` BY NAME to document the rule
    ("NEVER pass --owner"), so a naive ``"--owner" not in source`` assertion
    would fail on the very documentation explaining why it never happens. A
    docstring is one big string ``Constant``, not a list literal or a call
    -- the checks below inspect actual CODE SHAPES instead.
    """
    source = Path(session_ledger.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    # 1. No list literal anywhere in the module's CODE constructs the --owner
    #    flag. Every argv this module builds is a plain list literal, so
    #    this proves no code path can ever place --owner into argv.
    list_literal_strings = {
        elt.value
        for node in ast.walk(tree)
        if isinstance(node, ast.List)
        for elt in node.elts
        if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
    }
    assert "--owner" not in list_literal_strings, list_literal_strings

    # 2. No file-opening call anywhere in the module. The ONLY I/O this
    #    module performs is the subprocess call to the CLI, so it is
    #    structurally impossible for it to read ledger*.jsonl (or any other
    #    file) directly, regardless of what path/filename string might
    #    otherwise appear.
    file_opening_calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == "open":
            file_opening_calls.append("open(...)")
        elif isinstance(func, ast.Attribute) and func.attr in {
            "open",
            "read_text",
            "read_bytes",
            "readlines",
            "iterdir",
            "glob",
            "rglob",
        }:
            file_opening_calls.append(func.attr)
    assert not file_opening_calls, f"session_ledger must never touch the filesystem directly: {file_opening_calls}"

    # 3. Positive proof the relay mechanism is actually present -- an
    #    isolation test that would pass vacuously on a gutted file proves
    #    nothing.
    subprocess_run_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "run"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "subprocess"
    ]
    assert subprocess_run_calls, "expected at least one subprocess.run(...) call relaying the CLI"
    assert session_ledger._LEDGER_BIN == "/Users/youruser/Work/Ops/bin/ledger"

    # 4. The ONLY name that can ever feed the ["--root", <name>] list literal
    #    is a binding assigned from `_ledger_root()` -- a structural proof
    #    that --root can never be constructed from anything else (a query
    #    param, a hardcoded client-controlled string, a different call).
    root_bindings = {
        node.targets[0].id
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "_ledger_root"
    }
    assert root_bindings, "expected a `root = _ledger_root()` assignment somewhere"
    root_list_names = {
        elt.id
        for node in ast.walk(tree)
        if isinstance(node, ast.List)
        and any(isinstance(e, ast.Constant) and e.value == "--root" for e in node.elts)
        for elt in node.elts
        if isinstance(elt, ast.Name)
    }
    assert root_list_names, 'expected a ["--root", <name>] list literal somewhere'
    assert root_list_names <= root_bindings, (
        f"a name feeding the --root list literal is not bound from "
        f"_ledger_root(): {root_list_names - root_bindings}"
    )

    # 5. Neither route handler accepts a `root` parameter of its own --
    #    --root must only ever come from the server-side env-var seam
    #    (_ledger_root()), never from a client-supplied query param. A
    #    future `root: str = Query(...)` added to either route fails this
    #    guard structurally, not just by review.
    route_param_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in {"ledger_tail", "ledger_claims"}:
            route_param_names |= {arg.arg for arg in node.args.args}
            route_param_names |= {arg.arg for arg in node.args.kwonlyargs}
    assert "root" not in route_param_names, (
        f"a route handler accepts its own `root` param -- must only ever "
        f"come from _ledger_root(): {route_param_names}"
    )


# --- E2E: the CLI row round-trips through this route, against a SCRATCH ---
# --- ledger root -- never the real ~/.omniagentos/ops/session-ledger tree ---------


@pytest.mark.skipif(
    not Path(session_ledger._LEDGER_BIN).exists(),
    reason="the real ledger CLI is not present on this machine",
)
def test_ledger_tail_round_trips_a_row_appended_by_the_real_cli(
    asgi_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Append one real `note` row via the real ledger CLI (append-only --
    nothing is ever deleted) into a SCRATCH root (`--root tmp_path`, never
    the real estate ledger), refs task=<a real board task id>, then prove
    GET /api/ledger/tail?ref=task%3D<id> -- run against THIS worktree's test
    client, subprocess-shelling the SAME real CLI binary, pointed at the
    SAME scratch root via `OMNIAGENTOS_LEDGER_ROOT` -- relays it back.

    This is a REAL, unmocked subprocess call end to end (the acceptance
    criterion's designated stand-in for the live 127.0.0.1:8485 API, which
    runs current main and does not carry these routes yet: "prove the
    drawer path against your worktree's test client instead"). It is
    real-CLI, not real-DATA: a 2026-08-04 pre-merge review caught the first
    version of this test appending a fresh row to the actual
    ~/.omniagentos/ops/session-ledger tree on every single test run (append-only +
    hash-chained = permanent, unremovable pollution). Copying the CLI's own
    scrub-patterns.txt into the scratch root mirrors what `ledger.py`'s own
    `selftest()` does for the exact same isolation reason.
    """
    import shutil
    import uuid

    # Same absolute-path convention as _LEDGER_BIN itself (never resolved
    # via PATH or relative arithmetic).
    real_root = Path("/Users/youruser/Work/Ops/session-ledger")
    shutil.copy(real_root / "scrub-patterns.txt", tmp_path / "scrub-patterns.txt")
    monkeypatch.setenv(session_ledger._LEDGER_ROOT_ENV, str(tmp_path))

    marker = uuid.uuid4().hex[:12]
    summary = f"ledger-kanban integration round-trip check {marker}"
    env = {**os.environ, "LEDGER_SESSION": "session-B-omniagentos", "LEDGER_AGENT": "fable"}

    append = subprocess.run(
        [
            session_ledger._LEDGER_BIN,
            "--root",
            str(tmp_path),
            "append",
            "--event",
            "note",
            "--project",
            "t-smoke",
            "--summary",
            summary,
            "--refs",
            f"task={_REAL_BOARD_TASK_ID}",
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert append.returncode == 0, f"real ledger append (scratch root) failed: {append.stderr}"
    new_id = append.stdout.strip()
    assert len(new_id) == 64, f"expected a 64-hex row id on stdout, got: {new_id!r}"

    # Exact wire shape from the brief: ref=task%3D<id> (the literal '=' inside
    # the ref value, percent-encoded). The route reads OMNIAGENTOS_LEDGER_ROOT
    # (set above) and relays the SAME scratch root -- never the real ledger.
    response = asyncio.run(
        asgi_client.get(f"/api/ledger/tail?ref=task%3D{_REAL_BOARD_TASK_ID}&n=50")
    )

    assert response.status_code == 200
    rows = response.json()
    matches = [row for row in rows if row.get("id") == new_id]
    assert matches, (
        f"appended row {new_id} (marker {marker}) did not round-trip through "
        f"GET /api/ledger/tail?ref=task%3D{_REAL_BOARD_TASK_ID} -- got {len(rows)} rows"
    )
    row = matches[0]
    assert row["session"] == "session-B-omniagentos"
    assert row["agent"] == "fable"
    assert row["event"] == "note"
    assert row["project"] == "t-smoke"
    assert row["summary"] == summary
    assert row["refs"]["task"] == _REAL_BOARD_TASK_ID


def test_e2e_test_never_touches_the_real_estate_ledger_tree() -> None:
    """A structural guard on the E2E test itself, so a future edit cannot
    silently reintroduce production writes: the append subprocess call must
    always carry an explicit --root argument pointed at a pytest fixture
    (tmp_path), and the route-side read must go through the SAME
    OMNIAGENTOS_LEDGER_ROOT env var this file's own monkeypatch sets --
    never the bare `~/.omniagentos/ops/bin/ledger append ...` invocation (with no
    --root) the pre-fix version used."""
    source = Path(__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    e2e = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name == "test_ledger_tail_round_trips_a_row_appended_by_the_real_cli"
    )
    e2e_source = ast.get_source_segment(source, e2e) or ""
    assert '"--root",' in e2e_source
    assert "str(tmp_path)" in e2e_source
    assert "monkeypatch.setenv(session_ledger._LEDGER_ROOT_ENV" in e2e_source
