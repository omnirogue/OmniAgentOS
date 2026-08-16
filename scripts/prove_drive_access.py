#!/usr/bin/env python3
"""DRIVE ACCESS PROOF (W4): real end-to-end grant -> thread -> --add-dir -> disk.

Proves, through the REAL runner/adapter dir-threading path -- production
``ProjectStore`` + ``ProvisionStore.grant_dir`` (via ``grant_drive_dir``) +
``Runner._execute_agent`` + the real ``omniagentos.policy`` module (loaded
from the real ``configs/policy.yaml``) + the unmodified ``ClaudeAdapter``
-- that a project granted Drive folders can actually reach them:

1. grants a throwaway project READ access to the real
   ``~/Library/Mobile Documents/com~apple~CloudDocs/Media Buying/CopywritingBrainVault``
   and WRITE access to the real Google Drive ``My Drive/OmniAgent`` workspace
   (``omniagentos.drive.google_drive_workspace()``);
2. drives one real ``Runner.tick()`` -- only the actual CLI subprocess spawn is
   swapped for a capturing mock adapter, exactly like the rest of this repo's
   runner tests (tests/runner/test_state_machine.py et al.) -- and captures the
   real ``AgentInput`` the runner built for that run;
3. builds the REAL ``ClaudeAdapter._command()`` argv from that captured
   ``AgentInput`` and asserts ``--add-dir`` is present for BOTH granted dirs;
4. actually READS a real file from CopywritingBrainVault (proving the granted
   read dir is genuinely usable -- with a bounded retry, since a real iCloud
   file already on disk can still need on-demand materialization, see
   omniagentos/drive.py's module docstring) and WRITES a new file + a new
   subfolder under ``My Drive/OmniAgent`` (proving the granted write dir is
   genuinely usable; new files there sync to Google Drive on their own).

Writes NEW files/folders only -- never touches, overwrites, or deletes an
existing user file. Exit 0 on success; nonzero with a clear message on any
failure (including the two real-directory preconditions this proof assumes:
CopywritingBrainVault must exist, and a Google Drive mount must be signed in).
"""

from __future__ import annotations

import json
import shutil
import signal
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Any

# Allow `python scripts/prove_drive_access.py` from the worktree root without install.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from omniagentos import drive  # noqa: E402
from omniagentos.adapters.claude import ClaudeAdapter  # noqa: E402
from omniagentos.contracts import (  # noqa: E402
    ActionClass,
    AgentInput,
    BudgetDecision,
    HarnessType,
    RunState,
    TaskState,
    new_id,
    utc_now_iso,
)
from omniagentos.db.migrate import migrate  # noqa: E402
from omniagentos.db.store import SqliteStore  # noqa: E402
from omniagentos.mock_adapter import MockAdapter  # noqa: E402
from omniagentos.policy import evaluate_action, load_policy, sandbox_for_tools  # noqa: E402
from omniagentos.projects import ProjectStore  # noqa: E402
from omniagentos.provision import ProvisionStore, grant_drive_dir  # noqa: E402
from omniagentos.runner.core import Runner, RunnerDependencies  # noqa: E402

READ_DIR = (
    Path.home()
    / "Library"
    / "Mobile Documents"
    / "com~apple~CloudDocs"
    / "Media Buying"
    / "CopywritingBrainVault"
)
READ_FILE = READ_DIR / "00 - Start Here" / "Home.md"
MATERIALIZE_TIMEOUT_S = 90.0


class _MaterializeTimeout(Exception):
    pass


class _CapturingAdapter(MockAdapter):
    """Real MockAdapter behavior, plus records every AgentInput the runner built."""

    def __init__(self) -> None:
        self.calls: list[AgentInput] = []

    def run(self, input: AgentInput) -> Any:
        self.calls.append(input)
        return super().run(input)


def _ok(msg: str) -> None:
    print(f"  OK  {msg}")


def _section(title: str) -> None:
    print(f"\n=== {title} ===")


