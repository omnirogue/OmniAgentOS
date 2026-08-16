"""Multiple Claude accounts -> spawn rotation past a single account's rate limit.

An account is primarily a CLI CONFIG DIR (``CLAUDE_CONFIG_DIR``): the OAuth login lives
in that dir, so the registry stores only the PATH, never a credential. Token / API-key
accounts keep their secret in ``var/secrets`` (0600) referenced by ``secret_ref``; the
secret VALUE is injected into the spawned session's env at launch (the CLI needs it to
authenticate) while the secret FILE stays sandbox-denied -- the same credential-broker
rule the rest of the system follows (secrets never sit in the DB, only in var/secrets).

Selection is round-robin over ENABLED accounts (least-recently-used first), so adding +
enabling an account immediately spreads concurrent sessions across more rate limits.
"""

from __future__ import annotations

import glob
import json
import math
import os
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from omniagentos.contracts import default_db_path, new_id, utc_now_iso
from omniagentos.path_containment import inode_paths_equal

_AUTH_TYPES = ("config_dir", "oauth_token", "api_key")

# THE definition of "usable for a spawn right now". Every selection path
# (here and in routing.limit_state) interpolates this one fragment rather than
# re-spelling the predicate, because a pause honored by one query and ignored by
# another is worse than no pause at all -- the operator sees the account leave
# the rotation UI while a different code path keeps handing it out. Named
# parameters so a single ``:now`` covers both windows.
AVAILABLE_PREDICATE = (
    "enabled = 1 AND provider = :provider "
    "AND (cooldown_until IS NULL OR cooldown_until <= :now) "
    "AND (paused_until IS NULL OR paused_until <= :now)"
)

# Shared LRU ordering: never-used accounts first, then least-recently-picked.
LRU_ORDER = "ORDER BY (last_used_seq IS NOT NULL), last_used_seq ASC, created_at ASC, id ASC"


@dataclass
class SpawnAccount:
    """The resolved credential for one spawn: a config dir and/or extra auth env."""

    account_id: str
    label: str
    config_dir: str | None = None
    env: dict[str, str] = field(default_factory=dict)


def _home() -> str:
    return os.path.expanduser("~")


def _secrets_dir() -> Path:
    return Path(default_db_path()).resolve().parent / "secrets"


def _default_config_dir() -> str:
    return os.path.join(_home(), ".claude")


def _read_account_email(config_dir: str) -> str | None:
    """The logged-in email for a config dir, from its ``.claude.json`` oauthAccount.

    Handles both layouts: ``{config_dir}/.claude.json`` (a ``CLAUDE_CONFIG_DIR`` dir) and
    ``{config_dir}.json`` (the default ``~/.claude``, whose json is ``~/.claude.json``)."""
    for path in (os.path.join(config_dir, ".claude.json"), f"{config_dir}.json"):
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            continue
        account = data.get("oauthAccount") if isinstance(data, dict) else None
        if isinstance(account, dict):
            email = account.get("emailAddress") or account.get("email")
            if email:
                return str(email)
    return None


def detect_config_dirs() -> list[tuple[str, str | None]]:
    """Existing Claude config dirs on this machine, each as ``(config_dir, email)``.

    The default ``~/.claude`` first, then ``~/.claude-account-*`` (sorted). Only dirs
    that actually exist are returned."""
    home = _home()
    found: list[str] = []
    default = os.path.join(home, ".claude")
    if os.path.isdir(default):
        found.append(default)
    for path in sorted(glob.glob(os.path.join(home, ".claude-account-*"))):
        if os.path.isdir(path) and path not in found:
            found.append(path)
    return [(p, _read_account_email(p)) for p in found]


