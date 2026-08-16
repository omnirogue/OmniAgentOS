"""Pins the shared, failure-aware transport for loopqueue/ledger.jsonl."""

from __future__ import annotations

import ast
import errno
import importlib
import io
import json
import multiprocessing
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PKG))

from bridge import file_proposal, janitor  # noqa: E402
from bridge import ledger_write as lw  # noqa: E402


def _process_writer(queue: str, start: multiprocessing.synchronize.Barrier, actor: str) -> None:
    start.wait()
    for sequence in range(12):
        lw.append_event(
            Path(queue),
            {
                "ts": "2026-08-11T00:00:00Z",
                "role": "external",
                "event": "observed",
                "id": f"{actor}:{sequence}",
                "actor": actor,
                "detail": {"payload": actor * 5000},
            },
        )


@pytest.fixture
def queue(tmp_path: Path) -> Path:
    path = tmp_path / "loopqueue"
    path.mkdir()
    return path


def test_append_owns_serialization_newline_lock_and_durability(queue: Path) -> None:
    event = {"event": "observed", "detail": {"unicode": "snowman ☃"}}
    result = lw.append_event(queue, event)

    raw = (queue / "ledger.jsonl").read_bytes()
    assert raw.endswith(b"\n")
    assert raw.count(b"\n") == 1
    assert json.loads(raw).items() >= event.items()
    assert set(json.loads(raw)) == {"ts", *event}, "no key may leak into the written event"
    assert result == lw.AppendResult(bytes_written=len(raw), durable=True)
    assert (queue / "locks" / "ledger.lock").is_file()


