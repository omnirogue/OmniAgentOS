"""Gate-integrity tests for the verdict ARTIFACT and the verdict LINE.

Two values are under test here, and each one travels through several carriers.
Testing one carrier of either value is how this defect class survived: the fix
for the artifact was applied in ``review-pump.sh`` and never propagated to
``dispatch-verifier.sh``; the fix for the line grammar was applied in
``merge-if-only-known-blocker.sh`` and never propagated to its three siblings.

VALUE 1 — the verdict ARTIFACT (``var/swarm/verdicts/<branch>.md``)
    Writers : dispatch-verifier.sh, review-integration.sh, integrate.sh (FAILED
              fallback), the verifier agent itself.
    Readers : integrate.sh, bulletin.sh, land-approved-lanes.py, standing-roles.sh
              (merge-if-only-known-blocker.sh DELETED 2026-08-13, operator ruling).
    Invariant tested: a dispatch that produces nothing must never leave a
    PREVIOUS run's verdict on disk to be read as this run's result.

VALUE 2 — the verdict LINE (the ``VERDICT: <value>`` grammar)
    Carriers: dispatch-verifier.sh, review-pump.sh, integrate.sh (x2 —
              partition and aggregate). merge-if-only-known-blocker.sh was a carrier
              until it was DELETED 2026-08-13 (operator ruling).
    Invariant tested: a decorative markdown heading is not a verdict.

The tests EXECUTE the real scripts against throwaway repositories. Nothing here
touches the production ledger, the serving checkout, or a live provider.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"
DISPATCH_VERIFIER = SCRIPTS / "dispatch-verifier.sh"


# ---------------------------------------------------------------------------
# LOAD HARDENING (2026-08-10) — the process table is a MACHINE-GLOBAL namespace
#
# review-pump.sh and verdict-pump.sh answer "is this lane already being worked?"
# with `pgrep -f "<tag> $LANE"`, and rework-pump.sh does the same. That question
# is asked of the whole machine, not of this checkout: any other process on the
# box whose command line carries the same lane name answers it. A HARDCODED lane
# name in a test therefore imports every other checkout's activity into this
# test's result — and this box runs the same suite from ~20 agent worktrees.
#
# MEASURED here, at parallelism 8 on a loaded box (identical load, only the lane
# name changed): 33 of 48 dispatches SKIPPED with a shared lane name, 0 of 48
# with a unique one. A skipped dispatch stages no verdict, so the assertion
# below fails exactly as it would if the verdict grammar had broken — the
# instrument reporting a candidate defect, which is the worst failure a gate has.
#
# The fix is to make the lane name unownable by anyone else: pid + random. It
# weakens nothing (the pump's grammar still has to refuse the header) and it
# removes this file from the class of tests whose answer depends on the box.
# ---------------------------------------------------------------------------
def _unique_lane(stem: str) -> str:
    """A lane name no concurrent process on this box can already be holding."""
    return f"{stem}-{os.getpid()}-{uuid.uuid4().hex[:8]}"


def _proc_report(label: str, proc: subprocess.CompletedProcess[str] | None) -> str:
    """The evidence a failed subprocess-driven assertion has to carry.

    `subprocess.run(capture_output=True)` had already collected the pump's
    stdout and stderr and the assertion threw them away, so the two gate
    refusals on 2026-08-10 reported `log: (none)` and nothing else — the
    instrument recorded that it had learned nothing. Never assert about a
    child process without printing what the child said.
    """
    if proc is None:
        return f"{label}: (never ran)"
    return (
        f"{label}: rc={proc.returncode}\n"
        f"--- {label} stdout ---\n{proc.stdout}\n"
        f"--- {label} stderr ---\n{proc.stderr}\n"
    )


def _file_report(label: str, path: Path) -> str:
    if not path.is_file():
        return f"--- {label} ({path}) --- (absent)\n"
    return f"--- {label} ({path}) ---\n{path.read_text(errors='replace')}\n"


def _run_to_files(
    command: list[str],
    *,
    env: dict[str, str],
    scratch: Path,
    timeout: float = 600.0,
) -> subprocess.CompletedProcess[str]:
    """`subprocess.run` that waits for the COMMAND, not for its orphans.

    Both pumps under test leave a process behind that inherits this call's
    stdio: dispatch-verifier.sh's watchdog is `( sleep $LIMIT; kill ... ) &`, and
    the `sleep` survives the `kill $WATCHDOG` that follows. With
    `capture_output=True` the parent then blocks on pipe EOF until that sleep
    ends — every run pays the whole watchdog budget even when the verifier
    answered in 50ms (measured: eight nodes at exactly 3.10s each). That is why
    the budgets in this file were seconds rather than minutes, and a
    seconds-long wall clock on a 24-core box running eight suite workers plus
    agents is a coin toss, not an assertion.

    Redirecting to files unbinds the two: the budget can be generous (it only
    binds a genuine hang) and the fast path stays fast.
    """
    scratch.mkdir(parents=True, exist_ok=True)
    stem = scratch / uuid.uuid4().hex
    out_path, err_path = Path(f"{stem}.out"), Path(f"{stem}.err")
    with out_path.open("w") as out_f, err_path.open("w") as err_f:
        proc = subprocess.run(
            command,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=out_f,
            stderr=err_f,
            text=True,
            timeout=timeout,
        )
    return subprocess.CompletedProcess(
        proc.args,
        proc.returncode,
        out_path.read_text(errors="replace"),
        err_path.read_text(errors="replace"),
    )


# Every script that parses a verdict line. Kept as data so a new carrier is
# added here rather than tested by hand in one place and forgotten in the rest.
#
# review-integration.sh was MISSING from this tuple while being a real carrier
# (it sources the grammar and calls verdict_decision/verdict_line). A hand-kept
# list is exactly as complete as the last person to remember it, which is why
# the structural check below scans the whole tree instead of reading this tuple.
VERDICT_LINE_CARRIERS = (
    "dispatch-verifier.sh",
    "review-pump.sh",
    "integrate.sh",
    # "merge-if-only-known-blocker.sh" DELETED 2026-08-13 by operator ruling: the
    # signed-receipt producer landed (receipts exist since 2026-07-30) and the bypass
    # this comment used to name had never once executed. Carrier 4/4 below (the tests
    # that shelled out to it) was removed with it.
    "review-integration.sh",
)

# Shell files that read a verdict with their OWN tolerant pattern, on purpose.
# Declared here so the omission is a decision on the record rather than an
# oversight, and pinned by test_the_unconverted_verdict_readers_never_merge.
UNCONVERTED_VERDICT_READERS = {
    "rework-pump.sh": (
        "decides who to QUEUE for rework, never what to merge. The shared grammar is "
        "STRICTER, so converting it would silently drop refused lanes it currently picks "
        "up — a missed rework, which is the wound this pump exists to close."
    ),
    "bulletin.sh": (
        "same pattern as rework-pump.sh, coupled to it deliberately (see the note at "
        "bulletin.sh:152); its grep is `&& continue`, so a false positive skips adoption."
    ),
}

# A line that reaches for a matcher AND names a verdict must be using the shared
# grammar. `VERDICT_*_RE`, verdict_decision, verdict_line — any of those is the
# one implementation; anything else is a second one.
_GRAMMAR_REFERENCE = re.compile(r"VERDICT_[A-Z_]+_RE|verdict_decision|verdict_line")
_MATCHER_WORD = re.compile(r"(?<![\w./-])(grep|awk|sed)(?![\w-])")
# Paths and variables are not patterns: `var/swarm/verdicts/*.md`, `$VERDICT`,
# `$VAR/sol-verdict.md` all name a FILE. Scrub them before asking whether the
# line mentions a verdict, or every read of the artifact looks like a parse.
_PATHISH = re.compile(
    r"\$\{?[A-Za-z_][A-Za-z0-9_]*\}?"
    r"|[A-Za-z0-9_.*/-]*verdicts?[A-Za-z0-9_.*/-]*\.md"
    r"|[A-Za-z0-9_.*/-]*/verdicts?/[A-Za-z0-9_.*/-]*",
    re.IGNORECASE,
)
# An explicit, greppable waiver for a line that mentions a verdict without
# deciding one (an operator diagnostic, say), written on the line itself or in
# the comment immediately above it. It does NOT license a decision: a waived
# line carrying a decision word still fails.
_WAIVER = "# not-a-decision:"
_DECISION_WORD = re.compile(r"APPROVE|REJECT|FAIL|MERGE", re.IGNORECASE)


def _hand_rolled_verdict_matchers(scripts_root: Path) -> list[tuple[str, int, str]]:
    """Every line in `scripts_root` that greps/awks/seds a VERDICT on its own.

    Takes the root as an argument so this check can be MUTATION-TESTED: point it
    at a copy of scripts/ with a defect re-armed and it must report the defect.
    A check that has never been shown to fail has not been shown to work.
    """
    findings: list[tuple[str, int, str]] = []
    for path in sorted(scripts_root.rglob("*.sh")):
        rel = path.relative_to(scripts_root).as_posix()
        if rel == "lib/verdict-grammar.sh":
            continue  # it IS the grammar
        if path.name in UNCONVERTED_VERDICT_READERS:
            continue
        waiver_above = False
        for lineno, raw in enumerate(path.read_text(errors="replace").splitlines(), 1):
            stripped = raw.strip()
            if stripped.startswith("#"):
                waiver_above = waiver_above or _WAIVER in raw
                continue  # a comment decides nothing
            waived, waiver_above = (waiver_above or _WAIVER in raw), False
            if not stripped or not _MATCHER_WORD.search(raw):
                continue
            scrubbed = _PATHISH.sub(" ", raw)
            if "verdict" not in scrubbed.lower():
                continue
            if _GRAMMAR_REFERENCE.search(raw):
                continue
            if waived and not _DECISION_WORD.search(scrubbed):
                continue
            findings.append((rel, lineno, stripped))
    return findings


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _make_lane(root: Path, lane: str, branch: str) -> Path:
    """A throwaway lane clone: a REAL git repo, on a REAL branch, with a brief."""
    lane_dir = root / "var" / "swarm" / "clones" / lane
    (lane_dir / "var").mkdir(parents=True)
    (lane_dir / "var" / "task.md").write_text("CONTRACT: throwaway lane\n")
    _git(lane_dir, "init", "-q", "-b", branch)
    _git(lane_dir, "add", "-A")
    _git(
        lane_dir,
        "-c",
        "user.email=test@example.invalid",
        "-c",
        "user.name=test",
        "commit",
        "-q",
        "-m",
        "lane fixture",
    )
    return lane_dir


def _fake_claude(bin_dir: Path, body: str) -> None:
    """Install a fake ``claude`` on PATH. No provider is contacted."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    cli = bin_dir / "claude"
    cli.write_text("#!/usr/bin/env bash\n" + body)
    cli.chmod(0o755)


