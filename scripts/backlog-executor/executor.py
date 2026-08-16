#!/usr/bin/env python
"""backlog-executor -- nightly unattended backlog work, behind hard rails.

Runs at 00:30 via launchd (com.omniagentos.backlog-executor). One night:

  1. COLLECT open backlog candidates from devtasks/SWARM-EXECUTION-TODO.md
     (open ``⬜`` rows), the latest 3 fable-curator reports' "Deferred" /
     "Proposed …" sections, and the swarm optimizer playbook's
     "Improvement opportunities" tail.
  2. SELECT <=3 picks on Kimi (one retry; else skip the night). The
     selection prompt + runtime policy live in ``prompt.md`` next to this
     file -- editing that file genuinely changes selection behavior.
  3. ENFORCE the code-level deny list over pick briefs (a second layer the
     prompt cannot edit away): policy/approvals/migration/settings/secrets/
     payment/delete shapes are dropped post-selection.
  4. EXECUTE each pick sequentially via POST /api/swarm against a FRESH
     ``git clone`` of the repo under var/backlog/<date>-<id>/repo -- the
     live checkout is never a swarm working dir.
  5. MERGE-GATE: full pytest suite (main repo's venv, PYTHONPATH=clone,
     known env flakes deselected) must be green in the clone. Green work
     is then fetched into the live repo as branch ``backlog/<date>-<id>``.
     TWO-TIER merge policy: auto-merge --no-ff into the open integration
     batch worktree immediately ONLY when the item is risk_class none AND
     single-attempt AND the diff touches no test files and <= 6 files;
     green-but-bigger work is HELD on its branch (empty ``HOLD:`` commit
     notes why). Red never merges (branch + alert). After each clean-tier
     merge the suite runs once more in the batch worktree; red there ->
     immediate ``git revert -m 1`` + alert + stop night. main is never a
     merge target (no open/clean batch => held-for-morning).
  6. FINAL MERGE PASS (the operator: "branches should be merged by 5 AM, I can
     always roll back"): after all items finish, each held-but-GREEN branch
     is re-verified against the candidate merge (live main merged into the
     item clone, full suite there) and then merged into the open
     integration batch with a ``[held-tier]`` tag for the morning eyeball.
     Wall-clock enforced: the pass is skipped entirely past 04:45 and
     stops at 05:00 (policy ``merge_deadline_hour``). Only RED branches
     remain unmerged.
  7. BOOKKEEPING: improvement-log line per item (plus one per held-tier
     merge), TODO row flipped to ✅ for merged TODO-sourced items, morning
     digest (merged-clean / merged-held-tier / failed-unmerged) at
     var/backlog/digest-<date>.md + one "info" bell. Rollback path is the
     hourly GitHub backup job -- this process NEVER pushes.

DRY-RUN (``OMNIAGENTOS_BACKLOG_DRY_RUN=1``, the installed default): collect +
select + log/digest the picks, dispatch NOTHING.

Hard rails, in code (not prompt-editable): <=3 items/night; sequential
execution; deny-list post-selection; fresh-clone working dirs only; merge
gate + two-tier hold; auto-revert on post-merge red; stop on first red
merge; never ``git push``; never touch policy/approvals/settings surfaces
itself (it only merges branches whose suite is green).
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------
# Paths / constants
# --------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent.parent  # <repo>/scripts/backlog-executor -> <repo>

PROMPT_PATH = SCRIPT_DIR / "prompt.md"
TODO_PATH = ROOT / "devtasks" / "SWARM-EXECUTION-TODO.md"
PLAYBOOK_PATH = ROOT / "vault" / "swarm" / "playbook.md"
REPORTS_DIR = Path.home() / ".claude" / "curator-reports"
VENV_PY = ROOT / ".venv" / "bin" / "python"

# Runtime state paths. ``None`` means "derive from var_root()"; the suite's
# sandbox fixture monkeypatches these names to a tmp tree, which the accessors
# below honour. Resolution is lazy so importing this module (or running
# ``--help``) never depends on the environment or on an editable install.
LOG_PATH: Path | None = None
IMPROVEMENT_LOG: Path | None = None
BACKLOG_DIR: Path | None = None


def var_root() -> Path:
    """The var root this run writes to — resolved, never re-derived ad hoc.

    Under a campaign (``OMNIAGENTOS_SIM_MODE=1``) this is the campaign var root,
    so a simulated night cannot append to the operator checkout's improvement
    log or create backlog items in it. Outside simulation it stays
    ``<repo>/var`` exactly as before: the readers of these files
    (``omniagentos/improvement_chain.py``, ``api/routes/system.py``) are
    package/repo-anchored, so honouring ``OMNIAGENTOS_VAR_DIR`` in production
    would split live state in two with no migration.

    ``ROOT`` is read at call time so the sandbox fixture's patch applies, and
    the package is imported from THIS checkout's ``sys.path``.
    """
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from omniagentos.runtime_paths import TOKEN_VAR_ENV_KEYS, resolve_sim_context_or_none
    from omniagentos.runtime_paths import resolve_var_root as _resolve_var_root

    if resolve_sim_context_or_none() is None:
        return ROOT / "var"
    return _resolve_var_root(env_keys=TOKEN_VAR_ENV_KEYS)


def log_path() -> Path:
    return LOG_PATH if LOG_PATH is not None else var_root() / "log" / "backlog-executor.log"


def improvement_log_path() -> Path:
    return IMPROVEMENT_LOG if IMPROVEMENT_LOG is not None else var_root() / "improvement-log.jsonl"


def backlog_dir() -> Path:
    return BACKLOG_DIR if BACKLOG_DIR is not None else var_root() / "backlog"


def default_token_path() -> Path:
    return var_root() / "secrets" / "sessions-token"


# HARD caps -- prompt.md's policy block may lower these, never raise them.
HARD_MAX_ITEMS = 3
DEFAULT_AUTO_MERGE_MAX_FILES = 6

# Code-level deny list (second enforcement layer; immutable at runtime --
# prompt.md's own deny_list is unioned IN, it can never remove these).
CODE_DENY_PATTERNS: tuple[str, ...] = (
    r"policy\.yaml",
    r"approvals?",
    r"migration",
    r"settings\.json",
    r"secrets?",
    r"payment",
    r"delete",
)

# Known env flakes deselected from the merge-gate suite (they fail on
# environment, not code). Verified 2026-07-24: live full suite 3684 green;
# clone-mode gate (main venv + PYTHONPATH=clone) 3687 green with exactly ONE
# env-dependent failure -- the agentless e2e that spawns real git+pytest.
# The design brief anticipated two; only one reproduces today. Extend via
# OMNIAGENTOS_BACKLOG_DESELECT (comma-separated node ids) if a second shows.
KNOWN_ENV_FLAKES: tuple[str, ...] = (
    "tests/agentless/test_e2e_real.py::test_agentless_e2e_real_git_and_pytest",
)


def gate_deselects() -> tuple[str, ...]:
    extra = tuple(
        s.strip()
        for s in os.environ.get("OMNIAGENTOS_BACKLOG_DESELECT", "").split(",")
        if s.strip()
    )
    return KNOWN_ENV_FLAKES + extra


API_BASE_DEFAULT = "http://127.0.0.1:8485/api"


def utcnow_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg: str) -> None:
    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(f"{utcnow_iso()} backlog-executor {msg}\n")


# --------------------------------------------------------------------------
# Policy (parsed from prompt.md's fenced yaml block)
# --------------------------------------------------------------------------


DEFAULT_MERGE_DEADLINE_HOUR = 5
MERGE_PASS_CUTOFF_MARGIN_MIN = 15  # skip the whole pass past 04:45


@dataclass
class Policy:
    max_items: int = HARD_MAX_ITEMS
    auto_merge_max_files: int = DEFAULT_AUTO_MERGE_MAX_FILES
    deny_list: tuple[str, ...] = CODE_DENY_PATTERNS
    merge_deadline_hour: int = DEFAULT_MERGE_DEADLINE_HOUR

    @property
    def effective_max_items(self) -> int:
        return max(1, min(self.max_items, HARD_MAX_ITEMS))


def parse_policy(prompt_text: str) -> tuple[Policy, str | None]:
    """Extract the ``policy:`` mapping from prompt.md's fenced yaml block.

    Returns ``(policy, problem)`` -- ``problem`` is a log-worthy string when
    the block is missing/malformed and code defaults were used. The code
    deny list is ALWAYS included (union); prompt edits can only extend it.
    """
    match = re.search(r"```yaml\s*\n(.*?)```", prompt_text, re.DOTALL)
    if not match:
        return Policy(), "no fenced yaml policy block found; using code defaults"
    try:
        import yaml

        loaded = yaml.safe_load(match.group(1))
    except Exception as exc:  # noqa: BLE001 -- malformed yaml -> defaults, logged
        return Policy(), f"policy yaml failed to parse ({exc}); using code defaults"
    if not isinstance(loaded, dict) or not isinstance(loaded.get("policy"), dict):
        return Policy(), "yaml block has no 'policy:' mapping; using code defaults"
    raw = loaded["policy"]
    policy = Policy()
    if isinstance(raw.get("max_items"), int) and raw["max_items"] > 0:
        policy.max_items = raw["max_items"]
    if isinstance(raw.get("auto_merge_max_files"), int) and raw["auto_merge_max_files"] > 0:
        policy.auto_merge_max_files = raw["auto_merge_max_files"]
    if isinstance(raw.get("merge_deadline_hour"), int) and 0 < raw["merge_deadline_hour"] <= 23:
        policy.merge_deadline_hour = raw["merge_deadline_hour"]
    extra = raw.get("deny_list")
    patterns = list(CODE_DENY_PATTERNS)
    if isinstance(extra, list):
        for pat in extra:
            if isinstance(pat, str) and pat.strip():
                try:
                    re.compile(pat)
                except re.error:
                    continue
                if pat not in patterns:
                    patterns.append(pat)
    policy.deny_list = tuple(patterns)
    return policy, None


# --------------------------------------------------------------------------
# Candidate collection
# --------------------------------------------------------------------------


@dataclass
class Candidate:
    id: str
    title: str
    source: str
    text: str
    raw_line: str = ""  # exact TODO row line, for the ✅ update after a merge


def _slug(text: str, limit: int = 24) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (slug[:limit].rstrip("-")) or "item"


def parse_todo_candidates(text: str) -> list[Candidate]:
    """Open rows (⬜ TODO / ⬜ QUEUED) from devtasks/SWARM-EXECUTION-TODO.md."""
    candidates: list[Candidate] = []
    seen: set[str] = set()
    for line in text.splitlines():
        if "⬜" not in line or not line.lstrip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        key, what = cells[0], cells[1]
        base = f"todo-{_slug(key if key not in {'', '—', '-'} else what)}"
        cid = base
        n = 2
        while cid in seen:
            cid = f"{base}-{n}"
            n += 1
        seen.add(cid)
        title = f"{key}: {what}" if key not in {"", "—", "-"} else what
        candidates.append(
            Candidate(
                id=cid,
                title=title[:160],
                source="devtasks/SWARM-EXECUTION-TODO.md",
                text=line.strip(),
                raw_line=line,
            )
        )
    return candidates


def parse_report_candidates(text: str, report_name: str) -> list[Candidate]:
    """Bullets under 'Deferred' / 'Proposed …' headings of a curator report."""
    candidates: list[Candidate] = []
    in_section = False
    idx = 0
    for line in text.splitlines():
        heading = re.match(r"^#{1,6}\s+(.*)", line)
        if heading:
            in_section = bool(re.search(r"deferred|proposed", heading.group(1), re.IGNORECASE))
            continue
        if in_section:
            bullet = re.match(r"^[-*]\s+(.*\S)", line)
            if bullet:
                idx += 1
                body = bullet.group(1)
                candidates.append(
                    Candidate(
                        id=f"report-{Path(report_name).stem}-{idx}",
                        title=body[:120],
                        source=f"curator-report:{report_name}",
                        text=body,
                    )
                )
    return candidates


def parse_playbook_candidates(text: str) -> list[Candidate]:
    """Bullets of the LAST 'Improvement opportunities' section of playbook.md."""
    sections = re.split(r"^#{1,6}\s+", text, flags=re.MULTILINE)
    target: str | None = None
    for section in sections:
        first_line = section.splitlines()[0] if section.splitlines() else ""
        if re.search(r"improvement opportunities", first_line, re.IGNORECASE):
            target = section
    if target is None:
        return []
    candidates: list[Candidate] = []
    idx = 0
    for line in target.splitlines()[1:]:
        bullet = re.match(r"^[-*]\s+(.*\S)", line)
        if bullet:
            idx += 1
            body = bullet.group(1)
            candidates.append(
                Candidate(
                    id=f"playbook-{idx}",
                    title=re.sub(r"\*+", "", body)[:120],
                    source="vault/swarm/playbook.md",
                    text=body,
                )
            )
    return candidates


def collect_candidates(
    todo_path: Path | None = None,
    reports_dir: Path | None = None,
    playbook_path: Path | None = None,
) -> list[Candidate]:
    """All open backlog candidates, normalized to {id,title,source,text}."""
    todo_path = todo_path or TODO_PATH
    reports_dir = reports_dir or REPORTS_DIR
    playbook_path = playbook_path or PLAYBOOK_PATH
    candidates: list[Candidate] = []
    if todo_path.is_file():
        candidates.extend(parse_todo_candidates(todo_path.read_text(encoding="utf-8")))
    if reports_dir.is_dir():
        reports = sorted(p for p in reports_dir.glob("*.md") if p.is_file())
        for report in reports[-3:]:
            try:
                candidates.extend(
                    parse_report_candidates(report.read_text(encoding="utf-8"), report.name)
                )
            except OSError:
                continue
    if playbook_path.is_file():
        candidates.extend(parse_playbook_candidates(playbook_path.read_text(encoding="utf-8")))
    return candidates


# --------------------------------------------------------------------------
# Selection (Kimi primary, one retry -- Opus/Fable review the plan upstream)
# --------------------------------------------------------------------------


@dataclass
class Pick:
    id: str
    why: str
    brief: str
    verify_hint: str = ""


def build_selection_prompt(prompt_text: str, candidates: list[Candidate], max_items: int) -> str:
    payload = [
        {"id": c.id, "title": c.title, "source": c.source, "text": c.text} for c in candidates
    ]
    return (
        f"{prompt_text}\n\n"
        f"## Candidates (JSON)\n\n{json.dumps(payload, indent=1)}\n\n"
        f"## Reminder\n\nPick at most {max_items}. Respond with the single strict JSON "
        'object only: {"picks": [...]} -- no prose, no fences.\n'
    )


def _extract_json_object(text: str) -> dict[str, Any]:
    """Strict-first JSON extraction from a model reply (fences tolerated)."""
    try:
        loaded = json.loads(text)
        if isinstance(loaded, dict):
            return loaded
    except json.JSONDecodeError:
        pass
    fenced = re.search(r"```(?:json)?\s*\n(.*?)```", text, re.DOTALL)
    if fenced:
        try:
            loaded = json.loads(fenced.group(1))
            if isinstance(loaded, dict):
                return loaded
        except json.JSONDecodeError:
            pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        loaded = json.loads(text[start : end + 1])
        if isinstance(loaded, dict):
            return loaded
    raise ValueError("no JSON object found in selection reply")


def parse_picks(text: str, candidate_ids: set[str], max_items: int) -> list[Pick]:
    """Validate the judge's reply. Raises ValueError when malformed."""
    obj = _extract_json_object(text)
    raw_picks = obj.get("picks")
    if not isinstance(raw_picks, list):
        raise ValueError("reply JSON has no 'picks' list")
    picks: list[Pick] = []
    for raw in raw_picks:
        if not isinstance(raw, dict):
            raise ValueError("pick is not an object")
        pick_id, why, brief = raw.get("id"), raw.get("why"), raw.get("brief")
        if not (isinstance(pick_id, str) and isinstance(why, str) and isinstance(brief, str)):
            raise ValueError("pick missing id/why/brief strings")
        if not brief.strip():
            raise ValueError("pick has an empty brief")
        if pick_id not in candidate_ids:
            log(f"selection: dropped pick with unknown candidate id {pick_id!r}")
            continue
        verify_hint = raw.get("verify_hint")
        picks.append(
            Pick(
                id=pick_id,
                why=why.strip(),
                brief=brief.strip(),
                verify_hint=verify_hint.strip() if isinstance(verify_hint, str) else "",
            )
        )
    return picks[:max_items]


