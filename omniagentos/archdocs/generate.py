"""Deterministic inventory scanners + ARCHI.md regeneration (§8).

Four scans, all read-only, zero LLM involvement:
  - api routes:      regex over ``omniagentos/api/routes/*.py`` for ``@router.<verb>(...)``
  - migrations:      list ``omniagentos/db/migrations/*.sql`` (numbered)
  - launchd:         parse product-scoped ``com.omniagentos.*.plist`` for label + schedule
  - top-level pkgs:  list ``omniagentos/*/`` package directories

Each scan renders into a named ``<!-- generated:begin:<id> --> ... <!-- generated:end:<id> -->``
block. ``regenerate_archi()`` replaces ONLY the content between matching markers in the
existing file (or, if the file doesn't exist yet, writes the full bootstrap template) —
everything else, including ``## Notes (human)``, is preserved byte-for-byte.

CLI: ``python -m omniagentos.archdocs.generate [--repo-root DIR] [--check] [--update-status]``

``--update-status`` rewrites STATUS.md claims from repository evidence. Leave it off until
the integrated SHA is certified (L19); generators and tests still exercise the pure helpers.
"""

from __future__ import annotations

import argparse
import json
import plistlib
import re
import subprocess
from pathlib import Path
from typing import Any

from omniagentos.path_containment import inode_relative_parts_anchored

# ---------------------------------------------------------------------------
# Marker format shared with staleness.py / update.py
# ---------------------------------------------------------------------------

_MARKER_RE_TMPL = r"<!-- generated:begin:{id} -->\n(?P<body>.*?)<!-- generated:end:{id} -->\n?"
_ANY_MARKER_RE = re.compile(
    r"<!-- generated:begin:(?P<id>[a-z0-9_-]+) -->.*?<!-- generated:end:(?P=id) -->\n?",
    re.DOTALL,
)

_ROUTE_DECORATOR_RE = re.compile(
    r'@router\.(get|post|put|patch|delete)\(\s*["\']([^"\']*)["\']', re.MULTILINE
)
_LAUNCHD_GLOB = "com.omniagentos.*.plist"