def _run_dispatch(
    root: Path,
    lane: str,
    bin_dir: Path,
    *,
    verifier_timeout: str = "120",
) -> subprocess.CompletedProcess[str]:
    """Drive the real dispatch wrapper, with a watchdog that is generous AND free.

    The wrapper's watchdog is `( sleep $LIMIT; kill -TERM $PID ) &`. The `sleep`
    outlives the `kill $WATCHDOG` that follows the wait, and it INHERITS this
    call's stdio — so with `capture_output=True` (pipes) `subprocess.run` blocks
    on pipe EOF for the FULL watchdog budget on every run, however fast the
    verifier was. That is why the budget was 3 seconds: eight nodes in this file
    each paid it in full (measured 3.10s apiece, 25s of a 59s file).
    3s is also a wall clock this test does not own — on a contended box a fake
    CLI that is merely slow to be scheduled gets TERMed, and the wrapper then
    records a FAILED verdict that reads like the defect these tests hunt.
    Redirecting to FILES removes the coupling: `subprocess.run` now waits for the
    wrapper itself and not for its orphaned `sleep`, so the watchdog can be
    generous at zero cost when green. It still fires on a genuine hang (the outer
    timeout is 5x it, so the wrapper's own timeout path is what a hang exercises).
    """
    env = dict(os.environ)
    env["REPO"] = str(root)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    # An empty ACCOUNTS list falls back to CLAUDE_CONFIG_DIR; pointing HOME at the
    # sandbox keeps the real ~/.claude-account-* directories out of the test.
    env["HOME"] = str(root / "home")
    (root / "home").mkdir(exist_ok=True)
    env["VERIFIER_TIMEOUT"] = verifier_timeout
    return _run_to_files(["bash", str(DISPATCH_VERIFIER), lane], env=env, scratch=root / "capture")


STALE_APPROVE = """# Verdict — lane/stale

VERDICT: APPROVE

**Verifier:** opus (anthropic)
Written by a PREVIOUS run. Nothing in this run produced it.
"""


# ---------------------------------------------------------------------------
# VALUE 1 — the verdict artifact must not survive its own dispatch
# ---------------------------------------------------------------------------
def test_stale_approve_is_cleared_before_dispatch(tmp_path: Path) -> None:
    """A verifier that exits 0 writing nothing must NOT inherit a stale APPROVE.

    Before the fix the sequence was:
        BEFORE: VERDICT: APPROVE   (left by an earlier run)
        EXIT  = 0                  (fake verifier writes nothing)
        AFTER : VERDICT: APPROVE   (untouched)
    so the ``[ ! -s "$VERDICT" ]`` guard could not fire — it cannot tell "this
    run wrote nothing" from "the last run wrote something" — and the gate read
    an APPROVE that no verifier in this run produced.
    """
    lane, branch = _unique_lane("stale-lane"), "lane/stale"
    _make_lane(tmp_path, lane, branch)
    verdict = tmp_path / "var" / "swarm" / "verdicts" / "lane_stale.md"
    verdict.parent.mkdir(parents=True, exist_ok=True)
    verdict.write_text(STALE_APPROVE)

    # exits 0, writes nothing — the silent-success case the wrapper exists to catch
    _fake_claude(tmp_path / "bin", "exit 0\n")
    proc = _run_dispatch(tmp_path, lane, tmp_path / "bin")

    after = verdict.read_text()
    assert "VERDICT: APPROVE" not in after, (
        f"STALE APPROVE SURVIVED A DISPATCH THAT PRODUCED NOTHING.\nartifact after run:\n{after}"
    )
    assert after.startswith("# Verdict"), after
    assert "VERDICT: FAILED" in after, after
    # rc=1 is what verdict-pump.sh classifies as `crashed`; rc=0 would be
    # `completed`. A run that established nothing must never record completed.
    assert proc.returncode == 1, (proc.returncode, proc.stdout, proc.stderr)


def test_verdict_pump_maps_nonzero_dispatch_to_a_non_completed_end_reason() -> None:
    """Binding check: rc=1 from the wrapper is `crashed` in the ledger, not `completed`.

    The artifact assertion above is only meaningful if the pump records the
    wrapper's refusal. This pins the coupling in scripts/verdict-pump.sh rather
    than assuming it.
    """
    pump = (SCRIPTS / "verdict-pump.sh").read_text()
    classifier = (
        r"if [ $RC -eq 0 ]; then R=completed; "
        r"elif [ $RC -eq 3 ] || [ $RC -eq 124 ] || [ $RC -eq 137 ]; then R=timeout; "
        r"else R=crashed; fi"
    )
    normalised = " ".join(pump.replace("\\", "").split())
    assert " ".join(classifier.split()) in normalised, (
        "verdict-pump.sh no longer maps dispatch-verifier's exit code the way "
        "test_stale_approve_is_cleared_before_dispatch assumes"
    )


def test_healthy_verdict_is_not_destroyed_by_the_clear(tmp_path: Path) -> None:
    """POSITIVE CONTROL: clearing must not eat a verdict THIS run wrote.

    A fix that deleted the artifact unconditionally would pass the test above
    and break every real verification.
    """
    lane, branch = _unique_lane("good-lane"), "lane/good"
    _make_lane(tmp_path, lane, branch)
    verdict = tmp_path / "var" / "swarm" / "verdicts" / "lane_good.md"
    verdict.parent.mkdir(parents=True, exist_ok=True)
    verdict.write_text(STALE_APPROVE)

    _fake_claude(
        tmp_path / "bin",
        f'printf "# Verdict\\n\\nVERDICT: APPROVE\\n\\nVerifier: opus\\n" > "{verdict}"\nexit 0\n',
    )
    proc = _run_dispatch(tmp_path, lane, tmp_path / "bin")

    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    assert "VERDICT: APPROVE" in verdict.read_text()
    assert "Verifier: opus" in verdict.read_text()


def test_stale_artifact_is_cleared_even_when_the_verifier_crashes(tmp_path: Path) -> None:
    """The non-zero path must also refuse to inherit the previous verdict."""
    lane, branch = _unique_lane("crash-lane"), "lane/crash"
    _make_lane(tmp_path, lane, branch)
    verdict = tmp_path / "var" / "swarm" / "verdicts" / "lane_crash.md"
    verdict.parent.mkdir(parents=True, exist_ok=True)
    verdict.write_text(STALE_APPROVE)

    _fake_claude(tmp_path / "bin", "exit 7\n")
    proc = _run_dispatch(tmp_path, lane, tmp_path / "bin")

    after = verdict.read_text()
    assert "VERDICT: APPROVE" not in after, after
    assert "VERDICT: FAILED" in after, after
    assert proc.returncode == 1, (proc.returncode, proc.stdout, proc.stderr)


# ---------------------------------------------------------------------------
# VALUE 2 — a decorative markdown heading is not a verdict
#
# `vault/swarm/transcript-compilation-2026-07-29.md:232` records this already
# happening: "verdict-line extraction grabbed decorative headers for 2 lanes".
# The tolerant pattern `^\s*\**\s*#*\s*VERDICT` matches `# Verdict — lane/foo`,
# so the first line of a verdict file — its TITLE — was read as its verdict.
# ---------------------------------------------------------------------------
HEADER_ONLY = """# Verdict — lane/hdr

The reviewer wrote a title and then ran out of turn. There is no verdict here.
"""

# The dangerous shape: a TITLE that contains an approval word, over a body that
# rejects. Extraction by `head -1` on the tolerant pattern reads the title.
HEADER_SAYS_APPROVE_BODY_REJECTS = """# Verdict — approve-flow lane

VERDICT: REJECT

Verifier: opus (anthropic). The lane's own name contains the word the extractor
matched on; the actual decision is REJECT.
"""

HEADER_THEN_REAL_APPROVE = """# Verdict — mission

VERDICT: APPROVE

Verifier: opus (anthropic). Evidence attached.
"""


def test_dispatch_verifier_refuses_a_decorative_header(tmp_path: Path) -> None:
    """CARRIER 1/4 — dispatch-verifier.sh must not accept a title as a verdict."""
    lane, branch = _unique_lane("hdr-lane"), "lane/hdr"
    _make_lane(tmp_path, lane, branch)
    verdict = tmp_path / "var" / "swarm" / "verdicts" / "lane_hdr.md"
    verdict.parent.mkdir(parents=True, exist_ok=True)

    _fake_claude(
        tmp_path / "bin",
        f"cat > {verdict} <<'EOF'\n{HEADER_ONLY}EOF\nexit 0\n",
    )
    proc = _run_dispatch(tmp_path, lane, tmp_path / "bin")

    after = verdict.read_text()
    assert "VERDICT: FAILED" in after, (
        "a file whose only VERDICT-ish line is a markdown TITLE was accepted as a verdict\n"
        f"artifact:\n{after}\nstdout: {proc.stdout}"
    )
    assert "no VERDICT line" in after, after
    assert proc.returncode == 1, (proc.returncode, proc.stdout, proc.stderr)


# --- carrier 2/4: review-pump.sh -------------------------------------------
def _migrated_db(tmp_path: Path, name: str) -> Path:
    db = tmp_path / name
    subprocess.run(
        [str(REPO_ROOT / ".venv" / "bin" / "python"), "-m", "omniagentos.db.migrate", str(db)],
        check=True,
        capture_output=True,
        text=True,
    )
    return db


