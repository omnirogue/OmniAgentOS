"""Loop registration: the existing ``routines`` table, not a second registry.

MIGRATION_ARCHITECTURE.md is explicit — *do not build a second registry*. A
registered loop is a routine row whose ``task_template.input`` names this
runtime:

.. code-block:: json

    {"title": "...", "harness": "mock",
     "input": {"module": "omniagentos.loops",
               "kind": "loop",
               "template": "poll_classify_act_verify",
               "instance_id": "inbox_triage",
               "instance_module": "omniagentos_loops.instances.w2_inbox_triage",
               "params": {"...": "..."}}}

Everything the registry already owns comes for free: cron/event triggers,
enable/disable (``status``), the revision CAS (migration 097), ``last_fired``,
and the acceptance floor. ``omniagentos/scheduler/loop_jobs.py`` is the
production side of this contract and re-derives the same fields.

This module is import-light on purpose (no LangGraph): the row shape must be
readable from any venv.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

#: The ``task_template.input.module`` value that routes a routine to this runtime.
LOOP_MODULE = "omniagentos.loops"
LOOP_KIND = "loop"

#: Mirrors paths.SAFE_NAME_RE. Duplicated (not imported) because the production
#: hook validates the same shape without importing this package.
SAFE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

#: Mirrors ``worker.INSTANCE_MODULE_PREFIX`` and the same constant in
#: ``omniagentos/scheduler/loop_jobs.py``. A loop routine row names an import
#: path that a scheduler-launched subprocess will import, so the prefix is a
#: security boundary, not a naming convention.
INSTANCE_MODULE_PREFIX = "omniagentos_loops.instances."

#: One dotted segment of the module path AFTER the prefix.
_MODULE_SEGMENT_RE = re.compile(r"^[a-z_][a-z0-9_]*$")

DEFAULT_TIMEOUT_S = 600

# THERE IS NO DEFAULT GATE COMMAND, and adding one back is the defect.
#
# Until 2026-08-02 this module carried ``DEFAULT_GATE_COMMAND = "pytest
# tests/scheduler/test_loop_jobs.py"`` and ``loop_routine_row`` substituted it
# whenever an author omitted a gate. That suite proves the scheduler→worker
# mechanism — the same code for every loop — so it passes on every tick
# whatever the instance produced: a loop seeded without a gate settled
# favourable over garbage forever (rtn_1e5567b9f3314a2c9d76 reported 10 runs /
# 10 accepted / acceptance 1.0 while healing nothing).
#
# ``gate_command`` is therefore a REQUIRED parameter with no fallback, exactly
# like ``instance_module`` and for the same reason: the helper must be unable
# to express a row that cannot be judged. The production validator
# (``omniagentos.scheduler.routines._validate_loop_gate``) refuses such a row
# independently, so restoring a default here does not restore the behaviour —
# it just moves the refusal from this helper to the store.


class LoopSpecError(ValueError):
    """The routine row does not describe a runnable loop instance."""


@dataclass(frozen=True)
class LoopSpec:
    """The parsed, validated instruction carried by one routine row."""

    template: str
    instance_id: str
    instance_module: str
    params: dict[str, Any] = field(default_factory=dict)
    timeout_s: int = DEFAULT_TIMEOUT_S
    project_id: str | None = None


def _require_instance_module(value: Any) -> str:
    """The module that registers this instance's tools, or refuse the row.

    A loop row that names no instance module cannot possibly run: the worker
    registers NO tools of its own (a runtime that owns tools owns credentials),
    so every template's ``required_tools`` check fails and the tick reports
    ``instance is missing required tools: [...]`` ten minutes after seeding.
    That is exactly what happened in production on 2026-08-01 — the helper below
    could not express the field, and the failure surfaced at tick time instead of
    at creation time. Refusing here makes an unrunnable row impossible to build.

    The prefix is enforced with the same rule the worker applies before
    ``importlib.import_module``: a routine row is DATA, and an unconstrained
    import path in a scheduler-launched subprocess is arbitrary code execution.
    """
    module = str(value or "").strip()
    if not module:
        raise LoopSpecError(
            "a loop routine must name its instance_module — the "
            f"{INSTANCE_MODULE_PREFIX}<name> module whose register(ctx) supplies "
            "this instance's tools; without it every tick fails on missing tools"
        )
    if not module.startswith(INSTANCE_MODULE_PREFIX):
        raise LoopSpecError(
            f"loop instance_module must start with {INSTANCE_MODULE_PREFIX!r}: {module!r}"
        )
    suffix = module[len(INSTANCE_MODULE_PREFIX) :]
    if not suffix or not all(_MODULE_SEGMENT_RE.match(part) for part in suffix.split(".")):
        raise LoopSpecError(f"loop instance_module is not a valid module path: {module!r}")
    return module


def _require_gate_command(value: Any) -> str:
    """The objective verifier this loop is judged by, or refuse the row.

    A loop tick reports its OWN status; the only thing authorised to contradict
    it is this command, which ``routines_settle`` executes in the gate workspace
    on every tick. A row that carries no gate is therefore a row that grades
    itself, and the substituted default that used to fill this field graded the
    scheduler instead of the loop (see the note above
    :class:`LoopSpecError`) — so it always agreed.

    Only emptiness is decided here. WHICH commands are objective verifiers, and
    which ones are vacuous for a loop, is decided by
    ``omniagentos.scheduler.routines.validate_routine`` — the one authority,
    which this import-light module deliberately does not duplicate and
    ``loops/tests/test_registry.py`` runs against every row this helper builds.
    """
    command = str(value or "").strip()
    if not command:
        raise LoopSpecError(
            "a loop routine must declare gate_command — the objective verifier that "
            "goes RED when this instance's work is missing or wrong, e.g. "
            "'pytest loops/tests/instances/test_<instance>.py'. It is EXECUTED at "
            "settlement on every tick and is the only thing that can contradict the "
            "loop's own report, so there is deliberately no default: the one this "
            "helper used to substitute ('pytest tests/scheduler/test_loop_jobs.py') "
            "passes whatever the loop produced, and every loop seeded without a gate "
            "settled favourable forever."
        )
    return command


def loop_spec(task_template: dict[str, Any]) -> LoopSpec | None:
    """Parse a routine's ``task_template``; ``None`` when it is not a loop row."""
    payload = (task_template or {}).get("input") or {}
    if payload.get("module") != LOOP_MODULE:
        return None
    if payload.get("kind", LOOP_KIND) != LOOP_KIND:
        return None

    template = str(payload.get("template") or "")
    instance_id = str(payload.get("instance_id") or "")
    for value, kind in ((template, "template"), (instance_id, "instance_id")):
        if not SAFE_NAME_RE.match(value):
            raise LoopSpecError(f"loop routine has an invalid {kind}: {value!r}")

    instance_module = _require_instance_module(payload.get("instance_module"))

    params = payload.get("params") or {}
    if not isinstance(params, dict):
        raise LoopSpecError("loop routine params must be an object")

    try:
        timeout_s = int(payload.get("timeout_s") or DEFAULT_TIMEOUT_S)
    except (TypeError, ValueError):
        raise LoopSpecError("loop routine timeout_s must be an integer") from None

    return LoopSpec(
        template=template,
        instance_id=instance_id,
        instance_module=instance_module,
        params=dict(params),
        timeout_s=max(30, min(timeout_s, 3600)),
        project_id=(task_template or {}).get("project_id"),
    )


