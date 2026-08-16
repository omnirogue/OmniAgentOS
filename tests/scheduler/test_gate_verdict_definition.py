"""ONE counterfeit-corpus verdict rule, shared by the live gate and its receipts.

Two trust holes closed here, both found by post-merge review of the fix that
made ``merge-gate.sh`` refuse ``other>0``:

1. **Receipt reuse re-passed what the live rule refuses.** The receipt path
   captured only ``survived``, so a signed step receipt minted under the weaker
   rule replayed an errored corpus as a pass for its whole 24h freshness
   window. The verdict rule lived in TWO PLACES THAT DISAGREED — the defect
   class this repo names in its own code (``routines.py``: settled-definition
   divergence auto-paused routines four times on 2026-07-31).

2. **The gate discarded the harness's exit code** and judged
   candidate-controlled stdout instead.

The two NEGATIVE CONTROLS are marked ``NEGATIVE CONTROL`` below: each one is
red against the pre-fix module. A gate that has never been shown to fail is not
a gate.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from omniagentos.scheduler import gate_evidence as gate_evidence_mod
from omniagentos.scheduler.gate_evidence import (
    COUNTERFEIT_STEP,
    STEP_SCHEMA,
    GateEvidenceRefusal,
    GateEvidenceStore,
    GateStepReceipt,
    record_step_receipt,
    verify_step_receipt,
)

NOW = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)

CANDIDATE_SHA = "a" * 40
MERGE_BASE_SHA = "b" * 40
MERGE_TREE_SHA = "c" * 40
CF_COMMAND = "python -m tests.counterfeits.harness"

#: What a fully-green corpus prints (``format_report``'s last line).
CLEAN_LINE = "total=59  caught=59  survived=0  other=0"
#: The exact shape the live gate started refusing on 2026-08-01: nothing
#: survived, but one entry ERRORED, so its coverage is dead.
ERRORED_LINE = "total=59  caught=58  survived=0  other=1"
SURVIVED_LINE = "total=59  caught=58  survived=1  other=0"
#: WHAT THE HARNESS ACTUALLY PRINTS TODAY. ``skipped_platform`` joined
#: ``format_report`` when platform pinning landed, and this judge's pattern went
#: on requiring the four-field form — so a healthy, exit-0 corpus parsed as "NO
#: verdict line (did not run)" and was refused for disagreeing with its own exit
#: code. The two spellings are ONE verdict; the four-field lines above are kept
#: because receipts minted under the old format still have to verify.
FIVE_FIELD_CLEAN_LINE = "total=59  caught=59  survived=0  skipped_platform=0  other=0"
#: A platform skip is green by the harness's own ruling (``EntryResult.ok``
#: counts it beside "caught"), and it must still be ACCOUNTED for in the total.
FIVE_FIELD_SKIPPED_LINE = "total=59  caught=58  survived=0  skipped_platform=1  other=0"


def _artifact(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "counterfeit.out"
    path.write_text(text, encoding="utf-8")
    return path


def _corpus_output(verdict_line: str, *, prefix: str = "") -> str:
    return (
        "COUNTERFEIT CORPUS REPORT\n"
        f"{prefix}"
        "------------------------------------------------------------------------\n"
        f"{verdict_line}\n"
    )


def _plant_receipt(
    tmp_path: Path,
    *,
    summary: str,
    exit_code: int = 0,
    step: str = COUNTERFEIT_STEP,
) -> GateStepReceipt:
    """Write a SIGNED step receipt straight into the store.

    Deliberately bypasses :func:`record_step_receipt`: that is what a receipt
    minted by the PREVIOUS release (or by any future mint path that forgets the
    rule) looks like on disk. Verification must not trust it.
    """
    store = GateEvidenceStore(tmp_path / "gate-evidence")
    return store.record_step(
        GateStepReceipt(
            schema=STEP_SCHEMA,
            step=step,
            candidate_sha=CANDIDATE_SHA,
            merge_base_sha=MERGE_BASE_SHA,
            merge_tree_sha=MERGE_TREE_SHA,
            command=CF_COMMAND,
            workspace_digest=hashlib.sha256(b"workspace").hexdigest(),
            output_digest=hashlib.sha256(b"output").hexdigest(),
            exit_code=exit_code,
            summary=summary,
            started_at="2026-01-01T08:59:00Z",
            finished_at="2026-01-01T09:00:00Z",
            nonce="0" * 32,
        )
    )


def _verify(tmp_path: Path, **overrides: object) -> GateStepReceipt:
    fields: dict[str, object] = {
        "step": COUNTERFEIT_STEP,
        "candidate_sha": CANDIDATE_SHA,
        "merge_base_sha": MERGE_BASE_SHA,
        "merge_tree_sha": MERGE_TREE_SHA,
        "command": CF_COMMAND,
        "evidence_root": tmp_path / "gate-evidence",
        "now": NOW,
    }
    fields.update(overrides)
    return verify_step_receipt(**fields)  # type: ignore[arg-type]


def _mint(tmp_path: Path, **overrides: object) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    summary = str(overrides.pop("summary", CLEAN_LINE))
    output_text = str(overrides.pop("output_text", _corpus_output(summary)))
    fields: dict[str, object] = {
        "step": COUNTERFEIT_STEP,
        "candidate_sha": CANDIDATE_SHA,
        "merge_base_sha": MERGE_BASE_SHA,
        "merge_tree_sha": MERGE_TREE_SHA,
        "command": CF_COMMAND,
        "workspace": workspace,
        "output_path": _artifact(tmp_path, output_text),
        "exit_code": 0,
        "summary": summary,
        "evidence_root": tmp_path / "gate-evidence",
        "started_at": "2026-01-01T08:59:00Z",
        "finished_at": "2026-01-01T09:00:00Z",
    }
    fields.update(overrides)
    return record_step_receipt(**fields)  # type: ignore[arg-type]


# --- NEGATIVE CONTROL 1: reused receipt for a run that had other>0 -----------


def test_negative_control_reused_receipt_with_errored_counterfeits_is_refused(
    tmp_path: Path,
) -> None:
    """NEGATIVE CONTROL (defect 1): a signed, fresh, correctly-bound receipt
    whose recorded run had ``other>0`` must NOT skip the step.

    Pre-fix this receipt verified: the reuse path read only ``survived``, so a
    24h window existed in which dead coverage re-passed on a stale signature
    while the live gate refused the identical run.
    """
    _plant_receipt(tmp_path, summary=ERRORED_LINE)

    try:
        reused = _verify(tmp_path)
    except GateEvidenceRefusal as refusal:
        assert "errored" in str(refusal), refusal
        return

    pytest.fail(
        "REUSED RECEIPT WITH other>0 WAS ACCEPTED — a signed receipt is again "
        "certifying a counterfeit run the live gate refuses: "
        f"{reused.summary!r}"
    )


def test_reused_receipt_with_survivors_is_still_refused(tmp_path: Path) -> None:
    """The behaviour that already worked must survive the unification."""
    _plant_receipt(tmp_path, summary=SURVIVED_LINE)

    with pytest.raises(GateEvidenceRefusal, match="SURVIVED"):
        _verify(tmp_path)


def test_reused_receipt_whose_verdict_line_is_missing_is_refused(tmp_path: Path) -> None:
    """A MISSING verdict refuses: an instrument that did not run is not a pass.

    Also the format-shift guard for the reuse path — a receipt minted before
    ``other`` existed in the report line cannot certify that ``other`` was 0.
    """
    _plant_receipt(tmp_path, summary="total=59  caught=59  survived=0")

    with pytest.raises(GateEvidenceRefusal, match="NO verdict line"):
        _verify(tmp_path)


def test_reused_receipt_for_a_fully_green_corpus_is_still_reused(tmp_path: Path) -> None:
    """The fix must not break reuse: a genuinely green corpus still skips."""
    _plant_receipt(tmp_path, summary=CLEAN_LINE)

    receipt = _verify(tmp_path)

    assert receipt.step == COUNTERFEIT_STEP
    assert receipt.summary == CLEAN_LINE


def test_counterfeit_verdict_line_filed_under_another_step_is_still_judged(
    tmp_path: Path,
) -> None:
    """The rule binds to the VERDICT LINE too, not only to the step name, so a
    corpus result cannot be laundered through a different step's receipt."""
    _plant_receipt(tmp_path, summary=ERRORED_LINE, step="doctrine")

    with pytest.raises(GateEvidenceRefusal, match="errored"):
        _verify(tmp_path, step="doctrine")


