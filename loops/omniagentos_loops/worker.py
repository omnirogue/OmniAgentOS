"""``python -m omniagentos_loops.worker`` — one tick of one loop instance.

Launched by ``omniagentos/scheduler/loop_jobs.py`` (a routines-tick builtin) as
a short-lived child process. It opens no port, serves nothing, and talks to the
control plane over the local SQLite store — the security requirement "workers
are launchd children hitting :8485 loopback only, no new listening ports" is met
by never opening a socket at all.

Tools are supplied by an *instance module*: a python module exposing
``register(ctx) -> None`` (or ``TOOLS``) that registers this instance's
``LoopTool``s. W2 (inbox triage) and W3 (error monitor) each ship one; the
runtime ships none, because a runtime that owns tools owns credentials.
"""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import sys
from typing import Any

from omniagentos_loops import parent_seam
from omniagentos_loops.contracts import LoopStatus, RunReport
from omniagentos_loops.paths import require_safe_name
from omniagentos_loops.runtime import LoopContext, open_store, run_once
from omniagentos_loops.templates import get_template

logger = logging.getLogger(__name__)

#: Instance modules must live under this prefix — a loop routine row can name a
#: module, and an unconstrained import path in a scheduler-driven subprocess is
#: arbitrary code execution from a database row.
INSTANCE_MODULE_PREFIX = "omniagentos_loops.instances."


def load_instance_tools(ctx: LoopContext, module_name: str) -> None:
    """Import an instance module and let it register its tools on *ctx*."""
    if not module_name.startswith(INSTANCE_MODULE_PREFIX):
        raise ValueError(
            f"instance module {module_name!r} must start with {INSTANCE_MODULE_PREFIX!r}"
        )
    module = importlib.import_module(module_name)
    register = getattr(module, "register", None)
    if register is None:
        raise ValueError(f"instance module {module_name!r} exposes no register(ctx)")
    register(ctx)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--template", required=True)
    parser.add_argument("--instance", required=True)
    parser.add_argument("--params", default="{}", help="JSON object")
    parser.add_argument("--instance-module", default="", help="module exposing register(ctx)")
    parser.add_argument(
        "--db", default="", help="control-plane sqlite path (default: repo default)"
    )
    parser.add_argument("--project-id", default="")
    parser.add_argument(
        "--effect-socket",
        default="",
        help=(
            "AF_UNIX path of the PARENT's credentialed-effect seam for this tick. "
            "Passed in argv, never in the environment: the worker's environment is "
            "scrubbed of everything credential-shaped and that filter must not be "
            "asked to make exceptions (see omniagentos.scheduler.loop_effects)."
        ),
    )
    return parser


def run(args: argparse.Namespace, *, ctx_factory: Any = None) -> RunReport:
    template_name = require_safe_name(args.template, kind="template")
    instance_id = require_safe_name(args.instance, kind="instance")
    params = json.loads(args.params or "{}")
    if not isinstance(params, dict):
        raise ValueError("--params must be a JSON object")

    # Bind the credential seam BEFORE any tool registers: an instance module's
    # register(ctx) may capture whether a capability is reachable, and a tool
    # that thinks there is no seam would declare every effect unavailable.
    parent_seam.configure(getattr(args, "effect_socket", "") or "")

    template = get_template(template_name)
    if ctx_factory is not None:
        ctx = ctx_factory(template_name, instance_id, params)
    else:
        ctx = LoopContext(
            instance_id=instance_id,
            template=template_name,
            params=params,
            store=open_store(args.db or None),
            project_id=args.project_id or None,
        )
        if args.instance_module:
            load_instance_tools(ctx, args.instance_module)
    return run_once(ctx, template)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    args = build_parser().parse_args(argv)
    try:
        report = run(args)
    except Exception as exc:  # noqa: BLE001 — the tick reports, it does not crash
        logger.exception("loop worker failed")
        report = RunReport(
            args.instance,
            args.template,
            LoopStatus.FAILED,
            detail=f"{type(exc).__name__}: {exc}",
        )
    print(json.dumps(report.as_dict(), sort_keys=True, default=str))
    return 0 if report.status is not LoopStatus.FAILED else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["INSTANCE_MODULE_PREFIX", "build_parser", "load_instance_tools", "main", "run"]
