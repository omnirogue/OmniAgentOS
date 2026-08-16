"""Install and remove report-only Claude Code Session Bridge hooks."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from omniagentos.path_containment import inode_relative_parts
from omniagentos.sessions.token import _nearest_existing

REPORT_COMMAND = "OMNI_SESSION_HOOK_MODE=report omniagentos-session-hook"

#: Marker env var baked into the attention-routing command so install is
#: idempotent and uninstall can remove ONLY these three entries.
ATTENTION_EVENT_ENV = "OMNI_SESSION_ATTENTION_EVENT"
#: Fire-and-forget local POST; sibling profile hooks use 10-15s, not the 60s default.
ATTENTION_HOOK_TIMEOUT_S = 15
#: Claude Code hook type -> POST /api/session-events/hook ``event`` field.
#: Notification defaults to "notification"; notification_type=permission_request
#: and the dedicated PermissionRequest hook map to "permission_request".
ATTENTION_HOOK_EVENTS: tuple[tuple[str, str], ...] = (
    ("Notification", "notification"),
    ("PermissionRequest", "permission_request"),
    ("Stop", "stop"),
    ("SessionEnd", "session_end"),
)
_PERMISSION_TYPES = frozenset({"permission_request", "permission", "PermissionRequest"})

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BRIDGE_SETTINGS_LEAF = ("sessions", "bridge-settings.json")
#: The historical repo-root-relative bridge-settings file (non-sim default).
_LEGACY_BRIDGE_SETTINGS_PATH: Path = _REPO_ROOT.joinpath("var", *_BRIDGE_SETTINGS_LEAF)


class BridgeSettingsPathError(RuntimeError):
    """The bridge-settings path cannot be resolved safely."""


class SettingsPathError(ValueError):
    """The target is neither a Claude project dir nor a CLAUDE_CONFIG_DIR."""


def _is_legacy_path(candidate: Path, legacy: Path) -> bool | None:
    """Is ``candidate`` the production path ``legacy``? ``None`` means unknown.

    Mirrors ``sessions.token._is_legacy_token_path`` (see its docstring for why a
    string compare is unsound): identity is (ancestor directory by inode) AND (the
    exact remaining path components), so absent leaves never need to exist.
    """
    if _nearest_existing(candidate) is None:
        return None
    anchored = _nearest_existing(legacy)
    if anchored is None:
        return None
    anchor, legacy_parts = anchored
    parts = inode_relative_parts(candidate, anchor)
    if parts is None:
        return False
    return parts == legacy_parts


def _sim_var_root() -> Path | None:
    """The campaign var root under ``OMNIAGENTOS_SIM_MODE=1``, else ``None``.

    Mirrors the fail-closed sim resolution of ``sessions.token.token_path``: only
    the exact value ``"1"`` engages it (strict parsing of other values belongs to
    the future simgate), and a missing or relative var root raises rather than
    falling back to the production bridge-settings path.
    """
    if os.environ.get("OMNIAGENTOS_SIM_MODE") != "1":
        return None
    raw = (os.environ.get("OMNIAGENTOS_VAR_DIR") or os.environ.get("OMNIAGENTOS_VAR") or "").strip()
    if not raw:
        raise BridgeSettingsPathError(
            "OMNIAGENTOS_SIM_MODE=1 requires OMNIAGENTOS_VAR_DIR (or "
            "OMNIAGENTOS_VAR) to be set to an absolute campaign var root; "
            "refusing to fall back to the production bridge-settings path"
        )
    if not Path(raw).is_absolute():
        raise BridgeSettingsPathError(
            "OMNIAGENTOS_SIM_MODE=1 requires OMNIAGENTOS_VAR_DIR (or "
            "OMNIAGENTOS_VAR) to be an absolute path "
            f"(got {raw!r}); refusing to fall back to the production "
            "bridge-settings path"
        )
    return Path(raw)


def _bridge_settings_target() -> Path:
    """The active bridge-settings path, resolved fresh on every call.

    Outside sim mode this is byte-identical to the historical resolution: the
    ``__file__``-derived legacy path (env vars deliberately ignored). Resolution
    raises BEFORE anything is written, so a misconfigured sim campaign can never
    clobber the production file.
    """
    var_root = _sim_var_root()
    if var_root is None:
        return _LEGACY_BRIDGE_SETTINGS_PATH
    candidate = var_root.joinpath(*_BRIDGE_SETTINGS_LEAF)
    # Fail closed: True (it IS production) and None (cannot tell) both refuse.
    if _is_legacy_path(candidate, _LEGACY_BRIDGE_SETTINGS_PATH) is not False:
        raise BridgeSettingsPathError(
            "OMNIAGENTOS_SIM_MODE=1 refuses to use the production "
            "bridge-settings path: OMNIAGENTOS_VAR_DIR / OMNIAGENTOS_VAR "
            f"resolved to the legacy path {_LEGACY_BRIDGE_SETTINGS_PATH}"
        )
    return candidate


def _bridge_command() -> str:
    executable = shutil.which("omniagentos-session-hook")
    if executable:
        return shlex.quote(str(Path(executable).resolve()))
    return f"{shlex.quote(sys.executable)} -m omniagentos.sessions.hook_client"


def _entry(command: str, *, timeout: int | None = None) -> dict[str, Any]:
    hook: dict[str, Any] = {"type": "command", "command": command}
    if timeout is not None:
        hook["timeout"] = timeout
    return {"matcher": ".*", "hooks": [hook]}


def _is_ephemeral_interpreter(path: str) -> bool:
    """Lane worktrees are deleted after merge; never bake their .venv into hooks."""
    normalized = path.replace("\\", "/")
    return "/worktrees/" in normalized


_PROBE_SNIPPET = "from omniagentos.sessions.install import forward_attention_hook"


def _interpreter_reaches_hook(candidate: str) -> bool:
    """The real predicate: can this interpreter import the hook entry point?

    A path-shape heuristic is a proxy that fails silently (a pyenv shim that
    resolves omniagentos to a checkout WITHOUT the hook imports fine but lacks
    the symbol) — probe the import itself. The probe runs from a NEUTRAL cwd:
    Claude Code runs hooks from arbitrary project cwds, so an import that only
    works because of the installer's cwd would bake a hook that fails at hook
    time.
    """
    try:
        probe = subprocess.run(
            [candidate, "-c", _PROBE_SNIPPET],
            capture_output=True,
            timeout=15,
            check=False,
            cwd=os.sep,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return probe.returncode == 0


def _attention_interpreter() -> str:
    """Prefer the Session Bridge console-script env; never bake a worktree venv.

    Every candidate is PROBED for the hook entry point; a candidate that cannot
    import it is refused. No fallback: installing a hook whose interpreter
    cannot run it would be a silent no-op on every turn (measured failure
    class), so an unresolvable environment must refuse loudly instead.
    """
    override = (os.environ.get("OMNI_ATTENTION_PYTHONBIN") or "").strip()
    if override:
        # Explicit operator/test choice: still probed (an override that cannot
        # run the hook must refuse, not silently no-op), but the ephemeral
        # heuristic is skipped — the operator named this interpreter.
        if _interpreter_reaches_hook(override):
            return override
        raise SettingsPathError(
            f"OMNI_ATTENTION_PYTHONBIN={override} cannot import "
            "omniagentos.sessions.install.forward_attention_hook"
        )
    candidates: list[str] = []
    hook = shutil.which("omniagentos-session-hook")
    if hook:
        parent = Path(hook).resolve().parent
        for name in ("python3", "python"):
            sibling = parent / name
            if sibling.is_file():
                candidates.append(str(sibling))
    for name in ("python3", "python"):
        found = shutil.which(name)
        if found:
            candidates.append(found)
    candidates.append(sys.executable)
    for candidate in candidates:
        if _is_ephemeral_interpreter(candidate):
            continue
        if _interpreter_reaches_hook(candidate):
            return candidate
    raise SettingsPathError(
        "no durable interpreter can import "
        "omniagentos.sessions.install.forward_attention_hook; refusing to "
        f"install hooks that would silently no-op (tried: {candidates})"
    )


def _attention_command(payload_event: str) -> str:
    """Command hook that forwards stdin JSON to POST /api/session-events/hook.

    Token injection matches ``hook_client._hook_eval_headers``. The ``-c``
    snippet swallows every exception (including ImportError) so a missing
    venv or deleted worktree never prints a traceback into Claude Code.
    """
    # Failure evidence is stdlib-only and OUTSIDE the omniagentos import: an
    # import failure must land in the ledger, never be invisible. Nothing on
    # any path writes to stdout — PermissionRequest is a decision surface and
    # stdout is parsed as a permission decision.
    snippet = (
        "import json,os,time\n"
        "try:\n"
        " from omniagentos.sessions.install import forward_attention_hook as _f\n"
        " _f()\n"
        "except Exception as _e:\n"
        " try:\n"
        "  _p=os.environ.get('OMNI_ATTENTION_LEDGER') or os.path.expanduser("
        "'~/OmniAgentOS/var/attention-hook-ledger.jsonl')\n"
        "  open(_p,'a').write(json.dumps({'ts':time.time(),"
        "'event':os.environ.get('OMNI_SESSION_ATTENTION_EVENT'),"
        "'outcome':'hook_error','error':str(_e)[:200]})+'\\n')\n"
        " except Exception:\n"
        "  pass"
    )
    return (
        f"{ATTENTION_EVENT_ENV}={payload_event} "
        f"{shlex.quote(_attention_interpreter())} -c {shlex.quote(snippet)}"
    )


def _is_report_entry(entry: Any) -> bool:
    if not isinstance(entry, dict):
        return False
    hooks = entry.get("hooks")
    return isinstance(hooks, list) and any(
        isinstance(hook, dict)
        and isinstance(hook.get("command"), str)
        and "OMNI_SESSION_HOOK_MODE=report" in hook["command"]
        and "omniagentos-session-hook" in hook["command"]
        for hook in hooks
    )


def _is_attention_hook(hook: Any) -> bool:
    return (
        isinstance(hook, dict)
        and isinstance(hook.get("command"), str)
        and ATTENTION_EVENT_ENV in hook["command"]
        and "forward_attention_hook" in hook["command"]
    )


def _is_attention_entry(entry: Any) -> bool:
    if not isinstance(entry, dict):
        return False
    hooks = entry.get("hooks")
    return isinstance(hooks, list) and any(_is_attention_hook(hook) for hook in hooks)


def forward_attention_hook() -> None:
    """Read hook stdin and POST /api/session-events/hook. Never raises.

    Installed as the Notification / Stop / SessionEnd command. Failures
    (API down, bad JSON, missing token) are swallowed so the Claude session
    is never blocked by attention routing.
    """
    event = (os.environ.get(ATTENTION_EVENT_ENV) or "notification").strip() or "notification"
    session_id: str | None = None
    outcome = "ok"
    try:
        payload = json.load(sys.stdin)
    except (OSError, json.JSONDecodeError, ValueError):
        _ledger_append(event, None, "bad_stdin")
        return
    if not isinstance(payload, dict):
        _ledger_append(event, None, "bad_stdin")
        return

    def _opt(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    session_id = _opt(payload.get("session_id"))
    hook_name = str(payload.get("hook_event_name") or "")
    notice = str(payload.get("notification_type") or "")
    if event == "notification" and (notice in _PERMISSION_TYPES or hook_name in _PERMISSION_TYPES):
        event = "permission_request"
    if hook_name == "PermissionRequest":
        event = "permission_request"

    message = _opt(payload.get("message") or payload.get("notification"))
    if message is None and event == "permission_request":
        # PermissionRequest payloads carry {tool_name, tool_input, tool_use_id}
        # and no message — synthesize the reason the operator actually needs.
        tool_name = _opt(payload.get("tool_name"))
        if tool_name:
            try:
                detail = json.dumps(payload.get("tool_input") or {})[:120]
            except (TypeError, ValueError):
                detail = ""
            message = f"permission: {tool_name} {detail}".strip()

    body = {
        "session_id": session_id,
        "session_ref": _opt(payload.get("session_ref")),
        "event": event,
        "cwd": str(payload.get("cwd") or os.getcwd()),
        "message": message,
        "title": _opt(payload.get("title")),
    }
    try:
        from omniagentos.sessions.hook_client import _hook_eval_headers, _post

        _post("/api/session-events/hook", body, _hook_eval_headers())
    except Exception:  # noqa: BLE001 -- never block the Claude session
        outcome = "error"
    _ledger_append(event, session_id, outcome)


def merge_attention_hooks(settings: dict[str, Any]) -> dict[str, Any]:
    """Idempotently merge the three attention hook entries into ``settings``.

    Existing ``hooks`` arrays are preserved; only missing attention entries
    are appended. Operates on the dict in place and returns it.
    """
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("hooks must be a JSON object")
    for hook_type, payload_event in ATTENTION_HOOK_EVENTS:
        entries = hooks.setdefault(hook_type, [])
        if not isinstance(entries, list):
            raise ValueError(f"hooks.{hook_type} must be a JSON array")
        if not any(_is_attention_entry(entry) for entry in entries):
            entries.append(
                _entry(_attention_command(payload_event), timeout=ATTENTION_HOOK_TIMEOUT_S)
            )
    return settings


def strip_attention_hooks(settings: dict[str, Any]) -> dict[str, Any]:
    """Remove only our sub-hooks; keep the matcher entry if anything remains.

    A compound matcher that also carries a third-party hook (e.g.
    estate-host-guard) must survive uninstall. The whole entry is dropped
    only when its ``hooks`` array is empty after removal.
    """
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return settings
    for hook_type, _payload_event in ATTENTION_HOOK_EVENTS:
        entries = hooks.get(hook_type)
        if not isinstance(entries, list):
            continue
        retained: list[Any] = []
        for entry in entries:
            if not isinstance(entry, dict):
                retained.append(entry)
                continue
            subhooks = entry.get("hooks")
            if not isinstance(subhooks, list):
                retained.append(entry)
                continue
            kept = [hook for hook in subhooks if not _is_attention_hook(hook)]
            if not kept:
                continue
            if kept != subhooks:
                entry = dict(entry)
                entry["hooks"] = kept
            retained.append(entry)
        if retained:
            hooks[hook_type] = retained
        else:
            hooks.pop(hook_type, None)
    if not hooks:
        settings.pop("hooks", None)
    return settings


def install_attention_hooks(project: str | Path) -> Path:
    """Add Notification/Stop/SessionEnd attention hooks to a project's settings.json."""
    path = settings_path(project)
    settings = _load_settings(path)
    merge_attention_hooks(settings)
    _save_settings(path, settings)
    return path