# --- the mint side of the SAME rule ------------------------------------------


def test_minting_a_receipt_for_an_errored_corpus_is_refused(tmp_path: Path) -> None:
    """The rule is applied where receipts are BORN as well as where they are
    consumed — the hole is closed at both ends of the 24h window."""
    with pytest.raises(GateEvidenceRefusal, match="errored"):
        _mint(tmp_path, summary=ERRORED_LINE)


def test_minting_a_counterfeit_receipt_without_a_verdict_line_is_refused(
    tmp_path: Path,
) -> None:
    with pytest.raises(GateEvidenceRefusal, match="NO verdict line"):
        _mint(tmp_path, summary="59 passed in 61.00s")


def test_minting_a_green_counterfeit_receipt_still_works(tmp_path: Path) -> None:
    path = _mint(tmp_path)

    assert path.exists()
    assert json.loads(path.read_text(encoding="utf-8"))["summary"] == CLEAN_LINE


# --- NEGATIVE CONTROL 2: exit code is primary, disagreement refuses ----------


def _judge(tmp_path: Path, *, exit_code: int, text: str) -> tuple[int, str]:
    artifact = _artifact(tmp_path, text)
    rc = gate_evidence_mod._main(
        ["judge-counterfeit", "--exit-code", str(exit_code), "--output", str(artifact)]
    )
    return rc, str(artifact)


