"""Deterministic HIGH/LOW risk-tier classification for tiered verification.

The lander (``bridge.gate_loop``) puts EVERY candidate through the mandatory
mechanical merge-gate. Tiered verification decides, on top of that floor, whether
a candidate ALSO needs a separate cross-lineage LLM verdict before it may land:

  * ``HIGH`` — requires a genuine cross-lineage build-time verdict, exactly as the
    lander did before tiering existed.
  * ``LOW``  — a signed, receipt-verified merge-gate PASS on the candidate's own
    tip stands in for that verdict. The gate is still the floor; LOW only waives
    the *extra* LLM verdict, never the gate itself.

The classification is a pure function of the candidate's REAL changed paths (the
lander feeds it the ``git diff`` ground truth, never the envelope's self-reported
``paths``). Two properties are load-bearing and non-negotiable:

  * CONSERVATIVE / FAIL-CLOSED. A path is LOW-eligible only if it is provably one
    of a small set of bounded-mechanical shapes. Anything unknown, ambiguous, or
    unreadable is HIGH. An empty/unreadable diff is HIGH. There is no code path
    that defaults to LOW.
  * KILL SWITCH. Tiering is OFF unless ``OMNIAGENTOS_TIERED_VERIFY=1``. When OFF,
    :func:`classify` returns HIGH for every candidate, so the lander runs its
    exact pre-tiering approval check and behaviour is unchanged.

The two narrow LOW carve-outs that cannot be proven from a path alone — an
additive-only schema field already covered by a schema-validation test, and a
single small script with a passing execution-verified test — require an explicit
attestation. The lander intersects any attestation with the real diff before
passing it here, so a self-reported claim can only ever apply to a path the
candidate genuinely touched, and even then the mandatory gate re-runs the tests.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable

from bridge import review_policy

HIGH = "HIGH"
LOW = "LOW"

#: Only this exact value turns tiering on; anything else (unset, "0", "true",
#: "yes", whitespace) leaves it OFF. A first land ships OFF so the default is the
#: exact, reviewed pre-tiering behaviour.
ENV_FLAG = "OMNIAGENTOS_TIERED_VERIFY"

# ---------------------------------------------------------------- HARD-HIGH
#
# Surfaces that can NEVER be downgraded to LOW, whatever a candidate self-attests.
# Kept deliberately broad: over-classifying HIGH only costs a candidate its
# LLM verdict requirement (which it can satisfy), whereas a single wrong LOW
# lands self-governing code with no independent review. Safety over throughput.
#
# SUPERSET INVARIANT (safety guarantee): a candidate that the OLD build-review
# policy (:mod:`bridge.review_policy`) would have required a cross-lineage verdict
# for must NEVER be downgraded to attested-LOW. So the hard-HIGH nets below are
# DERIVED FROM ``review_policy`` — its risky words and exact files are unioned in
# wholesale, never re-typed, so the two can never silently drift apart again (that
# drift is exactly what reopened ``scripts/policy_check.py`` and
# ``tests/test_gate_foo.py`` to LOW). ``test_risk_tier`` asserts the subset
# relationship mechanically.
#
# The ONE deliberate exception to a strict superset is the schema/contracts
# attestation carve-out: ``review_policy`` makes ``schema/``/``contracts/`` always
# risky, whereas CONTRACT.md §2 permits an ADDITIVE-ONLY schema field, covered by
# a schema-validation test and an explicit envelope attestation, to be LOW. Those
# prefixes are therefore handled by the attestation-downgradable SCHEMA category
# below, NOT hoisted into hard-HIGH — see ``_RISKY_PREFIXES_SCHEMA_CARVEOUT``.
_RISK_TIER_EXTRA_EXACT = {
    "pipeline/bridge/integration.py",     # main-writer / lander core
    "pipeline/bridge/gate_loop.py",       # main-writer / lander core
    "pipeline/bridge/review_policy.py",   # the risk policy itself
    "pipeline/bridge/risk_tier.py",       # this classifier itself
    "pipeline/bridge/train_assembler.py",
    "pipeline/bridge/gate_host.py",
    "pipeline/bridge/land_detect.py",     # land path
    "pipeline/bridge/close_on_land.py",   # land path
    "scripts/merge-gate.sh",              # the gate itself
    "scripts/mint-merge-candidate.py",    # mints the gate's signed receipts
}
#: Union of this classifier's own additions with every exact file the old policy
#: flagged, so no risky exact path can be LOW here that was HIGH there.
_HARD_HIGH_EXACT = _RISK_TIER_EXTRA_EXACT | set(review_policy._RISKY_EXACT)

#: ``review_policy`` prefixes that are the deliberate schema attestation carve-out
#: (§2): kept OUT of hard-HIGH so an attested additive-only schema field can be
#: LOW, exactly as the contract permits. Everything else the old policy flagged
#: by prefix is unioned into the hard-HIGH prefix net.
_RISKY_PREFIXES_SCHEMA_CARVEOUT = ("schema/", "contracts/", "pipeline/schema/")
_RISK_TIER_EXTRA_PREFIXES = (
    "gates/",                    # the gate itself
)
_HARD_HIGH_PREFIXES = _RISK_TIER_EXTRA_PREFIXES + tuple(
    p for p in review_policy._RISKY_PREFIXES
    if p not in _RISKY_PREFIXES_SCHEMA_CARVEOUT
)
#: Path split on ``/ _ . -`` into word tokens; ANY of these => HARD HIGH. Covers
#: auth/permissions/approvals, secrets/credentials, money/banking, migrations,
#: the gate/policy machinery, and reachability wherever they appear in a path.
#: DERIVED as a strict superset of ``review_policy._RISKY_PATH_WORDS`` (which
#: carries ``gate``/``policy``) plus this classifier's own bank/reachability
#: additions — so the two word nets can never drift.
_RISK_TIER_EXTRA_WORDS = {"bank", "banking", "reachability"}
_HARD_HIGH_WORDS = set(review_policy._RISKY_PATH_WORDS) | _RISK_TIER_EXTRA_WORDS
#: Basename prefixes matched CASE-INSENSITIVELY (spec: ``PROMPT-*`` prompts,
#: ``ARCHI*`` architecture). Case-folded so ``prompt-foo.md`` / ``archi.md`` can
#: never slip past as ordinary docs — the earlier case-SENSITIVE check let a
#: lowercase prompt/arch file downgrade to LOW.
_HARD_HIGH_BASENAME_PREFIXES = ("prompt", "archi")
#: Test-HARNESS / build-config basenames that DEFINE what "PASS" means (pytest
#: collection hooks, fixtures, gate config). A candidate that edits one of these
#: controls its own gate verdict, so they are hard-HIGH even inside ``tests/`` and
#: can never be a bounded-mechanical LOW. Matched case-insensitively on basename.
_HARD_HIGH_BASENAMES = {
    "conftest.py", "pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml",
}

# --------------------------------------------------------------- SCHEMA / SCRIPT
#
# HIGH by default, but downgradable to LOW by an explicit, real-diff-bound
# attestation (see module docstring).
_SCHEMA_PREFIXES = ("schema/", "contracts/", "pipeline/schema/")
_SCHEMA_SUFFIX = ".schema.json"
_SCRIPT_PREFIXES = ("scripts/",)
_SCRIPT_SUFFIXES = (".py", ".sh")

# -------------------------------------------------------------- path categories
_C_HIGH = "high"
_C_MECHANICAL = "mechanical"
_C_SCHEMA = "schema"
_C_SCRIPT = "script"
_C_UNKNOWN = "unknown"


def _norm(path: object) -> str:
    p = str(path).replace("\\", "/").strip()
    while p.startswith("./"):
        p = p[2:]
    return p


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _words(path: str) -> set[str]:
    return {w for w in re.split(r"[/_.\-]+", path.casefold()) if w}


def is_hard_high(path: str) -> bool:
    """A surface that no attestation may ever downgrade to LOW."""
    p = _norm(path)
    low = p.casefold()
    base = _basename(low)
    if base in _HARD_HIGH_BASENAMES:
        return True
    if any(base.startswith(pref) for pref in _HARD_HIGH_BASENAME_PREFIXES):
        return True
    if low in _HARD_HIGH_EXACT:
        return True
    if low.startswith(_HARD_HIGH_PREFIXES):
        return True
    if _words(p) & _HARD_HIGH_WORDS:
        return True
    return False


def _is_doc(path: str) -> bool:
    # Non-prompt Markdown. PROMPT-*/ARCHI* and prompts/ dirs are caught by
    # is_hard_high first, so a .md that reaches here is genuine documentation.
    return _norm(path).casefold().endswith(".md")


def _is_test(path: str) -> bool:
    # A bounded-mechanical test is one that lives inside a ``tests/`` DIRECTORY —
    # the contract's carve-out is ``tests/`` (a location), not a basename
    # convention. A bare ``test_*.py`` / ``*_test.py`` basename ANYWHERE ELSE
    # (e.g. ``scripts/test_deploy.py``) is NOT auto-mechanical: that convention
    # is trivially forgeable to smuggle arbitrary code past review as a "test".
    # Such a file falls through to the script/unknown categories and only reaches
    # LOW via the explicit, single-small-script attestation (with its own guard).
    p = _norm(path).casefold()
    segs = p.split("/")
    return "tests" in segs[:-1]      # a ``tests`` path COMPONENT, never the basename


def _is_schema(path: str) -> bool:
    low = _norm(path).casefold()
    return low.startswith(_SCHEMA_PREFIXES) or low.endswith(_SCHEMA_SUFFIX)


def _is_script(path: str) -> bool:
    low = _norm(path).casefold()
    return low.startswith(_SCRIPT_PREFIXES) and low.endswith(_SCRIPT_SUFFIXES)


def _category(path: str) -> str:
    """Coarse category BEFORE attestation. HARD-HIGH always wins."""
    if is_hard_high(path):
        return _C_HIGH
    if _is_doc(path) or _is_test(path):
        return _C_MECHANICAL
    if _is_schema(path):
        return _C_SCHEMA       # HIGH unless an additive-only attestation covers it
    if _is_script(path):
        return _C_SCRIPT       # HIGH unless a single-small-script attestation covers it
    return _C_UNKNOWN          # fail closed => HIGH


def tiering_enabled(env: dict | None = None) -> bool:
    """True only when ``OMNIAGENTOS_TIERED_VERIFY`` is exactly ``"1"``."""
    src = os.environ if env is None else env
    return str(src.get(ENV_FLAG, "")).strip() == "1"


def classify(
    changed_paths: Iterable[str] | None,
    *,
    enabled: bool | None = None,
    attested_additive_schema: Iterable[str] = (),
    attested_scripts: Iterable[str] = (),
    env: dict | None = None,
) -> str:
    """Classify a candidate ``HIGH`` or ``LOW`` from its REAL changed paths.

    ``enabled`` overrides the kill switch (defaults to reading the environment).
    ``attested_additive_schema`` / ``attested_scripts`` are the producer's
    already-real-diff-bound attestations for the two narrow carve-outs. The
    result is LOW only when tiering is enabled AND every changed path is
    bounded-mechanical (or an attested schema/script carve-out); otherwise HIGH.
    """
    if enabled is None:
        enabled = tiering_enabled(env)
    if not enabled:
        return HIGH  # kill switch: everything HIGH, lander runs its pre-tiering check

    paths = [_norm(p) for p in (changed_paths or ()) if str(p).strip()]
    if not paths:
        return HIGH  # empty / unreadable diff => fail closed, never LOW

    attested_schema = {_norm(p) for p in (attested_additive_schema or ())}
    attested_script = {_norm(p) for p in (attested_scripts or ())}
    script_seen = 0
    for p in paths:
        cat = _category(p)
        if cat == _C_MECHANICAL:
            continue
        if cat == _C_SCHEMA and p in attested_schema:
            continue
        if cat == _C_SCRIPT and p in attested_script:
            script_seen += 1
            if script_seen > 1:      # "a SINGLE small script", never a fleet of them
                return HIGH
            continue
        return HIGH                  # HIGH, UNKNOWN, or unattested schema/script
    return LOW
