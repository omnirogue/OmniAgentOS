"""Every mutator of the shared decision namespace holds ``_state_lock``.

``decisions._state_lock``'s own docstring states the contract: *every* caller
that reads ``team-decisions.json``, decides something from it (next_number
allocation, a status flip) and writes it back must hold the lock for the whole
span. ``decisions.py`` honoured it; ``overnight.py`` did not, so
``register_suggestions`` could allocate a number while a locked registrar held
``LOCK_EX`` — both issued the same number and the second save silently dropped
the first registration. Because ``overnight.render_numbered`` then asks the
operator to reply ``N yes`` against that number, a collision means the reply
authorizes a decision the operator was never shown.

Two kinds of test, deliberately:

* a BEHAVIOURAL one that probes the real ``flock`` at the moment the state is
  written — no sleeps, no threads, no subprocess timing;
* a STRUCTURAL one that GLOBS the mutators rather than listing them, so a fifth
  registrar added later fails the build instead of silently joining the
  unlocked half. A hand-written list of the four we know about would have the
  same failure mode as the unlocked call sites it is meant to catch.
"""

from __future__ import annotations

import ast
import fcntl
import inspect
from pathlib import Path
from typing import Any

import pytest

from omniagentos.team import compute, decisions, overnight

#: Modules that share ``team-decisions.json``. All are walked whole.
_NAMESPACE_MODULES = (compute, decisions, overnight)
_LOCK_NAME = "_state_lock"
_STATE_FUNCS = frozenset({"load_state", "save_state"})


# --------------------------------------------------------------------------
# behavioural: is the real flock actually held when the state is written?
# --------------------------------------------------------------------------


def _lock_is_held(repo_root: Path) -> bool:
    """True when something holds ``LOCK_EX`` on the namespace lockfile.

    ``flock`` is owned by the open file DESCRIPTION, not the process, so a
    second ``open()`` of the same path conflicts even from this very process.
    That makes this an exact probe rather than a timing heuristic.
    """
    lock_path = decisions._lock_path(repo_root)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as probe:
        try:
            fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(probe.fileno(), fcntl.LOCK_UN)
        return False


@pytest.fixture
def namespace_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("OMNIAGENTOS_VAR", str(tmp_path))
    return tmp_path


