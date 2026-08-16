"""One routing table, or the drift comes back.

`validate_proposal` accepted ``skill`` and `write_document_change` routed
``skill`` — but `resolve_target_path` had never heard of it, so it returned
``None``, the hard-stop gate was skipped, and the writer's ``else: file_path =
"AGENTS.md"`` appended the proposal to the agents' own instruction file:

    >>> p = {'id': 'x1', 'kind': 'skill', 'target': {}, 'current': '',
    ...      'proposed': 'malicious content'}
    >>> validate_proposal(p, {})
    (True, '')

Two copies of one routing table, free to diverge — and they did, silently, the
moment a kind was added. Adding ``"skill"`` to the third list would have closed
this instance and left the class open.

So these tests do not (only) assert that ``skill`` is refused. They assert:

1. **fail closed** — an unrecognised or untargeted kind is REFUSED by the
   validator AND by the writers, never routed to a default document; and
2. **one source** — the validator, the document writer, the YAML writer and the
   fable gate all resolve a proposal's target through the SAME object in
   ``omniagentos.reflection.kinds``, and they still name the same file for
   every kind/target shape.

(2) is the deliverable: it is what fails if the tables are ever forked again.
"""

from __future__ import annotations

import re
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, get_args

import pytest

from omniagentos.reflection import apply as apply_mod
from omniagentos.reflection import contracts as contracts_mod
from omniagentos.reflection import fable_gate as fable_gate_mod
from omniagentos.reflection import kinds as kinds_mod
from omniagentos.reflection import propose as propose_mod
from omniagentos.reflection import validate as validate_mod
from omniagentos.reflection.apply import apply_yaml_change, write_document_change
from omniagentos.reflection.kinds import (
    ALLOWED_KINDS,
    CONFIG_KINDS,
    DOCUMENT_FALLBACKS,
    DOCUMENT_KINDS,
    UNATTENDED_APPLY_KINDS,
    resolve_target_path,
)
from omniagentos.reflection.validate import validate_proposal

LIMITS: dict = {}

UNKNOWN_KINDS = ["", "wat", "lesson_doc", "doc", "skill_doc", "risk_pin", "SKILL"]


def _proposal(kind: Any, target: Any) -> dict[str, Any]:
    return {
        "id": "p-1",
        "kind": kind,
        "target": target,
        "current": "",
        "proposed": "malicious content",
        "rationale": "r",
    }


# ---------------------------------------------------------------------------
# 1. The reported escalation, and the class it belongs to.
# ---------------------------------------------------------------------------


class TestFailClosed:
    def test_targetless_skill_is_refused(self):
        """The live reproduction from the report. Returned (True, '') before."""
        ok, err = validate_proposal(
            {
                "id": "x1",
                "kind": "skill",
                "target": {},
                "current": "",
                "proposed": "malicious content",
            },
            {},
        )
        assert ok is False, "a target-less skill proposal must not validate"
        assert "no writable target" in err

    @pytest.mark.parametrize("kind", UNKNOWN_KINDS)
    def test_unknown_kind_is_refused_by_the_validator(self, kind):
        ok, err = validate_proposal(_proposal(kind, {}), LIMITS)
        assert ok is False, f"unknown kind {kind!r} must be refused, not defaulted"
        assert "Invalid proposal kind" in err

    @pytest.mark.parametrize("kind", UNKNOWN_KINDS)
    def test_unknown_kind_is_refused_by_the_writer_too(self, tmp_path, kind):
        """The writer is reachable WITHOUT the validator.

        ``POST /reflection/{id}/approve`` (omniagentos/api/routes/reflection.py)
        calls ``apply_proposal`` directly on human approval — no
        ``validate_proposal`` anywhere on that path. A refusal that lives only
        in the validator does not cover the dashboard.
        """
        with pytest.raises(ValueError):
            write_document_change(tmp_path, kind, {"doc": "docs/x.md"}, "body")
        assert list(tmp_path.rglob("*")) == [], "a refused write must write nothing"

    @pytest.mark.parametrize("kind", sorted(DOCUMENT_KINDS - set(DOCUMENT_FALLBACKS)))
    def test_targetless_document_kind_never_defaults_to_agents_md(self, tmp_path, kind):
        agents = tmp_path / "AGENTS.md"
        agents.write_text("# house rules\n", encoding="utf-8")

        with pytest.raises(ValueError):
            write_document_change(tmp_path, kind, {}, "malicious content")

        assert agents.read_text(encoding="utf-8") == "# house rules\n"

    @pytest.mark.parametrize("kind", sorted(ALLOWED_KINDS - {"lesson"}))
    def test_every_untargeted_kind_is_refused(self, kind):
        """``lesson`` is the only kind with a legitimate default route."""
        ok, err = validate_proposal(_proposal(kind, {}), LIMITS)
        assert ok is False, f"{kind} with no target must be refused"
        assert "no writable target" in err

    def test_untargeted_lesson_still_routes_to_its_dated_file(self):
        """Negative control: fail-closed must not mean refuse-everything."""
        ok, err = validate_proposal(_proposal("lesson", {}), LIMITS)
        assert ok is True, f"an untargeted lesson should still validate, got {err!r}"


