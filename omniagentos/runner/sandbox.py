"""OS-level filesystem confinement for runner-executed commands (AC-policy #2).

The runner's own ``_run_command`` (validate steps and effect probes) historically
ran with the full inherited environment and no OS sandbox -- an out-of-scope
``rm``/``cp``/redirect could touch any file the runner user could. This module
wraps those commands in macOS ``sandbox-exec`` with a profile that DENIES every
file write outside the per-run workspace (and a small set of harmless sinks/temp),
so out-of-scope file harm is *physically* impossible regardless of what the
command tries -- a defense-in-depth backstop underneath the deny-by-default
classifier.

Network is left allowed because the model CLIs must reach their provider APIs
(api.anthropic.com, api.moonshot.ai, ...) or they cannot run at all; exfil is
instead defended by making secrets UNREADABLE (``secret_read_deny_roots`` +
the shared secret registry) so there is nothing sensitive to send. Money is
protected by removing payment/bank credentials from the subprocess environment
(the broker is the sole money path); SBPL cannot filter by hostname anyway.
Reads of home dotfiles are denied by default, then narrowly re-allowed for the
workspace/granted CLI state dirs and benign git config. The secret registry is
denied last. Writes are denied outside the workspace + CLI state dirs, and CLI or
project-local config/hooks/MCP files are denied even inside those dirs
(``config_write_deny_targets`` and ``project_config_write_deny_targets``), so a
sub-CLI cannot plant a hook or MCP server that disables the guardrail on the next
run.

If ``sandbox-exec`` is unavailable or a live self-test does not actually block an
out-of-workspace write (e.g. a non-macOS host), ``wrap_command`` returns the argv
unchanged. IMPORTANT: for the runner's own ``_run_command`` (validate/effect
probes) the deny-by-default classifier is still the guarantee in that case, but a
runner AGENT sub-CLI installs NO hook and has NO classifier gate -- so the
adapters FAIL CLOSED and refuse to launch a write/network-capable sub-CLI when the
OS sandbox is unavailable (see ``adapters.common.CliAdapter.run``). The self-test
means we never *claim* confinement we cannot demonstrate.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile
from collections.abc import Sequence
from fnmatch import fnmatchcase
from functools import lru_cache
from pathlib import Path

from omniagentos.path_containment import inode_relative_parts_anchored

_SANDBOX_EXEC = "/usr/bin/sandbox-exec"
_SBPL_REGEX_ESCAPE_CHARS = frozenset('\\"^$.|?*+()[]{}')

#: Egress modes ``build_profile`` understands. ``open`` is the DEFAULT and the
#: historical behavior (no network directives are emitted at all, so the profile
#: string is byte-identical to every profile built before this parameter existed).
NET_MODES: tuple[str, ...] = ("open", "deny", "proxy")
DEFAULT_NET_MODE = "open"


def _realpath(path: str) -> str:
    try:
        return os.path.realpath(path)
    except (OSError, ValueError):
        return path


def _sbpl_quote(path: str) -> str:
    """Quote a path for an SBPL double-quoted string literal."""
    return path.replace("\\", "\\\\").replace('"', '\\"')


def _sbpl_regex_escape(text: str) -> str:
    """Escape a literal string embedded in an SBPL ``regex #"..."`` pattern."""
    return "".join(f"\\{ch}" if ch in _SBPL_REGEX_ESCAPE_CHARS else ch for ch in text)


def home_dotfile_read_deny_pattern() -> str:
    """Regex denying reads beneath every dotfile directly under ``$HOME``.

    This is the fail-closed backstop for credential paths absent from the positive
    registry. It is evaluated at access time, so dotfiles created after profile
    construction are covered too. Legitimate roots are re-opened later by
    :func:`home_dotfile_read_allow_roots`.
    """
    home = _realpath(os.path.expanduser("~"))
    return f"^{_sbpl_regex_escape(home)}/\\.[^/]*(/.*)?$"


# Git reads these user-level, non-credential config paths even for read-only
# status/log/diff operations. They are read-only exceptions; neither is added to
# any write allowlist.
_BENIGN_HOME_DOTFILE_READS: tuple[str, ...] = (".gitconfig", ".config/git")

# Language runtimes, version managers, and user-level CLI installs live beneath
# home dotdirs. A confined command's interpreter and its shared libraries (a
# uv-managed python whose real binary + libpython sit under ~/.local/share/uv, the
# standalone `claude` binary under ~/.local/share/claude, node under ~/.nvm) must
# be READABLE or execvp fails with "Operation not permitted" and NOTHING runs --
# the default dotfile deny would otherwise turn every in-scope validate/exec and
# `claude -p` step into an OS-level EPERM. These grants are kept deliberately
# BELOW each manager root: opening all of ~/.local would also expose unrelated
# application state and defeat the fail-closed dotfile backstop. Config/state
# siblings (e.g. ~/.cargo/credentials) are NOT listed and stay denied, and the
# secret registry is still emitted LAST so it wins over any of these allows.
_HOME_RUNTIME_READ_ALLOW_RELS: tuple[str, ...] = (
    ".agents/skills",  # shared, credential-free Codex skill discovery root
    ".local/bin",  # user-level launchers/shims (e.g. the `claude` symlink)
    ".local/share/claude",  # standalone Claude Code binary + app bundle
    ".local/share/uv",  # uv-managed python interpreters + libpython + stdlib
    ".nvm/versions",  # node version manager runtimes
    ".pyenv/shims",  # pyenv-managed python
    ".pyenv/versions",
    ".rbenv/shims",  # rbenv-managed ruby
    ".rbenv/versions",
    ".rustup/toolchains",  # rust toolchains (.cargo holds creds -> excluded)
    ".asdf/shims",  # asdf multi-language runtimes
    ".asdf/installs",
)


def home_dotfile_read_allow_roots(write_roots: list[str]) -> list[str]:
    """Read allowlist layered over the home-dotfile default deny.

    Re-open the same roots already trusted for writes (workspace plus explicitly
    granted CLI state/cache roots), the credential-free language-runtime roots a
    confined command needs to execute at all, and the minimal benign git-config
    exceptions. The secret registry is still applied after this allow, so these
    re-openings can never expose a registered credential store.
    """
    home = os.path.expanduser("~")
    roots = (
        list(write_roots)
        + [os.path.join(home, rel) for rel in _HOME_RUNTIME_READ_ALLOW_RELS]
        + [os.path.join(home, rel) for rel in _BENIGN_HOME_DOTFILE_READS]
    )
    seen: list[str] = []
    for root in roots:
        resolved = _realpath(root)
        if resolved and resolved not in seen:
            seen.append(resolved)
    return seen


