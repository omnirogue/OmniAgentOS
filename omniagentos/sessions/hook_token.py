"""Per-session, narrowly-scoped credential for the PreToolUse hook (AC-policy hook-auth).

WHY this exists: a bridge session runs inside a macOS ``sandbox-exec`` confinement
(``runner.sandbox``) that denies reads of ``var/secrets`` (the control-plane session
token every OTHER mutating route requires). The hook subprocess (``hook_client.py``)
shares that exact confinement with the session's own Bash/Read tools, so it can never
read that token either -- it fails closed before it can even POST to hook-eval, and the
session parks in ``awaiting_approval`` forever with zero approval rows ever created.

The fix is NOT to make the control-plane token readable from the sandbox (that would
let the agent self-grant scope and author runs -- the exact Round-7 escape
``require_session_token`` closed). Instead each session gets its OWN throwaway
credential, minted here right before launch, that authorizes EXACTLY ONE thing:
``POST /api/sessions/hook-eval`` for that session's own row (see
``api.routes.sessions._hook_eval_authorized``). It cannot reach any other mutating
route -- those still require the real control-plane token, checked at the app level
(``api.main.require_session_token`` / ``_PUBLIC_MUTATION_PATHS``) -- and it changes
nothing about WHAT hook-eval decides: money/delete still always escalates to a human,
regardless of which credential authenticated the call.

Storage is a single file per session under ``var/sessions/hook-tokens/`` (deliberately
NOT ``var/secrets`` -- a different, narrower trust tier). It is denied-by-default in
EVERY sandbox profile like a secret dir (``runner.sandbox.hook_token_read_deny_root``)
and re-opened as a single literal ONLY in the profile of the session it belongs to
(``runner.sandbox.session_hook_token_read_allow``), so a sibling session sharing the
same sandbox mechanism cannot read a DIFFERENT session's credential even though reads
are otherwise allowed by default. No new DB column/migration is needed: both the
(unsandboxed) supervisor that mints it and the (unsandboxed) API process that verifies
it can read this file directly with normal OS permissions.

It is fine that a session's OWN Bash/Read tools could in principle also reach this
file (they share hook_client's sandbox) -- that grants nothing beyond what the hook
already does automatically for every real tool call, and it can never widen past its
own session's hook-eval decision. That is the entire safety argument, and it is
narrower on every axis than the control-plane token this replaces for this one route.
"""

from __future__ import annotations

import hmac
import os
import secrets as _secrets
from pathlib import Path

from omniagentos.path_containment import inode_relative_parts
from omniagentos.sessions.token import _nearest_existing

_REPO_ROOT = Path(__file__).resolve().parents[2]
_HOOK_TOKENS_LEAF = ("sessions", "hook-tokens")
#: The historical repo-root-relative store. It is BOTH the non-sim default AND
#: the sentinel that means "HOOK_TOKENS_ROOT has not been overridden".
_LEGACY_HOOK_TOKENS_ROOT: Path = _REPO_ROOT.joinpath("var", *_HOOK_TOKENS_LEAF)

# One directory, one file per LIVE session. Mirrors sessions/token.py's TOKEN_PATH
# pattern (repo-root-relative default, monkeypatchable in tests, resolved per call
# by _hook_tokens_root()) but is a SEPARATE tree from var/secrets on purpose -- see
# module docstring.
HOOK_TOKENS_ROOT: Path = _LEGACY_HOOK_TOKENS_ROOT