def uninstall_attention_hooks(project: str | Path) -> Path:
    """Remove only the attention-routing hook entries from a project's settings.json."""
    path = settings_path(project)
    settings = _load_settings(path)
    strip_attention_hooks(settings)
    _save_settings(path, settings)
    return path


def _load_settings(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as settings_file:
        value = json.load(settings_file)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _attention_backup_path(path: Path) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%d")
    return path.with_name(f"{path.name}.bak-attention-{stamp}")


def _save_settings(path: Path, settings: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(settings, indent=2, sort_keys=True) + "\n"
    if path.exists():
        # Write-once per day: the first save of the day snapshots the pristine
        # pre-mutation state; later saves must not overwrite that evidence.
        backup = _attention_backup_path(path)
        if not backup.exists():
            backup.write_bytes(path.read_bytes())
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def settings_path(project: str | Path, *, allow_bootstrap: bool = False) -> Path:
    """Resolve a Claude Code settings.json for a project dir or CLAUDE_CONFIG_DIR.

    Config-dir (profile root): ``settings.json`` already at the given path, or
    the directory basename matches ``.claude*`` (e.g. ``~/.claude-account-5``).
    Project layout: ``<dir>/.claude/settings.json``. Anything else is refused.
    """
    raw = Path(project).expanduser()
    try:
        path = raw.resolve()
    except OSError as exc:
        raise SettingsPathError(f"refused settings path {raw}: {exc}") from exc
    if path.is_file():
        if path.name == "settings.json":
            return path
        raise SettingsPathError(f"refused settings path {path}: not a Claude settings.json")
    root_settings = path / "settings.json"
    nested = path / ".claude" / "settings.json"
    name = path.name
    if root_settings.is_file() or name.startswith(".claude"):
        return root_settings
    if nested.is_file() or (path / ".claude").is_dir():
        return nested
    if allow_bootstrap:
        # Explicit opt-in for the project-oriented Session Bridge installer,
        # which legitimately bootstraps .claude/ into a fresh (possibly not
        # yet created) project dir — its historical behavior. The attention
        # rollout paths never pass this: a typo'd profile must refuse, not
        # get a decoy write.
        return nested
    raise SettingsPathError(
        f"refused settings path {path}: neither a Claude project "
        f"(<dir>/.claude/settings.json) nor a CLAUDE_CONFIG_DIR "
        f"(settings.json at the profile root)"
    )


def _ledger_path() -> Path:
    override = (os.environ.get("OMNI_ATTENTION_LEDGER") or "").strip()
    if override:
        return Path(override)
    return _REPO_ROOT / "var" / "attention-hook-ledger.jsonl"


def _ledger_append(event: str, session_id: str | None, outcome: str) -> None:
    """JSONL evidence line, same discipline as notify-owner.sh's ledger."""
    try:
        from omniagentos.contracts import utc_now_iso

        path = _ledger_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            {
                "ts": utc_now_iso(),
                "event": event,
                "session_id": session_id,
                "outcome": outcome,
            },
            separators=(",", ":"),
        )
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except Exception:  # noqa: BLE001 -- evidence must never break the hook
        pass


def _guard_commands(settings: dict[str, Any]) -> list[str]:
    commands: list[str] = []
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return commands
    for entries in hooks.values():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            for hook in entry.get("hooks") or []:
                if (
                    isinstance(hook, dict)
                    and isinstance(hook.get("command"), str)
                    and not _is_attention_hook(hook)
                ):
                    commands.append(hook["command"])
    return commands


def preflight_attention_rollout(
    profile: str | Path,
    *,
    hook_invoker: Any | None = None,
) -> dict[str, Any]:
    """Assert a fixture profile is safe to install. Never call on a live profile.

    Checks settings.json exists and is valid JSON, existing guard commands
    survive a simulated merge, then optionally runs ``hook_invoker`` (a
    synthetic hook POST) and records that it returned a truthy row update.
    """
    path = settings_path(profile)
    if not path.is_file():
        raise SettingsPathError(f"preflight: settings.json missing at {path}")
    before = _load_settings(path)
    guards_before = _guard_commands(before)
    simulated = json.loads(json.dumps(before))
    merge_attention_hooks(simulated)
    guards_after = _guard_commands(simulated)
    if guards_before != guards_after:
        raise SettingsPathError(f"preflight: simulated install would drop guards at {path}")
    invoked = False
    if hook_invoker is not None:
        result = hook_invoker()
        if not result:
            raise SettingsPathError(f"preflight: synthetic hook did not update a row at {path}")
        invoked = True
    return {
        "path": str(path),
        "guards_before": guards_before,
        "guards_after": guards_after,
        "guards_preserved": True,
        "synthetic_invoked": invoked,
    }


def install(project: str | Path) -> Path:
    path = settings_path(project, allow_bootstrap=True)
    settings = _load_settings(path)
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError(f"{path} hooks must be a JSON object")
    for event in ("PostToolUse", "Stop"):
        entries = hooks.setdefault(event, [])
        if not isinstance(entries, list):
            raise ValueError(f"{path} hooks.{event} must be a JSON array")
        if not any(_is_report_entry(entry) for entry in entries):
            entries.append(_entry(REPORT_COMMAND))
    _save_settings(path, settings)
    return path


def uninstall(project: str | Path) -> Path:
    path = settings_path(project, allow_bootstrap=True)
    settings = _load_settings(path)
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return path
    for event in ("PostToolUse", "Stop"):
        entries = hooks.get(event)
        if isinstance(entries, list):
            retained = [entry for entry in entries if not _is_report_entry(entry)]
            if retained:
                hooks[event] = retained
            else:
                hooks.pop(event, None)
    if not hooks:
        settings.pop("hooks", None)
    _save_settings(path, settings)
    return path


def bridge_settings_path() -> Path:
    """Write and return supervisor-owned policy + live-steering hooks."""
    path = _bridge_settings_target()
    _save_settings(
        path,
        {
            "hooks": {
                "PreToolUse": [_entry(_bridge_command())],
                "PostToolUse": [_entry(_bridge_command())],
            }
        },
    )
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="Install report-only OmniAgentOS Claude hooks")
    parser.add_argument("--project", required=True, help="Claude project directory")
    parser.add_argument(
        "--uninstall", action="store_true", help="Remove only OmniAgentOS hook blocks"
    )
    parser.add_argument(
        "--attention",
        action="store_true",
        help="Install attention-routing Notification/Stop/SessionEnd hooks",
    )
    parser.add_argument(
        "--uninstall-attention",
        action="store_true",
        help="Remove only attention-routing hook entries",
    )
    parser.add_argument(
        "--forward",
        action="store_true",
        help="Forward a Claude Code hook payload from stdin to the attention API "
        "(the installed hook command's entry point)",
    )
    parser.add_argument(
        "--preflight-attention",
        action="store_true",
        help="Validate a profile before/after an attention-hook rollout",
    )
    args = parser.parse_args()
    if args.forward:
        forward_attention_hook()
        return
    try:
        if args.preflight_attention:
            report = preflight_attention_rollout(args.project)
            print(json.dumps(report, indent=2, default=str))
            return
        if args.uninstall_attention:
            path = uninstall_attention_hooks(args.project)
        elif args.attention:
            path = install_attention_hooks(args.project)
        elif args.uninstall:
            path = uninstall(args.project)
        else:
            path = install(args.project)
    except SettingsPathError as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(2) from exc
    print(path)


if __name__ == "__main__":
    main()