def _connect(db_path: str | None) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path or default_db_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _row_to_account(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    account = dict(row)
    account["enabled"] = bool(account.get("enabled"))
    account["is_default"] = bool(account.get("is_default"))
    # Never leak the on-disk secret path/value to callers. ``has_secret`` means
    # a secret is *loadable*, not merely that a secret_ref string is stored —
    # a dangling ref would otherwise render as a present credential (phantom
    # secret, the governing non-result-as-favourable class).
    secret_ref = account.pop("secret_ref", None)
    account["has_secret"] = bool(_load_secret(str(secret_ref)) if secret_ref else False)
    # ``paused`` is DERIVED, never stored: nothing sweeps expired pauses (the
    # only caller of clear_expired_cooldowns is longhaul's engine loop, which
    # may not be running), so a stored flag would keep reading "paused" long
    # after rotation had resumed using the account. Deriving it means the UI
    # cannot disagree with AVAILABLE_PREDICATE.
    paused_until = account.get("paused_until")
    account["paused"] = bool(paused_until and str(paused_until) > utc_now_iso())
    if not account["paused"]:
        account["pause_reason"] = None
    return account


def _reconcile_detected(conn: sqlite3.Connection, config_dir: str, email: str, now: str) -> None:
    """Keep a registered config dir's identity in step with who it now logs in as.

    A config dir is a mutable thing: ``claude /login`` inside it swaps the
    account without touching the registry. The original backfill used
    ``COALESCE(email, ?)``, which only ever filled a NULL -- so after a re-login
    the row kept the PREVIOUS account's address forever, and with it any stale
    ``status``. That is how a healthy login ends up displayed as a different,
    broken account and benched out of rotation.

    A changed email means a genuinely different login, so the health recorded
    against the old one no longer describes anything: status resets. ``enabled``
    is deliberately NOT touched -- it belongs to the operator and to the
    auth-failure stop-the-line, and silently re-enabling an account someone
    disabled on purpose would fight both.
    """
    row = conn.execute(
        "SELECT email, label FROM claude_accounts WHERE config_dir = ?", (config_dir,)
    ).fetchone()
    if row is None:
        return
    stored = str(row["email"]) if row["email"] else None

    if stored is None:  # first time we could read it: plain backfill
        conn.execute(
            "UPDATE claude_accounts SET email = ?, "
            "status = CASE WHEN status = 'unknown' THEN 'ok' ELSE status END, "
            "updated_at = ? WHERE config_dir = ?",
            (email, now, config_dir),
        )
        return

    if stored == email:
        return

    # Re-login. Carry the label across only if it was tracking the email; a
    # label the operator typed themselves is theirs to keep.
    label = str(row["label"]) if row["label"] else ""
    conn.execute(
        "UPDATE claude_accounts SET email = ?, label = ?, status = 'ok', "
        "status_detail = ?, updated_at = ? WHERE config_dir = ?",
        (
            email,
            email if label == stored else label,
            f"re-login detected: was {stored}",
            now,
            config_dir,
        ),
    )


def ensure_detected(conn: sqlite3.Connection) -> None:
    """Idempotently register detected config dirs.

    The default ``~/.claude`` is registered enabled + is_default (it is what the spawner
    uses today); the others are registered DISABLED, so listing shows every account
    immediately while rotation only uses the ones the operator explicitly enables."""
    now = utc_now_iso()
    default_dir = _default_config_dir()
    existing = {
        str(r["config_dir"])
        for r in conn.execute("SELECT config_dir FROM claude_accounts WHERE config_dir IS NOT NULL")
    }
    have_default = bool(
        conn.execute("SELECT 1 FROM claude_accounts WHERE is_default = 1 LIMIT 1").fetchone()
    )
    for config_dir, email in detect_config_dirs():
        if config_dir in existing:
            if email:
                _reconcile_detected(conn, config_dir, email, now)
            continue
        # Safety (`is True`): only positively equal identity is auto-enabled as default.
        is_default = (
            1 if (inode_paths_equal(config_dir, default_dir) is True and not have_default) else 0
        )
        enabled = is_default
        if is_default:
            have_default = True
        label = email or os.path.basename(config_dir.rstrip("/")) or "default"
        conn.execute(
            "INSERT OR IGNORE INTO claude_accounts "
            "(id, label, auth_type, config_dir, email, enabled, is_default, status, "
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                new_id("acct"),
                label,
                "config_dir",
                config_dir,
                email,
                enabled,
                is_default,
                "ok" if email else "unknown",
                now,
                now,
            ),
        )
        existing.add(config_dir)
    conn.commit()


def list_accounts(db_path: str | None = None) -> list[dict[str, Any]]:
    """Every registered account (auto-registering detected config dirs first), ordered
    default-first then enabled-first. Secrets are never included -- only ``has_secret``."""
    conn = _connect(db_path)
    try:
        ensure_detected(conn)
        rows = conn.execute(
            "SELECT * FROM claude_accounts ORDER BY is_default DESC, enabled DESC, created_at ASC"
        ).fetchall()
        return [_row_to_account(r) for r in rows]
    finally:
        conn.close()