def secret_read_deny_roots() -> list[str]:
    """Directories a confined subprocess must NEVER be able to READ (AC-policy #B2a).

    Derived from the ONE shared secret registry (``omniagentos.policy.secrets``) so
    the OS-sandbox deny-list can never again be a SUBSET of the classifier's set
    (fix4 BLOCKER 1: previously this returned only omni/.aws/.gnupg while the
    classifier also flagged ~/.ssh and ~/.config/gcloud, so a sub-CLI read of
    ~/.ssh/id_rsa was denied by neither layer). Now both layers cover exactly the
    shared registry (``policy/secrets.py`` -> ``SECRET_DIR_RELS``): ~/.config/omni,
    ~/.ssh, ~/.aws, ~/.config/gcloud, ~/.gnupg, ~/.docker, ~/.kube, ~/.config/gh,
    and <repo>/var/secrets.

    SAFE to deny: the broker resolves credentials from ``os.environ`` in the PARENT
    process, never from a sandboxed subprocess reading the file, so denying the read
    stops `cat connections.env` from shipping live keys into the model context."""
    from omniagentos.policy.secrets import secret_dirs

    return secret_dirs()


def _literal_relative_parts(secret_dir: str, resolved_root: str) -> tuple[str, ...] | None:
    """STRING-based fallback for when inode resolution of ``secret_dir`` relative to
    ``resolved_root`` cannot positively PROVE containment (Gemini review round 1,
    finding 1 BLOCKER).

    ``inode_relative_parts_anchored`` proves containment by walking real inodes, so
    it needs at least one lstat-able node between ``secret_dir`` and ``resolved_root``
    to anchor on. That is normally the write root itself — but if the anchor node it
    finds turns out to be an UNSTATABLE (broken-symlink) node, or any other inode
    lookup along the way raises something other than "missing", the walk returns
    ``None`` -- indistinguishable, to the caller, from "genuinely not nested". A
    REGISTERED secret dir being simply ABSENT at sandbox-build time must never make
    its ancestor-rename deny silently vanish from the profile, so ``secret_dir`` and
    ``resolved_root`` (both already ``_realpath``-normalized by the caller) are
    compared here as plain strings and, if ``secret_dir`` is textually nested under
    ``resolved_root``, its components are derived directly from the configured path
    -- fail CLOSED (an extra deny is always safe) rather than fail open (a silently
    omitted one is not).
    """
    try:
        rel = os.path.relpath(secret_dir, resolved_root)
    except ValueError:
        # No common drive/anchor (only possible on non-POSIX paths) -- unrelated.
        return None
    if rel in (os.curdir, ""):
        return ()
    if rel.startswith(os.pardir) or os.path.isabs(rel):
        # ``..``-prefixed or still absolute: secret_dir is not textually inside
        # resolved_root, so there is nothing to protect against a parent rename.
        return None
    return tuple(part for part in rel.split(os.sep) if part)


def secret_store_write_deny_targets(
    write_roots: list[str],
) -> tuple[list[str], list[str]]:
    """WRITE denials protecting the registered secret stores (AC-policy P0).

    Returns ``(subpath_denies, ancestor_literals)``:

    * ``subpath_denies`` — every registered secret dir. Reading them was already
      denied; a store that sits INSIDE the writable workspace (``<repo>/var/secrets``)
      was still WRITABLE, so a confined command could overwrite/plant credential
      material or truncate the session token. Denied as a subpath (covers the dir
      and everything under it, including a rename of the dir itself).
    * ``ancestor_literals`` — each directory strictly BETWEEN a granted write root
      and a secret dir nested inside it (e.g. ``<repo>/var`` for
      ``<repo>/var/secrets``). Renaming such an ancestor (``mv var var2``) would
      relocate the store past every path-based deny — a path-based deny that must
      survive a parent rename. A LITERAL (not subpath) deny blocks the rename/unlink
      of that node itself while files inside it stay writable (``<repo>/var/sessions``
      is untouched). The workspace root itself needs no literal deny: renaming it
      requires write on its parent, which is outside the sandbox and already denied.

    Ancestor derivation prefers the inode-verified walk
    (:func:`omniagentos.path_containment.inode_relative_parts_anchored`, which
    already tolerates a secret dir that does not exist yet by anchoring on its
    nearest EXISTING ancestor) and falls back to a literal string comparison
    (:func:`_literal_relative_parts`) only when that inode walk cannot positively
    resolve containment -- so a registered-but-currently-absent secret dir, or one
    behind an unstatable node, still contributes its ancestor-rename deny instead of
    being silently dropped from the profile (Gemini review round 1, finding 1).
    """
    secret_dir_list = [_realpath(d) for d in secret_read_deny_roots()]
    subpath_denies = list(dict.fromkeys(d for d in secret_dir_list if d))
    ancestor_literals: list[str] = []
    seen_roots: list[str] = []
    for root in write_roots:
        resolved_root = _realpath(root)
        if not resolved_root or resolved_root in seen_roots:
            continue
        seen_roots.append(resolved_root)
        for secret_dir in subpath_denies:
            parts = inode_relative_parts_anchored(secret_dir, resolved_root)
            if parts is None:
                # Inode resolution could not positively prove containment (either
                # genuinely unrelated, or inconclusive e.g. an unstatable node along
                # the way) -- fail CLOSED via the literal configured path instead of
                # silently omitting a real nested store's ancestor deny.
                parts = _literal_relative_parts(secret_dir, resolved_root)
            if parts is None or len(parts) < 2:
                # None: store not under this root. len<2: store is a DIRECT child
                # (its own subpath deny already blocks its rename; parent is root).
                continue
            for depth in range(1, len(parts)):
                ancestor = _realpath(os.path.join(resolved_root, *parts[:depth]))
                if ancestor and ancestor not in ancestor_literals:
                    ancestor_literals.append(ancestor)
    return subpath_denies, ancestor_literals


def config_write_deny_targets() -> tuple[list[str], list[str]]:
    """CLI config/hooks/MCP files whose WRITE is denied even inside a writable CLI
    state dir (AC-policy fix4 config-write BLOCKER).

    Returns ``(literal_files, subpath_dirs)``. The surrounding state dir stays
    writable (so the CLIs function) but these security-sensitive files -- which
    configure hooks, tool permissions, and MCP servers -- are denied, so a sub-CLI
    (or a classifier-missed session command) cannot plant a hook / auto-approve
    permission / malicious MCP server that disables the guardrail on the NEXT run.
    The deny rule is emitted AFTER the allow rules so it WINS (SBPL: last match)."""
    home = os.path.expanduser("~")

    def h(*parts: str) -> str:
        return _realpath(os.path.join(home, *parts))

    literals = [
        h(".claude", "settings.json"),  # hooks / permissions
        h(".claude", "settings.local.json"),  # hooks / permissions (local)
        h(".codex", "config.toml"),  # Codex config (MCP, approvals)
        h(".config", "codex", "config.toml"),
        h(".gemini", "settings.json"),  # Gemini config (MCP, tools)
        h(".config", "gemini", "settings.json"),
        h(".kimi", "config.toml"),  # Kimi/Moonshot config (providers)
        h(".config", "kimi", "config.toml"),
        h(".qwen", "settings.json"),  # Qwen config (providers, MCP, approvals)
    ]
    subpaths = [
        h(".claude", "hooks"),  # hook script dir
    ]
    return literals, subpaths


