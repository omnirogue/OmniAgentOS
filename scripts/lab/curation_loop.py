#!/usr/bin/env python3
"""Observe-first curation loop for the self-improvement lab (N10).

The lab can already build a 20-slot explore/exploit portfolio
(``omniagentos.lab.campaign.propose_experiments``) but nothing calls it on a
schedule, so the curation loop never runs.  This runner closes that gap in the
weakest possible way: it proposes, serializes the proposals to a timestamped
artifact, and exits.

Observe-only is enforced structurally, not by convention:

* ``propose_experiments`` persists what it proposes (``store.create_experiment``)
  and versions a challenger surface on disk (``surfaces.version_prompt``).  So the
  runner never hands it live state: the lab database is **copied** into a throwaway
  sandbox and ``omniagentos.lab.surfaces._repository_root`` is redirected at that
  same sandbox for the duration of the pass.  Every write the campaign performs
  lands in a directory that is deleted on exit.
* The runner fingerprints the live database's campaign tables before and after the
  pass and records ``campaign_fingerprint_before``/``_after`` in the artifact.  A
  mismatch is a hard failure (exit 4), so a future regression that reintroduces
  live writes cannot pass silently.
* Nothing here calls ``run_experiment``, ``decide``, or any promotion API.

N4r lesson (a launchd job that died with exit 126 because its program was not
executable): ``preflight_problems`` asserts this file and ``run_curation.sh`` are
mode 0755 before any work happens, and ``plist_problems`` asserts a rendered
plist's ``ProgramArguments[0]`` is an absolute path that exists and carries the
executable bit.  ``run`` refuses to start when preflight fails, so the job cannot
reach launchd in a state that reproduces exit 126.

Subcommands
-----------
``run``        observe pass -> ``var/lab/curation/proposals-<utc>.json``
``render``     render ``ops/launchd/com.omniagentos.lab-curation.plist.template``
``self-test``  preflight + (optionally) validate a rendered plist

Nothing in this file loads or bootstraps a launchd job; installation stays a
documented, human-run step (see ``docs/runbooks/lab-curation-loop.md``).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts.lib.plist_write import write_plist_atomic

LABEL = "com.omniagentos.lab-curation"
RUNNER_MODE = 0o755
DEFAULT_HOUR = 3
DEFAULT_MINUTE = 20

# D2: off→shadow→enforce gate (default off). Shadow and enforce both run the
# observe-only pass; off is a no-op so the scheduled job is inert until armed.
LAB_CURATION_MODE_ENV = "OMNIAGENTOS_LAB_CURATION_MODE"
LAB_CURATION_MODES = ("off", "shadow", "enforce")
DEFAULT_LAB_CURATION_MODE = "off"


def lab_curation_mode() -> str:
    raw = os.environ.get(LAB_CURATION_MODE_ENV, DEFAULT_LAB_CURATION_MODE)
    value = str(raw or DEFAULT_LAB_CURATION_MODE).strip().lower()
    if value in LAB_CURATION_MODES:
        return value
    return DEFAULT_LAB_CURATION_MODE

# The campaign-owned tables. If a pass changes any of these, observe-only is broken.
_FINGERPRINTED_TABLES = (
    "experiments",
    "surfaces",
    "champions",
    "eval_results",
    "judge_records",
    "tournaments",
)

_PROPOSAL_FIELDS = (
    "id",
    "discipline",
    "hypothesis",
    "explore_policy",
    "mutable_surface_kind",
    "champion_surface_id",
    "challenger_surface_id",
    "eval_suite_id",
    "dataset_hash",
    "primary_metric",
    "status",
    "created_at",
)


def repo_root() -> Path:
    """Return the checkout root, independent of the caller's working directory."""
    return Path(__file__).resolve().parents[2]


def runner_path() -> Path:
    return Path(__file__).resolve()


def wrapper_path() -> Path:
    return runner_path().parent / "run_curation.sh"


def template_path() -> Path:
    return repo_root() / "ops" / "launchd" / f"{LABEL}.plist.template"


def utc_stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


# --- N4r guard: executable-bit and plist preflight ------------------------------


def _mode_problem(path: Path, label: str) -> str | None:
    if not path.exists():
        return f"{label} is missing: {path}"
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode != RUNNER_MODE:
        return f"{label} must be mode {RUNNER_MODE:04o}, found {mode:04o}: {path}"
    if not os.access(path, os.X_OK):
        return f"{label} is not executable by this user: {path}"
    return None