def _stub_repo(tmp_path: Path) -> Path:
    """A throwaway $REPO: real .venv (for $PY), real package, no production state.

    ``omniagentos`` is linked in for the same reason ``_pump_env`` links it, and
    the omission cost a train. review-pump.sh does ``cd "$REPO"`` and then
    ``$PY -m omniagentos.swarm.dal`` — and ``-m`` puts the CWD FIRST on
    ``sys.path``, ahead of ``PYTHONPATH`` and site-packages. With no package
    under the stub, that import fell through to whatever tree the ambient
    environment named: on this estate ``PYTHONPATH`` is
    ``/Users/youruser/OmniAgentOS`` and the gate workspace's ``.venv`` is
    a SYMLINK to the serving checkout's, whose editable ``.pth`` names the same
    path. So the pump's preflight was importing the LIVE SERVING CHECKOUT — the
    tree the gate loop re-pins and thirty worktrees write to — while the suite
    graded a candidate somewhere else. An import that races a re-pin there fails,
    the preflight exits 1 before its first log line, and the gate reports
    ``review-pump produced no staged verdict`` against innocent code.

    REPRODUCED exactly, byte for byte with the train's junit: make the ambient
    tree unresolvable and the pump dies rc=1, stdout empty, stderr
    ``FATAL(preflight): swarm ledger CLI unusable (omniagentos.swarm.dal)``,
    REVIEW_LOG absent. With the link below the same sabotage changes nothing,
    because the pump now resolves the package from the tree under test — which
    is also the only tree this suite is entitled to grade.

    ``scripts`` is deliberately NOT linked: review-pump's only use of it is a
    fire-and-forget ``fleet-ledger.py scan`` whose output and status it discards,
    so linking it would add real work to every run and change nothing that is
    asserted. verdict-pump genuinely execs ``./scripts/dispatch-verifier.sh``,
    which is why ``_pump_env`` links it and this does not.
    """
    root = tmp_path / "repo"
    (root / "var" / "swarm").mkdir(parents=True)
    (root / ".venv").symlink_to(REPO_ROOT / ".venv")
    (root / "omniagentos").symlink_to(REPO_ROOT / "omniagentos")
    return root


def _warm_swarm_dal(root: Path, env: dict[str, str], *, attempts: int = 3) -> None:
    """Run the pump's OWN preflight command first, and make its failure speak.

    review-pump.sh:89 and verdict-pump.sh:78 both gate on
    ``$PY -m omniagentos.swarm.dal pump-hash`` with ``>/dev/null 2>&1`` and then
    exit 1 with ``FATAL(preflight): swarm ledger CLI unusable``. The refusal is
    fail-closed and correct; what it cannot do is say WHY, so when it reached a
    gate on 2026-08-10 the only evidence was that the pump had not run.

    Doing it here first, through the SAME interpreter path, cwd and environment
    the pump will use, means (a) any one-off cost of the first import on a cold
    workspace is paid outside the pump's fail-closed window, (b) a transient
    failure gets two more chances before it can red a train, and (c) a genuine
    breakage is reported as the Python error it actually is, on the first
    occurrence, instead of a slug with no log. It cannot hide a defect: a module
    that is really broken fails all three attempts and fails the test.
    """
    py = root / ".venv" / "bin" / "python"
    command = [
        str(py),
        "-W",
        "ignore::RuntimeWarning",
        "-m",
        "omniagentos.swarm.dal",
        "pump-hash",
        "--verdict-hash",
        "warmup",
    ]
    reports: list[str] = []
    for attempt in range(1, attempts + 1):
        proc = subprocess.run(
            command, cwd=root, env=env, capture_output=True, text=True, timeout=300
        )
        if proc.returncode == 0:
            return
        reports.append(_proc_report(f"swarm-dal warm-up attempt {attempt}", proc))
    raise AssertionError(
        "the pump's own preflight command cannot run, so this test could only "
        "ever report `FATAL(preflight): swarm ledger CLI unusable` with no log. "
        f"The real error, over {attempts} attempts:\n" + "\n".join(reports)
    )


def test_review_pump_refuses_a_decorative_header(tmp_path: Path) -> None:
    """CARRIER 2/4 — review-pump.sh's sol-verdict check, driven through the real pump."""
    if not (REPO_ROOT / ".venv" / "bin" / "python").is_file():
        pytest.skip("no venv")
    root = _stub_repo(tmp_path)
    lane = _unique_lane("hdr-review-lane")
    lane_dir = root / "var" / "swarm" / "clones" / lane
    (lane_dir / ".git").mkdir(parents=True)
    (lane_dir / "var").mkdir(parents=True)
    (lane_dir / "var" / "task.md").write_text("CONTRACT: throwaway lane\n")
    stage = root / "var" / "swarm" / "sol-verdicts"
    stage.mkdir(parents=True)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    codex = bin_dir / "codex"
    codex.write_text(
        "#!/usr/bin/env bash\n"
        "cat > /dev/null\n"  # drain the prompt on stdin
        'printf "%s" "$FAKE_SOL_BODY" > "$FAKE_SOL_PATH"\n'
        "exit 0\n"
    )
    codex.chmod(0o755)

    log = tmp_path / "review-pump.log"
    inner = lane_dir / "var" / "sol-verdict.md"
    env = dict(os.environ)
    env.update(
        REPO=str(root),
        PATH=f"{bin_dir}:{env['PATH']}",
        OMNIAGENTOS_DB=str(_migrated_db(tmp_path, "review.sqlite3")),
        REVIEW_STAGE=str(stage),
        REVIEW_CLONES=str(root / "var" / "swarm" / "clones"),
        REVIEW_QUARANTINE=str(root / "var" / "swarm" / "quarantine"),
        REVIEW_LOG=str(log),
        REVIEW_CYCLES="1",
        # The pump's free-slot count is `REVIEW_MAX - pgrep -f 'sol-review-lane'`,
        # counted across the WHOLE MACHINE. State the cap instead of inheriting a
        # default that another checkout's reviewers can eat into.
        REVIEW_MAX="4096",
        # Generous on purpose: this is the wrapper's hard cap on a PROVIDER hang,
        # and the provider here is a local bash script that returns in
        # milliseconds. A tight budget cannot make this test more correct — it
        # can only convert host contention into a false "the grammar broke".
        REVIEW_DISPATCH_TIMEOUT_S="300",
        REVIEW_QUEUE_CMD=f"echo {lane}",
        REWORK_DRY_RUN="0",
        FAKE_SOL_PATH=str(inner),
        FAKE_SOL_BODY=HEADER_ONLY,
    )
    _warm_swarm_dal(root, env)
    proc = subprocess.run(
        ["bash", str(SCRIPTS / "review-pump.sh")],
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )

    out = stage / f"{lane}.md"
    # The pump backgrounds review_exec and exits; `subprocess.run` above already
    # waited for that child (it holds the captured pipes). This is a belt-and-
    # braces wait, so it polls cheaply rather than forking 60 `sleep` processes
    # onto a box that is already short of cores.
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if out.is_file() and out.read_text().strip():
            break
        time.sleep(0.1)

    def _evidence() -> str:
        return (
            _proc_report("review-pump", proc)
            + _file_report("REVIEW_LOG", log)
            + _file_report("lane sol-review.log", lane_dir / "var" / "sol-review.log")
            + _file_report("lane sol-verdict.md", inner)
            + f"--- stage dir {stage} ---\n"
            + "\n".join(sorted(p.name for p in stage.iterdir()))
            + "\n"
        )

    assert out.is_file(), "review-pump produced no staged verdict\n" + _evidence()
    # A dispatch that never reached the reviewer ALSO writes `VERDICT: FAILED`
    # (review_exec's rc!=0 fallback), so the line below would pass on a crashed
    # run without this: the refusal under test is the one the GRAMMAR makes about
    # a header the reviewer really wrote.
    assert inner.is_file() and inner.read_text().strip() == HEADER_ONLY.strip(), (
        "the fake reviewer never wrote its header-only verdict, so a FAILED "
        "artifact here would prove nothing about the grammar\n" + _evidence()
    )
    body = out.read_text()
    assert "VERDICT: FAILED" in body, (
        "review-pump accepted a markdown TITLE as a first-pass verdict\n" + body + _evidence()
    )


# --- carrier 3/4: integrate.sh (partition + aggregate) ----------------------
def _integrate_repo(tmp_path: Path) -> Path:
    root = _stub_repo(tmp_path)
    (root / "scripts").mkdir()
    gate = root / "scripts" / "merge-gate.sh"
    gate.write_text("#!/usr/bin/env bash\necho 'fake gate: not run in tests'\nexit 0\n")
    gate.chmod(0o755)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "test")
    (root / "README.md").write_text("integrate fixture\n")
    _git(root, "add", "README.md")
    _git(root, "commit", "-q", "-m", "base")
    return root


def _integrate_lane(root: Path, lane: str, branch: str, verdict_body: str) -> None:
    lane_dir = root / "var" / "swarm" / "clones" / lane
    lane_dir.mkdir(parents=True)
    (lane_dir / "file.txt").write_text("lane content\n")
    _git(lane_dir, "init", "-q", "-b", branch)
    _git(lane_dir, "config", "user.email", "test@example.invalid")
    _git(lane_dir, "config", "user.name", "test")
    _git(lane_dir, "add", "-A")
    _git(lane_dir, "commit", "-q", "-m", "lane")
    stage = root / "var" / "swarm" / "sol-verdicts"
    stage.mkdir(parents=True, exist_ok=True)
    (stage / f"{lane}.md").write_text(verdict_body)


def _run_integrate(root: Path) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.update(REPO=str(root), DRY_RUN="1", INTEGRATION_BRANCH="integration/test")
    return subprocess.run(
        ["bash", str(SCRIPTS / "integrate.sh")],
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )


def test_integrate_reads_the_verdict_line_not_the_title(tmp_path: Path) -> None:
    """CARRIER 3/4a — integrate.sh's partition took `head -1` of the tolerant match.

    A verdict whose file opens with `# Verdict — mission` had its TITLE read as
    its decision, so a genuine APPROVE partitioned as `failed`.
    """
    root = _integrate_repo(tmp_path)
    _integrate_lane(root, "good-lane", "lane/good", HEADER_THEN_REAL_APPROVE)
    proc = _run_integrate(root)
    assert "verdicts: approved=1 rejected=0 failed=0" in proc.stdout, (
        "a titled verdict file did not partition on its VERDICT line\n" + proc.stdout
    )