@pytest.fixture
def lock_witness(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """Record whether the lock was held at every ``save_state`` in this test."""
    observed: list[bool] = []
    real_save = decisions.save_state

    def witnessed_save(repo_root: Path, state: dict[str, Any]) -> None:
        observed.append(_lock_is_held(repo_root))
        real_save(repo_root, state)

    monkeypatch.setattr(decisions, "save_state", witnessed_save)
    return observed


def test_the_probe_can_tell_the_two_states_apart(namespace_root: Path) -> None:
    """Vacuity guard: a probe stuck on one answer would pass everything below."""
    assert _lock_is_held(namespace_root) is False
    with decisions._state_lock(namespace_root):
        assert _lock_is_held(namespace_root) is True
    assert _lock_is_held(namespace_root) is False


def test_register_suggestions_allocates_its_number_under_the_lock(
    namespace_root: Path, lock_witness: list[bool]
) -> None:
    """``notify.py`` calls this from the 16:00 pulse with no lock held."""
    overnight.register_suggestions(
        namespace_root,
        [{"employee_id": "emp_alice", "card_id": "btk_a", "ref": "U3", "title": "card"}],
    )
    assert lock_witness, "register_suggestions never wrote the namespace"
    assert all(lock_witness), (
        "register_suggestions wrote team-decisions.json without holding "
        "_state_lock; a concurrent registrar reads the same stale next_number "
        "and one registration is silently lost"
    )


def test_reserve_approved_flips_status_under_the_lock(
    namespace_root: Path, lock_witness: list[bool]
) -> None:
    """``decisions.main()`` calls this AFTER ``process_replies`` released."""
    overnight.register_suggestions(
        namespace_root,
        [{"employee_id": "emp_alice", "card_id": "btk_a", "ref": "U3", "title": "card"}],
    )
    lock_witness.clear()
    state = decisions.load_state(namespace_root)
    for decision in state["decisions"]:
        decision["status"] = "approved"
    decisions.save_state(namespace_root, state)
    lock_witness.clear()

    assert overnight.reserve_approved(namespace_root) == 1
    assert lock_witness, "reserve_approved never wrote the namespace"
    assert all(lock_witness), (
        "reserve_approved flipped a decision to 'executed' without holding "
        "_state_lock; a concurrent pass can clobber the status back"
    )


def test_no_self_deadlock_on_the_documented_call_order(namespace_root: Path) -> None:
    """``decisions.main()`` runs ``reserve_approved`` after ``process_replies``.

    ``_state_lock`` is a blocking ``LOCK_EX`` on one open description, so a
    nested acquisition inside a still-held lock would hang forever rather than
    fail. Pin the real order: acquire, release, then reserve.
    """
    with decisions._state_lock(namespace_root):
        state = decisions.load_state(namespace_root)
        decisions.save_state(namespace_root, state)
    # Lock released — the sequential re-acquisition below must not hang.
    assert overnight.reserve_approved(namespace_root) == 0
    overnight.register_suggestions(
        namespace_root,
        [{"employee_id": "emp_alice", "card_id": "btk_a", "ref": "U3", "title": "x"}],
    )
    assert overnight.reserve_approved(namespace_root) == 0


# --------------------------------------------------------------------------
# structural: glob the mutators so a NEW one cannot join the unlocked half
# --------------------------------------------------------------------------


def _is_named_call(node: ast.AST, names: frozenset[str] | set[str]) -> bool:
    """``load_state(...)`` or ``decisions.load_state(...)``."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id in names
    if isinstance(func, ast.Attribute):
        return func.attr in names
    return False


def _opens_lock(node: ast.AST) -> bool:
    return isinstance(node, ast.With | ast.AsyncWith) and any(
        _is_named_call(item.context_expr, {_LOCK_NAME}) for item in node.items
    )


def _calls_under(
    node: ast.AST, names: frozenset[str] | set[str], *, in_lock: bool
) -> list[tuple[int, bool]]:
    """Every call to ``names`` under ``node``, each tagged with its lock state.

    Recurses by hand rather than using ``ast.walk`` because whether a call is
    protected depends on its ANCESTRY, which ``walk`` discards.
    """
    found: list[tuple[int, bool]] = []
    if _is_named_call(node, names):
        found.append((node.lineno, in_lock))  # type: ignore[attr-defined]
    child_in_lock = True if _opens_lock(node) else in_lock
    for child in ast.iter_child_nodes(node):
        found.extend(_calls_under(child, names, in_lock=child_in_lock))
    return found


def _module_functions(module: Any) -> list[tuple[str, ast.AST]]:
    source = Path(inspect.getsourcefile(module)).read_text(encoding="utf-8")
    return [
        (f"{module.__name__}.{node.name}", node)
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    ]


def _mutators() -> dict[str, list[tuple[int, bool]]]:
    """Every function in the namespace modules that touches the state file."""
    out: dict[str, list[tuple[int, bool]]] = {}
    for module in _NAMESPACE_MODULES:
        for qualname, node in _module_functions(module):
            calls = [
                call
                for stmt in node.body  # type: ignore[attr-defined]
                for call in _calls_under(stmt, _STATE_FUNCS, in_lock=False)
            ]
            if calls:
                out[qualname] = calls
    return out


def _locked_helper_names() -> set[str]:
    return {
        qualname.rsplit(".", 1)[-1]
        for qualname in _mutators()
        if qualname.rsplit(".", 1)[-1].endswith("_locked")
    }


def test_the_glob_actually_finds_the_known_mutators() -> None:
    """Vacuity guard: a walker that finds nothing would pass every case below."""
    mutators = _mutators()
    for expected in (
        "omniagentos.team.compute.register_balancer_proposals",
        "omniagentos.team.decisions.process_replies",
        "omniagentos.team.decisions._register_repair_proposals_locked",
        "omniagentos.team.overnight.register_suggestions",
        "omniagentos.team.overnight.reserve_approved",
    ):
        assert expected in mutators, f"{expected} not discovered — the walker is broken"


@pytest.mark.parametrize("qualname", sorted(_mutators()))
def test_every_namespace_mutator_runs_under_the_state_lock(qualname: str) -> None:
    """A function that writes the namespace must hold ``_state_lock``.

    A ``*_locked`` name is the one exemption: it declares that its CALLER owns
    the lock, and :func:`test_locked_helpers_are_only_called_under_the_lock`
    holds it to that.
    """
    if qualname.rsplit(".", 1)[-1].endswith("_locked"):
        pytest.skip("declares its caller holds the lock; checked by the call-site test")
    unprotected = [lineno for lineno, in_lock in _mutators()[qualname] if not in_lock]
    assert not unprotected, (
        f"{qualname} touches team-decisions.json outside _state_lock at line(s) "
        f"{unprotected}. Two such cycles racing both read the same stale "
        "next_number and the second save silently drops the first decision."
    )


def test_locked_helpers_are_only_called_under_the_lock() -> None:
    """``*_locked`` helpers are exempt above ONLY because their callers lock."""
    helpers = _locked_helper_names()
    assert helpers, "no *_locked helper found — the exemption above is untested"
    for module in _NAMESPACE_MODULES:
        for qualname, node in _module_functions(module):
            if node.name in helpers:  # type: ignore[attr-defined]
                continue
            unprotected = [
                lineno
                for stmt in node.body  # type: ignore[attr-defined]
                for lineno, in_lock in _calls_under(stmt, helpers, in_lock=False)
                if not in_lock
            ]
            assert not unprotected, (
                f"{qualname} calls a *_locked helper outside _state_lock at {unprotected}"
            )
