#!/usr/bin/env python3
"""Standing wiring/drift audit — the health-sentinel's second CONSUMER.

WHY THIS EXISTS
---------------
Eleven of the twelve acceptance tests in this program run exactly ONCE, at
landing. After that nothing re-asserts them, and the program's own thesis is
that config drift and "built, tested, never wired" reaccumulate silently. This
arm is the thing that stops every other item's savings from decaying: each
standing assertion becomes a REGISTERED ROW in ``configs/audit-checks.yaml``
carrying its threshold AND that threshold's provenance (``derived`` from real
data, or an honest ``default-guess``).

CONTRACT
--------
* READ-ONLY. The audit is a pure function of (repo tree, config files, ledger).
  It mutates nothing — no snapshot file, no state file, no DB write. Findings go
  to stdout, and to an append-only log only when ``--audit-log`` is passed
  explicitly.
* A check that CANNOT RUN reports ``fail``, never ``skip``. Silence may never be
  mistaken for a pass. A missing template, an unreadable DB, an absent config —
  all of them are ``fail`` WITH the reason, because the alternative is a green
  audit that is green because it did nothing.
* Every check reports. ``--json`` always emits one row per registered check, so
  a silently-vanished check is detectable (``tests/acceptance/s00_audit.sh``
  step 0 asserts exactly this).

THE TEN CHECKS
---------------
 1 ``config_digest``        three-account effective permissions vs the tracked template
 2 ``mcp_roster``           the LOADED roster (.mcp.json) subset-of configs/mcp-approved.yaml
 3 ``never_wired``          newly added scripts/modules with no call site
 4 ``pump_ledger_wiring``   the four dispatch pumps actually write swarm_attempts
 5 ``soak_window_diff``     declared observe-mode flags vs the running reality
 6 ``lane_brief``           every lane clone has a brief, BY DECLARED CLASS
 7 ``single_signer``        promotions in a window signed by more than one key
 8 ``unscheduled_heartbeat`` correct scripts that are not a launchd label at all
 9 ``loopback_connectors``  declared local base_urls that do not accept a TCP connect
10 ``provider_daily_spend`` provider_call_usage directly exceeds 80% / 100% of cap

CHECK 8 IS THE ONE ``health_sentinel.check_launchd`` STRUCTURALLY CANNOT SEE.
``check_launchd`` compares RENDERED -> INSTALLED -> LOADED plists. A correct,
working script that is not a launchd label at all is INVISIBLE to it — there is
no plist to find missing. ``configs/expected-heartbeats.yaml`` maps script ->
(artifact, max age) so the absence of a schedule shows up as a stale artifact.

CHECK 9 NOTIFIES AND NEVER RESTARTS. The ``https://127.0.0.1:8443`` LiteLLM ->
OpenRouter proxy spends real money under a $50/day cap enforced by a SEPARATE
loaded watchdog (``com.youruser.litellm-spendguard``), and its log ends in a
clean shutdown. An auto-restarter here would race a guard whose entire job is to
stop that process. Read-only TCP connect, report, stop.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import socket
import sqlite3
import subprocess
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from omniagentos.adapters.spend_db import SpendDbResolutionError, resolve_spend_db_path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REGISTRY = REPO_ROOT / "configs" / "audit-checks.yaml"

OK = "ok"
WARN = "warn"
FAIL = "fail"
_RANK = {OK: 0, WARN: 1, FAIL: 2}

SUBPROCESS_TIMEOUT = 60.0

# Must equal MIN_JUSTIFICATION in scripts/gates/mech_gate.sh. configs/audit-checks.yaml
# names that gate as this check's derivation, so the two disagreeing about the same
# rule is itself a defect -- and did occur: the profile branch here checked only for a
# non-empty justification while the gate enforced the floor.
_MIN_JUSTIFICATION = 20


# --------------------------------------------------------------------------- model


@dataclass
class AuditContext:
    """Everything a check is allowed to look at, so checks stay testable.

    ``repo_root`` and ``accounts_root`` are parameters rather than constants
    precisely so the acceptance suite can plant a defect in a throwaway tree
    instead of damaging the real one.
    """

    repo_root: Path
    accounts_root: Path
    registry: dict[str, Any]
    now: datetime

    def check_cfg(self, check_id: str) -> dict[str, Any]:
        checks = self.registry.get("checks") or {}
        node = checks.get(check_id) if isinstance(checks, dict) else None
        return dict(node) if isinstance(node, dict) else {}


@dataclass
class AuditResult:
    check_id: str
    status: str
    evidence: str
    threshold: Any = None
    provenance: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.check_id,
            "status": self.status,
            "evidence": self.evidence,
            "threshold": self.threshold,
            "provenance": self.provenance,
            "detail": self.detail,
        }


def _num(cfg: dict[str, Any], key: str, default: float) -> float:
    """Read a numeric config value where ZERO is a legitimate setting.

    ``cfg.get(key) or default`` is the bug this exists to prevent: a registry
    that declares ``min_age_minutes: 0`` would silently get 60 back, and the
    check would report ok on a tree full of brief-less clones.
    """
    raw = cfg.get(key)
    if raw is None:
        return float(default)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return float(default)


def _worst(statuses: list[str]) -> str:
    return max(statuses, key=lambda s: _RANK.get(s, 0)) if statuses else OK


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_yaml(path: Path) -> Any:
    import yaml  # noqa: PLC0415

    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _git(ctx: AuditContext, *args: str) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(ctx.repo_root), *args],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=SUBPROCESS_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, f"{type(exc).__name__}: {exc}"
    return proc.returncode, proc.stdout


# --------------------------------------------------------------------------- 1 config digest


_PERMISSION_BUCKETS = ("allow", "deny", "ask")


def _effective_permissions(account_dir: Path) -> tuple[list[str], dict[str, Any], list[str]]:
    """Merge ``settings.json`` then ``settings.local.json``, deduped + sorted.

    Returns ``(rules, managed_scalars, notes)``. The 168-entry allowlist on this
    box lives in ``settings.local.json``, NOT ``settings.json`` — a digest taken
    over settings.json alone reads 8 entries and calls it parity.
    """
    rules: set[str] = set()
    scalars: dict[str, Any] = {}
    notes: list[str] = []
    for name in ("settings.json", "settings.local.json"):
        path = account_dir / name
        if not path.is_file():
            notes.append(f"{name}: absent")
            continue
        try:
            data = _read_json(path)
        except (OSError, ValueError) as exc:
            notes.append(f"{name}: unreadable ({type(exc).__name__})")
            continue
        if not isinstance(data, dict):
            notes.append(f"{name}: not an object")
            continue
        perms = data.get("permissions")
        if isinstance(perms, dict):
            for bucket in _PERMISSION_BUCKETS:
                entries = perms.get(bucket)
                if isinstance(entries, list):
                    rules.update(f"{bucket}:{entry}" for entry in entries if isinstance(entry, str))
            if isinstance(perms.get("defaultMode"), str):
                scalars["defaultMode"] = perms["defaultMode"]
        if "agentPushNotifEnabled" in data:
            scalars["agentPushNotifEnabled"] = data["agentPushNotifEnabled"]
        if "statusLine" in data:
            scalars["statusLine"] = data["statusLine"]
        if isinstance(data.get("defaultMode"), str):
            scalars["defaultMode"] = data["defaultMode"]
    return sorted(rules), scalars, notes


def check_config_digest(ctx: AuditContext) -> AuditResult:
    cfg = ctx.check_cfg("config_digest")
    accounts = cfg.get("accounts") or [".claude", ".claude-account-2", ".claude-account-3"]
    template_rel = cfg.get("template") or "configs/canonical-claude-settings.json"
    template = ctx.repo_root / template_rel
    provenance = cfg.get("provenance")
    threshold = cfg.get("threshold", "sha256-equality")

    observed: dict[str, Any] = {}
    all_rules: set[str] = set()
    for name in accounts:
        account_dir = ctx.accounts_root / name
        if not account_dir.is_dir():
            observed[name] = {"present": False}
            continue
        rules, scalars, notes = _effective_permissions(account_dir)
        all_rules.update(rules)
        observed[name] = {
            "present": True,
            "rule_count": len(rules),
            "sha256": _sha256("\n".join(rules)),
            "scalars": scalars,
            "notes": notes,
        }
    merged = sorted(all_rules)
    merged_sha = _sha256("\n".join(merged))
    detail: dict[str, Any] = {
        "accounts": observed,
        "merged_permissions_sha256": merged_sha,
        "merged_rule_count": len(merged),
        "template": str(template),
    }

    if not template.is_file():
        # Another package owns this template. Its absence is a FAIL WITH A
        # REASON, never a crash and never a skip.
        return AuditResult(
            "config_digest",
            FAIL,
            f"template-missing: {template_rel} is not present; "
            f"observed merged permissions sha256={merged_sha[:16]} over {len(merged)} rules",
            threshold,
            provenance,
            detail,
        )
    try:
        tpl = _read_json(template)
    except (OSError, ValueError) as exc:
        return AuditResult(
            "config_digest",
            FAIL,
            f"template-unreadable: {template_rel}: {type(exc).__name__}: {exc}",
            threshold,
            provenance,
            detail,
        )
    expected = str(
        ((tpl.get("_meta") or {}).get("merged_permissions_sha256"))
        or tpl.get("merged_permissions_sha256")
        or ""
    )
    detail["template_sha256"] = expected
    if not expected:
        return AuditResult(
            "config_digest",
            FAIL,
            f"template-has-no-digest: {template_rel} declares no merged_permissions_sha256",
            threshold,
            provenance,
            detail,
        )
    if expected != merged_sha:
        return AuditResult(
            "config_digest",
            FAIL,
            f"config drift: merged permissions sha256={merged_sha[:16]} != template {expected[:16]} "
            f"({len(merged)} effective rules across {len(accounts)} accounts)",
            threshold,
            provenance,
            detail,
        )
    return AuditResult(
        "config_digest",
        OK,
        f"{len(merged)} effective rules across {len(accounts)} accounts match the tracked template",
        threshold,
        provenance,
        detail,
    )


# --------------------------------------------------------------------------- 2 roster


_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def check_mcp_roster(ctx: AuditContext) -> AuditResult:
    """Audit the roster the runtime loads, and assert that it IS the reviewed one.

    Kept deliberately in step with ``scripts/gates/mech_gate.sh --check-mcp-roster``;
    configs/audit-checks.yaml names that gate as this check's derivation, so the
    two disagreeing is itself a defect. Both used to default to the mirror
    ``tools/mcp-servers.json`` on the premise that ``.mcp.json`` was a tracked
    symlink to it -- a premise 00000000 broke on 2026-08-02. This check went on
    reporting ``[OK] 2 roster server(s) all approved`` about a tree whose loaded
    roster held 11.
    """
    cfg = ctx.check_cfg("mcp_roster")
    mirror_path = ctx.repo_root / (cfg.get("mirror") or "tools/mcp-servers.json")
    configured_roster = cfg.get("roster")
    if configured_roster:
        roster_path = ctx.repo_root / configured_roster
    else:
        # Default resolution mirrors the shell gate: prefer the loaded file, but
        # a tree carrying only the mirror still has a roster worth auditing.
        loaded = ctx.repo_root / ".mcp.json"
        roster_path = loaded if loaded.is_file() or not mirror_path.is_file() else mirror_path
    approved_path = ctx.repo_root / (cfg.get("approved") or "configs/mcp-approved.yaml")
    provenance = cfg.get("provenance")
    threshold = cfg.get("threshold", "roster subset-of approved; no empty ${VAR}")
    detail: dict[str, Any] = {"roster": str(roster_path), "approved": str(approved_path)}

    if not roster_path.is_file():
        return AuditResult(
            "mcp_roster", FAIL, f"roster-missing: {roster_path}", threshold, provenance, detail
        )
    if not approved_path.is_file():
        return AuditResult(
            "mcp_roster",
            FAIL,
            f"approved-list-missing: {approved_path}",
            threshold,
            provenance,
            detail,
        )
    try:
        roster = _read_json(roster_path)
        servers = roster.get("mcpServers") if isinstance(roster, dict) else None
        servers = servers if isinstance(servers, dict) else {}
    except (OSError, ValueError) as exc:
        return AuditResult(
            "mcp_roster",
            FAIL,
            f"roster-unreadable: {type(exc).__name__}: {exc}",
            threshold,
            provenance,
            detail,
        )

    # Premise assertion: auditing a roster only means something if it is the
    # roster the runtime loads. When a second roster file exists, the two must
    # agree by parsed content -- formatting is not a finding. Without this, a
    # reviewed file passes while a divergent one is what ships, which is exactly
    # how the 2-vs-11 divergence stayed green for five days.
    if mirror_path.is_file() and mirror_path.resolve() != roster_path.resolve():
        try:
            mirror_doc = _read_json(mirror_path)
            mirror_servers = mirror_doc.get("mcpServers") if isinstance(mirror_doc, dict) else None
            mirror_servers = mirror_servers if isinstance(mirror_servers, dict) else {}
        except (OSError, ValueError) as exc:
            return AuditResult(
                "mcp_roster",
                FAIL,
                f"mirror-unreadable: {mirror_path}: {type(exc).__name__}: {exc}",
                threshold,
                provenance,
                detail,
            )
        if mirror_servers != servers:
            only_loaded = sorted(set(servers) - set(mirror_servers))
            only_mirror = sorted(set(mirror_servers) - set(servers))
            parts = []
            if only_loaded:
                parts.append(f"only in {roster_path.name}: {', '.join(only_loaded)}")
            if only_mirror:
                parts.append(f"only in {mirror_path.name}: {', '.join(only_mirror)}")
            if not parts:
                parts.append("same server names, differing definitions")
            detail.update(
                {
                    "mirror": str(mirror_path),
                    "only_in_roster": only_loaded,
                    "only_in_mirror": only_mirror,
                }
            )
            return AuditResult(
                "mcp_roster",
                FAIL,
                f"roster-divergence: {roster_path.name} and {mirror_path.name} disagree, so the "
                f"reviewed roster is not necessarily the loaded one ({'; '.join(parts)})",
                threshold,
                provenance,
                detail,
            )
    try:
        approved_doc = _read_yaml(approved_path) or {}
        approved = (approved_doc.get("approved") or {}) if isinstance(approved_doc, dict) else {}
    except Exception as exc:  # noqa: BLE001
        return AuditResult(
            "mcp_roster",
            FAIL,
            f"approved-list-unreadable: {type(exc).__name__}: {exc}",
            threshold,
            provenance,
            detail,
        )

    unapproved = sorted(set(servers) - set(approved))
    no_justification = sorted(
        name
        for name in servers
        if name in approved
        and not str((approved.get(name) or {}).get("justification") or "").strip()
    )
    empty_vars: list[str] = []
    for name, spec in servers.items():
        blob = json.dumps(spec)
        for var in _VAR_RE.findall(blob):
            if not os.environ.get(var):
                empty_vars.append(f"{name}:${{{var}}}")

    # Opt-in profiles (configs/toolbroker/mcp-profiles/*.json).
    #
    # Kept in step with scripts/gates/mech_gate.sh --check-mcp-roster, which grew
    # the same loop in the same commit. Without it, the nine servers trimmed from
    # the default roster on 2026-08-13 would live in profile files that NO control
    # reads -- this check inspects three fixed paths -- so the re-accretion it
    # exists to detect would recur one directory over while it reported OK. That
    # is the identical failure mode as the 2-vs-11 divergence above: a control
    # asserting something true about a file that is not the one in play.
    profile_approved = (
        (approved_doc.get("profile_approved") or {}) if isinstance(approved_doc, dict) else {}
    )
    profile_allowed = {**approved, **profile_approved}
    profile_dir = approved_path.parent / "toolbroker" / "mcp-profiles"
    profile_problems: list[str] = []
    profiles_checked = 0
    if profile_dir.is_dir():
        for ppath in sorted(profile_dir.glob("*.json")):
            try:
                pdoc = _read_json(ppath)
                pservers = pdoc.get("mcpServers") if isinstance(pdoc, dict) else None
                pservers = pservers if isinstance(pservers, dict) else {}
            except (OSError, ValueError) as exc:
                profile_problems.append(f"{ppath.name}: unreadable ({type(exc).__name__})")
                continue
            profiles_checked += 1
            if not pservers:
                profile_problems.append(f"{ppath.name}: declares no servers")
            for name, spec in sorted(pservers.items()):
                if name not in profile_allowed:
                    profile_problems.append(f"{ppath.name}: {name} unapproved")
                else:
                    # The >=20-char floor must match mech_gate.sh's
                    # MIN_JUSTIFICATION exactly. This checked only for a
                    # NON-EMPTY justification, so a 9-character one passed here
                    # while the gate refused it -- the two controls disagreeing
                    # about the same rule, which is the very defect class this
                    # pair exists to prevent, re-introduced in the commit that
                    # claimed to close it.
                    just = str((profile_allowed.get(name) or {}).get("justification") or "").strip()
                    if not just:
                        profile_problems.append(f"{ppath.name}: {name} has no justification")
                    elif len(just) < _MIN_JUSTIFICATION:
                        profile_problems.append(
                            f"{ppath.name}: {name} justification is trivial "
                            f"({len(just)} < {_MIN_JUSTIFICATION} chars)"
                        )
                for var in _VAR_RE.findall(json.dumps(spec)):
                    if not os.environ.get(var):
                        profile_problems.append(f"{ppath.name}: {name}:${{{var}}} empty")

    # Vacuity guard, kept in step with mech_gate.sh. Once the default roster is
    # legitimately empty, "roster subset-of approved" is satisfied by a tree in
    # which NO MCP server is reachable at all: delete the profile directory and
    # this check reports ok while the whole capability surface has vanished.
    # Measured before the guard: gate=0, audit=ok with the directory removed.
    # Conditioned on an empty roster so older trees, which carry servers in
    # .mcp.json and no profiles, are unaffected.
    if not servers and profiles_checked == 0:
        profile_problems.append(
            "default roster is empty AND no profiles exist, so no MCP server is reachable at all"
        )
    detail.update(
        {
            "roster_servers": sorted(servers),
            "approved_servers": sorted(approved),
            "unapproved": unapproved,
            "missing_justification": no_justification,
            "empty_vars": sorted(empty_vars),
            "profiles_checked": profiles_checked,
            "profile_problems": profile_problems,
        }
    )
    problems = []
    if unapproved:
        problems.append(
            f"re-accretion: {len(unapproved)} unapproved server(s): {', '.join(unapproved)}"
        )
    if no_justification:
        problems.append(f"approved with empty justification: {', '.join(no_justification)}")
    if empty_vars:
        problems.append(f"placeholder resolves empty: {', '.join(sorted(empty_vars))}")
    if profile_problems:
        problems.append(f"profile re-accretion: {'; '.join(profile_problems)}")
    if problems:
        return AuditResult("mcp_roster", FAIL, "; ".join(problems), threshold, provenance, detail)
    return AuditResult(
        "mcp_roster",
        OK,
        f"{len(servers)} roster server(s) all approved with justifications; no empty ${{VAR}}; "
        f"{profiles_checked} profile(s) checked",
        threshold,
        provenance,
        detail,
    )


# --------------------------------------------------------------------------- 3 never wired


_WORD_RE = re.compile(r"[A-Za-z0-9_.\-]+")


def _added_recently(
    ctx: AuditContext, window_days: int, patterns: list[str]
) -> tuple[list[str], str | None]:
    code, out = _git(
        ctx,
        "log",
        "--diff-filter=A",
        f"--since={window_days} days ago",
        "--name-only",
        "--pretty=format:",
    )
    if code != 0:
        return [], f"git log failed (rc={code}): {out.strip()[:200]}"
    seen: set[str] = set()
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        if any(fnmatch.fnmatch(line, pat) for pat in patterns):
            seen.add(line)
    return sorted(seen), None


def check_never_wired(ctx: AuditContext) -> AuditResult:
    cfg = ctx.check_cfg("never_wired")
    window_days = int(_num(cfg, "window_days", 30))
    provenance = cfg.get("provenance") or "default-guess-30d"
    threshold = cfg.get(
        "threshold", f"added <= {window_days}d must have >=1 call site outside its own tests"
    )
    patterns = cfg.get("patterns") or ["scripts/*.sh", "omniagentos/*.py", "omniagentos/**/*.py"]
    ignore = cfg.get("ignore") or []
    detail: dict[str, Any] = {
        "window_days": window_days,
        "window_days_provenance": provenance,
        "patterns": patterns,
    }

    candidates, err = _added_recently(ctx, window_days, patterns)
    if err:
        return AuditResult("never_wired", FAIL, f"cannot-run: {err}", threshold, provenance, detail)
    candidates = [c for c in candidates if not any(fnmatch.fnmatch(c, ig) for ig in ignore)]
    # Only files that still exist can be unwired; deleted paths are not drift.
    candidates = [c for c in candidates if (ctx.repo_root / c).is_file()]
    detail["candidate_count"] = len(candidates)
    if not candidates:
        return AuditResult(
            "never_wired",
            OK,
            f"no scripts/modules added in the last {window_days}d (provenance: {provenance})",
            threshold,
            provenance,
            detail,
        )

    tokens: dict[str, set[str]] = {}
    for rel in candidates:
        keys = {Path(rel).name}
        if rel.endswith(".py"):
            dotted = rel[:-3].replace("/", ".")
            keys.add(dotted)
            if dotted.endswith(".__init__"):
                keys.add(dotted[: -len(".__init__")])
            keys.add(Path(rel).stem)
        tokens[rel] = keys

    lookup: dict[str, set[str]] = {}
    for rel, keys in tokens.items():
        for key in keys:
            lookup.setdefault(key, set()).add(rel)

    code, listing = _git(ctx, "ls-files", "-z")
    if code != 0:
        return AuditResult(
            "never_wired",
            FAIL,
            f"cannot-run: git ls-files failed (rc={code})",
            threshold,
            provenance,
            detail,
        )
    referenced: dict[str, set[str]] = {rel: set() for rel in candidates}
    universe = set(lookup)
    for rel_file in listing.split("\0"):
        if not rel_file:
            continue
        path = ctx.repo_root / rel_file
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if b"\x00" in data[:2048]:
            continue
        text = data.decode("utf-8", "replace")
        for key in universe & set(_WORD_RE.findall(text)):
            for owner in lookup[key]:
                if rel_file == owner:
                    continue  # self-reference is not a call site
                if _is_own_test(rel_file, owner):
                    continue  # its own tests are not a call site either
                referenced[owner].add(rel_file)

    unwired = sorted(rel for rel, sites in referenced.items() if not sites)
    detail["unwired"] = unwired
    detail["referenced_sample"] = {
        rel: sorted(sites)[:3] for rel, sites in list(referenced.items())[:5] if sites
    }
    if unwired:
        head = ", ".join(unwired[:5])
        more = f" (+{len(unwired) - 5} more)" if len(unwired) > 5 else ""
        return AuditResult(
            "never_wired",
            FAIL,
            f"{len(unwired)}/{len(candidates)} file(s) added in the last {window_days}d have no call "
            f"site outside their own tests: {head}{more} "
            f"[window provenance: {provenance}]",
            threshold,
            provenance,
            detail,
        )
    return AuditResult(
        "never_wired",
        OK,
        f"all {len(candidates)} file(s) added in the last {window_days}d are referenced "
        f"[window provenance: {provenance}]",
        threshold,
        provenance,
        detail,
    )


def _is_own_test(rel_file: str, owner: str) -> bool:
    """True when *rel_file* is a test that exists only to exercise *owner*."""
    if not (
        rel_file.startswith("tests/")
        or "/tests/" in rel_file
        or Path(rel_file).name.startswith("test_")
    ):
        return False
    stem = Path(owner).stem
    return bool(stem) and stem in Path(rel_file).name


# --------------------------------------------------------------------------- 4 pump ledger


def _resolve_db(ctx: AuditContext, cfg: dict[str, Any]) -> Path:
    """The registry's repo-relative DB wins over ``OMNIAGENTOS_DB``.

    Precedence matters for testability: with ``--audit-repo-root`` pointing at a
    sandbox tree, an ambient ``OMNIAGENTOS_DB`` from ``launch-env.sh`` would
    silently drag the check back onto the REAL control plane and the sandbox
    would prove nothing.
    """
    db_rel = cfg.get("db") or "var/runtime/state.sqlite3"
    local = ctx.repo_root / db_rel
    if local.is_file():
        return local
    env = os.environ.get("OMNIAGENTOS_DB")
    return Path(env) if env else local


def _open_ro(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def check_pump_ledger_wiring(ctx: AuditContext) -> AuditResult:
    cfg = ctx.check_cfg("pump_ledger_wiring")
    provenance = cfg.get("provenance") or "insufficient-data-report-only"
    threshold = cfg.get("threshold")
    pumps = cfg.get("pumps") or ["rework", "review", "verdict", "sim"]
    window_hours = int(_num(cfg, "window_hours", 24))
    report_only = bool(cfg.get("report_only", True))
    identical_verdict_run = int(_num(cfg, "identical_verdict_hash_run", 5))
    run_id = cfg.get("pump_ledger_run_id") or "swr_pumpledger"
    db_path = _resolve_db(ctx, cfg)
    detail: dict[str, Any] = {
        "db": str(db_path),
        "pumps": pumps,
        "window_hours": window_hours,
        "report_only": report_only,
        "dispatch_rate_threshold": threshold,
    }

    if not db_path.is_file():
        return AuditResult(
            "pump_ledger_wiring",
            FAIL,
            f"cannot-run: control-plane DB absent at {db_path}",
            threshold,
            provenance,
            detail,
        )
    try:
        conn = _open_ro(db_path)
    except sqlite3.Error as exc:
        return AuditResult(
            "pump_ledger_wiring",
            FAIL,
            f"cannot-run: {type(exc).__name__}: {exc}",
            threshold,
            provenance,
            detail,
        )
    try:
        tables = {r[0] for r in conn.execute("select name from sqlite_master where type='table'")}
        if "swarm_attempts" not in tables:
            return AuditResult(
                "pump_ledger_wiring",
                FAIL,
                "cannot-run: swarm_attempts table absent",
                threshold,
                provenance,
                detail,
            )
        cutoff = (ctx.now - timedelta(hours=window_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows = conn.execute(
            "select board_task_id, started_at, detail from swarm_attempts "
            "where swarm_run_id = ? and started_at >= ?",
            (run_id, cutoff),
        ).fetchall()
        total_recent = conn.execute(
            "select count(*) from swarm_attempts where started_at >= ?", (cutoff,)
        ).fetchone()[0]
    except sqlite3.Error as exc:
        return AuditResult(
            "pump_ledger_wiring",
            FAIL,
            f"cannot-run: {type(exc).__name__}: {exc}",
            threshold,
            provenance,
            detail,
        )
    finally:
        conn.close()

    per_pump = {pump: 0 for pump in pumps}
    hashes: list[str] = []
    for row in rows:
        task_id = str(row["board_task_id"] or "")
        for pump in pumps:
            if task_id.startswith(f"btk_pump_{pump}"):
                per_pump[pump] += 1
        try:
            blob = json.loads(row["detail"] or "{}")
        except (TypeError, ValueError):
            blob = {}
        vh = blob.get("verdict_hash") if isinstance(blob, dict) else None
        if vh:
            hashes.append(str(vh))
    run_len, best_run = 0, 0
    previous = None
    for value in hashes:
        run_len = run_len + 1 if value == previous else 1
        best_run = max(best_run, run_len)
        previous = value

    missing = sorted(p for p, n in per_pump.items() if n == 0)
    detail.update(
        {
            "attempts_in_window": len(rows),
            "all_attempts_in_window": total_recent,
            "per_pump": per_pump,
            "longest_identical_verdict_hash_run": best_run,
            "identical_verdict_hash_run_threshold": identical_verdict_run,
        }
    )

    if report_only or threshold in (None, "", "unmeasured"):
        return AuditResult(
            "pump_ledger_wiring",
            WARN,
            f"REPORT-ONLY (provenance: {provenance}): {len(rows)} pump-ledger attempt(s) in {window_hours}h; "
            f"silent pumps={missing or 'none'}; longest identical verdict_hash run={best_run}. "
            "A week of per-lane hourly dispatch data does not exist yet, so no runaway threshold is enforced.",
            threshold,
            provenance,
            detail,
        )
    problems = []
    if missing:
        problems.append(
            f"pumps with no swarm_attempts row in {window_hours}h: {', '.join(missing)}"
        )
    per_hour = len(rows) / max(1, window_hours)
    if isinstance(threshold, (int, float)) and per_hour > float(threshold):
        problems.append(f"runaway: {per_hour:.1f} dispatches/hour > threshold {threshold}")
    if best_run >= identical_verdict_run:
        problems.append(
            f"runaway: {best_run} identical verdict_hash in a row (K={identical_verdict_run})"
        )
    if problems:
        return AuditResult(
            "pump_ledger_wiring", FAIL, "; ".join(problems), threshold, provenance, detail
        )
    return AuditResult(
        "pump_ledger_wiring",
        OK,
        f"all {len(pumps)} pumps attributable in {window_hours}h; {per_hour:.1f} dispatches/hour",
        threshold,
        provenance,
        detail,
    )


# --------------------------------------------------------------------------- 5 soak windows


def check_soak_window_diff(ctx: AuditContext) -> AuditResult:
    cfg = ctx.check_cfg("soak_window_diff")
    provenance = cfg.get("provenance")
    threshold = cfg.get("threshold", "declared mode == observed mode; window not past closes_at")
    windows = cfg.get("windows") or []
    detail: dict[str, Any] = {"windows": []}
    if not windows:
        return AuditResult(
            "soak_window_diff",
            FAIL,
            "cannot-run: no observe/report windows declared in the registry "
            "(an empty window list is indistinguishable from 'nothing is soaking')",
            threshold,
            provenance,
            detail,
        )
    drift: list[str] = []
    for window in windows:
        name = str(window.get("name") or window.get("env") or "?")
        env_var = window.get("env")
        declared = str(window.get("declared_mode") or "")
        observed = os.environ.get(env_var) if env_var else None
        effective = (
            observed
            if observed not in (None, "")
            else str(window.get("default_when_unset") or declared)
        )
        closes_at = window.get("closes_at")
        expired = False
        if closes_at:
            parsed = _parse_date(str(closes_at))
            expired = bool(parsed and ctx.now.date() > parsed)
        row = {
            "name": name,
            "env": env_var,
            "declared_mode": declared,
            "observed_env": observed,
            "effective_mode": effective,
            "closes_at": closes_at,
            "expired": expired,
        }
        detail["windows"].append(row)
        if declared and effective != declared:
            drift.append(f"{name}: declared {declared!r} but effective {effective!r}")
        if expired:
            drift.append(f"{name}: observe window closed {closes_at} and is still open")
    if drift:
        return AuditResult(
            "soak_window_diff", FAIL, "; ".join(drift), threshold, provenance, detail
        )
    return AuditResult(
        "soak_window_diff",
        OK,
        f"{len(windows)} observe/report window(s) match their declared mode and none has expired",
        threshold,
        provenance,
        detail,
    )


def _parse_date(value: str) -> date | None:
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


# --------------------------------------------------------------------------- 6 lane briefs


def check_lane_brief(ctx: AuditContext) -> AuditResult:
    """Lane-brief existence BY DECLARED CLASS.

    There are TWO lane creators. ``omniagentos/swarm/spawn.py``'s
    ``write_task_md`` writes ``var/task.md``; ``scripts/new-lane.sh`` creates a
    clone with ``git clone`` + ``mkdir -p $DEST/var`` and, in 95 lines,
    contained ZERO occurrences of ``task.md`` or ``LANE-BRIEF``. Quarantining a
    whole creator's output is how a check like this gets muted in week two, so
    the exemption is a DECLARED CLASS in ``var/LANE-CLASS`` (written by
    new-lane.sh) whose exempt values are listed in the registry — not a path
    blocklist, and an UNDECLARED class is a finding, not a pass.
    """
    cfg = ctx.check_cfg("lane_brief")
    provenance = cfg.get("provenance")
    min_age_minutes = _num(cfg, "min_age_minutes", 60)
    threshold = cfg.get(
        "threshold", f"non-empty brief required once a clone is > {min_age_minutes}m old"
    )
    root = ctx.repo_root / (cfg.get("root") or "var/swarm/clones")
    brief_paths = cfg.get("brief_paths") or ["var/task.md", "LANE-BRIEF.md", "var/LANE-BRIEF.md"]
    class_marker = cfg.get("class_marker") or "var/LANE-CLASS"
    exempt = {str(c) for c in (cfg.get("exempt_classes") or [])}
    known = exempt | {str(c) for c in (cfg.get("known_classes") or [])}
    detail: dict[str, Any] = {
        "root": str(root),
        "brief_paths": brief_paths,
        "class_marker": class_marker,
        "exempt_classes": sorted(exempt),
    }
    if not root.is_dir():
        return AuditResult(
            "lane_brief",
            FAIL,
            f"cannot-run: lane root absent at {root}",
            threshold,
            provenance,
            detail,
        )
    cutoff = ctx.now.timestamp() - min_age_minutes * 60
    briefless: list[str] = []
    undeclared: list[str] = []
    exempted: list[str] = []
    healthy = 0
    total = 0
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        total += 1
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        if mtime > cutoff:
            continue  # too young to have been briefed yet
        marker = entry / class_marker
        declared = None
        if marker.is_file():
            try:
                declared = marker.read_text(encoding="utf-8").strip().splitlines()[0].strip()
            except (OSError, IndexError):
                declared = ""
        has_brief = any(
            (entry / rel).is_file() and (entry / rel).stat().st_size > 0 for rel in brief_paths
        )
        if has_brief:
            healthy += 1
            continue
        if declared is None:
            briefless.append(entry.name)
            continue
        if declared in exempt:
            exempted.append(f"{entry.name}[{declared}]")
            continue
        if known and declared not in known:
            undeclared.append(f"{entry.name}[{declared or 'empty'}]")
            continue
        briefless.append(f"{entry.name}[{declared}]")
    detail.update(
        {
            "clones": total,
            "with_brief": healthy,
            "briefless": briefless,
            "exempted": exempted,
            "undeclared_class": undeclared,
        }
    )
    problems = []
    if briefless:
        problems.append(
            f"{len(briefless)} brief-less clone(s) of {total}: {', '.join(briefless[:6])}"
        )
    if undeclared:
        problems.append(
            f"{len(undeclared)} clone(s) declare an unregistered LANE-CLASS: {', '.join(undeclared[:6])}"
        )
    if problems:
        return AuditResult("lane_brief", FAIL, "; ".join(problems), threshold, provenance, detail)
    return AuditResult(
        "lane_brief",
        OK,
        f"{healthy}/{total} clone(s) carry a brief; {len(exempted)} exempt by declared class",
        threshold,
        provenance,
        detail,
    )


# --------------------------------------------------------------------------- 7 single signer


def check_single_signer(ctx: AuditContext) -> AuditResult:
    cfg = ctx.check_cfg("single_signer")
    provenance = cfg.get("provenance")
    window_days = int(_num(cfg, "window_days", 30))
    threshold = cfg.get(
        "threshold", f">=2 distinct signing keys across a {window_days}d promotion window"
    )
    table = cfg.get("table") or "promotions"
    column = cfg.get("signer_column") or "signing_key_id"
    ts_column = cfg.get("timestamp_column") or "created_at"
    db_path = _resolve_db(ctx, cfg)
    detail: dict[str, Any] = {"db": str(db_path), "table": table, "window_days": window_days}

    if not db_path.is_file():
        return AuditResult(
            "single_signer",
            FAIL,
            f"cannot-run: control-plane DB absent at {db_path}",
            threshold,
            provenance,
            detail,
        )
    try:
        conn = _open_ro(db_path)
    except sqlite3.Error as exc:
        return AuditResult(
            "single_signer",
            FAIL,
            f"cannot-run: {type(exc).__name__}: {exc}",
            threshold,
            provenance,
            detail,
        )
    try:
        tables = {r[0] for r in conn.execute("select name from sqlite_master where type='table'")}
        if table not in tables:
            return AuditResult(
                "single_signer",
                FAIL,
                f"cannot-run: promotion table {table!r} does not exist — single-signer risk is "
                "UNMEASURED, which is a fail, not a pass",
                threshold,
                provenance,
                detail,
            )
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            return AuditResult(
                "single_signer",
                FAIL,
                f"cannot-run: {table}.{column} absent (columns: {sorted(cols)[:8]})",
                threshold,
                provenance,
                detail,
            )
        cutoff = (ctx.now - timedelta(days=window_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        where = f"where {ts_column} >= ?" if ts_column in cols else ""
        params = (cutoff,) if where else ()
        rows = conn.execute(
            f"select {column} as k, count(*) as n from {table} {where} group by 1", params
        ).fetchall()
    except sqlite3.Error as exc:
        return AuditResult(
            "single_signer",
            FAIL,
            f"cannot-run: {type(exc).__name__}: {exc}",
            threshold,
            provenance,
            detail,
        )
    finally:
        conn.close()

    signers = {str(r["k"]): int(r["n"]) for r in rows}
    detail["signers"] = signers
    total = sum(signers.values())
    if total == 0:
        return AuditResult(
            "single_signer",
            WARN,
            f"no promotions in the last {window_days}d — nothing to attribute",
            threshold,
            provenance,
            detail,
        )
    if len(signers) <= 1:
        only = next(iter(signers))
        return AuditResult(
            "single_signer",
            FAIL,
            f"single-signer: all {total} promotion(s) in {window_days}d signed by key {only!r} — "
            "one compromised or absent key is the whole promotion path",
            threshold,
            provenance,
            detail,
        )
    return AuditResult(
        "single_signer",
        OK,
        f"{total} promotion(s) in {window_days}d across {len(signers)} distinct signing keys",
        threshold,
        provenance,
        detail,
    )


# --------------------------------------------------------------------------- 8 heartbeats


def check_unscheduled_heartbeat(ctx: AuditContext) -> AuditResult:
    cfg = ctx.check_cfg("unscheduled_heartbeat")
    provenance = cfg.get("provenance")
    registry_rel = cfg.get("registry") or "configs/expected-heartbeats.yaml"
    registry_path = ctx.repo_root / registry_rel
    threshold = cfg.get("threshold", "each declared artifact newer than its declared max_age_hours")
    detail: dict[str, Any] = {"registry": str(registry_path), "entries": []}

    if not registry_path.is_file():
        return AuditResult(
            "unscheduled_heartbeat",
            FAIL,
            f"cannot-run: {registry_rel} absent — the class check_launchd() structurally cannot "
            "see would be unmonitored",
            threshold,
            provenance,
            detail,
        )
    try:
        doc = _read_yaml(registry_path) or {}
    except Exception as exc:  # noqa: BLE001
        return AuditResult(
            "unscheduled_heartbeat",
            FAIL,
            f"cannot-run: {registry_rel} unreadable: {type(exc).__name__}: {exc}",
            threshold,
            provenance,
            detail,
        )
    entries = (doc.get("heartbeats") or {}) if isinstance(doc, dict) else {}
    if not entries:
        return AuditResult(
            "unscheduled_heartbeat",
            FAIL,
            f"cannot-run: {registry_rel} declares no heartbeats (an empty registry is a silent pass)",
            threshold,
            provenance,
            detail,
        )
    stale: list[str] = []
    for name, spec in entries.items():
        spec = spec or {}
        artifact = ctx.repo_root / str(spec.get("artifact") or "")
        max_age_hours = _num(spec, "max_age_hours", 24)
        row: dict[str, Any] = {
            "script": str(spec.get("script") or name),
            "artifact": str(artifact),
            "max_age_hours": max_age_hours,
            "provenance": spec.get("provenance"),
        }
        if not artifact.is_file():
            row["age_hours"] = None
            row["status"] = "missing"
            stale.append(f"{name}: artifact missing ({artifact})")
        else:
            age_hours = (ctx.now.timestamp() - artifact.stat().st_mtime) / 3600.0
            row["age_hours"] = round(age_hours, 2)
            row["status"] = "stale" if age_hours > max_age_hours else "fresh"
            if age_hours > max_age_hours:
                stale.append(
                    f"{name}: {artifact.name} is {age_hours:.1f}h old (max {max_age_hours}h) — "
                    f"{spec.get('script')} is in no plist and no crontab"
                )
        detail["entries"].append(row)
    if stale:
        return AuditResult(
            "unscheduled_heartbeat", FAIL, "; ".join(stale), threshold, provenance, detail
        )
    return AuditResult(
        "unscheduled_heartbeat",
        OK,
        f"{len(entries)} declared heartbeat artifact(s) fresh",
        threshold,
        provenance,
        detail,
    )


# --------------------------------------------------------------------------- 9 loopback


_URL_RE = re.compile(
    r"base_url:\s*[\"']?(?P<url>https?://(?P<host>127\.0\.0\.1|localhost|\[::1\]):(?P<port>\d+)[^\"'\s]*)"
)


def _declared_loopbacks(path: Path) -> list[dict[str, Any]]:
    """Scan connectors.yaml TEXTUALLY for loopback base_urls.

    Textual on purpose: the file is 1300+ lines of nested capability blocks and a
    structural walk would have to know every nesting shape it might grow. A line
    carrying ``expected_down: true`` within the same block suppresses the finding.
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for index, line in enumerate(lines):
        match = _URL_RE.search(line)
        if not match:
            continue
        window = lines[max(0, index - 6) : index + 7]
        expected_down = any(re.search(r"expected_down:\s*true", w) for w in window)
        out.append(
            {
                "url": match.group("url"),
                "host": match.group("host"),
                "port": int(match.group("port")),
                "line": index + 1,
                "expected_down": expected_down,
            }
        )
    return out


