"""Local caller token for the session-hook API surface.

Resolution order for the active token path (see :func:`token_path`):

1. An in-process assignment to module-level ``TOKEN_PATH`` that differs from the
   historical repo-root default wins immediately (test overrides, deliberate
   callers). This is intentional even under ``OMNIAGENTOS_SIM_MODE=1``.
2. Otherwise ``OMNIAGENTOS_VAR_DIR``, then ``OMNIAGENTOS_VAR``, supply the var
   root; the token lives at ``<var root>/secrets/sessions-token``.
3. ``OMNIAGENTOS_SIM_MODE`` itself is parsed strictly via
   :func:`omniagentos.simgate._parse_strict_sim_mode` (unset/empty is
   production, exactly ``"1"`` is simulation, anything else — ``"0"``,
   ``"true"``, stray whitespace, ... — is refused loudly rather than silently
   treated as production).
4. Under an effective ``OMNIAGENTOS_SIM_MODE=1`` the resolver is fail-closed: a
   missing or relative var root, or a candidate that is the production path by
   inode identity — ancestor directory via ``os.path.samestat`` plus an exact
   match on the remaining components, never a string compare — raises
   :class:`TokenPathError` and never falls back to the operator checkout token.
5. Outside sim mode an empty var root falls back to the historical
   repo-relative path (current production behaviour).

TWO SEAMS, deliberately separate:

* :func:`_load_token` READS. It creates nothing — no ``mkdir``, no ``O_CREAT``,
  no ``chmod`` — and returns ``None`` when the token does not exist.
  :func:`verify_token` uses it, so a missing token is an authentication FAILURE.
* :func:`load_or_create_token` MINTS. It is the explicit creator for the
  legitimate first-boot path (``sessions/hook_client.py`` needs a credential to
  reach the local control plane).

Verification used to go through the minting seam, so ANY process that could
merely check a presented token — every unauthenticated request to every
control-plane route — created ``<var root>/secrets/sessions-token`` and its
0700 parent. Minting the production credential is not a side effect of a failed
auth check.
"""

from __future__ import annotations

import hmac
import os
import secrets
from pathlib import Path

from omniagentos.path_containment import inode_relative_parts
from omniagentos.simgate import SimGateError, _parse_strict_sim_mode

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_VAR_ROOT = _REPO_ROOT / "var"
_TOKEN_LEAF = ("secrets", "sessions-token")
#: The historical repo-root-relative path. It is BOTH the non-sim default AND
#: the sentinel that means "TOKEN_PATH has not been overridden".
_LEGACY_TOKEN_PATH: Path = _DEFAULT_VAR_ROOT.joinpath(*_TOKEN_LEAF)

#: Backward-compatible override slot. Left alone, the real path is resolved
#: per call by token_path(). Assigned (as ~15 test modules do), the
#: assignment wins.
TOKEN_PATH: Path = _LEGACY_TOKEN_PATH


class TokenPathError(RuntimeError):
    """The token path cannot be resolved safely."""


def _nearest_existing(path: Path | str) -> tuple[str, tuple[str, ...]] | None:
    """Split ``path`` into its nearest existing ancestor plus missing parts.

    Returns ``None`` when no existing ancestor can be reached (identity is
    undeterminable, so callers must fail closed).
    """
    try:
        node = os.path.abspath(os.path.expanduser(os.fspath(path)))
    except (OSError, TypeError, ValueError):
        return None
    missing: list[str] = []
    while True:
        try:
            os.lstat(node)
            return node, tuple(reversed(missing))
        except (FileNotFoundError, NotADirectoryError):
            pass
        except (OSError, ValueError):
            return None
        parent = os.path.dirname(node)
        if parent == node:
            return None
        missing.append(os.path.basename(node))
        node = parent


def _is_legacy_token_path(candidate: Path) -> bool | None:
    """Is ``candidate`` the production token file? ``None`` means unknown.

    This is exact token-file identity, not directory containment, and a string
    compare is not sound for it: on macOS the firmlink spelling
    ``/System/Volumes/Data/<path>`` is a *different string* naming the *same
    inode*, so a canonical-spelling check would admit the production token.
    Identity is therefore decided as (ancestor directory by inode) AND (the
    exact remaining path components), which keeps ``os.path.samestat``
    semantics without requiring the token leaf — commonly absent — to exist.
    """
    if _nearest_existing(candidate) is None:
        return None
    anchored = _nearest_existing(_LEGACY_TOKEN_PATH)
    if anchored is None:
        return None
    anchor, legacy_parts = anchored
    # inode_relative_parts() re-canonicalizes the candidate and walks parents by
    # samestat; () means "is the anchor itself".
    parts = inode_relative_parts(candidate, anchor)
    if parts is None:
        # Not below the legacy anchor at all: a different file, not an error.
        return False
    # Exact remaining components only — never case-fold / normcase / lower.
    return parts == legacy_parts