_PROJECT_CONFIG_WRITE_DENY_RELS: tuple[str, ...] = (
    ".claude/settings.json",
    ".claude/settings.local.json",
    ".mcp.json",
)
_PROJECT_CONFIG_WRITE_DENY_SUBPATH_RELS: tuple[str, ...] = (".claude/hooks",)


def _root_is_git_dir(resolved_root: str) -> bool:
    """True when a granted write root IS a git dir itself (the swarm worktree
    model grants the main repo's ``.git`` common dir so worker commits can
    write objects/refs). Detected by name (``.git``) or by the bare-repo
    marker layout (``HEAD`` file + ``objects`` dir), so a bare/renamed common
    dir is still covered."""
    if os.path.basename(resolved_root) == ".git":
        return True
    return os.path.isfile(os.path.join(resolved_root, "HEAD")) and os.path.isdir(
        os.path.join(resolved_root, "objects")
    )


def project_config_write_deny_targets(
    write_roots: list[str],
) -> tuple[list[str], list[str]]:
    """Config/hook/MCP write denials beneath every granted write root.

    A project-local Claude setting or MCP file persists into the operator's real
    project and can affect the next interactive session, so these targets remain
    denied even though their containing workspace is writable.

    A write root that is ITSELF a git dir (the swarm worktree model grants the
    main repo's ``.git`` common dir) additionally gets ``<root>/config``
    (literal) and ``<root>/hooks`` (subpath) denied: with the common dir
    writable a worker could otherwise plant a post-merge/post-checkout hook
    the coordinator later executes, or rewrite ``core.hooksPath``/aliases via
    ``.git/config`` — the exact hook-planting vector the CLI-state denies
    above close for ``~/.claude`` (security prerequisite of merge-model
    Phase 2).
    """
    literals: list[str] = []
    subpaths: list[str] = []
    seen_roots: list[str] = []
    for root in write_roots:
        resolved = _realpath(root)
        if not resolved or resolved in seen_roots:
            continue
        seen_roots.append(resolved)
        literals.extend(
            _realpath(os.path.join(resolved, rel)) for rel in _PROJECT_CONFIG_WRITE_DENY_RELS
        )
        subpaths.extend(
            _realpath(os.path.join(resolved, rel))
            for rel in _PROJECT_CONFIG_WRITE_DENY_SUBPATH_RELS
        )
        if _root_is_git_dir(resolved):
            literals.append(_realpath(os.path.join(resolved, "config")))
            subpaths.append(_realpath(os.path.join(resolved, "hooks")))
    return literals, subpaths


def git_dir_ref_write_deny_targets(
    write_roots: list[str],
) -> tuple[list[str], list[str]]:
    """Ref-tampering denials beneath every granted git-dir write root (M3,
    merge-model Phase 2).

    Returns ``(packed_refs_literals, refs_heads_subpaths)``. Granting the git
    COMMON dir lets a swarm worker write objects and its own worktree
    metadata — but an unrestricted grant would also let it move ``main`` (or
    a sibling run's branch) to an arbitrary commit, or rewrite
    ``packed-refs`` wholesale. These denies close that: the whole
    ``refs/heads`` subtree and the ``packed-refs`` literal are write-denied,
    and ONLY the run's own branch namespace is re-opened afterwards (see
    :func:`git_dir_ref_write_allow_subpaths`)."""
    literals: list[str] = []
    subpaths: list[str] = []
    seen: list[str] = []
    for root in write_roots:
        resolved = _realpath(root)
        if not resolved or resolved in seen:
            continue
        seen.append(resolved)
        if _root_is_git_dir(resolved):
            literals.append(_realpath(os.path.join(resolved, "packed-refs")))
            subpaths.append(_realpath(os.path.join(resolved, "refs", "heads")))
    return literals, subpaths


def git_dir_ref_write_allow_subpaths(write_roots: list[str]) -> list[str]:
    """Granted write roots that sit INSIDE a git-dir root's ``refs/heads``
    subtree — the run's own branch namespace
    (``<git dir>/refs/heads/swarm/<run_id>``), re-opened AFTER the
    :func:`git_dir_ref_write_deny_targets` denies (SBPL last match wins).

    Workers must still commit their OWN branch, and git's ref lockfiles
    (``<ref>.lock``) live beside the refs inside the namespace dir, so the
    re-allow is the namespace SUBTREE. ACCEPTED RESIDUAL: sibling refs of the
    same run inside that namespace stay mutually writable — the scheduler
    neutralizes this by merging the exact quality-gate-verified SHA, never
    the branch ref."""
    _, heads_dirs = git_dir_ref_write_deny_targets(write_roots)
    allows: list[str] = []
    for root in write_roots:
        resolved = _realpath(root)
        if not resolved or resolved in allows:
            continue
        if any(inode_relative_parts_anchored(resolved, heads) is not None for heads in heads_dirs):
            allows.append(resolved)
    return allows


def git_dir_state_write_deny_targets(
    write_roots: list[str],
) -> tuple[list[str], list[str]]:
    """Main-workspace git-STATE denials beneath every granted git-dir root
    (R1, merge-model Phase 2).

    Returns ``(literal_files, subpath_dirs)``. The M3 ref denies stopped a
    worker from moving ``main``, but the shared common dir left the main
    workspace's OWN state writable: ``<common>/HEAD`` (live-confirmed HEAD
    hijack — coordinator merges then advance a hijacked ref, defeating the
    SHA-merge protection), ``<common>/index``/``index.lock`` (staging-area
    poisoning of the coordinator's next commit), ``<common>/logs`` (reflog
    forgery), and sibling ``<common>/worktrees/<other-id>/`` gitdirs (HEAD
    hijack of another task's worktree). A worker in a LINKED worktree never
    needs any of these — its own HEAD/index/reflogs live under
    ``<common>/worktrees/<its-id>/``, re-opened by
    :func:`git_dir_worktree_write_allow_subpaths` (and its branch reflog by
    :func:`git_dir_reflog_write_allow_subpaths`)."""
    literals: list[str] = []
    subpaths: list[str] = []
    seen: list[str] = []
    for root in write_roots:
        resolved = _realpath(root)
        if not resolved or resolved in seen:
            continue
        seen.append(resolved)
        if _root_is_git_dir(resolved):
            literals.extend(
                _realpath(os.path.join(resolved, name)) for name in ("HEAD", "index", "index.lock")
            )
            subpaths.extend(
                _realpath(os.path.join(resolved, name)) for name in ("logs", "worktrees")
            )
    return literals, subpaths


