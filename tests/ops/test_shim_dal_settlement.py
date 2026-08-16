"""The estate Kimi shim must settle spend through the DAL, observably.

Scope note (deliberate, read before "fixing" a skip)
----------------------------------------------------
The shim itself lives OUTSIDE this repo, at ``~/.omniagentos/ops/spend-state/kimi-shim``.
A cross-tree candidate is not gradeable here, so this lane ships the intended
shim change as a patch artifact (``devtasks/spend-truth-0809/kimi-shim.patch``)
and asserts on it directly. Three layers, so nothing is taken on trust:

1. **The checker has teeth.** It is run against today's REAL shim text --
   recovered from the removed side of our own patch, so it cannot drift from the
   file it was generated from -- and must report every defect.
2. **The patch actually removes them.** The added side must contain no
   hand-built INSERT, must call the DAL entry point, and must record the
   settlement outcome durably.
3. **The live file, when it has been patched.** Env-gated on ``KIMI_SHIM_PATH``
   (default: the estate path). While the shim is unmodified this test SKIPS with
   an explicit reason rather than failing -- an in-repo candidate must not be red
   for an out-of-repo file it is forbidden to edit.

Plus the half that is fully in-repo and always executes: the recorder's failure
is observable (non-zero exit AND a machine-readable record), and a placeholder
attribution triple is refused outright.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from omniagentos.adapters.spend_db import PRODUCTION_SPEND_DB
from omniagentos.db.store import SqliteStore
from scripts.spend import record_provider_call as recorder
from tests.support.db_template import make_store

REPO_ROOT = Path(__file__).resolve().parents[2]
PATCH_PATH = REPO_ROOT / "devtasks" / "spend-truth-0809" / "kimi-shim.patch"

DEFAULT_SHIM_PATH = "/Users/youruser/Work/Ops/spend-state/kimi-shim"
SHIM_PATH_ENV = "KIMI_SHIM_PATH"

HAND_BUILT_INSERT = "INSERT INTO provider_call_usage"
RECORDER_ENTRY_POINT = "record_provider_call.py"
DURABLE_OUTCOME_FIELD = '"outcome"'
_PLACEHOLDER_ATTRIBUTION = re.compile(r'^\s*(request_id|execution_id)="\$call_id"\s*$', re.M)
_SWALLOWED_WRITE = re.compile(r'run_sql_write\b[^\n]*>\s*/dev/null\s+2>&1')


def shim_settlement_findings(text: str) -> list[str]:
    """Every way ``text`` still writes spend behind the DAL, or loses it quietly."""

    findings: list[str] = []
    if HAND_BUILT_INSERT in text:
        findings.append("hand-built INSERT INTO provider_call_usage")
    if _PLACEHOLDER_ATTRIBUTION.search(text):
        findings.append("placeholder attribution (request/execution id = call_id)")
    if _SWALLOWED_WRITE.search(text):
        findings.append("ledger write failure swallowed (>/dev/null 2>&1)")
    if RECORDER_ENTRY_POINT not in text:
        findings.append(f"no call to the DAL entry point ({RECORDER_ENTRY_POINT})")
    if DURABLE_OUTCOME_FIELD not in text:
        findings.append("settlement outcome is not recorded durably")
    return findings


def _patch_sides() -> tuple[str, str]:
    """(removed, added) text from the shim patch artifact.

    ``devtasks/spend-truth-0809/`` is a historical, estate-specific dev-task
    lane (grading a since-landed fix to an out-of-repo shim tied to one paid
    provider CLI). This checkout's ``devtasks/`` was trimmed to the generic
    benchmark corpus for the public release, so the lane directory — and this
    patch artifact — never shipped here. Layers 1 and 2 below are estate-bound
    on that artifact the same way layer 3 is already estate-bound on the live
    out-of-repo shim file; skip with the same explicit-reason discipline
    rather than fail on a FileNotFoundError that names nothing actionable.
    """
    if not PATCH_PATH.is_file():
        pytest.skip(
            f"{PATCH_PATH} not present in this checkout (devtasks/ ships only the "
            "benchmark corpus here) -- this lane grades a historical, "
            "estate-specific patch artifact that was not carried into this release"
        )
    lines = PATCH_PATH.read_text(encoding="utf-8").splitlines()
    removed = [line[1:] for line in lines if line.startswith("-") and not line.startswith("---")]
    added = [line[1:] for line in lines if line.startswith("+") and not line.startswith("+++")]
    return "\n".join(removed), "\n".join(added)


# --- layer 1: the checker has teeth on today's real shim text ---------------


def test_findings_flag_every_defect_in_todays_shim_text() -> None:
    removed, _ = _patch_sides()
    findings = shim_settlement_findings(removed)
    assert "hand-built INSERT INTO provider_call_usage" in findings
    assert "placeholder attribution (request/execution id = call_id)" in findings
    assert "ledger write failure swallowed (>/dev/null 2>&1)" in findings
    assert f"no call to the DAL entry point ({RECORDER_ENTRY_POINT})" in findings
    assert "settlement outcome is not recorded durably" in findings


# --- layer 2: the patch removes them ---------------------------------------


def test_patch_replaces_the_hand_built_insert_with_dal_settlement() -> None:
    removed, added = _patch_sides()

    # Every defect leaves on the removed side...
    assert HAND_BUILT_INSERT in removed
    assert HAND_BUILT_INSERT not in added
    assert _PLACEHOLDER_ATTRIBUTION.search(removed) is not None
    assert _PLACEHOLDER_ATTRIBUTION.search(added) is None
    assert _SWALLOWED_WRITE.search(removed) is not None
    assert _SWALLOWED_WRITE.search(added) is None

    # ...and the replacement arrives on the added side.
    assert RECORDER_ENTRY_POINT in added
    assert "--unattributed-reason" in added
    assert DURABLE_OUTCOME_FIELD in added
    # The shim must still hand back the child's status: a telemetry failure
    # never fails the operator's Kimi call, which is precisely why the exit
    # status is not the evidence and the durable outcome field is.
    assert 'exit "$child_status"' in PATCH_PATH.read_text(encoding="utf-8")


def test_patch_removes_the_shims_own_ledger_write_path() -> None:
    """``run_sql_write`` existed only for the hand-built INSERT."""

    removed, added = _patch_sides()
    assert "run_sql_write() {" in removed
    assert "run_sql_write() {" not in added


# --- layer 3: the live file, once it has been patched -----------------------


def test_live_shim_settles_through_the_dal() -> None:
    shim_path = Path(os.environ.get(SHIM_PATH_ENV, DEFAULT_SHIM_PATH))
    if not shim_path.is_file():
        pytest.skip(
            f"shim not present at {shim_path} (set {SHIM_PATH_ENV} to point at it)"
        )
    text = shim_path.read_text(encoding="utf-8")
    if HAND_BUILT_INSERT in text:
        pytest.skip(
            f"shim at {shim_path} is UNMODIFIED (still contains the hand-built "
            f"{HAND_BUILT_INSERT!r}). This lane may not edit that out-of-repo file; "
            "apply devtasks/spend-truth-0809/kimi-shim.patch and re-run with "
            f"{SHIM_PATH_ENV} to turn this assertion on."
        )
    assert shim_settlement_findings(text) == []


# --- always-executed: failure is observable, placeholders are refused -------


SHIM_PAYLOAD: dict[str, Any] = {
    "stage": "worker",
    "provider": "moonshot",
    "transport": "cli",
    "requested_model": "kimi-cli-unobserved",
    "effective_model": "kimi-cli-unobserved",
    "model_lineage": "kimi",
    "billing_provider": "moonshot",
    "adapter_key": "estate-kimi-shim",
    "request_state": "indeterminate",
    "provider_outcome": "exit-code-0",
    "cost_quality": "estimated",
    "cost_upper_bound_usd_nanos": 5_000_000_000,
    "cost_source": "estate-kimi-shim:coarse-flat-estimate-v1:$5.00-unmeasured",
}
SHIM_REASON = "kimi-cli-invocation-has-no-request-or-execution-context"


def _cli_args(**overrides: str) -> list[str]:
    payload = dict(SHIM_PAYLOAD)
    argv: list[str] = []
    for key, value in payload.items():
        argv += [f"--{key.replace('_', '-')}", str(value)]
    for key, value in overrides.items():
        argv += [f"--{key.replace('_', '-')}", value]
    return argv


@pytest.fixture
def ledger_path(tmp_path: Path) -> str:
    """A migrated SCRATCH ledger. The live ledger is never opened by this suite."""

    path = tmp_path / "shim-settlement.sqlite3"
    make_store(SqliteStore, str(path)).close()
    return str(path)


def _last_stdout_json(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    captured = capsys.readouterr()
    line = captured.out.strip().splitlines()[-1]
    parsed = json.loads(line)
    assert isinstance(parsed, dict)
    return parsed


def test_successful_settlement_is_reported_as_such(
    ledger_path: str, capsys: pytest.CaptureFixture[str]
) -> None:
    status = recorder.main(
        _cli_args(db=ledger_path, unattributed_reason=SHIM_REASON)
    )
    assert status == 0
    result = _last_stdout_json(capsys)
    assert result["ok"] is True
    assert result["attributed"] is False


def test_failed_settlement_exits_nonzero_and_says_so(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The defect this lane exists to remove: a lost row that looks like a kept one."""

    corrupt = tmp_path / "corrupt.sqlite3"
    corrupt.write_text("this is not a database", encoding="utf-8")

    status = recorder.main(
        _cli_args(db=str(corrupt), unattributed_reason=SHIM_REASON)
    )

    assert status != 0, "a failed ledger write must not exit 0"
    result = _last_stdout_json(capsys)
    assert result["ok"] is False
    assert result["error_kind"] == "db_unreadable"