def loop_routine_row(
    *,
    name: str,
    template: str,
    instance_id: str,
    instance_module: str,
    gate_command: str,
    cron: str | None = None,
    event: str | None = None,
    params: dict[str, Any] | None = None,
    description: str = "",
    hard_cap_value: float = 5.0,
    notification_channel: str = "desktop",
    timeout_s: int = DEFAULT_TIMEOUT_S,
) -> dict[str, Any]:
    """A VALID ``routines`` payload for one loop instance (W2/W3 use this).

    ``instance_module`` is REQUIRED and has no default. It is the module whose
    ``register(ctx)`` supplies this instance's tools, and
    ``omniagentos/scheduler/loop_jobs.py`` passes it to the worker as
    ``--instance-module``. A row without it is never valid: the worker ships no
    tools of its own, so the tick dies on ``required_tools`` — which is how this
    helper shipped a row that could not run for a whole day (routine
    ``rtn_1e5567b9f3314a2c9d76``, ``stop_reason=builtin_failed`` every ten
    minutes).

    It is a parameter rather than a convention derived from ``instance_id``
    because the two names are NOT the same thing and the live rows prove it:
    instance ``w3_health_monitor`` registers from
    ``...instances.health_monitor``, and instance ``w2_inbox`` from
    ``...instances.w2_inbox_triage``. A derived default would have produced two
    import paths that do not exist, moving the failure from "helper refuses"
    back to "tick fails ten minutes later" — the exact defect being fixed. One
    module may also serve several instances (the same graph, different params),
    which no 1:1 rule can express.

    ``gate_command`` is REQUIRED for the same reason and has no default. It is
    EXECUTED, and it did not use to be — which is the whole point of choosing it
    carefully:

    * ``RoutinesStore._refuse_active_mock_harness`` (the D5 belt) refuses an
      ACTIVE ``harness='mock'`` row unless it ships a non-vacuous verifier
      command — so a loop row cannot use ``metric_threshold``;
    * a loop tick's *claim* is still produced per tick by
      ``loop_jobs.run_loop_job`` from the worker's status, but it is recorded as
      a claim (``routine_runs.self_reported_status``, migration 104) on a
      PENDING row with a real ``run_id``. The ``settle_pending`` pass at the end
      of the same tick then executes this command in the gate workspace and
      writes ``gate_passed`` from the signed evidence. A loop reporting
      ``completed`` whose gate fails settles ADVERSE.

    So this command is load-bearing on every tick: pick a suite that goes RED
    when THIS instance's work is missing or wrong, and that is hermetic — the
    gate runner's environment is sanitised down to PATH/HOME/LANG, so a verifier
    needing a credential or a live service is a Class P probe and does not
    belong in ``gate_config``.

    There is no default and there must not be one. The one this helper used to
    substitute — ``pytest tests/scheduler/test_loop_jobs.py`` — grades the
    scheduler→worker MECHANISM, which is identical for every loop, so it passed
    on every tick regardless of what the instance did: any loop seeded without
    an explicit gate settled favourable over garbage forever. The production
    validator refuses that command for a loop row outright
    (``routines._validate_loop_gate``), so a restored default here would only
    move the refusal from this helper to the store.

    A gate MAY name this instance's own tests under ``loops/``, and that is the
    stronger choice where they exist: ``gate_runner`` derives the interpreter
    from the resolved target path, so a ``loops/`` target executes on the loops
    venv — the same runtime the worker itself runs on — and everything else on
    the production venv. It did not always: for one merged commit every such
    gate died on ``ModuleNotFoundError: No module named 'langgraph'`` and settled
    a favourable tick as ADVERSE. One command may not mix the two trees; the gate
    refuses that outright rather than half-running it.

    ``status`` stays ``"active"``, deliberately and against the review that
    asked for ``"disabled"``. The argument for disabled is good — an operator
    should read the gate before the work starts — but the enable path cannot
    accept a loop row TODAY: every loop row is ``harness='mock'`` by
    construction, ``POST /api/routines/{id}/enable`` runs
    ``_assert_activation_harness`` which fails 400 on ``mock``
    (``omniagentos/api/routes/routines.py:361-367``), and
    ``RoutinesStore.set_status_cas`` re-refuses it without the create-time
    ``allow_mock_with_real_gate`` exemption (``store.py:542-543``). Both
    refusals are pinned by the counterfeit ``cf-enable-mock-harness``. Seeding
    disabled would therefore produce a row nobody can start except by a direct
    DB write — worse than the problem. A caller who wants the review-first shape
    can already have it (``{**loop_routine_row(...), "status": "disabled"}``);
    making it the default has to wait until the activation belt learns about
    mock-harness loop rows, with its own counterfeit.
    """
    module = _require_instance_module(instance_module)
    command = _require_gate_command(gate_command)
    if not cron and not event:
        raise LoopSpecError("a loop routine needs either a cron expression or an event name")
    trigger_type = "cron" if cron else "event"
    trigger_config = {"cron": cron} if cron else {"event": event}
    return {
        "name": name,
        "description": description or f"Loop {template}/{instance_id}",
        "trigger_type": trigger_type,
        "trigger_config": trigger_config,
        "task_template": {
            "title": description or f"Loop tick: {instance_id}",
            "harness": "mock",
            "input": {
                "module": LOOP_MODULE,
                "kind": LOOP_KIND,
                "template": template,
                "instance_id": instance_id,
                "instance_module": module,
                "params": dict(params or {}),
                "timeout_s": timeout_s,
            },
        },
        "gate_type": "test_command",
        "gate_config": {"command": command, "expected_exit_code": 0},
        "hard_cap_type": "budget_usd",
        "hard_cap_value": hard_cap_value,
        "notification_target": {"channel": notification_channel},
        "scope": "system",
        "purpose": "loop",
        "status": "active",
    }


__all__ = [
    "DEFAULT_TIMEOUT_S",
    "INSTANCE_MODULE_PREFIX",
    "LOOP_KIND",
    "LOOP_MODULE",
    "SAFE_NAME_RE",
    "LoopSpec",
    "LoopSpecError",
    "loop_routine_row",
    "loop_spec",
]