def git_dir_worktree_write_allow_subpaths(write_roots: list[str]) -> list[str]:
    """Granted write roots that sit INSIDE a git-dir root's ``worktrees``
    subtree — the worker's OWN linked-worktree gitdir
    (``<common>/worktrees/<id>``, resolved by the scheduler via
    ``git rev-parse --absolute-git-dir`` and passed as an extra root at
    spawn), re-opened AFTER the :func:`git_dir_state_write_deny_targets`
    ``worktrees`` deny (SBPL last match wins). The worker's own
    HEAD/index/reflogs live there and every git operation inside its
    worktree needs them; sibling ``worktrees/<other-id>/`` dirs stay
    denied."""
    _, state_subpaths = git_dir_state_write_deny_targets(write_roots)
    worktree_dirs = [p for p in state_subpaths if os.path.basename(p) == "worktrees"]
    allows: list[str] = []
    for root in write_roots:
        resolved = _realpath(root)
        if not resolved or resolved in allows:
            continue
        if any(inode_relative_parts_anchored(resolved, wts) is not None for wts in worktree_dirs):
            allows.append(resolved)
    return allows


def git_dir_reflog_write_allow_subpaths(write_roots: list[str]) -> list[str]:
    """Reflog twins of the run's allowed branch namespaces: for every granted
    ``<git dir>/refs/heads/<ns>`` root, re-open
    ``<git dir>/logs/refs/heads/<ns>`` AFTER the ``logs`` subtree deny.

    Live-verified requirement: a commit in a linked worktree appends the
    branch reflog at ``<common>/logs/refs/heads/<branch>`` — with the whole
    ``logs`` subtree denied (:func:`git_dir_state_write_deny_targets`) and no
    re-allow, every worker ``git commit`` fails
    ("unable to append to logs/..."). ``logs/HEAD`` (the main workspace's
    reflog) and other runs' reflogs stay denied; same-run sibling reflogs
    share the namespace — the accepted residual already neutralized by
    SHA-merge."""
    _, heads_dirs = git_dir_ref_write_deny_targets(write_roots)
    allows: list[str] = []
    for root in write_roots:
        resolved = _realpath(root)
        if not resolved:
            continue
        for heads in heads_dirs:
            if inode_relative_parts_anchored(resolved, heads) is not None:
                git_dir = os.path.dirname(os.path.dirname(heads))  # strip refs/heads
                log_ns = os.path.join(git_dir, "logs", os.path.relpath(resolved, git_dir))
                if log_ns not in allows:
                    allows.append(log_ns)
    return allows


def hook_token_read_deny_root() -> str:
    """Directory holding every session's narrowly-scoped hook-eval credential
    (AC-policy hook-auth; see sessions.hook_token). Denied by default in EVERY
    sandbox profile below -- same treatment as ``secret_read_deny_roots()`` -- so a
    session can never read a SIBLING session's hook token even though reads are
    otherwise ``(allow default)``. The owning session's OWN file is re-opened
    per-profile via ``extra_read_allow`` (see ``session_hook_token_read_allow`` and
    ``SessionSupervisor._launch``). Deliberately a SEPARATE tree from
    ``var/secrets``: this credential authorizes ONLY hook-eval for its own
    session, never the control-plane token that authorizes every other mutating
    route."""
    from omniagentos.sessions.hook_token import HOOK_TOKENS_ROOT

    return _realpath(str(HOOK_TOKENS_ROOT))


def session_hook_token_read_allow(session_id: str) -> list[str]:
    """The ONE file re-opened for read in ``session_id``'s own sandbox profile:
    its own hook-eval credential (see ``hook_token_read_deny_root``). A literal,
    not a subpath, so re-opening it can never widen read access to any sibling
    file in the same denied directory."""
    from omniagentos.sessions.hook_token import hook_token_path

    return [_realpath(str(hook_token_path(session_id)))]


def ssh_key_read_deny_root() -> str:
    """Grant store denied wholesale in every sandbox profile.

    This is the metadata store under ``var/sessions/ssh-keys`` -- never ``~/.ssh``
    itself.  The existing shared secret registry continues to deny the latter,
    after which a session profile re-opens only a few literal credential files.
    """
    from omniagentos.sessions.ssh_keys import SSH_KEYS_ROOT

    return _realpath(str(SSH_KEYS_ROOT))


def _ssh_host_parts(entry: str) -> tuple[str | None, str]:
    user, separator, host = entry.rpartition("@")
    return (user if separator else None, host if separator else entry)


def _host_patterns_match(patterns: list[str], host: str) -> bool:
    """Apply OpenSSH Host positive/negated patterns to one granted alias."""
    matched = False
    lowered = host.casefold()
    for raw in patterns:
        negated = raw.startswith("!")
        pattern = raw[1:] if negated else raw
        if fnmatchcase(lowered, pattern.casefold()):
            if negated:
                return False
            matched = True
    return matched


def _expand_identity_file(raw: str, entry: str, home: str) -> str | None:
    """Expand only deterministic OpenSSH IdentityFile tokens; fail closed otherwise."""
    user, host = _ssh_host_parts(entry)
    expanded = raw.replace("%%", "\0")
    replacements = {"%d": home, "%h": host, "%r": user or os.environ.get("USER", "")}
    for token, value in replacements.items():
        expanded = expanded.replace(token, value)
    expanded = expanded.replace("\0", "%")
    if "%" in expanded or not expanded:
        return None
    expanded = os.path.expanduser(expanded)
    if not os.path.isabs(expanded):
        expanded = os.path.join(home, expanded)
    return _realpath(expanded)


def _path_is_within(path: str, root: str) -> bool:
    return inode_relative_parts_anchored(_realpath(path), _realpath(root)) is not None


def _ssh_identity_path_allowed(path: str, home: str) -> bool:
    """Never let IdentityFile syntax punch through a non-SSH secret boundary."""
    ssh_root = _realpath(os.path.join(home, ".ssh"))
    if _path_is_within(path, ssh_root):
        return True
    protected = [
        *(
            root
            for root in secret_read_deny_roots()
            if inode_relative_parts_anchored(_realpath(root), ssh_root) != ()
        ),
        hook_token_read_deny_root(),
        ssh_key_read_deny_root(),
    ]
    return not any(_path_is_within(path, root) for root in protected)