def test_lock_is_acquired_before_the_data_file_is_opened(
    queue: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_flock = lw.fcntl.flock
    real_open = lw.os.open
    locked = False

    def recording_flock(fd: int, operation: int) -> None:
        nonlocal locked
        real_flock(fd, operation)
        locked = True

    def guarded_open(path: os.PathLike[str] | str, flags: int, mode: int = 0o777) -> int:
        if Path(path).name == "ledger.jsonl":
            assert locked
        return real_open(path, flags, mode)

    monkeypatch.setattr(lw.fcntl, "flock", recording_flock)
    monkeypatch.setattr(lw.os, "open", guarded_open)
    lw.append_event(queue, {"event": "observed"})


def test_package_and_legacy_imports_share_the_same_thread_lock(queue: Path) -> None:
    sys.path.insert(0, str(PKG / "bridge"))
    try:
        legacy = importlib.import_module("ledger_write")
    finally:
        sys.path.remove(str(PKG / "bridge"))

    canonical_path = queue / "locks" / "ledger.lock"
    assert legacy._thread_lock(canonical_path) is lw._thread_lock(canonical_path)

    alias = queue.parent / "queue-alias"
    alias.symlink_to(queue, target_is_directory=True)
    assert lw._thread_lock(alias / "locks" / "ledger.lock") is lw._thread_lock(canonical_path)


def test_short_writes_are_completed_under_the_same_lock(
    queue: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_write = lw.os.write
    calls = 0

    def short_write(fd: int, data: bytes | memoryview) -> int:
        nonlocal calls
        calls += 1
        return real_write(fd, data[:7])

    monkeypatch.setattr(lw.os, "write", short_write)
    event = {"event": "observed", "detail": {"payload": "x" * 100}}
    lw.append_event(queue, event)

    assert calls > 1
    written_short = json.loads((queue / "ledger.jsonl").read_text())
    assert written_short.items() >= event.items()
    assert set(written_short) == {"ts", *event}, "no key may leak into the written event"


def test_eintr_is_retried(queue: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real_write = lw.os.write
    interrupted = False

    def write_after_interrupt(fd: int, data: bytes | memoryview) -> int:
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            raise OSError(errno.EINTR, "interrupted")
        return real_write(fd, data)

    monkeypatch.setattr(lw.os, "write", write_after_interrupt)
    lw.append_event(queue, {"event": "observed"})
    assert interrupted
    assert (queue / "ledger.jsonl").read_bytes().endswith(b"\n")


def test_zero_progress_is_a_definite_no_bytes_failure(
    queue: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lw.os, "write", lambda _fd, _data: 0)
    with pytest.raises(lw.LedgerAppendError) as caught:
        lw.append_event(queue, {"event": "observed"})

    assert caught.value.phase == "write"
    assert caught.value.bytes_written == 0
    assert not caught.value.complete
    assert (queue / "ledger.jsonl").read_bytes() == b""


def test_partial_failure_is_reported_and_the_next_append_refuses_the_tail(
    queue: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_write = lw.os.write
    calls = 0

    def partial_then_fail(fd: int, data: bytes | memoryview) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            return real_write(fd, data[:5])
        raise OSError(errno.EIO, "injected")

    monkeypatch.setattr(lw.os, "write", partial_then_fail)
    with pytest.raises(lw.LedgerAppendError) as caught:
        lw.append_event(queue, {"event": "observed", "detail": {"x": 1}})

    assert caught.value.bytes_written == 5
    assert not caught.value.complete
    monkeypatch.setattr(lw.os, "write", real_write)
    before = (queue / "ledger.jsonl").read_bytes()
    with pytest.raises(lw.LedgerTailUnhealthy):
        lw.append_event(queue, {"event": "observed", "detail": {"x": 2}})
    assert (queue / "ledger.jsonl").read_bytes() == before

    lock_fd = os.open(queue / "locks" / "ledger.lock", os.O_RDWR)
    try:
        lw.fcntl.flock(lock_fd, lw.fcntl.LOCK_EX | lw.fcntl.LOCK_NB)
        lw.fcntl.flock(lock_fd, lw.fcntl.LOCK_UN)
    finally:
        os.close(lock_fd)


def test_fsync_failure_reports_a_complete_but_not_durable_event(
    queue: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (queue / "ledger.jsonl").touch()

    def fail_fsync(_fd: int) -> None:
        raise OSError(errno.EIO, "injected")

    monkeypatch.setattr(lw.os, "fsync", fail_fsync)
    with pytest.raises(lw.LedgerAppendError) as caught:
        lw.append_event(queue, {"event": "observed"})

    assert caught.value.phase == "fsync"
    assert caught.value.bytes_written > 0
    assert caught.value.complete
    assert not caught.value.durable
    assert (queue / "ledger.jsonl").read_bytes().endswith(b"\n")


def test_new_ledger_directory_close_failure_keeps_typed_commit_state(
    queue: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_open = lw.os.open
    real_close = lw.os.close
    directory_fd: int | None = None

    def recording_open(path: os.PathLike[str] | str, flags: int, mode: int = 0o777) -> int:
        nonlocal directory_fd
        fd = real_open(path, flags, mode)
        if Path(path) == queue:
            directory_fd = fd
        return fd

    def fail_directory_close(fd: int) -> None:
        if fd == directory_fd:
            real_close(fd)
            raise OSError(errno.EIO, "injected directory close")
        real_close(fd)

    monkeypatch.setattr(lw.os, "open", recording_open)
    monkeypatch.setattr(lw.os, "close", fail_directory_close)
    with pytest.raises(lw.LedgerAppendError) as caught:
        lw.append_event(queue, {"event": "observed"})

    assert caught.value.phase == "directory_close"
    assert caught.value.bytes_written > 0
    assert caught.value.complete
    assert caught.value.durable

    def raise_same(_queue: Path, _event: dict[str, object]) -> None:
        raise caught.value

    monkeypatch.setattr(lw, "append_event", raise_same)
    monkeypatch.setattr(lw.sys, "stdin", io.StringIO('{"event":"observed"}'))
    assert lw._main(["append", "--queue", str(queue)]) == 3


def test_proposal_rollback_distinguishes_zero_from_possible_ledger_bytes(
    queue: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(error: Exception):
        def raise_error(_queue: Path, _event: dict[str, object]) -> None:
            raise error

        return raise_error

    definitely_absent = file_proposal.LedgerAppendError(
        "before write", phase="write", bytes_written=0
    )
    monkeypatch.setattr(file_proposal, "append_event", fail(definitely_absent))
    with pytest.raises(file_proposal.LedgerAppendError):
        file_proposal._append_ledger_line(queue, '{"event":"proposed"}\n')

    possibly_present = file_proposal.LedgerAppendError(
        "after partial write", phase="write", bytes_written=3
    )
    monkeypatch.setattr(file_proposal, "append_event", fail(possibly_present))
    with pytest.raises(file_proposal.LedgerNotDurable):
        file_proposal._append_ledger_line(queue, '{"event":"proposed"}\n')


def test_existing_unterminated_tail_is_never_buried(queue: Path) -> None:
    ledger = queue / "ledger.jsonl"
    ledger.write_bytes(b'{"event":"old"}')

    with pytest.raises(lw.LedgerTailUnhealthy) as caught:
        lw.append_event(queue, {"event": "new"})

    assert caught.value.bytes_written == 0
    assert ledger.read_bytes() == b'{"event":"old"}'


def test_concurrent_processes_preserve_every_large_event_once(queue: Path) -> None:
    context = multiprocessing.get_context("spawn")
    actors = ["alpha", "bravo", "charlie", "delta"]
    start = context.Barrier(len(actors))
    workers = [
        context.Process(target=_process_writer, args=(str(queue), start, actor)) for actor in actors
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=30)
        assert worker.exitcode == 0

    physical = (queue / "ledger.jsonl").read_text().splitlines()
    events = [json.loads(line) for line in physical]
    expected = Counter((actor, sequence) for actor in actors for sequence in range(12))
    actual = Counter((event["actor"], int(event["id"].rsplit(":", 1)[1])) for event in events)
    assert actual == expected
    assert len(physical) == len(expected)
    assert all(len(line.encode()) > 4096 for line in physical)


def test_cli_requires_one_object_and_explicit_absolute_queue(queue: Path) -> None:
    script = PKG / "bridge" / "ledger_write.py"
    event = {"event": "observed", "actor": "cli"}
    success = subprocess.run(
        [sys.executable, str(script), "append", "--queue", str(queue)],
        input=json.dumps(event),
        text=True,
        capture_output=True,
        check=False,
    )
    invalid = subprocess.run(
        [sys.executable, str(script), "append", "--queue", str(queue)],
        input="[]",
        text=True,
        capture_output=True,
        check=False,
    )
    relative = subprocess.run(
        [sys.executable, str(script), "append", "--queue", "var/loopqueue"],
        input=json.dumps(event),
        text=True,
        capture_output=True,
        check=False,
    )

    assert success.returncode == 0
    written_cli = json.loads((queue / "ledger.jsonl").read_text())
    assert written_cli.items() >= event.items()
    assert set(written_cli) == {"ts", *event}, "no key may leak into the written event"
    assert invalid.returncode == 2
    assert relative.returncode == 2


def test_known_producers_do_not_reimplement_the_transport() -> None:
    repo = PKG.parent
    inventory = json.loads((PKG / "ledger-writer-carriers.json").read_text())
    carriers = inventory["repository_producers"]
    for relative in carriers:
        source = (repo / relative).read_text()
        assert "append_event" in source, relative
        direct_append = re.compile(
            r"ledger\.jsonl.{0,400}os\.O_APPEND|os\.O_APPEND.{0,400}ledger\.jsonl",
            re.DOTALL,
        )
        assert not direct_append.search(source), relative

    bootstrap = (repo / "pipeline/bootstrap.sh").read_text()
    assert ': > "$ROOT/ledger.jsonl"' not in bootstrap


def _direct_ledger_mutations(path: Path) -> list[str]:
    source = path.read_text(errors="replace")
    findings: list[str] = []
    if "ledger.jsonl" not in source:
        return findings
    if path.suffix == ".py":
        try:
            tree = ast.parse(source)
        except SyntaxError:
            tree = None
        if tree is not None:
            scope_types = (
                ast.Module,
                ast.FunctionDef,
                ast.AsyncFunctionDef,
                ast.Lambda,
                ast.ClassDef,
            )

            def direct_nodes(scope: ast.AST) -> tuple[list[ast.AST], list[ast.AST]]:
                nodes: list[ast.AST] = []
                nested: list[ast.AST] = []
                stack = list(ast.iter_child_nodes(scope))
                while stack:
                    node = stack.pop()
                    if isinstance(node, scope_types[1:]):
                        nested.append(node)
                        continue
                    nodes.append(node)
                    stack.extend(ast.iter_child_nodes(node))
                return nodes, nested

            def target_names(target: ast.AST) -> set[str]:
                return {child.id for child in ast.walk(target) if isinstance(child, ast.Name)}

            def ledger_literal(node: ast.AST) -> bool:
                return any(
                    isinstance(child, ast.Constant)
                    and isinstance(child.value, str)
                    and "ledger.jsonl" in child.value
                    for child in ast.walk(node)
                )

            def scan_scope(
                scope: ast.AST, inherited_live: set[str], inherited_scratch: set[str]
            ) -> None:
                nodes, nested = direct_nodes(scope)
                live = set(inherited_live)
                scratch = set(inherited_scratch)
                assignments: list[tuple[set[str], ast.AST]] = []
                for node in nodes:
                    if isinstance(node, ast.Assign):
                        names = set().union(*(target_names(target) for target in node.targets))
                        assignments.append((names, node.value))
                    elif isinstance(node, ast.AnnAssign) and node.value is not None:
                        assignments.append((target_names(node.target), node.value))
                    elif isinstance(node, ast.NamedExpr):
                        assignments.append((target_names(node.target), node.value))

                changed = True
                while changed:
                    changed = False
                    for names, value in assignments:
                        used = {n.id for n in ast.walk(value) if isinstance(n, ast.Name)}
                        value_text = ast.get_source_segment(source, value) or ""
                        if ledger_literal(value) or used & live:
                            destination = (
                                scratch if "scratch" in value_text and not used & live else live
                            )
                        elif used & scratch:
                            destination = scratch
                        else:
                            continue
                        before = len(destination)
                        destination.update(names)
                        changed |= len(destination) != before

                for node in nodes:
                    if not isinstance(node, ast.Call):
                        continue
                    used = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
                    call = ast.get_source_segment(source, node) or ""
                    is_live_ledger = (ledger_literal(node) and "scratch" not in call) or bool(
                        used & live
                    )
                    if not is_live_ledger:
                        continue
                    method = node.func.attr if isinstance(node.func, ast.Attribute) else ""
                    write_mode = bool(re.search(r"[\"'][awx][bt+]*[\"']", call))
                    write_flags = any(
                        flag in call
                        for flag in ("O_APPEND", "O_WRONLY", "O_RDWR", "O_CREAT", "O_TRUNC")
                    )
                    if method == "open" and (write_mode or write_flags):
                        findings.append(f"line {node.lineno}: direct append/open")
                    if method in {
                        "write",
                        "write_text",
                        "write_bytes",
                        "touch",
                        "rename",
                        "replace",
                        "unlink",
                    }:
                        findings.append(f"line {node.lineno}: direct mutation")

                for child_scope in nested:
                    scan_scope(child_scope, live, scratch)

            scan_scope(tree, set(), set())
    else:
        ledger_vars = set(
            re.findall(r"(?m)^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=.*ledger\.jsonl", source)
        )
        patterns = (
            r"os\.open\([^)]*ledger\.jsonl[^)]*O_APPEND",
            r"os\.open\([^)]*O_APPEND[^)]*ledger\.jsonl",
            r"(?:^|[^>])>>?\s*[^\n]*ledger\.jsonl",
        )
        for pattern in patterns:
            if re.search(pattern, source, re.DOTALL | re.MULTILINE):
                findings.append(f"pattern {pattern!r}: direct mutation")
        for variable in ledger_vars:
            reference = rf"\$(?:{re.escape(variable)}\b|\{{{re.escape(variable)}\}})"
            if re.search(rf"(?m)(?:>>?|\btee\b)[^\n]*{reference}", source):
                findings.append(f"shell variable {variable}: direct mutation")
    return findings


def test_carrier_scanner_catches_two_step_mutations(tmp_path: Path) -> None:
    python_counterfeit = tmp_path / "counterfeit.py"
    python_counterfeit.write_text(
        'from pathlib import Path\nledger = Path("queue") / "ledger.jsonl"\n'
        'ledger.open("a").write("{}\\n")\n'
    )
    shell_counterfeit = tmp_path / "counterfeit.sh"
    shell_counterfeit.write_text(
        'LEDGER="$ROOT/ledger.jsonl"\nprintf \'%s\\n\' "$event" >> "$LEDGER"\n'
    )
    reader = tmp_path / "reader.py"
    reader.write_text('from pathlib import Path\np = Path("ledger.jsonl")\np.open("rb").read()\n')

    assert _direct_ledger_mutations(python_counterfeit)
    assert _direct_ledger_mutations(shell_counterfeit)
    assert _direct_ledger_mutations(reader) == []


def test_production_tree_has_no_unregistered_direct_ledger_mutation() -> None:
    repo = PKG.parent
    inventory = json.loads((PKG / "ledger-writer-carriers.json").read_text())
    initializers = set(inventory["repository_initializers"])
    offenders: dict[str, list[str]] = {}
    for root in (repo / "pipeline", repo / "scripts"):
        for path in root.rglob("*"):
            if path.suffix not in {".py", ".sh"}:
                continue
            if "tests" in path.parts or path == PKG / "bridge" / "ledger_write.py":
                continue
            relative = str(path.relative_to(repo))
            if relative in initializers:
                continue
            # Other subsystems have their own `ledger.jsonl` files. This guard
            # targets the loopqueue source of truth, not every JSONL in the repo.
            if "loopqueue" not in path.read_text(errors="replace"):
                continue
            findings = _direct_ledger_mutations(path)
            if findings:
                offenders[relative] = findings
    assert offenders == {}


def test_installed_producers_use_the_serving_checkout_cli() -> None:
    inventory = json.loads((PKG / "ledger-writer-carriers.json").read_text())
    expected_cli = "/Users/youruser/OmniAgentOS/pipeline/bridge/ledger_write.py"
    offenders: dict[str, list[str]] = {}
    for raw in inventory["installed_producers"]:
        path = Path(raw)
        if not path.exists():
            continue
        source = path.read_text(errors="replace")
        findings = _direct_ledger_mutations(path)
        if expected_cli not in source:
            findings.append("absolute serving-checkout ledger CLI is absent")
        if findings:
            offenders[raw] = findings
    assert offenders == {}


def test_unsafe_rollover_is_fail_closed(queue: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ledger = queue / "ledger.jsonl"
    original = b'{"event":"observed","id":"kept"}\n'
    ledger.write_bytes(original)
    monkeypatch.setattr(janitor, "LEDGER_ROLL_BYTES", 1)

    sweep = janitor.Janitor(queue, apply=True)
    sweep.sweep()

    assert ledger.read_bytes() == original
    assert not list(queue.glob("ledger-*.jsonl"))
    assert any("rollover REFUSED" in action for action in sweep.actions)
    assert any("unsafe rollover is disabled" in alert for alert in sweep.alerts)


def _last_event(queue: Path) -> dict:
    return json.loads((queue / "ledger.jsonl").read_bytes().splitlines()[-1])


def test_append_stamps_ts_when_the_caller_omits_it(queue: Path) -> None:
    """The 4.7% of live events with no `ts` at all cannot recur through here."""

    lw.append_event(queue, {"role": "implementer", "event": "observed"})

    written = _last_event(queue)
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", written["ts"])
    assert "ts_claims" not in written


def test_append_clock_overrides_a_future_caller_ts_and_keeps_it_as_evidence(
    queue: Path,
) -> None:
    """The exact shape that fails the hang-recycler closed: a ts not yet reached.

    The recycler cannot date an event stamped in the future, so it stops
    verifying loop liveness. The claimed value is retained, not discarded --
    it is evidence about which writer invented it.
    """

    claimed = "2099-01-01T00:00:00Z"
    lw.append_event(
        queue, {"ts": claimed, "role": "implementer", "event": "observed"}
    )

    written = _last_event(queue)
    assert written["ts"] < claimed
    assert written["ts_claims"] == [claimed]


def test_stamped_ts_is_first_key_and_other_fields_survive(queue: Path) -> None:
    lw.append_event(
        queue,
        {
            "role": "implementer",
            "event": "merged",
            "id": "sha256:abc",
            "detail": {"merge_sha": "deadbeef"},
        },
    )

    written = _last_event(queue)
    assert next(iter(written)) == "ts"
    assert written["id"] == "sha256:abc"
    assert written["detail"] == {"merge_sha": "deadbeef"}
    assert written["event"] == "merged"


def test_every_appended_event_satisfies_the_schema_required_ts(queue: Path) -> None:
    """`ts` is schema-required; before this change nothing on the write path set it."""

    for event in ({}, {"event": "observed"}, {"ts": None, "role": "planner"}):
        lw.append_event(queue, dict(event))

    lines = (queue / "ledger.jsonl").read_bytes().splitlines()
    assert len(lines) == 3
    assert all(json.loads(line).get("ts") for line in lines)


def test_none_and_nonstring_claimed_ts_are_preserved_as_evidence(queue: Path) -> None:
    """Sol FINDING-1: a caller-supplied `ts` of None, an epoch float or an
    empty string is EVIDENCE about the writer. Silently erasing it makes the
    record indistinguishable from an honest no-ts append — a favourable-absence
    violation frozen into an append-only log. Every differing value survives
    in the `ts_claims` list, verbatim."""

    for claimed in (None, 1723600000.5, ""):
        lw.append_event(queue, {"ts": claimed, "role": "implementer", "event": "observed"})
        written = _last_event(queue)
        assert "ts_claims" in written, f"claimed ts {claimed!r} was silently dropped"
        assert written["ts_claims"] == [claimed]
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", written["ts"])


def test_ts_claims_list_is_extended_never_clobbered(queue: Path) -> None:
    """Sol rounds 2-3: any FIXED scalar key scheme is clobberable one nesting
    level deeper, so claims live in an append-only LIST. A caller-supplied
    ``ts_claims`` — list or scalar — is extended, never replaced, at any
    depth."""

    lw.append_event(queue, {"ts": "2001-01-01T00:00:00Z",
                            "ts_claims": ["1988-01-01T00:00:00Z",
                                          "1999-01-01T00:00:00Z"],
                            "role": "implementer", "event": "observed"})
    written = _last_event(queue)
    assert written["ts_claims"] == ["1988-01-01T00:00:00Z",
                                    "1999-01-01T00:00:00Z",
                                    "2001-01-01T00:00:00Z"]

    lw.append_event(queue, {"ts": "2002-01-01T00:00:00Z",
                            "ts_claims": "1977-01-01T00:00:00Z",
                            "role": "implementer", "event": "observed"})
    written = _last_event(queue)
    assert written["ts_claims"] == ["1977-01-01T00:00:00Z",
                                    "2002-01-01T00:00:00Z"]


def test_lying_eq_object_is_preserved_not_omitted() -> None:
    """Sol round-2 residual: only a STRING exactly equal to the fresh stamp may
    be omitted. A non-string whose __eq__ lies about equality is evidence and
    must survive as ts_claimed."""

    class LyingEq:
        def __eq__(self, other: object) -> bool:
            return True

        def __hash__(self) -> int:
            return 0

    out = lw.stamp_ts({"ts": LyingEq(), "event": "observed"})
    assert "ts_claims" in out

    class DifferingString(str):
        """Sol FINDING-5: a str SUBCLASS passes isinstance and can lie
        through __eq__; only exact built-in str may earn the omission."""

        def __eq__(self, other: object) -> bool:
            return True

        def __hash__(self) -> int:
            return 0

    out = lw.stamp_ts({"ts": DifferingString("1999-01-01T00:00:00Z"),
                       "event": "observed"})
    assert out["ts_claims"] == ["1999-01-01T00:00:00Z"]


def test_explicit_empty_ts_claims_survives(queue: Path) -> None:
    """An explicitly-supplied empty claims list is itself a statement and must
    reach the written record, not vanish."""

    lw.append_event(queue, {"ts_claims": [], "role": "implementer",
                            "event": "observed"})
    written = _last_event(queue)
    assert written["ts_claims"] == []


def test_lying_dict_subclass_cannot_hide_its_real_ts() -> None:
    """A dict subclass can lie through __contains__/get while json serializes
    its real storage; stamp_ts normalizes with dict() so preservation and
    serialization always see the same data."""

    class LyingDict(dict):
        def __contains__(self, key: object) -> bool:
            return False

        def get(self, key, default=None):  # type: ignore[override]
            return default

    evil = LyingDict({"ts": "1999-01-01T00:00:00Z", "event": "observed"})
    out = lw.stamp_ts(evil)
    assert out["ts_claims"] == ["1999-01-01T00:00:00Z"], (
        "the real underlying ts must be preserved despite lying accessors")

    class AccessorTrap(dict):
        """Sol FINDING-6: overriding __iter__ makes dict(x) fall back to the
        mapping protocol and call the subclass's own keys(); dict.copy(x)
        reads the real storage unconditionally."""

        def __iter__(self):
            return iter(())

        def keys(self):  # type: ignore[override]
            raise AssertionError("subclass keys() must never be consulted")

        def items(self):  # type: ignore[override]
            raise AssertionError("subclass items() must never be consulted")

        def get(self, key, default=None):  # type: ignore[override]
            return default

        def __contains__(self, key: object) -> bool:
            return False

    trap = AccessorTrap({"ts": "1998-01-01T00:00:00Z", "event": "observed"})
    out = lw.stamp_ts(trap)
    assert out["ts_claims"] == ["1998-01-01T00:00:00Z"], (
        "the real underlying ts must survive an __iter__-overriding subclass")
    assert out["event"] == "observed"

    class OnlyIter(list):
        """Grok FINDING-7: the list-side twin — list(x) iterates, which an
        __iter__ override can empty; list.copy(x) reads real storage."""

        def __iter__(self):
            return iter(())

        def copy(self):  # type: ignore[override]
            return ["fake"]

    hidden = OnlyIter(["1997-01-01T00:00:00Z"])
    out = lw.stamp_ts({"ts": "1996-01-01T00:00:00Z", "ts_claims": hidden,
                       "event": "observed"})
    assert out["ts_claims"] == ["1997-01-01T00:00:00Z",
                                "1996-01-01T00:00:00Z"], (
        "real prior claims must survive an __iter__-overriding list subclass")

    # Grok FINDING-8: the same lie NESTED as an element — the written BYTES
    # are what must carry the claim, so assert through a real append.
    nested = ["outer", OnlyIter(["1995-01-01T00:00:00Z"])]
    out = lw.stamp_ts({"ts": "1994-01-01T00:00:00Z", "ts_claims": nested,
                       "event": "observed"})
    assert out["ts_claims"][1] == ["1995-01-01T00:00:00Z"], (
        "a nested lying container must be materialized from real storage")
    assert type(out["ts_claims"][1]) is list, (
        "the nested container must be a PLAIN list so json serializes storage")


def test_clock_is_read_under_the_ledger_lock(
    queue: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sol FINDING-2: a writer that loses the lock race must also stamp later.

    The stamp is taken AFTER flock succeeds, so physical append order and `ts`
    order can never disagree — the property every reader that treats `ts` as
    append time (hang-recycler event-age basis, integrity, backlog census)
    depends on."""

    order: list[str] = []
    real_flock = lw.fcntl.flock

    def flock_spy(fd: int, op: int) -> None:
        if op == lw.fcntl.LOCK_EX:
            order.append("lock")
        real_flock(fd, op)

    real_now = lw._now_iso

    def now_spy() -> str:
        order.append("clock")
        return real_now()

    monkeypatch.setattr(lw.fcntl, "flock", flock_spy)
    monkeypatch.setattr(lw, "_now_iso", now_spy)
    lw.append_event(queue, {"event": "observed"})

    assert "lock" in order and "clock" in order, order
    assert order.index("lock") < order.index("clock"), (
        f"ts was stamped before the ledger lock was held: {order}"
    )