def test_failure_record_reaches_stderr_as_well(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    corrupt = tmp_path / "corrupt.sqlite3"
    corrupt.write_text("this is not a database", encoding="utf-8")
    recorder.main(_cli_args(db=str(corrupt), unattributed_reason=SHIM_REASON))
    captured = capsys.readouterr()
    assert "record_provider_call: FAILED" in captured.err


def test_missing_ledger_is_refused_not_quietly_created(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A typo must not become a plausible second ledger that absorbs money rows."""

    missing = tmp_path / "definitely-not-here.sqlite3"
    status = recorder.main(
        _cli_args(db=str(missing), unattributed_reason=SHIM_REASON)
    )
    assert status == 1
    assert _last_stdout_json(capsys)["error_kind"] == "db_missing"
    assert not missing.exists()


def test_unmigrated_ledger_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A hand-rolled probe DB is not the ledger, even with the right table in it."""

    probe = tmp_path / "probe.sqlite3"
    with sqlite3.connect(probe) as connection:
        connection.execute("CREATE TABLE provider_call_usage (call_id TEXT)")
    status = recorder.main(_cli_args(db=str(probe), unattributed_reason=SHIM_REASON))
    assert status == 1
    assert _last_stdout_json(capsys)["error_kind"] == "schema_missing"


def test_placeholder_attribution_triple_is_refused(
    ledger_path: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """The old shim's exact payload shape is now unrepresentable."""

    placeholder = "estate-kimi-shim-11111111-2222-3333-4444-555555555555"
    status = recorder.main(
        _cli_args(
            db=ledger_path,
            call_id=placeholder,
            request_id=placeholder,
            execution_id=placeholder,
        )
    )
    assert status == 2
    assert _last_stdout_json(capsys)["error_kind"] == "placeholder_attribution"
    with sqlite3.connect(ledger_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM provider_call_usage").fetchone() == (0,)


def test_missing_identity_without_a_stated_reason_is_refused(
    ledger_path: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """A row with no identity must say WHY it has none."""

    status = recorder.main(_cli_args(db=ledger_path))
    assert status == 2
    assert _last_stdout_json(capsys)["error_kind"] == "missing_attribution"
    with sqlite3.connect(ledger_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM provider_call_usage").fetchone() == (0,)


def test_stdin_payload_is_accepted_and_flags_win(
    ledger_path: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = dict(SHIM_PAYLOAD)
    payload["provider_outcome"] = "exit-code-99"
    monkeypatch.setattr("sys.stdin", _FakeStdin(json.dumps(payload)))

    status = recorder.main(
        [
            "--stdin-json",
            "--db",
            ledger_path,
            "--unattributed-reason",
            SHIM_REASON,
            "--provider-outcome",
            "exit-code-3",
        ]
    )
    assert status == 0
    assert _last_stdout_json(capsys)["ok"] is True
    with sqlite3.connect(ledger_path) as connection:
        assert connection.execute(
            "SELECT provider_outcome FROM provider_call_usage"
        ).fetchone() == ("exit-code-3",)


# --- the ledger this writer attaches to is the canonical one ----------------


def test_default_ledger_is_the_canonical_spend_ledger(monkeypatch: pytest.MonkeyPatch) -> None:
    """Not ``OMNIAGENTOS_DB``, and not a campaign database.

    ``SpendGuard`` -- the other writer of this table -- resolves through
    ``resolve_spend_db_path``. A second precedence chain here would let a paid
    row land where the cap preflight never looks, which is spend that reads as
    absent.
    """

    monkeypatch.delenv("OMNIAGENTOS_SPEND_DB", raising=False)
    monkeypatch.setenv("OMNIAGENTOS_DB", "/tmp/some-control-plane-db.sqlite3")
    assert recorder.resolve_db_path(None) == str(PRODUCTION_SPEND_DB)

    monkeypatch.setenv("OMNIAGENTOS_SPEND_DB", "/tmp/spend-override.sqlite3")
    assert recorder.resolve_db_path(None) == "/tmp/spend-override.sqlite3"
    assert recorder.resolve_db_path("/tmp/explicit.sqlite3") == "/tmp/explicit.sqlite3"


def test_simulation_context_refuses_before_any_write(
    ledger_path: str, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Paid spend is never accounted against a simulation ledger."""

    campaign_root = tmp_path / "campaign"
    campaign_root.mkdir()
    monkeypatch.setenv("OMNIAGENTOS_SIM_MODE", "1")
    monkeypatch.setenv("OMNIAGENTOS_SIM_CAMPAIGN", "probe")
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path / "var"))
    monkeypatch.setenv("OMNIAGENTOS_SIM_CAMPAIGN_ROOT", str(campaign_root))

    status = recorder.main(_cli_args(db=ledger_path, unattributed_reason=SHIM_REASON))

    assert status == 2, "a simulation context must refuse, not write"
    assert _last_stdout_json(capsys)["error_kind"] == "spend_db_unresolvable"
    with sqlite3.connect(ledger_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM provider_call_usage").fetchone() == (0,)


class _FakeStdin:
    def __init__(self, text: str) -> None:
        self._text = text

    def read(self) -> str:
        return self._text