def _tcp_open(host: str, port: int, timeout: float) -> bool:
    target = "127.0.0.1" if host in ("127.0.0.1", "localhost") else host
    try:
        with socket.create_connection((target, port), timeout=timeout):
            return True
    except OSError:
        return False


def check_loopback_connectors(ctx: AuditContext) -> AuditResult:
    cfg = ctx.check_cfg("loopback_connectors")
    provenance = cfg.get("provenance")
    timeout = _num(cfg, "connect_timeout_seconds", 1.5)
    threshold = cfg.get("threshold", "every declared loopback base_url accepts a TCP connect")
    connectors = ctx.repo_root / (cfg.get("connectors") or "configs/connectors.yaml")
    detail: dict[str, Any] = {"connectors": str(connectors), "endpoints": []}
    if not connectors.is_file():
        return AuditResult(
            "loopback_connectors",
            FAIL,
            f"cannot-run: {connectors} absent",
            threshold,
            provenance,
            detail,
        )
    declared = _declared_loopbacks(connectors)
    if not declared:
        return AuditResult(
            "loopback_connectors",
            OK,
            "no loopback base_url declared in connectors.yaml",
            threshold,
            provenance,
            detail,
        )
    dead: list[str] = []
    probed: dict[tuple[str, int], bool] = {}
    for entry in declared:
        key = (entry["host"], entry["port"])
        if key not in probed:
            probed[key] = _tcp_open(entry["host"], entry["port"], timeout)
        entry["reachable"] = probed[key]
        detail["endpoints"].append(entry)
        if not entry["reachable"] and not entry["expected_down"]:
            dead.append(f"{entry['url']} (connectors.yaml:{entry['line']})")
    detail["note"] = "NOTIFY ONLY — this check never restarts anything; see module docstring."
    if dead:
        unique = sorted(set(dead))
        return AuditResult(
            "loopback_connectors",
            FAIL,
            f"{len(unique)} declared loopback endpoint(s) refuse a TCP connect: {'; '.join(unique)} "
            "[NOTIFY ONLY: never auto-restarted]",
            threshold,
            provenance,
            detail,
        )
    return AuditResult(
        "loopback_connectors",
        OK,
        f"all {len(probed)} declared loopback endpoint(s) accept a TCP connect "
        f"({sum(1 for e in declared if e['expected_down'])} annotated expected_down)",
        threshold,
        provenance,
        detail,
    )