def _resolve_bin(name: str, extra_candidates: tuple[str, ...]) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    for candidate in extra_candidates:
        expanded = os.path.expanduser(candidate)
        if os.access(expanded, os.X_OK):
            return expanded
    return None


def run_grok_selection(prompt: str, cwd: Path, timeout: int = 420) -> str:
    """grok-4.5, non-interactive JSON -- the fusion-worker.sh proven pattern
    plus ``--sandbox read-only`` (selection must not write anything)."""
    grok_bin = _resolve_bin("grok", ("~/.grok/bin/grok",))
    if grok_bin is None:
        raise RuntimeError("grok CLI not found")
    prompt_file = cwd / "selection-prompt.md"
    prompt_file.write_text(prompt, encoding="utf-8")
    proc = subprocess.run(
        [
            grok_bin,
            "--prompt-file",
            str(prompt_file),
            "-m",
            "grok-4.5",
            "--reasoning-effort",
            "high",
            "--sandbox",
            "read-only",
            "--cwd",
            str(cwd),
            "--always-approve",
            "--output-format",
            "json",
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"grok rc={proc.returncode}: {proc.stderr.strip()[:300]}")
    envelope = _extract_json_object(proc.stdout)
    text = envelope.get("text")
    if not isinstance(text, str) or not text.strip():
        raise RuntimeError("grok envelope had no text")
    return text


def run_kimi_selection(prompt: str, cwd: Path, timeout: int = 420) -> str:
    """Run the central Kimi loop model in the repo's read-only sandbox."""
    del cwd
    from omniagentos.improvement_chain import run_kimi_json

    schema = {
        "type": "object",
        "required": ["picks"],
        "properties": {
            "picks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["id", "why", "brief"],
                    "properties": {
                        "id": {"type": "string"},
                        "why": {"type": "string"},
                        "brief": {"type": "string"},
                        "verify_hint": {"type": "string"},
                    },
                },
            }
        },
    }
    result = run_kimi_json(prompt, schema, wall_ms=timeout * 1000)
    if not isinstance(result, dict):
        raise RuntimeError("Kimi selection returned no structured result")
    return json.dumps(result, ensure_ascii=False)