def preflight_problems() -> list[str]:
    """Return every reason this job would fail the way N4r's job did (exit 126)."""
    problems: list[str] = []
    for path, label in ((runner_path(), "runner"), (wrapper_path(), "wrapper")):
        problem = _mode_problem(path, label)
        if problem is not None:
            problems.append(problem)
    wrapper = wrapper_path()
    if wrapper.exists():
        first_line = wrapper.read_text(encoding="utf-8").splitlines()[:1]
        if not first_line or not first_line[0].startswith("#!"):
            problems.append(f"wrapper has no shebang line: {wrapper}")
    return problems


def plist_problems(path: Path) -> list[str]:
    """Validate a *rendered* plist without loading it into any launchd domain."""
    problems: list[str] = []
    if not path.exists():
        return [f"rendered plist is missing: {path}"]

    plutil = shutil.which("plutil")
    if plutil is not None:
        lint = subprocess.run(  # noqa: S603 - fixed argv, operator-supplied path only
            [plutil, "-lint", str(path)], capture_output=True, text=True, check=False
        )
        if lint.returncode != 0:
            problems.append(f"plutil -lint failed: {lint.stdout.strip()} {lint.stderr.strip()}")

    try:
        data: Any = plistlib.loads(path.read_bytes())
    except (plistlib.InvalidFileException, ValueError) as error:
        return [*problems, f"plist is not parseable: {error}"]
    if not isinstance(data, dict):
        return [*problems, "plist root is not a dict"]

    if "{{" in path.read_text(encoding="utf-8"):
        problems.append("plist still contains unrendered {{PLACEHOLDER}} markers")
    if data.get("RunAtLoad", False) is not False:
        problems.append("RunAtLoad must be false for an observe-first job")
    if not data.get("Label"):
        problems.append("plist has no Label")

    args = data.get("ProgramArguments")
    if not isinstance(args, list) or not args:
        problems.append("ProgramArguments must be a non-empty array")
    else:
        program = Path(str(args[0]))
        if not program.is_absolute():
            problems.append(f"ProgramArguments[0] must be an absolute path: {program}")
        elif not program.exists():
            problems.append(f"ProgramArguments[0] does not exist: {program}")
        elif not os.access(program, os.X_OK):
            # This is the exit-126 failure mode, caught before install.
            problems.append(f"ProgramArguments[0] is not executable: {program}")

    for key in ("StandardOutPath", "StandardErrorPath"):
        value = data.get(key)
        if not value:
            problems.append(f"{key} is required so launchd failures are recoverable")
            continue
        target = Path(str(value))
        if not target.is_absolute():
            problems.append(f"{key} must be an absolute path: {target}")
        elif not target.parent.is_dir():
            problems.append(f"{key} directory does not exist: {target.parent}")

    if "StartCalendarInterval" not in data and "StartInterval" not in data:
        problems.append("no StartCalendarInterval/StartInterval: the job would never run")
    return problems


# --- plist rendering ------------------------------------------------------------


def _xml_escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_plist(
    template: str,
    *,
    label: str,
    program_args: Sequence[str],
    working_dir: str,
    hour: int,
    minute: int,
    stdout_path: str,
    stderr_path: str,
) -> str:
    """Fill the launchd template. Pure string work — never invokes launchctl."""
    args_xml = "\n".join(f"        <string>{_xml_escape(arg)}</string>" for arg in program_args)
    replacements = {
        "{{LABEL}}": _xml_escape(label),
        "{{PROGRAM_ARGS}}": "<array>\n" + args_xml + "\n    </array>",
        "{{WORKING_DIR}}": _xml_escape(working_dir),
        "{{HOUR}}": str(int(hour)),
        "{{MINUTE}}": str(int(minute)),
        "{{STDOUT_PATH}}": _xml_escape(stdout_path),
        "{{STDERR_PATH}}": _xml_escape(stderr_path),
    }
    rendered = template
    for placeholder, value in replacements.items():
        rendered = rendered.replace(placeholder, value)
    return rendered