def test_integrate_does_not_approve_on_a_title_containing_approve(tmp_path: Path) -> None:
    """CARRIER 3/4a — the FAIL-OPEN direction: a title must never carry the decision."""
    root = _integrate_repo(tmp_path)
    _integrate_lane(root, "rej-lane", "lane/rej", HEADER_SAYS_APPROVE_BODY_REJECTS)
    proc = _run_integrate(root)
    assert "verdicts: approved=0 rejected=1 failed=0" in proc.stdout, (
        "a REJECT was partitioned as approved because its TITLE contained 'approve'\n" + proc.stdout
    )


# CARRIER 4/4 (merge-if-only-known-blocker.sh) DELETED 2026-08-13 by operator ruling:
# the signed-receipt producer landed (receipts exist since 2026-07-30) and the bypass
# had never once executed. Its four carrier tests (accepts a heading-form approval,
# refuses a title-only approve, refuses a REJECT) went with it — nothing left to shell
# out to. The other three carriers below (integrate.sh's title-vs-body precedence) are
# unaffected and still pinned.


# ---------------------------------------------------------------------------
# PROPAGATION — the grammar must be shared, not copied into four files
# ---------------------------------------------------------------------------
def test_every_carrier_uses_the_one_shared_grammar() -> None:
    """The defect being fixed IS incomplete propagation. Pin it structurally.

    A future edit to one carrier's inline regex is the exact way this recurs, so
    there must be no inline regex left to edit.
    """
    lib = SCRIPTS / "lib" / "verdict-grammar.sh"
    assert lib.is_file(), "scripts/lib/verdict-grammar.sh is missing"
    for name in VERDICT_LINE_CARRIERS:
        text = (SCRIPTS / name).read_text()
        assert "lib/verdict-grammar.sh" in text, f"{name} does not source the shared grammar"
        assert "#*\\s*VERDICT" not in text, f"{name} still carries the tolerant inline pattern"
        assert "[[:space:]]*VERDICT[[:space:]]*[:=-]" not in text, (
            f"{name} still carries a hand-rolled verdict pattern"
        )