def test_negative_control_nonzero_exit_with_a_clean_summary_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """NEGATIVE CONTROL (defect 2): the harness exits non-zero while printing a
    clean-looking summary line — the gate must REFUSE.

    Pre-fix the gate discarded ``wait``'s status and scored the grepped line, so
    this run was a PASS. The exit code is produced by the instrument; the line
    is produced by the tree under judgement.
    """
    rc, _ = _judge(tmp_path, exit_code=1, text=_corpus_output(CLEAN_LINE))
    captured = capsys.readouterr()

    assert rc == 1, (
        "NON-ZERO HARNESS EXIT WAS SCORED AS A PASS because the report line looked "
        "clean — the gate is judging candidate-controlled text again"
    )
    assert "DISAGREE" in captured.err
    assert "exited 1" in captured.err


def test_zero_exit_with_an_errored_summary_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Disagreement refuses in BOTH directions — the exit code does not get to
    silently overrule the report line either."""
    rc, _ = _judge(tmp_path, exit_code=0, text=_corpus_output(ERRORED_LINE))
    captured = capsys.readouterr()

    assert rc == 1
    assert "dead coverage" in captured.err
    assert "DISAGREE" in captured.err


def test_green_exit_and_green_line_pass_and_print_the_verdict_verbatim(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The pass path prints the line VERBATIM: the gate stores it as the receipt
    summary, and ``record_step_receipt`` re-checks it against the artifact bytes."""
    rc, _ = _judge(tmp_path, exit_code=0, text=_corpus_output(CLEAN_LINE))
    captured = capsys.readouterr()

    assert rc == 0
    assert captured.out.strip() == CLEAN_LINE