def _configured_identity_files(config: Path, grants: list[str], home: str) -> list[str]:
    """Return literal IdentityFile paths from matching Host blocks.

    ``Match`` and malformed lines are skipped fail-closed.  We intentionally do
    not grant config ``Include`` globs: opening a globbed subtree would violate the
    literal-only credential boundary.  Operators can keep the relevant Host and
    IdentityFile declarations in the main config.
    """
    try:
        lines = config.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []

    active = list(grants)  # options before the first Host block are global
    identities: list[str] = []
    for line in lines:
        try:
            parts = shlex.split(line, comments=True, posix=True)
        except ValueError:
            active = []
            continue
        if not parts:
            continue
        keyword = parts[0].casefold()
        if keyword == "host":
            patterns = parts[1:]
            active = [
                entry
                for entry in grants
                if patterns and _host_patterns_match(patterns, _ssh_host_parts(entry)[1])
            ]
            continue
        if keyword == "match":
            active = []
            continue
        if keyword != "identityfile" or len(parts) != 2:
            continue
        for entry in active:
            resolved = _expand_identity_file(parts[1], entry, home)
            if (
                resolved
                and _ssh_identity_path_allowed(resolved, home)
                and resolved not in identities
            ):
                identities.append(resolved)
    return identities


def session_ssh_credentials_read_allow(session_id: str) -> list[str]:
    """Literal SSH files re-opened for one session -- never the ``~/.ssh`` tree."""
    from omniagentos.sessions.ssh_keys import read_ssh_key_grant, ssh_key_grant_path

    home = _realpath(os.path.expanduser("~"))
    config = Path(home) / ".ssh" / "config"
    known_hosts = Path(home) / ".ssh" / "known_hosts"
    grants = read_ssh_key_grant(session_id)
    paths = [
        _realpath(str(config)),
        _realpath(str(known_hosts)),
        _realpath(str(ssh_key_grant_path(session_id))),
        *_configured_identity_files(config, grants, home),
    ]
    return list(dict.fromkeys(paths))


def network_rules(
    net_mode: str = DEFAULT_NET_MODE,
    loopback_ports: Sequence[int] = (),
) -> str:
    """SBPL egress directives for one profile, or ``""`` for the default mode.

    THE PLATFORM LIMIT, live-verified on macOS 25.3 (Darwin 25.3.0) and the reason
    this function looks the way it does: Seatbelt's ``remote ip`` HOST filter is a
    no-op. ``(deny network-outbound)`` followed by
    ``(allow network-outbound (remote ip "localhost:*"))`` re-opens **every**
    outbound TCP connection, not just loopback — a profile written that way would
    read as "loopback only" and enforce nothing. The numeric spellings that would
    fix it are rejected by the parser outright ("localhost in network address" for
    both ``(remote ip "127.0.0.1:*")`` and ``(remote tcp "127.0.0.1:*")``).

    What Seatbelt DOES enforce is the PORT half of the filter:
    ``(remote ip "localhost:56522")`` genuinely permits connections to port 56522
    and denies every other port, on every host. That single working primitive is
    what ``proxy`` mode is built on — deny everything, then re-open exactly the
    ephemeral loopback port the parent's allowlisting CONNECT proxy is listening
    on (see ``omniagentos.lease.proxy``). The child can then reach the proxy and
    nothing else, and domain policy is enforced in the proxy where it CAN be.

    Modes:

    ``open``
        Emit nothing. The historical, unchanged behavior: the model CLIs must
        reach their provider APIs and SBPL cannot filter by hostname, so egress
        is defended by making secrets unreadable instead (see the module
        docstring). Returning ``""`` is what keeps a default profile byte-identical.
    ``deny``
        ``(deny network*)`` and nothing else — the same directive
        ``reliability/sandbox.py`` already appends for self-improvement patch
        testing. There is DELIBERATELY no loopback re-allow: as established
        above, the only spelling of one that the parser accepts would silently
        allow all egress, and a guardrail that lies is worse than no guardrail.
        A lane that needs a loopback exception asks for ``proxy`` mode with the
        specific ports it needs.
    ``proxy``
        ``(deny network*)`` plus one ``allow network-outbound`` per port in
        ``loopback_ports``. An EMPTY port list therefore degrades to exactly
        ``deny`` — fail closed, never fail open.

        HONEST RESIDUAL, and it must not be oversold: because the host half of the
        filter does not work, the re-opened port is reachable on ANY host, not only
        on loopback. A child that WANTS to evade the allowlist can therefore
        connect to ``attacker.example:<proxy port>`` — and it knows that port,
        since we hand it over in ``HTTPS_PROXY``. So ``proxy`` mode is a real
        control over accidental and incidental egress (nothing reaches :443 or :80
        directly, and every deliberate provider call is allowlisted at the proxy),
        but it is NOT an exfiltration-proof boundary against a hostile sub-CLI.
        The defense against a hostile sub-CLI remains the one that already exists
        and does not depend on hostname filtering: the credentials are not there to
        steal (secret dirs are read-denied, ``_scrubbed_env`` is an allowlist, and
        the broker resolves real secrets only in the parent).

    Raises ``ValueError`` on an unknown mode: an unrecognized egress mode must
    abort profile construction (and, via ``wrap_command``, the launch) rather
    than silently fall back to the permissive default.
    """
    if net_mode not in NET_MODES:
        raise ValueError(f"unknown sandbox net_mode: {net_mode!r}")
    if net_mode == DEFAULT_NET_MODE:
        return ""
    lines = ["(deny network*)"]
    if net_mode == "proxy":
        seen: list[int] = []
        for raw in loopback_ports:
            port = int(raw)
            if not 1 <= port <= 65535:
                raise ValueError(f"loopback port out of range: {port}")
            if port not in seen:
                seen.append(port)
                lines.append(f'(allow network-outbound (remote ip "localhost:{port}"))')
    return "\n".join(lines) + "\n"