def token_path() -> Path:
    """The active token path, resolved fresh on every call."""
    current = TOKEN_PATH
    if Path(current) != _LEGACY_TOKEN_PATH:
        # Explicit in-process override (tests, deliberate callers). Honour it
        # with no further checks — even under sim mode. The defect is the silent
        # implicit default, not an intentional assignment.
        return Path(current)

    raw = (os.environ.get("OMNIAGENTOS_VAR_DIR") or os.environ.get("OMNIAGENTOS_VAR") or "").strip()
    try:
        sim_mode = _parse_strict_sim_mode(os.environ)
    except SimGateError as exc:
        # Reuse simgate's fail-closed parse (same refusal for "0" / "true" /
        # stray whitespace / ... as production) but keep this module's own
        # exception type — every other refusal in token_path() raises
        # TokenPathError, and callers should not have to catch two types.
        raise TokenPathError(str(exc)) from exc

    if sim_mode:
        if not raw:
            raise TokenPathError(
                "OMNIAGENTOS_SIM_MODE=1 requires OMNIAGENTOS_VAR_DIR (or "
                "OMNIAGENTOS_VAR) to be set to an absolute campaign var root; "
                "refusing to fall back to the production token path"
            )
        if not Path(raw).is_absolute():
            raise TokenPathError(
                "OMNIAGENTOS_SIM_MODE=1 requires OMNIAGENTOS_VAR_DIR (or "
                "OMNIAGENTOS_VAR) to be an absolute path "
                f"(got {raw!r}); refusing to fall back to the production "
                "token path"
            )
        candidate = Path(raw).joinpath(*_TOKEN_LEAF)
        # Fail closed: True (it IS production) and None (cannot tell) both refuse.
        if _is_legacy_token_path(candidate) is not False:
            raise TokenPathError(
                "OMNIAGENTOS_SIM_MODE=1 refuses to use the production token "
                "path: OMNIAGENTOS_VAR_DIR / OMNIAGENTOS_VAR resolved to the "
                f"legacy path {_LEGACY_TOKEN_PATH}"
            )
        return candidate

    if raw:
        return Path(raw).joinpath(*_TOKEN_LEAF)
    return _LEGACY_TOKEN_PATH


def load_or_create_token() -> str:
    """Return the local hook token, creating it with owner-only permissions."""
    path = token_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    created = False
    # MINT SEAM (see the module docstring): the explicit creator, used by the
    # legitimate first-boot minter (``sessions/hook_client.py``). Verification
    # goes through the READ seam, _load_token(), so a failed auth check can never
    # mint the credential as a side effect.
    # (The one-line docstring above is load-bearing: tests/counterfeits/patches/
    # sleep-hides-leak.patch anchors its context on it.)
    token = _load_token()
    if token is None:
        token = secrets.token_urlsafe(32)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        try:
            descriptor = os.open(path, flags, 0o600)
        except FileExistsError:
            token = path.read_text(encoding="utf-8").strip()
        else:
            with os.fdopen(descriptor, "w", encoding="utf-8") as token_file:
                token_file.write(f"{token}\n")
            created = True
    # ``os.open(..., 0o600)`` already creates this owner-only. Keep the explicit
    # chmod as defense in depth only on that creation path: sandboxed hook clients
    # are intentionally allowed to read the existing token but forbidden to mutate
    # var/secrets, so chmod-on-every-read made every hook fail before its HTTP call.
    if created:
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    if not token:
        raise RuntimeError("session token file is empty")
    return token


def _load_token() -> str | None:
    """READ SEAM: the active token, or ``None`` when it does not exist.

    Creates and modifies nothing — no ``mkdir``, no ``O_CREAT``, no ``chmod``.
    """
    path = token_path()
    try:
        token = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    if not token:
        raise RuntimeError("session token file is empty")
    return token


def verify_token(presented: str | None) -> bool:
    """Compare a presented local token without data-dependent timing.

    VERIFICATION SEAM: reads only. A missing token file is an authentication
    FAILURE, not a mint event — previously this went through
    :func:`load_or_create_token`, so every unauthenticated request to any
    control-plane route created ``<var>/secrets/sessions-token`` (and its 0700
    parent) if it was absent: any process that could merely *verify* could mint
    the production credential.
    """
    if not presented:
        return False
    token = _load_token()
    return token is not None and hmac.compare_digest(token, presented)


__all__ = [
    "TOKEN_PATH",
    "TokenPathError",
    "load_or_create_token",
    "token_path",
    "verify_token",
]