def select_picks(
    candidates: list[Candidate],
    prompt_text: str,
    policy: Policy,
    *,
    workdir: Path,
    grok_runner: Callable[[str, Path], str] = run_grok_selection,
    kimi_runner: Callable[[str, Path], str] = run_kimi_selection,
) -> list[Pick]:
    """Kimi -> one retry -> [] (skip the night, logged).

    ``grok_runner`` remains in the signature for callers that injected the old
    seam, but model policy no longer selects it.
    """
    del grok_runner
    if not candidates:
        log("selection: zero candidates collected; nothing to do")
        return []
    max_items = policy.effective_max_items
    prompt = build_selection_prompt(prompt_text, candidates, max_items)
    ids = {c.id for c in candidates}
    for runner, label in ((kimi_runner, "kimi"), (kimi_runner, "kimi-retry")):
        try:
            reply = runner(prompt, workdir)
            try:  # persist the raw reply for the morning eyeball (best-effort)
                (workdir / f"selection-reply-{label}.txt").write_text(reply, encoding="utf-8")
            except OSError:
                pass
            picks = parse_picks(reply, ids, max_items)
            log(f"selection: {label} returned {len(picks)} pick(s)")
            return picks
        except Exception as exc:  # noqa: BLE001 -- each rung logged, next rung tried
            log(f"selection: {label} failed: {exc}")
    log("selection: all judges failed; SKIPPING the night")
    return []