# ---------------------------------------------------------------------------
# 2. One source of truth. These are the tests that stop the recurrence.
# ---------------------------------------------------------------------------


@contextmanager
def _swapped(module, name, value):
    """Rebind ``module.name`` for the block, then put the original back.

    Deliberately not the ``monkeypatch`` fixture: the tests below must stay
    callable as plain methods, with no pytest machinery, so that a drift probe
    can invoke one directly and get a real assertion failure.
    """
    original = getattr(module, name)
    setattr(module, name, value)
    try:
        yield
    finally:
        setattr(module, name, original)


def _spy(answer):
    """A resolver stand-in that records its calls and returns ``answer``."""
    calls: list[tuple] = []

    def resolver(*args):
        calls.append(args)
        return answer

    resolver.calls = calls  # type: ignore[attr-defined]
    return resolver


class TestSingleSourceOfTruth:
    def test_every_stage_shares_one_resolver_object(self):
        """Same object — AND every stage still asks it, and obeys the answer.

        The identity assertions alone were a hole, and precisely the hole this
        file exists to close. ``is`` proves the name was imported; it says
        nothing about whether any call site still reaches it. A stage rewritten
        to re-read ``target["doc"]`` itself, or replaced outright, keeps the
        import binding intact and sails through — which is exactly what a drift
        probe demonstrated: stubbing ``fable_gate._eligibility`` with a function
        that ignores the resolver entirely left this test green.

        So each stage is now driven through a SPY: swap in a resolver that
        returns a known sentinel, run the stage, and assert the stage's decision
        followed the sentinel rather than the target. A stage that stopped
        consulting the resolver cannot produce the sentinel, and fails here.
        """
        # 1. The bindings are one object. Necessary, and — as the probe showed —
        #    nowhere near sufficient on its own.
        assert validate_mod.resolve_target_path is kinds_mod.resolve_target_path
        assert apply_mod.resolve_target_path is kinds_mod.resolve_target_path
        assert fable_gate_mod.resolve_target_path is kinds_mod.resolve_target_path
        assert apply_mod.config_target_path is kinds_mod.config_target_path

        harmless_doc = {"doc": "docs/lessons/2026-01-01-note.md"}

        # 2. The validator. Control first: this proposal validates today, so a
        #    refusal below is the spy's doing and not the proposal's.
        ok, err = validate_proposal(_proposal("lesson", harmless_doc), LIMITS)
        assert ok is True, f"control proposal should validate, got {err!r}"

        spy = _spy("scripts/merge-gate.sh")
        with _swapped(validate_mod, "resolve_target_path", spy):
            ok, err = validate_proposal(_proposal("lesson", harmless_doc), LIMITS)
        assert spy.calls, "validate_proposal never asked the shared resolver"
        assert ok is False and "hard-stop" in err.lower(), (
            "the validator judged a path the resolver did not name: it is "
            "reading the target itself instead of routing through kinds.py"
        )

        # 3. The document writer. Its answer must be the resolver's answer.
        spy = _spy("docs/sentinel-doc.md")
        with tempfile.TemporaryDirectory() as tmp, _swapped(
            apply_mod, "resolve_target_path", spy
        ):
            written = write_document_change(Path(tmp), "lesson", harmless_doc, "body")
            created = sorted(
                p.relative_to(tmp).as_posix() for p in Path(tmp).rglob("*") if p.is_file()
            )
        assert spy.calls, "write_document_change never asked the shared resolver"
        assert written == "docs/sentinel-doc.md"
        assert created == ["docs/sentinel-doc.md"], (
            f"the writer wrote {created}, not what the resolver named"
        )

        # 4. The YAML writer, which routes through config_target_path.
        spy = _spy("configs/sentinel.yaml")
        with tempfile.TemporaryDirectory() as tmp, _swapped(
            apply_mod, "config_target_path", spy
        ):
            written = apply_yaml_change(
                Path(tmp), {"file": "configs/other.yaml", "key": "lane_floors"}, {"a": 1}
            )
            created = sorted(
                p.relative_to(tmp).as_posix() for p in Path(tmp).rglob("*") if p.is_file()
            )
        assert spy.calls, "apply_yaml_change never asked the shared reader"
        assert written == "configs/sentinel.yaml"
        assert created == ["configs/sentinel.yaml"], (
            f"the writer wrote {created}, not what the reader named"
        )

        # 5. The fable gate. Reached through the module attribute on purpose, so
        #    that replacing `_eligibility` wholesale — the drift the probe
        #    performed — is caught here rather than silently tolerated.
        row = {
            "id": "p-1",
            "status": "pending",
            "risk_class": "low",
            "kind": "lesson",
            "target": '{"doc": "docs/lessons/2026-01-01-note.md"}',
        }
        assert fable_gate_mod._eligibility(row) is None, (
            "control row should be gate-eligible; _eligibility is not behaving "
            "like the real implementation"
        )

        spy = _spy(None)
        with _swapped(fable_gate_mod, "resolve_target_path", spy):
            reason = fable_gate_mod._eligibility(row)
        assert spy.calls, "_eligibility never asked the shared resolver"
        assert reason is not None and "unroutable" in reason, (
            "the gate ruled on a document target the resolver did not name"
        )

        spy = _spy("AGENTS.md")
        with _swapped(fable_gate_mod, "resolve_target_path", spy):
            reason = fable_gate_mod._eligibility(row)
        assert reason is not None and "protected document" in reason, (
            "the gate ignored the resolver and judged the raw target instead"
        )

    def test_every_stage_shares_one_kind_table(self):
        assert validate_mod.ALLOWED_KINDS is kinds_mod.ALLOWED_KINDS
        assert apply_mod.CONFIG_KINDS is kinds_mod.CONFIG_KINDS
        assert apply_mod.DOCUMENT_KINDS is kinds_mod.DOCUMENT_KINDS
        assert apply_mod.UNATTENDED_APPLY_KINDS is kinds_mod.UNATTENDED_APPLY_KINDS

    def test_the_two_apply_lanes_partition_the_allowed_kinds(self):
        """No kind may be routable by both writers, and none by neither.

        ``apply_proposal`` dispatches config-then-document-then-raise; a kind in
        neither set raises, a kind in both would be silently decided by
        statement order.
        """
        assert CONFIG_KINDS.isdisjoint(DOCUMENT_KINDS)
        assert CONFIG_KINDS | DOCUMENT_KINDS == ALLOWED_KINDS

    def test_fallbacks_are_a_subset_of_the_document_kinds(self):
        assert set(DOCUMENT_FALLBACKS) <= DOCUMENT_KINDS

    def test_unattended_apply_is_a_deliberate_subset(self):
        """Adding a document kind must not auto-grant unattended write access."""
        assert UNATTENDED_APPLY_KINDS < DOCUMENT_KINDS
        assert "skill" not in UNATTENDED_APPLY_KINDS, (
            "a skill is executable guidance to the fleet; it does not get "
            "applied without a human"
        )

    @pytest.mark.parametrize(
        "model",
        [contracts_mod.ImprovementProposal, propose_mod.ImprovementProposal],
        ids=["contracts", "propose"],
    )
    def test_the_pydantic_schemas_accept_exactly_the_allowed_kinds(self, model):
        """Two more copies of the list, as static ``Literal``s pydantic needs.

        They cannot import a frozenset (a Literal must be static), so they are
        pinned here instead: add a kind to one and this fails until every
        carrier agrees.
        """
        declared = set(get_args(model.model_fields["kind"].annotation))
        assert declared == set(ALLOWED_KINDS)

    def test_the_fable_gate_allowlist_is_a_subset(self):
        assert fable_gate_mod.GATE_KIND_ALLOWLIST <= ALLOWED_KINDS
        assert "skill" not in fable_gate_mod.GATE_KIND_ALLOWLIST