def test_a_verdict_line_a_candidate_printed_first_never_wins(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Candidate-controlled text (entry ids, rationales, pytest excerpts) is
    printed BEFORE ``format_report``'s summary, so the LAST line is the verdict.

    A forged clean line planted in an entry's detail must not shadow the real
    one — this is what ``grep ... | tail -1`` meant, now the only implementation.
    """
    forged = f"SURVIVED  cf-evil\n         detail:    {CLEAN_LINE}\n"
    rc, _ = _judge(tmp_path, exit_code=1, text=_corpus_output(SURVIVED_LINE, prefix=forged))
    captured = capsys.readouterr()

    assert rc == 1
    assert "SURVIVED" in captured.err


def test_no_verdict_line_at_all_is_refused_even_on_a_zero_exit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A crash before the summary, an empty capture, or a changed report format
    all land here: fail closed, with a diagnostic instead of an empty reason."""
    rc, _ = _judge(
        tmp_path,
        exit_code=0,
        text="COUNTERFEIT GATE REFUSED: duplicate id 'cf-a' in corpus.d/x.toml\n",
    )
    captured = capsys.readouterr()

    assert rc == 1
    assert "NO verdict line" in captured.err
    # The pre-fix reason-grep matched neither the harness's own refusal prefix
    # nor a traceback, so the operator got "produced NO verdict (did not run) — ".
    assert "COUNTERFEIT GATE REFUSED" in captured.err


#: What the harness ACTUALLY prints when its unpatched control pass goes red:
#: a header line ending in a colon, the reason on the NEXT line, then a pytest
#: excerpt whose ``FAILED`` lines name the offending nodes.
CONTROL_FAILED_OUTPUT = (
    "COUNTERFEIT GATE CONTROL FAILED:\n"
    "control (unpatched) must_fail set is not green — corpus points at broken "
    "or missing tests (rc=1)\n"
    ".....F...F........................ [ 21%]\n"
    "=================================== FAILURES ===================================\n"
    "E   AssertionError: bounded coordinator timeout for run swr_9f44259c23e3\n"
    "=========================== short test summary info ============================\n"
    "FAILED tests/simharness/test_orchestration.py::test_attempt_timeout_is_closed_and_escalated\n"
    "FAILED tests/simharness/test_orchestration.py::test_malformed_provider_json_does_not_kill_run\n"
    "2 failed, 334 passed in 65.36s\n"
)


def test_a_control_failure_names_its_reason_and_not_just_the_header(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """REGRESSION (2026-08-05): a control refusal must carry a REAL error line.

    ``_HARNESS_DIAGNOSTIC_RE`` matched the harness's own header
    ``COUNTERFEIT GATE CONTROL FAILED:`` — which is a colon and nothing else.
    The reason and the failing node ids live on the lines BELOW it, and the
    gate's ``$SCRATCH`` (with ``counterfeit.out`` in it) is destroyed by the
    EXIT trap, so the operator's only surviving record of ~8 consecutive
    in-gate refusals was a bare header. A one-line diagnostic that names
    nothing costs a whole diagnostic cycle per refusal; this pins that the
    header carries its first reason line AND the first failing node id.
    """
    rc, _ = _judge(tmp_path, exit_code=1, text=CONTROL_FAILED_OUTPUT)
    captured = capsys.readouterr()

    assert rc == 1
    reason = captured.err
    assert "COUNTERFEIT GATE CONTROL FAILED" in reason
    assert "must_fail set is not green" in reason, (
        "the control refusal was reported as a bare header again — the reason "
        f"line below it never reached the operator:\n{reason}"
    )
    assert (
        "tests/simharness/test_orchestration.py::test_attempt_timeout_is_closed_and_escalated"
        in reason
    ), (
        "the diagnostic does not name a single failing node, so the operator "
        f"cannot act on it without re-running the gate:\n{reason}"
    )


def test_a_long_reason_truncates_the_prose_and_never_the_node_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The diagnostic is CAPPED, so its parts are ordered by what an operator needs.

    The reason line is bounded only by whatever the harness printed. With the
    node id appended LAST, a 500-character reason pushed it past the cap and the
    operator was handed a header plus half a sentence — the same
    "diagnostic that names nothing" this package exists to end, one layer in.
    Truncation must eat prose first.
    """
    long_reason = "control (unpatched) must_fail set is not green — " + ("detail " * 70)
    text = (
        "COUNTERFEIT GATE CONTROL FAILED:\n"
        f"{long_reason}\n"
        "FAILED tests/simharness/test_orchestration.py::test_attempt_timeout_is_closed_and_escalated\n"
    )
    assert len(long_reason) > 400, "reason must exceed the cap or this proves nothing"

    rc, _ = _judge(tmp_path, exit_code=1, text=text)
    reason = capsys.readouterr().err

    assert rc == 1
    assert "test_attempt_timeout_is_closed_and_escalated" in reason, (
        "the cap dropped the one actionable token — order the node id BEFORE "
        f"the free-text body:\n{reason}"
    )


def test_a_diagnostic_line_that_is_already_complete_is_not_padded(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The header/continuation rule only fires for headers that END in a colon.

    ``COUNTERFEIT GATE REFUSED: <reason>`` already states its reason on the
    matched line; appending the next line of an unrelated excerpt to it would
    make every refusal noisier for no gain.
    """
    rc, _ = _judge(
        tmp_path,
        exit_code=2,
        text="COUNTERFEIT GATE REFUSED: duplicate id 'cf-a' in corpus.d/x.toml\nunrelated noise\n",
    )
    captured = capsys.readouterr()

    assert rc == 1
    assert "duplicate id 'cf-a'" in captured.err
    assert "unrelated noise" not in captured.err


def test_an_unreadable_capture_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = gate_evidence_mod._main(
        ["judge-counterfeit", "--exit-code", "0", "--output", str(tmp_path / "absent.out")]
    )

    assert rc == 1
    assert "unreadable" in capsys.readouterr().err


def test_an_empty_corpus_never_certifies_itself(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``total=0`` is a corpus that measured nothing, not a corpus that passed."""
    rc, _ = _judge(
        tmp_path, exit_code=0, text=_corpus_output("total=0  caught=0  survived=0  other=0")
    )

    assert rc == 1
    assert "empty" in capsys.readouterr().err


def test_the_format_the_harness_prints_today_is_a_verdict_the_judge_can_read(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """THE DEFECT, executed: a green corpus that the judge could not parse.

    ``tests/counterfeits/harness.py`` prints ``skipped_platform`` between
    ``survived`` and ``other``. Against the four-field pattern that line did not
    match at all, so an exit-0 run was scored "produced NO verdict line (did not
    run)" AND as an exit-code/report disagreement — the gate refusing its own
    healthy instrument, on the live path and for every stored receipt.
    """
    rc, _ = _judge(tmp_path, exit_code=0, text=_corpus_output(FIVE_FIELD_CLEAN_LINE))
    captured = capsys.readouterr()

    assert rc == 0, captured.err
    assert captured.out.strip() == FIVE_FIELD_CLEAN_LINE


def test_a_platform_skip_is_green_and_still_has_to_add_up(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Skips are the harness's own green, but they cannot go missing.

    ``EntryResult.ok`` counts ``skipped_platform`` beside ``caught`` and the
    harness exits 0 on it, so refusing here would re-create the two-definitions
    defect this module exists to prevent. The counting rule is what keeps it
    honest: the skip is added into the total, so a corpus cannot skip its way to
    a shorter run than it claims.
    """
    rc, _ = _judge(tmp_path, exit_code=0, text=_corpus_output(FIVE_FIELD_SKIPPED_LINE))
    assert rc == 0, capsys.readouterr().err

    rc, _ = _judge(
        tmp_path,
        exit_code=0,
        text=_corpus_output("total=59  caught=58  survived=0  skipped_platform=0  other=0"),
    )
    assert rc == 1, "a five-field line that does not add up must still be refused"
    assert "does not add up" in capsys.readouterr().err


def test_a_verdict_line_that_does_not_add_up_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, _ = _judge(
        tmp_path, exit_code=0, text=_corpus_output("total=59  caught=12  survived=0  other=0")
    )

    assert rc == 1
    assert "does not add up" in capsys.readouterr().err


# --- the two paths are ONE definition, not two that happen to agree ----------


@pytest.mark.parametrize(
    "verdict_line",
    [
        pytest.param(CLEAN_LINE, id="green"),
        pytest.param(ERRORED_LINE, id="errored"),
        pytest.param(SURVIVED_LINE, id="survived"),
        pytest.param(FIVE_FIELD_CLEAN_LINE, id="five-field-green"),
        pytest.param(FIVE_FIELD_SKIPPED_LINE, id="five-field-platform-skip"),
        pytest.param("total=59  caught=59  survived=0", id="old-3-field-format"),
        pytest.param("59 passed in 61.00s", id="not-a-corpus-line"),
        pytest.param("total=0  caught=0  survived=0  other=0", id="empty-corpus"),
    ],
)
def test_live_path_and_receipt_path_reach_the_same_verdict(
    tmp_path: Path, verdict_line: str
) -> None:
    """The property that makes defect 1 unrepeatable.

    Whatever the corpus prints, the live gate's answer and the reuse path's
    answer are the SAME answer, because they are the same function. A second
    definition that can drift is the bug — not any particular mismatch between
    the two.
    """
    live_rc, _ = _judge(tmp_path, exit_code=0, text=_corpus_output(verdict_line))
    live_accepts = live_rc == 0

    _plant_receipt(tmp_path, summary=verdict_line)
    try:
        _verify(tmp_path)
        receipt_accepts = True
    except GateEvidenceRefusal:
        receipt_accepts = False

    assert live_accepts == receipt_accepts, (
        f"VERDICT DIVERGENCE for {verdict_line!r}: live gate accepts={live_accepts} but "
        f"receipt reuse accepts={receipt_accepts} — the rule has two definitions again"
    )
