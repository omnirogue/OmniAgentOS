"""The single, lightweight shared SECRET REGISTRY (AC-policy fix4).

ONE source of truth for "which filesystem paths are credential stores / secret
files". Reading any of them ships live credentials into an agent's context (or
off-machine), so every guardrail layer must agree on the set. Before fix4 the
three consumers each carried their OWN list and they had DRIFTED -- the shell
classifier flagged ``~/.ssh``/``~/.config/gcloud`` while the OS sandbox deny-list
did not, so a secret read that one layer missed the other also missed. Now all
THREE derive from THIS module:

  * ``policy.shell.classify_shell``          -> hard-stop a shell secret read
  * ``sessions.policy_map.classify_tool``    -> hard-stop a native Read/Grep/... read
  * ``runner.sandbox.secret_read_deny_roots``-> SBPL deny-read the same dirs

The resolver (``references_secret``) does the SAME normalization for every layer
-- expand ``~`` and ``$VAR``, resolve ``..`` and symlinks via ``os.path.realpath``,
then match against the resolved secret dirs -- so the classifier gate and the OS
sandbox gate can never again disagree about what counts as a secret.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from omniagentos.path_containment import inode_relative_parts_anchored
from omniagentos.runtime_paths import (
    TOKEN_VAR_ENV_KEYS,
    resolve_sim_context_or_none,
    resolve_var_root,
)

# Secret DIRECTORIES, relative to $HOME. Anything resolving inside one of these is
# a credential store: reading it hard-stops. (AC-policy fix4 -- was split across
# shell.py and sandbox.py with .ssh/.config/gcloud missing from the sandbox side.)
SECRET_DIR_RELS: tuple[str, ...] = (
    ".config/omni",  # omni connections.env: payment/bank/infra keys
    ".ssh",  # ssh private keys / authorized_keys
    ".aws",  # aws credentials + config
    ".config/gcloud",  # gcloud application-default credentials
    ".gnupg",  # gpg private keyring
    ".docker",  # docker registry credentials
    ".kube",  # kubernetes cluster credentials
    ".config/gh",  # GitHub CLI tokens
)

# Distinctive secret / private-key BASENAMES: hard-stop by basename alone, wherever
# the file lives (a private key copied out of ~/.ssh is still a private key).
SECRET_BASENAMES: frozenset[str] = frozenset(
    {
        "connections.env",
        "secrets",
        "vault",
        ".env",
        ".env.local",
        ".env.production",
        ".env.development",
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
    }
)

# Secret filename SUFFIXES flagged unconditionally, anywhere. These are
# unambiguous credential containers or encrypted/backup materials; broad
# project-common certificate suffixes remain in OOS_SECRET_SUFFIXES below.
SECRET_SUFFIXES: tuple[str, ...] = (
    ".enc",
    ".vault",
    ".secrets",
    ".creds",
    ".key-backup",
    ".db-backup",
)

# Decorations used when a distinctive secret basename is copied or archived.
# Keep the boundary explicit so ``id_rsa.bakery`` is not treated as a backup.
_SECRET_COMPOUND_DECORATIONS: tuple[str, ...] = (
    ".tar.gz",
    ".backup",
    ".archive",
    ".bak",
    ".old",
    ".orig",
    ".tar",
    ".zip",
    ".7z",
    ".rar",
)
_COMPRESSION_SUFFIXES: tuple[str, ...] = (".gz", ".bz2", ".xz", ".zst")

# Secret basenames flagged ONLY for OUT-OF-PROJECT paths (AC-policy fix5): these
# names are routine project config/fixtures in scope, but their per-user copies
# commonly contain live credentials and must hard-stop.
OOS_SECRET_BASENAMES: frozenset[str] = frozenset(
    {
        ".netrc",
        ".git-credentials",
        ".npmrc",
        ".pypirc",
        ".terraformrc",
        "credentials",
    }
)

# Secret suffixes flagged ONLY for OUT-OF-PROJECT paths (AC-policy fix4 LOW): an
# in-scope ``./fullchain.pem`` is routine project material and must not hard-stop
# normal work, but reading an out-of-scope ``~/foo.pem`` / ``/etc/ssl/x.pem`` is a
# credential read and does hard-stop.
OOS_SECRET_SUFFIXES: tuple[str, ...] = (".pem", ".key")

# The repo-local secrets directory (``<repo>/var/secrets``), realpath below.
_REPO_ROOT = Path(__file__).resolve().parents[1]


def _linked_worktree_main_root(repo_root: Path) -> Path | None:
    """The MAIN working tree root when ``repo_root`` is a linked git WORKTREE.

    A linked worktree's ``.git`` is a FILE (``gitdir: <path>``), and the shared
    repo's ``var/secrets`` credential store lives under the MAIN working tree, not
    the linked worktree — so a classifier/sandbox running from a worktree that
    registered only ``<worktree>/var/secrets`` would leave the real store
    unguarded (the worktree miss this closes). Resolved ONCE at import via pure
    file I/O (read ``.git`` then the gitdir's ``commondir``); never a subprocess on
    the per-classification hot path. Returns ``None`` for a normal checkout or any
    parse failure (fail-safe: the normal repo-local dir is still registered)."""
    git_marker = repo_root / ".git"
    try:
        if not git_marker.is_file():
            return None
        text = git_marker.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text.startswith("gitdir:"):
        return None
    gitdir = Path(text[len("gitdir:") :].strip())
    if not gitdir.is_absolute():
        gitdir = repo_root / gitdir
    try:
        gitdir = gitdir.resolve()
        common_text = (gitdir / "commondir").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    common_git = Path(common_text)
    if not common_git.is_absolute():
        common_git = gitdir / common_git
    try:
        common_git = common_git.resolve()
    except OSError:
        return None
    # The main working tree is the parent of the shared ``.git`` common dir.
    return common_git.parent if common_git.name == ".git" else None


# When running from a linked worktree, the MAIN checkout's ``var/secrets`` too.
_MAIN_WORKTREE_ROOT = _linked_worktree_main_root(_REPO_ROOT)


def _realpath(path: str) -> str:
    try:
        return os.path.realpath(path)
    except (OSError, ValueError):
        return path


def _casefold_path(path: str) -> str:
    """Normalize path case for containment on case-insensitive filesystems.

    macOS's default filesystem is case-insensitive but case-preserving, and
    ``realpath`` does not canonicalize case. Explicitly case-folding on Darwin
    prevents case-variant references such as ``~/.SSH`` from bypassing secret-dir
    containment. ``str.casefold()`` (not ``str.lower()``) is used deliberately:
    ``.lower()`` is a simple, LOCALE-INSENSITIVE lowercasing that misses several
    Unicode caseless-matching collisions APFS treats as equal -- most notably
    German ``ẞ``/``ß`` both lowercasing to ``ß`` while only ``.casefold()`` maps
    them (and ``ss``) to the same string, plus assorted other multi-character
    case-fold expansions in the Unicode casefolding tables that ``.lower()``
    simply does not perform (Gemini review round 1, finding 4). Linux remains
    case-sensitive, where differently-cased paths are genuinely distinct.
    ``normcase`` retains the correct Windows behavior.
    """
    normalized = os.path.normcase(path)
    if sys.platform == "darwin":
        normalized = normalized.casefold()
    return normalized


def secret_dirs() -> list[str]:
    """Absolute, realpath-resolved secret directories.

    Home credential stores (``SECRET_DIR_RELS``) plus the repo-local
    ``var/secrets`` dir. Used by BOTH the classifier's containment check and the
    OS sandbox's deny-read roots, so the two layers cover exactly the same tree.

    Under simulation the campaign's OWN secrets directory is added too. Without
    it a campaign's credentials were protected by neither layer while the
    production tree stayed guarded — the campaign token is a real credential for
    the campaign's control plane. The directory is derived with exactly the
    spelling ``sessions/token.py`` uses (``OMNIAGENTOS_VAR_DIR`` then
    ``OMNIAGENTOS_VAR``, leaf ``secrets``), so it tracks the token file wherever
    the launcher puts it instead of guessing a layout. It is added
    UNCONDITIONALLY: an ``is_dir()`` guard would leave a secrets directory
    created after this call unprotected.

    An incoherent simulation environment propagates ``SimGateError`` rather than
    returning a production-only list: this feeds a deny-list, and a deny-list
    that silently narrows is the failure mode this registry exists to prevent.
    """
    home = os.path.expanduser("~")
    dirs = [_realpath(os.path.join(home, rel)) for rel in SECRET_DIR_RELS]
    dirs.append(_realpath(str(_REPO_ROOT / "var" / "secrets")))
    # Under a linked worktree, the shared store lives in the MAIN checkout — cover
    # it too, else a worktree-run classifier/sandbox misses the real credentials.
    if _MAIN_WORKTREE_ROOT is not None:
        dirs.append(_realpath(str(_MAIN_WORKTREE_ROOT / "var" / "secrets")))
    if resolve_sim_context_or_none() is not None:
        campaign_secrets = resolve_var_root(env_keys=TOKEN_VAR_ENV_KEYS, leaf=("secrets",))
        dirs.append(_realpath(str(campaign_secrets)))
    seen: list[str] = []
    for d in dirs:
        if d and d not in seen:
            seen.append(d)
    return seen


def _project_root(project_dir: str | None) -> str | None:
    if not project_dir or not isinstance(project_dir, str) or not project_dir.strip():
        return None
    try:
        return _realpath(os.path.expanduser(project_dir))
    except (OSError, ValueError):
        return None


def _resolve(raw: str, project_dir: str | None) -> str | None:
    """Resolve a raw token to a realpath the SAME way for every guardrail layer.

    Expands ``~`` and ``$VAR``, then realpath-resolves (following ``..`` and
    symlinks). Absolute inputs resolve directly; relative inputs resolve against
    the project scope (the caller's cwd IS the project). Returns None when nothing
    can be resolved (no project scope for a relative path)."""
    expanded = os.path.expanduser(os.path.expandvars(raw))
    try:
        if os.path.isabs(expanded):
            return _realpath(expanded)
        root = _project_root(project_dir)
        if root is not None:
            return _realpath(os.path.join(root, expanded))
    except (OSError, ValueError):
        return None
    return None


def _within(resolved: str | None, project_dir: str | None) -> bool:
    root = _project_root(project_dir)
    if root is None or resolved is None:
        return False
    return inode_relative_parts_anchored(resolved, root) is not None


def _is_secret_compound_basename(base: str) -> bool:
    """Return whether ``base`` is a renamed copy/archive of a secret basename."""
    for secret_base in SECRET_BASENAMES:
        if not base.startswith(secret_base):
            continue
        decoration = base[len(secret_base) :]
        for marker in _SECRET_COMPOUND_DECORATIONS:
            if decoration == marker:
                return True
            if any(decoration.startswith(marker + separator) for separator in ("-", "_")):
                return True
            if decoration.startswith(marker) and decoration[len(marker) :] in _COMPRESSION_SUFFIXES:
                return True
    return False


def _sep_normalized(path: str) -> str:
    """Separators rewritten to ``/`` -- ONLY where ``\\`` really IS a separator.

    Round 1 rewrote backslashes unconditionally, which is wrong on POSIX where
    ``\\`` is a perfectly LEGAL filename character: ``~/.ssh\\harmless.txt`` is a
    single file sitting directly in ``$HOME``, not something inside ``~/.ssh``, and
    rewriting the separator turned it into a false hard-stop (Gemini round 1, F4).
    Windows is the only platform where the rewrite is a normalization rather than a
    corruption, so it is gated on ``os.sep``. The hostile corpus never needed the
    POSIX rewrite -- rule 2's own ``var/secrets`` fast-path keeps its historical
    behavior and is untouched."""
    if os.sep == "\\" or os.altsep == "\\":
        return path.replace("\\", "/")
    return path


def _collapse_leading_double_slash(path: str) -> str:
    """Collapse a POSIX ``//`` prefix, which ``normpath`` deliberately PRESERVES.

    POSIX leaves a path beginning with exactly two slashes implementation-defined,
    so ``os.path.normpath`` keeps ``//home/u/.SSH/x`` as-is (three or more are
    already collapsed to one). A preserved ``//`` made the home prefix test fail
    and let a double-slash spelling walk past the guard (Gemini round 1, F2). Every
    mainstream POSIX system resolves ``//x`` to ``/x``, so collapsing it here makes
    the comparison agree with the filesystem."""
    if path.startswith("//") and not path.startswith("///"):
        return path[1:]
    return path


def _normalized_abs(path: str) -> str:
    """``normpath`` + separator + ``//`` normalization, for prefix comparison."""
    return _collapse_leading_double_slash(_sep_normalized(os.path.normpath(path)))


def _home_spellings() -> tuple[str, ...]:
    """Every spelling of ``$HOME`` a path may legitimately carry (raw + realpath)."""
    spellings: list[str] = []
    try:
        home = _normalized_abs(os.path.expanduser("~"))
    except (OSError, ValueError):
        return ()
    for candidate in (home, _normalized_abs(_realpath(home))):
        if candidate and candidate not in spellings:
            spellings.append(candidate)
    return tuple(spellings)


def _base_spellings(project_dir: str | None) -> tuple[str, ...]:
    """Absolute spellings of the base a RELATIVE token resolves against.

    Mirrors what :func:`_resolve` does with ``project_dir`` (the caller's cwd IS
    the project scope), so a relative token reaches the home-relative check the
    same way an absolute one does.

    ``project_dir`` ITSELF may be relative (``"."``, ``"src/"``). Round 2 bailed
    out to ``()`` on ``not os.path.isabs(base)``, which silently SKIPPED rule 5 --
    a favourable-absence fail-open, and a mismatch with ``_resolve``, which has
    always handled a relative scope correctly because ``_realpath`` resolves it
    against the process CWD (Gemini round 2, F5). The authoritative base is now
    taken from :func:`_project_root` -- the EXACT expression ``_resolve`` uses
    (``_realpath(os.path.expanduser(project_dir))``) -- so the two resolution paths
    can no longer disagree. The as-written absolute spelling (and, for a relative
    scope, the plain CWD join) is kept ALONGSIDE it, un-realpath'd, so a symlinked
    scope is matched under either spelling of HOME."""
    if not project_dir or not isinstance(project_dir, str) or not project_dir.strip():
        return ()
    spellings: list[str] = []

    def _add(candidate: str | None) -> None:
        if not candidate:
            return
        try:
            normalized = _normalized_abs(candidate)
        except (OSError, ValueError):
            return
        if normalized and normalized not in spellings:
            spellings.append(normalized)

    try:
        base = os.path.expanduser(os.path.expandvars(project_dir))
        if os.path.isabs(base):
            _add(base)
        else:
            # Relative scope: the same base ``_resolve`` lands on, spelled without
            # realpath first (``_project_root`` below supplies the realpath'd form).
            _add(os.path.join(os.getcwd(), base))
    except (OSError, ValueError):
        pass
    # The authoritative base: byte-for-byte the expression ``_resolve`` resolves
    # against, absolute for a relative scope because ``_realpath`` uses the CWD.
    _add(_project_root(project_dir))
    return tuple(spellings)


def _relative_to_home(path: str, home: str) -> str | None:
    """``path`` expressed relative to ``home``, or None when it is not under it."""
    if home == "/":
        # A root HOME (service accounts, some containers) is legitimate: EVERY
        # absolute path is then home-rooted, and ``~/.ssh`` genuinely IS ``/.ssh``.
        # Round 1 ``rstrip("/")``-ed this to "" and skipped it, silently disabling
        # the guard for those accounts (Gemini round 1, F3). Matching stays precise
        # because the caller anchors on SEGMENTS: ``/etc/hosts`` -> ``etc/hosts``
        # and ``/home/u/.ssh/x`` -> ``home/u/.ssh/x`` match no secret rel.
        return "" if path == "/" else (path[1:] if path.startswith("/") else None)
    home = home.rstrip("/")
    if not home:
        return None
    if path == home:
        return ""
    if path.startswith(home + "/"):
        return path[len(home) + 1 :]
    return None


def _home_relative_rels(raw: str, project_dir: str | None = None) -> tuple[str, ...]:
    """Every HOME-relative spelling of ``raw``, computed by PURE STRING work.

    Expands ``$VAR`` and ``~``, resolves a RELATIVE token against the project base
    the same way :func:`_resolve` does, collapses ``..``/``.`` via ``normpath``, and
    returns the part of each resulting path below ``$HOME``: ``""`` when the path IS
    home, the relative suffix when it is under home. Returns an EMPTY tuple when no
    spelling is home-rooted. All of home's and the base's spellings (as written and
    realpath-resolved) are tried and every home-rooted result is returned, because
    the consumer is a DENY check and must see every form a hostile token could take.

    WHY a string-only path exists at all: the inode-true containment check
    (``inode_relative_parts_anchored``) can only prove that ``~/.SSH/authorized_keys``
    is the ``~/.ssh`` store when the two spellings ALIAS THE SAME INODE -- which
    requires (a) the directory to EXIST on disk and (b) the filesystem to be
    case-insensitive. macOS's default APFS supplies both for free
    (``os.stat("~/.SSH") == os.stat("~/.ssh")``), so the case-variant hostile
    corpus passed on Darwin. A case-SENSITIVE Linux filesystem never aliases, so
    the same call returned ``None`` and ``references_secret("~/.SSH/authorized_keys")``
    was False -- ``classify_tool`` then auto-ran the read as READ_ONLY where macOS
    hard-stopped it as IRREVERSIBLE (Ubuntu CI:
    ``test_classifier_layer_hard_stops_every_hostile_case`` -- "classifier let
    'read-tool case secret' auto-run").

    This helper is deliberately NOT Darwin-gated and deliberately does NOT require
    the directory to exist. The guard it feeds is deny-biased: refusing to auto-run
    a read/write of ``~/.SSH/...`` on a case-sensitive filesystem -- where that is
    technically a different directory -- costs one approval prompt, while allowing
    it costs live credentials. Identical behavior on every platform is itself the
    property the two-layer invariant needs.
    """
    try:
        expanded = os.path.expanduser(os.path.expandvars(raw))
    except (OSError, ValueError):
        return ()
    absolutes: list[str] = []
    try:
        if os.path.isabs(expanded):
            absolutes.append(_normalized_abs(expanded))
        else:
            # A RELATIVE token resolves against the project scope, so a relative
            # case-variant (``.SSH/authorized_keys`` with project_dir=~) is the same
            # reference as ``~/.SSH/authorized_keys``. Round 1 returned None for
            # every relative token, so that spelling walked straight past the guard
            # (Gemini round 1, F1 -- BLOCKER).
            for base in _base_spellings(project_dir):
                absolutes.append(_normalized_abs(os.path.join(base, expanded)))
    except (OSError, ValueError):
        return ()
    homes = _home_spellings()
    rels: list[str] = []
    for candidate in absolutes:
        for home in homes:
            rel = _relative_to_home(candidate, home)
            if rel is not None and rel not in rels:
                rels.append(rel)
    return tuple(rels)


def _case_variant_home_secret_dir(raw: str, project_dir: str | None = None) -> bool:
    """True when ``raw`` names (or sits inside) a registered home secret DIR under
    ANY case spelling, proved without the filesystem.

    The portable companion to inode containment: case-folds the HOME-relative form
    of ``raw`` and matches it SEGMENT-ANCHORED against every entry of
    :data:`SECRET_DIR_RELS`, so ``~/.SSH/authorized_keys`` and
    ``~/.Config/gcloud/creds.db`` are secret references on a case-sensitive Linux
    filesystem exactly as they already were on case-insensitive APFS, while
    look-alikes that merely share a PREFIX (``~/.sshfoo/bar``,
    ``~/.config/gcloudx/y``, ``~/.bashrc``) are not. Relative tokens resolve against
    ``project_dir`` exactly as :func:`_resolve` resolves them. See
    :func:`_home_relative_rels` for why this is not Darwin-gated."""
    for home_rel in _home_relative_rels(raw, project_dir):
        home_rel_cf = home_rel.strip("/").casefold()
        if not home_rel_cf:
            continue
        for dir_rel in SECRET_DIR_RELS:
            dir_rel_cf = dir_rel.strip("/").casefold()
            if home_rel_cf == dir_rel_cf or home_rel_cf.startswith(dir_rel_cf + "/"):
                return True
    return False


def references_secret(token: str, project_dir: str | None) -> bool:
    """True when a token points at a known credential store / secret file.

    Shared by the shell classifier, the native-tool classifier, and (via
    ``secret_dirs``) the OS sandbox, so all three treat identical inputs
    identically. Deny-by-default: a secret read hard-stops (IRREVERSIBLE) in AUTO
    mode."""
    if not isinstance(token, str):
        return False
    raw = token.strip().strip("\"'")
    if not raw:
        return False
    base = os.path.basename(raw.rstrip("/")).lower()

    # 1. Distinctive secret basenames / suffixes: flag regardless of directory.
    if (
        base in SECRET_BASENAMES
        or base.endswith(SECRET_SUFFIXES)
        or _is_secret_compound_basename(base)
    ):
        return True

    # 2. Repo-local var/secrets, matched on the literal path segment (covers a
    #    relative ``var/secrets/...`` before any resolution). Case-folded
    #    UNCONDITIONALLY (deny-biased, mirroring rule 5's ratified rationale): a
    #    registered store is a registry FACT, not a filesystem probe, so a
    #    case-variant spelling (``var/SECRETS/token``) hard-stops on every
    #    platform — the mount under the path may be case-insensitive regardless
    #    of the host OS, and folding can only widen the deny set.
    normalized = raw.replace("\\", "/").casefold()
    if "var/secrets/" in normalized or normalized.rstrip("/").endswith("var/secrets"):
        return True

    resolved = _resolve(raw, project_dir)

    # 3. OOS-scoped secret suffixes/basenames: secret only when OUT of project
    #    scope. Routine in-project material (a .pem/.key, credentials fixture, or
    #    project .npmrc) remains readable. An unresolvable path fails closed.
    if base.endswith(OOS_SECRET_SUFFIXES) or base in OOS_SECRET_BASENAMES:
        return not _within(resolved, project_dir)

    # 4. Directory containment. A bare word (no path separator, not ~/$-rooted) is
    #    not a path reference and cannot be a secret dir hit.
    if "/" not in raw and not raw.startswith("~") and not raw.startswith("$"):
        return False
    if resolved is not None:
        for secret_dir in secret_dirs():
            if inode_relative_parts_anchored(resolved, secret_dir) is not None:
                return True

    # 5. PORTABLE home secret-dir containment (the Linux gap). Rule 4 proves a
    #    case-variant store spelling only via inode aliasing, which a case-SENSITIVE
    #    filesystem never provides — so ``~/.SSH/authorized_keys`` hard-stopped on
    #    macOS and auto-ran on Linux. This string-only, case-folded, segment-anchored
    #    check makes the two platforms agree, and still runs when rule 4 could not
    #    resolve the path at all (deny-biased; see :func:`_home_relative_rel`).
    return _case_variant_home_secret_dir(raw, project_dir)


def _within_secret_dir(resolved: str | None) -> bool:
    """True when ``resolved`` IS or is INSIDE a registered secret directory.

    Inode-true containment (never a string prefix), so a symlink/worktree spelling
    of the same store resolves to the same registered dir. A CASE-variant spelling
    only aliases here on a case-INSENSITIVE filesystem; callers pair this with
    :func:`_case_variant_home_secret_dir` for the portable guarantee."""
    if resolved is None:
        return False
    for secret_dir in secret_dirs():
        if inode_relative_parts_anchored(resolved, secret_dir) is not None:
            return True
    return False


def _is_under_real_home_outside_repo(resolved: str | None) -> bool:
    """True when ``resolved`` sits ANYWHERE under the machine's actual HOME
    directory -- at ANY nesting depth -- and is NOT also inside a genuine
    omniagentos checkout root (this module's own ``_REPO_ROOT``, or the MAIN
    working tree when this process runs from a linked worktree,
    ``_MAIN_WORKTREE_ROOT`` -- see :func:`_linked_worktree_main_root`).

    Generalizes the P3 downgrade-seam check (Gemini review round 2, finding 2
    remainder). Round 1's version of this check (``_is_direct_child_of_real_home``,
    now replaced) only caught a target sitting DIRECTLY in ``~`` -- a NESTED OOS
    credential under the SAME laundered root (``~/.config/.npmrc``, one level
    deep; ``~/.aws/credentials``, likewise) still fell through: its parent isn't
    ``~`` itself, and it genuinely IS "within" ``project_dir`` once a hook has
    re-classified the granted OOS root (``~``) AS ``project_dir`` for this
    write-scope call, so a plain ``_within(resolved, project_dir)`` check WRONGLY
    said "in scope" at any depth below the laundered root.

    Home-rooted config/dotfile trees (``~/.config``, ``~/.aws``, ``~/.ssh``, a
    project-less ``~/.npmrc``, ...) are never a genuine PROJECT checkout, at ANY
    depth, so containment under real HOME alone -- independent of what
    ``project_dir`` claims and independent of nesting depth -- is the right
    signal here. The explicit carve-out for a target ALSO inside a genuine repo
    checkout keeps a fixture that legitimately lives inside an omniagentos
    working tree under the user's home (e.g. ``~/omniagentos/aws/
    credentials``) from being caught by this rule -- that is exactly the
    "genuinely in-repo fixture" class the scoped dir check and this carve-out
    both exist to keep allowed."""
    if resolved is None:
        return False
    try:
        real_home = _realpath(os.path.expanduser("~"))
    except (OSError, ValueError):
        return False
    if inode_relative_parts_anchored(resolved, real_home) is None:
        return False  # not under the real home directory at all
    for repo_root in (_REPO_ROOT, _MAIN_WORKTREE_ROOT):
        if repo_root is None:
            continue
        if inode_relative_parts_anchored(resolved, _realpath(str(repo_root))) is not None:
            return False  # inside a genuine checkout -- not a bare home dotfile
    return True


def write_target_references_secret(token: str, project_dir: str | None) -> bool:
    """True when a WRITE lands INSIDE a REGISTERED secret store, OR targets an
    OOS-scoped credential basename/suffix that is genuinely out of project scope.

    The write-side counterpart to :func:`references_secret`. Unlike the READ path,
    it deliberately does NOT apply the UNCONDITIONAL distinctive-basename / suffix
    rule (:data:`SECRET_BASENAMES` / :data:`SECRET_SUFFIXES`, rule 1 of
    :func:`references_secret`): reading an ``id_rsa`` / ``.env.local`` anywhere
    ships a live credential into context, but matching THAT rule for writes
    over-refused ~201 legitimate in-project writes (``dashboard/.env.local``,
    fixtures) that merely SHARE a credential name.

    Two further checks are UNIONed instead:

    * WRITE/containment refusal scoped to the registered secret DIRS: a write
      INTO a real store (``~/.ssh``, ``<repo>/var/secrets``, a campaign's
      secrets dir) hard-stops while a secret-NAMED write outside every store
      proceeds. The case-insensitive ``var/secrets`` fast-path covers an
      unresolvable relative path with no project scope (mirrors
      :func:`references_secret` rule 2), and the portable case-folded home
      secret-dir check (:func:`_case_variant_home_secret_dir`) covers a
      case-variant store spelling on filesystems that do NOT alias inodes by case
      (mirrors :func:`references_secret` rule 5 — a pure widening of the deny set,
      never a narrowing of any allow above).
    * The OOS-scoped basename/suffix rule (:data:`OOS_SECRET_BASENAMES` /
      :data:`OOS_SECRET_SUFFIXES`, mirrors :func:`references_secret` rule 3):
      a write to ``~/.git-credentials``, ``~/.npmrc``, ``~/.config/.npmrc``, an
      out-of-project ``*.pem``/``*.key``, ... still hard-stops when it is
      genuinely OUT of the passed project scope OR sits anywhere under the
      machine's real HOME outside a genuine repo checkout
      (:func:`_is_under_real_home_outside_repo`) -- the latter closes the P3
      downgrade seam where a hook re-classifies a granted OOS root (``~``) AS
      ``project_dir``, which would otherwise make a plain scope check say "in
      scope" for a home-rooted credential file AT ANY NESTING DEPTH under that
      laundered root (Gemini review round 1 finding 2, and round 2's remaining
      gap: this restores the basename protection that a prior refactor
      dropped, ON TOP of, not instead of, the scoped dir check -- a write to a
      genuinely in-project fixture that merely SHARES one of these names, e.g.
      ``<project>/aws/credentials``, stays allowed; a write to
      ``~/.git-credentials``, ``~/.config/.npmrc``, or an out-of-project
      ``.pem``/``.key`` stays denied regardless of how deeply it is nested
      under the laundered root). When no real ``project_dir`` is known
      (``None``, e.g. a scope-independent classification pass), the home
      check still applies but the plain scope check is skipped rather than
      treating "no scope known" as "definitely out of scope" -- that would
      flag every OOS-basenamed in-project fixture on every such pass,
      regressing the very over-refusal this function exists to avoid.
    """
    if not isinstance(token, str):
        return False
    raw = token.strip().strip("\"'")
    if not raw:
        return False
    base = os.path.basename(raw.rstrip("/")).lower()
    # Unconditional deny-biased fold — same rationale as rule 2 in
    # references_secret: a registered store is a registry fact, not a
    # filesystem probe, and the mount's case semantics are unknowable here.
    normalized = raw.replace("\\", "/").casefold()
    if "var/secrets/" in normalized or normalized.rstrip("/").endswith("var/secrets"):
        return True
    resolved = _resolve(raw, project_dir)
    if _within_secret_dir(resolved):
        return True
    # Portable case-variant store containment, the WRITE-side twin of
    # :func:`references_secret` rule 5: ``_within_secret_dir`` is inode-only, so a
    # case-variant WRITE target (``~/.SSH/authorized_keys``) was denied on
    # case-insensitive APFS and ALLOWED on a case-sensitive Linux filesystem. This
    # is a pure WIDENING of the deny set — it is only ever consulted after the
    # existing checks declined, and it can only return True — so every allow the
    # scoped-dir / OOS / real-home rules above already grant is untouched.
    if _case_variant_home_secret_dir(raw, project_dir):
        return True
    if base.endswith(OOS_SECRET_SUFFIXES) or base in OOS_SECRET_BASENAMES:
        if _is_under_real_home_outside_repo(resolved):
            return True
        if project_dir is not None:
            return not _within(resolved, project_dir)
    return False


def path_relocates_secret_dir(token: str, project_dir: str | None) -> bool:
    """True when renaming/moving ``token`` would relocate a registered secret store.

    ``token`` resolves to a path that IS, is INSIDE, or is an ANCESTOR of a
    registered secret directory. Renaming an ANCESTOR (``mv var var2``) moves
    ``<repo>/var/secrets`` past every path-based deny — the second bypass the prior
    review found — so the shell classifier and the OS sandbox must both refuse it.
    Ancestor detection is inode-true, so a case/symlink spelling of the ancestor
    still resolves to the same store."""
    if not isinstance(token, str):
        return False
    raw = token.strip().strip("\"'")
    if not raw:
        return False
    resolved = _resolve(raw, project_dir)
    if resolved is None:
        return False
    for secret_dir in secret_dirs():
        # resolved within-or-equal the store, OR resolved is an ancestor of it.
        if inode_relative_parts_anchored(resolved, secret_dir) is not None:
            return True
        if inode_relative_parts_anchored(secret_dir, resolved) is not None:
            return True
    return False


# Native Claude-tool argument keys that carry a path/pattern the tool will READ.
# Read/NotebookRead -> file_path/notebook_path; LS/Grep/Glob -> path; Glob/Grep
# also carry a `pattern` that can itself name a secret (e.g. Glob ``~/.ssh/id_*``).
_PATH_ARG_KEYS: tuple[str, ...] = ("file_path", "notebook_path", "path", "pattern")

# Native Claude-tool argument keys that carry the path a WRITE tool MODIFIES.
_WRITE_PATH_ARG_KEYS: tuple[str, ...] = ("file_path", "notebook_path", "path")


def tool_input_write_references_secret(
    tool_input: dict[str, object] | None, project_dir: str | None
) -> bool:
    """True when a native write-tool's path argument targets a REGISTERED secret
    store (see :func:`write_target_references_secret`). Unlike
    :func:`tool_input_references_secret`, it does not inspect ``pattern`` (a write
    tool has no glob) and never flags a merely secret-NAMED in-scope write."""
    if not isinstance(tool_input, dict):
        return False
    for key in _WRITE_PATH_ARG_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str) and write_target_references_secret(value, project_dir):
            return True
    return False


def tool_input_references_secret(
    tool_input: dict[str, object] | None, project_dir: str | None
) -> bool:
    """True when a native read-tool's path/pattern argument targets a secret."""
    if not isinstance(tool_input, dict):
        return False
    for key in _PATH_ARG_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str) and references_secret(value, project_dir):
            return True
    return False


__all__ = [
    "OOS_SECRET_BASENAMES",
    "OOS_SECRET_SUFFIXES",
    "SECRET_BASENAMES",
    "SECRET_DIR_RELS",
    "SECRET_SUFFIXES",
    "path_relocates_secret_dir",
    "references_secret",
    "secret_dirs",
    "tool_input_references_secret",
    "tool_input_write_references_secret",
    "write_target_references_secret",
]
