"""The reachability EXEMPTION TRAP, pinned against the real ``scripts/merge-gate.sh``.

THE TRAP. ``scripts/reachability-gate.py`` grades the CANDIDATE'S CODE
(``git show <ref>:<path>``) but resolves ``EXEMPT_FILE`` from its own
``__file__`` — so it reads ``devtasks/REACHABILITY-EXEMPT.txt`` out of the
checkout the gate RUNS IN (the pinned main workspace), never out of the branch.
An agent that records its exemption ON THE BRANCH is therefore refused AT THE
EXACT SYMBOL IT JUST EXEMPTED, and the refusal text tells it to add the line it
already added. Measured over 90 recorded merge-gate refusals: 32 were
reachability and 28 of those were ONE symbol (``seed_cursor``), ~10 minutes
each.

WHAT IS PINNED HERE is the SELF-EXPLAINING REFUSAL, not a change in verdict.
``reach_exempt_trap`` detects one state — the refused symbol IS exempt in the
candidate's copy and is NOT exempt in the running checkout's copy — and
``reach_exempt_explain`` names the remedy. Nothing here can make the gate accept
anything it previously refused, so every case below asserts on TEXT and on the
detector's own exit status.

The three helpers are extracted BY NAME from the real script and evaluated, so
this file cannot pass against a ``merge-gate.sh`` that does not define them:
:func:`_extract_func` raises rather than returning empty, which is the
favourable-absence guard (a missing function must not read as a silent pass).
The call-site test then pins BOTH refusal sites — the hoisted
``MERGE_GATE_PINNED=1`` path and the un-hoisted path most runs actually take —
because a fix that reaches one carrier of two is this repo's second-most-common
rework cause.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MERGE_GATE = REPO_ROOT / "scripts" / "merge-gate.sh"

EXEMPT_HEADER = (
    "# Symbols exempt from the reachability gate.\n#\n# One `path:symbol  reason` per line.\n"
)

# Byte-shaped like scripts/reachability-gate.py's own refusal block
# (``  {path}:{line}  {sym}()`` under a ``REFUSED — ...`` header), including the
# framework-registered PASS lines it prints ABOVE that header at the same indent.
FRAMEWORK_LINE = (
    "  framework-registered: omniagentos/api/routes.py  register_thing()  [include_router]"
)


def _refusal_output(*symbols: str, framework: bool = False) -> str:
    head = ["reachability: 3 new public symbol(s) on cand vs main"]
    if framework:
        head.append(FRAMEWORK_LINE)
    body = [f"REFUSED — {len(symbols)} new public symbol(s) with NO production caller:", ""]
    for i, key in enumerate(symbols):
        path, sym = key.rsplit(":", 1)
        body.append(f"  {path}:{40 + i}  {sym}()")
    body.append("")
    body.append("Each is 'built, tested, never wired' — this repo's signature defect.")
    return "\n".join([*head, "", *body])


def _extract_func(name: str) -> str:
    """Pull one shell function verbatim out of the real merge-gate.sh.

    Fails LOUDLY when the function is absent. Returning "" here would let this
    whole file pass against pre-fix code by evaluating nothing — the exact
    abnormal-condition-as-favourable-value shape these tests exist to catch.
    """
    src = MERGE_GATE.read_text()
    match = re.search(rf"^{re.escape(name)}\(\) \{{.*?^\}}$", src, re.MULTILINE | re.DOTALL)
    if match is None:
        raise AssertionError(
            f"{name}() is not defined in {MERGE_GATE} — the exemption-trap explanation "
            "is missing, so a branch-side exemption still refuses with no remedy named."
        )
    return match.group(0)


@pytest.fixture(scope="module")
def helpers() -> str:
    return "\n".join(
        _extract_func(n) for n in ("reach_exempt_keys", "reach_exempt_trap", "reach_exempt_explain")
    )


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture()
def fixture_repo(tmp_path: Path) -> Path:
    """A repo whose CANDIDATE ref and WORKING TREE carry different exemption files.

    That divergence is the trap itself: the gate reads the ref for the code and
    the working checkout for the exemptions.
    """
    repo = tmp_path / "repo"
    (repo / "devtasks").mkdir(parents=True)
    _git(repo.parent, "init", "-q", "-b", "main", str(repo))
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "devtasks" / "REACHABILITY-EXEMPT.txt").write_text(EXEMPT_HEADER)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    return repo


def _set_candidate_exemptions(repo: Path, body: str) -> str:
    """Commit ``body`` as the CANDIDATE's exemption file; return its ref."""
    (repo / "devtasks" / "REACHABILITY-EXEMPT.txt").write_text(EXEMPT_HEADER + body)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "candidate exemption")
    return _git(repo, "rev-parse", "HEAD")


