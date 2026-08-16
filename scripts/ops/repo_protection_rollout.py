#!/usr/bin/env python3
"""Idempotent GitHub merge-protection rollout: Alice reviews every PR.

Reads ``configs/company_repos.yaml`` and drives every listed repo to the
ratified scheme (the operator's standing goal 2026-08-13, mirroring the initech
ruleset the operator ratified):

  (a) alice-dev has at least push access (collaborator invite if absent),
  (b) ``.github/CODEOWNERS`` on the default branch carries a ``* @alice-dev``
      default rule (created or appended, existing lines never removed),
  (c) an active ruleset "Require Alice review on <default-branch>" targeting
      ``~DEFAULT_BRANCH`` with pull_request (1 approval, code-owner review,
      dismiss-stale), non_fast_forward and deletion rules, bypassed only by
      the repository-admin role (actor_id 5, always).

Authorization boundary: collaborator invites for alice-dev, CODEOWNERS file
commits, and rulesets — nothing else. The script never creates/deletes/
transfers/archives repos, never removes protections or collaborators.

Everything goes through ``gh api`` (subprocess); no deps beyond PyYAML.
Default mode is a read-only plan (--dry-run); --apply mutates. Every run
writes per-repo receipts (before-state, actions, after-state) to
``/Users/youruser/Work/Ops/repo-protection-receipts-<date>.json``
(dry runs get a ``-dryrun`` suffix so they never clobber apply receipts).
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = REPO_ROOT / "configs" / "company_repos.yaml"
DEFAULT_RECEIPTS_DIR = Path("/Users/youruser/Work/Ops")

REVIEWER = "alice-dev"
CODEOWNERS_PATH = ".github/CODEOWNERS"
CODEOWNERS_COMMIT_MESSAGE = "chore(governance): route reviews to Alice"
CODEOWNERS_DEFAULT_RULE = f"* @{REVIEWER}"
RULESET_NAME_TEMPLATE = "Require Alice review on {branch}"

# Never touched in ANY way (no reads, no writes): already compliant, and it is
# the landing-train serving repo — blast radius outweighs any benefit.
EXCLUDED_REPOS = {"Globex/OmniAgentOS"}

#: the operator's ruling 2026-08-13 (mid-rollout): merge protection applies to the
#: Globex org ONLY. The first estate-wide apply was rolled back on the
#: personal repos the same day (~/.omniagentos/ops/repo-protection-rollback-2026-08-13.json).
#: Owners outside this set are skipped unless named via --only (operator override).
PROTECTION_SCOPE_OWNERS = {"Globex"}

# Repos where the operator's brief scopes the rollout to a subset of the scheme.
# Anything not listed gets the full action set. Values are the ONLY actions
# the script may plan/apply there (state is still read and receipted).
ACTION_OVERRIDES: dict[str, frozenset[str]] = {
    # classic protection + CODEOWNERS already exist; Alice just lacks access
    "Globex/OmniAgentOS-sandbox": frozenset({"invite"}),
    # ratified 'Protect critical paths' ruleset + CODEOWNERS + Alice admin exist;
    # only the pull_request ruleset is missing
    "Globex/initech": frozenset({"ruleset"}),
    # classic 1-review protection + Alice write exist
    "example-org/ThreeLoops": frozenset({"codeowners", "ruleset"}),
}
ALL_ACTIONS = frozenset({"invite", "codeowners", "ruleset"})

PULL_REQUEST_PARAMETERS = {
    "required_approving_review_count": 1,
    "require_code_owner_review": True,
    "dismiss_stale_reviews_on_push": True,
    "required_review_thread_resolution": False,
    "require_last_push_approval": False,
}


class GhError(RuntimeError):
    def __init__(self, path: str, status: int, body: Any):
        super().__init__(f"gh api {path} -> HTTP {status}: {body}")
        self.status = status
        self.body = body


def gh_api(
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    ok_missing: bool = False,
) -> tuple[int, Any]:
    """Run ``gh api`` returning (http_status, parsed_body).

    ``ok_missing=True`` turns a 404 into ``(404, None)`` instead of raising.
    """
    cmd = ["gh", "api", "-i", "-X", method, path]
    stdin = None
    if payload is not None:
        cmd += ["--input", "-"]
        stdin = json.dumps(payload)
    proc = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=120)
    out = proc.stdout
    # -i prepends the response headers; status line looks like "HTTP/2.0 200 OK"
    status = 0
    body: Any = None
    if out.startswith("HTTP/"):
        head, _, rest = out.partition("\r\n\r\n")
        if not rest:
            head, _, rest = out.partition("\n\n")
        m = re.match(r"HTTP/[\d.]+ (\d{3})", head)
        if m:
            status = int(m.group(1))
        rest = rest.strip()
        if rest:
            try:
                body = json.loads(rest)
            except json.JSONDecodeError:
                body = rest
    if status == 0:
        raise GhError(path, 0, proc.stderr.strip() or out[:500])
    if status == 404 and ok_missing:
        return status, None
    if status >= 400:
        raise GhError(path, status, body)
    return status, body


def gh_api_list(path: str) -> list[Any]:
    """GET with --paginate for list endpoints."""
    proc = subprocess.run(
        ["gh", "api", "--paginate", "--slurp", path],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        raise GhError(path, -1, proc.stderr.strip()[:500])
    pages = json.loads(proc.stdout) if proc.stdout.strip() else []
    items: list[Any] = []
    for page in pages:
        items.extend(page if isinstance(page, list) else [page])
    return items


# --- state reads -----------------------------------------------------------


def read_repo_state(full: str) -> dict[str, Any]:
    owner, name = full.split("/", 1)
    _, repo = gh_api(f"repos/{owner}/{name}")
    state: dict[str, Any] = {
        "default_branch": repo.get("default_branch"),
        "private": repo.get("private"),
        "archived": repo.get("archived"),
    }
    branches = gh_api_list(f"repos/{owner}/{name}/branches?per_page=10")
    state["empty"] = len(branches) == 0
    if state["empty"]:
        return state

    # reviewer access: direct/team collaborator, else pending invite
    status, _ = gh_api(f"repos/{owner}/{name}/collaborators/{REVIEWER}", ok_missing=True)
    state["reviewer_is_collaborator"] = status == 204
    if state["reviewer_is_collaborator"]:
        _, perm = gh_api(f"repos/{owner}/{name}/collaborators/{REVIEWER}/permission")
        state["reviewer_permission"] = perm.get("permission")
    invites = gh_api_list(f"repos/{owner}/{name}/invitations")
    pending = [inv for inv in invites if (inv.get("invitee") or {}).get("login") == REVIEWER]
    state["reviewer_invite_pending"] = bool(pending)

    # CODEOWNERS on the default branch
    branch = state["default_branch"]
    status, contents = gh_api(
        f"repos/{owner}/{name}/contents/{CODEOWNERS_PATH}?ref={branch}",
        ok_missing=True,
    )
    if status == 404:
        state["codeowners"] = {"exists": False}
    else:
        text = base64.b64decode(contents["content"]).decode("utf-8")
        state["codeowners"] = {
            "exists": True,
            "sha": contents["sha"],
            "has_default_alice_rule": _has_default_alice_rule(text),
            "content": text,
        }

    # rulesets
    rulesets = gh_api_list(f"repos/{owner}/{name}/rulesets?per_page=100")
    wanted = RULESET_NAME_TEMPLATE.format(branch=branch)
    state["ruleset_name_wanted"] = wanted
    match = next((r for r in rulesets if r.get("name") == wanted), None)
    state["existing_ruleset_names"] = [r.get("name") for r in rulesets]
    if match is None:
        state["ruleset"] = {"exists": False}
    else:
        _, detail = gh_api(f"repos/{owner}/{name}/rulesets/{match['id']}")
        state["ruleset"] = {
            "exists": True,
            "id": match["id"],
            "enforcement": detail.get("enforcement"),
            "rule_types": sorted(r["type"] for r in detail.get("rules", [])),
            "compliant": _ruleset_compliant(detail),
        }
    return state


def _has_default_alice_rule(text: str) -> bool:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("*") and not stripped.startswith("**"):
            tokens = stripped.split()
            if tokens[0] == "*" and f"@{REVIEWER}" in tokens[1:]:
                return True
    return False


def _ruleset_compliant(detail: dict[str, Any]) -> bool:
    if detail.get("enforcement") != "active":
        return False
    rules = {r["type"]: r for r in detail.get("rules", [])}
    if not {"pull_request", "non_fast_forward", "deletion"} <= rules.keys():
        return False
    params = rules["pull_request"].get("parameters", {})
    return all(params.get(key) == value for key, value in PULL_REQUEST_PARAMETERS.items())


# --- desired-state payloads ------------------------------------------------


def ruleset_payload(branch: str) -> dict[str, Any]:
    return {
        "name": RULESET_NAME_TEMPLATE.format(branch=branch),
        "target": "branch",
        "enforcement": "active",
        "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
        "rules": [
            {"type": "pull_request", "parameters": dict(PULL_REQUEST_PARAMETERS)},
            {"type": "non_fast_forward"},
            {"type": "deletion"},
        ],
        # repo-admin role bypass keeps the operator/fleet landing flow working —
        # mirrors the ratified initech scheme
        "bypass_actors": [{"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "always"}],
    }


# --- planning + applying ---------------------------------------------------


def plan_actions(state: dict[str, Any], allowed: frozenset[str]) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    sufficient = {"admin", "maintain", "write", "push"}
    if "invite" in allowed:
        if not state["reviewer_is_collaborator"] and not state["reviewer_invite_pending"]:
            actions.append({"action": "invite", "detail": f"invite {REVIEWER} with push"})
        elif (
            state["reviewer_is_collaborator"]
            and str(state.get("reviewer_permission") or "").lower() not in sufficient
        ):
            # Read-only access cannot satisfy code-owner review — bump to push.
            actions.append({"action": "invite", "detail": f"raise {REVIEWER} permission to push"})
    if "codeowners" in allowed:
        co = state["codeowners"]
        if not co["exists"]:
            actions.append({"action": "codeowners", "detail": "create CODEOWNERS"})
        elif not co["has_default_alice_rule"]:
            actions.append({"action": "codeowners", "detail": "append default rule to CODEOWNERS"})
    if "ruleset" in allowed:
        rs = state["ruleset"]
        if not rs["exists"]:
            actions.append({"action": "ruleset", "detail": "create ruleset"})
        elif not rs["compliant"]:
            actions.append({"action": "ruleset", "detail": "update non-compliant ruleset"})
    return actions


def apply_action(full: str, action: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    owner, name = full.split("/", 1)
    branch = state["default_branch"]
    kind = action["action"]
    if kind == "invite":
        status, body = gh_api(
            f"repos/{owner}/{name}/collaborators/{REVIEWER}",
            method="PUT",
            payload={"permission": "push"},
        )
        return {
            "http_status": status,
            "invitation_id": (body or {}).get("id") if isinstance(body, dict) else None,
        }
    if kind == "codeowners":
        co = state["codeowners"]
        if co["exists"]:
            new_text = co["content"]
            if not new_text.endswith("\n"):
                new_text += "\n"
            new_text += CODEOWNERS_DEFAULT_RULE + "\n"
        else:
            new_text = f"# Default: every change is reviewed by Alice.\n{CODEOWNERS_DEFAULT_RULE}\n"
        payload: dict[str, Any] = {
            "message": CODEOWNERS_COMMIT_MESSAGE,
            "content": base64.b64encode(new_text.encode("utf-8")).decode("ascii"),
            "branch": branch,
        }
        if co["exists"]:
            payload["sha"] = co["sha"]
        status, body = gh_api(
            f"repos/{owner}/{name}/contents/{CODEOWNERS_PATH}",
            method="PUT",
            payload=payload,
        )
        return {
            "http_status": status,
            "commit": ((body or {}).get("commit") or {}).get("sha"),
        }
    if kind == "ruleset":
        rs = state["ruleset"]
        payload = ruleset_payload(branch)
        if rs["exists"]:
            status, body = gh_api(
                f"repos/{owner}/{name}/rulesets/{rs['id']}",
                method="PUT",
                payload=payload,
            )
        else:
            status, body = gh_api(f"repos/{owner}/{name}/rulesets", method="POST", payload=payload)
        return {"http_status": status, "ruleset_id": (body or {}).get("id")}
    raise ValueError(f"unknown action {kind!r}")


def summarize_state(state: dict[str, Any]) -> dict[str, Any]:
    """Receipt-sized view (drops raw CODEOWNERS content)."""
    out = dict(state)
    co = out.get("codeowners")
    if isinstance(co, dict):
        out["codeowners"] = {k: v for k, v in co.items() if k != "content"}
    return out


def process_repo(full: str, company: str, apply: bool) -> dict[str, Any]:
    receipt: dict[str, Any] = {"repo": full, "company": company}
    if full in EXCLUDED_REPOS:
        receipt["status"] = "skipped_excluded"
        receipt["reason"] = "already compliant; serving repo — never touched"
        return receipt
    allowed = ACTION_OVERRIDES.get(full, ALL_ACTIONS)
    receipt["allowed_actions"] = sorted(allowed)
    try:
        state = read_repo_state(full)
    except GhError as exc:
        receipt["status"] = "error"
        receipt["error"] = str(exc)
        return receipt
    receipt["before"] = summarize_state(state)
    if state.get("archived"):
        receipt["status"] = "skipped_archived"
        return receipt
    if state["empty"]:
        receipt["status"] = "skipped_empty"
        receipt["reason"] = "no branches — unprotectable until first push"
        return receipt

    actions = plan_actions(state, allowed)
    receipt["planned_actions"] = actions
    if not actions:
        receipt["status"] = "compliant"
        return receipt
    if not apply:
        receipt["status"] = "would_change"
        return receipt

    applied: list[dict[str, Any]] = []
    errors: list[str] = []
    for action in actions:
        entry = dict(action)
        try:
            entry["result"] = apply_action(full, action, state)
            entry["ok"] = True
        except GhError as exc:
            entry["ok"] = False
            entry["error"] = str(exc)
            errors.append(f"{action['action']}: {exc}")
        applied.append(entry)
    receipt["applied"] = applied
    try:
        receipt["after"] = summarize_state(read_repo_state(full))
    except GhError as exc:
        errors.append(f"after-state read: {exc}")
    if errors:
        receipt["status"] = "error"
        receipt["errors"] = errors
    else:
        receipt["status"] = "updated"
    return receipt


# --- config + main ---------------------------------------------------------


def load_repos(config_path: Path) -> list[tuple[str, str]]:
    """Return (company_slug, owner/repo) pairs in config order."""
    config = yaml.safe_load(config_path.read_text())
    pairs: list[tuple[str, str]] = []
    for slug, spec in (config.get("companies") or {}).items():
        for entry in spec.get("github") or []:
            full = entry if isinstance(entry, str) else entry["repo"]
            pairs.append((slug, full))
    return pairs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="mutate GitHub (default is a read-only dry-run plan)",
    )
    parser.add_argument(
        "--only", action="append", metavar="OWNER/REPO", help="limit to specific repos (repeatable)"
    )
    parser.add_argument("--receipts-dir", type=Path, default=DEFAULT_RECEIPTS_DIR)
    args = parser.parse_args(argv)

    pairs = load_repos(args.config)
    if args.only:
        wanted = set(args.only)
        pairs = [p for p in pairs if p[1] in wanted]
        missing = wanted - {full for _, full in pairs}
        if missing:
            print(f"unknown repos (not in config): {sorted(missing)}", file=sys.stderr)
            return 2
    else:
        in_scope = [p for p in pairs if p[1].split("/", 1)[0] in PROTECTION_SCOPE_OWNERS]
        skipped = len(pairs) - len(in_scope)
        if skipped:
            print(
                f"scope: Globex org only (the operator 2026-08-13) — "
                f"{skipped} out-of-scope repo(s) skipped; use --only to override",
                flush=True,
            )
        pairs = in_scope

    mode = "apply" if args.apply else "dry-run"
    receipts: list[dict[str, Any]] = []
    for company, full in pairs:
        print(f"[{mode}] {full} ({company}) ...", flush=True)
        receipt = process_repo(full, company, apply=args.apply)
        print(
            f"  -> {receipt['status']}"
            + (
                f" ({len(receipt.get('planned_actions', []))} action(s))"
                if receipt.get("planned_actions")
                else ""
            )
        )
        receipts.append(receipt)

    now = datetime.now(UTC)
    suffix = "" if args.apply else "-dryrun"
    out_path = (
        args.receipts_dir / f"repo-protection-receipts-{now.strftime('%Y-%m-%d')}{suffix}.json"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "generated_at": now.isoformat(),
        "mode": mode,
        "reviewer": REVIEWER,
        "config": str(args.config),
        "receipts": receipts,
    }
    out_path.write_text(json.dumps(document, indent=2) + "\n")
    print(f"receipts -> {out_path}")

    counts: dict[str, int] = {}
    for receipt in receipts:
        counts[receipt["status"]] = counts.get(receipt["status"], 0) + 1
    print("summary:", ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return 1 if counts.get("error") else 0


if __name__ == "__main__":
    sys.exit(main())