def _repo_root_default() -> Path:
    """Best-effort repo root: walk up from this file to the directory containing ARCHI.md."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "ARCHI.md").exists() or (parent / ".git").exists():
            return parent
    return here.parents[2]


# ---------------------------------------------------------------------------
# Scanners (pure functions — repo_root/launchd_dir injectable for tests)
# ---------------------------------------------------------------------------


def scan_routes(repo_root: Path) -> list[tuple[str, str, str]] | None:
    """Return sorted (method, path, module) tuples scanned from api/routes/*.py.

    Three-valued: ``None`` when the routes directory is absent or unreadable
    (source could not be measured); ``[]`` when the directory exists and is
    readable but has no decorated routes; a non-empty list when routes were
    found. Callers must not treat ``None`` as empty — that is the favourable
    ``**0 routes**`` counterfeit.

    Unreadable, undecodable, or syntactically invalid route *modules* are NOT
    silently dropped or counted as real routes: they appear with method
    ``"UNPARSEABLE"`` so renderers never treat them as a measured empty
    inventory (``**0 routes across 0 modules.**``) or as a successful path
    match. An unreadable *directory* is ``None`` (enumeration failed), not
    measured-empty ``[]``.
    """
    routes_dir = repo_root / "omniagentos" / "api" / "routes"
    if not routes_dir.is_dir():
        return None
    # Directory present but unreadable must not become measured-empty [].
    # ``Path.glob`` can silently yield nothing under PermissionError-class
    # conditions; probe via ``iterdir`` so enumeration failure is None.
    try:
        pyfiles = sorted(p for p in routes_dir.iterdir() if p.is_file() and p.suffix == ".py")
    except OSError:
        return None
    found: list[tuple[str, str, str]] = []
    for pyfile in pyfiles:
        if pyfile.name == "__init__.py":
            continue
        try:
            # Strict UTF-8: errors="replace" would mask invalid bytes and let an
            # unparseable file collapse into the same empty scan as a genuine
            # empty module (measured zero counterfeit).
            text = pyfile.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            found.append(("UNPARSEABLE", pyfile.name, pyfile.stem))
            continue
        try:
            # Regex alone would treat syntax-invalid source as real routes when
            # a decorator string happens to match — unparseable must surface.
            compile(text, str(pyfile), "exec")
        except SyntaxError:
            found.append(("UNPARSEABLE", pyfile.name, pyfile.stem))
            continue
        for method, path in _ROUTE_DECORATOR_RE.findall(text):
            found.append((method.upper(), path, pyfile.stem))
    found.sort(key=lambda t: (t[2], t[1], t[0]))
    return found


def scan_migrations(repo_root: Path) -> list[tuple[int, str]] | None:
    """Return sorted (number, filename) pairs from db/migrations/*.sql.

    Three-valued: ``None`` when the migrations directory is absent or unreadable
    (source could not be measured); ``[]`` when the directory exists and is
    readable but has no numbered migrations; a non-empty list when migrations
    were found. Callers must not treat ``None`` as empty — that is the
    favourable ``**0 migrations**`` / ``current version 0`` counterfeit.

    An unreadable *directory* is ``None`` (enumeration failed), not
    measured-empty ``[]``. ``Path.glob`` can silently yield nothing under
    PermissionError-class conditions.
    """
    mig_dir = repo_root / "omniagentos" / "db" / "migrations"
    if not mig_dir.is_dir():
        return None
    # Directory present but unreadable must not become measured-empty [].
    # ``Path.glob`` can silently yield nothing under PermissionError-class
    # conditions; probe via ``iterdir`` so enumeration failure is None.
    try:
        sqlfiles = sorted(p for p in mig_dir.iterdir() if p.is_file() and p.suffix == ".sql")
    except OSError:
        return None
    out: list[tuple[int, str]] = []
    for sqlfile in sqlfiles:
        m = re.match(r"^(\d+)_", sqlfile.name)
        if m:
            out.append((int(m.group(1)), sqlfile.name))
    out.sort(key=lambda t: t[0])
    return out


def scan_launchd(launchd_dir: Path) -> list[tuple[str, str]] | None:
    """Return sorted (label, schedule-description) pairs from installed plists.

    Three-valued: ``None`` when the launchd directory is absent or unreadable
    (source could not be measured); ``[]`` when the directory exists and is
    readable but has no matching plists; a non-empty list when jobs were found.
    Callers must not treat ``None`` as empty — that is the favourable
    ``_(none found)_`` counterfeit.

    Unparseable/unreadable plists are NOT silently dropped: they appear with
    schedule ``"unparseable"`` so renderers never treat them as ``_(none found)_``.
    An unreadable *directory* is ``None`` (enumeration failed), not
    measured-empty ``[]``.
    """
    if not launchd_dir.is_dir():
        return None
    # Directory present but unreadable must not become measured-empty [].
    # ``Path.glob`` can silently yield nothing under PermissionError-class
    # conditions; probe via ``iterdir`` so enumeration failure is None.
    try:
        plist_paths = sorted(
            p for p in launchd_dir.iterdir() if p.is_file() and p.match(_LAUNCHD_GLOB)
        )
    except OSError:
        return None
    out: list[tuple[str, str]] = []
    for plist_path in plist_paths:
        try:
            with plist_path.open("rb") as fh:
                data = plistlib.load(fh)
        except Exception:
            # Unparseable must surface — never collapse to the same empty scan
            # as a genuinely empty directory (renders as favourable "none found").
            out.append((plist_path.stem, "unparseable"))
            continue
        label = data.get("Label", plist_path.stem)
        out.append((label, _describe_schedule(data)))
    out.sort(key=lambda t: t[0])
    return out


def _describe_schedule(data: dict) -> str:
    interval = data.get("StartInterval")
    if isinstance(interval, int):
        return f"every {interval}s"
    cal = data.get("StartCalendarInterval")
    if isinstance(cal, dict):
        return _describe_calendar_entry(cal)
    if isinstance(cal, list):
        return ", ".join(_describe_calendar_entry(e) for e in cal)
    return "keep-alive / on-demand"


def _describe_calendar_entry(entry: dict) -> str:
    hour = entry.get("Hour", "*")
    minute = entry.get("Minute", 0)
    minute_str = f"{minute:02d}" if isinstance(minute, int) else "*"
    return f"{hour}:{minute_str}"


def scan_packages(repo_root: Path) -> list[str] | None:
    """Return sorted top-level package names under ``omniagentos/``.

    Three-valued: ``None`` when the ``omniagentos/`` package root is absent
    (source could not be measured); ``[]`` when the directory exists but has no
    top-level packages; a non-empty list when packages were found. Callers must
    not treat ``None`` as empty — that is the favourable ``_(none found)_``
    counterfeit.
    """
    base = repo_root / "omniagentos"
    if not base.is_dir():
        return None
    return sorted(
        p.name
        for p in base.iterdir()
        if p.is_dir() and not p.name.startswith("__") and (p / "__init__.py").exists()
    )


def _git_rev(repo_root: Path, *args: str) -> str:
    """Return one git revision value, or ``"unknown"`` without raising."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", *args],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode == 0:
            return result.stdout.strip() or "unknown"
    except Exception:
        pass
    return "unknown"


def git_head(repo_root: Path) -> str:
    """Short git HEAD sha, or 'unknown' outside a git repo (never raises)."""
    return _git_rev(repo_root, "--short", "HEAD")


def git_head_full(repo_root: Path) -> str:
    """Full immutable git HEAD sha, or ``"unknown"`` outside a git repo."""
    return _git_rev(repo_root, "HEAD")


def git_tree_clean(repo_root: Path) -> bool:
    """Return true only when tracked, staged, and untracked source state is clean."""
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception:
        return False
    return result.returncode == 0 and not result.stdout


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _wrap(section_id: str, lines: list[str]) -> str:
    body = "\n".join(lines) if lines else "_(none found)_"
    return f"<!-- generated:begin:{section_id} -->\n{body}\n<!-- generated:end:{section_id} -->\n"


def render_packages_block(repo_root: Path) -> str:
    pkgs = scan_packages(repo_root)
    if pkgs is None:
        # Absent source is not the same as a measured empty inventory.
        return _wrap("packages", ["_(source unavailable)_"])
    lines = [f"- `omniagentos/{name}/`" for name in pkgs]
    return _wrap("packages", lines)


def render_migrations_block(repo_root: Path) -> str:
    migs = scan_migrations(repo_root)
    if migs is None:
        # Absent source is not the same as measured zero / version 0.
        return _wrap("migrations", ["_(source unavailable)_"])
    lines = [f"- `{fname}` (v{num})" for num, fname in migs]
    highest = migs[-1][0] if migs else 0
    lines.append(f"\n**{len(migs)} migrations, current version {highest}.**")
    return _wrap("migrations", lines)


def render_launchd_block(launchd_dir: Path) -> str:
    jobs = scan_launchd(launchd_dir)
    if jobs is None:
        # Absent source is not the same as a measured empty inventory.
        return _wrap("launchd", ["_(source unavailable)_"])
    lines = [f"- `{label}` — {schedule}" for label, schedule in jobs]
    return _wrap("launchd", lines)


def render_routes_block(repo_root: Path) -> str:
    routes = scan_routes(repo_root)
    if routes is None:
        # Absent source is not the same as measured zero routes.
        return _wrap("routes", ["_(source unavailable)_"])
    by_module: dict[str, list[tuple[str, str]]] = {}
    for method, path, module in routes:
        by_module.setdefault(module, []).append((method, path))
    lines: list[str] = []
    measured = 0
    unparseable_modules = 0
    for module in sorted(by_module):
        entries_raw = by_module[module]
        if any(m == "UNPARSEABLE" for m, _ in entries_raw):
            # Surface unparseable modules — never count the sentinel as a route.
            lines.append(f"- `{module}.py`: _(unparseable)_")
            unparseable_modules += 1
            continue
        entries = ", ".join(f"{m} {p}" if p else f'{m} ""' for m, p in entries_raw)
        lines.append(f"- `{module}.py`: {entries}")
        measured += len(entries_raw)
    if unparseable_modules and not measured:
        # Unparseable-only inventory is not a favourable measured count.
        lines.append("\n**route sources unparseable — inventory incomplete.**")
    elif unparseable_modules:
        # Mixed: report measured count only; incomplete inventory is named.
        lines.append(
            f"\n**{measured} routes across {len(by_module) - unparseable_modules} "
            f"modules; {unparseable_modules} module(s) unparseable — inventory incomplete.**"
        )
    else:
        lines.append(f"\n**{measured} routes across {len(by_module)} modules.**")
    return _wrap("routes", lines)


# Claims that must never be inferred from source-directory existence.  A
# certification result tied to the exact repository SHA is the only authority.
_STATUS_CLAIMS = (
    "Certification",
    "Decision Center",
    "TaskContract",
    "Toolplane",
    "fan-in",
    "isolation",
    "P12",
)
_STATUS_CLAIM_TERMS: dict[str, tuple[str, ...]] = {
    "Certification": ("certification suite",),
    "Decision Center": ("decision center",),
    "TaskContract": ("taskcontract",),
    "Toolplane": ("toolplane",),
    "fan-in": ("fan-in",),
    "isolation": ("isolation drill",),
    "P12": ("p12",),
}
_CLAIM_EVIDENCE_PATHS: dict[str, frozenset[str]] = {
    "Decision Center": frozenset({"tests/decision_center", "tests/decision"}),
    "TaskContract": frozenset({"tests/taskcontract"}),
    "Toolplane": frozenset({"tests/toolplane"}),
    "fan-in": frozenset({"tests/fanin"}),
    "isolation": frozenset({"tests/scope/test_isolation_drill.py"}),
    "P12": frozenset({"tests/p12"}),
}
_EVIDENCE_SCHEMA = "omniagentos.certification-evidence.v1"
_DEFAULT_EVIDENCE_PATH = Path("var/certification/evidence.json")

_PORT_DEFAULT_API = "8485"
_PORT_DEFAULT_DASH = "3003"
_PORT_RE = re.compile(
    r"""OMNIAGENTOS_(API|DASH)_PORT=["']?\$\{OMNIAGENTOS_(?:API|DASH)_PORT:-(\d+)\}"""
)


def _claim_passed(
    claim: str,
    value: Any,
    repo_root: Path,
    executed_paths: frozenset[str],
) -> bool:
    """Require a passed disposition plus named, non-empty behavioral evidence."""
    if not isinstance(value, dict) or value.get("status") != "passed":
        return False
    named_evidence = value.get("evidence")
    if not isinstance(named_evidence, list) or not named_evidence:
        return False
    allowed_paths = _CLAIM_EVIDENCE_PATHS.get(claim, frozenset())
    resolved_root = repo_root.resolve()
    for item in named_evidence:
        if not isinstance(item, str) or item not in allowed_paths or item not in executed_paths:
            return False
        path = (repo_root / item).resolve()
        if inode_relative_parts_anchored(path, resolved_root) is None:
            return False
        if path.is_file():
            if path.suffix != ".py" or not path.name.startswith("test_"):
                return False
        elif path.is_dir():
            if not any(candidate.is_file() for candidate in path.rglob("test_*.py")):
                return False
        else:
            return False
    return True


def scan_evidence(repo_root: Path, evidence_path: Path | None = None) -> dict[str, bool]:
    """Read certified claim results for the exact current SHA.

    Directory presence, test names, and stale result files are not evidence.
    The manifest must be produced by the certification command, record a clean
    tree and successful test exit, and match the full current git SHA.  Any
    malformed or missing field fails every claim closed.
    """
    result = dict.fromkeys(_STATUS_CLAIMS, False)
    expected_sha = git_head_full(repo_root)
    if expected_sha == "unknown" or not git_tree_clean(repo_root):
        return result

    path = evidence_path or repo_root / _DEFAULT_EVIDENCE_PATH
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return result
    if not isinstance(payload, dict):
        return result
    test_counts = payload.get("test_counts")
    executed_paths_value = payload.get("executed_paths")
    if not isinstance(executed_paths_value, list) or not all(
        isinstance(item, str) and item for item in executed_paths_value
    ):
        return result
    executed_paths = frozenset(executed_paths_value)
    if len(executed_paths) != len(executed_paths_value):
        return result
    expected_test_counts = payload.get("expected_test_counts")
    actual_test_counts = payload.get("actual_test_counts")
    if (
        not isinstance(expected_test_counts, dict)
        or not isinstance(actual_test_counts, dict)
        or set(expected_test_counts) != executed_paths
        or set(actual_test_counts) != executed_paths
        or not all(type(value) is int and value > 0 for value in expected_test_counts.values())
        or expected_test_counts != actual_test_counts
    ):
        return result
    tests_count = test_counts.get("tests") if isinstance(test_counts, dict) else None
    passed_count = test_counts.get("passed") if isinstance(test_counts, dict) else None
    skipped_count = test_counts.get("skipped") if isinstance(test_counts, dict) else None
    failed_count = test_counts.get("failed") if isinstance(test_counts, dict) else None
    if not all(
        type(value) is int for value in (tests_count, passed_count, skipped_count, failed_count)
    ):
        return result
    assert isinstance(tests_count, int)
    assert isinstance(passed_count, int)
    assert isinstance(skipped_count, int)
    assert isinstance(failed_count, int)
    if (
        payload.get("schema") != _EVIDENCE_SCHEMA
        or payload.get("repository_sha") != expected_sha
        or payload.get("clean_tree") is not True
        or payload.get("pytest_exit_code") != 0
        or payload.get("certified") is not True
        or payload.get("source") != "scripts/certify-omniagentos.sh"
        or payload.get("suite_complete") is not True
        or payload.get("inventory_errors") != []
        or tests_count <= 0
        or passed_count <= 0
        or skipped_count != 0
        or failed_count != 0
        or tests_count != passed_count + skipped_count + failed_count
        or tests_count != sum(expected_test_counts.values())
    ):
        return result

    claims = payload.get("claims")
    if not isinstance(claims, dict):
        return result
    result = {
        claim: _claim_passed(claim, claims.get(claim), repo_root, executed_paths)
        for claim in _CLAIM_EVIDENCE_PATHS
    }
    result["Certification"] = True
    return result


def launcher_default_ports(repo_root: Path) -> tuple[str, str] | None:
    """Read canonical default ports from the launcher script.

    Checks ``scripts/launch-supervised.sh`` (the supervised launcher, the
    canonical home of the ``OMNIAGENTOS_API_PORT`` / ``OMNIAGENTOS_DASH_PORT``
    default declarations) first, falling back to ``scripts/launch-omniagentos.sh``
    (the one-press dev launcher) so a repo — or a test fixture — that only
    carries the latter is still measured.

    Three-valued: ``None`` when neither launcher is present, both are
    unreadable, or every present launcher declares neither/only one of
    ``OMNIAGENTOS_API_PORT`` / ``OMNIAGENTOS_DASH_PORT``; a concrete
    ``(api, dash)`` pair only when both are measured from the same file.
    Callers must not treat ``None`` as the favourable baked-in defaults — that
    is the non-result-as-concrete-fact counterfeit (including empty/unparseable
    launcher content).
    """
    for launcher_name in ("launch-supervised.sh", "launch-omniagentos.sh"):
        launcher = repo_root / "scripts" / launcher_name
        if not launcher.exists():
            continue
        try:
            text = launcher.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        api_port: str | None = None
        dash_port: str | None = None
        for match in _PORT_RE.finditer(text):
            kind, value = match.group(1), match.group(2)
            if kind == "API":
                api_port = value
            else:
                dash_port = value
        # Present but unparseable must not invent _PORT_DEFAULT_* facts; try
        # the next candidate rather than falsely publishing a partial pair.
        if api_port is None or dash_port is None:
            continue
        return api_port, dash_port
    return None


def emit_archi_json(
    repo_root: Path,
    archi_path: Path | None = None,
    launchd_dir: Path | None = None,
    archi_content: str | None = None,
) -> dict:
    """Generate a strict JSON structure representing the codebase layout and metadata."""
    from omniagentos.archdocs.staleness import compute_stamp, is_stamp_stale, parse_stamp_comment

    repo_root = Path(repo_root)
    archi_path = archi_path if archi_path is not None else repo_root / "ARCHI.md"
    launchd_dir = (
        Path(launchd_dir) if launchd_dir is not None else Path.home() / "Library" / "LaunchAgents"
    )

    archi_source_present = True
    if archi_content is None:
        if archi_path.exists():
            archi_content = archi_path.read_text(encoding="utf-8")
        else:
            # Absent ARCHI is not a measured-empty subsystem list.
            archi_content = ""
            archi_source_present = False

    # Parse subsystems from ## Subsystems bullet list.
    # Three-valued publication:
    # - None when ARCHI is absent, OR present but has no parseable
    #   ``## Subsystems`` section, OR section present with bullets that
    #   fail ``**name**`` shape (malformed items ≠ measured-empty)
    # - [] when the section is present and measured empty (no bullets)
    # - non-empty list when every collected bullet parsed
    # Present-but-malformed must NOT collapse to the same favourable
    # measured-empty ``[]`` as a valid empty Subsystems section.
    subsystems: list[str] = []
    subsystems_match = re.search(
        r"## Subsystems(.*?)(?=^## )", archi_content, re.MULTILINE | re.DOTALL
    )
    subsystems_section_present = subsystems_match is not None
    if subsystems_match:
        subsystems_block = subsystems_match.group(1)
        current_bullet: list[str] = []
        for line in subsystems_block.splitlines():
            line_str = line.strip()
            if not line_str:
                continue
            if line_str.startswith(("- ", "* ")):
                if current_bullet:
                    subsystems.append(" ".join(current_bullet))
                current_bullet = [line_str]
            else:
                if current_bullet:
                    current_bullet.append(line_str)
        if current_bullet:
            subsystems.append(" ".join(current_bullet))

    parsed_subsystems: list[dict[str, str]] = []
    subsystems_items_unparseable = False
    for bullet in subsystems:
        bullet_content = re.sub(r"^[-*]\s+", "", bullet.strip())
        name_match = re.match(r"^\*\*(.*?)\*\*(.*)$", bullet_content)
        if name_match:
            name = name_match.group(1).strip()
            desc = name_match.group(2).strip()
            desc = re.sub(r"^[—\-\s:]+", "", desc).strip()
            parsed_subsystems.append({"name": name, "description": desc})
        else:
            # Bullet present but not ``**name**`` — unparseable inventory,
            # never skip into the same [] as a genuinely empty section.
            subsystems_items_unparseable = True

    # Parse env flags from launch script — absent launcher is null, not [].
    launcher = repo_root / "scripts" / "launch-omniagentos.sh"
    if not launcher.exists():
        env_flags: list[dict[str, str]] | None = None
    else:
        env_flags = []
        launcher_text = launcher.read_text(encoding="utf-8")
        env_pattern = re.compile(r"^export\s+(OMNIAGENTOS_[A-Z_]+)=(.*)$", re.MULTILINE)
        for match in env_pattern.finditer(launcher_text):
            name = match.group(1).strip()
            value = match.group(2).strip()
            if (value.startswith('"') and value.endswith('"')) or (
                value.startswith("'") and value.endswith("'")
            ):
                value = value[1:-1]
            env_flags.append({"name": name, "value": value})

    # Ports: None when launcher source is unmeasurable — never bake defaults
    # into JSON as if they were measured from the repo.
    port_pair = launcher_default_ports(repo_root)
    if port_pair is None:
        ports: dict[str, int | str] | None = None
    else:
        api_port, dash_port = port_pair
        ports = {
            "api": int(api_port) if api_port.isdigit() else api_port,
            "dashboard": int(dash_port) if dash_port.isdigit() else dash_port,
        }

    # Get/compute stamp. THE DEFECT (fixed here): recomputing only when the
    # embedded stamp is ABSENT reused a present-but-STALE stamp verbatim, so
    # the sanctioned `python -m omniagentos.archdocs.generate` regeneration
    # path carried a stale stamp forward into ARCHI.json unchanged. Recompute
    # whenever the parsed stamp is either absent OR stale (is_stamp_stale
    # reuses is_stale()'s exact comparison — including the legitimate
    # HEAD-parent owned-refresh-successor exception — so a genuinely fresh
    # stamp is never needlessly rewritten). In the normal `render_archi()` ->
    # `emit_archi_json()` flow this is a no-op: render_archi already splices
    # a fresh stamp into `archi_content` when stale, so this check finds it
    # already fresh and reuses it verbatim — one canonical stamp shared by
    # both oracles. This check exists independently for callers that build
    # `archi_content` without going through render_archi.
    stamp = parse_stamp_comment(archi_content)
    if stamp is None or is_stamp_stale(repo_root, stamp, archi_path):
        stamp = compute_stamp(repo_root)

    stamp_dict = {
        "git_head": stamp.git_head,
        "max_migration": stamp.max_migration,
        "route_count": stamp.route_count,
        "generated_at": stamp.generated_at,
    }

    packages = scan_packages(repo_root)
    routes = scan_routes(repo_root)
    migrations = scan_migrations(repo_root)
    jobs = scan_launchd(launchd_dir)
    return {
        "schema_version": 1,
        "stamp": stamp_dict,
        # Explicit is None — never bare-truthiness (None → favourable empty).
        # Absent source, missing section, OR present section with malformed
        # bullets → None; parseable section (possibly empty) → list.
        "subsystems": (
            None
            if (
                not archi_source_present
                or not subsystems_section_present
                or subsystems_items_unparseable
            )
            else parsed_subsystems
        ),
        "packages": None if packages is None else packages,
        "routes": None if routes is None else [list(r) for r in routes],
        "migrations": None if migrations is None else [list(m) for m in migrations],
        "launchd_jobs": None if jobs is None else [list(j) for j in jobs],
        "ports": ports,
        "env_flags": env_flags,
    }


def update_status_file(
    repo_root: Path,
    status_path: Path | None = None,
    evidence_path: Path | None = None,
) -> str:
    """Return STATUS.md content with ports/migrations/checklist exits derived from evidence.

    Pure helper: does not write. ``main(--update-status)`` is the only writer, and that
    flag stays off until L19 certifies the integrated SHA.
    """
    path = status_path if status_path is not None else repo_root / "STATUS.md"
    if not path.exists():
        return ""
    content = path.read_text(encoding="utf-8")
    evidence = scan_evidence(repo_root, evidence_path=evidence_path)

    out_lines: list[str] = []
    for line in content.splitlines():
        if "|" in line:
            lower = line.lower()
            required = [
                claim
                for claim, terms in _STATUS_CLAIM_TERMS.items()
                if any(term in lower for term in terms)
            ]
            if required:
                status = (
                    "**done**"
                    if all(evidence[claim] for claim in required)
                    else "**pending certified evidence**"
                )
                line = re.sub(
                    r"\*\*[^*]+\*\*",
                    status,
                    line,
                    count=1,
                    flags=re.IGNORECASE,
                )
        out_lines.append(line)

    out_str = "\n".join(out_lines)
    if not evidence["Certification"]:
        out_str = re.sub(
            r"^(# OmniAgentOS) — DONE\b",
            r"\1 — PENDING CERTIFIED EVIDENCE",
            out_str,
        )
    out_str = re.sub(
        r"^# \d+ passed\b.*$",
        "# See the SHA-bound certification evidence for the current result.",
        out_str,
        flags=re.MULTILINE,
    )
    out_str = re.sub(
        r"^Live API .* OK$",
        "Live API health is not claimed by offline certification; verify it at runtime.",
        out_str,
        flags=re.MULTILINE,
    )
    port_pair = launcher_default_ports(repo_root)
    if port_pair is not None:
        api_port, dash_port = port_pair
        out_str = re.sub(
            r"Launch on :\d+ / :\d+",
            f"Launch on :{api_port} / :{dash_port}",
            out_str,
        )

    migs = scan_migrations(repo_root)
    if migs is not None:
        highest = migs[-1][0] if migs else 0
        out_str = re.sub(
            r"Migration claim \d+[–-]\d+ Grok-exclusive",
            f"Migration claim 060–{highest:03d} Grok-exclusive",
            out_str,
        )
    else:
        # Absent migrations: never leave a favourable **done** on the migration
        # row, and never rewrite the ceiling to version 0.
        out_str = re.sub(
            r"(\| Migration claim \d+[–-]\d+ Grok-exclusive \| )\*\*[^*]+\*\*",
            r"\1**source unavailable**",
            out_str,
            count=1,
            flags=re.IGNORECASE,
        )
    return out_str + ("\n" if not out_str.endswith("\n") else "")


def update_archi_facts(repo_root: Path, content: str) -> str:
    """Refresh canonical port facts outside generated inventory blocks."""
    port_pair = launcher_default_ports(repo_root)
    if port_pair is None:
        # Unmeasurable launcher — leave existing port facts untouched.
        return content
    api_port, dash_port = port_pair
    content = re.sub(
        r"(bound `127\.0\.0\.1:)\d+(`\.)",
        rf"\g<1>{api_port}\g<2>",
        content,
    )
    content = re.sub(
        r"(Dashboard: Next\.js app under `dashboard/`, `npm run dev`/`npm run start`,\s+port )\d+(\.)",
        rf"\g<1>{dash_port}\g<2>",
        content,
    )
    content = re.sub(
        r"(- API: `127\.0\.0\.1:)\d+(` \(loopback only)",
        rf"\g<1>{api_port}\g<2>",
        content,
    )
    content = re.sub(
        r"(- Dashboard: `127\.0\.0\.1:)\d+(`, same-origin)",
        rf"\g<1>{dash_port}\g<2>",
        content,
    )
    return content


def generated_sections(repo_root: Path, launchd_dir: Path) -> dict[str, str]:
    """All four generated blocks keyed by section id (marker body only, no delimiters)."""
    return {
        "packages": render_packages_block(repo_root),
        "migrations": render_migrations_block(repo_root),
        "launchd": render_launchd_block(launchd_dir),
        "routes": render_routes_block(repo_root),
    }


# ---------------------------------------------------------------------------
# Regeneration: replace-in-place, preserving everything outside markers
# ---------------------------------------------------------------------------


def replace_generated_blocks(existing_content: str, new_blocks: dict[str, str]) -> str:
    """Replace each ``<!-- generated:begin:<id> -->`` block whose id is a key of
    ``new_blocks`` with the corresponding rendered block (which already includes its
    own delimiters). Blocks whose id is NOT in ``new_blocks``, and all narrative text
    (including ``## Notes (human)``), are left untouched byte-for-byte."""

    def _sub(match: re.Match) -> str:
        section_id = match.group("id")
        if section_id in new_blocks:
            return new_blocks[section_id]
        return match.group(0)

    return _ANY_MARKER_RE.sub(_sub, existing_content)


def _bootstrap_template(blocks: dict[str, str]) -> str:
    """Full ARCHI.md skeleton used only when the file doesn't exist yet."""
    return f"""# ARCHI.md — OmniAgentOS architecture map

Compact top map for agents and humans: what exists, where it lives, how to extend it.
Regenerate the machine-verifiable sections with `python -m omniagentos.archdocs.generate`;
narrative sections and the human-notes section below are preserved on regeneration.
Per-domain detail lives in `docs/architecture/*.md` (execution, governance, knowledge,
ui, scheduling, reliability, organization).

## Subsystems

- **Execution** — Fable-conducted task/run/step state machine (runner, orchestrator,
  routing/adapters). See `docs/architecture/execution.md`.
- **Governance** — ActionClass risk gate, approvals, policy config, budgets, ledger.
  See `docs/architecture/governance.md`.
- **Knowledge** — skills library, Synapse knowledge graph, memory layer, vaultgraph,
  repomap, filesearch. See `docs/architecture/knowledge.md`.
- **UI** — Next.js mission-control dashboard (SSE, approvals, sessions, board).
  See `docs/architecture/ui.md`.
- **Scheduling** — launchd job templates + installers, routines engine.
  See `docs/architecture/scheduling.md`.
- **Reliability** — V2 self-improving failure-detection/recovery/judge/pipeline system.
  See `docs/architecture/reliability.md`.
- **Organization** — V2 agent org hierarchy (CTO/VPs/managers/specialists/judges).
  See `docs/architecture/organization.md`.

## Entry points

- API: `omniagentos.api:app` (FastAPI, uvicorn), bound `127.0.0.1:8485`.
- Runner: `python -m omniagentos.runner` (or `scripts/launch-omniagentos.sh`), polling
  worker executing persisted step plans.
- Dashboard: Next.js app under `dashboard/`, `npm run dev`/`npm run start`, port 3003.
- CLI entry points: `python -m omniagentos.<package>` for repomap, filesearch,
  reliability (§11), skills curator, steward alerts/briefing/comms/metrics.

## Database — table groups (by migration)

{blocks["migrations"]}

## Scheduling — launchd jobs

{blocks["launchd"]}

## Repository layout — top-level packages

{blocks["packages"]}

## API routes

{blocks["routes"]}

## Ports & env flags

- API: `127.0.0.1:8485` (loopback only, no external bind).
- Dashboard: `127.0.0.1:3003`, same-origin token proxy (browser never holds the
  session token).
- Key flags: `OMNIAGENTOS_KNOWLEDGE` (Postgres+pgvector recall/ingest),
  `OMNIAGENTOS_MEMORY` (conversation history injection), `OMNIAGENTOS_CASCADE` /
  `OMNIAGENTOS_REFLEXION` (tier escalation + reflection), `OMNIAGENTOS_ACCOUNT_POOL`
  (multi-account Claude config-dir pooling), `OMNIAGENTOS_VAULT_AUTOCOMMIT` (vault
  note git auto-commit), `OMNIAGENTOS_CURATOR_LIVE_AGENT` (curator narrative pass).

## How to extend

- New DB tables → new numbered migration under `omniagentos/db/migrations/`, never
  edit `contracts/schema.sql` (Wave 0 frozen) or earlier migrations.
- New API route module → register in `omniagentos/api/main.py`
  (`app.include_router(...)`); respect the `{{"error": {{"code","message","detail"}}}}`
  envelope and the session-token gate.
- New SSE event type → add to `omniagentos.contracts.Events` (frozen; needs an
  explicit amendment) or, for additive V2 surfaces, document as a string type in a
  new `contracts/*.md` file (see `contracts/reliability-api.md`) — the frozen
  `Events`/`useEvents` contract stays untouched.
- New launchd job → follow the `install-steward.sh` idiom: render a plist template,
  install via a `scripts/<subsystem>/install-*.sh`, source `connections.env`, pin
  `.venv/bin/python`.
- Regenerate this file's inventories → `python -m omniagentos.archdocs.generate`;
  agent-driven narrative edits go through `omniagentos.archdocs.update.apply_update`
  (Tier S — only ever called by the pipeline applying an APPROVED docs improvement).

## Notes (human)

"""


def _archi_check_payload(content: str | None) -> str | None:
    """Normalize ARCHI.md text for ``--check`` equality (mirrors `_json_check_payload`).

    `generated_at` is wall-clock metadata, not a content signal — `is_stale()`
    already ignores it, and `render_archi()` now recomputes and splices a
    fresh stamp whenever the embedded one is stale (previously it was never
    touched, so this normalization was unnecessary; now that regeneration can
    legitimately change the stamp, raw-byte comparison would flip `--check`
    to exit 1 solely because a second ticked between the stored stamp's
    `generated_at` and the freshly recomputed one — the same false-drift
    class `_json_check_payload` already guards against for ARCHI.json).
    Returns ``None`` when ``content`` is ``None``.
    """
    if content is None:
        return None
    from omniagentos.archdocs.staleness import _STAMP_RE

    match = _STAMP_RE.search(content)
    if not match:
        return content
    normalized = (
        f"<!-- archdocs:stamp git_head={match.group('git_head')} "
        f"max_migration={match.group('max_migration')} "
        f"route_count={match.group('route_count')} generated_at=CONSTANT -->"
    )
    return content[: match.start()] + normalized + content[match.end() :]


def _json_check_payload(raw: str | None) -> str | None:
    """Normalize ARCHI.json text for ``--check`` equality.

    When ``stamp.generated_at`` is present, its value is replaced with a constant
    so a wall-clock tick alone cannot make a freshly written file look drifted.
    The *key* is retained: an absent ``generated_at`` must not normalize equal to
    a present one (absent cannot check as good). Returns None when ``raw`` is
    None. Malformed JSON is returned unchanged so a corrupt file still fails check.
    """
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    if not isinstance(data, dict):
        return raw
    stamp = data.get("stamp")
    if isinstance(stamp, dict):
        stamp_norm = dict(stamp)
        if "generated_at" in stamp_norm:
            stamp_norm["generated_at"] = ""
        data = {**data, "stamp": stamp_norm}
    return json.dumps(data, sort_keys=True, indent=2) + "\n"


def regenerate_archi(
    repo_root: str | Path,
    archi_path: str | Path | None = None,
    launchd_dir: str | Path | None = None,
) -> str:
    """Regenerate `ARCHI.md`'s inventory sections in place.

    - If `archi_path` doesn't exist, writes the full bootstrap template.
    - If it exists, replaces ONLY the four generated blocks; all narrative
      (including `## Notes (human)`) is preserved byte-for-byte.

    Returns the new file content (also written to disk). `launchd_dir` defaults to
    `~/Library/LaunchAgents` and is injectable so tests never touch the real dir.
    """
    repo_root = Path(repo_root)
    archi_path = Path(archi_path) if archi_path is not None else repo_root / "ARCHI.md"
    new_content = render_archi(repo_root, archi_path, launchd_dir)
    archi_path.write_text(new_content, encoding="utf-8")

    # Generate and write ARCHI.json
    json_path = archi_path.parent / "ARCHI.json"
    json_data = emit_archi_json(
        repo_root,
        archi_path=archi_path,
        launchd_dir=Path(launchd_dir) if launchd_dir is not None else None,
        archi_content=new_content,
    )
    json_str = json.dumps(json_data, sort_keys=True, indent=2) + "\n"
    json_path.write_text(json_str, encoding="utf-8")

    return new_content


def render_archi(
    repo_root: str | Path,
    archi_path: str | Path | None = None,
    launchd_dir: str | Path | None = None,
) -> str:
    """Return the regenerated ARCHI.md content without writing anything.

    The four generated inventory blocks are always refreshed. The embedded
    `<!-- archdocs:stamp ... -->` comment (narrative, outside the generated
    markers) is left byte-for-byte untouched when it is still fresh — but
    recomputed and spliced in when it is absent OR stale, so the sanctioned
    `python -m omniagentos.archdocs.generate` path (this function's only
    caller of consequence) cannot exit clean while carrying a stale stamp
    forward. Freshness reuses `staleness.is_stamp_stale()` — the same
    comparison `is_stale()` uses, including the legitimate HEAD-parent
    owned-refresh-successor exception `archi-morning` depends on — so a
    legitimately-fresh stamp is never needlessly rewritten.
    """
    from omniagentos.archdocs.staleness import (
        compute_stamp,
        is_stamp_stale,
        parse_stamp_comment,
        splice_stamp_comment,
    )

    repo_root = Path(repo_root)
    archi_path = Path(archi_path) if archi_path is not None else repo_root / "ARCHI.md"
    launchd_dir = (
        Path(launchd_dir) if launchd_dir is not None else Path.home() / "Library" / "LaunchAgents"
    )
    blocks = generated_sections(repo_root, launchd_dir)
    if archi_path.exists():
        existing_content = archi_path.read_text(encoding="utf-8")
        content = replace_generated_blocks(existing_content, blocks)
    else:
        existing_content = ""
        content = _bootstrap_template(blocks)
    content = update_archi_facts(repo_root, content)

    stored_stamp = parse_stamp_comment(existing_content)
    if is_stamp_stale(repo_root, stored_stamp, archi_path):
        content = splice_stamp_comment(content, compute_stamp(repo_root))
    return content


def main() -> None:
    parser = argparse.ArgumentParser(prog="omniagentos.archdocs.generate", description=__doc__)
    parser.add_argument("--repo-root", default=str(_repo_root_default()))
    parser.add_argument("--archi-path", default=None)
    parser.add_argument("--status-path", default=None)
    parser.add_argument("--evidence-path", default=None)
    parser.add_argument("--launchd-dir", default=None)
    parser.add_argument(
        "--update-status",
        action="store_true",
        help="Rewrite STATUS.md from evidence (opt-in; keep off until L19 certifies)",
    )
    parser.add_argument(
        "--check", action="store_true", help="exit 1 if regeneration would change the file"
    )
    args = parser.parse_args()

    repo_root = Path(args.repo_root)
    archi_path = Path(args.archi_path) if args.archi_path else repo_root / "ARCHI.md"
    launchd_dir = Path(args.launchd_dir) if args.launchd_dir else None

    before = archi_path.read_text(encoding="utf-8") if archi_path.exists() else None
    after = render_archi(repo_root, archi_path, launchd_dir=launchd_dir)

    json_path = archi_path.parent / "ARCHI.json"
    before_json_str = json_path.read_text(encoding="utf-8") if json_path.exists() else None
    after_json_data = emit_archi_json(
        repo_root, archi_path=archi_path, launchd_dir=launchd_dir, archi_content=after
    )
    after_json_str = json.dumps(after_json_data, sort_keys=True, indent=2) + "\n"

    status_path = Path(args.status_path) if args.status_path else repo_root / "STATUS.md"
    evidence_path = Path(args.evidence_path) if args.evidence_path else None
    status_before = status_path.read_text(encoding="utf-8") if status_path.exists() else None
    status_after = status_before
    if args.update_status:
        status_after = update_status_file(
            repo_root,
            status_path=status_path,
            evidence_path=evidence_path,
        )

    if args.check:
        # generated_at is wall-clock metadata, not a content signal — is_stale
        # already ignores it, and render_archi() now legitimately recomputes
        # the stamp when stale. Comparing raw bytes would flip --check to
        # exit 1 solely because a second ticked between the stored stamp and
        # the freshly recomputed one (false drift, mirrors _json_check_payload).
        archi_changed = _archi_check_payload(before) != _archi_check_payload(after)
        status_changed = args.update_status and status_before != status_after
        # generated_at is wall-clock metadata, not a content signal — is_stale
        # already ignores it. Comparing full JSON would flip --check to exit 1
        # solely because a second ticked between regenerate and re-render
        # (false drift: unmeasurable clock noise presented as "would change").
        json_changed = _json_check_payload(before_json_str) != _json_check_payload(after_json_str)
        raise SystemExit(1 if archi_changed or status_changed or json_changed else 0)

    archi_path.write_text(after, encoding="utf-8")
    json_path.write_text(after_json_str, encoding="utf-8")
    if args.update_status and status_after:
        status_path.write_text(status_after, encoding="utf-8")


if __name__ == "__main__":
    main()