def _profile_for(
    write_roots: list[str],
    extra_read_allow: list[str] | None = None,
    *,
    ssh_key_grant_session_id: str | None = None,
    net_mode: str = DEFAULT_NET_MODE,
    net_loopback_ports: Sequence[int] = (),
    extra_write_deny_literals: Sequence[str] = (),
) -> str:
    seen: list[str] = []
    for root in write_roots:
        if root and root not in seen:
            seen.append(root)
    subpaths = "\n".join(f'  (subpath "{_sbpl_quote(r)}")' for r in seen)
    read_denies = "\n".join(
        f'(deny file-read* (subpath "{_sbpl_quote(r)}"))' for r in secret_read_deny_roots()
    )
    cfg_literals, cfg_subpaths = config_write_deny_targets()
    proj_literals, proj_subpaths = project_config_write_deny_targets(seen)
    config_write_denies = "\n".join(
        [f'(deny file-write* (literal "{_sbpl_quote(p)}"))' for p in cfg_literals + proj_literals]
        + [f'(deny file-write* (subpath "{_sbpl_quote(p)}"))' for p in cfg_subpaths + proj_subpaths]
    )
    # Merge-model Phase 2 (M3 + R1): a granted git COMMON dir keeps objects
    # writable, but refs/heads, packed-refs, AND the main workspace's git
    # state (HEAD, index, index.lock, logs/, worktrees/) are denied, after
    # which ONLY granted roots INSIDE those subtrees are re-opened: the run's
    # own swarm/<run_id> branch namespace (M3), the worker's OWN linked-
    # worktree gitdir under worktrees/ (R1), and the reflog twin of the
    # branch namespace under logs/ (worker commits append the branch reflog
    # there). Deny then allow, in that order — SBPL's last matching rule wins.
    ref_literals, ref_subpaths = git_dir_ref_write_deny_targets(seen)
    state_literals, state_subpaths = git_dir_state_write_deny_targets(seen)
    ref_allow_roots = git_dir_ref_write_allow_subpaths(seen)
    own_worktree_allow_roots = git_dir_worktree_write_allow_subpaths(seen)
    reflog_allow_roots = git_dir_reflog_write_allow_subpaths(seen)
    git_ref_write_rules = "\n".join(
        [f'(deny file-write* (literal "{_sbpl_quote(p)}"))' for p in ref_literals + state_literals]
        + [
            f'(deny file-write* (subpath "{_sbpl_quote(p)}"))'
            for p in ref_subpaths + state_subpaths
        ]
        + [
            f'(allow file-write* (subpath "{_sbpl_quote(p)}"))'
            for p in ref_allow_roots + own_worktree_allow_roots + reflog_allow_roots
        ]
    )
    home_dotfile_read_deny = f'(deny file-read* (regex #"{home_dotfile_read_deny_pattern()}"))'
    home_dotfile_read_allows = "\n".join(
        f'(allow file-read* (subpath "{_sbpl_quote(root)}"))'
        for root in home_dotfile_read_allow_roots(seen)
    )
    # Node/realpath walks lstat EVERY path component, and the dotfile deny regex
    # covers the intermediate dirs themselves (e.g. ~/.nvm when only
    # ~/.nvm/versions is re-opened) — grant METADATA ONLY (no content reads) on
    # those intermediate components so runtimes under dotfile dirs can resolve.
    _home_root = _realpath(os.path.expanduser("~"))
    _meta_parents: list[str] = []
    for _allow_root in home_dotfile_read_allow_roots(seen):
        _parent = os.path.dirname(_allow_root)
        while inode_relative_parts_anchored(_parent, _home_root) not in (None, ()):
            if _parent not in _meta_parents:
                _meta_parents.append(_parent)
            _parent = os.path.dirname(_parent)
    if _meta_parents:
        home_dotfile_read_allows += "\n" + "\n".join(
            f'(allow file-read-metadata (literal "{_sbpl_quote(p)}"))' for p in _meta_parents
        )
    # AC-policy hook-auth: deny the whole hook-token store by default (same tier as
    # a secret dir), then re-open ONLY the caller's own file, if any was granted.
    # Emitted AFTER read_denies and evaluated last (SBPL: last match wins), so a
    # per-session literal allow here overrides the directory-level deny above it.
    hook_token_deny = f'(deny file-read* (subpath "{_sbpl_quote(hook_token_read_deny_root())}"))'
    hook_token_allows = "\n".join(
        f'(allow file-read* (literal "{_sbpl_quote(p)}"))' for p in (extra_read_allow or [])
    )
    # Registered secret stores are WRITE-denied too (not only read-denied): a store
    # inside the workspace (<repo>/var/secrets) must not be writable, and each
    # ancestor dir between the workspace and a nested store is literal-denied so a
    # parent rename (`mv var var2`) cannot relocate it past the path-based deny.
    secret_subpath_denies, secret_ancestor_literals = secret_store_write_deny_targets(seen)
    secret_write_denies = "\n".join(
        [f'(deny file-write* (subpath "{_sbpl_quote(p)}"))' for p in secret_subpath_denies]
        + [f'(deny file-write* (literal "{_sbpl_quote(p)}"))' for p in secret_ancestor_literals]
    )
    ssh_key_deny = f'(deny file-read* (subpath "{_sbpl_quote(ssh_key_read_deny_root())}"))'
    ssh_key_write_deny = f'(deny file-write* (subpath "{_sbpl_quote(ssh_key_read_deny_root())}"))'
    ssh_key_allows = "\n".join(
        f'(allow file-read* (literal "{_sbpl_quote(p)}"))'
        for p in (
            session_ssh_credentials_read_allow(ssh_key_grant_session_id)
            if ssh_key_grant_session_id
            else []
        )
    )
    return (
        "(version 1)\n"
        "(allow default)\n"
        "(deny file-write*)\n"
        "(allow file-write*\n"
        f"{subpaths}\n"
        '  (literal "/dev/null")\n'
        '  (literal "/dev/zero")\n'
        '  (literal "/dev/dtracehelper")\n'
        '  (literal "/dev/tty")\n'
        '  (literal "/dev/stdout")\n'
        '  (literal "/dev/stderr")\n'
        '  (regex #"^/dev/fd/[0-9]+$")\n'
        '  (regex #"^/dev/ttys[0-9]+$"))\n'
        # CLI config/hooks/MCP files are write-denied AFTER the allow so this WINS
        # (SBPL: last matching rule) even though the state dir around them is
        # writable -- no planting a guardrail-disabling hook/MCP for the next run.
        f"{config_write_denies}\n"
        # Git-dir ref + state confinement (M3/R1): refs/heads, packed-refs,
        # HEAD, index(.lock), logs/ and worktrees/ denied, then only the run's
        # own branch namespace, the worker's own linked-worktree gitdir, and
        # the namespace's reflog twin re-opened (order matters).
        f"{git_ref_write_rules}\n"
        # Reads under any $HOME dotfile fail closed. Re-open only already-granted
        # roots plus benign git config. A workspace nested below a home dotdir is
        # therefore still usable.
        f"{home_dotfile_read_deny}\n"
        f"{home_dotfile_read_allows}\n"
        # Secret stores are read-denied LAST so this overrides `(allow default)`.
        # It also wins over the preceding allow when var/secrets is in a workspace.
        f"{read_denies}\n"
        # Hook-token store: deny-by-default, then this ONE profile's own re-allow
        # (empty for a non-session command, e.g. the runner's own validate/effect
        # probes -- they never carry a hook token to re-open in the first place).
        f"{hook_token_deny}\n"
        f"{hook_token_allows}\n"
        # SSH grant metadata is also deny-by-default.  A session gets only its
        # own grant plus config, known_hosts, and matching IdentityFile literals.
        # These narrow literal exceptions intentionally follow the ~/.ssh secret
        # directory deny; no var/secrets path is ever re-opened.
        f"{ssh_key_deny}\n"
        # A workspace rooted above var/sessions must not let the agent rewrite
        # its own allowlist (or a sibling's) and self-grant another destination.
        f"{ssh_key_write_deny}\n"
        f"{ssh_key_allows}\n"
        # Registered secret stores: WRITE-denied (subpath) and their in-workspace
        # ancestors literal-denied, emitted after every write-allow so they WIN
        # (SBPL last match). Closes the writable-store gap and the parent-rename
        # relocation (fix4 + fix7). EMPTY string collapses to a harmless blank line.
        f"{secret_write_denies}\n"
        # Caller-supplied write denies, emitted AFTER every allow so they WIN.
        # EMPTY by default, so a call that does not pass them is byte-identical.
        # The autonomy lease uses this to make its OWN config file unwritable by a
        # leased child whose workspace happens to be this repository: without it,
        # one agent could edit configs/lease.yaml and disable or widen the lease
        # for every subsequent launch (security review BLOCKER #4). Deliberately
        # a PARAMETER rather than a hardcoded rel in the project deny list, so the
        # default profile keeps byte-for-byte parity and only a lease-enabled
        # launch pays for the extra directive.
        + "".join(
            f'(deny file-write* (literal "{_sbpl_quote(_realpath(p))}"))\n'
            for p in extra_write_deny_literals
            if p
        )
        # Egress directives LAST (SBPL last-match-wins), and EMPTY for the default
        # "open" mode -- a default profile is byte-identical to every profile this
        # module built before the parameter existed. See network_rules().
        + network_rules(net_mode, net_loopback_ports)
    )