def enforce_deny_list(
    picks: list[Pick], patterns: tuple[str, ...] = CODE_DENY_PATTERNS
) -> tuple[list[Pick], list[tuple[Pick, str]]]:
    """Code-level rail: drop any pick whose brief/why matches a deny regex."""
    compiled = [re.compile(p, re.IGNORECASE) for p in patterns]
    kept: list[Pick] = []
    dropped: list[tuple[Pick, str]] = []
    for pick in picks:
        haystack = f"{pick.brief}\n{pick.why}\n{pick.verify_hint}"
        hit = next((c.pattern for c in compiled if c.search(haystack)), None)
        if hit is None:
            kept.append(pick)
        else:
            dropped.append((pick, hit))
    return kept, dropped


# --------------------------------------------------------------------------
# Swarm API client (loopback, session token)
# --------------------------------------------------------------------------


class SwarmApi:
    def __init__(self, base: str | None = None, token_path: Path | None = None) -> None:
        self.base = (base or os.environ.get("OMNIAGENTOS_BACKLOG_API") or API_BASE_DEFAULT).rstrip(
            "/"
        )
        token_path = token_path or default_token_path()
        self.token = token_path.read_text(encoding="utf-8").strip() if token_path.is_file() else ""

    def _request(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"{self.base}{path}",
            data=data,
            method=method,
            headers={
                "Content-Type": "application/json",
                "X-Session-Token": self.token,
            },
        )
        with urllib.request.urlopen(req, timeout=600) as resp:
            return json.loads(resp.read().decode())

    def dispatch(self, brief: str, working_dir: str) -> str:
        out = self._request("POST", "/swarm", {"brief": brief, "working_dir": working_dir})
        run_id = out.get("swarm_run_id")
        if not isinstance(run_id, str) or not run_id:
            raise RuntimeError(f"dispatch returned no swarm_run_id: {out}")
        return run_id

    def get_run(self, run_id: str) -> dict[str, Any]:
        return self._request("GET", f"/swarm/{run_id}")

    def cancel(self, run_id: str) -> None:
        try:
            self._request("POST", f"/swarm/{run_id}/cancel")
        except (urllib.error.URLError, RuntimeError, OSError) as exc:
            log(f"cancel {run_id} failed (best-effort): {exc}")


TERMINAL_RUN_STATUSES = {"completed", "failed", "cancelled"}


# --------------------------------------------------------------------------
# Git + pytest plumbing (injectable for tests)
# --------------------------------------------------------------------------


def run_git(args: list[str], cwd: Path, timeout: int = 600) -> tuple[int, str]:
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=timeout, check=False
    )
    return proc.returncode, (proc.stdout + proc.stderr).strip()


# Held when there is no usable open integration batch for overnight merges.
NO_BATCH_HELD_REASON = (
    "no open integration batch (or batch worktree dirty); refusing to merge — "
    "open one with: python -m omniagentos.integration.batch open"
)

# Branches that must never be overnight merge targets (extensible).
PROTECTED_BRANCHES: tuple[str, ...] = ("main",)


@dataclass(frozen=True)
class BatchTarget:
    """Open integration batch merge destination (branch + worktree path)."""

    branch: str
    worktree_path: Path


def _same_directory(a: Path, b: Path) -> bool | None:
    """Return whether *a* and *b* name the same directory.

    Compares resolved real paths and, when needed, inodes via ``samefile``.
    Returns ``None`` when identity cannot be determined (caller must fail closed).
    """
    try:
        ra = a.resolve()
        rb = b.resolve()
    except (OSError, RuntimeError):
        return None
    if ra == rb:
        return True
    try:
        if ra.is_dir() and rb.is_dir() and ra.samefile(rb):
            return True
    except OSError:
        return None
    return False


# Later swap: from omniagentos.integration.batch import resolve_current_batch as _resolve_batch_target
def _resolve_batch_target(root: Path) -> BatchTarget | None:
    """Read-only resolve of the open integration batch merge target.

    Reads ``<root>/var/integration/current-batch.json``
    (``{branch, worktree, status, opened_at, pinned_sha}``). Returns None
    unless the file parses, ``status == "open"``, the recorded branch is not
    protected, the worktree directory exists and does not alias the repo root,
    ``git status --porcelain`` is clean, and
    ``git symbolic-ref --quiet --short HEAD`` equals the recorded branch.
    Fail closed: unknown path identity or protected branch ⇒ None.
    """
    path = root / "var" / "integration" / "current-batch.json"
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    if raw.get("status") != "open":
        return None
    branch = raw.get("branch")
    worktree_raw = raw.get("worktree")
    if not isinstance(branch, str) or not branch.strip():
        return None
    if not isinstance(worktree_raw, str) or not worktree_raw.strip():
        return None
    branch = branch.strip()
    if branch in PROTECTED_BRANCHES:
        return None
    try:
        worktree = Path(worktree_raw)
        if not worktree.is_absolute():
            worktree = root / worktree
        worktree = worktree.resolve()
        root_resolved = root.resolve()
    except (OSError, RuntimeError):
        return None
    if not worktree.is_dir():
        return None
    # Refuse targets that alias the live repo root (symlink / trailing "/." etc.).
    same_as_root = _same_directory(worktree, root_resolved)
    if same_as_root is not False:  # True (alias) or None (unknown) → fail closed
        return None
    rc, dirty = run_git(["status", "--porcelain"], worktree)
    if rc != 0 or dirty.strip():
        return None
    rc, head = run_git(["symbolic-ref", "--quiet", "--short", "HEAD"], worktree)
    if rc != 0 or head.strip() != branch:
        return None
    return BatchTarget(branch=branch, worktree_path=worktree)