def test_the_production_bash_grammar_lets_a_refusal_outrank_an_approval(
    tmp_path: Path,
) -> None:
    """Refusal precedence, pinned directly against the LIVE bash grammar.

    merge-if-only-known-blocker.sh (deleted 2026-08-13) was the only carrier that
    exercised this invariant through a real subprocess of scripts/lib/verdict-grammar.sh;
    the parametrized precedence table below this point only drives the UNWIRED Python
    parser in verdicts.py, whose own docstring records that no production consumer of it
    has landed. Without this test, "a refusal anywhere outranks an approval anywhere" was
    pinned against code nothing in production calls. Source the grammar directly instead
    of resurrecting a wrapper script.
    """
    lib = SCRIPTS / "lib" / "verdict-grammar.sh"

    def decide(body: str) -> str:
        artifact = tmp_path / f"verdict-{uuid.uuid4().hex}.md"
        artifact.write_text(body)
        proc = subprocess.run(
            ["bash", "-c", f'. "{lib}" && verdict_decision "$1"', "--", str(artifact)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.returncode == 0, proc.stderr
        return proc.stdout.strip()

    approve_then_reject = decide("VERDICT: APPROVE\n\nOn reflection:\nVERDICT: REJECT\n")
    assert approve_then_reject == "REJECT", (
        "an APPROVE followed by a later REJECT did not decide REJECT in the production "
        f"bash grammar (got {approve_then_reject!r})"
    )

    reject_then_approve = decide("VERDICT: REJECT\n\nOn reflection:\nVERDICT: APPROVE\n")
    assert reject_then_approve == "REJECT", (
        "a REJECT followed by a later APPROVE did not decide REJECT in the production "
        f"bash grammar (got {reject_then_approve!r}) — order must never matter, only "
        "presence"
    )


def test_no_shell_carrier_extracts_a_verdict_with_its_own_matcher() -> None:
    """The PROPERTY, because the blocklist above does not bite.

    The two asserts in the previous test name two exact literal strings. Mutation
    testing said what that is worth: four re-armed defects were copied into a
    throwaway `scripts/` tree and 4 of 4 passed undetected, including the exact
    one this lane removed —

        grep -q "VERDICT: APPROVE" "$V" && MERGE=1
        grep -iE '^VERDICT[:=-]' "$f" | head -1
        grep -qiE '^\\**[[:space:]]*VERDICT.*APPROVE'
        awk '/^VERDICT/ && /APPROVE/ {ok=1}'

    A blocklist of literals is also structurally incapable of finding a pattern
    built from a variable or a capture group — which is why `fleet-ledger.py`
    never appeared in it at all. So the check is now a property over the whole
    tree, not a list of spellings in a hand-kept tuple: reaching for grep/awk/sed
    on a line that names a verdict is only allowed when that same line names the
    shared grammar.
    """
    findings = _hand_rolled_verdict_matchers(SCRIPTS)
    assert not findings, "verdict parsed by a private matcher:\n" + "\n".join(
        f"  {rel}:{lineno}  {line}" for rel, lineno, line in findings
    )


def test_the_unconverted_verdict_readers_never_merge() -> None:
    """The declared exceptions are allowed to QUEUE work. They may never land it.

    rework-pump.sh and bulletin.sh keep a tolerant hand-rolled pattern on purpose
    (the shared grammar is stricter, and dropping a refused lane is worse here
    than picking up an extra one). That reasoning holds only while neither can
    merge, so pin it rather than trusting the comment.
    """
    for name in UNCONVERTED_VERDICT_READERS:
        text = (SCRIPTS / name).read_text()
        code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
        assert "merge-gate.sh" not in code, f"{name} reaches the merge gate"
        assert not re.search(r"\bgit\b[^\n|;&]*\bmerge\b", code), f"{name} runs git merge"
        assert "land-approved-lanes" not in code, f"{name} reaches the landing script"


# ---------------------------------------------------------------------------
# VALUE 1, degenerate carrier — a verdict must never land at a path that
# collides or that no reader can see
#
# `git rev-parse --abbrev-ref HEAD` yields "" for an unborn branch or a corrupt
# clone and the literal "HEAD" when detached. The artifact then lands at
# `var/swarm/verdicts/.md` — a DOTFILE that integrate.sh:48's `*.md` glob does
# not match — or at `HEAD.md`, which EVERY detached lane overwrites.
# `var/swarm/verdicts/HEAD.md` exists in the live repo, so this is not theory.
# ---------------------------------------------------------------------------
def test_unresolvable_branch_refuses_before_dispatch(tmp_path: Path) -> None:
    """A clone whose HEAD does not resolve must be refused, not written to `.md`."""
    lane = _unique_lane("broken-lane")
    lane_dir = tmp_path / "var" / "swarm" / "clones" / lane
    (lane_dir / ".git").mkdir(parents=True)  # looks like a clone, resolves to nothing
    (lane_dir / "var").mkdir(parents=True)

    marker = tmp_path / "verifier-was-invoked"
    _fake_claude(tmp_path / "bin", f'touch "{marker}"\nexit 0\n')
    proc = _run_dispatch(tmp_path, lane, tmp_path / "bin")

    assert proc.returncode == 2, (proc.returncode, proc.stdout, proc.stderr)
    assert not (tmp_path / "var" / "swarm" / "verdicts" / ".md").exists(), (
        "a verdict was written to the degenerate dotfile path"
    )
    assert not marker.exists(), "a verifier was dispatched for a lane with no branch"
    assert "branch" in (proc.stdout + proc.stderr).lower(), proc.stdout + proc.stderr


def test_detached_head_refuses_rather_than_colliding_on_head_md(tmp_path: Path) -> None:
    """Detached HEAD gives the literal branch name "HEAD" — a shared, colliding path."""
    lane = _unique_lane("detached-lane")
    lane_dir = _make_lane(tmp_path, lane, "lane/detached")
    _git(lane_dir, "checkout", "-q", "--detach", "HEAD")

    marker = tmp_path / "verifier-was-invoked"
    _fake_claude(tmp_path / "bin", f'touch "{marker}"\nexit 0\n')
    proc = _run_dispatch(tmp_path, lane, tmp_path / "bin")

    assert proc.returncode == 2, (proc.returncode, proc.stdout, proc.stderr)
    assert not (tmp_path / "var" / "swarm" / "verdicts" / "HEAD.md").exists(), (
        "a detached lane wrote the shared HEAD.md artifact every other detached lane uses"
    )
    assert not marker.exists(), "a verifier was dispatched for a detached lane"


def test_a_named_branch_still_dispatches(tmp_path: Path) -> None:
    """POSITIVE CONTROL: the guard must refuse degenerate names, not all names."""
    lane = _unique_lane("named-lane")
    _make_lane(tmp_path, lane, "lane/named")
    verdict = tmp_path / "var" / "swarm" / "verdicts" / "lane_named.md"

    _fake_claude(
        tmp_path / "bin",
        f'printf "VERDICT: APPROVE\\n\\nopus\\n" > "{verdict}"\nexit 0\n',
    )
    proc = _run_dispatch(tmp_path, lane, tmp_path / "bin")
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    assert verdict.is_file()


# ---------------------------------------------------------------------------
# The fixes' OWN abnormal branches must also be distinguishable from healthy
# ---------------------------------------------------------------------------
def test_an_empty_grammar_refuses_to_run(tmp_path: Path) -> None:
    """A truncated grammar library leaves an EMPTY pattern, which matches every line.

    That is the most fail-open state this change could introduce, so the carriers
    check that the patterns loaded AND are non-empty rather than assuming it.
    """
    fake_scripts = tmp_path / "scripts"
    (fake_scripts / "lib").mkdir(parents=True)
    (fake_scripts / "dispatch-verifier.sh").write_text(DISPATCH_VERIFIER.read_text())
    (fake_scripts / "lib" / "verdict-grammar.sh").write_text("# truncated\n")

    lane = _unique_lane("grammar-lane")
    _make_lane(tmp_path, lane, "lane/grammar")
    env = dict(os.environ)
    env.update(REPO=str(tmp_path), HOME=str(tmp_path))
    proc = subprocess.run(
        ["bash", str(fake_scripts / "dispatch-verifier.sh"), lane],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 1, (proc.returncode, proc.stdout, proc.stderr)
    assert "verdict grammar unusable" in proc.stderr, proc.stderr


def test_a_stale_artifact_that_cannot_be_cleared_refuses(tmp_path: Path) -> None:
    """A clear that silently FAILED leaves exactly the state the clear exists to remove."""
    lane = _unique_lane("locked-lane")
    _make_lane(tmp_path, lane, "lane/locked")
    verdicts = tmp_path / "var" / "swarm" / "verdicts"
    verdicts.mkdir(parents=True)
    stale = verdicts / "lane_locked.md"
    stale.write_text(STALE_APPROVE)
    verdicts.chmod(0o555)  # unlink in this directory is now denied
    try:
        marker = tmp_path / "verifier-was-invoked"
        _fake_claude(tmp_path / "bin", f'touch "{marker}"\nexit 0\n')
        proc = _run_dispatch(tmp_path, lane, tmp_path / "bin")
        assert proc.returncode == 2, (proc.returncode, proc.stdout, proc.stderr)
        assert "cannot clear the previous verdict" in proc.stderr, proc.stderr
        assert not marker.exists(), "a verifier ran with a stale APPROVE still on disk"
        assert stale.read_text() == STALE_APPROVE
    finally:
        verdicts.chmod(0o755)


# ---------------------------------------------------------------------------
# MGI-001 — a separator is not a discriminator; the VALUE is
#
# Sol's repro, reproduced against the real gate at 2514adf1: this file returned
# rc=0 and MERGED the lane, logging `Anthropic verdict: Verdict: Approve-flow
# lane`. An ordinary human title with a colon whose first word STARTS with
# "approve" beat the `VERDICT: REJECT` below it, because values were matched by
# prefix and the decision was taken from the first matching line.
# ---------------------------------------------------------------------------
TITLE_APPROVE_OVER_REJECT = """# Verdict: Approve-flow lane

VERDICT: REJECT

Verifier: opus (anthropic)
"""


def test_integrate_refuses_a_title_that_outranks_a_reject(tmp_path: Path) -> None:
    """MGI-001 reaches the partition too — same value, different carrier."""
    root = _integrate_repo(tmp_path)
    _integrate_lane(root, "title-lane", "lane/title", TITLE_APPROVE_OVER_REJECT)
    proc = _run_integrate(root)
    assert "verdicts: approved=0 rejected=1 failed=0" in proc.stdout, proc.stdout


@pytest.mark.parametrize(
    ("value", "decision"),
    [
        ("APPROVE", "APPROVE"),
        ("APPROVE-WITH-NOTES", "APPROVE"),  # 61 live files
        ("approve-with-notes", "APPROVE"),
        ("APPROVED", "APPROVE"),  # 1 live file
        ("**APPROVE-WITH-NOTES**", "APPROVE"),  # 10 live files
        ("APPROVE (with the notes below)", "APPROVE"),
        ("REJECT", "REJECT"),
        ("FAILED (verifier did not establish a claim)", "FAILED"),
        ("Approve-flow lane", "NONE"),  # MGI-001: a title, not a token
        ("Approved-by-nobody thing", "NONE"),
        ("APPROVEMENT", "NONE"),
        ("MAYBE", "NONE"),
        ("", "NONE"),
    ],
)
def test_verdict_value_is_matched_as_a_whole_token(
    tmp_path: Path, value: str, decision: str
) -> None:
    """The token boundary, stated as a table so a future edit has to face all of it."""
    f = tmp_path / "v.md"
    f.write_text(f"# Verdict: {value}\n")
    got = subprocess.run(
        [
            "bash",
            "-c",
            f'. "{SCRIPTS / "lib" / "verdict-grammar.sh"}"; verdict_decision "{f}"',
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert got == decision, f"`# Verdict: {value}` -> {got}, expected {decision}"


# ---------------------------------------------------------------------------
# MGI-002 — the sibling carrier I enumerated as a writer and wrongly cleared
#
# `review-integration.sh` had the SAME silent-success defect as
# dispatch-verifier.sh. I judged it safe because it "copies inner->outer only on
# a parseable line" — but a STALE line parses perfectly. The reviewer is
# sandboxed and writes inside its worktree, so a previous run's inner artifact
# sits exactly where this run's reviewer was told to write, and a reviewer that
# exits 0 writing nothing had its predecessor's APPROVE copied into the
# canonical artifact merge-gate.sh reads. This was live on main.
# ---------------------------------------------------------------------------
def _review_tree(tmp_path: Path, branch: str = "integration/test") -> Path:
    tree = tmp_path / "review-tree"
    tree.mkdir()
    _git(tree, "init", "-q", "-b", "main")
    _git(tree, "config", "user.email", "test@example.invalid")
    _git(tree, "config", "user.name", "test")
    (tree / "base.txt").write_text("base\n")
    _git(tree, "add", "-A")
    _git(tree, "commit", "-q", "-m", "base")
    _git(tree, "checkout", "-q", "-b", branch)
    (tree / "lane.txt").write_text("lane\n")
    _git(tree, "add", "-A")
    _git(tree, "commit", "-q", "-m", "lane work")
    return tree


def _run_review_integration(
    tmp_path: Path, tree: Path, branch: str = "integration/test"
) -> subprocess.CompletedProcess[str]:
    root = tmp_path / "repo"
    root.mkdir(exist_ok=True)
    env = dict(os.environ)
    env.update(
        REPO=str(root),
        PATH=f"{tmp_path / 'bin'}:{env['PATH']}",
        HOME=str(tmp_path / "home"),
    )
    (tmp_path / "home").mkdir(exist_ok=True)
    return subprocess.run(
        ["bash", str(SCRIPTS / "review-integration.sh"), branch, str(tree)],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_review_integration_does_not_reuse_a_stale_inner_verdict(tmp_path: Path) -> None:
    """MGI-002: a silent aggregate reviewer must not inherit the last run's APPROVE."""
    tree = _review_tree(tmp_path)
    (tree / "var-verdict.md").write_text("VERDICT: APPROVE\n\nReviewer: opus (anthropic)\n")
    _fake_claude(tmp_path / "bin", "exit 0\n")

    _run_review_integration(tmp_path, tree)

    canonical = tmp_path / "repo" / "var" / "swarm" / "verdicts" / "integration_test.md"
    body = canonical.read_text() if canonical.exists() else ""
    assert "VERDICT: APPROVE" not in body, (
        "a stale aggregate APPROVE survived a run that established nothing\n" + body
    )
    assert "VERDICT: FAILED" in body, body


def test_review_integration_records_a_verdict_the_current_run_wrote(tmp_path: Path) -> None:
    """POSITIVE CONTROL: clearing must not stop a real aggregate review recording."""
    tree = _review_tree(tmp_path)
    (tree / "var-verdict.md").write_text("VERDICT: APPROVE\n\nstale, from a previous run\n")
    _fake_claude(
        tmp_path / "bin",
        f'printf "VERDICT: REJECT\\n\\nopus: seam defect at foo.py\\n" > "{tree / "var-verdict.md"}"\nexit 0\n',
    )

    _run_review_integration(tmp_path, tree)

    canonical = tmp_path / "repo" / "var" / "swarm" / "verdicts" / "integration_test.md"
    body = canonical.read_text()
    assert "VERDICT: REJECT" in body, body
    assert "seam defect at foo.py" in body, body


# ---------------------------------------------------------------------------
# MGI-003 — the clear is correct sequentially and wrong concurrently
#
# My positive control only covered ONE dispatch at a time. With two overlapping
# dispatches for a lane, the second one's pre-dispatch clear DELETED the first
# one's freshly written genuine APPROVE, and the surviving artifact was the
# second one's FAILED: a real verdict destroyed and replaced by a false negative.
# ---------------------------------------------------------------------------
def test_an_overlapping_dispatch_is_refused_before_it_mutates_anything(
    tmp_path: Path,
) -> None:
    """MGI-003: verifier A holds; B must refuse at exit 2 without touching the artifact."""
    lane = _unique_lane("race-lane")
    _make_lane(tmp_path, lane, "lane/race")
    verdict = tmp_path / "var" / "swarm" / "verdicts" / "lane_race.md"
    wrote = tmp_path / "a-wrote"
    release = tmp_path / "release-a"

    # A writes a genuine APPROVE and then blocks until the test releases it.
    _fake_claude(
        tmp_path / "bin",
        'if [ "$FAKE_ROLE" = A ]; then\n'
        f'  printf "VERDICT: APPROVE\\n\\nReviewer: opus (anthropic)\\n" > "{verdict}"\n'
        f'  touch "{wrote}"\n'
        f'  while [ ! -e "{release}" ]; do sleep 0.05; done\n'
        "fi\n"
        "exit 0\n",
    )

    env = dict(os.environ)
    env.update(
        REPO=str(tmp_path),
        PATH=f"{tmp_path / 'bin'}:{env['PATH']}",
        HOME=str(tmp_path / "home"),
        # A must still be HOLDING the lane when B arrives; the watchdog is what
        # ends A. 20s was the whole budget for "start A, see its verdict, start
        # B" on a box that also runs eight suite workers — miss it and A is
        # TERMed, B finds no lock, and the artifact-mutation defect this test
        # hunts is reported against innocent code. Generous costs nothing here:
        # A's output goes to a file, so nothing waits on the watchdog's orphan.
        VERIFIER_TIMEOUT="300",
    )
    (tmp_path / "home").mkdir(exist_ok=True)
    capture = tmp_path / "capture"
    capture.mkdir(exist_ok=True)
    a_log = capture / "verifier-a.log"

    a_sink = a_log.open("w")
    a = subprocess.Popen(
        ["bash", str(DISPATCH_VERIFIER), lane],
        env=dict(env, FAKE_ROLE="A"),
        stdin=subprocess.DEVNULL,
        stdout=a_sink,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.monotonic() + 120
        while not wrote.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert wrote.exists(), "first verifier never wrote its healthy verdict\n" + _file_report(
            "verifier A", a_log
        )

        b = _run_to_files(
            ["bash", str(DISPATCH_VERIFIER), lane],
            env=dict(env, FAKE_ROLE="B"),
            scratch=capture,
        )
        assert b.returncode == 2, (
            "an overlapping dispatch was not refused before artifact mutation",
            b.returncode,
            b.stdout,
            b.stderr,
        )
        assert "already being verified" in b.stderr, b.stderr
        assert "VERDICT: APPROVE" in verdict.read_text(), (
            "the second dispatch deleted a freshly written verdict"
        )
    finally:
        release.touch()
        a.wait(timeout=120)
        a_sink.close()


def test_the_lock_is_released_on_a_normal_run(tmp_path: Path) -> None:
    """A lock that outlives its run is an outage. Release on every exit path."""
    lane = _unique_lane("lock-lane")
    _make_lane(tmp_path, lane, "lane/lock")
    verdict = tmp_path / "var" / "swarm" / "verdicts" / "lane_lock.md"
    _fake_claude(
        tmp_path / "bin", f'printf "VERDICT: APPROVE\\n\\nopus\\n" > "{verdict}"\nexit 0\n'
    )
    proc = _run_dispatch(tmp_path, lane, tmp_path / "bin")
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    assert not (tmp_path / "var" / "swarm" / "verdict-locks" / "lane_lock.lock").exists()


def test_a_lock_held_by_a_dead_process_is_reclaimed(tmp_path: Path) -> None:
    """The guard needs its own way back: a holder that DIED must not freeze the lane."""
    lane = _unique_lane("stale-lock-lane")
    _make_lane(tmp_path, lane, "lane/stalelock")
    lock = tmp_path / "var" / "swarm" / "verdict-locks" / "lane_stalelock.lock"
    lock.mkdir(parents=True)
    # A pid that cannot be running: this test's own child that has already exited.
    dead = subprocess.run(["bash", "-c", "echo $$"], capture_output=True, text=True)
    (lock / "pid").write_text(dead.stdout.strip() + "\n")

    verdict = tmp_path / "var" / "swarm" / "verdicts" / "lane_stalelock.md"
    _fake_claude(
        tmp_path / "bin", f'printf "VERDICT: APPROVE\\n\\nopus\\n" > "{verdict}"\nexit 0\n'
    )
    proc = _run_dispatch(tmp_path, lane, tmp_path / "bin")
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    assert "reclaimed a lock left by dead pid" in proc.stdout, proc.stdout
    assert "VERDICT: APPROVE" in verdict.read_text()


# ---------------------------------------------------------------------------
# MGI-004 — a fail-closed guard with no correction path is its own outage
#
# The degenerate-branch refusal (ac858221) is right, but the operator who fixes
# the lane exactly as the refusal instructs got nothing back: the dispatch
# payload hashed only the CONTRACT, so a repaired lane produced a byte-identical
# payload, was blocked `verdict-repeat`, and was quarantined. Quarantine is
# checked before the gate, so that was permanent.
# ---------------------------------------------------------------------------
def _pump_env(tmp_path: Path, lane: str) -> tuple[dict[str, str], Path, Path]:
    root = tmp_path / "repo"
    root.mkdir(exist_ok=True)
    for link in ("scripts", "omniagentos", ".venv"):
        (root / link).symlink_to(REPO_ROOT / link)
    marker = tmp_path / "verifier-invoked"
    # The side-log proves the fake EXECUTED even if the marker's visibility is
    # in question (the runner-context anomaly, finding 0f0956495800); it feeds
    # _await_dispatch_evidence's diagnosis, never an assertion.
    _fake_claude(
        tmp_path / "bin",
        f'echo "fake-claude ran cwd=$PWD ts=$(date -u +%H:%M:%S)"'
        f' >> "{tmp_path / "fake-claude.log"}"\n'
        f'touch "{marker}"\nexit 0\n',
    )
    db = _migrated_db(tmp_path, "pump.sqlite3")
    quarantine = tmp_path / "quarantine"
    env = dict(os.environ)
    env.update(
        REPO=str(root),
        PATH=f"{tmp_path / 'bin'}:{env['PATH']}",
        HOME=str(tmp_path / "home"),
        OMNIAGENTOS_DB=str(db),
        PUMP_CLONES=str(root / "var" / "swarm" / "clones"),
        PUMP_QUARANTINE=str(quarantine),
        PUMP_PAYLOAD_DIR=str(tmp_path / "payloads"),
        PUMP_CYCLES="1",
        PUMP_QUEUE_CMD=f"echo {lane}",
        PUMP_LOG=str(tmp_path / "pump.log"),
        PUMP_INTERVAL="0",
        # Free slots are `PUMP_MAX - pgrep -f 'dispatch-verifier.sh'` counted
        # across the WHOLE MACHINE, and the default is 40. On a box running a
        # real verifier fleet the pump under test can therefore see zero free
        # slots and dispatch nothing, which reads here as "the repaired lane was
        # not re-dispatched". State the cap; do not inherit the host's.
        PUMP_MAX="4096",
        # Generous, and free: _run_pump captures to files, so the watchdog's
        # orphaned `sleep` no longer holds this call open for its full budget.
        VERIFIER_TIMEOUT="120",
        VERDICT_DISPATCH_TIMEOUT_S="300",
    )
    (tmp_path / "home").mkdir(exist_ok=True)
    # verdict-pump.sh:78 carries the byte-identical fail-closed preflight that
    # cost review-pump a train. It has never been the victim only because the
    # `omniagentos` link above already makes its `cd $REPO; $PY -m ...` resolve
    # from the tree under test — the exact protection `_stub_repo` was missing.
    # Warm it here too rather than leaving the sibling one accident from red.
    _warm_swarm_dal(root, env)
    return env, quarantine, marker


def _run_pump(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return _run_to_files(
        ["bash", str(SCRIPTS / "verdict-pump.sh")],
        env=env,
        # Beside the pump's own log, which _pump_env already put in tmp_path.
        scratch=Path(env["PUMP_LOG"]).parent / "pump-capture",
    )


# ESTATE-RUNNER FLAKE (2026-08-14) — 30s was itself the fix for an earlier race
# (da3f3c6df, 2026-08-06) and is STILL not generous enough on the shared estate box.
#
# The async chain between "pump backgrounded the dispatch" and "the marker exists"
# is a `( trap "" HUP; timeout ... dispatch-verifier.sh "$LANE" ) </dev/null &`
# subshell (the tty-less detach that replaced nohup, which never execed on the
# gate runner — see verdict-pump.sh) forking through several more processes (git
# rev-parse x2-3, mkdir/lock handling, `ln -sfn`, a subshell that finally execs
# the fake `claude`) — each hop pays real OS scheduling latency. On an
# idle box that whole chain lands in well under 100ms: reproduced locally (20x green,
# and even an artificially zero-wait single check still found the marker already
# written every time — see the session's repro notes) — consistent with "never
# locally" in the failure reports (#424, #436's first estate runs; passed on re-run).
# The estate runner is not idle: it shares its cores with the suite's own parallel
# workers AND the live agent fleet, and TESTING.md's own baseline for THIS machine
# measures a 3.0x wall-clock swing from exactly that contention. 30s was calibrated
# for the idle case; it has no margin for a documented 3x slowdown on the box that
# actually flaked. Widen it by the same 3x this machine already measures, so a
# transient scheduling delay reads as "still waiting", not "never happened" — the
# assertion still demands the dispatch genuinely land, just gives it a bound that
# matches this box's own observed variance rather than the idle-box happy path.
def _is_fresh(path: Path, since: float) -> bool:
    """A file only counts as evidence when it was written AFTER ``since``.

    1s tolerance for filesystem timestamp granularity; ``since=0.0`` disables
    the fence. OSError (vanished mid-stat) reads as not-evidence, fail closed.
    """
    try:
        return path.exists() and path.stat().st_mtime >= since - 1.0
    except OSError:
        return False


def _await_marker(marker: Path, timeout_s: float = 90.0, since: float = 0.0) -> bool:
    """verdict-pump backgrounds the dispatch (`( trap "" HUP; ... ) </dev/null &`),
    so the verifier runs AFTER the pump exits. Reading the marker immediately is a race — the pump's own
    `DISPATCH lane=` line is the synchronous decision; this is the async effect.

    ``since`` fences out a marker left by an earlier cycle of the same test
    (review r3 finding 3): only a marker written after it counts.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if _is_fresh(marker, since):
            return True
        time.sleep(0.1)
    return _is_fresh(marker, since)


# DISPATCH EVIDENCE, not just the marker (review of PR #470, 2026-08-14): on the
# estate GH runners the two positive-marker tests failed 6/6 across three gate
# runs while every OTHER file the same dispatch chain writes (quarantine
# reasons, the FAILED verdict artifact, verify.log) was demonstrably visible to
# the polling test process — forensics from the surviving pytest-8873 tmp on
# mw0002, and the identical pair passes in 10s on the same box/user via the wq
# pool (wq_01M00B4M2SKMDHJ8YMNBWKFWQ0). The verifier's own terminal artifact —
# record_failure() writes $REPO/var/swarm/verdicts/<slug>.md ("this file exists
# so the run is VISIBLE") — is therefore the robust proof the dispatch genuinely
# landed, and it is exactly what the fake-claude flow produces (the fake writes
# no verdict, so the verifier records FAILED). The marker stays as corroboration;
# a verdict-visible-but-marker-missing pass EMITS the full divergence as a
# pytest warning (round-2 review blocker: the anomaly must never pass silently —
# warnings survive a passing run in the CI summary, while tmp_path does not),
# feeding finding 0f0956495800 on its next runner-context occurrence.
def _dispatch_diagnosis(marker: Path, verdict: Path, env: dict[str, str], header: str) -> str:
    import shutil as _shutil

    pump_log = Path(env.get("PUMP_LOG", "/nonexistent"))
    fake_log = marker.parent / "fake-claude.log"
    return (
        f"{header}\n"
        f"  marker {marker}: exists={marker.exists()}\n"
        f"  verdict {verdict}: exists={verdict.exists()}\n"
        f"  fake-claude side-log: "
        f"{fake_log.read_text() if fake_log.exists() else '<never ran>'}\n"
        f"  claude resolves to: {_shutil.which('claude', path=env.get('PATH'))}\n"
        f"  pump log tail: "
        f"{pump_log.read_text()[-2000:] if pump_log.exists() else '<missing>'}"
    )


def _record_anomaly(diagnosis: str) -> None:
    """Persist anomaly telemetry where a PASSING run cannot lose it.

    Primary sink: an append-only file OUTSIDE tmp_path — RUNNER_TEMP on GitHub
    runners (collected with the job, survives the workspace), the system temp
    dir elsewhere — so the evidence exists regardless of pytest warning
    filters (review r3 finding 2: `-W ignore`/filterwarnings config must not
    reduce the anomaly to silence again). The warning stays as the visible
    secondary channel. A sink failure must not fail the passing test.
    """
    import tempfile
    import warnings

    sink = Path(os.environ.get("RUNNER_TEMP") or tempfile.gettempdir()) / (
        "verdict-dispatch-anomaly.log"
    )
    try:
        with sink.open("a", encoding="utf-8") as fh:
            fh.write(f"--- {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n{diagnosis}\n")
    except OSError:
        pass
    warnings.warn(f"{diagnosis}\n  (telemetry appended to {sink})", stacklevel=3)


def _await_dispatch_evidence(
    marker: Path,
    verdict: Path,
    env: dict[str, str],
    dispatched_after: float,
    timeout_s: float = 90.0,
) -> str:
    """Return '' when THIS dispatch provably landed; else a full diagnosis.

    ``dispatched_after`` (``time.time()`` taken just before the dispatching
    pump run) fences BOTH evidence files symmetrically (review r3 finding 1):
    a marker or verdict older than the dispatch is stale evidence from some
    earlier cycle and never satisfies the await.
    """

    def _fresh_marker() -> bool:
        return _is_fresh(marker, dispatched_after)

    def _fresh_verdict() -> bool:
        return _is_fresh(verdict, dispatched_after)

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if _fresh_marker() or _fresh_verdict():
            break
        time.sleep(0.1)
    if _fresh_marker() or _fresh_verdict():
        if not _fresh_marker():
            # Terminal artifact visible but the fake's touch is not — the exact
            # runner-context anomaly under investigation. Never silent, and
            # never dependent on warning filters: file sink + warning.
            _record_anomaly(
                _dispatch_diagnosis(
                    marker,
                    verdict,
                    env,
                    "DISPATCH-MARKER ANOMALY (finding 0f0956495800): verdict visible, marker not —",
                )
            )
        return ""
    return _dispatch_diagnosis(marker, verdict, env, f"no dispatch evidence within {timeout_s}s:")


# The default 180s pytest-timeout is calibrated for the idle-box case; a 90s
# _await_marker budget plus this test's own setup (venv warm-up, migration, two
# pump runs) leaves too little margin once the SAME contention that motivated the
# wider marker budget also slows the rest of the test. Give it explicit headroom
# rather than let a genuinely-slow-but-correct estate run get clipped by the
# global ceiling before the assertion even has its full budget to observe.
@pytest.mark.timeout(300)
def test_a_repaired_lane_is_dispatched_again_rather_than_frozen(tmp_path: Path) -> None:
    """MGI-004: repair the fault exactly as instructed and the lane must come back."""
    lane = _unique_lane("broken-lane")
    env, quarantine, marker = _pump_env(tmp_path, lane)
    lane_dir = tmp_path / "repo" / "var" / "swarm" / "clones" / lane
    (lane_dir / "var").mkdir(parents=True)
    (lane_dir / "var" / "task.md").write_text("CONTRACT: repairable setup fault\n")
    _git(lane_dir, "init", "-q", "-b", "lane/repaired")  # unborn: no commit yet

    t_refusal = time.time()
    first = _run_pump(env)  # refused by dispatch-verifier (exit 2), nothing verified
    # The refusal is what must be observed; give the backgrounded dispatch time to have
    # produced a marker if it were going to, so this is not a race that passes by luck.
    assert not _await_marker(marker, timeout_s=5, since=t_refusal), (
        "a verifier was spent on a lane with no branch\n" + first.stdout
    )

    # Repair, exactly as the refusal instructs.
    _git(lane_dir, "config", "user.email", "test@example.invalid")
    _git(lane_dir, "config", "user.name", "test")
    _git(lane_dir, "add", "-A")
    _git(lane_dir, "commit", "-q", "-m", "repair branch")

    t_dispatch = time.time()  # fences the verdict artifact to THIS dispatch
    result = _run_pump(env)
    assert not (quarantine / lane / "reason").exists(), (
        "a corrected lane was frozen by the prior setup failure\n" + result.stdout
    )
    assert "DISPATCH lane=" in result.stdout, result.stdout
    verdict = tmp_path / "repo" / "var" / "swarm" / "verdicts" / "lane_repaired.md"
    diagnosis = _await_dispatch_evidence(marker, verdict, env, dispatched_after=t_dispatch)
    assert diagnosis == "", "a corrected lane was not re-dispatched\n" + diagnosis


# Same headroom rationale as test_a_repaired_lane_is_dispatched_again_rather_than_frozen
# above: the 90s _await_marker budget must not collide with the global 180s ceiling
# once this test's own three pump runs are also slower under the contention that
# motivated the wider marker budget.
@pytest.mark.timeout(300)
def test_a_quarantined_lane_is_released_once_its_state_changes(tmp_path: Path) -> None:
    """The general correction path: quarantine expires when its subject moves."""
    lane = _unique_lane("brief-less-lane")
    env, quarantine, marker = _pump_env(tmp_path, lane)
    lane_dir = tmp_path / "repo" / "var" / "swarm" / "clones" / lane
    (lane_dir / "var").mkdir(parents=True)
    _git(lane_dir, "init", "-q", "-b", "lane/briefless")
    (lane_dir / "seed.txt").write_text("seed\n")
    _git(lane_dir, "config", "user.email", "test@example.invalid")
    _git(lane_dir, "config", "user.name", "test")
    _git(lane_dir, "add", "-A")
    _git(lane_dir, "commit", "-q", "-m", "seed")

    _run_pump(env)
    assert (quarantine / lane / "reason").read_text().strip() == "missing-brief"
    assert (quarantine / lane / "fingerprint").is_file(), (
        "quarantine recorded no evidence of WHAT it was about, so it can never expire"
    )

    # A quarantine that still holds must NOT be released just because time passed.
    _run_pump(env)
    assert (quarantine / lane / "reason").exists(), "an unchanged lane was released"

    # Supply the missing contract — the thing the quarantine was about.
    (lane_dir / "var" / "task.md").write_text("CONTRACT: now it has one\n")
    t_dispatch = time.time()  # fences the verdict artifact to THIS dispatch
    result = _run_pump(env)
    assert not (quarantine / lane / "reason").exists(), (
        "a lane whose quarantine reason was fixed stayed quarantined\n" + result.stdout
    )
    assert "RELEASE lane=" in result.stdout, result.stdout
    assert "DISPATCH lane=" in result.stdout, result.stdout
    verdict = tmp_path / "repo" / "var" / "swarm" / "verdicts" / "lane_briefless.md"
    diagnosis = _await_dispatch_evidence(marker, verdict, env, dispatched_after=t_dispatch)
    assert diagnosis == "", "a released lane was not dispatched\n" + diagnosis


def test_an_unchanged_quarantine_does_not_churn(tmp_path: Path) -> None:
    """Re-evaluating a quarantine must not cost an announcement or a row per cycle.

    Releasing on change is only safe if a lane that has NOT changed stays parked
    silently — otherwise the announce-once property becomes announce-forever, and
    the ledger fills with rows for a lane nobody is dispatching.
    """
    lane = _unique_lane("quiet-lane")
    env, quarantine, _ = _pump_env(tmp_path, lane)
    lane_dir = tmp_path / "repo" / "var" / "swarm" / "clones" / lane
    (lane_dir / "var").mkdir(parents=True)
    _git(lane_dir, "init", "-q", "-b", "lane/quiet")

    first = _run_pump(env)
    assert "QUARANTINE lane=" in first.stdout, first.stdout
    db = tmp_path / "pump.sqlite3"
    rows_after_first = int(
        subprocess.run(
            ["sqlite3", str(db), "SELECT count(*) FROM swarm_attempts;"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    )

    for _ in range(3):
        again = _run_pump(env)
        assert "QUARANTINE lane=" not in again.stdout, "re-announced an unchanged quarantine"
        assert "RELEASE lane=" not in again.stdout, "released a lane that had not changed"
    rows_after = int(
        subprocess.run(
            ["sqlite3", str(db), "SELECT count(*) FROM swarm_attempts;"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    )
    assert rows_after == rows_after_first, (
        f"an unchanged quarantine wrote {rows_after - rows_after_first} extra ledger rows"
    )


# ---------------------------------------------------------------------------
# THE PYTHON CARRIERS — MGI-001 reaches them too
#
# `scripts/land-approved-lanes.py:50` took the FIRST `re.search` match under
# MULTILINE, so `# Verdict: Approve` over a body of `VERDICT: REJECT` returned
# APPROVED on a LANDING path. `omniagentos/integration/verdicts.py` had the same
# first-line-wins shape. Sol's exact string survives in both only because
# "Approve-flow" is not an exact token — that is luck, not a guard.
# ---------------------------------------------------------------------------
TITLE_ONE_WORD_OVER_REJECT = "# Verdict: Approve\n\nVERDICT: REJECT\n\nopus (anthropic)\n"


def _land_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "land_approved_lanes", SCRIPTS / "land-approved-lanes.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _partition(tmp_path: Path, body: str, name: str = "lane-x"):
    mod = _land_module()
    d = tmp_path / "sol-verdicts"
    d.mkdir(exist_ok=True)
    (d / f"{name}.md").write_text(body)
    mod.SOL_VERDICTS_DIR = d
    return mod.parse_sol_verdicts()


def test_landing_script_does_not_approve_on_a_title_over_a_reject(tmp_path: Path) -> None:
    """The fifth carrier, on a landing path: APPROVED before the fix."""
    approved, rejected, failed = _partition(tmp_path, TITLE_ONE_WORD_OVER_REJECT)
    assert approved == [], f"a title outranked a REJECT on the landing path: {approved}"
    assert [lane for lane, _ in rejected] == ["lane-x"], (rejected, failed)


def test_landing_script_still_lands_a_real_approval(tmp_path: Path) -> None:
    """POSITIVE CONTROL: the live corpus's own approval form must still land."""
    approved, rejected, failed = _partition(
        tmp_path, "# Verdict — lane/x\n\n## VERDICT: APPROVE-WITH-NOTES\n\nopus\n"
    )
    assert approved == ["lane-x"], (approved, rejected, failed)


def test_landing_script_fails_closed_when_the_grammar_is_unusable(tmp_path: Path) -> None:
    """It delegates to the shell grammar; a broken delegation must not approve."""
    mod = _land_module()
    d = tmp_path / "sol-verdicts"
    d.mkdir(exist_ok=True)
    (d / "lane-x.md").write_text("VERDICT: APPROVE\n\nopus\n")
    mod.SOL_VERDICTS_DIR = d
    mod.VERDICT_GRAMMAR = tmp_path / "does-not-exist.sh"
    approved, _rejected, failed = mod.parse_sol_verdicts()
    assert approved == [], "an unusable grammar was read as approval"
    assert failed and "grammar" in failed[0][1].lower(), failed


def test_landing_script_reports_an_unclearable_artifact_as_a_refusal(tmp_path: Path) -> None:
    """The caller catches RuntimeError ONLY, so every failure must arrive as one.

    `clear_verdict_artifact` caught FileNotFoundError alone. A PermissionError /
    IsADirectoryError / read-only filesystem propagated past
    `except RuntimeError` at the call site, so the documented refusal — "a stale
    APPROVE there would merge to main. Refusing." — never printed and a traceback
    replaced it. Fail-closed either way; unreadable to the operator either way,
    which is how a stale APPROVE stops being investigated.
    """
    mod = _land_module()
    d = tmp_path / "verdicts"
    d.mkdir()
    stale = d / "aggregate_integration.md"
    stale.write_text(STALE_APPROVE)
    d.chmod(0o555)  # unlink in this directory is now denied
    try:
        with pytest.raises(RuntimeError) as caught:
            mod.clear_verdict_artifact(stale)
        assert "cannot clear a previous verdict" in str(caught.value), caught.value
        assert stale.read_text() == STALE_APPROVE, "the stale APPROVE was reported gone"
    finally:
        d.chmod(0o755)

    # POSITIVE CONTROLS: "already gone" is still silent, and a real clear still clears.
    mod.clear_verdict_artifact(d / "never-existed.md")
    live = d / "live.md"
    live.write_text(STALE_APPROVE)
    mod.clear_verdict_artifact(live)
    assert not live.exists()


def test_landing_script_holds_no_verdict_grammar_of_its_own() -> None:
    """One implementation. A second spelling is how the two drift apart.

    WRITING `VERDICT: FAILED` is fine — that is producing an artifact. DECIDING from
    a VERDICT literal is not: this file had `"VERDICT: APPROVE" in verdict_content`
    guarding a merge to main, which any occurrence anywhere satisfied.
    """
    import ast

    source = (SCRIPTS / "land-approved-lanes.py").read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare) and any(
            isinstance(op, ast.In | ast.NotIn) for op in node.ops
        ):
            left = node.left
            if isinstance(left, ast.Constant) and isinstance(left.value, str):
                assert "VERDICT" not in left.value.upper(), (
                    f"a verdict decision is made by substring again: {left.value!r}"
                )
        if isinstance(node, ast.Attribute) and node.attr in {"search", "compile", "match"}:
            assert not (isinstance(node.value, ast.Name) and node.value.id == "re"), (
                "the landing script parses verdicts with its own regex again"
            )
    code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))
    for token in ("APPROVE-WITH-NOTES", "APPROVE WITH NOTES"):
        assert token not in code, f"the landing script re-spells the grammar ({token})"
    assert "verdict-grammar.sh" in code, "the landing script no longer calls the one grammar"


@pytest.mark.parametrize(
    ("body", "must_not_approve"),
    [
        (TITLE_ONE_WORD_OVER_REJECT, True),
        ("VERDICT: APPROVE\n\nlater, on reflection:\n\nVERDICT: REJECT\n", True),
        ("VERDICT: APPROVE\n", False),
    ],
)
def test_python_verdict_parser_lets_a_refusal_outrank_an_approval(
    body: str, must_not_approve: bool
) -> None:
    """omniagentos/integration/verdicts.py — the sixth carrier, same shape."""
    from omniagentos.integration.verdicts import _parse_verdict

    decision = _parse_verdict(body).decision
    approving = decision in {"approve", "approve_with_notes"}
    assert approving is not must_not_approve, (body, decision)


# Where the live verdict corpus lives when this repo is not the serving checkout.
# A lane worktree has no var/swarm/verdicts, and that is the normal case.
LIVE_VERDICT_DIR_ENV = "OMNIAGENTOS_LIVE_VERDICT_DIR"


def _live_verdict_corpus() -> list[Path]:
    """The live artifacts, or an empty list — never a silent empty from a typo.

    An explicit override that points nowhere is a MISTAKE and fails loudly; an
    absent default is the ordinary worktree case and returns [] for the caller
    to skip on.
    """
    override = os.environ.get(LIVE_VERDICT_DIR_ENV, "").strip()
    if override:
        directory = Path(override)
        assert directory.is_dir(), f"{LIVE_VERDICT_DIR_ENV}={override!r} is not a directory"
        found = sorted(p for p in directory.glob("*.md") if p.name != "README.md")
        assert found, f"{LIVE_VERDICT_DIR_ENV}={override!r} holds no verdict artifacts"
        return found
    default = REPO_ROOT / "var" / "swarm" / "verdicts"
    if not default.is_dir():
        return []
    return sorted(p for p in default.glob("*.md") if p.name != "README.md")


def _assert_the_two_grammars_agree(path: Path) -> None:
    """Neither implementation may APPROVE what the other REFUSES."""
    from omniagentos.integration.verdicts import _parse_verdict

    shell = subprocess.run(
        [
            "bash",
            "-c",
            '. "$1" || exit 1; verdict_decision "$2"',
            "drift",
            str(SCRIPTS / "lib" / "verdict-grammar.sh"),
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    py = _parse_verdict(path.read_text(errors="replace")).decision
    if py in {"approve", "approve_with_notes"}:
        assert shell == "APPROVE", (
            f"{path.name}: python approves ({py}) what the shell refuses ({shell})"
        )
    if shell in {"REJECT", "FAILED"}:
        assert py not in {"approve", "approve_with_notes"}, (
            f"{path.name}: shell {shell} but python {py}"
        )


def test_the_shell_and_python_grammars_cannot_drift_into_disagreement() -> None:
    """The two implementations are bound by the property that actually matters.

    They are NOT equivalent and must not be asserted to be: the shell grammar is
    operational (it must recognise `VERDICT: FAILED`, which the pumps write, and
    `APPROVE-WITH-NOTES`, which 61 live files use), while verdicts.py is a
    deliberately narrow counterfeit-resistant parser whose three-token vocabulary
    is pinned by tests/integration/test_verdicts.py.

    What must hold in BOTH directions of severity is the safety direction:
    neither may approve what the other refuses. Python being stricter is fine;
    Python approving something the shell rejects is MGI-001 all over again.

    THE CASE TABLE IS THE ARM THAT ALWAYS RUNS. The live-corpus arm lives in its
    own test below, because it cannot run everywhere and a test that quietly
    proves less in a worktree than in the serving checkout is worse than one
    that says so.
    """
    import tempfile

    cases = [
        TITLE_ONE_WORD_OVER_REJECT,
        "# Verdict: Approve-flow lane\n\nVERDICT: REJECT\n",
        "VERDICT: APPROVE\n",
        "VERDICT: approve\n",
        "**VERDICT:** approve with notes\n",
        "## VERDICT: REJECT\n",
        "VERDICT: MAYBE\n",
        "VERDICT:\n",
        "no verdict here\n",
        "VERDICT: APPROVE\n\nVERDICT: REJECT\n",
        "  VERDICT: APPROVE\n",
        "> VERDICT: APPROVE\n",
        # DECORATED REFUSALS. Each of these parsed as an approval on one side or
        # the other: the underscore pair because `_` could open a verdict line
        # but not close one, the list markers because neither grammar had ever
        # looked at them. All four are things a reviewer plausibly writes.
        "_VERDICT: REJECT_\n",
        "## VERDICT: APPROVE\n\n_VERDICT: REJECT_\n",
        "_VERDICT: REJECT\n\nVERDICT: APPROVE\n",
        "VERDICT: APPROVE\n\n- VERDICT: REJECT\n",
        "VERDICT: APPROVE\n\n1. VERDICT: REJECT\n",
        "- VERDICT: REJECT\n",
        "1. VERDICT: REJECT\n",
        # The estate's own critic vocabulary (opus-critic.md, codex-critic.md).
        "VERDICT: APPROVE-WITH-CHANGES\n",
        "VERDICT: REWORK\n",
        "VERDICT: UNREPRODUCIBLE-FINDINGS\n",
        "VERDICT: INCONCLUSIVE\n",
        "VERDICT: APPROVE-WITH-CHANGES\n\nVERDICT: REWORK\n",
    ]
    with tempfile.TemporaryDirectory() as td:
        for i, body in enumerate(cases):
            path = Path(td) / f"case{i}.md"
            path.write_text(body)
            _assert_the_two_grammars_agree(path)


def test_the_two_grammars_agree_on_every_live_verdict_artifact() -> None:
    """The same property, over the artifacts the gate actually reads.

    This arm was `(REPO_ROOT / "var" / "swarm" / "verdicts").glob("*.md")` inside
    the test above. That directory does not exist in a lane WORKTREE, `.glob` on
    a missing directory returns [] with no error, and the loop then exercised
    only the hardcoded cases while reporting PASS — so "asserts over every live
    artifact" was true in the serving checkout and false everywhere lanes
    actually run. A silent no-op is the favourable-absence defect wearing a
    green tick.

    It now either runs against a real corpus or says out loud that it did not.
    Point OMNIAGENTOS_LIVE_VERDICT_DIR at one to run it from a worktree.
    """
    corpus = _live_verdict_corpus()
    if not corpus:
        pytest.skip(
            "NO LIVE VERDICT CORPUS — this arm proved nothing. "
            f"{LIVE_VERDICT_DIR_ENV} is unset and {REPO_ROOT / 'var/swarm/verdicts'} "
            "holds no *.md (expected in a lane worktree). Set "
            f"{LIVE_VERDICT_DIR_ENV}=<serving checkout>/var/swarm/verdicts to run it."
        )
    for path in corpus:
        _assert_the_two_grammars_agree(path)