def build_profile(
    workspace_dir: str,
    extra_write_roots: list[str] | None = None,
    *,
    extra_read_allow: list[str] | None = None,
    ssh_key_grant_session_id: str | None = None,
    workspace_writable: bool = True,
    net_mode: str = DEFAULT_NET_MODE,
    net_loopback_ports: Sequence[int] = (),
    extra_write_deny_literals: Sequence[str] = (),
) -> str:
    """SBPL profile: allow reads/exec/network, DENY every file write except inside
    the (realpath-resolved) workspace + any explicitly allowed extra roots + a few
    harmless dev sinks.

    Deliberately STRICT -- the system temp dir is NOT a runner write root; the
    runner redirects a command's TMPDIR into the workspace instead (see
    core._run_command). The session path passes the CLI's own state dirs as extra
    roots (see session_write_roots). Paths are realpath-resolved because SBPL
    matches the resolved path (``/tmp`` -> ``/private/tmp`` on macOS).

    ``extra_read_allow`` re-opens individual files denied by a directory-level
    read deny (currently only the hook-token store -- see
    ``session_hook_token_read_allow``); it does NOT affect writes.

    ``workspace_writable=False`` builds a READ-ONLY-workspace profile: the
    workspace is NOT granted as a write root, only ``extra_write_roots`` (the
    CLI state/config dirs a sub-CLI genuinely needs) are. Used for read-only
    adapter invocations (e.g. the swarm reviewer) whose inner CLI sandbox is
    disabled under the outer wrap -- the outer profile must then be the layer
    that keeps the workspace unwritable.

    ``net_mode`` (``open``/``deny``/``proxy``, default ``open``) selects the egress
    directives appended at the very end of the profile; ``net_loopback_ports`` is
    the port allowlist ``proxy`` mode re-opens. The DEFAULT emits nothing, so a
    call that does not pass these produces a byte-identical profile — that
    equality is asserted by a regression test. See :func:`network_rules` for the
    macOS filter limitation that shapes both non-default modes.
    """
    roots = [_realpath(workspace_dir)] if workspace_writable else []
    for extra in extra_write_roots or []:
        roots.append(_realpath(extra))
    return _profile_for(
        roots,
        extra_read_allow=extra_read_allow,
        ssh_key_grant_session_id=ssh_key_grant_session_id,
        net_mode=net_mode,
        net_loopback_ports=net_loopback_ports,
        extra_write_deny_literals=extra_write_deny_literals,
    )


def claude_tmp_root() -> str:
    """The dir Claude Code's Bash tool uses for its own scratch (``/tmp/claude-<uid>``).

    Verified against a live session: every Bash command writes here, so omitting it
    made EVERY unattended Bash call fail EPERM. The supervisor/adapter os.makedirs
    this before launch so a fresh host does not need /tmp itself writable."""
    try:
        uid = os.getuid()
    except AttributeError:  # pragma: no cover - non-POSIX
        uid = 0
    return _realpath(f"/tmp/claude-{uid}")


# CLI-specific cache subdirs (AC-policy fix4 LOW #6): the node/CLI caches the
# subscription CLIs write, scoped so a sub-CLI cannot write the WHOLE ``~/.cache``
# (which also holds unrelated app data: huggingface, gh, puppeteer, ...) or the
# whole ``~/.npm``. npm's write surface is its ``_`` subdirs (cache/logs/npx).
_CLI_CACHE_RELS = (
    ".cache/claude",
    ".cache/claude-cli-nodejs",
    ".cache/node",
    ".cache/codex",
    ".cache/codex-runtimes",
    ".cache/gemini",
    ".cache/kimi",
    ".cache/grok",
    ".cache/qwen",
    ".cache/vscode-ripgrep",
    ".npm/_cacache",
    ".npm/_logs",
    ".npm/_npx",
)


def session_write_roots(project_dir: str) -> list[str]:
    """Write roots for a bridged Claude session: the project + the CLI's own state
    dirs it legitimately needs (incl. Claude Code's /tmp/claude-<uid> Bash scratch).

    Scoped to EXACTLY the CLI state dirs (``~/.claude`` and friends) and the
    CLI-specific cache subdirs, NOT the whole home, NOT the whole ~/.cache or
    ~/.npm, and NOT the whole system temp -- so ~/.ssh, ~/.zshrc, other projects and
    system dirs remain unwritable even if the classifier ever misses a command.
    Security-sensitive config/hook/MCP files inside these dirs are additionally
    write-denied by the profile (config_write_deny_targets)."""
    home = os.path.expanduser("~")
    roots = [_realpath(project_dir), claude_tmp_root()]
    for rel in (".claude", ".claude.json", ".config/claude", *_CLI_CACHE_RELS):
        roots.append(_realpath(os.path.join(home, rel)))
    return roots