def suite_command(deselect: tuple[str, ...] | None = None) -> list[str]:
    cmd = [str(VENV_PY), "-m", "pytest", "tests", "-q", "-x"]
    for flake in gate_deselects() if deselect is None else deselect:
        cmd.extend(["--deselect", flake])
    return cmd


def run_suite(cwd: Path, timeout_minutes: int) -> tuple[bool, str]:
    """Full suite via the MAIN repo's venv, imports pinned to ``cwd`` via
    PYTHONPATH (the editable install would otherwise import the live repo)."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(cwd)
    env.pop("OMNIAGENTOS_DB", None)
    try:
        proc = subprocess.run(
            suite_command(),
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout_minutes * 60,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, f"suite timed out after {timeout_minutes} min"
    tail = "\n".join((proc.stdout + "\n" + proc.stderr).strip().splitlines()[-25:])
    return proc.returncode == 0, tail


# --------------------------------------------------------------------------
# Two-tier merge classification (user-directed)
# --------------------------------------------------------------------------

TEST_PATH_RE = re.compile(r"(^|/)tests?/|(^|/)test_[^/]*$|_test\.py$")


def is_test_path(path: str) -> bool:
    return bool(TEST_PATH_RE.search(path))


def classify_merge_tier(
    *,
    risk_classes: list[str],
    max_attempts_per_task: int,
    changed_files: list[str],
    policy: Policy,
) -> tuple[str, str | None]:
    """AUTO-MERGE only for: risk_class none + single-attempt + no test files
    + <= auto_merge_max_files files. Everything else green is HELD."""
    risky = sorted({rc for rc in risk_classes if rc and rc != "none"})
    if risky:
        return "hold", f"risk_class {','.join(risky)} (auto-merge requires none)"
    if max_attempts_per_task > 1:
        return (
            "hold",
            f"needed {max_attempts_per_task} attempts (auto-merge requires single-attempt)",
        )
    touched_tests = [p for p in changed_files if is_test_path(p)]
    if touched_tests:
        return "hold", f"diff touches test files ({touched_tests[0]})"
    if len(changed_files) > policy.auto_merge_max_files:
        return "hold", f"{len(changed_files)} files changed (cap {policy.auto_merge_max_files})"
    return "merge", None


def run_detail_gate_inputs(detail: dict[str, Any]) -> tuple[list[str], int]:
    """(risk_classes, max attempts per task) from GET /api/swarm/{id}."""
    risk_classes: list[str] = []
    plan_raw = (detail.get("run") or {}).get("plan_json")
    try:
        plan = json.loads(plan_raw) if isinstance(plan_raw, str) else (plan_raw or {})
        for task in plan.get("tasks", []) if isinstance(plan, dict) else []:
            if isinstance(task, dict):
                risk_classes.append(str(task.get("risk_class") or "none"))
    except (json.JSONDecodeError, AttributeError, TypeError):
        risk_classes.append("unparseable")
    attempts = detail.get("attempts") or {}
    max_attempts = 0
    if isinstance(attempts, dict):
        for rows in attempts.values():
            if isinstance(rows, list):
                max_attempts = max(max_attempts, len(rows))
    return risk_classes, max_attempts


# --------------------------------------------------------------------------
# Bookkeeping: improvement log, TODO row flip, digest, bells
# --------------------------------------------------------------------------


def append_improvement_log(entry: dict[str, Any], path: Path | None = None) -> None:
    path = path or improvement_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def improvement_entry(item: ItemResult, date: str) -> dict[str, Any]:
    return {
        "ts": utcnow_iso(),
        "improver": "backlog-executor",
        "changes": [
            {
                "path": item.branch or f"backlog/{date}-{item.safe_id}",
                "kind": "backlog",
                "summary": item.title,
                "outcome": item.outcome,
            }
        ],
        "notes": item.note,
    }


def mark_todo_row_done(todo_path: Path, raw_line: str, date: str) -> bool:
    """Flip the exact matched open row to ✅ DONE (backlog-executor <date>)."""
    if not todo_path.is_file() or not raw_line.strip():
        return False
    text = todo_path.read_text(encoding="utf-8")
    if raw_line not in text.splitlines():
        return False
    updated = re.sub(r"⬜\s*(TODO|QUEUED)", f"✅ DONE (backlog-executor {date})", raw_line, count=1)
    if updated == raw_line:
        updated = raw_line.replace("⬜", f"✅ DONE (backlog-executor {date})", 1)
    lines = text.splitlines(keepends=False)
    lines[lines.index(raw_line)] = updated
    todo_path.write_text("\n".join(lines) + ("\n" if text.endswith("\n") else ""), encoding="utf-8")
    return True


def render_digest(date: str, results: list[ItemResult], dry_run: bool = False) -> str:
    """Morning digest, <= 30 lines: merged-clean / merged-held-tier /
    failed-unmerged (a green-but-unmerged held branch lands in
    failed-unmerged with its exact merge command)."""
    clean = [r for r in results if r.outcome == "merged"]
    held_tier = [r for r in results if r.outcome == "merged-held-tier"]
    unmerged = [r for r in results if r.outcome in {"failed", "reverted", "held-for-morning"}]
    lines: list[str] = [f"# Overnight backlog digest — {date}" + (" (DRY-RUN)" if dry_run else "")]
    lines.append(
        f"{len(clean)} merged-clean, {len(held_tier)} merged-held-tier, "
        f"{len(unmerged)} failed-unmerged"
    )
    if clean:
        lines.append("## Merged (clean)")
        for r in clean:
            lines.append(f"- {(r.merge_sha or '')[:10]} {r.title} — {r.note}")
    if held_tier:
        lines.append("## Merged (held-tier — eyeball these first)")
        for r in held_tier:
            lines.append(f"- {(r.merge_sha or '')[:10]} {r.title} — held: {r.held_reason}")
    if unmerged:
        lines.append("## Failed / unmerged")
        for r in unmerged:
            lines.append(f"- {r.branch or r.safe_id} — [{r.outcome}] {r.note[:160]}")
            if r.outcome == "held-for-morning" and r.branch:
                lines.append(f"  merge: git -C {ROOT} merge --no-ff {r.branch}")
    if not results:
        lines.append("No items ran tonight.")
    if len(lines) > 30:
        lines = lines[:29] + ["… (truncated)"]
    return "\n".join(lines) + "\n"


def notify(kind: str, title: str, body: str = "", payload: dict[str, Any] | None = None) -> None:
    """Best-effort bell -- a notification failure never fails the night."""
    try:
        from omniagentos.notifications.service import record_notification

        record_notification(
            kind=kind,
            title=title,
            body=body,
            severity="warning" if kind == "alert" else "info",
            payload=payload or {},
        )
    except Exception as exc:  # noqa: BLE001
        log(f"notify failed (best-effort): {exc}")


# --------------------------------------------------------------------------
# Per-item execution
# --------------------------------------------------------------------------


@dataclass
class ItemResult:
    pick: Pick
    title: str
    safe_id: str
    # merged | merged-held-tier | held-for-morning | failed | reverted
    outcome: str = "failed"
    note: str = ""
    branch: str = ""
    merge_sha: str = ""
    clone_dir: str = ""
    held_reason: str = ""
    stop_night: bool = False


@dataclass
class Runtime:
    """Injectable side-effect seams (tests swap these for fakes)."""

    api: Any
    git: Callable[[list[str], Path], tuple[int, str]] = run_git
    suite: Callable[[Path, int], tuple[bool, str]] = run_suite
    notify: Callable[..., None] = notify
    now_fn: Callable[[], datetime] = datetime.now
    poll_seconds: int = 30
    item_timeout_min: int = 120
    suite_timeout_min: int = 45


def _compose_brief(pick: Pick, clone_dir: Path) -> str:
    extra = f"\n\nVerification hint: {pick.verify_hint}" if pick.verify_hint else ""
    return (
        f"{pick.brief}{extra}\n\n"
        "Boundaries (hard): work ONLY inside this checkout; small single-agent "
        "change; do NOT touch policy/approvals/migrations/settings/secrets/"
        "payment surfaces or the dashboard build system; the full pytest suite "
        "must stay green; add or update the verifying test if one is missing."
    )


def _await_terminal(rt: Runtime, run_id: str) -> dict[str, Any] | None:
    deadline = time.monotonic() + rt.item_timeout_min * 60
    while time.monotonic() < deadline:
        try:
            detail = rt.api.get_run(run_id)
            status = str((detail.get("run") or {}).get("status") or "")
            if status in TERMINAL_RUN_STATUSES:
                return detail
        except Exception as exc:  # noqa: BLE001 -- transient API blips must not kill the poll
            log(f"poll {run_id}: {exc}")
        time.sleep(rt.poll_seconds)
    rt.api.cancel(run_id)
    return None


def execute_item(pick: Pick, title: str, date: str, policy: Policy, rt: Runtime) -> ItemResult:
    safe_id = _slug(pick.id, limit=40)
    result = ItemResult(pick=pick, title=title, safe_id=safe_id)
    branch = f"backlog/{date}-{safe_id}"
    item_dir = backlog_dir() / f"{date}-{safe_id}"
    clone_dir = item_dir / "repo"
    result.clone_dir = str(clone_dir)
    item_dir.mkdir(parents=True, exist_ok=True)

    # 1. Fresh local clone -- overnight work NEVER touches the live checkout.
    if clone_dir.exists():
        result.note = f"clone dir already exists: {clone_dir}"
        return result
    rc, out = rt.git(["clone", "--branch", "main", str(ROOT), str(clone_dir)], ROOT)
    if rc != 0:
        result.note = f"git clone failed: {out[:200]}"
        return result
    rc, base_sha = rt.git(["rev-parse", "HEAD"], clone_dir)
    if rc != 0:
        result.note = f"rev-parse failed: {base_sha[:200]}"
        return result

    # 2. Dispatch + poll to terminal.
    try:
        run_id = rt.api.dispatch(_compose_brief(pick, clone_dir), str(clone_dir))
    except Exception as exc:  # noqa: BLE001
        result.note = f"dispatch failed: {exc}"
        return result
    log(f"item {safe_id}: dispatched swarm run {run_id} in {clone_dir}")
    detail = _await_terminal(rt, run_id)
    if detail is None:
        result.note = f"run {run_id} timed out after {rt.item_timeout_min} min (cancelled)"
        rt.notify("alert", f"Backlog item failed overnight: {title}", result.note)
        return result
    status = str((detail.get("run") or {}).get("status") or "")

    rc, head_sha = rt.git(["rev-parse", "main"], clone_dir)
    has_commits = rc == 0 and head_sha.split()[0] != base_sha.split()[0]

    def fetch_branch() -> bool:
        frc, fout = rt.git(["fetch", str(clone_dir), f"main:refs/heads/{branch}"], ROOT)
        if frc != 0:
            log(f"item {safe_id}: branch fetch failed: {fout[:200]}")
            return False
        result.branch = branch
        return True

    if status != "completed":
        if has_commits:
            fetch_branch()
        result.note = f"swarm run ended {status}"
        rt.notify("alert", f"Backlog item failed overnight: {title}", result.note)
        return result
    if not has_commits:
        result.note = "run completed but produced no commits"
        rt.notify("alert", f"Backlog item failed overnight: {title}", result.note)
        return result

    # 3. Merge gate: full suite in the clone (main venv, PYTHONPATH=clone).
    green, tail = rt.suite(clone_dir, rt.suite_timeout_min)
    if not green:
        fetch_branch()
        result.note = (
            f"merge gate RED — suite failed in clone; branch left for review. {tail[-300:]}"
        )
        rt.notify("alert", f"Backlog item failed overnight: {title}", result.note)
        return result

    # 4. Two-tier decision.
    rc, diff_out = rt.git(["diff", "--name-only", f"{base_sha.split()[0]}..main"], clone_dir)
    changed_files = (
        [ln for ln in diff_out.splitlines() if ln.strip()] if rc == 0 else ["<diff-failed>"]
    )
    risk_classes, max_attempts = run_detail_gate_inputs(detail)
    tier, reason = classify_merge_tier(
        risk_classes=risk_classes,
        max_attempts_per_task=max_attempts,
        changed_files=changed_files,
        policy=policy,
    )
    if tier == "hold":
        rt.git(
            ["commit", "--allow-empty", "-m", f"HOLD: {reason} (backlog-executor {date})"],
            clone_dir,
        )
        fetch_branch()
        result.outcome = "held-for-morning"
        result.held_reason = reason or ""
        result.note = f"green but held: {reason}"
        return result

    # 5. Auto-merge into open integration batch (preflight: open clean batch).
    batch = _resolve_batch_target(ROOT)
    if batch is None:
        fetch_branch()
        result.outcome = "held-for-morning"
        result.held_reason = NO_BATCH_HELD_REASON
        result.note = f"green but held: {NO_BATCH_HELD_REASON}"
        return result
    if not fetch_branch():
        result.note = "could not fetch branch into live repo"
        rt.notify("alert", f"Backlog item failed overnight: {title}", result.note)
        return result
    target = batch.worktree_path
    rc, merge_out = rt.git(
        [
            "merge",
            "--no-ff",
            "--no-edit",
            "-m",
            f"backlog-executor: merge {safe_id} — {title}\n\n"
            f"Overnight backlog item (green gate). Branch {branch} → {batch.branch}.\n\n"
            "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>",
            branch,
        ],
        target,
    )
    if rc != 0:
        rt.git(["merge", "--abort"], target)
        result.outcome = "held-for-morning"
        result.held_reason = f"merge conflict with {batch.branch}"
        result.note = f"green but held: merge conflict with {batch.branch} ({merge_out[:150]})"
        return result
    rc, merge_sha = rt.git(["rev-parse", "HEAD"], target)
    result.merge_sha = merge_sha.split()[0] if rc == 0 else ""

    # 6. Auto-revert rail: suite in the batch worktree after the merge; red -> revert + stop.
    live_green, live_tail = rt.suite(target, rt.suite_timeout_min)
    if not live_green:
        rt.git(["revert", "-m", "1", "--no-edit", result.merge_sha or "HEAD"], target)
        result.outcome = "reverted"
        result.stop_night = True
        result.note = f"post-merge suite RED — merge {result.merge_sha[:10]} auto-reverted. {live_tail[-300:]}"
        rt.notify(
            "alert",
            f"Backlog merge auto-reverted overnight: {title}",
            result.note,
            {"branch": branch, "merge_sha": result.merge_sha},
        )
        return result

    result.outcome = "merged"
    result.note = f"merged {result.merge_sha[:10]} ({len(changed_files)} files, single attempt)"
    return result


# --------------------------------------------------------------------------
# Final merge pass (Addendum 3: held branches merged by 05:00, [held-tier])
# --------------------------------------------------------------------------


def merge_pass_deadline_state(now: datetime, deadline_hour: int) -> str:
    """'skip' when past the 04:45-style cutoff, 'stop' when past the deadline
    itself, else 'open'. Outside the overnight 00:00->deadline window the
    deadline has passed by definition (manual daytime runs skip the pass)."""
    deadline = now.replace(hour=deadline_hour, minute=0, second=0, microsecond=0)
    if now >= deadline:
        return "stop"
    if now >= deadline - timedelta(minutes=MERGE_PASS_CUTOFF_MARGIN_MIN):
        return "skip"
    return "open"


def final_merge_pass(results: list[ItemResult], date: str, policy: Policy, rt: Runtime) -> None:
    """Merge every held-but-GREEN branch before the 05:00 deadline.

    Per branch: re-verify the CANDIDATE merge by merging live main into the
    item's clone and running the full suite there, then merge --no-ff into
    the open integration batch worktree with a ``[held-tier]`` commit-message
    tag. A red re-verify, a conflict, missing batch, or the deadline leaves
    the branch unmerged (outcome stays held-for-morning, digest carries the
    manual merge command). Mutates ``results`` in place.
    """
    held = [r for r in results if r.outcome == "held-for-morning" and r.branch]
    if not held:
        return
    state = merge_pass_deadline_state(rt.now_fn(), policy.merge_deadline_hour)
    if state != "open":
        log(
            f"final merge pass SKIPPED ({len(held)} held branch(es)): past the "
            f"{policy.merge_deadline_hour:02d}:00 deadline cutoff"
        )
        for r in held:
            r.note = f"{r.note}; final merge pass skipped (deadline)"
        return
    log(
        f"final merge pass: {len(held)} held branch(es), deadline {policy.merge_deadline_hour:02d}:00"
    )
    for r in held:
        if merge_pass_deadline_state(rt.now_fn(), policy.merge_deadline_hour) == "stop":
            log("final merge pass STOPPED at the deadline")
            r.note = f"{r.note}; final merge pass hit the deadline"
            continue
        clone_dir = Path(r.clone_dir) if r.clone_dir else None
        if clone_dir is None or not clone_dir.is_dir():
            r.note = f"{r.note}; no clone dir for re-verify"
            continue
        # Candidate merge in the clone: live main INTO the item's main.
        rc, out = rt.git(["fetch", str(ROOT), "main"], clone_dir)
        if rc != 0:
            r.note = f"{r.note}; re-verify fetch failed"
            continue
        rc, out = rt.git(["merge", "--no-ff", "--no-edit", "FETCH_HEAD"], clone_dir)
        if rc >= 128:
            # Sibling carrier of the merge-gate.sh trial-merge defect (same
            # VALUE: never blame the branch for the instrument's own failure).
            # rc>=128 means git could not run the merge at all (no committer
            # identity, corrupt object, locked index); recording "merge
            # conflict" makes the item's durable note a false statement about
            # a branch git never examined. `out` was already captured here and
            # thrown away -- it is the only evidence of the real fault.
            rt.git(["merge", "--abort"], clone_dir)
            r.note = (
                f"{r.note}; re-verify NOT JUDGED — git merge exited {rc} "
                f"(instrument failure, not a conflict): {out.strip()[:200]}"
            )
            continue
        if rc != 0:
            rt.git(["merge", "--abort"], clone_dir)
            r.note = f"{r.note}; re-verify merge conflict — left unmerged"
            continue
        green, tail = rt.suite(clone_dir, rt.suite_timeout_min)
        if not green:
            r.note = f"{r.note}; re-verify suite RED — left unmerged. {tail[-200:]}"
            rt.notify(
                "alert",
                f"Held backlog branch failed re-verify: {r.title}",
                r.note,
                {"branch": r.branch},
            )
            continue
        # Batch preflight, then the real merge with the [held-tier] tag.
        batch = _resolve_batch_target(ROOT)
        if batch is None:
            # held_reason must state the no-batch refusal (same contract as clean-tier).
            prior = (r.held_reason or "").strip()
            r.held_reason = (
                f"{NO_BATCH_HELD_REASON} (earlier: {prior})"
                if prior and prior != NO_BATCH_HELD_REASON
                else NO_BATCH_HELD_REASON
            )
            r.note = f"{r.note}; {NO_BATCH_HELD_REASON}"
            continue
        target = batch.worktree_path
        rc, merge_out = rt.git(
            [
                "merge",
                "--no-ff",
                "--no-edit",
                "-m",
                f"[held-tier] backlog-executor: merge {r.safe_id} — {r.title}\n\n"
                f"Held reason: {r.held_reason or 'see HOLD commit'}. Re-verified green on the "
                f"candidate merge (clone integration + full suite) before the "
                f"{policy.merge_deadline_hour:02d}:00 deadline. Target: {batch.branch}.\n\n"
                "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>",
                r.branch,
            ],
            target,
        )
        if rc != 0:
            rt.git(["merge", "--abort"], target)
            r.note = f"{r.note}; batch merge conflict at merge pass — left unmerged"
            continue
        rc, sha = rt.git(["rev-parse", "HEAD"], target)
        r.merge_sha = sha.split()[0] if rc == 0 else ""
        r.outcome = "merged-held-tier"
        r.note = f"held-tier merged {r.merge_sha[:10]} (re-verified on candidate merge)"
        append_improvement_log(improvement_entry(r, date))
        log(f"final merge pass: merged {r.branch} as {r.merge_sha[:10]} [held-tier]")


# --------------------------------------------------------------------------
# Night orchestration
# --------------------------------------------------------------------------


def dry_run_enabled() -> bool:
    return os.environ.get("OMNIAGENTOS_BACKLOG_DRY_RUN", "").strip().lower() in {"1", "true", "yes"}


def run_night(rt: Runtime | None = None, *, dry_run: bool | None = None) -> list[ItemResult]:
    backlog = backlog_dir()
    date = datetime.now().strftime("%Y-%m-%d")
    dry = dry_run_enabled() if dry_run is None else dry_run
    backlog.mkdir(parents=True, exist_ok=True)
    log(f"night start date={date} dry_run={dry}")

    prompt_text = PROMPT_PATH.read_text(encoding="utf-8") if PROMPT_PATH.is_file() else ""
    policy, problem = parse_policy(prompt_text)
    if problem:
        log(f"policy: {problem}")

    candidates = collect_candidates()
    log(f"collected {len(candidates)} candidate(s)")
    by_id = {c.id: c for c in candidates}

    workdir = backlog / f"{date}-selection"
    workdir.mkdir(parents=True, exist_ok=True)
    picks = select_picks(candidates, prompt_text, policy, workdir=workdir)
    picks, dropped = enforce_deny_list(picks, policy.deny_list)
    for pick, pattern in dropped:
        log(f"deny-list: dropped pick {pick.id} (matched {pattern!r})")
    picks = picks[: policy.effective_max_items]
    for pick in picks:
        log(f"pick: {pick.id} — {pick.why}")

    if dry:
        digest = render_digest(date, [], dry_run=True)
        pick_lines = [f"- {p.id}: {p.why}" for p in picks] or ["- (no picks)"]
        digest += "## Dry-run picks (nothing dispatched)\n" + "\n".join(pick_lines) + "\n"
        digest_path = backlog / f"digest-{date}.md"
        digest_path.write_text(digest, encoding="utf-8")
        notify(
            "info",
            f"Backlog dry-run: {len(picks)} pick(s) selected, nothing dispatched",
            "\n".join(pick_lines),
            {"digest_path": str(digest_path)},
        )
        log(f"dry-run: {len(picks)} pick(s) logged, ZERO dispatches; digest at {digest_path}")
        return []

    if rt is None:
        rt = Runtime(
            api=SwarmApi(),
            poll_seconds=int(os.environ.get("OMNIAGENTOS_BACKLOG_POLL_SECONDS", "30")),
            item_timeout_min=int(os.environ.get("OMNIAGENTOS_BACKLOG_ITEM_TIMEOUT_MIN", "120")),
            suite_timeout_min=int(os.environ.get("OMNIAGENTOS_BACKLOG_SUITE_TIMEOUT_MIN", "45")),
        )

    results: list[ItemResult] = []
    stopped = False
    for pick in picks:  # sequential, never parallel -- blast-radius control
        candidate = by_id.get(pick.id)
        title = candidate.title if candidate else pick.id
        result = execute_item(pick, title, date, policy, rt)
        results.append(result)
        append_improvement_log(improvement_entry(result, date))
        log(f"item {result.safe_id}: outcome={result.outcome} note={result.note[:200]}")
        if result.stop_night:
            log("stopping the night on first red merge (auto-revert rail)")
            stopped = True
            break

    # Held-tier final merge pass -- never after a red merge stopped the night
    # (an auto-revert means the estate is suspect; held branches wait).
    if stopped:
        log("final merge pass skipped: night stopped on a red merge")
    else:
        final_merge_pass(results, date, policy, rt)

    for result in results:  # TODO ✅ flips for everything that ended merged
        candidate = by_id.get(result.pick.id)
        if (
            result.outcome in {"merged", "merged-held-tier"}
            and candidate
            and candidate.raw_line
            and mark_todo_row_done(TODO_PATH, candidate.raw_line, date)
        ):
            log(f"item {result.safe_id}: TODO row marked ✅")

    digest = render_digest(date, results)
    digest_path = backlog / f"digest-{date}.md"
    digest_path.write_text(digest, encoding="utf-8")
    clean = sum(1 for r in results if r.outcome == "merged")
    held_tier = sum(1 for r in results if r.outcome == "merged-held-tier")
    unmerged = sum(1 for r in results if r.outcome in {"failed", "reverted", "held-for-morning"})
    rt.notify(
        "info",
        f"Overnight backlog digest: {clean} merged, {held_tier} held-tier merged, "
        f"{unmerged} failed/unmerged",
        digest,
        {"digest_path": str(digest_path)},
    )
    log(
        f"night end: {clean} merged-clean, {held_tier} merged-held-tier, "
        f"{unmerged} failed-unmerged; digest at {digest_path}"
    )
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="executor.py", description="Run the guarded nightly backlog executor."
    )
    parser.parse_args(argv)
    backlog = backlog_dir()
    backlog.mkdir(parents=True, exist_ok=True)
    lock_path = backlog / ".executor.lock"
    with lock_path.open("w") as lock_fh:
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            log("another backlog-executor instance holds the lock; exiting")
            return 0
        try:
            run_night()
        except Exception as exc:  # noqa: BLE001 -- a crashed night must still leave a trace
            log(f"NIGHT CRASHED: {exc!r}")
            notify("alert", "Backlog executor crashed overnight", repr(exc)[:500])
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
