"""Doctrine: revert-test + counterfeit for Context Capsule v1 claims.

Decisive claim: the manifest describes the *delivered prompt*, not the request.
Named counterfeit: "a manifest that describes the request instead of the
delivered prompt" — brief_digest/brief_bytes derived from ids rather than prompt.
"""

from __future__ import annotations

from pathlib import Path

from tests.doctrine import TextReplace, counterfeit_test, revert_test

_CAPSULE = Path(__file__).resolve().parents[2] / "omniagentos" / "context" / "capsule.py"
_NODE_REFLECTS = (
    "tests/context/test_context_capsule.py::test_manifest_reflects_delivered_prompt_not_request"
)
_NODE_INERT = (
    "tests/context/test_context_capsule.py::"
    "test_context_capsule_flag_does_not_change_delivered_prompt_bytes"
)


def test_revert_manifest_must_measure_delivered_prompt() -> None:
    """Break brief_digest to a constant → decisive test (c) must FAIL."""
    report = revert_test(
        target=_CAPSULE,
        mutation=TextReplace(
            old=(
                '    brief_digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()\n'
                '    brief_bytes = len(prompt.encode("utf-8"))'
            ),
            new=(
                '    brief_digest = "0" * 64  # REVERT: ignore delivered prompt\n'
                "    brief_bytes = 0"
            ),
        ),
        nodeid=_NODE_REFLECTS,
    )
    assert "assert" in report.failure_text.lower() or "Error" in report.failure_text
    # Expose verbatim failure for the lane report.
    print("VERBATIM_REVERT_FAILURE_BEGIN")
    print(report.failure_text)
    print("VERBATIM_REVERT_FAILURE_END")


def test_counterfeit_manifest_from_request_ids_not_prompt() -> None:
    """Counterfeit: digest/bytes from task/run ids (request) instead of prompt."""
    report = counterfeit_test(
        target=_CAPSULE,
        counterfeit=TextReplace(
            old=(
                '    brief_digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()\n'
                '    brief_bytes = len(prompt.encode("utf-8"))'
            ),
            new=(
                "    brief_digest = hashlib.sha256(\n"
                '        f"{task_id}{run_id}".encode("utf-8")\n'
                "    ).hexdigest()  # COUNTERFEIT: request ids, not prompt\n"
                '    brief_bytes = len(f"{task_id}{run_id}".encode("utf-8"))'
            ),
        ),
        nodeid=_NODE_REFLECTS,
    )
    assert report.counterfeit_failure.failed
    print("VERBATIM_COUNTERFEIT_FAILURE_BEGIN")
    print(report.failure_text)
    print("VERBATIM_COUNTERFEIT_FAILURE_END")


def test_counterfeit_inertness_if_observe_rewrote_prompt() -> None:
    """Second counterfeit for inertness: observe path must not rewrite prompts.

    Mutating the observation helper to return a rewritten prompt string would be
    the classic shadow-that-is-not-shadow counterfeit. The inertness test builds
    prompts only through ``_render_coral_context`` (which capsule must not touch),
    so we counterfeit the *render* path with a capsule-mode branch that appends
    a marker — proving the headline test binds to byte equality of the real
    render, not a vacuous constant.
    """
    spawn_path = Path(__file__).resolve().parents[2] / "omniagentos" / "swarm" / "spawn.py"
    # Insert a capsule-aware mutation into _render_coral_context's return.
    # If the inertness test only checked a constant, this would slip through.
    report = counterfeit_test(
        target=spawn_path,
        counterfeit=TextReplace(
            old='        return "\\n".join(lines)',
            new=(
                '        joined = "\\n".join(lines)\n'
                "        # COUNTERFEIT: pretend shadow capsule rewrites the brief\n"
                "        import os as _os\n"
                '        if _os.environ.get("OMNIAGENTOS_CONTEXT_CAPSULE", "").strip().lower()'
                ' in {"shadow", "enforce"}:\n'
                '            joined = joined + "\\n[capsule-shadow-marker]"\n'
                "        return joined"
            ),
        ),
        nodeid=_NODE_INERT,
    )
    assert report.counterfeit_failure.failed
    print("VERBATIM_INERT_COUNTERFEIT_FAILURE_BEGIN")
    print(report.failure_text)
    print("VERBATIM_INERT_COUNTERFEIT_FAILURE_END")