# The shape of the defect: a literal default path spelled out in a module that
# is not the routing table. `else: file_path = "AGENTS.md"` is the exact line
# that turned an unrecognised kind into a write to the agents' instructions.
_PRIVATE_FALLBACK = re.compile(r"""(?:return|=)\s*f?["'](?:AGENTS\.md|docs/lessons/)""")


def test_no_module_reintroduces_a_private_fallback_table():
    package = Path(kinds_mod.__file__).resolve().parent
    offenders = []
    for path in sorted(package.glob("*.py")):
        if path.name == "kinds.py":
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _PRIVATE_FALLBACK.search(line):
                offenders.append(f"{path.name}:{lineno}: {line.strip()}")
    assert offenders == [], (
        "default document paths belong in omniagentos/reflection/kinds.py and "
        "nowhere else — a private copy here is the drift that made a "
        "target-less proposal an append to AGENTS.md:\n  " + "\n  ".join(offenders)
    )


# ---------------------------------------------------------------------------
# 3. Behavioural cross-check: the resolver and the writers must name the SAME
#    file, for every kind and every target shape. This fails on divergence even
#    if someone re-implements the resolver instead of importing it.
# ---------------------------------------------------------------------------

DOC_TARGET_SHAPES: list[Any] = [
    {},
    None,
    "",
    {"key": "a"},
    {"doc": "docs/a.md"},
    {"file": "docs/b.md"},
    {"doc": "docs/a.md", "file": "docs/b.md"},
    "docs/c.md",
]