def adapter_write_roots(provider: str | None = None, config_dir: str | None = None) -> list[str]:
    """Write roots for a runner AGENT sub-CLI (kimi/gemini/codex/claude/grok...).

    The sub-CLI's own state dirs + Claude Code's Bash scratch + CLI-specific cache
    subdirs. Deliberately does NOT include the whole ``~/.config`` (that holds
    ``~/.config/omni`` secrets), the whole ``~/.cache``, or the whole ``~/.npm``, so
    a force-auto CLI (kimi) cannot write outside its state/cache dirs. The working
    dir is the primary write root, passed separately to wrap_command. Config/hook/
    MCP files inside these dirs are additionally write-denied by the profile.

    ``provider`` scopes a WP3 provider-exec profile to that CLI's state only.
    This is also the read allowlist layered over the home-dotfile deny, so other
    providers' credential/config directories remain unreadable.  The no-provider
    form preserves the broader legacy adapter-direct behavior.
    """
    home = os.path.expanduser("~")
    roots = [claude_tmp_root()]
    provider_rels: dict[str, tuple[str, ...]] = {
        "claude": (
            ".claude",
            # CLI-owned mutable state (sessions/cache/auth refresh), not hooks/permissions.
            ".claude.json",
            ".config/claude",
        ),
        "codex": (".codex", ".config/codex"),
        "grok": (".grok",),
        "gemini": (".gemini", ".config/gemini"),
        "kimi": (".kimi-code", ".kimi", ".moonshot", ".config/kimi"),
        "qwen": (".qwen",),
    }
    if provider is None:
        rels: tuple[str, ...] = (
            ".kimi-code",
            ".kimi",
            ".moonshot",
            ".codex",
            ".claude",
            ".claude.json",
            ".gemini",
            ".grok",
            ".qwen",
            ".config/claude",
            ".config/codex",
            ".config/kimi",
            ".config/gemini",
            *_CLI_CACHE_RELS,
        )
    else:
        cache_rels = tuple(
            rel
            for rel in _CLI_CACHE_RELS
            if rel.startswith(f".cache/{provider}")
            or rel.startswith(".npm/")
            or rel == ".cache/node"
            or rel == ".cache/vscode-ripgrep"
        )
        rels = (*provider_rels.get(provider, ()), *cache_rels)
    for rel in rels:
        roots.append(_realpath(os.path.join(home, rel)))
    if config_dir:
        roots.append(_realpath(os.path.expanduser(config_dir)))
    return roots


@lru_cache(maxsize=1)
def sandbox_available() -> bool:
    """True only when ``sandbox-exec`` exists AND a live self-test proves it blocks
    a write OUTSIDE the allowed workspace. Cached: probed once per process."""
    if os.name != "posix" or not os.path.exists(_SANDBOX_EXEC):
        return False
    try:
        with tempfile.TemporaryDirectory() as base:
            real_base = _realpath(base)
            ws = os.path.join(real_base, "ws")
            os.mkdir(ws)
            profile = _profile_for([ws])  # ONLY the workspace is writable
            victim = os.path.join(real_base, "should_not_write.txt")  # sibling, not allowed
            subprocess.run(
                [_SANDBOX_EXEC, "-p", profile, "/bin/sh", "-c", f"echo x > {victim}"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if os.path.exists(victim):
                return False  # the sandbox did not actually confine -- do not claim it
            keeper = os.path.join(ws, "ok.txt")
            subprocess.run(
                [_SANDBOX_EXEC, "-p", profile, "/bin/sh", "-c", f"echo x > {keeper}"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            return os.path.exists(keeper)
    except Exception:  # noqa: BLE001 -- any probe failure means "do not claim it".
        return False


def wrap_available(argv: list[str], workspace_dir: str | None) -> bool:
    """True when ``wrap_command`` would actually wrap this argv in the OS sandbox
    (workspace known, program resolvable, sandbox available and proven).

    This predicate is LOAD-BEARING for the inner-sandbox disable of the
    self-sandboxing CLIs (see ``omniagentos.adapters.common``): a CLI's own
    ``--sandbox`` may only be disabled when the outer Seatbelt profile will
    actually engage — otherwise the CLI's own sandbox is the sole confinement
    and must be preserved."""
    if not workspace_dir or not argv:
        return False
    # Resolvability check FIRST (cheap, no subprocess): sandbox-exec needs to resolve
    # the program, and skipping unresolvable programs also avoids probing the sandbox
    # for a command that will not run anyway.
    if shutil.which(argv[0]) is None and not Path(argv[0]).exists() and "/" not in argv[0]:
        return False
    return sandbox_available()


def wrap_command(
    argv: list[str],
    workspace_dir: str | None,
    *,
    extra_write_roots: list[str] | None = None,
    extra_read_allow: list[str] | None = None,
    ssh_key_grant_session_id: str | None = None,
    workspace_writable: bool = True,
    net_mode: str = DEFAULT_NET_MODE,
    net_loopback_ports: Sequence[int] = (),
    extra_write_deny_literals: Sequence[str] = (),
) -> list[str]:
    """Return argv wrapped in ``sandbox-exec`` confining writes to ``workspace_dir``
    (+ any ``extra_write_roots``), with ``extra_read_allow`` re-opening individual
    denied files (see ``build_profile``). ``workspace_writable=False`` keeps the
    wrap but does NOT grant the workspace as a write root (read-only adapter
    invocations -- see ``build_profile``).

    Returns argv unchanged when no workspace is known, argv is empty, the program
    cannot be resolved, or the OS sandbox is not available/proven (fallback to
    classifier + keyless-env guarantees) — exactly the ``wrap_available``
    conditions.

    ``net_mode``/``net_loopback_ports`` are forwarded to :func:`build_profile`;
    both default to the historical no-op, so an existing call site is unchanged.
    NOTE the interaction with the unwrapped fallback: when the sandbox is
    unavailable the argv is returned bare and a requested ``deny``/``proxy``
    egress policy is NOT applied. Callers that depend on egress confinement must
    therefore check :func:`wrap_available` themselves and refuse the launch — the
    lease seam does exactly that (``omniagentos.lease.seam``)."""
    if not workspace_dir or not wrap_available(argv, workspace_dir):
        return argv
    profile = build_profile(
        workspace_dir,
        extra_write_roots,
        extra_read_allow=extra_read_allow,
        ssh_key_grant_session_id=ssh_key_grant_session_id,
        workspace_writable=workspace_writable,
        net_mode=net_mode,
        net_loopback_ports=net_loopback_ports,
        extra_write_deny_literals=extra_write_deny_literals,
    )
    return [_SANDBOX_EXEC, "-p", profile, *argv]


__all__ = [
    "DEFAULT_NET_MODE",
    "NET_MODES",
    "adapter_write_roots",
    "build_profile",
    "claude_tmp_root",
    "config_write_deny_targets",
    "git_dir_ref_write_allow_subpaths",
    "git_dir_ref_write_deny_targets",
    "git_dir_reflog_write_allow_subpaths",
    "git_dir_state_write_deny_targets",
    "git_dir_worktree_write_allow_subpaths",
    "home_dotfile_read_allow_roots",
    "home_dotfile_read_deny_pattern",
    "hook_token_read_deny_root",
    "network_rules",
    "project_config_write_deny_targets",
    "sandbox_available",
    "secret_read_deny_roots",
    "secret_store_write_deny_targets",
    "session_hook_token_read_allow",
    "session_ssh_credentials_read_allow",
    "session_write_roots",
    "ssh_key_read_deny_root",
    "wrap_available",
    "wrap_command",
]
