"""Bench runner — B0/B1 baseline ladder over task files.

CLI: ``python -m omniagentos.harnesses.bench --arms b0,b1 --tasks devtasks
--limit N [--replicates R] [--dry-run]``
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from omniagentos.contracts import (
    AgentInput,
    AgentResult,
    Arm,
    HarnessProfile,
    HarnessType,
    ResultStatus,
    RunManifest,
    RunState,
    default_db_path,
    default_ledger_dir,
    default_vault_dir,
    digest,
    new_id,
    utc_now_iso,
)
from omniagentos.harnesses.envhash import env_hash
from omniagentos.wrapper.durable import finalize_manifest

# arm name → (Arm enum, HarnessType for HarnessProfile)
_ARM_TABLE: dict[str, tuple[Arm, HarnessType]] = {
    "b0": (Arm.B0, HarnessType.CLI_CLAUDE),
    "b1": (Arm.B1, HarnessType.MINI_SWE),
    "champion": (Arm.CHAMPION, HarnessType.FUSION),
}


def load_tasks(tasks_dir: str | Path) -> list[dict[str, Any]]:
    """Load ``task_*`` YAML/JSON files from *tasks_dir* (sorted by filename).

    The benchmark corpus shares ``devtasks/`` with coordination metadata and
    planning artifacts.  Only filename-scoped task files belong to this loader;
    a malformed ``task_*`` file still fails closed below.
    """
    root = Path(tasks_dir)
    if not root.is_dir():
        raise FileNotFoundError(f"tasks directory not found: {root}")

    tasks: list[dict[str, Any]] = []
    for path in sorted(root.iterdir()):
        if not path.is_file():
            continue
        if not path.name.startswith("task_") or path.suffix.lower() not in {
            ".json",
            ".yaml",
            ".yml",
        }:
            continue
        data = _load_task_file(path)
        for key in ("id", "prompt", "discipline"):
            if key not in data:
                raise ValueError(f"task file {path} missing required field {key!r}")
        tasks.append(
            {
                "id": str(data["id"]),
                "prompt": str(data["prompt"]),
                "discipline": str(data["discipline"]),
                "path": str(path),
            }
        )
    return tasks


def _load_task_file(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        data = json.loads(text)
    else:
        import yaml

        data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError(f"task file {path} must be a mapping, got {type(data).__name__}")
    return data


def resolve_arm_adapter(arm: str, *, dry_run: bool = False) -> Any:
    """Return an AgentAdapter for *arm*.

    --dry-run always yields MockAdapter (no external calls).
    B0 → cli-claude via resolve_adapter; B1 → MiniSweAdapter (direct or registry).
    """
    if dry_run:
        from omniagentos.mock_adapter import MockAdapter

        return MockAdapter()

    key = arm.lower().strip()
    if key == "b0":
        from omniagentos.harnesses.bench.b0 import _resolve_cli_claude

        return _resolve_cli_claude()

    if key == "b1":
        return _resolve_miniswe()

    if key == "grok":
        # First-class benchmark arm (not a contracts.Arm member: the Arm enum is
        # frozen at b0/b1/champion). Resolved through the adapter registry so the
        # bench ladder and production share one GrokAdapter construction path.
        return _resolve_via_registry(HarnessType.CLI_GROK)

    if key == "champion":
        # Champion is out of scope for p11 baseline; prefer registry if present.
        return _resolve_via_registry(HarnessType.FUSION)

    raise KeyError(f"unknown arm {arm!r}; known: {sorted(_ARM_TABLE)}")


def _resolve_miniswe() -> Any:
    try:
        resolve = _import_resolve_adapter()
        return resolve(HarnessType.MINI_SWE)
    except (ImportError, KeyError, AttributeError, TypeError):
        from omniagentos.harnesses.miniswe.adapter import MiniSweAdapter

        return MiniSweAdapter()


def _resolve_via_registry(harness: HarnessType) -> Any:
    resolve = _import_resolve_adapter()
    return resolve(harness)


def _import_resolve_adapter() -> Callable[[HarnessType], Any]:
    from typing import cast

    try:
        from omniagentos.adapters import resolve_adapter  # type: ignore[attr-defined]

        return cast(Callable[[HarnessType], Any], resolve_adapter)
    except (ImportError, AttributeError):
        pass
    from omniagentos.adapters.registry import (  # type: ignore[import-untyped,import-not-found]
        resolve_adapter,
    )

    return cast(Callable[[HarnessType], Any], resolve_adapter)


def build_manifest(
    *,
    run_id: str,
    task: dict[str, Any],
    arm: Arm,
    harness_type: HarnessType,
    adapter: Any,
    result: AgentResult,
    started_at: str,
    finished_at: str,
    dry_run: bool = False,
    replicate: int = 0,
) -> RunManifest:
    """Build a terminal RunManifest with arm + harness identity + env_hash."""
    state = RunState.COMPLETED if result.status is ResultStatus.OK else RunState.FAILED
    params: dict[str, Any] = {}
    if dry_run:
        params["dry_run"] = True
    if replicate:
        params["replicate"] = replicate

    profile = HarnessProfile(
        harness=harness_type,
        version=str(getattr(adapter, "version", "") or ""),
        env_hash=env_hash(),
        params=params,
    )
    output_digest = digest(result.output_text) if result.output_text else None
    return RunManifest(
        run_id=run_id,
        task_id=str(task["id"]),
        discipline=str(task.get("discipline") or "") or None,
        arm=arm,
        harness=profile,
        agent=str(getattr(adapter, "name", None) or "") or None,
        model=None,
        state=state,
        started_at=started_at,
        finished_at=finished_at,
        usage=result.usage,
        output_digest=output_digest,
        extra={"error": result.error} if result.error else {},
    )


def run_bench(
    *,
    arms: list[str],
    tasks_dir: str | Path,
    limit: int | None = None,
    replicates: int = 1,
    dry_run: bool = False,
    ledger_dir: str | None = None,
    vault_dir: str | None = None,
    db_path: str | None = None,
    working_dir: str = ".",
    append: bool = True,
) -> list[RunManifest]:
    """Execute task × arm × replicate and return manifests (also print JSONL)."""
    tasks = load_tasks(tasks_dir)
    if limit is not None:
        tasks = tasks[: max(0, limit)]
    if replicates < 1:
        raise ValueError("replicates must be >= 1")

    resolved_arms: list[tuple[str, Arm, HarnessType]] = []
    for raw in arms:
        key = raw.lower().strip()
        if key not in _ARM_TABLE:
            raise KeyError(f"unknown arm {raw!r}; known: {sorted(_ARM_TABLE)}")
        arm_enum, harness_type = _ARM_TABLE[key]
        resolved_arms.append((key, arm_enum, harness_type))

    ledger = ledger_dir if ledger_dir is not None else default_ledger_dir()
    vault = vault_dir if vault_dir is not None else default_vault_dir()
    database = db_path if db_path is not None else default_db_path()

    manifests: list[RunManifest] = []
    for task in tasks:
        for arm_key, arm_enum, harness_type in resolved_arms:
            adapter = resolve_arm_adapter(arm_key, dry_run=dry_run)
            for rep in range(replicates):
                run_id = new_id("run")
                started_at = utc_now_iso()
                agent_input = AgentInput(
                    run_id=run_id,
                    task_id=str(task["id"]),
                    prompt=str(task["prompt"]),
                    working_dir=working_dir,
                    tools_allowed=[],
                    metadata={"arm": arm_key, "dry_run": dry_run, "replicate": rep},
                )
                if dry_run:
                    # Keep mock replies deterministic & cheap.
                    agent_input.metadata["mock"] = {
                        "reply": f"dry-run ok arm={arm_key} task={task['id']}",
                    }

                if arm_key == "b0" and not dry_run:
                    from omniagentos.harnesses.bench.b0 import run_b0_arm

                    result = run_b0_arm(agent_input)
                else:
                    result = adapter.run(agent_input)

                finished_at = utc_now_iso()
                manifest = build_manifest(
                    run_id=run_id,
                    task=task,
                    arm=arm_enum,
                    harness_type=harness_type,
                    adapter=adapter,
                    result=result,
                    started_at=started_at,
                    finished_at=finished_at,
                    dry_run=dry_run,
                    replicate=rep,
                )
                manifests.append(manifest)
                line = json.dumps(manifest.model_dump(mode="json"), sort_keys=True)
                print(line, flush=True)
                if append:
                    try:
                        finalize_manifest(
                            manifest,
                            task_title=str(task["id"]),
                            task_input={"prompt": task["prompt"], "path": task["path"]},
                            output_text=result.output_text,
                            ledger_dir=ledger,
                            vault_dir=vault,
                            db_path=database,
                        )
                    except Exception as exc:  # noqa: BLE001
                        raise RuntimeError(
                            f"durable finalization failed for run {manifest.run_id}: {exc}"
                        ) from exc
    return manifests


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m omniagentos.harnesses.bench",
        description="Baseline ladder bench runner (B0/B1) over task files.",
    )
    p.add_argument(
        "--arms",
        required=True,
        help="Comma-separated arm names (b0,b1,champion)",
    )
    p.add_argument(
        "--tasks",
        required=True,
        help="Path to tasks directory (YAML/JSON files)",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Limit to first N tasks",
    )
    p.add_argument(
        "--replicates",
        type=int,
        default=1,
        help="Replicates per task×arm (default 1)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Use MockAdapter (no external calls)",
    )
    p.add_argument(
        "--ledger-dir",
        default=None,
        help="Ledger directory for append_manifest (default OMNIAGENTOS_LEDGER_DIR/ledger)",
    )
    p.add_argument(
        "--vault-dir",
        default=None,
        help="Vault directory for per-run notes (default OMNIAGENTOS_VAULT_DIR/vault)",
    )
    p.add_argument(
        "--db-path",
        default=None,
        help="SQLite Store path (default OMNIAGENTOS_DB/var/omniagentos.db)",
    )
    p.add_argument(
        "--working-dir",
        default=".",
        help="Working directory passed to adapters",
    )
    p.add_argument(
        "--no-append",
        action="store_true",
        help="Do not call append_manifest (stdout only)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    if not arms:
        print("error: --arms must list at least one arm", file=sys.stderr)
        return 2
    try:
        run_bench(
            arms=arms,
            tasks_dir=args.tasks,
            limit=args.limit,
            replicates=args.replicates,
            dry_run=args.dry_run,
            ledger_dir=args.ledger_dir,
            vault_dir=args.vault_dir,
            db_path=args.db_path,
            working_dir=args.working_dir,
            append=not args.no_append,
        )
    except (FileNotFoundError, KeyError, RuntimeError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0