def _read_with_materialization_retry(path: Path, *, timeout_s: float) -> bytes:
    """Read a cloud-drive file's content, retrying while it on-demand materializes.

    A real iCloud/Google Drive file can exist on disk (stat succeeds instantly)
    while its CONTENT is still being fetched from the cloud by the OS sync
    daemon -- an ordinary, expected condition for real Drive-backed storage
    (see omniagentos/drive.py's module docstring), not an error. Retries with a
    bounded per-attempt alarm so a genuinely stuck daemon fails loudly instead
    of hanging the proof forever.
    """
    if not path.is_file():
        raise RuntimeError(f"expected a real file at {path}")

    def _on_alarm(signum: int, frame: Any) -> None:
        raise _MaterializeTimeout()

    previous_handler = signal.signal(signal.SIGALRM, _on_alarm)
    deadline = time.monotonic() + timeout_s
    attempt = 0
    try:
        while True:
            attempt += 1
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"{path} did not materialize its content within {timeout_s:g}s "
                    "(the local iCloud/Google Drive sync daemon may be busy with a "
                    "large unrelated backlog -- this is an environmental condition, "
                    "not a dir-threading/grant failure; the grant + --add-dir "
                    "wiring above already succeeded)"
                )
            signal.setitimer(signal.ITIMER_REAL, min(15.0, remaining))
            try:
                data = path.read_bytes()
                return data
            except _MaterializeTimeout:
                print(f"  .   attempt {attempt}: still materializing, retrying...")
                continue
            finally:
                signal.setitimer(signal.ITIMER_REAL, 0)
    finally:
        signal.signal(signal.SIGALRM, previous_handler)


def _write_note(root: str, relpath: str, content: str) -> str:
    path = Path(root) / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return str(path)