class HookTokenPathError(RuntimeError):
    """The hook-token store path cannot be resolved safely."""


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
    falling back to the production store.
    """
    if os.environ.get("OMNIAGENTOS_SIM_MODE") != "1":
        return None
    raw = (os.environ.get("OMNIAGENTOS_VAR_DIR") or os.environ.get("OMNIAGENTOS_VAR") or "").strip()
    if not raw:
        raise HookTokenPathError(
            "OMNIAGENTOS_SIM_MODE=1 requires OMNIAGENTOS_VAR_DIR (or "
            "OMNIAGENTOS_VAR) to be set to an absolute campaign var root; "
            "refusing to fall back to the production hook-token store"
        )
    if not Path(raw).is_absolute():
        raise HookTokenPathError(
            "OMNIAGENTOS_SIM_MODE=1 requires OMNIAGENTOS_VAR_DIR (or "
            "OMNIAGENTOS_VAR) to be an absolute path "
            f"(got {raw!r}); refusing to fall back to the production "
            "hook-token store"
        )
    return Path(raw)


def _hook_tokens_root() -> Path:
    """The active hook-token store root, resolved fresh on every call.

    Outside sim mode this is byte-identical to the historical resolution: the
    ``__file__``-derived ``HOOK_TOKENS_ROOT`` (env vars deliberately ignored).
    """
    current = HOOK_TOKENS_ROOT
    if Path(current) != _LEGACY_HOOK_TOKENS_ROOT:
        # Explicit in-process override (tests, deliberate callers). Honour it
        # with no further checks -- even under sim mode.
        return Path(current)
    var_root = _sim_var_root()
    if var_root is None:
        return _LEGACY_HOOK_TOKENS_ROOT
    candidate = var_root.joinpath(*_HOOK_TOKENS_LEAF)
    # Fail closed: True (it IS production) and None (cannot tell) both refuse.
    if _is_legacy_path(candidate, _LEGACY_HOOK_TOKENS_ROOT) is not False:
        raise HookTokenPathError(
            "OMNIAGENTOS_SIM_MODE=1 refuses to use the production hook-token "
            "store: OMNIAGENTOS_VAR_DIR / OMNIAGENTOS_VAR resolved to the "
            f"legacy path {_LEGACY_HOOK_TOKENS_ROOT}"
        )
    return candidate


def _safe_name(session_id: str) -> str:
    """Defang ``session_id`` before it becomes a filename component.

    Every real id is a ``contracts.new_id('ses')`` hex slug (alnum + '_'), but this
    still refuses to let a malformed/attacker-supplied id resolve outside
    ``HOOK_TOKENS_ROOT`` via ``/`` or ``..`` -- fail closed on anything else."""
    cleaned = "".join(ch for ch in session_id if ch.isalnum() or ch in "-_")
    if not cleaned:
        raise ValueError("session_id must contain at least one safe character")
    return cleaned


def hook_token_path(session_id: str) -> Path:
    """Path to ``session_id``'s current hook-eval credential."""
    return _hook_tokens_root() / f"{_safe_name(session_id)}.token"


def issue_hook_token(session_id: str) -> str:
    """Mint (or rotate) ``session_id``'s hook-eval credential; persist it 0600.

    Called ONLY by the (unsandboxed) supervisor immediately before it launches the
    sandboxed session process -- for both the initial spawn and every resume -- so
    the file exists before the hook can ever be invoked. Overwrites any prior
    value: one live credential per session at a time, freshly rotated on resume.
    """
    path = hook_token_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    token = _secrets.token_urlsafe(32)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(token)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return token


def read_hook_token(session_id: str) -> str | None:
    """Read ``session_id``'s current hook token, or ``None`` if missing/empty.

    Used by the hook client (inside the sandbox, where ONLY its own session's file
    is re-opened for reads) and by hook-eval's verification (outside the sandbox,
    where normal OS permissions already allow reading any session's file)."""
    try:
        value = hook_token_path(session_id).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def verify_hook_token(session_id: str, presented: str | None) -> bool:
    """Constant-time check that ``presented`` is the CURRENT hook token for ``session_id``.

    Reads straight off disk -- the API process is unsandboxed, control-plane
    trusted code, so this needs no new DB column. This proves ONLY that the caller
    already holds the exact file minted for THIS session; it says nothing about,
    and grants nothing for, any other session or any other route."""
    if not presented or not session_id:
        return False
    expected = read_hook_token(session_id)
    if not expected:
        return False
    return hmac.compare_digest(expected, presented)


def revoke_hook_token(session_id: str) -> None:
    """Best-effort delete on session terminalization; never raises.

    Shrinks the residual on-disk credential surface once a session is truly done
    (completed/failed/cancelled/killed). Idempotent: a missing file is a no-op."""
    try:
        hook_token_path(session_id).unlink(missing_ok=True)
    except OSError:
        pass


__all__ = [
    "HOOK_TOKENS_ROOT",
    "HookTokenPathError",
    "hook_token_path",
    "issue_hook_token",
    "read_hook_token",
    "revoke_hook_token",
    "verify_hook_token",
]