def _set_running_exemptions(repo: Path, body: str) -> None:
    """Write the copy the GATE ACTUALLY READS ($REPO working tree)."""
    (repo / "devtasks" / "REACHABILITY-EXEMPT.txt").write_text(EXEMPT_HEADER + body)


def _drive(repo: Path, helpers: str, ref: str, gate_output: str) -> tuple[int, str, str]:
    """Run the extracted helpers exactly as merge-gate.sh's refusal sites do."""
    out_file = repo / ".gate-output"
    out_file.write_text(gate_output)
    script = repo / ".driver.sh"
    script.write_text(
        "set -uo pipefail\n"
        f"REPO={repo!s}\n"
        f"{helpers}\n"
        'GATE_OUT=$(cat "$1")\n'
        'if TRAP=$(reach_exempt_trap "$2" "$GATE_OUT"); then\n'
        '  printf "TRAPPED %s\\n" "$TRAP"\n'
        '  reach_exempt_explain "${TRAP#*|}" "${TRAP%%|*}"\n'
        "else\n"
        '  printf "SILENT\\n"\n'
        "fi\n"
    )
    proc = subprocess.run(
        ["bash", str(script), str(out_file), ref],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout, proc.stderr


# --------------------------------------------------------------------------- #
# The trap fires, and says which copy of the file is the problem
# --------------------------------------------------------------------------- #


def test_path_symbol_form_is_detected_and_explained(fixture_repo: Path, helpers: str) -> None:
    ref = _set_candidate_exemptions(
        fixture_repo, "omniagentos/db/cursor.py:seed_cursor  operator entry point\n"
    )
    _set_running_exemptions(fixture_repo, "")
    _rc, stdout, stderr = _drive(
        fixture_repo, helpers, ref, _refusal_output("omniagentos/db/cursor.py:seed_cursor")
    )
    assert stdout.startswith("TRAPPED all|omniagentos/db/cursor.py:seed_cursor"), stdout
    assert "READ THIS BEFORE RE-GATING" in stderr
    assert "omniagentos/db/cursor.py:seed_cursor" in stderr
    assert "land the exemption line on main FIRST" in stderr
    assert "chore(gates):" in stderr


def test_bare_symbol_form_is_detected(fixture_repo: Path, helpers: str) -> None:
    """reachability-gate.py matches ``sym in exempt`` too; so must the detector."""
    ref = _set_candidate_exemptions(fixture_repo, "seed_cursor  operator entry point\n")
    _set_running_exemptions(fixture_repo, "")
    _rc, stdout, stderr = _drive(
        fixture_repo, helpers, ref, _refusal_output("omniagentos/db/cursor.py:seed_cursor")
    )
    assert stdout.startswith("TRAPPED all|"), stdout
    assert "READ THIS BEFORE RE-GATING" in stderr


# --------------------------------------------------------------------------- #
# It stays quiet on every state that is NOT the trap
# --------------------------------------------------------------------------- #


def test_silent_when_exempt_in_both_copies(fixture_repo: Path, helpers: str) -> None:
    line = "omniagentos/db/cursor.py:seed_cursor  operator entry point\n"
    ref = _set_candidate_exemptions(fixture_repo, line)
    _set_running_exemptions(fixture_repo, line)
    _rc, stdout, stderr = _drive(
        fixture_repo, helpers, ref, _refusal_output("omniagentos/db/cursor.py:seed_cursor")
    )
    assert stdout.strip() == "SILENT", stdout
    assert "READ THIS BEFORE RE-GATING" not in stderr


def test_silent_when_exempt_in_neither_copy(fixture_repo: Path, helpers: str) -> None:
    """A genuinely unwired symbol must keep the plain refusal — no false remedy."""
    ref = _set_candidate_exemptions(fixture_repo, "omniagentos/other.py:unrelated  x\n")
    _set_running_exemptions(fixture_repo, "omniagentos/other.py:unrelated  x\n")
    _rc, stdout, stderr = _drive(
        fixture_repo, helpers, ref, _refusal_output("omniagentos/db/cursor.py:seed_cursor")
    )
    assert stdout.strip() == "SILENT", stdout
    assert "READ THIS BEFORE RE-GATING" not in stderr


def test_framework_registered_pass_lines_are_not_read_as_refusals(
    fixture_repo: Path, helpers: str
) -> None:
    """Those lines are PASSES printed above the header at the same indent."""
    ref = _set_candidate_exemptions(
        fixture_repo, "omniagentos/api/routes.py:register_thing  plugin hook\n"
    )
    _set_running_exemptions(fixture_repo, "")
    output = "\n".join(
        [
            "reachability: 2 new public symbol(s) on cand vs main",
            FRAMEWORK_LINE,
            "  every new public symbol is called by production code or registered",
        ]
    )
    _rc, stdout, _stderr = _drive(fixture_repo, helpers, ref, output)
    assert stdout.strip() == "SILENT", stdout


def test_missing_exemption_file_on_the_candidate_is_not_evidence(
    fixture_repo: Path, helpers: str
) -> None:
    """An unreadable candidate copy must go quiet, never claim a trap."""
    ref = _git(fixture_repo, "rev-parse", "HEAD")  # header only, no entries
    _set_running_exemptions(fixture_repo, "")
    _rc, stdout, _stderr = _drive(
        fixture_repo, helpers, ref, _refusal_output("omniagentos/db/cursor.py:seed_cursor")
    )
    assert stdout.strip() == "SILENT", stdout


# --------------------------------------------------------------------------- #
# A PARTIAL remedy must not be presented as a complete one
# --------------------------------------------------------------------------- #


def test_mixed_refusal_does_not_promise_that_landing_on_main_is_enough(
    fixture_repo: Path, helpers: str
) -> None:
    """One trapped symbol + one genuinely unwired symbol.

    Landing the exemption on main clears the first and NOT the second, so the
    "re-running unchanged will refuse identically" claim is false here and must
    not be printed. Reporting a partial remedy as a complete one is the same
    abnormal-as-favourable shape the gate exists to refuse, aimed at the reader.
    """
    ref = _set_candidate_exemptions(
        fixture_repo, "omniagentos/db/cursor.py:seed_cursor  operator entry point\n"
    )
    _set_running_exemptions(fixture_repo, "")
    _rc, stdout, stderr = _drive(
        fixture_repo,
        helpers,
        ref,
        _refusal_output(
            "omniagentos/db/cursor.py:seed_cursor",
            "omniagentos/db/cursor.py:never_wired",
        ),
    )
    assert stdout.startswith("TRAPPED mixed|omniagentos/db/cursor.py:seed_cursor"), stdout
    assert "never_wired" not in stdout
    assert "NOT EVERY SYMBOL IN THE REFUSAL ABOVE IS THIS TRAP" in stderr
    assert "WILL REFUSE IDENTICALLY" not in stderr
    assert "land the exemption line on main FIRST" in stderr


def test_all_trapped_keeps_the_refuse_identically_warning(fixture_repo: Path, helpers: str) -> None:
    """Negative control for the case above: with nothing else refused, it stays."""
    ref = _set_candidate_exemptions(
        fixture_repo,
        "omniagentos/db/cursor.py:seed_cursor  operator entry point\n"
        "omniagentos/db/cursor.py:seed_other  operator entry point\n",
    )
    _set_running_exemptions(fixture_repo, "")
    _rc, stdout, stderr = _drive(
        fixture_repo,
        helpers,
        ref,
        _refusal_output(
            "omniagentos/db/cursor.py:seed_cursor",
            "omniagentos/db/cursor.py:seed_other",
        ),
    )
    assert stdout.startswith("TRAPPED all|"), stdout
    assert "WILL REFUSE IDENTICALLY" in stderr
    assert "NOT EVERY SYMBOL IN THE REFUSAL ABOVE IS THIS TRAP" not in stderr


# --------------------------------------------------------------------------- #
# Both carriers, not one
# --------------------------------------------------------------------------- #


def test_both_reachability_refusal_sites_explain_the_trap() -> None:
    """merge-gate.sh emits a symbol-list reachability refusal from TWO places.

    The hoisted ``MERGE_GATE_PINNED=1`` path refuses on ``$CANDIDATE_SHA``; the
    un-hoisted path — which every un-pinned run takes — fails on ``$BRANCH``.
    Wiring only the first leaves the trap intact on the more common path.
    """
    src = MERGE_GATE.read_text()
    assert 'reach_exempt_trap "$CANDIDATE_SHA" "$REACH_OUT"' in src
    assert 'reach_exempt_trap "$BRANCH" "$REACH"' in src

    # Every refusal that PRINTS A SYMBOL LIST must be preceded by the probe.
    # (The "gate could not run" refusals carry no symbols and are excluded by
    # construction: they never grep the omniagentos/ lines.)
    symbol_list_sites = re.findall(
        r"REACH_DETAIL=\$\(printf '%s' \"\$REACH(?:_OUT)?\" \| grep -E", src
    )
    assert len(symbol_list_sites) == 2, symbol_list_sites
    assert src.count("reach_exempt_trap ") == 2


# --------------------------------------------------------------------------- #
# FIX 2 bind half: the width the process uses is the width the receipt key claims
# --------------------------------------------------------------------------- #


def test_counterfeit_pool_width_is_bound_to_the_receipt_key_and_the_process() -> None:
    """$CF_CMD is the key the counterfeit step receipt is stored and verified
    under. If the pool width is applied to the process but omitted from that
    string, a receipt minted from a SERIAL run verifies a WIDE one and lets it
    be skipped — the defect already fixed once for the ladder (constraint 3 at
    LADDER_CMD). Both carriers must read the SAME shell variable.
    """
    src = MERGE_GATE.read_text()
    assert (
        'CF_CMD="OMNIAGENTOS_CF_POOL_WORKERS=$CF_POOL_WORKERS python -m tests.counterfeits.harness"'
        in src
    ), "the pool width is not rendered into the step-receipt command key"
    assert 'OMNIAGENTOS_CF_POOL_WORKERS="$CF_POOL_WORKERS" \\' in src, (
        "the pool width is not applied to the counterfeit harness process"
    )
    assert 'MG_CF_POOL_WORKERS="${CF_POOL_WORKERS:-}"' in src
    assert '"counterfeit_pool_workers": as_opt_int(env("MG_CF_POOL_WORKERS")),' in src


def test_pool_width_validation_precedes_the_backgrounded_ladder() -> None:
    """``refuse`` exits. Validating after the ladder is backgrounded would orphan
    a pytest process holding the scratch worktree the EXIT trap then removes.
    """
    src = MERGE_GATE.read_text()
    first_validation = src.index('refuse "bad-cf-pool-workers"')
    background_launch = src.index('suite_worker "$STEP_DIR/ladder.out"')
    assert first_validation < background_launch
    assert src.count('refuse "bad-cf-pool-workers"') == 2  # non-integer and < 1


def test_unreached_counterfeit_step_records_a_null_width_not_a_serial_one() -> None:
    """The receipt comment claims null means "never reached". That is only true
    if the width is assigned INSIDE the tests/counterfeits/ branch.
    """
    src = MERGE_GATE.read_text()
    # RE-ANCHORED 2026-08-07: the presence test moved from a bare
    # `[ -d "$SCRATCH/tests/counterfeits" ]` to `suite_dirs_present`, which also
    # RECORDS the skip (a counterfeit corpus the candidate deleted used to leave
    # no trace in any carrier). The property asserted here is unchanged — the
    # width must be assigned INSIDE the present-branch, or "null" in the receipt
    # would stop meaning "never reached".
    branch_open = src.index("if suite_dirs_present tests/counterfeits; then")
    assign = src.index('CF_POOL_WORKERS="$CF_POOL_WORKERS_REQ"')
    cmd_build = src.index('CF_CMD="OMNIAGENTOS_CF_POOL_WORKERS=')
    assert branch_open < assign < cmd_build
    # …and the outer initialiser must leave both empty rather than pre-seeding a
    # width that a skipped step never used.
    assert 'CF_CMD="" CF_PRESENT=0' in src
