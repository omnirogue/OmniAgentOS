"""A merge-gate classifier that could not RUN must refuse, never report clean.

``grep`` exit status carries three meanings — 0 matched, 1 no match (normal),
>=2 grep ITSELF FAILED — and ``|| true`` erases the difference between the last
two. Every classifier in ``merge-gate.sh`` captured its grep that way, so a grep
that could not run yielded an empty capture, and every check reads empty as
"nothing forbidden": the gate reported ``secrets ok`` for a branch carrying
``configs/accounts.yaml`` and went on to mint a SIGNED PASS RECEIPT asserting the
secret scan had been performed.

``scripts/gates/forbidden-paths.sh:138-158`` records this REPRODUCED against its
own copy of the same shape and carries the fix. It never propagated here — the
same incomplete-propagation shape this codebase keeps re-learning, and the reason
the enumeration matters more than the individual repair.

It needs no adversary and no PATH shim to happen for real: a malformed pattern
and an unreadable input both make real grep exit 2.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

from omniagentos.scheduler.gate_evidence import GateEvidenceStore
from tests.scripts.test_merge_gate_m8_refusals import (
    MERGE_GATE,
    FixtureBranch,
    M8Repo,
    _commit_file,
    _output,
    _receipt,
    _run_gate,
    m8_repo,  # noqa: F401  — re-exported so pytest resolves the fixture here
)

# The gate's own secret pattern, quoted exactly as it appears at merge-gate.sh's
# SECRET_RE. The shim keys on this so it breaks the SECRET classifier and nothing
# else: a shim that failed every grep would make the gate refuse for some
# unrelated reason, and the test would pass while proving nothing.
_SECRET_RE_FRAGMENT = r"configs/accounts\.yaml"


def _surgical_grep_shim(tmp_path: Path) -> Path:
    """A ``grep`` that fails ONLY for the secret classifier, and works otherwise.

    Precision is the whole point. `test_forbidden_paths.py` can afford a shim
    that fails every grep because that script is small; merge-gate.sh runs
    hundreds of greps across receipt verification, the ladder and the trial
    merge, and breaking all of them yields a refusal that says nothing about the
    classifier under test.
    """
    shim_dir = tmp_path / "grep-shim"
    shim_dir.mkdir()
    shim = shim_dir / "grep"
    shim.write_text(
        "#!/bin/sh\n"
        'for a in "$@"; do\n'
        f"  case \"$a\" in *'{_SECRET_RE_FRAGMENT}'*)\n"
        '    echo "grep: memory exhausted" >&2; exit 2;;\n'
        "  esac\n"
        "done\n"
        'exec /usr/bin/grep "$@"\n',
        encoding="utf-8",
    )
    shim.chmod(0o755)
    return shim_dir


def test_a_secret_scan_that_could_not_run_refuses_instead_of_reporting_ok(
    m8_repo: M8Repo,  # noqa: F811
    tmp_path: Path,
) -> None:
    """The defect, end to end, on the path CI actually runs.

    ``MERGE_GATE_PINNED`` defaults to 0 and CI never sets it, so the ladder copy
    of the secret check is the live one; the hoisted copy is only armed by
    ``integrate.sh``. This exercises the live one.
    """
    case = _commit_file(
        m8_repo.path,
        "fixture/broken-secret-scan",
        "configs/accounts.yaml",
        "token: not-a-real-credential\n",
    )
    shim_dir = _surgical_grep_shim(tmp_path)

    # This case must reach the secrets classifier to prove anything about it, so
    # it needs a signed receipt on file — same as every other m8_repo candidate —
    # rather than tripping the (correctly) immediate signed-receipt-missing
    # refusal that scripts/merge-gate.sh now raises before the classifier ladder
    # runs at all (2026-08-09 receipt hoist).
    merge_base_sha = next(iter(m8_repo.branches.values())).merge_base_sha
    branch = FixtureBranch(
        name="fixture/broken-secret-scan",
        candidate_sha=case,
        merge_base_sha=merge_base_sha,
        refusal="classifier-unusable",
        reason="secrets classifier could not run",
    )
    store = GateEvidenceStore(m8_repo.evidence_root)
    signed = store.sign(_receipt(branch, m8_repo.path))
    receipt_path = m8_repo.evidence_root / "records" / "merge-gate" / f"{case}.json"
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_text(
        json.dumps(signed.to_payload(), sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )

    completed = _run_gate(
        m8_repo,
        m8_repo.branches.get("secret") or _FixtureRef(case),
        env_extra={"PATH": f"{shim_dir}{os.pathsep}{os.environ['PATH']}"},
    )
    out = _output(completed)

    # Name the CLASSIFIER, not just the slug. The shim is surgical, but a future
    # edit could make some other classifier fail first and this test would then
    # pass while proving nothing about the secret scan.
    assert "the secrets classifier could not run" in out, out
    assert "classifier-unusable" in out, out
    # The favourable answer is the one that must be impossible. Measured against
    # base_sha e6cdefd70 this exact run printed `secrets  ok` for a branch
    # carrying configs/accounts.yaml, and refused only for an unrelated reason.
    assert not re.search(r"^\s*secrets\s+ok\b", out, re.M), out
    # 2, not 1: a classifier that could not run is an INSTRUMENT failure, and the
    # receipt has to carry it as one. Reporting it as a candidate defect sends
    # the next agent to debug a branch that was never judged.
    assert completed.returncode == 2, out


# Captures that pipe into grep but decide NOTHING — the refusal is already
# settled above them and they only build the human message. Each needs a reason,
# and the test below fails if one of these disappears, so the list cannot quietly
# go stale and start excusing a real classifier that inherited the name.
_RENDER_ONLY = {
    "REACH_DETAIL": (
        "reachability message rendering only: both sites sit in the `else` branch "
        "where the probe has ALREADY failed, and the rc==2 'probe-unusable' refusal "
        "is taken above them. An empty capture degrades the message, not the ruling."
    ),
    "tail_line": (
        "suite_worker's pytest summary line. The suite verdict rides on the "
        "MEASURED rc, which report_suite reads from line 1 of the status file; "
        "tail_line is line 2 and only renders it. Traced the one path where an "
        "empty capture could still matter — the step receipt's summary — and it "
        "fails CLOSED: gate_evidence.py:1640-1641 rejects a receipt whose summary "
        "is empty ('step receipt has no verdict summary'), so a swallowed grep "
        "status costs a receipt reuse and re-runs the suite, never skips it."
    ),
}

# The identifier class is deliberately BOTH cases. Shell's house style for a
# variable inside a function is lowercase, and merge-gate.sh is full of functions
# (`suite_worker`, `report_suite`, `counterfeit_worker`, `reach_exempt_trap`), so
# "the next one will be added by someone who never read this file" — this scan's
# own stated threat model — describes a LOWERCASE capture more often than an
# uppercase one. An uppercase-only class made the guard refuse or wave through
# the identical defect depending on the case of a name.
_CAPTURE_NAME = r"[A-Za-z_][A-Za-z_0-9]*"


def _unguarded_grep_captures(source: str) -> tuple[list[str], set[str]]:
    """(offenders, exemptions actually seen) for one merge-gate source text.

    Split out of the invariant below so the counterfeit test can drive the
    SAME scanner over a mutated copy. A guard whose coverage is only ever
    measured against a tree that satisfies it cannot report its own blind
    spots — which is exactly the defect this helper was extracted to fix.
    """
    # Join line continuations first — two of the classifiers wrap their pattern
    # onto a second line, and a naive per-line scan misses exactly those.
    lines = source.replace("\\\n", " ").splitlines()

    offenders: list[str] = []
    seen_render_only: set[str] = set()
    for index, line in enumerate(lines):
        match = re.match(rf"^\s*({_CAPTURE_NAME})=\$\(.*\bgrep\b", line)
        if not match:
            continue
        if match.group(1) in _RENDER_ONLY:
            seen_render_only.add(match.group(1))
            continue
        following = lines[index + 1].strip() if index + 1 < len(lines) else ""
        guarded = line.rstrip().endswith("rc=$?") or "classifier_rc" in following
        if not guarded:
            offenders.append(line.strip())
    return offenders, seen_render_only


def test_no_classifier_still_swallows_its_grep_status() -> None:
    """The invariant, so the clone family cannot regrow.

    A source-level check rather than a behavioural one for the same reason
    `test_forbidden_paths.py` gives at its own guard: there are twelve of these
    and the next one will be added by someone who never read this file. The
    behavioural test above proves ONE of them; this proves the set.

    Deliberately NOT keyed on ``|| true``. That token is what the defect happened
    to be spelled with, not what the defect IS: with ``set -uo pipefail`` and no
    ``-e``, a BARE ``X=$(... | grep ...)`` with no rc test is the identical
    carrier. Now that the ``|| true``s are gone the house style is a bare capture
    followed by ``rc=$?``, so a bare capture is precisely the shape the next
    instance will arrive in — and a test keyed on the old spelling would wave it
    through.
    """
    source = MERGE_GATE.read_text(encoding="utf-8")
    offenders, seen_render_only = _unguarded_grep_captures(source)

    assert offenders == [], (
        "these captures read a failed grep as 'nothing found' — capture `rc=$?` "
        "and call classifier_rc, or justify them in _RENDER_ONLY: " + repr(offenders)
    )
    # A stale exemption is how a real classifier gets excused by inheriting an
    # allowlisted name. If the site is gone, the entry must go with it.
    assert seen_render_only == set(_RENDER_ONLY), (
        "_RENDER_ONLY is stale; these names no longer match any capture: "
        + repr(set(_RENDER_ONLY) - seen_render_only)
    )


def test_classifier_rc_fails_closed_on_a_non_numeric_status(tmp_path: Path) -> None:
    """The guard must not reproduce the fail-open it exists to close.

    `[ "" -ge 2 ]` does not evaluate false — it errors, so `&&` skips the refusal
    and the helper returns 0. A guard that waves through a status it could not
    read is the same favourable-absence shape one level up.
    """
    source = MERGE_GATE.read_text(encoding="utf-8")
    body = re.search(r"^classifier_rc\(\) \{.*?^\}", source, re.M | re.S)
    assert body, "classifier_rc not found — re-anchor this test rather than deleting it"

    harness = tmp_path / "probe.sh"
    harness.write_text(
        "set -uo pipefail\n"
        'refuse() { echo "REFUSED:$1"; exit 2; }\n'
        f"{body.group(0)}\n"
        'classifier_rc "$1" "probe"\n'
        'echo "CONTINUED"\n',
        encoding="utf-8",
    )
    for status in ("", "abc", "-1"):
        done = subprocess.run(
            ["bash", str(harness), status], capture_output=True, text=True, check=False
        )
        assert "CONTINUED" not in done.stdout, (status, done.stdout)
        assert done.returncode == 2, (status, done.stdout, done.stderr)
    # ...and a genuine "no match" still costs nothing.
    ok = subprocess.run(["bash", str(harness), "1"], capture_output=True, text=True, check=False)
    assert "CONTINUED" in ok.stdout, ok.stdout


def test_no_capture_chains_two_fallible_stages_into_one_status() -> None:
    """One fallible stage per capture, because pipefail only reports the last one.

    Under `pipefail` a pipeline's status is the RIGHTMOST NON-ZERO stage. An awk
    that died (127) ahead of a `grep -v` that kept nothing (1) reports 1 — a
    normal no-match, under the >=2 threshold — and the dead stage is invisible.
    Measured on this host, which is why the symlink classifier is split.
    """
    source = MERGE_GATE.read_text(encoding="utf-8")
    offenders = [
        line.strip()
        for line in source.replace("\\\n", " ").splitlines()
        if re.match(rf"^\s*{_CAPTURE_NAME}=\$\(", line)
        and re.search(r"\bawk\b", line)
        and re.search(r"\bgrep\b", line)
    ]
    assert offenders == [], (
        "these captures chain awk and grep, so a failed awk is masked by grep's "
        "normal rc=1; split them into one capture per stage: " + repr(offenders)
    )


# The defect shape the invariant exists to catch, in the house style a NEW one
# would arrive in: an unguarded capture whose emptiness decides a refusal.
_COUNTERFEIT_CAPTURE = (
    '{name}=$(printf \'%s\\n\' "$SWEPT_PATHS" | grep -E "$SECRET_RE")\n'
    '[ -n "${{{name}}}" ] && fail "secrets-extra" "leak" || pass "secrets-extra"'
)


def test_the_invariant_catches_the_defect_in_either_case() -> None:
    """A guard that keys on the CASE of a name is decoration for half the file.

    The invariant above can only ever report what its scanner can see, so
    measuring it against a tree that already satisfies it proves nothing about
    its coverage. This drives the same scanner over a mutated copy and asserts
    the ruling is identical for `leaked_extra` and `LEAKED_EXTRA` — two texts
    that differ only in the case of an identifier.

    Keyed on the DEFECT (an unguarded capture must be reported) rather than on
    the regex's spelling: rewriting the scan is fine, narrowing what it can see
    is not.
    """
    source = MERGE_GATE.read_text(encoding="utf-8")
    anchor = 'LEAKED=$(printf \'%s\\n\' "$SWEPT_PATHS" | grep -E "$SECRET_RE"); rc=$?'
    assert anchor in source, (
        "the secrets classifier moved — re-anchor this counterfeit to the "
        "guarded source rather than deleting it"
    )

    clean, _ = _unguarded_grep_captures(source)
    assert clean == [], f"tree is not clean before mutation: {clean!r}"

    rulings = {}
    for name in ("leaked_extra", "LEAKED_EXTRA"):
        mutated = source.replace(anchor, anchor + "\n" + _COUNTERFEIT_CAPTURE.format(name=name), 1)
        offenders, _ = _unguarded_grep_captures(mutated)
        rulings[name] = offenders

    for name, offenders in rulings.items():
        assert len(offenders) == 1 and name in offenders[0], (
            f"the invariant did not catch an unguarded capture named {name!r} — "
            f"a defect it refuses in one case and waves through in the other is "
            f"defeated by renaming a variable: {offenders!r}"
        )


class _FixtureRef:
    """Minimal stand-in matching the ``.name`` attribute ``_run_gate`` reads."""

    def __init__(self, name: str) -> None:
        self.name = name