# --------------------------------------------------------------------------- 10 spend caps


def check_provider_daily_spend(ctx: AuditContext) -> AuditResult:
    """Independently compare today's ledger spend with configured provider caps.

    This deliberately does not import or call the spend guard. A regression that
    bypasses that guard must still become visible from ``provider_call_usage``.
    """

    cfg = ctx.check_cfg("provider_daily_spend")
    threshold = cfg.get("threshold")
    provenance = cfg.get("provenance")
    caps_path = ctx.repo_root / str(cfg.get("caps") or "configs/spend-caps.yaml")
    providers = cfg.get("providers") or ["moonshot", "fireworks"]
    if (
        not isinstance(providers, list)
        or not providers
        or not all(isinstance(item, str) and item for item in providers)
    ):
        return AuditResult(
            "provider_daily_spend",
            FAIL,
            "cannot-run: providers registry row is invalid",
            threshold,
            provenance,
        )
    try:
        db_path = resolve_spend_db_path()
    except SpendDbResolutionError as exc:
        return AuditResult(
            "provider_daily_spend",
            FAIL,
            f"cannot-run: spend ledger identity refused ({exc})",
            threshold,
            provenance,
        )
    try:
        caps_doc = _read_yaml(caps_path)
        provider_cfg = caps_doc.get("providers") if isinstance(caps_doc, dict) else None
        if not isinstance(provider_cfg, dict):
            raise ValueError("providers mapping is absent")
        capped_provider_names = {
            str(name)
            for name, row in provider_cfg.items()
            if isinstance(row, dict) and row.get("enabled") is True
        }
        caps_nanos: dict[str, int] = {}
        for provider in providers:
            row = provider_cfg.get(provider)
            if not isinstance(row, dict) or row.get("enabled") is not True:
                raise ValueError(f"enabled cap row is absent for {provider}")
            cap_decimal = Decimal(str(row.get("daily_cap_usd")))
            cap_as_nanos = cap_decimal * Decimal(1_000_000_000)
            if (
                not cap_decimal.is_finite()
                or cap_decimal <= 0
                or cap_as_nanos != cap_as_nanos.to_integral_value()
            ):
                raise ValueError(f"daily cap is not a positive whole nano-USD for {provider}")
            caps_nanos[provider] = int(cap_as_nanos)
    except Exception as exc:  # noqa: BLE001 - inability to read a cap is FAIL
        return AuditResult(
            "provider_daily_spend",
            FAIL,
            f"cannot-run: spend caps unreadable ({type(exc).__name__}: {exc})",
            threshold,
            provenance,
            {"caps": str(caps_path)},
        )
    if not db_path.is_file():
        return AuditResult(
            "provider_daily_spend",
            FAIL,
            f"cannot-run: ledger missing: {db_path}",
            threshold,
            provenance,
            {"db": str(db_path)},
        )

    utc_day = ctx.now.astimezone(UTC).strftime("%Y-%m-%d")
    try:
        connection = sqlite3.connect(
            f"file:{db_path}?mode=ro",
            uri=True,
            timeout=1.5,
        )
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(
                "SELECT billing_provider AS provider, "
                "SUM(COALESCE(cost_usd_nanos, cost_upper_bound_usd_nanos, 0)) "
                "AS spend_usd_nanos "
                "FROM provider_call_usage WHERE substr(created_at, 1, 10) = ? "
                "AND billing_provider IS NOT NULL GROUP BY billing_provider",
                (utc_day,),
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        return AuditResult(
            "provider_daily_spend",
            FAIL,
            f"cannot-run: provider_call_usage unreadable ({type(exc).__name__}: {exc})",
            threshold,
            provenance,
            {"db": str(db_path), "utc_day": utc_day},
        )

    observed_nanos = {str(row["provider"]): int(row["spend_usd_nanos"] or 0) for row in rows}
    detail: dict[str, Any] = {"utc_day": utc_day, "db": str(db_path), "providers": {}}
    statuses: list[str] = []
    evidence_parts: list[str] = []
    unconfigured = sorted(set(observed_nanos) - capped_provider_names)
    if unconfigured:
        # Money under an unknown billing identity has no enforceable ceiling;
        # treat it as FAIL, the same severity as exceeding a known 100% cap.
        statuses.append(FAIL)
        detail["uncapped_billing_providers"] = unconfigured
        evidence_parts.append("UNCAPPED billing provider(s): " + ", ".join(unconfigured))
    for provider in providers:
        spend_nanos = observed_nanos.get(provider, 0)
        daily_cap_nanos = caps_nanos[provider]
        status = (
            FAIL
            if spend_nanos > daily_cap_nanos
            else WARN
            if spend_nanos * 100 > daily_cap_nanos * 80
            else OK
        )
        statuses.append(status)
        ratio = spend_nanos / daily_cap_nanos
        detail["providers"][provider] = {
            "spend_usd_nanos": spend_nanos,
            "cap_usd_nanos": daily_cap_nanos,
            "ratio": ratio,
            "status": status,
        }
        evidence_parts.append(
            f"{provider}=${spend_nanos / 1_000_000_000:.6f}/"
            f"${daily_cap_nanos / 1_000_000_000:.2f} ({ratio:.1%})"
        )
    return AuditResult(
        "provider_daily_spend",
        _worst(statuses),
        f"UTC {utc_day}: " + "; ".join(evidence_parts),
        threshold,
        provenance,
        detail,
    )


# --------------------------------------------------------------------------- runner


AUDIT_CHECKS: tuple[tuple[str, Any], ...] = (
    ("config_digest", check_config_digest),
    ("mcp_roster", check_mcp_roster),
    ("never_wired", check_never_wired),
    ("pump_ledger_wiring", check_pump_ledger_wiring),
    ("soak_window_diff", check_soak_window_diff),
    ("lane_brief", check_lane_brief),
    ("single_signer", check_single_signer),
    ("unscheduled_heartbeat", check_unscheduled_heartbeat),
    ("loopback_connectors", check_loopback_connectors),
    ("provider_daily_spend", check_provider_daily_spend),
)
CHECK_IDS = tuple(name for name, _fn in AUDIT_CHECKS)


def load_registry(path: Path) -> tuple[dict[str, Any], str | None]:
    if not path.is_file():
        return {}, f"registry missing: {path}"
    try:
        doc = _read_yaml(path) or {}
    except Exception as exc:  # noqa: BLE001
        return {}, f"registry unreadable: {type(exc).__name__}: {exc}"
    if not isinstance(doc, dict):
        return {}, "registry is not a mapping"
    return doc, None


def run_audit(
    *,
    repo_root: Path = REPO_ROOT,
    accounts_root: Path | None = None,
    registry_path: Path | None = None,
    only: list[str] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run every REGISTERED check and return a report. Mutates nothing."""
    started = time.monotonic()
    registry_path = registry_path or (repo_root / "configs" / "audit-checks.yaml")
    registry, registry_error = load_registry(registry_path)
    ctx = AuditContext(
        repo_root=repo_root,
        accounts_root=accounts_root or Path.home(),
        registry=registry,
        now=now or datetime.now(UTC),
    )
    registered = registry.get("checks") if isinstance(registry.get("checks"), dict) else {}
    results: list[AuditResult] = []
    ran: set[str] = set()

    for check_id, fn in AUDIT_CHECKS:
        if only and check_id not in only:
            continue
        cfg = registered.get(check_id) if isinstance(registered, dict) else None
        if registry and not isinstance(cfg, dict):
            results.append(
                AuditResult(
                    check_id,
                    FAIL,
                    f"unregistered: {check_id} has no row in {registry_path.name} — an assertion "
                    "with no registered threshold is an assertion nobody can audit",
                )
            )
            ran.add(check_id)
            continue
        if isinstance(cfg, dict) and cfg.get("enabled") is False:
            results.append(
                AuditResult(
                    check_id,
                    FAIL,
                    "disabled in the registry — a disabled standing check is indistinguishable "
                    "from a passing one, so it reports fail",
                    cfg.get("threshold"),
                    cfg.get("provenance"),
                )
            )
            ran.add(check_id)
            continue
        try:
            result = fn(ctx)
        except Exception as exc:  # noqa: BLE001 - a check that crashes is a FAIL, never a skip
            result = AuditResult(
                check_id,
                FAIL,
                f"check crashed: {type(exc).__name__}: {exc}",
                (cfg or {}).get("threshold") if isinstance(cfg, dict) else None,
                (cfg or {}).get("provenance") if isinstance(cfg, dict) else None,
                {"exception": f"{type(exc).__name__}: {exc}"},
            )
        results.append(result)
        ran.add(check_id)

    # Registered-but-unimplemented rows must also REPORT, or the registry becomes
    # a place to hide an assertion nobody runs.
    registered_map = registered if isinstance(registered, dict) else {}
    for check_id in sorted(set(registered_map) - set(CHECK_IDS)):
        if only and check_id not in only:
            continue
        registered_row = registered_map.get(check_id)
        registered_cfg = registered_row if isinstance(registered_row, dict) else {}
        results.append(
            AuditResult(
                check_id,
                FAIL,
                f"registered but not implemented: {check_id} appears in {registry_path.name} "
                "with no code behind it",
                registered_cfg.get("threshold"),
                registered_cfg.get("provenance"),
            )
        )
        ran.add(check_id)

    statuses = [r.status for r in results]
    missing_provenance = sorted(
        r.check_id for r in results if r.threshold is not None and not r.provenance
    )
    report = {
        "ts": ctx.now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "repo_root": str(repo_root),
        "registry": str(registry_path),
        "registry_error": registry_error,
        "registered": sorted(registered) if isinstance(registered, dict) else [],
        "reported": sorted(ran),
        "overall": _worst(statuses),
        "counts": {s: statuses.count(s) for s in (OK, WARN, FAIL)},
        "failing": [r.check_id for r in results if r.status == FAIL],
        "warning": [r.check_id for r in results if r.status == WARN],
        "missing_provenance": missing_provenance,
        "checks": [r.as_dict() for r in results],
        "duration_seconds": round(time.monotonic() - started, 3),
        "read_only": True,
    }
    return report


# --------------------------------------------------------------------------- cli


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--audit", action="store_true", help="run the standing wiring/drift audit")
    parser.add_argument(
        "--audit-repo-root", default=None, help="audit a different tree (tests use this)"
    )
    parser.add_argument(
        "--audit-accounts-root", default=None, help="override ~ for the account configs"
    )
    parser.add_argument("--audit-registry", default=None, help="override configs/audit-checks.yaml")
    parser.add_argument(
        "--audit-only", action="append", default=None, help="run a subset (repeatable)"
    )
    parser.add_argument(
        "--fail-on-finding",
        action="store_true",
        help="exit 1 when any check reports fail (default: exit 0 as long as every check REPORTED)",
    )
    parser.add_argument(
        "--audit-log",
        default=None,
        help="opt-in append-only JSONL of audit reports; the audit writes NOTHING without it",
    )


def run_audit_cli(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    repo_root = (
        Path(os.path.expanduser(args.audit_repo_root)).resolve()
        if args.audit_repo_root
        else REPO_ROOT
    )
    accounts_root = (
        Path(os.path.expanduser(args.audit_accounts_root)).resolve()
        if args.audit_accounts_root
        else None
    )
    registry_path = (
        Path(os.path.expanduser(args.audit_registry)).resolve()
        if args.audit_registry
        else repo_root / "configs" / "audit-checks.yaml"
    )
    report = run_audit(
        repo_root=repo_root,
        accounts_root=accounts_root,
        registry_path=registry_path,
        only=args.audit_only,
    )
    if getattr(args, "audit_log", None):
        log_path = Path(os.path.expanduser(args.audit_log))
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(report, sort_keys=True, default=str) + "\n")

    reported = set(report["reported"])
    expected = set(CHECK_IDS) | set(report["registered"])
    if args.audit_only:
        expected &= set(args.audit_only)
    if reported != expected:
        report["machinery_error"] = f"checks that did not report: {sorted(expected - reported)}"
        return 2, report
    if report["registry_error"]:
        return 2, report
    if getattr(args, "fail_on_finding", False) and report["failing"]:
        return 1, report
    return 0, report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Standing wiring/drift audit (standalone entry point)"
    )
    add_arguments(parser)
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    parser.add_argument(
        "--dry-run", action="store_true", help="accepted for symmetry; the audit is read-only"
    )
    parser.add_argument("--no-push", action="store_true", default=True, help="never push (DEFAULT)")
    args = parser.parse_args(argv)
    code, report = run_audit_cli(args)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
    else:
        print(
            f"audit {report['ts']} overall={report['overall']} "
            f"(ok={report['counts'][OK]} warn={report['counts'][WARN]} fail={report['counts'][FAIL]}) "
            f"in {report['duration_seconds']}s"
        )
        icons = {OK: "OK  ", WARN: "WARN", FAIL: "FAIL"}
        for check in report["checks"]:
            print(f"  [{icons.get(check['status'], '????')}] {check['id']}: {check['evidence']}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