def render_default_plist(
    target: Path,
    *,
    root: Path | None = None,
    label: str = LABEL,
    hour: int = DEFAULT_HOUR,
    minute: int = DEFAULT_MINUTE,
) -> Path:
    """Render the shipped template for this checkout and return the written path."""
    checkout = (root or repo_root()).resolve()
    log_dir = checkout / "var" / "log"
    log_dir.mkdir(parents=True, exist_ok=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    rendered = render_plist(
        template_path().read_text(encoding="utf-8"),
        label=label,
        program_args=[str(checkout / "scripts" / "lab" / "run_curation.sh")],
        working_dir=str(checkout),
        hour=hour,
        minute=minute,
        stdout_path=str(log_dir / "lab-curation.log"),
        stderr_path=str(log_dir / "lab-curation.log"),
    )
    write_plist_atomic(target, rendered)
    return target


# --- observe-only sandbox -------------------------------------------------------


def campaign_fingerprint(db_path: Path) -> str | None:
    """Hash the campaign-owned rows of the live database, read-only.

    Deliberately *not* a hash of the file: sqlite rewrites pages and checkpoints a
    WAL for reasons that have nothing to do with this job, and a byte-level check
    would cry wolf. What must not change is the campaign's own state, so the
    fingerprint covers exactly those tables and is stable across page churn.
    """
    if not db_path.exists():
        return None
    digest = hashlib.sha256()
    try:
        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:  # pragma: no cover - unreadable/locked database
        return None
    try:
        present = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        for table in _FINGERPRINTED_TABLES:
            if table not in present:
                continue
            rows = connection.execute(f"SELECT * FROM {table}").fetchall()  # noqa: S608 - fixed names
            digest.update(table.encode())
            for row in sorted(repr(tuple(row)) for row in rows):
                digest.update(row.encode())
    except sqlite3.Error:  # pragma: no cover - corrupt/foreign schema
        return None
    finally:
        connection.close()
    return digest.hexdigest()


@contextmanager
def observe_sandbox(db_path: Path) -> Iterator[tuple[str, Path]]:
    """Yield ``(sandbox_db_path, sandbox_root)`` that campaign writes may safely hit.

    The live database is copied, never opened for writing, and the lab's surface
    root is redirected so ``version_prompt``/``version_genome`` write their
    challenger files into the sandbox instead of ``vault/`` in the checkout.
    """
    from omniagentos.lab import surfaces

    with tempfile.TemporaryDirectory(prefix="lab-curation-observe-") as tmp:
        sandbox = Path(tmp)
        sandbox_db = sandbox / "lab-observe.db"
        if db_path.exists():
            shutil.copy2(db_path, sandbox_db)
            for suffix in ("-wal", "-shm"):
                sidecar = db_path.with_name(db_path.name + suffix)
                if sidecar.exists():
                    shutil.copy2(sidecar, sandbox_db.with_name(sandbox_db.name + suffix))
        original = surfaces._repository_root
        setattr(surfaces, "_repository_root", lambda: sandbox)  # noqa: B010 - module patch
        try:
            yield str(sandbox_db), sandbox
        finally:
            setattr(surfaces, "_repository_root", original)  # noqa: B010 - module patch


def _proposal_payload(proposal: Any) -> dict[str, Any]:
    if hasattr(proposal, "model_dump"):
        raw: dict[str, Any] = proposal.model_dump(mode="json")
    else:  # pragma: no cover - alternate campaign return shapes
        raw = dict(proposal)
    return {field: raw.get(field) for field in _PROPOSAL_FIELDS}


def discover_disciplines(store: Any) -> list[str]:
    rows = store.discipline_summaries()
    return sorted({str(row["discipline"]) for row in rows if row.get("discipline")})


def collect_proposals(
    store: Any, disciplines: Sequence[str]
) -> tuple[list[dict[str, Any]], list[str]]:
    """Propose for each discipline. Read-only with respect to the caller's state."""
    from omniagentos.lab.campaign import propose_experiments

    proposals: list[dict[str, Any]] = []
    errors: list[str] = []
    for discipline in disciplines:
        try:
            for proposal in propose_experiments(store, discipline):
                proposals.append(_proposal_payload(proposal))
        except (LookupError, RuntimeError, ValueError, OSError) as error:
            errors.append(f"{discipline}: {type(error).__name__}: {error}")
    return proposals, errors


def observe(
    *, db_path: Path, disciplines: Sequence[str] | None = None
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """Run one observe-only proposal pass. Returns (proposals, disciplines, errors)."""
    from omniagentos.lab.db import LabStore

    with observe_sandbox(db_path) as (sandbox_db, _sandbox_root):
        store = LabStore(sandbox_db)
        names = list(disciplines) if disciplines else discover_disciplines(store)
        proposals, errors = collect_proposals(store, names)
    return proposals, names, errors


# --- subcommands ----------------------------------------------------------------


def _log_line(log_path: Path, message: str) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"{stamp} lab-curation: {message}\n")


def cmd_run(args: argparse.Namespace) -> int:
    problems = preflight_problems()
    if problems:
        for problem in problems:
            print(f"preflight: {problem}", file=sys.stderr)
        return 3

    mode = lab_curation_mode()
    root = repo_root()
    log_path = Path(args.log_path or root / "var" / "log" / "lab-curation.log")
    if mode == "off":
        _log_line(
            log_path,
            f"mode=off ({LAB_CURATION_MODE_ENV}); skipping observe pass",
        )
        if not args.quiet:
            print(f"lab-curation mode=off; set {LAB_CURATION_MODE_ENV}=shadow|enforce to arm")
        return 0

    from omniagentos.contracts import default_db_path

    db_path = Path(args.db_path or default_db_path()).resolve()
    out_dir = Path(args.out_dir or root / "var" / "lab" / "curation")

    before = campaign_fingerprint(db_path)
    proposals, disciplines, errors = observe(db_path=db_path, disciplines=args.discipline)
    after = campaign_fingerprint(db_path)

    artifact = {
        "label": LABEL,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "mode": mode,
        "observe_only": True,
        "promoted": [],
        "executed": [],
        "db_path": str(db_path),
        "db_exists": db_path.exists(),
        "campaign_fingerprint_before": before,
        "campaign_fingerprint_after": after,
        "disciplines": disciplines,
        "proposal_count": len(proposals),
        "proposals": proposals,
        "errors": errors,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = out_dir / f"proposals-{utc_stamp()}.json"
    artifact_path.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    _log_line(
        log_path,
        f"observed {len(proposals)} proposal(s) across {len(disciplines)} discipline(s) "
        f"-> {artifact_path}" + (f"; errors={len(errors)}" if errors else ""),
    )
    if not args.quiet:
        print(str(artifact_path))

    if before != after:
        # Observe-only was violated: the pass mutated live campaign state.
        print("observe-only violation: campaign state changed during the pass", file=sys.stderr)
        _log_line(log_path, "observe-only violation: campaign fingerprint changed")
        return 4
    return 0


def cmd_render(args: argparse.Namespace) -> int:
    root = repo_root()
    target = Path(args.target or root / "var" / "launchd" / "rendered" / f"{LABEL}.plist")
    written = render_default_plist(
        target, root=root, label=args.label, hour=args.hour, minute=args.minute
    )
    print(str(written))
    return 0


def cmd_self_test(args: argparse.Namespace) -> int:
    problems = preflight_problems()
    if args.plist:
        problems.extend(f"plist: {item}" for item in plist_problems(Path(args.plist)))
    for problem in problems:
        print(f"FAIL {problem}", file=sys.stderr)
    if problems:
        return 3
    print("ok: runner and wrapper are 0755" + (", rendered plist is valid" if args.plist else ""))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="curation_loop", description=__doc__)
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="observe-only proposal pass (default)")
    run.add_argument("--db-path", default=None, help="override the lab db path")
    run.add_argument(
        "--discipline",
        action="append",
        default=None,
        help="propose for this discipline only (repeatable; default: every known discipline)",
    )
    run.add_argument("--out-dir", default=None, help="artifact directory")
    run.add_argument("--log-path", default=None, help="append-only run log")
    run.add_argument("--quiet", action="store_true", help="do not print the artifact path")
    run.set_defaults(func=cmd_run)

    render = sub.add_parser("render", help="render the launchd plist template (never loads it)")
    render.add_argument("--target", default=None, help="output plist path")
    render.add_argument("--label", default=LABEL)
    render.add_argument("--hour", type=int, default=DEFAULT_HOUR)
    render.add_argument("--minute", type=int, default=DEFAULT_MINUTE)
    render.set_defaults(func=cmd_render)

    self_test = sub.add_parser("self-test", help="N4r guard: exec bits + rendered plist")
    self_test.add_argument("--plist", default=None, help="also validate this rendered plist")
    self_test.set_defaults(func=cmd_self_test)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    raw = list(argv) if argv is not None else sys.argv[1:]
    if not raw or raw[0].startswith("-"):
        raw = ["run", *raw]  # bare invocation means the scheduled observe pass
    args = build_parser().parse_args(raw)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