def get_account(account_id: str, db_path: str | None = None) -> dict[str, Any] | None:
    conn = _connect(db_path)
    try:
        row = conn.execute("SELECT * FROM claude_accounts WHERE id = ?", (account_id,)).fetchone()
        return _row_to_account(row) if row is not None else None
    finally:
        conn.close()


def add_account(
    *,
    label: str,
    auth_type: str = "config_dir",
    config_dir: str | None = None,
    secret: str | None = None,
    enabled: bool = True,
    db_path: str | None = None,
) -> dict[str, Any]:
    """Register a new account.

    ``config_dir`` accounts store only the (validated, existing) path. ``oauth_token`` /
    ``api_key`` accounts write the secret to ``var/secrets`` (0600) and store only a
    reference. Raises ``ValueError`` on bad input or a duplicate config dir."""
    if auth_type not in _AUTH_TYPES:
        raise ValueError(f"invalid auth_type: {auth_type}")
    now = utc_now_iso()
    account_id = new_id("acct")
    email: str | None = None
    secret_ref: str | None = None

    if auth_type == "config_dir":
        if not config_dir or not config_dir.strip():
            raise ValueError("config_dir is required for a config_dir account")
        config_dir = os.path.abspath(os.path.expanduser(config_dir.strip()))
        if not os.path.isdir(config_dir):
            raise ValueError(f"config_dir does not exist: {config_dir}")
        email = _read_account_email(config_dir)
    else:
        if not secret or not secret.strip():
            raise ValueError("a token or API key is required")
        secret_ref = f"claude-account-{account_id}"
        secrets_dir = _secrets_dir()
        secrets_dir.mkdir(parents=True, exist_ok=True)
        secret_path = secrets_dir / secret_ref
        secret_path.write_text(secret.strip() + "\n", encoding="utf-8")
        os.chmod(secret_path, 0o600)

    conn = _connect(db_path)
    try:
        try:
            conn.execute(
                "INSERT INTO claude_accounts "
                "(id, label, auth_type, config_dir, email, secret_ref, enabled, "
                " is_default, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    account_id,
                    label.strip() or (email or config_dir or "account"),
                    auth_type,
                    config_dir,
                    email,
                    secret_ref,
                    1 if enabled else 0,
                    0,
                    "ok" if (email or secret_ref) else "unknown",
                    now,
                    now,
                ),
            )
            conn.commit()
        except sqlite3.IntegrityError as exc:
            if secret_ref:  # roll back the just-written secret file
                (_secrets_dir() / secret_ref).unlink(missing_ok=True)
            raise ValueError("that config dir is already registered") from exc
        row = conn.execute("SELECT * FROM claude_accounts WHERE id = ?", (account_id,)).fetchone()
        return _row_to_account(row)
    finally:
        conn.close()