def main() -> int:
    if not READ_DIR.is_dir():
        print(f"PROOF FAILED: expected real read-dir not found: {READ_DIR}", file=sys.stderr)
        return 1
    write_dir_str = drive.google_drive_workspace()
    if write_dir_str is None:
        print("PROOF FAILED: no Google Drive mount detected on this machine", file=sys.stderr)
        return 1
    write_dir = Path(write_dir_str)

    workspace = Path(tempfile.mkdtemp(prefix="omniagentos-drive-proof-"))
    db_path = workspace / "proof.db"
    ledger_dir = workspace / "ledger"
    vault_dir = workspace / "vault"
    scratch_working_dir = workspace / "scratch-working-dir"
    scratch_working_dir.mkdir(parents=True)

    try:
        migrate(str(db_path))
        store = SqliteStore(str(db_path))

        _section("1. Provision a throwaway project + grant Drive access")
        project = ProjectStore(store).create_project(
            {"name": f"drive-access-proof-{new_id('proof')}"}
        )
        project_id = str(project["id"])
        prov = ProvisionStore(store)
        grant_drive_dir(prov, project_id, str(READ_DIR))
        grant_drive_dir(prov, project_id, str(write_dir))
        row = ProjectStore(store).get_project(project_id)
        assert row is not None
        assert str(READ_DIR.resolve()) in row["root_dirs"]
        assert str(write_dir.resolve()) in row["root_dirs"]
        _ok(f"granted READ:  {READ_DIR}")
        _ok(f"granted WRITE: {write_dir}")

        _section("2. Drive one real Runner.tick() and capture the built AgentInput")
        task_id = new_id("tsk")
        run_id = new_id("run")
        now = utc_now_iso()
        store.create_task(
            {
                "id": task_id,
                "discipline_id": "code-changes",
                "title": "W4 drive access proof",
                "project_id": project_id,
                "input_json": json.dumps(
                    {
                        "prompt": "prove drive access",
                        "tools_allowed": ["file_write"],
                        "working_dir": str(scratch_working_dir),
                    }
                ),
                "acceptance_json": "{}",
                "state": TaskState.QUEUED.value,
                "risk": "low",
                "created_at": now,
                "updated_at": now,
            }
        )
        plan = [
            {
                "name": "prove",
                "kind": "agent",
                "action_class": ActionClass.SANDBOXED_CREATION.value,
                "params": {"adapter": "mock"},
            }
        ]
        store.enqueue_run(
            {
                "id": run_id,
                "task_id": task_id,
                "discipline_id": "code-changes",
                "harness": HarnessType.MOCK.value,
                "state": RunState.QUEUED.value,
                "plan_json": json.dumps(plan),
                "budget_json": "{}",
                "trace_id": f"trace-{run_id}",
                "queued_at": now,
                "created_at": now,
                "updated_at": now,
            }
        )

        cfg = load_policy()  # the REAL configs/policy.yaml
        adapter = _CapturingAdapter()
        deps = RunnerDependencies(
            evaluate_policy=lambda action: evaluate_action(action, cfg),
            sandbox_for_tools=lambda harness, tools: sandbox_for_tools(harness, tools, cfg),
            check_budget=lambda *_a: BudgetDecision(allowed=True),
            resolve_adapter=lambda _harness: adapter,
            append_manifest=lambda root, manifest: str(Path(root) / f"{manifest.run_id}.jsonl"),
            render_run_note=lambda run, _steps, _manifest, _receipts, **_kw: (
                f"runs/{run['id']}.md",
                "drive access proof",
            ),
            write_note=_write_note,
        )
        runner = Runner(
            store,
            "proof-worker",
            dependencies=deps,
            ledger_dir=str(ledger_dir),
            vault_dir=str(vault_dir),
        )
        did_work = runner.tick()
        assert did_work, "runner.tick() did no work"
        assert len(adapter.calls) == 1, f"expected exactly one agent call, got {len(adapter.calls)}"
        captured = adapter.calls[0]
        assert captured.working_dir == str(scratch_working_dir)
        extra_dirs = captured.metadata.get("extra_dirs")
        assert extra_dirs, f"expected extra_dirs to carry both grants, got {extra_dirs!r}"
        assert str(READ_DIR.resolve()) in extra_dirs
        assert str(write_dir.resolve()) in extra_dirs
        assert str(scratch_working_dir) not in extra_dirs
        run_row = store.get_run(run_id)
        assert run_row is not None and run_row["state"] == RunState.COMPLETED.value
        _ok(f"AgentInput.working_dir = {captured.working_dir}")
        _ok(f"AgentInput.metadata['extra_dirs'] = {extra_dirs}")
        _ok(f"run {run_id} reached state={run_row['state']}")

        _section("3. Build the REAL ClaudeAdapter argv and assert --add-dir for BOTH dirs")
        argv = ClaudeAdapter()._command(captured, captured.prompt, None)
        add_dir_targets = [argv[i + 1] for i, tok in enumerate(argv) if tok == "--add-dir"]
        assert str(READ_DIR.resolve()) in add_dir_targets, add_dir_targets
        assert str(write_dir.resolve()) in add_dir_targets, add_dir_targets
        _ok(f"claude argv --add-dir targets: {add_dir_targets}")

        _section("4a. List a real file in the granted READ dir (metadata only, no fetch needed)")
        listing = sorted(p.name for p in (READ_DIR / "00 - Start Here").iterdir() if p.is_file())
        assert READ_FILE.name in listing, listing
        _ok(f"listed {READ_DIR / '00 - Start Here'}: {listing}")
        _ok(f"target file present: {READ_FILE.name}")

        _section("4b. Read that file's head (real cloud-drive content fetch)")
        read_ok = False
        head_preview = ""
        try:
            data = _read_with_materialization_retry(READ_FILE, timeout_s=MATERIALIZE_TIMEOUT_S)
            head_preview = data[:200].decode("utf-8", errors="replace")
            _ok(f"read {READ_FILE} ({len(data)} bytes)")
            print("  --- head (first 200 bytes) ---")
            for line in head_preview.splitlines()[:6]:
                print(f"  | {line}")
            read_ok = True
        except TimeoutError as exc:
            print(f"  !!  {exc}", file=sys.stderr)

        _section("4c. Write a new file + new subfolder under the granted WRITE dir")
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        proof_subfolder = write_dir / f"drive_access_proof_{stamp}"
        proof_subfolder.mkdir(parents=True, exist_ok=False)
        proof_file = proof_subfolder / "proof.txt"
        proof_file.write_text(
            "OmniAgentOS W4 Drive Access proof.\n"
            f"run_id={run_id}\n"
            f"project_id={project_id}\n"
            f"read_from={READ_FILE}\n"
            f"read_content_fetched={read_ok}\n"
            f"written_at={stamp}\n",
            encoding="utf-8",
        )
        assert proof_subfolder.is_dir()
        assert proof_file.is_file()
        _ok(f"created folder: {proof_subfolder}")
        _ok(f"wrote file:      {proof_file}")

        _section("RESULT")
        print(f"  Directory listing (00 - Start Here): {listing}")
        print(f"  READ target file     : {READ_FILE}")
        print(
            f"  READ content fetched : {read_ok}" + ("" if read_ok else " (see 4b warning above)")
        )
        print(f"  WRITE folder         : {proof_subfolder}")
        print(f"  WRITE file           : {proof_file}")
        if read_ok:
            print("\nDRIVE ACCESS PROOF: PASSED\n")
            return 0
        print(
            "\nDRIVE ACCESS PROOF: PARTIAL -- grant/thread/--add-dir + directory listing + "
            "Google Drive write all verified live; the CopywritingBrainVault CONTENT fetch "
            "specifically timed out (local iCloud sync daemon backlog, not a code defect).\n"
        )
        return 1
    except Exception as exc:
        traceback.print_exc()
        print(f"\nDRIVE ACCESS PROOF FAILED: {exc}\n", file=sys.stderr)
        return 1
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