CONFIG_TARGET_SHAPES: list[Any] = [
    {},
    None,
    {"file": "configs/x.yaml", "key": "a"},
    "configs/x.yaml",
    # The same doc/file split, in the config lane: apply_yaml_change reads
    # ``file`` only, so a resolver that preferred ``doc`` would check
    # docs/harmless.md and then write configs/x.yaml.
    {"doc": "docs/harmless.md", "file": "configs/x.yaml", "key": "a"},
]


@pytest.mark.parametrize("kind", sorted(ALLOWED_KINDS))
@pytest.mark.parametrize("target", DOC_TARGET_SHAPES, ids=lambda t: repr(t)[:32])
def test_document_writer_writes_exactly_what_the_resolver_named(tmp_path, kind, target):
    expected = resolve_target_path(kind, target)

    if kind not in DOCUMENT_KINDS:
        with pytest.raises(ValueError):
            write_document_change(tmp_path, kind, target, "body")
        return

    if expected is None:
        with pytest.raises(ValueError):
            write_document_change(tmp_path, kind, target, "body")
        assert [p for p in tmp_path.rglob("*") if p.is_file()] == [], (
            "an unroutable document proposal must write nothing at all"
        )
        return

    written = write_document_change(tmp_path, kind, target, "body")
    assert written == expected
    created = sorted(
        p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*") if p.is_file()
    )
    assert created == [expected], (
        f"the writer touched {created} but the hard-stop gate was shown {expected!r}"
    )


@pytest.mark.parametrize("kind", sorted(CONFIG_KINDS))
@pytest.mark.parametrize("target", CONFIG_TARGET_SHAPES, ids=lambda t: repr(t)[:40])
def test_yaml_writer_writes_exactly_what_the_resolver_named(tmp_path, kind, target):
    (tmp_path / "configs").mkdir()
    expected = resolve_target_path(kind, target)

    if expected is None:
        with pytest.raises(ValueError):
            apply_yaml_change(tmp_path, target, {"a": 1})
        return

    if kinds_mod.target_key(target) is None:
        # CHANGED 2026-08-07, deliberately and not quietly: this case used to
        # assert that a keyless config target WRITES. It does not any more.
        #
        # A keyless config write replaces the entire file with the payload, so
        # a rule set written about NAMED keys cannot see anything it carries —
        # `{"api_key": "stolen", "budget": 999999}` landed verbatim through a
        # guard that was inspecting only target["key"]. The validator already
        # refused this shape; the writer now agrees. See
        # omniagentos.reflection.guard.examine_payload for why the alternatives
        # (scan the bulk payload, require an explicit key list) are worse.
        with pytest.raises(ValueError):
            apply_yaml_change(tmp_path, target, {"a": 1})
        assert [p for p in tmp_path.rglob("*") if p.is_file()] == [], (
            "a refused keyless config write must write nothing at all"
        )
        return

    written = apply_yaml_change(tmp_path, target, {"a": 1})
    assert written == expected
    created = sorted(
        p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*") if p.is_file()
    )
    assert created == [expected], (
        f"the writer touched {created} but the hard-stop gate was shown {expected!r}"
    )


def test_resolver_refuses_before_it_reads_the_target():
    """An unknown kind resolves to None even when it names a real path.

    The routing table is the gate: ``resolve_target_path`` must not hand back a
    usable path for a kind no writer is allowed to apply.
    """
    for kind in UNKNOWN_KINDS:
        assert resolve_target_path(kind, {"doc": "docs/x.md"}) is None
        assert resolve_target_path(kind, "docs/x.md") is None