def set_enabled(account_id: str, enabled: bool, db_path: str | None = None) -> bool:
    conn = _connect(db_path)
    try:
        cur = conn.execute(
            "UPDATE claude_accounts SET enabled = ?, updated_at = ? WHERE id = ?",
            (1 if enabled else 0, utc_now_iso(), account_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def set_default(account_id: str, db_path: str | None = None) -> bool:
    """Make one account the default (used when rotation is empty). At most one default."""
    conn = _connect(db_path)
    try:
        if (
            conn.execute("SELECT 1 FROM claude_accounts WHERE id = ?", (account_id,)).fetchone()
            is None
        ):
            return False
        now = utc_now_iso()
        conn.execute("UPDATE claude_accounts SET is_default = 0, updated_at = ?", (now,))
        # A default account is implicitly enabled (it must be usable).
        conn.execute(
            "UPDATE claude_accounts SET is_default = 1, enabled = 1, updated_at = ? WHERE id = ?",
            (now, account_id),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def remove_account(account_id: str, db_path: str | None = None) -> bool:
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT secret_ref FROM claude_accounts WHERE id = ?", (account_id,)
        ).fetchone()
        if row is None:
            return False
        cur = conn.execute("DELETE FROM claude_accounts WHERE id = ?", (account_id,))
        conn.commit()
        secret_ref = row["secret_ref"]
        if secret_ref:  # best-effort secret cleanup
            (_secrets_dir() / str(secret_ref)).unlink(missing_ok=True)
        return cur.rowcount > 0
    finally:
        conn.close()


def _load_secret(secret_ref: str | None) -> str | None:
    if not secret_ref:
        return None
    try:
        return (_secrets_dir() / secret_ref).read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _required_auth_env(account: dict[str, Any]) -> dict[str, str] | None:
    """Auth env for token/key accounts, or None when the secret cannot load.

    ``config_dir`` accounts need no secret file and return ``{}``.
    ``oauth_token`` / ``api_key`` accounts return the env mapping when the secret
    is loadable, else ``None`` (required credential absent — never confuse with
    empty env from a config-dir account).
    """
    auth_type = account.get("auth_type")
    if auth_type == "oauth_token":
        secret = _load_secret(account.get("secret_ref"))
        if not secret:
            return None
        return {"CLAUDE_CODE_OAUTH_TOKEN": secret}
    if auth_type == "api_key":
        secret = _load_secret(account.get("secret_ref"))
        if not secret:
            return None
        return {"ANTHROPIC_API_KEY": secret}
    return {}


def spawn_account_from_row(account: dict[str, Any]) -> SpawnAccount:
    """Build the spawn credential for a ``claude_accounts`` row.

    Loads the account's secret (if any) into the appropriate auth env var. The
    env-var names are the claude CLI's; non-claude providers get their env
    shaped by the spawn wrapper (WP3), which consumes ``config_dir`` only.

    Does **not** raise on a missing oauth/api_key secret. Existing production
    callers (e.g. ``routing.limit_state.reserve_account``) may have already
    written a reservation/LRU advance before invoking this helper; raising
    after that write leaves a durable successful-looking pick with no usable
    credential. Selection paths that own the transaction
    (:func:`next_account_for_spawn`) must refuse hollow token/key picks via
    :func:`_required_auth_env` *before* advancing the successful-pick cursor.
    """
    # Three-valued auth env: None = required secret missing. Handle None
    # explicitly — never ``auth_env or {}`` (empty ``{}`` is falsy and is a
    # legitimate return for config_dir accounts).
    auth_env = _required_auth_env(account)
    env: dict[str, str] = {} if auth_env is None else auth_env
    return SpawnAccount(
        account_id=str(account["id"]),
        label=str(account["label"]),
        config_dir=account.get("config_dir"),
        env=env,
    )


# At/above this cached weekly-window consumption an account is treated as
# predictively OUT (<10% remaining — the estate alert threshold, the operator 2026-08-12)
# and deprioritized in spawn rotation while a fresher account exists.
_PREDICTIVE_OUT_PCT = 90.0


def _predictively_out(config_dir: Any, *, now_ts: float | None = None) -> bool:
    """The account's CACHED weekly usage says it has <10% of its window left.

    Fail-open by design: missing/corrupt/unavailable usage, a non-claude auth
    type (no config_dir), or a weekly window whose ``resets_at`` has already
    passed all read as NOT out. This function may only ever REORDER preference
    among available accounts — it must never shrink the pool (observed-refusal
    cooldowns in ``limit_state`` own that), so an unknown balance is treated
    exactly like a healthy one.
    """
    if not config_dir or not isinstance(config_dir, str):
        return False
    try:
        # Lazy import: accounts.usage imports this module at top level.
        from omniagentos.accounts.usage import collect_claude

        snapshot = collect_claude(config_dir)
    except Exception:  # noqa: BLE001 — telemetry must never break spawning
        return False
    if not snapshot.available:
        return False
    now_ts = time.time() if now_ts is None else now_ts
    worst: float | None = None
    for window in snapshot.windows:
        if window.kind not in ("weekly_all", "weekly_scoped"):
            continue
        if window.resets_at:
            try:
                reset = datetime.fromisoformat(window.resets_at.replace("Z", "+00:00")).timestamp()
            except ValueError:
                reset = None
            if reset is not None and reset <= now_ts:
                continue  # the window has already reset; the cache is moot
        if not math.isfinite(window.percent):
            continue  # inf/NaN is corrupt telemetry, not a 100%-used account
        worst = window.percent if worst is None else max(worst, window.percent)
    return worst is not None and worst >= _PREDICTIVE_OUT_PCT


def predictive_out_ids(conn: sqlite3.Connection, provider: str, now: str) -> set[str]:
    """ids of AVAILABLE accounts whose cached weekly usage reads predictively out.

    Computed OUTSIDE any write transaction (the usage probe is filesystem I/O
    and must not extend a ``BEGIN IMMEDIATE`` write-lock hold). Total: any
    failure returns the empty set — fail-open, the pool never shrinks."""
    try:
        rows = conn.execute(
            f"SELECT id, config_dir FROM claude_accounts WHERE {AVAILABLE_PREDICATE}",
            {"provider": provider, "now": now},
        ).fetchall()
        return {str(row["id"]) for row in rows if _predictively_out(row["config_dir"])}
    except Exception:  # noqa: BLE001 — preference only, never an error source
        return set()


def next_account_for_spawn(
    db_path: str | None = None, provider: str = "claude"
) -> SpawnAccount | None:
    """Round-robin: the least-recently-used ENABLED account (not cooling), marked used now.

    Returns ``None`` when no account is enabled and not under cooldown -- the spawner then
    falls back to the process's default ``~/.claude`` login, exactly as before this feature.

    Cooldown filter (longhaul): accounts with cooldown_until <= now are excluded from rotation
    until their cooldown expires.

    ``provider`` filters the generalized accounts table (migration 045 added
    ``claude_accounts.provider`` with backfill 'claude'); the default keeps every
    pre-existing caller's behavior byte-identical.

    Token/api_key accounts whose secret cannot be loaded are stop-the-lined
    (disabled + ``status='error'``) and skipped — never returned as a hollow
    pick with empty env.
    """
    conn = _connect(db_path)
    try:
        ensure_detected(conn)
        now = utc_now_iso()
        # Predictive probe BEFORE the write transaction: the usage-cache read is
        # filesystem I/O and must not extend the BEGIN IMMEDIATE write-lock hold.
        out_ids = predictive_out_ids(conn, provider, now)
        # Selection + cursor advance run inside ONE write transaction so two
        # concurrent pickers serialize instead of both reading the same LRU row
        # and double-advancing it (cross-lineage review RTE-02).
        conn.execute("BEGIN IMMEDIATE")
        # LRU via a monotonic rotation cursor (never-used first, then least-recently
        # picked) -- clock-independent, so concurrent same-second spawns still spread.
        # Re-query each iteration: a failed secret load disables that row, so the
        # next AVAILABLE_PREDICATE pass naturally skips it. Bound the loop by the
        # *current eligible pool size* — a fixed 64 would return None while a healthy
        # account remains past that artificial cutoff (non-result as "no account").
        pool_size_row = conn.execute(
            f"SELECT COUNT(*) AS n FROM claude_accounts WHERE {AVAILABLE_PREDICATE}",
            {"provider": provider, "now": now},
        ).fetchone()
        pool_size = int(pool_size_row["n"] if pool_size_row is not None else 0)

        def _pick(account: dict[str, Any]) -> SpawnAccount:
            spawn = spawn_account_from_row(account)
            conn.execute(
                "UPDATE claude_accounts SET "
                "last_used_seq = (SELECT COALESCE(MAX(last_used_seq), 0) + 1 "
                "                 FROM claude_accounts), "
                "last_used_at = ?, updated_at = ? WHERE id = ?",
                (now, now, str(account["id"])),
            )
            conn.commit()
            return spawn

        def _next_row(exclude: list[str]) -> sqlite3.Row | None:
            params: dict[str, Any] = {"provider": provider, "now": now}
            clause = ""
            if exclude:
                keys = []
                for index, skip_id in enumerate(exclude):
                    params[f"skip{index}"] = skip_id
                    keys.append(f":skip{index}")
                clause = f" AND id NOT IN ({', '.join(keys)})"
            return conn.execute(
                f"SELECT * FROM claude_accounts WHERE {AVAILABLE_PREDICATE}{clause} "
                f"{LRU_ORDER} LIMIT 1",
                params,
            ).fetchone()

        # Predictive-balance preference (operator directive 2026-08-12): an
        # account whose CACHED weekly window is >= _PREDICTIVE_OUT_PCT used is
        # deprioritized while a fresher alternative exists. Preference only —
        # if every candidate is predictively out (or usage is unknown), the
        # pick degrades to exactly the pre-feature LRU behavior. Shrinking the
        # pool on observed refusals remains limit_state's job, never this one.
        predictive_skipped: list[str] = []
        for _ in range(pool_size):
            row = _next_row(predictive_skipped)
            if row is None:
                break
            account = dict(row)
            account_id = str(account["id"])
            # Credential check BEFORE any successful-pick cursor advance.
            # oauth/api_key with unreadable secret: stop-the-line (disable +
            # error), skip — never return hollow env as a pick, never advance
            # last_used_seq. Uses _required_auth_env (None = required secret
            # missing) rather than raising from spawn_account_from_row, which
            # must stay non-raising for production callers that write first.
            auth_env = _required_auth_env(account)
            if auth_env is None:
                detail = (
                    f"account {account_id}: {account.get('auth_type')} secret missing or unreadable"
                )
                conn.execute(
                    "UPDATE claude_accounts SET enabled = 0, status = ?, "
                    "status_detail = ?, updated_at = ? WHERE id = ?",
                    ("error", detail, now, account_id),
                )
                # No commit here: committing would drop the BEGIN IMMEDIATE
                # write lock mid-selection and let a concurrent picker select
                # the same row (review RTE-02 R2). The disable rides the
                # pick's commit or the terminal commit below.
                continue
            if account_id in out_ids:
                predictive_skipped.append(account_id)
                continue
            return _pick(account)
        # Every remaining candidate was predictively out: fall back to the
        # pre-feature behavior — LRU order among them (the skip list preserved
        # it). Re-check availability per id: a row may have been disabled or
        # cooled between the two passes.
        for account_id in predictive_skipped:
            row = conn.execute(
                f"SELECT * FROM claude_accounts WHERE {AVAILABLE_PREDICATE} AND id = :id",
                {"provider": provider, "now": now, "id": account_id},
            ).fetchone()
            if row is None:
                continue
            account = dict(row)
            if _required_auth_env(account) is None:
                continue
            return _pick(account)
        conn.commit()
        return None
    finally:
        try:
            if conn.in_transaction:
                conn.rollback()
        except Exception:  # noqa: BLE001 — closing must never mask the real error
            pass
        conn.close()


def mark_status(
    account_id: str, status: str, detail: str | None = None, db_path: str | None = None
) -> None:
    """Record an account's health (e.g. ``rate_limited``/``error`` after a failed spawn)."""
    conn = _connect(db_path)
    try:
        conn.execute(
            "UPDATE claude_accounts SET status = ?, status_detail = ?, updated_at = ? WHERE id = ?",
            (status, detail, utc_now_iso(), account_id),
        )
        conn.commit()
    finally:
        conn.close()


def set_account_cooldown(
    account_id: str,
    until_iso: str,
    detail: str = "",
    db_path: str | None = None,
    *,
    status: str | None = "rate_limited",
) -> bool:
    """Set an account's cooldown_until timestamp (THE single cooldown writer, WP2).

    Returns True if the account was updated, False if not found.

    Args:
        account_id: The account to cool down
        until_iso: UTC ISO timestamp until which the account is cooled
        detail: Optional detail string (reason for cooldown)
        db_path: Database path
        status: Status to record alongside the cooldown. The default
            ``'rate_limited'`` is the usage-limit path; pass ``None`` to leave
            the current status untouched (the 'overloaded' 30-120s backoff is a
            wait, NOT a rate_limited status change -- plan WP2 four-class table).

    Auth stop-the-line and disabled accounts are preserved: late transient /
    quota / overloaded outcomes may still record a cooldown window, but must
    never erase ``status='error'`` / ``status_detail`` or paint a disabled
    account as healthy or merely rate-limited.
    """
    conn = _connect(db_path)
    try:
        now = utc_now_iso()
        row = conn.execute(
            "SELECT status, enabled FROM claude_accounts WHERE id = ?",
            (account_id,),
        ).fetchone()
        if row is None:
            return False

        # Preserve operator-visible stop reason for auth errors and any
        # disabled account. Cooldown may still be set (rotation already
        # excludes disabled/cooled rows); status/detail are frozen.
        preserve_status = str(row["status"] or "") == "error" or not bool(row["enabled"])
        if preserve_status:
            cur = conn.execute(
                "UPDATE claude_accounts SET cooldown_until = ?, updated_at = ? WHERE id = ?",
                (until_iso, now, account_id),
            )
        elif status is None:
            cur = conn.execute(
                "UPDATE claude_accounts SET "
                "status_detail = ?, cooldown_until = ?, updated_at = ? "
                "WHERE id = ?",
                (detail, until_iso, now, account_id),
            )
        else:
            cur = conn.execute(
                "UPDATE claude_accounts SET "
                "status = ?, status_detail = ?, cooldown_until = ?, updated_at = ? "
                "WHERE id = ?",
                (status, detail, until_iso, now, account_id),
            )
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def pause_account(
    account_id: str, until_iso: str, reason: str = "", db_path: str | None = None
) -> bool:
    """Temporarily remove an account from rotation until ``until_iso`` (operator lever).

    Writes ONLY ``paused_until``/``pause_reason``. It deliberately does not touch
    ``cooldown_until`` or ``status``, which belong to ``routing.limit_state``:
    that module's STRUCTURED-FIRST ``ok`` path NULLs ``cooldown_until`` on every
    clean completion, so a pause parked there would be erased by the next
    successful session on the account. Keeping the columns separate also means a
    pause can never SHORTEN a live rate-limit cooldown -- both windows are ANDed
    in :data:`AVAILABLE_PREDICATE`, so the account returns only when both have
    elapsed.

    Re-pausing an already-paused account overwrites the window: the operator's
    most recent instruction is the one that counts. Returns False if not found.
    """
    conn = _connect(db_path)
    try:
        cur = conn.execute(
            "UPDATE claude_accounts SET paused_until = ?, pause_reason = ?, updated_at = ? "
            "WHERE id = ?",
            (until_iso, reason.strip() or None, utc_now_iso(), account_id),
        )
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def resume_account(account_id: str, db_path: str | None = None) -> bool:
    """Lift an operator pause early, before ``paused_until`` elapses.

    Clears the pause columns ONLY. A resumed account that is still under a
    provider ``cooldown_until`` stays out of rotation until that expires --
    resuming states "I no longer want this held back", not "ignore the rate
    limit the provider just gave us". Returns False if not found.
    """
    conn = _connect(db_path)
    try:
        cur = conn.execute(
            "UPDATE claude_accounts SET paused_until = NULL, pause_reason = NULL, "
            "updated_at = ? WHERE id = ?",
            (utc_now_iso(), account_id),
        )
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def clear_expired_cooldowns(now_iso: str, db_path: str | None = None) -> list[str]:
    """Clear expired cooldowns (cooldown_until <= now).

    Only restores ``status='ok'`` (and clears ``status_detail``) when the
    account is currently ``rate_limited`` *and* still enabled — the status the
    cooldown writer itself sets on healthy usage-limit rows. Auth stop-the-line
    rows keep ``status='error'`` (and their detail). Disabled accounts never
    become ``ok`` from a sweep, so a lapsed backoff cannot make a disabled or
    auth-failed account appear healthy.

    Returns list of account_ids whose cooldown window was cleared.

    Args:
        now_iso: Current UTC ISO timestamp (used for comparison)
        db_path: Database path
    """
    conn = _connect(db_path)
    try:
        # Find accounts to clear
        rows = conn.execute(
            "SELECT id FROM claude_accounts WHERE cooldown_until IS NOT NULL AND cooldown_until <= ?",
            (now_iso,),
        ).fetchall()
        account_ids = [row["id"] for row in rows]

        if account_ids:
            # Clear cooldown always; only flip enabled+rate_limited → ok.
            # Preserve auth-error / disabled / any non-rate_limited status/detail.
            placeholders = ",".join("?" * len(account_ids))
            conn.execute(
                f"UPDATE claude_accounts SET "
                f"status = CASE WHEN status = 'rate_limited' AND enabled = 1 "
                f"THEN 'ok' ELSE status END, "
                f"status_detail = CASE WHEN status = 'rate_limited' AND enabled = 1 "
                f"THEN NULL ELSE status_detail END, "
                f"cooldown_until = NULL, updated_at = ? "
                f"WHERE id IN ({placeholders})",
                [now_iso] + account_ids,
            )
            conn.commit()

        return account_ids
    finally:
        conn.close()
