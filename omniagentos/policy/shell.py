"""The single, shared shell-command classifier (AC-policy).

ONE function -- ``classify_shell(command, project_dir)`` -- is the sole source of
truth for "what class of action is this shell command?", used by BOTH the Session
Bridge hook (``omniagentos.sessions.policy_map.classify_tool`` for its ``Bash``
branch) AND the runner's command gate
(``omniagentos.runner.core.Runner._command_action_class``). Unifying them removed
an entire class of guardrail bypass ("safe in one gate, dangerous in the other").

DENY-BY-DEFAULT. The classifier is a *positive allowlist*, not a denylist:

  * A small set of *provably* read-only commands (ls/cat/grep/rg/git-read/
    find-read-only/pwd/echo/head/tail/wc/...) classify READ_ONLY (auto).
  * A cp/mv/tee/install/sort-o/uniq-out/`>` whose EVERY write target resolves
    inside the project (absolute realpath, or relative resolved against the
    project) is an in-scope write -> INTERNAL_REVERSIBLE (auto).
  * EVERYTHING ELSE -- interpreters, any delete/destroy primitive, command
    substitution / $VAR-as-executable, ANY write target that escapes the project
    or cannot be proven in-scope, or an unrecognised command -- classifies
    IRREVERSIBLE, the ONE class that hard-stops in AUTO mode.

Redirects are parsed by an explicit, quote-aware lexer (``_lex``) rather than a
regex, because the shell redirect grammar (``&>``, ``>&``, ``2>&1``, ``N>``,
``>|`` ...) is exactly where a regex silently lets a truncating write slip past
(AC-policy fix2 / reviewer B1-B3).
"""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path
from typing import Any

from omniagentos.contracts import ActionClass
from omniagentos.path_containment import inode_relative_parts_anchored
from omniagentos.policy.secrets import references_secret as _references_secret

# Rank so a compound/pipeline command takes its MOST dangerous segment's class.
_RANK: dict[ActionClass, int] = {
    ActionClass.READ_ONLY: 0,
    ActionClass.SANDBOXED_CREATION: 1,
    ActionClass.INTERNAL_REVERSIBLE: 2,
    ActionClass.EXTERNAL_REVERSIBLE: 3,
    ActionClass.CONSEQUENTIAL: 4,
    ActionClass.IRREVERSIBLE: 5,
}


# --- interpreters: argv[0] that can run arbitrary code -----------------------
_INTERPRETERS = frozenset(
    {
        "sh",
        "bash",
        "zsh",
        "dash",
        "ksh",
        "csh",
        "tcsh",
        "fish",
        "ash",
        "python",
        "python2",
        "python3",
        "pypy",
        "pypy3",
        "node",
        "nodejs",
        "deno",
        "bun",
        "ts-node",
        "tsx",
        "perl",
        "perl5",
        "ruby",
        "php",
        "lua",
        "luajit",
        "tclsh",
        "rscript",
        "osascript",
        "expect",
        "groovy",
        "scala",
        "env",
        "eval",
        "exec",
        "source",
        "xargs",
        "parallel",
        "awk",
        "gawk",
        "nawk",
        "mawk",
        "sed",
        "sudo",
        "doas",
        "nohup",
        "setsid",
        "time",
        "watch",
        "nice",
        "ionice",
        "ssh",
        "telnet",
    }
)

# --- provably read-only commands (no write mode) -----------------------------
# NB: Tools with even one mutating mode are NOT here. sort/uniq/file/sips/
# identify are handled explicitly so their arguments are checked before a
# read-only verdict (reviewer B1).
_READ_ONLY_SIMPLE = frozenset(
    {
        "ls",
        "cat",
        "grep",
        "egrep",
        "fgrep",
        "pwd",
        "echo",
        "printf",
        "head",
        "tail",
        "wc",
        "cut",
        "tr",
        "nl",
        "od",
        "hexdump",
        "test",
        "[",
        "true",
        "false",
        "basename",
        "dirname",
        "realpath",
        "readlink",
        "stat",
        "date",
        "whoami",
        "id",
        "hostname",
        "uname",
        "diff",
        "cmp",
        "column",
        # Media inspection (L1 / AUTO-APPROVE): read metadata, never mutate.
        "ffprobe",
        "mdls",
        "md5",
        "shasum",
        "sha256sum",
    }
)
_READ_ONLY_GIT_SUBCOMMANDS = frozenset(
    {
        "status",
        "log",
        "diff",
        "show",
        "branch",
        "remote",
        "rev-parse",
        "rev-list",
        "describe",
        "ls-files",
        "ls-tree",
        "cat-file",
        "blame",
        "shortlog",
        "whatchanged",
        "name-rev",
        "symbolic-ref",
        "config",  # read when no write; write still local via _git_is_local_write
    }
)
_GIT_UNSAFE_TOKENS = frozenset(
    {"-o", "-O", "--ext-diff", "-c", "-C", "--exec-path", "--upload-pack"}
)
_GIT_BRANCH_DESTRUCTIVE_OPTIONS = frozenset({"-d", "-D", "--delete", "-f", "--force", "-C", "-M"})
_GIT_CHECKOUT_DESTRUCTIVE_OPTIONS = frozenset({"-B", "-f", "--force"})
_GIT_SWITCH_DESTRUCTIVE_OPTIONS = frozenset(
    {"-C", "-f", "--discard-changes", "--force", "--force-create"}
)
_GIT_FORCE_OPTIONS = frozenset(
    {
        "-f",
        "--discard-changes",
        "--force",
        "--force-create",
        "--force-if-includes",
    }
)
_GIT_CONFIG_MUTATION_OPTIONS = frozenset(
    {
        "--add",
        "--edit",
        "-e",
        "--fixed-value",
        "--rename-section",
        "--remove-section",
        "--replace-all",
        "--unset",
        "--unset-all",
    }
)
_GIT_CONFIG_READ_ACTIONS = frozenset(
    {"--get", "--get-all", "--get-regexp", "--get-urlmatch", "--list", "-l"}
)
_GIT_CONFIG_OPTIONS_WITH_VALUE = frozenset({"--default", "--file", "-f", "--type"})
_GIT_CONFIG_SAFE_OPTIONS = frozenset(
    {
        "--global",
        "--includes",
        "--local",
        "--name-only",
        "--no-includes",
        "--null",
        "-z",
        "--show-names",
        "--show-origin",
        "--show-scope",
        "--system",
        "--worktree",
    }
)
_GIT_CONFIG_NONLOCAL_SCOPES = frozenset({"--global", "--system", "--file", "-f", "--blob"})
_FIND_READONLY_PREDICATES = frozenset(
    {
        "-name",
        "-iname",
        "-type",
        "-maxdepth",
        "-mindepth",
        "-path",
        "-ipath",
        "-wholename",
        "-iwholename",
        "-regex",
        "-iregex",
        "-prune",
        "-print",
        "-print0",
        "-empty",
        "-newer",
        "-size",
        "-mtime",
        "-mmin",
        "-ctime",
        "-atime",
        "-depth",
        "-follow",
        "-a",
        "-o",
        "-and",
        "-or",
        "-not",
        "-ls",
    }
)
_RG_SUBPROCESS_OPTIONS = frozenset({"--pre", "--pre-glob", "--search-zip", "-z", "--hostname-bin"})
_RG_UNSAFE_SHORT_FLAGS = frozenset("z")

# --- write commands whose destination is scope-analysable --------------------
_WRITE_COMMANDS = frozenset({"cp", "mv", "tee", "install", "ln", "rsync"})
# Create empty files/dirs: every path operand must resolve in-scope (AUTO-APPROVE 2c).
_SCOPED_CREATE_COMMANDS = frozenset({"mkdir", "touch"})
# Network fetch into a scoped file (L1): destination must be in-scope; pipes to
# shell remain irreversible via the |sh patterns elsewhere.
_SCOPED_FETCH_COMMANDS = frozenset({"curl", "wget"})
# uniq's flags that consume the following token (so it is not the output operand).
_UNIQ_ARG_FLAGS = frozenset({"-f", "-s", "-w", "--skip-fields", "--skip-chars", "--check-chars"})
_COPY_MOVE_SHORT_FLAGS: dict[str, frozenset[str]] = {
    "cp": frozenset("abdfHilLnpPrRsTuvx"),
    "mv": frozenset("bfinTuvZ"),
}
_COPY_MOVE_VALUE_SHORT_OPTIONS = frozenset({"S", "t"})
_COPY_MOVE_LONG_FLAGS: dict[str, frozenset[str]] = {
    "cp": frozenset(
        {
            "--archive",
            "--attributes-only",
            "--backup",
            "--dereference",
            "--force",
            "--interactive",
            "--link",
            "--no-clobber",
            "--no-dereference",
            "--no-target-directory",
            "--one-file-system",
            "--parents",
            "--preserve",
            "--preserve-links",
            "--recursive",
            "--remove-destination",
            "--symbolic-link",
            "--update",
            "--verbose",
        }
    ),
    "mv": frozenset(
        {
            "--backup",
            "--exchange",
            "--force",
            "--interactive",
            "--no-clobber",
            "--no-copy",
            "--no-target-directory",
            "--update",
            "--verbose",
        }
    ),
}
_COPY_MOVE_VALUE_LONG_OPTIONS = frozenset({"--suffix", "--target-directory"})
_COPY_MOVE_INLINE_LONG_OPTIONS = frozenset(
    {
        "--backup",
        "--context",
        "--no-preserve",
        "--preserve",
        "--reflink",
        "--sparse",
        "--update",
    }
)
_INSTALL_SHORT_FLAGS = frozenset("bCcdDpsvTZ")
_INSTALL_VALUE_SHORT_OPTIONS = frozenset({"g", "m", "o", "S", "t"})
_INSTALL_LONG_FLAGS = frozenset(
    {
        "--backup",
        "--compare",
        "--create-leading",
        "--directory",
        "--no-target-directory",
        "--preserve-timestamps",
        "--strip",
        "--verbose",
    }
)
_INSTALL_VALUE_LONG_OPTIONS = frozenset(
    {
        "--group",
        "--mode",
        "--owner",
        "--suffix",
        "--target-directory",
    }
)
_INSTALL_INLINE_LONG_OPTIONS = frozenset({"--backup", "--context"})

# Env prefixes that change *which binary runs* (AUTO-APPROVE 2a). Everything else
# (NODE_PATH, PYTHONPATH, LANG, TZ, CI, npm_config_*) is inert: strip and classify.
_EXEC_ALTERING_PREFIXES = frozenset(
    {
        "PATH",
        "LD_PRELOAD",
        "LD_LIBRARY_PATH",
        "DYLD_INSERT_LIBRARIES",
        "DYLD_LIBRARY_PATH",
        "NODE_OPTIONS",
        "PHPRC",
        "PHP_INI_SCAN_DIR",
        "PYTHONSTARTUP",
        "PYTHONHOME",
        "PYTHONINSPECT",
        "BASH_ENV",
        "ENV",
    }
)

# Git write subcommands that stay local (AUTO-APPROVE 2d). push/fetch stay out.
_GIT_LOCAL_WRITE_SUBCOMMANDS = frozenset(
    {
        "add",
        "commit",
        "checkout",
        "branch",
        "stash",
        "tag",
        "restore",
        "switch",
        "merge",
        "rebase",
        "cherry-pick",
        "reset",  # --hard still caught by _git_is_destructive
        "rm",  # git rm still destructive via _git_is_destructive
        "mv",
        "init",
        "config",  # local config only; --global is not path-scoped but rare in agent work
    }
)
_GIT_REMOTE_WRITE_SUBCOMMANDS = frozenset(
    {"push", "fetch", "pull", "clone", "remote", "submodule", "archive"}
)

# --- destructive / delete primitives (hard-stop) -----------------------------
_DESTRUCTIVE_COMMANDS = frozenset({"rm", "rmdir", "unlink", "shred", "mkfs", "truncate", "dd"})
_FIND_DELETE_EXEC = frozenset({"rm", "rmdir", "shred", "unlink"})
# Delete PAYLOADS: library calls and fork bombs that a lexer cannot see as an
# argv word because they live inside a quoted string. These stay an unconditional
# hard-stop.
#
# The plain shell forms ("rm -rf", "rm -f ", ...) that used to sit in this tuple
# were removed: a raw substring match cannot tell `rm -f ./build/tmp` (in scope,
# provable) from `rm -rf ~` (not), so EVERY delete hard-stopped regardless of
# where it pointed. _has_destructive_primitive + _scoped_delete_class now decide
# that precisely, on lexed operands. Nothing was widened by the removal: an
# interpreter payload (`bash -c "rm -rf /"`, `python -c ...`) is already
# IRREVERSIBLE via _INTERPRETERS before any operand is inspected.
_DELETE_SUBSTRINGS = (
    "shutil.rmtree",
    "os.remove",
    "os.unlink",
    "os.rmdir",
    ".unlink(",
    "rmsync",
    "unlinksync",
    "rmdirsync",
    "fs.rm(",
    ".rmtree(",
    ":(){",
    "rimraf",
)
# Interpreter payloads that still hard-stop even when interpreters are otherwise
# allowed for normal in-workspace work (full-auto). Covers classic RCE / shell-out
# patterns; ordinary `python script.py` / `node check.js` stay INTERNAL_REVERSIBLE.
_DANGEROUS_INTERPRETER_MARKERS = (
    "os.system",
    "subprocess.call",
    "subprocess.run",
    "subprocess.popen",
    "popen(",
    "pty.spawn",
    "socket.socket",
    "/dev/tcp",
    "base64 -d",
    "base64 --decode",
    "__import__",
    "ctypes.cdll",
    "eval(",
    "exec(",
)
_RSYNC_DELETE = ("--delete", "--del", "--delete-after", "--delete-before", "--delete-during")

_REDIRECT_SAFE_TARGETS = ("/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty")
_INDIRECTION_MARKERS = ("$(", "`", "${", "<(", ">(")

# --- literal shell-variable resolution ---------------------------------------
# `W=/abs/workspace` followed by `rm -f "$W/tmp"` is the single most common shape
# an agent writes, and it is provable: the value is a literal in the SAME command.
_ASSIGNMENT_RE = re.compile(r"\A([A-Za-z_][A-Za-z0-9_]*)=(.*)\Z", re.DOTALL)
_VAR_REF_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")
# A binding whose VALUE carries any of these is not a literal we will trust.
_UNSAFE_BINDING_CHARS = ("$", "`", "*", "?", "[", "~", "\n")
# A path operand carrying any of these can never be proven: `$`/backtick means an
# unresolved expansion, and a glob can match siblings the operand never named.
_UNPROVABLE_PATH_CHARS = ("$", "`", "*", "?", "[")

# Deletes we are willing to PROVE in-scope: a leading rm/rmdir/unlink whose every
# operand resolves inside the project. Everything else destructive (dd, mkfs,
# truncate, shred, find -delete, git rm, rsync --delete, a buried rm) is out of
# this shape and keeps hard-stopping.
_SCOPED_DELETE_COMMANDS = frozenset({"rm", "rmdir", "unlink"})
_SCOPED_DELETE_FLAGS = frozenset(
    {
        "-f",
        "-r",
        "-R",
        "-rf",
        "-fr",
        "-Rf",
        "-fR",
        "-d",
        "-v",
        "-rfv",
        "-fv",
        "--force",
        "--recursive",
        "--verbose",
        "--dir",
        "--",
    }
)

# Remote production commands align with the operator's policy: **AUTO-APPROVE EVERYTHING EXCEPT
# (a) file/data DELETION and (b) MONEY/finance.** Deployments, restarts, config edits,
# etc. auto-approve. Only deletion and money operations hard-stop.
_REMOTE_DESTRUCTIVE_KEYWORDS = frozenset(
    {
        # File/data deletion operations (hard-stop)
        "rm",
        "rmdir",
        "dd",
        "mkfs",
        "shred",
        "truncate",
        "truncate ",
        # SQL DELETE/DROP/TRUNCATE (hard-stop)
        "DROP",
        "TRUNCATE",
        "DELETE",
        # Git destructive operations (hard-stop)
        "git reset --hard",
        "git clean -fd",
        # Package removal (hard-stop)
        "apt remove",
        "apt-get remove",
        "yum remove",
        "dnf remove",
        "npm uninstall -g",
    }
)
_REMOTE_MONEY_KEYWORDS = frozenset(
    {"stripe", "payment", "invoice", "charge", "refund", "paypal", "nmi", "billing", "transactions"}
)

_SSH_OPTIONS_WITH_VALUE = frozenset({"-p", "-o"})
_SSH_SAFE_FLAG_OPTIONS = frozenset({"-4", "-6", "-C", "-n", "-q", "-T", "-v", "-vv", "-vvv", "-x"})
# Safe -o option values (connection-only, no code execution risk)
_SSH_SAFE_O_OPTIONS = frozenset(
    {
        "batchmode=yes",
        "batchmode=no",
        "connecttimeout=",
        "connecttimeout",
        "stricthostkeychecking=yes",
        "stricthostkeychecking=no",
        "stricthostkeychecking=accept-new",
        "identitiesonly=yes",
        "identitiesonly=no",
        "serveraliveinterval=",
        "compressionlevel=",
        "ciphers=",
        "macs=",
        "hostkeyalgorithms=",
        "pubkeyacceptedkeytypes=",
    }
)
_SCP_OPTIONS_WITH_VALUE = frozenset({"-P"})
_SCP_SAFE_FLAG_OPTIONS = frozenset({"-3", "-4", "-6", "-C", "-O", "-p", "-q", "-r", "-v"})
_RSYNC_SAFE_OPTIONS_WITH_VALUE = frozenset(
    {"--exclude", "--include", "--max-size", "--min-size", "--port", "--timeout"}
)
_RSYNC_SAFE_LONG_OPTIONS = frozenset(
    {
        "--archive",
        "--compress",
        "--human-readable",
        "--list-only",
        "--numeric-ids",
        "--recursive",
        "--verbose",
    }
)
_RSYNC_SAFE_SHORT_FLAGS = frozenset("avzrhqn")

# --- secret stores: reading these ships live credentials into model context ---
# (AC-policy #B2b / fix4) The secret registry is now SHARED with the native-tool
# classifier and the OS sandbox (``omniagentos.policy.secrets``), so no single
# layer can silently disagree about what a "secret" is. ``_references_secret`` is
# that module's resolver, imported above.

# Redirect operator tokens produced by the lexer. BOTH truncation AND append are
# scope-checked writes: appending an attacker key to an out-of-scope
# ~/.ssh/authorized_keys needs no truncation, so `>>`/`&>>` are writes too.
_FILE_WRITE_OPS = frozenset({">", ">|", "&>", ">>", "&>>"})
_INPUT_OPS = frozenset({"<", "<<", "<<<", "<&", "<>"})
_DUP_OR_REDIRECT = frozenset({">&"})  # >&N = dup(safe); >&file = write
_CONTROL_OPS = frozenset({"&&", "||", "|", "|&", "&", ";", "\n", "(", ")"})
# Quote provenance normally disappears when the lexer removes shell quotes. Keep
# single-quoted dollar signs distinguishable so the AWK positive grammar cannot
# mistake a double-quoted, shell-expanded ``$NF`` for literal AWK field syntax.
_LITERAL_DOLLAR_SENTINEL = "\ue000"


def _token_basename(token: str) -> str:
    return os.path.basename(token.strip("\"'")) or token


def _project_root(project_dir: str | None) -> Path | None:
    if not project_dir or not isinstance(project_dir, str) or not project_dir.strip():
        return None
    try:
        return Path(project_dir).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return None


def _project_roots(project_dir: str | None, extra_roots: list[str] | None) -> list[Path]:
    """The project root plus any P3 session-granted roots, realpath-resolved.

    ``extra_roots`` are a bridge session's persisted, validate_grant_dir-checked
    scope grants (project ``root_dirs`` + ``allowed_dirs``); a write proven inside
    ANY of them is in-scope. They ONLY widen the write boundary here -- the secret
    and delete checks in :func:`classify_shell` run against ``project_dir`` first
    and are untouched, so widening scope never re-opens a credential store."""
    roots: list[Path] = []
    primary = _project_root(project_dir)
    if primary is not None:
        roots.append(primary)
    for extra in extra_roots or ():
        resolved = _project_root(extra)
        if resolved is not None and resolved not in roots:
            roots.append(resolved)
    return roots


def _path_in_project(
    raw_path: str,
    project_dir: str | None,
    extra_roots: list[str] | None = None,
    bindings: dict[str, str] | None = None,
) -> bool:
    """True when raw_path provably resolves INSIDE the project (or a granted root).

    ABSOLUTE paths are realpath-resolved and checked. RELATIVE paths are resolved
    against project_dir (the caller's cwd IS the project for both the Session
    Bridge and the runner) and realpath-checked, so an in-scope relative write is
    proven in-scope while a ``../../..`` escape resolves out and fails (reviewer
    B3). ``extra_roots`` (P3) widens the in-scope set to a session's granted
    ``root_dirs``/``allowed_dirs``. A None/empty scope means nothing can be proven
    -> False (hard-stop).

    ``bindings`` are literal ``NAME=value`` assignments from the SAME command; a
    ``$NAME`` operand is resolved through them FIRST. A path that still carries an
    expansion or a glob after that is UNPROVABLE and returns False. That guard
    closes a real hole: without it ``$W/x`` was treated as the relative path
    ``$W/x``, joined to the project root, and "proved" in-scope -- so
    ``W=/etc; cp payload "$W/passwd"`` classified INTERNAL_REVERSIBLE and
    auto-executed a write to /etc."""
    roots = _project_roots(project_dir, extra_roots)
    if not roots:
        return False
    text = raw_path.strip().strip("\"'")
    if not text:
        return False
    resolved_text = _expand_literal(text, bindings)
    if resolved_text is None:
        return False
    text = resolved_text.strip().strip("\"'")
    if not text or any(ch in text for ch in _UNPROVABLE_PATH_CHARS):
        return False
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        # A relative path is resolved against the PRIMARY scope (the caller's cwd);
        # granted roots widen only what an absolute/escaping path may resolve INTO.
        candidate = roots[0] / candidate
    try:
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return False
    for root in roots:
        if inode_relative_parts_anchored(resolved, root) is not None:
            return True
    return False


def _path_in_primary_from_cwd(
    raw_path: str,
    project_dir: str | None,
    effective_cwd: str | None,
    bindings: dict[str, str] | None = None,
) -> bool:
    """Prove a path beneath the primary project while resolving from actual cwd."""
    primary = _project_root(project_dir)
    if primary is None:
        return False
    text = raw_path.strip().strip("\"'")
    expanded = _expand_literal(text, bindings)
    if expanded is None or any(ch in expanded for ch in _UNPROVABLE_PATH_CHARS):
        return False
    candidate = Path(expanded).expanduser()
    if not candidate.is_absolute():
        cwd = _project_root(effective_cwd) or primary
        candidate = cwd / candidate
    try:
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return False
    relative = inode_relative_parts_anchored(resolved, primary)
    return relative is not None and bool(relative)


def _is_safe_sink(target: str) -> bool:
    t = target.strip().strip("\"'")
    return t in _REDIRECT_SAFE_TARGETS


def _is_assignment_only(argv: list[str]) -> bool:
    """True when every word is ``NAME=value`` — a pure binding, running no command."""
    return bool(argv) and all(_ASSIGNMENT_RE.match(word) is not None for word in argv)


def _literal_binding_states(
    segments: list[list[str]],
    segment_controls: list[str | None],
    segment_outgoing_controls: list[str | None],
    segment_depths: list[int],
) -> list[dict[str, str]]:
    """Literal bindings visible *before* each segment in the same shell command.

    Only fully literal values qualify (no expansion, no glob, no ``~``, no
    newline), and a name bound twice to different values is DROPPED entirely —
    an operand referencing it then cannot be proven and hard-stops. Assignments
    are recognised only where the shell recognises them: an assignment-only
    segment. A binding is never applied to its own segment, because redirects
    and other expansions on that segment see the prior shell environment.

    Conditional, grouped, pipeline, and background assignments are not persistent
    enough to prove a later path. Their assigned names are dropped. Taking a
    snapshot per segment also prevents a later assignment from laundering an
    earlier operand (``rm "$HOME/x"; HOME=/project``).

    This never widens what a proven path may be; it only lets the very common
    ``W=/abs/workspace; rm -f "$W/tmp"`` shape reach the same proof an inline
    literal path would have reached.
    """
    if not (
        len(segments)
        == len(segment_controls)
        == len(segment_outgoing_controls)
        == len(segment_depths)
    ):
        return [{} for _segment in segments]

    states: list[dict[str, str]] = []
    bindings: dict[str, str] = {}
    dropped: set[str] = set()
    for index, argv in enumerate(segments):
        states.append(dict(bindings))
        if not argv or not _is_assignment_only(argv):
            # A segment that also runs a command (`PATH=/evil ls`) is NOT a binding
            # source: an env prefix changes what that command DOES, so it keeps its
            # deny-by-default classification instead of being silently stripped.
            continue

        incoming = segment_controls[index]
        outgoing = segment_outgoing_controls[index]
        persistent = (
            segment_depths[index] == 0
            and incoming in {None, ";", "\n"}
            and outgoing not in {"||", "|", "|&", "&", "(", ")"}
        )
        for word in argv:
            match = _ASSIGNMENT_RE.match(word)
            if match is None:
                break
            name, value = match.group(1), match.group(2)
            if not persistent or not value or any(ch in value for ch in _UNSAFE_BINDING_CHARS):
                dropped.add(name)
                bindings.pop(name, None)
                continue
            if name in bindings and bindings[name] != value:
                dropped.add(name)  # ambiguous: which value reaches the operand?
                bindings.pop(name, None)
                continue
            if name not in dropped:
                bindings[name] = value
    return states


def _expand_literal(text: str, bindings: dict[str, str] | None) -> str | None:
    """*text* with ``$NAME``/``${NAME}`` resolved from *bindings*; None if it cannot be.

    Fails closed: an unknown name, or any ``$`` surviving substitution, yields
    None so the caller treats the operand as unprovable.
    """
    if "$" not in text:
        return text
    if not bindings:
        return None
    missing = False

    def _substitute(match: re.Match[str]) -> str:
        nonlocal missing
        name = match.group(1) or match.group(2)
        if name not in bindings:
            missing = True
            return ""
        return bindings[name]

    expanded = _VAR_REF_RE.sub(_substitute, text)
    if missing or "$" in expanded:
        return None
    return expanded


def _evaluation_marker_at(text: str, index: int) -> bool:
    """Whether *index* starts shell evaluation that cannot be classified lexically."""
    return text[index] == "`" or any(
        text.startswith(marker, index) for marker in ("$(", "<(", ">(")
    )


def _heredoc_delimiter(
    command: str,
    start: int,
) -> tuple[str, bool, bool, int] | None:
    """Parse a ``<<`` delimiter as ``(text, quoted, strip_tabs, end_index)``."""
    index = start
    strip_tabs = index < len(command) and command[index] == "-"
    if strip_tabs:
        index += 1
    while index < len(command) and command[index] in " \t":
        index += 1
    if index >= len(command) or command[index] in "\r\n":
        return None

    delimiter: list[str] = []
    quote: str | None = None
    quoted = False
    while index < len(command):
        char = command[index]
        if quote is not None:
            if char == quote:
                quote = None
            elif char == "\\" and quote == '"' and index + 1 < len(command):
                quoted = True
                delimiter.append(command[index + 1])
                index += 1
            else:
                delimiter.append(char)
            index += 1
            continue
        if char in ("'", '"'):
            quote = char
            quoted = True
            index += 1
            continue
        if char == "\\":
            if index + 1 >= len(command):
                return None
            quoted = True
            delimiter.append(command[index + 1])
            index += 2
            continue
        if char.isspace() or char in "<>|&;":
            break
        delimiter.append(char)
        index += 1
    if quote is not None or not delimiter:
        return None
    return "".join(delimiter), quoted, strip_tabs, index


def _unquoted_heredoc_has_evaluation(text: str) -> bool:
    """Shell expands substitutions in an unquoted heredoc despite quote characters."""
    # An odd trailing backslash joins this line to the next before heredoc
    # expansion. Refuse the shape rather than let two individually harmless lines
    # synthesize ``$(``, a backtick expression, or an escaping path.
    if (len(text) - len(text.rstrip("\\"))) % 2 == 1:
        return True
    index = 0
    while index < len(text):
        if text[index] == "\\" and index + 1 < len(text):
            if text[index + 1] in "\\$`\n":
                index += 2
                continue
        if _evaluation_marker_at(text, index):
            return True
        index += 1
    return False


def _line_continuation_end(text: str, index: int) -> int | None:
    """Return the index after a shell backslash-newline continuation."""
    if text.startswith("\\\r\n", index):
        return index + 3
    if text.startswith("\\\n", index):
        return index + 2
    return None


def _lex_source_without_evaluation(command: str) -> str | None:
    """Return *command* with heredoc bodies masked, or ``None`` when unsafe.

    This is deliberately a conservative shell scanner, not an evaluator. Executable
    command/process/arithmetic substitution is rejected before token classification.
    Ordinary single quotes and quoted-heredoc bodies are literal; Bash ANSI-C
    quoting is deliberately unsupported because its escape decoding can synthesize
    secret paths the path/secret classifiers never saw. Unquoted heredocs still
    expand substitutions. Masking heredoc bodies lets the normal lexer classify the
    *entire* command around them without an unsafe early return when literal body
    text contains unmatched shell quote characters.
    """
    if _LITERAL_DOLLAR_SENTINEL in command:
        return None
    index = 0
    quote: str | None = None
    pending_heredocs: list[tuple[str, bool, bool]] = []
    heredoc_index = 0
    lex_source: list[str] = []
    logical_marker: str | None = None
    while index < len(command):
        char = command[index]

        if quote == "'":
            lex_source.append(char)
            if char == "'":
                quote = None
                logical_marker = None
            index += 1
            continue
        if quote == '"':
            if char == "\\":
                continuation_end = _line_continuation_end(command, index)
                if continuation_end is not None:
                    # Bash removes both bytes before parsing. Preserve the previous
                    # logical marker so ``$\\\n(`` is seen as ``$(``.
                    index = continuation_end
                    continue
                if index + 1 >= len(command):
                    return None
                lex_source.extend((char, command[index + 1]))
                logical_marker = None
                index += 2
                continue
            if char == "(" and logical_marker == "$":
                return None
            lex_source.append(char)
            if char == '"':
                quote = None
                logical_marker = None
                index += 1
                continue
            if _evaluation_marker_at(command, index):
                return None
            logical_marker = "$" if char == "$" else None
            index += 1
            continue
        if char == "\\":
            continuation_end = _line_continuation_end(command, index)
            if continuation_end is not None:
                # Emit the logical shell source, not the physical two-line form.
                # This also prevents path operands such as ``../out\\\nside`` from
                # being proved against a different filename than Bash will use.
                index = continuation_end
                continue
            if index + 1 >= len(command):
                return None
            lex_source.extend((char, command[index + 1]))
            logical_marker = None
            index += 2
            continue
        if command.startswith("$'", index):
            return None
        if char in ("'", '"'):
            lex_source.append(char)
            quote = char
            logical_marker = None
            index += 1
            continue
        if command.startswith("<<", index) and not command.startswith("<<<", index):
            parsed = _heredoc_delimiter(command, index + 2)
            if parsed is None:
                return None
            delimiter, quoted, strip_tabs, end = parsed
            sentinel = f"__OMNI_HEREDOC_{heredoc_index}__"
            heredoc_index += 1
            pending_heredocs.append((delimiter, quoted, strip_tabs))
            # Normalize every delimiter spelling (quoted, escaped, or <<-) to a
            # lexer-only sentinel. Evaluation semantics were already captured above.
            lex_source.append(f"<< {sentinel}")
            logical_marker = None
            index = end
            continue
        if char == "(" and logical_marker in {"$", "<", ">"}:
            return None
        if _evaluation_marker_at(command, index):
            return None
        if char != "\n" or not pending_heredocs:
            lex_source.append(char)
            logical_marker = char if char in {"$", "<", ">"} else None
            index += 1
            continue

        lex_source.append(char)
        logical_marker = None
        index += 1
        for delimiter, quoted, strip_tabs in pending_heredocs:
            while index <= len(command):
                line_end = command.find("\n", index)
                has_newline = line_end != -1
                if not has_newline:
                    line_end = len(command)
                line = command[index:line_end].rstrip("\r")
                comparable = line.lstrip("\t") if strip_tabs else line
                if comparable == delimiter:
                    if has_newline:
                        lex_source.append("\n")
                    index = line_end + (1 if has_newline else 0)
                    break
                if not quoted and _unquoted_heredoc_has_evaluation(line):
                    return None
                if not has_newline:
                    return None
                # Preserve only line structure. Body text is literal data and must
                # not become argv, operators, or quote state in the command lexer.
                lex_source.append("\n")
                index = line_end + 1
            else:
                return None
        pending_heredocs.clear()
    if pending_heredocs or quote is not None:
        return None
    return "".join(lex_source)


# --- explicit, quote-aware shell lexer ---------------------------------------


def _match_operator(command: str, i: int) -> tuple[str, int]:
    """Match the longest shell operator starting at index i. Returns (op, next_i)."""
    three = command[i : i + 3]
    if three in ("<<<", "&>>"):
        return three, i + 3
    two = command[i : i + 2]
    if two in ("&&", "||", "|&", ">>", "&>", ">&", ">|", "<<", "<&", "<>"):
        return two, i + 2
    return command[i], i + 1


def _lex(command: str) -> list[tuple[str, str]] | None:
    """Tokenize a shell command into (kind, text) where kind is 'WORD' or 'OP'.

    Quote-aware (single/double), backslash-aware. Fd-number prefixes glued to a
    redirect (``2>``) are dropped rather than surfaced as bogus operands. Returns
    None if quoting is unbalanced (caller fails closed)."""
    tokens: list[tuple[str, str]] = []
    buf: list[str] = []
    word_started = False
    i, n = 0, len(command)

    def flush_word() -> None:
        nonlocal word_started
        if word_started:
            tokens.append(("WORD", "".join(buf)))
            buf.clear()
            word_started = False

    while i < n:
        c = command[i]
        if c in ("'", '"'):
            word_started = True
            quote = c
            j = i + 1
            while j < n and command[j] != quote:
                if quote == '"' and command[j] == "\\" and j + 1 < n:
                    buf.append(command[j + 1])
                    j += 2
                    continue
                if quote == "'" and command[j] == "$":
                    buf.extend((_LITERAL_DOLLAR_SENTINEL, "$"))
                    j += 1
                    continue
                buf.append(command[j])
                j += 1
            if j >= n:
                return None  # unbalanced quote -> fail closed
            i = j + 1
            continue
        if c == "\\" and i + 1 < n:
            word_started = True
            buf.append(command[i + 1])
            i += 2
            continue
        if c in "<>|&;\n()":
            # An fd number glued to a '<'/'>' (e.g. `2>`) is not an operand.
            if c in "<>" and buf and all(ch.isdigit() for ch in buf):
                buf.clear()
                word_started = False
            else:
                flush_word()
            op, i = _match_operator(command, i)
            tokens.append(("OP", op))
            continue
        if c.isspace():
            flush_word()
            i += 1
            continue
        word_started = True
        buf.append(c)
        i += 1
    flush_word()
    return tokens


def _next_word(tokens: list[tuple[str, str]], op_idx: int) -> str | None:
    j = op_idx + 1
    if j < len(tokens) and tokens[j][0] == "WORD":
        return tokens[j][1]
    return None


def _is_fd_ref(word: str) -> bool:
    """True for a fd duplication/close target like `1`, `2`, `-` (not a file)."""
    w = word.strip()
    return w == "-" or w.isdigit()


def _parse(
    tokens: list[tuple[str, str]],
) -> tuple[
    list[list[str]],
    list[tuple[str, int]],
    list[str | None],
    list[str | None],
    list[int],
    bool,
]:
    """Split a token stream into command segments (argv lists) and collect the
    targets of FILE-WRITE redirects (truncate AND append). Redirect operators +
    their targets are removed from the segments; control operators split segments
    and are preserved as the operator that gates each segment. Each segment also
    carries its shell-group depth; cwd changes inside ``(...)`` must never be used
    as proof for a command in the parent shell.

    Heredoc bodies (``<<EOF`` … ``EOF``) are *data*, not executable segments.
    Without this, a common write shape like
    ``cat > /Desktop/file.html <<'EOF'\\n...\\nEOF`` was classified as
    IRREVERSIBLE because the body lines (``hello``, ``EOF``) were treated as
    unknown commands. That parked legitimate Desktop deliverables for approval.
    """
    segments: list[list[str]] = []
    segment_controls: list[str | None] = []
    segment_outgoing_controls: list[str | None] = []
    segment_depths: list[int] = []
    current: list[str] = []
    next_control: str | None = None
    write_targets: list[tuple[str, int]] = []
    group_depth = 0
    grouping_valid = True
    idx = 0
    while idx < len(tokens):
        kind, text = tokens[idx]
        if kind == "OP":
            if text in _CONTROL_OPS:
                if current:
                    segments.append(current)
                    segment_controls.append(next_control)
                    segment_outgoing_controls.append(text)
                    segment_depths.append(group_depth)
                    current = []
                if text == "(":
                    group_depth += 1
                elif text == ")":
                    if group_depth == 0:
                        grouping_valid = False
                    else:
                        group_depth -= 1
                next_control = text
            elif text in _FILE_WRITE_OPS:
                target = _next_word(tokens, idx)
                if target is not None:
                    write_targets.append((target, len(segments)))
                    idx += 1  # also skip the target word
            elif text in _DUP_OR_REDIRECT:  # '>&'
                target = _next_word(tokens, idx)
                if target is not None:
                    if not _is_fd_ref(target):
                        # `>&file` writes the file.
                        write_targets.append((target, len(segments)))
                    idx += 1
            elif text == "<<<":
                # Here-string: one word payload, not a new command segment.
                if _next_word(tokens, idx) is not None:
                    idx += 1
            elif text == "<<":
                # The pre-lexer scanner validated and removed every body/closing
                # delimiter. Consume only this command-line delimiter so redirects
                # that follow it (`cat <<EOF > out`) are still classified.
                if _next_word(tokens, idx) is not None:
                    idx += 1
            elif text in _INPUT_OPS:
                if _next_word(tokens, idx) is not None:
                    idx += 1  # consume redirect target (input -> not a write)
            # any other operator: ignore
        else:
            current.append(text)
        idx += 1
    if current:
        segments.append(current)
        segment_controls.append(next_control)
        segment_outgoing_controls.append(None)
        segment_depths.append(group_depth)
    return (
        segments,
        write_targets,
        segment_controls,
        segment_outgoing_controls,
        segment_depths,
        grouping_valid and group_depth == 0,
    )


# --- per-command / per-primitive predicates ----------------------------------


def _git_branch_is_read_only(args: list[str]) -> bool:
    if not args:
        return True
    if any(token in _GIT_BRANCH_DESTRUCTIVE_OPTIONS for token in args):
        return False
    # A positional branch name without an explicit listing mode creates a branch.
    listing_modes = {
        "--all",
        "-a",
        "--contains",
        "--list",
        "--merged",
        "--no-contains",
        "--no-merged",
        "--remotes",
        "-r",
        "--show-current",
        "-v",
        "-vv",
        "--verbose",
    }
    return any(token in listing_modes or token.startswith("--list=") for token in args)


def _git_remote_is_read_only(args: list[str]) -> bool:
    if not args:
        return True
    if all(token in {"-v", "--verbose"} for token in args):
        return True
    return args[0] in {"get-url", "show"} and not any(
        token in {"--add", "--delete", "--push"} for token in args[1:]
    )


def _git_config_operands(args: list[str]) -> tuple[list[str], bool] | None:
    """Return config operands and whether an explicit read action was present."""
    operands: list[str] = []
    explicit_read = False
    index = 0
    while index < len(args):
        token = args[index]
        base = token.split("=", 1)[0]
        if base in _GIT_CONFIG_MUTATION_OPTIONS:
            return None
        if base in _GIT_CONFIG_READ_ACTIONS:
            explicit_read = True
            index += 1
            continue
        if token in _GIT_CONFIG_OPTIONS_WITH_VALUE:
            if index + 1 >= len(args):
                return None
            index += 2
            continue
        if base in _GIT_CONFIG_OPTIONS_WITH_VALUE and "=" in token:
            index += 1
            continue
        if token in _GIT_CONFIG_SAFE_OPTIONS or token.startswith("--type="):
            index += 1
            continue
        if token.startswith("-"):
            return None
        operands.append(token)
        index += 1
    return operands, explicit_read


def _git_config_is_read_only(args: list[str]) -> bool:
    parsed = _git_config_operands(args)
    if parsed is None:
        return False
    operands, explicit_read = parsed
    return explicit_read or len(operands) <= 1


def _git_is_read_only(args: list[str]) -> bool:
    if not args or args[0] not in _READ_ONLY_GIT_SUBCOMMANDS:
        return False
    for token in args[1:]:
        base = token.split("=", 1)[0]
        if base in _GIT_UNSAFE_TOKENS or base.startswith("--output"):
            return False
    if args[0] == "branch":
        return _git_branch_is_read_only(args[1:])
    if args[0] == "remote":
        return _git_remote_is_read_only(args[1:])
    if args[0] == "config":
        return _git_config_is_read_only(args[1:])
    return True


def _git_subcommand(args: list[str]) -> str | None:
    """First non-flag git subcommand (skips ``-C path`` / ``-c key=val``)."""
    i = 0
    while i < len(args):
        tok = args[i]
        if tok in ("-C", "-c"):
            i += 2
            continue
        if tok.startswith("-"):
            i += 1
            continue
        return tok.lower()
    return None


def _git_is_local_write(args: list[str]) -> bool:
    """True for in-repo write subcommands that do not reach a remote (2d)."""
    sub = _git_subcommand(args)
    if sub is None or sub in _GIT_REMOTE_WRITE_SUBCOMMANDS:
        return False
    if sub not in _GIT_LOCAL_WRITE_SUBCOMMANDS:
        return False
    if sub == "config":
        sub_index = next(
            (index for index, token in enumerate(args) if token.lower() == "config"),
            -1,
        )
        config_args = args[sub_index + 1 :]
        if _git_config_is_read_only(config_args):
            return False
        if any(token.split("=", 1)[0] in _GIT_CONFIG_NONLOCAL_SCOPES for token in config_args):
            return False
    # Destructive git shapes stay hard-stop even when "local write".
    if _git_is_destructive(["git", *args]):
        return False
    return True


def _git_is_destructive(tokens: list[str]) -> bool:
    if not tokens or tokens[0].lower() != "git":
        return False
    git_args = tokens[1:]
    sub_index = -1
    index = 0
    while index < len(git_args):
        token = git_args[index]
        if token in {"-C", "-c"}:
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        sub_index = index
        break
    if sub_index < 0:
        return False
    sub = git_args[sub_index].lower()
    sub_args = git_args[sub_index + 1 :]
    lowered_args = [token.lower() for token in sub_args]

    if sub == "reset" and "--hard" in lowered_args:
        return True
    if sub == "clean" and any(
        token.startswith("-") and "f" in token.lstrip("-") for token in lowered_args
    ):
        return True
    if sub == "checkout" and ("--" in sub_args or "." in sub_args):
        return True
    if sub == "rm":
        return True
    if sub == "branch" and any(
        token in _GIT_BRANCH_DESTRUCTIVE_OPTIONS
        or (
            token.startswith("-")
            and not token.startswith("--")
            and any(flag in token[1:] for flag in "dDfMC")
        )
        for token in sub_args
    ):
        return True
    if sub == "tag" and any(
        token in {"-f", "--force"}
        or (token.startswith("-") and not token.startswith("--") and "f" in token[1:])
        for token in sub_args
    ):
        return True
    if sub == "checkout" and any(
        token in _GIT_CHECKOUT_DESTRUCTIVE_OPTIONS
        or (token.startswith("-B") and not token.startswith("--"))
        for token in sub_args
    ):
        return True
    if sub == "switch" and any(
        token in _GIT_SWITCH_DESTRUCTIVE_OPTIONS
        or (token.startswith("-C") and not token.startswith("--"))
        for token in sub_args
    ):
        return True
    if any(
        token in _GIT_FORCE_OPTIONS or token.startswith("--force-with-lease") for token in sub_args
    ):
        return True
    if sub in {"branch", "checkout", "switch", "tag"} and any(
        token.startswith("-") and not token.startswith("--") and "f" in token[1:]
        for token in sub_args
    ):
        return True
    return False


def _strip_inert_env_prefix(argv: list[str]) -> list[str] | None:
    """Strip inert ``NAME=value`` prefixes; return None if any are exec-altering.

    ``NODE_PATH=… node script.js`` → ``[node, script.js]``.
    ``PATH=/evil ls`` → None (caller keeps IRREVERSIBLE).
    """
    if not argv:
        return argv
    i = 0
    while i < len(argv):
        match = _ASSIGNMENT_RE.match(argv[i])
        if match is None:
            break
        name = match.group(1)
        if name in _EXEC_ALTERING_PREFIXES:
            return None
        i += 1
    if i == 0:
        return argv
    if i >= len(argv):
        # Pure assignments only — handled by _is_assignment_only upstream.
        return argv
    return argv[i:]


def _cd_target_and_rest(argv: list[str]) -> tuple[str, list[str]] | None:
    """If argv is ``cd <dir> [flags…]``, return (dir, remaining_after_cd). Else None.

    Used by the segment classifier only for a lone ``cd`` (compound chains are
    handled in ``classify_shell`` via cwd rebinding across ``&&`` segments).
    """
    if not argv or _token_basename(argv[0]).lower() != "cd":
        return None
    # cd [-L|-P] dir
    args = [a for a in argv[1:] if a not in ("-L", "-P")]
    if len(args) != 1 or args[0] == "-":
        return None
    return args[0], []


def _sips_is_read_only(args: list[str]) -> bool:
    """Accept only sips property/profile queries with explicit input operands."""
    index = 0
    saw_query = False
    saw_input = False
    options = True
    while index < len(args):
        token = args[index]
        if options and token == "--":
            options = False
            index += 1
            continue
        if options and token in {"-1", "--oneLine"}:
            saw_query = True
            index += 1
            continue
        if options and token in {"-g", "--getProperty"}:
            if index + 1 >= len(args) or args[index + 1].startswith("-"):
                return False
            saw_query = True
            index += 2
            continue
        if options and token.startswith("--getProperty="):
            if not token.partition("=")[2]:
                return False
            saw_query = True
            index += 1
            continue
        if options and token == "--verify":
            saw_query = True
            index += 1
            continue
        if options and token.startswith("-"):
            # Includes output/modification/extract forms and all unknown flags.
            return False
        if token == "-":
            return False
        saw_input = True
        index += 1
    return saw_query and saw_input


def _identify_is_read_only(args: list[str]) -> bool:
    """Accept ImageMagick identify's documented inspection-only grammar."""
    zero_arg_options = frozenset(
        {
            "-antialias",
            "-auto-orient",
            "-clip",
            "-help",
            "-matte",
            "-moments",
            "-monitor",
            "-negate",
            "-ping",
            "-quiet",
            "-regard-warnings",
            "-respect-parentheses",
            "-strip",
            "-unique",
            "-verbose",
            "-version",
        }
    )
    one_arg_options = frozenset(
        {
            "-alpha",
            "-authenticate",
            "-channel",
            "-clip-mask",
            "-clip-path",
            "-colorspace",
            "-crop",
            "-debug",
            "-define",
            "-density",
            "-depth",
            "-endian",
            "-extract",
            "-features",
            "-format",
            "-fuzz",
            "-gamma",
            "-grayscale",
            "-interlace",
            "-interpolate",
            "-list",
            "-log",
            "-precision",
            "-sampling-factor",
            "-seed",
            "-size",
            "-units",
            "-virtual-pixel",
        }
    )
    two_arg_options = frozenset({"-limit", "-set"})
    plus_options = frozenset(
        {
            "+antialias",
            "+clip",
            "+matte",
            "+monitor",
            "+ping",
            "+quiet",
            "+regard-warnings",
            "+respect-parentheses",
            "+strip",
            "+verbose",
        }
    )
    standalone_queries = frozenset({"-help", "-version"})

    index = 0
    saw_input = False
    saw_standalone_query = False
    options = True
    while index < len(args):
        token = args[index]
        if options and token == "--":
            options = False
            index += 1
            continue
        if token in {"-write", "+write"}:
            return False
        if options and token in zero_arg_options:
            saw_standalone_query = saw_standalone_query or token in standalone_queries
            index += 1
            continue
        if options and token in plus_options:
            index += 1
            continue
        if options and token in one_arg_options:
            if index + 1 >= len(args):
                return False
            saw_standalone_query = saw_standalone_query or token == "-list"
            index += 2
            continue
        if options and token in two_arg_options:
            if index + 2 >= len(args):
                return False
            index += 3
            continue
        if options and token.startswith(("-", "+")) and token != "-":
            # Unknown flags fail closed, including unrecognised write aliases.
            return False
        saw_input = True
        index += 1
    return saw_input or saw_standalone_query


def _find_exec_is_read_only(argv: list[str]) -> bool:
    """A tiny positive grammar for ``find -exec`` read-only inspection."""
    # Requiring the literal command name prevents ``-exec /tmp/ls`` from turning
    # an attacker-controlled executable into a supposedly read-only inspection.
    if not argv or argv[0] != "ls":
        return False
    safe_ls_flags = frozenset("1ABCDFGHLOPRSTUWabcdefghiklmnopqrstuwx")
    for token in argv[1:]:
        if token == "{}" or not token.startswith("-") or token == "-":
            continue
        if token.startswith("--"):
            if token not in {
                "--classify",
                "--color=always",
                "--color=auto",
                "--color=never",
                "--directory",
                "--human-readable",
                "--inode",
                "--numeric-uid-gid",
                "--quote-name",
                "--recursive",
                "--reverse",
                "--size",
                "--time-style=long-iso",
            }:
                return False
            continue
        if any(char not in safe_ls_flags for char in token[1:]):
            return False
    return True


def _find_is_read_only(args: list[str]) -> bool:
    index = 0
    while index < len(args):
        token = args[index]
        if token == "-exec":
            end = index + 1
            while end < len(args) and args[end] not in {";", "+"}:
                end += 1
            if end >= len(args) or not _find_exec_is_read_only(args[index + 1 : end]):
                return False
            index = end + 1
            continue
        if token.startswith("-") and token not in _FIND_READONLY_PREDICATES:
            return False
        index += 1
    return True


_SED_READ_ONLY_PROGRAM_RE = re.compile(r"\A(?:\d+(?:,\d+)?)?p\Z")


def _sed_is_read_only(args: list[str]) -> bool:
    """Accept only a positive, print-only sed grammar."""
    programs: list[str] = []
    index = 0
    options = True
    input_mode = False
    while index < len(args):
        token = args[index]
        if input_mode:
            if token.startswith("-") and token != "-":
                return False
            index += 1
            continue
        if options and token == "--":
            options = False
            index += 1
            continue
        if options and token in {
            "-n",
            "--quiet",
            "--silent",
            "-E",
            "-r",
            "--regexp-extended",
            "-u",
            "--unbuffered",
        }:
            index += 1
            continue
        if options and token in {"-e", "--expression"}:
            if index + 1 >= len(args):
                return False
            programs.append(args[index + 1])
            index += 2
            continue
        if options and token.startswith("--expression="):
            programs.append(token.split("=", 1)[1])
            index += 1
            continue
        if options and token.startswith("-e") and len(token) > 2:
            programs.append(token[2:])
            index += 1
            continue
        if options and token.startswith("-"):
            return False
        if not programs:
            programs.append(token)
        # After a bare program (or the first input following -e), GNU option
        # permutation must not smuggle a later -i/-f/effect flag into the argv.
        input_mode = True
        index += 1
    return bool(programs) and all(
        _SED_READ_ONLY_PROGRAM_RE.fullmatch(program.strip()) is not None for program in programs
    )


_AWK_READ_ONLY_PROGRAM_RE = re.compile(
    r"\A\{\s*print\s+\$[0-9A-Za-z_]+"
    r"(?:\s*,\s*\$[0-9A-Za-z_]+)*\s*\}\Z"
)


def _awk_is_read_only(args: list[str]) -> bool:
    """Accept only field-printing awk programs with no file/command effects."""
    if len(args) != 1:
        return False
    program = args[0].strip()
    literal_dollar = f"{_LITERAL_DOLLAR_SENTINEL}$"
    if "$" not in program or any(
        program[index] == "$" and (index == 0 or program[index - 1] != _LITERAL_DOLLAR_SENTINEL)
        for index in range(len(program))
    ):
        return False
    normalized = program.replace(literal_dollar, "$")
    return (
        _LITERAL_DOLLAR_SENTINEL not in normalized
        and _AWK_READ_ONLY_PROGRAM_RE.fullmatch(normalized) is not None
    )


def _rg_is_read_only(args: list[str]) -> bool:
    for token in args:
        head = token.split("=", 1)[0]
        if head in _RG_SUBPROCESS_OPTIONS:
            return False
        if len(head) >= 2 and head[0] == "-" and head[1] != "-":
            if any(ch in _RG_UNSAFE_SHORT_FLAGS for ch in head[1:]):
                return False
    return True


def _sort_output_target(args: list[str]) -> str | None:
    """Return sort's output file (-o FILE / -oFILE / --output=FILE / --output FILE)."""
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-o", "--output"):
            return args[i + 1] if i + 1 < len(args) else ""
        if a.startswith("--output="):
            return a.split("=", 1)[1]
        if a.startswith("-o") and len(a) > 2 and not a.startswith("--"):
            return a[2:]
        i += 1
    return None


def _uniq_output_target(args: list[str]) -> str | None:
    """Return uniq's OUTPUT operand (the 2nd positional), if present."""
    operands: list[str] = []
    skip = False
    for tok in args:
        if skip:
            skip = False
            continue
        if tok in _UNIQ_ARG_FLAGS:
            skip = True
            continue
        if tok.startswith("-") and tok != "-":
            continue
        operands.append(tok)
    return operands[1] if len(operands) >= 2 else None


def _curl_get_class(
    args: list[str],
    project_dir: str | None,
    extra_roots: list[str] | None,
    bindings: dict[str, str] | None,
) -> ActionClass:
    """Classify a positive grammar for HTTPS/loopback GET and HEAD requests."""
    safe_short_flags = frozenset("fIkgLsS")
    safe_long_flags = frozenset(
        {
            "--fail",
            "--head",
            "--insecure",
            "--location",
            "--show-error",
            "--silent",
        }
    )
    value_options = {
        "-A": "text",
        "-H": "header",
        "-m": "number",
        "-o": "output",
        "-w": "write-out",
        "--connect-timeout": "number",
        "--header": "header",
        "--max-time": "number",
        "--output": "output",
        "--url": "url",
        "--user-agent": "text",
        "--write-out": "write-out",
    }
    output: str | None = None
    urls: list[str] = []
    index = 0
    options = True

    def consume_value(kind: str, value: str) -> bool:
        nonlocal output
        if not value:
            return False
        if kind == "output":
            if output is not None:
                return False
            output = value
            return True
        if kind == "url":
            urls.append(value)
            return True
        if kind == "number":
            try:
                return float(value) >= 0
            except ValueError:
                return False
        if kind == "header":
            return not value.startswith("@")
        if kind == "write-out":
            lowered = value.lower()
            return not value.startswith("@") and "%output{" not in lowered
        return not value.startswith("@")

    while index < len(args):
        token = args[index]
        if options and token == "--":
            options = False
            index += 1
            continue
        if not options or token == "-" or not token.startswith("-"):
            urls.append(token)
            index += 1
            continue
        if token.startswith("--"):
            head, separator, inline = token.partition("=")
            if head in safe_long_flags and not separator:
                index += 1
                continue
            kind = value_options.get(head)
            if kind is None:
                return ActionClass.IRREVERSIBLE
            if separator:
                if not consume_value(kind, inline):
                    return ActionClass.IRREVERSIBLE
                index += 1
                continue
            if index + 1 >= len(args) or not consume_value(kind, args[index + 1]):
                return ActionClass.IRREVERSIBLE
            index += 2
            continue
        if token in value_options:
            if index + 1 >= len(args) or not consume_value(value_options[token], args[index + 1]):
                return ActionClass.IRREVERSIBLE
            index += 2
            continue
        if all(char in safe_short_flags for char in token[1:]):
            index += 1
            continue
        return ActionClass.IRREVERSIBLE

    if not urls or any(
        not (
            url.startswith("https://")
            or re.match(r"\Ahttp://(?:127\.0\.0\.1|localhost|\[::1\])(?::\d+)?(?:/|\Z)", url)
        )
        for url in urls
    ):
        return ActionClass.IRREVERSIBLE
    if output is None or _is_safe_sink(output):
        return ActionClass.EXTERNAL_REVERSIBLE
    return (
        ActionClass.INTERNAL_REVERSIBLE
        if _path_in_project(output, project_dir, extra_roots, bindings)
        else ActionClass.IRREVERSIBLE
    )


def _copy_move_install_targets(
    base: str,
    args: list[str],
) -> tuple[list[str], list[str]] | None:
    """Return ``(write_targets, delete_sources)`` for supported copy/move forms.

    Options are parsed as options, including their values. Unsupported or ambiguous
    shapes return ``None`` so a value such as ``-t /outside`` can never be mistaken
    for a source while an in-scope source is "proved" as the destination. ``mv``
    removes its sources, so those paths are returned separately and must prove
    beneath the primary project root rather than a write-only granted root.
    """
    operands: list[str] = []
    target_directory: str | None = None
    directory_mode = False
    exchange_mode = False
    options = True
    index = 0

    def set_target(value: str) -> bool:
        nonlocal target_directory
        if not value or target_directory is not None:
            return False
        target_directory = value
        return True

    while index < len(args):
        token = args[index]
        if options and token == "--":
            options = False
            index += 1
            continue
        if not options or token == "-" or not token.startswith("-"):
            operands.append(token)
            index += 1
            continue

        if token.startswith("--"):
            head, separator, inline_value = token.partition("=")
            if base == "install" and head == "--strip-program":
                # GNU install executes this arbitrary program when stripping. It
                # is code execution, not a path-option value that can be ignored.
                return None
            if head == "--target-directory":
                if separator:
                    if not set_target(inline_value):
                        return None
                    index += 1
                    continue
                if index + 1 >= len(args) or not set_target(args[index + 1]):
                    return None
                index += 2
                continue
            if base == "install" and head == "--directory" and not separator:
                directory_mode = True
                index += 1
                continue
            if base == "mv" and head == "--exchange" and not separator:
                exchange_mode = True
                index += 1
                continue
            value_options = (
                _INSTALL_VALUE_LONG_OPTIONS if base == "install" else _COPY_MOVE_VALUE_LONG_OPTIONS
            )
            if head in value_options:
                if separator:
                    if not inline_value:
                        return None
                    index += 1
                    continue
                if index + 1 >= len(args):
                    return None
                index += 2
                continue
            flag_options = _INSTALL_LONG_FLAGS if base == "install" else _COPY_MOVE_LONG_FLAGS[base]
            inline_options = (
                _INSTALL_INLINE_LONG_OPTIONS
                if base == "install"
                else _COPY_MOVE_INLINE_LONG_OPTIONS
            )
            if not separator and head in flag_options:
                index += 1
                continue
            if separator and head in inline_options and inline_value:
                index += 1
                continue
            return None

        short_flags = _INSTALL_SHORT_FLAGS if base == "install" else _COPY_MOVE_SHORT_FLAGS[base]
        value_options = (
            _INSTALL_VALUE_SHORT_OPTIONS if base == "install" else _COPY_MOVE_VALUE_SHORT_OPTIONS
        )
        position = 1
        while position < len(token):
            option = token[position]
            if option in value_options:
                inline_value = token[position + 1 :]
                if not inline_value:
                    if index + 1 >= len(args):
                        return None
                    inline_value = args[index + 1]
                    index += 1
                if option == "t" and not set_target(inline_value):
                    return None
                position = len(token)
                continue
            if option not in short_flags:
                return None
            if base == "install" and option == "d":
                directory_mode = True
            position += 1
        index += 1

    if directory_mode and target_directory is not None:
        return None
    if directory_mode:
        return (operands, []) if operands else None
    if exchange_mode:
        if target_directory is not None or len(operands) != 2:
            return None
        return [], operands
    if target_directory is not None:
        if not operands:
            return None
        return ([target_directory], operands) if base == "mv" else ([target_directory], [])
    if len(operands) < 2:
        return None
    return ([operands[-1]], operands[:-1]) if base == "mv" else ([operands[-1]], [])


def _git_mv_sources(argv: list[str]) -> list[str] | None:
    """SOURCE operands of a ``git mv`` segment, or ``None`` if not one.

    Mirrors :func:`_git_subcommand`'s flag-skipping (``-C path`` / ``-c key=val``)
    to find the subcommand, then treats every remaining non-flag token except the
    last (the destination) as a source -- ``git mv`` supports far fewer flags than
    plain ``mv`` (``-f``/``-k``/``-n``/``--dry-run``), so no dedicated options
    parser is needed."""
    if not argv or _token_basename(argv[0]).lower() != "git":
        return None
    if _git_subcommand(argv[1:]) != "mv":
        return None
    index = 1
    while index < len(argv) and argv[index] in ("-C", "-c"):
        index += 2
    # ``index`` now sits on the "mv" token itself (or a leading flag before it,
    # which _git_subcommand already tolerated) -- operands start after it.
    sub_index = next(
        (i for i in range(index, len(argv)) if argv[i].lower() == "mv"),
        None,
    )
    if sub_index is None:
        return None
    operands = [a for a in argv[sub_index + 1 :] if not a.startswith("-")]
    if len(operands) < 2:
        return None
    return operands[:-1]


def _rsync_remove_source_sources(argv: list[str]) -> list[str] | None:
    """SOURCE operands of an ``rsync --remove-source-files ...`` segment.

    Without ``--remove-source-files`` a plain ``rsync`` only COPIES (the source
    stays put, so nothing is relocated past a path-based deny); WITH it, rsync
    deletes each source after a successful transfer -- functionally a move."""
    if not argv or _token_basename(argv[0]).lower() != "rsync":
        return None
    if not any(a == "--remove-source-files" for a in argv[1:]):
        return None
    operands = [a for a in argv[1:] if not a.startswith("-")]
    if len(operands) < 2:
        return None
    return operands[:-1]


def _rename_command_sources(argv: list[str]) -> list[str] | None:
    """SOURCE operands of a ``rename`` segment (Perl-style or util-linux).

    ``rename`` has two INCOMPATIBLE common forms -- Perl-style
    (``rename 's/old/new/' FILE...``, a regex applied to each FILE) and
    util-linux (``rename FROM TO FILE...``, a literal substring replace) -- with
    no reliable way to tell which is in play, or which token is the "old"
    spelling, from argv alone. Rather than silently skip the check for either
    form, every non-flag operand is treated as a candidate SOURCE: fail CLOSED
    (an extra, unnecessary check on the expression/replacement token is
    harmless) rather than fail open (missing the actual file operands)."""
    if not argv or _token_basename(argv[0]).lower() != "rename":
        return None
    operands = [a for a in argv[1:] if not a.startswith("-")]
    return operands or None


def _relocation_command_sources(argv: list[str]) -> list[str] | None:
    """SOURCE operands of a relocation-shaped command, or ``None`` if ``argv`` does
    not rename/move/relocate anything (Gemini review round 1, finding 3: recognize
    more than literal ``mv``).

    Every spelling below can move a path past a parent's path-based deny exactly
    like plain ``mv`` (AC-policy P0 fix7) -- ``git mv`` (an in-repo rename with its
    own porcelain), a ``rename`` invocation (either common form), and
    ``rsync --remove-source-files`` (copy-then-delete-source, i.e. a move). Any
    OTHER command -- including an unrecognized program that merely LOOKS
    relocation-shaped -- is not treated as one here: a ``cp`` (no flag) only reads
    its source (OS-sandbox read-denied, so it cannot exfiltrate the store), and an
    arbitrary program name has no established relocation semantics to fail closed
    on without also over-matching ordinary commands."""
    if not argv:
        return None
    base = _token_basename(argv[0]).lower()
    if base == "mv":
        parsed = _copy_move_install_targets("mv", argv[1:])
        # An unparseable mv already hard-stops elsewhere; fall back to all operands.
        return parsed[1] if parsed is not None else [a for a in argv[1:] if not a.startswith("-")]
    if base == "git":
        return _git_mv_sources(argv)
    if base == "rsync":
        return _rsync_remove_source_sources(argv)
    if base == "rename":
        return _rename_command_sources(argv)
    return None


def _move_relocates_secret(segments: list[list[str]], project_dir: str | None) -> bool:
    """True when any relocation-shaped segment (``mv``, ``git mv``, ``rename``,
    ``rsync --remove-source-files`` -- see :func:`_relocation_command_sources`)
    would relocate a registered secret store.

    A rename of an ANCESTOR of ``<repo>/var/secrets`` (``mv var var2``) moves the
    store past every path-based deny — the parent-rename bypass. Renaming the store
    itself (``mv var/secrets x``) is already caught by the read-secret gate, but the
    ancestor case is not, so each candidate's SOURCE operands are checked against
    the registry's ancestor-aware predicate. A plain copy (``cp``, no source
    deletion) is intentionally NOT covered: it reads its source, which the OS
    sandbox read-denies, so it cannot exfiltrate the store. The TARGET is
    intentionally NOT checked with this predicate — copying a file INTO ``var/``
    (an ancestor dir) is legitimate."""
    from omniagentos.secret_registry import path_relocates_secret_dir

    for argv in segments:
        sources = _relocation_command_sources(argv)
        if sources is None:
            continue
        if any(path_relocates_secret_dir(src, project_dir) for src in sources):
            return True
    return False


def _has_destructive_primitive(words: list[str]) -> bool:
    bases = [_token_basename(t).lower() for t in words]
    if any(b in _DESTRUCTIVE_COMMANDS for b in bases):
        return True
    if "find" in bases:
        if "-delete" in words:
            return True
        if ("-exec" in words or "-execdir" in words) and any(b in _FIND_DELETE_EXEC for b in bases):
            return True
    if "git" in bases and _git_is_destructive(words):
        return True
    if "rsync" in bases and any(flag in words for flag in _RSYNC_DELETE):
        return True
    if any(b in {"chmod", "chown", "chgrp"} for b in bases) and any(
        t in {"-R", "--recursive"} or (t.startswith("-") and not t.startswith("--") and "R" in t)
        for t in words
    ):
        return True
    return False


def _scoped_delete_class(
    segments: list[list[str]],
    binding_states: list[dict[str, str]],
    project_dir: str | None,
    _extra_roots: list[str] | None,
    segment_controls: list[str | None] | None = None,
    segment_outgoing_controls: list[str | None] | None = None,
    segment_depths: list[int] | None = None,
) -> tuple[ActionClass, set[int]] | None:
    """Prove deletes only beneath the primary project root.

    The index set matters: ONLY those segments are re-classified as the proven
    class. Every other segment still goes through ``_classify_segment``, so
    ``rm ./tmp && cp x /etc/passwd`` keeps hard-stopping on the ``cp``.

    Returns ``None`` — meaning "not a shape we will prove", so the caller
    hard-stops — for anything outside a narrow, checkable form:

      * a destructive primitive that is not a LEADING ``rm``/``rmdir``/``unlink``
        (``find -delete``, ``git rm``, ``rsync --delete``, ``dd``, ``mkfs``,
        ``truncate``, ``shred``, or an ``rm`` buried mid-argv),
      * any flag outside the small recognised set,
      * an operand that does not resolve (unknown variable, surviving ``$``),
        that globs, that escapes the primary project, or that IS the project root.

    Additional roots grant write scope only. They do not grant deletion authority:
    removing content outside the primary workspace needs explicit approval even
    when creating or updating content there is allowed.
    """
    roots = _project_roots(project_dir, None)
    if not roots:
        return None
    primary = roots[0]
    controls = segment_controls or [None] * len(segments)
    outgoing_controls = segment_outgoing_controls or [None] * len(segments)
    depths = segment_depths or [0] * len(segments)
    if not (
        len(controls)
        == len(outgoing_controls)
        == len(depths)
        == len(binding_states)
        == len(segments)
    ):
        return None
    cwd_by_depth: dict[int, Path] = {0: primary}
    cwd_known_by_depth: dict[int, bool] = {0: True}
    cd_seen_by_depth: dict[int, bool] = {0: False}
    previous_depth = 0

    def resolve_operand(
        raw_path: str,
        bindings: dict[str, str],
        effective_cwd: Path,
        cwd_known: bool,
    ) -> Path | None:
        text = raw_path.strip().strip("\"'")
        expanded = _expand_literal(text, bindings)
        if expanded is None or any(ch in expanded for ch in _UNPROVABLE_PATH_CHARS):
            return None
        candidate = Path(expanded).expanduser()
        if not candidate.is_absolute():
            if not cwd_known:
                return None
            candidate = effective_cwd / candidate
        try:
            return candidate.resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            return None

    def inside_primary(path: Path) -> bool:
        return inode_relative_parts_anchored(path, primary) is not None

    proven: set[int] = set()
    for index, argv in enumerate(segments):
        control = controls[index]
        outgoing_control = outgoing_controls[index]
        depth = depths[index]
        bindings = binding_states[index]
        if depth < 0:
            return None
        if depth > previous_depth:
            for nested_depth in range(previous_depth + 1, depth + 1):
                parent_depth = nested_depth - 1
                cwd_by_depth[nested_depth] = cwd_by_depth[parent_depth]
                cwd_known_by_depth[nested_depth] = cwd_known_by_depth[parent_depth]
                # A subshell inherits cwd, but its later cd never mutates the
                # parent's cd/control-flow state.
                cd_seen_by_depth[nested_depth] = False
        elif depth < previous_depth:
            for nested_depth in range(previous_depth, depth, -1):
                cwd_by_depth.pop(nested_depth, None)
                cwd_known_by_depth.pop(nested_depth, None)
                cd_seen_by_depth.pop(nested_depth, None)
        previous_depth = depth
        effective_cwd = cwd_by_depth[depth]
        cwd_known = cwd_known_by_depth[depth]
        cd_seen = cd_seen_by_depth[depth]
        if cd_seen and index > 0 and control in {"||", ";", "\n", "&"}:
            # `cd x || rm relative`, `cd x; rm relative`, and pipeline/background
            # forms can execute the delete after a failed/non-propagating cd. The
            # effective cwd is then unknown until a later absolute cd proves it.
            cwd_known = False
            cwd_known_by_depth[depth] = False
        if not argv or _is_assignment_only(argv):
            continue  # a pure binding segment deletes nothing
        words = argv  # an env prefix is NOT stripped: `PATH=/evil rm x` must not prove
        base = _token_basename(words[0]).lower()
        if base in {"do", "then", "else"} and len(words) > 1:
            words = words[1:]
            base = _token_basename(words[0]).lower()
        if base != "cd" and any(_token_basename(token).lower() == "cd" for token in words[1:]):
            return None
        if base == "cd":
            allowed_controls = {None, "&&", ";", "\n"}
            if depth > 0:
                allowed_controls.add("(")
            if control not in allowed_controls or outgoing_control in {"|", "|&", "&"}:
                # A conditional cd may be skipped on a path that still reaches a
                # later operation. A group-local cd is allowed only inside that
                # group's independent cwd state.
                return None
            parsed_cd = _cd_target_and_rest(words)
            if parsed_cd is None:
                return None
            resolved_cd = resolve_operand(
                parsed_cd[0],
                bindings,
                effective_cwd,
                cwd_known,
            )
            if resolved_cd is None or not inside_primary(resolved_cd):
                return None
            cwd_by_depth[depth] = resolved_cd
            cwd_known_by_depth[depth] = True
            cd_seen_by_depth[depth] = True
            continue
        if base not in _SCOPED_DELETE_COMMANDS:
            if _has_destructive_primitive(words):
                return None  # destructive, but not a shape we prove
            continue
        operands: list[str] = []
        for token in words[1:]:
            if token.startswith("-") and token != "-":
                if token not in _SCOPED_DELETE_FLAGS:
                    return None  # an unrecognised flag may change the target set
                continue
            operands.append(token)
        if not operands:
            return None
        for operand in operands:
            resolved = resolve_operand(
                operand,
                bindings,
                effective_cwd,
                cwd_known,
            )
            if resolved is None:
                return None
            in_scope = inside_primary(resolved)
            # Scratch is only valid INSIDE project_dir (containment precondition).
            scratch = _is_scratch_delete_path(str(resolved), project_dir)
            if not in_scope and not scratch:
                return None
            if in_scope and resolved == primary:
                return None  # never auto-delete the workspace root itself
        proven.add(index)
    if not proven:
        return None
    return ActionClass.INTERNAL_REVERSIBLE, proven


def _write_target_class(
    target: str,
    project_dir: str | None,
    extra_roots: list[str] | None = None,
    bindings: dict[str, str] | None = None,
) -> ActionClass:
    if _is_safe_sink(target):
        return ActionClass.READ_ONLY
    return (
        ActionClass.INTERNAL_REVERSIBLE
        if _path_in_project(target, project_dir, extra_roots, bindings)
        else ActionClass.IRREVERSIBLE
    )


# Paths where file/folder deletion is routine cleanup, not a hard-stop event.
# Operator decision (2026-07-25): stale git worktrees and task workspaces may be
# removed without approval; finance writes and real (non-scratch) deletes still stop.
_SCRATCH_DELETE_MARKERS: tuple[str, ...] = (
    "/worktrees/",
    "/.git/worktrees/",
    "/var/runs/",
    "/var/intake-workspace/",
    "/var/projects/",
    "/.fusion-wt-",
    "/OmniAgentOS-worktrees/",
    "/OmniAgentOS-worktrees/",
    "/.fusion/worktrees/",
)


def _is_scratch_delete_path(raw_path: str, project_dir: str | None = None) -> bool:
    """True when *raw_path* is an ephemeral workspace INSIDE the current project.

    Two independent conditions, BOTH required:
      1. the resolved path is contained by ``project_dir``, and
      2. it matches a known ephemeral-workspace marker.

    Condition 1 stops ``rm -rf ~/OmniAgentOS/worktrees/main`` — a sibling
    product's worktree — from reading as routine cleanup. A substring match alone
    treated every directory named ``worktrees`` anywhere on the machine as scratch.
    """
    text = (raw_path or "").strip().strip("\"'")
    if not text or not project_dir:
        return False
    try:
        resolved = Path(text).expanduser().resolve(strict=False)
        root = Path(project_dir).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return False
    relative = inode_relative_parts_anchored(resolved, root)
    if relative is None:
        return False
    if not relative:
        return False  # never the workspace root itself
    lowered = str(resolved).replace("\\", "/")
    return any(marker in lowered for marker in _SCRATCH_DELETE_MARKERS)


# Inline interpreter payloads are arbitrary code — no denylist can be made sound.
_INLINE_PAYLOAD_FLAGS: frozenset[str] = frozenset({"-c", "-e", "-E", "--eval", "--command", "-"})
_PYTHON_INTERPRETERS = frozenset({"python", "python2", "python3", "pypy", "pypy3"})
_NODE_INTERPRETERS = frozenset({"node", "nodejs"})
_SHELL_INTERPRETERS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "csh", "tcsh", "fish", "ash"})
_DIRECT_SCRIPT_INTERPRETERS = frozenset(
    {
        "ts-node",
        "tsx",
        "perl",
        "perl5",
        "ruby",
        "lua",
        "luajit",
        "tclsh",
        "rscript",
        "osascript",
        "expect",
        "groovy",
        "scala",
    }
)
_BUN_SUBCOMMANDS = frozenset(
    {
        "a",
        "add",
        "audit",
        "build",
        "ci",
        "completions",
        "create",
        "exec",
        "help",
        "i",
        "info",
        "init",
        "install",
        "link",
        "outdated",
        "patch",
        "pm",
        "publish",
        "remove",
        "repl",
        "rm",
        "run",
        "search",
        "test",
        "un",
        "uninstall",
        "unlink",
        "up",
        "update",
        "upgrade",
        "why",
        "x",
    }
)
_DIRECT_INTERPRETER_SUBCOMMANDS: dict[str, frozenset[str]] = {
    "scala": frozenset(
        {
            "clean",
            "compile",
            "doc",
            "fmt",
            "package",
            "publish",
            "repl",
            "run",
            "setup-ide",
            "test",
        }
    ),
    "tsx": frozenset({"watch"}),
}
_DIRECT_SCRIPT_SUFFIXES: dict[str, tuple[str, ...]] = {
    "bun": (".cjs", ".cts", ".js", ".jsx", ".mjs", ".mts", ".ts", ".tsx"),
    "scala": (".java", ".sc", ".scala"),
    "tsx": (".cjs", ".cts", ".js", ".jsx", ".mjs", ".mts", ".ts", ".tsx"),
}


def _looks_like_direct_script_path(base: str, token: str) -> bool:
    """Disambiguate script paths from version-extensible CLI subcommands."""
    lowered = token.lower()
    return (
        "/" in token
        or token in {".", ".."}
        or lowered.endswith(_DIRECT_SCRIPT_SUFFIXES.get(base, ()))
    )


def _has_inline_payload(args: list[str]) -> bool:
    """True when the interpreter runs code from argv or stdin rather than a file.

    Clustered short flags count (``python3 -Sc "…"``, ``perl -ne '…'``). A false
    positive costs an approval prompt; a false negative costs arbitrary code
    execution — so this errs toward IRREVERSIBLE.
    """
    for token in args:
        if token in _INLINE_PAYLOAD_FLAGS:
            return True
        if len(token) > 1 and token[0] == "-" and not token.startswith("--"):
            if any(ch in token[1:] for ch in "ce"):
                return True
    return False


def _php_safe_option_tail(args: list[str]) -> list[str] | None:
    """Strip a narrow set of PHP options whose effects are fully known."""
    index = 0
    while index < len(args):
        token = args[index]
        if token == "-n":
            index += 1
            continue
        if token == "-d":
            if index + 1 >= len(args):
                return None
            setting = args[index + 1]
            index += 2
        elif token.startswith("-d") and len(token) > 2:
            setting = token[2:]
            index += 1
        else:
            break
        if not re.fullmatch(r"opcache\.enable(?:_cli)?=[01]", setting, flags=re.I):
            return None
    return args[index:]


def _php_loopback_server(args: list[str]) -> bool:
    """Whether PHP args are exactly a loopback-only built-in server invocation."""
    tail = _php_safe_option_tail(args)
    if tail is None or len(tail) != 2 or tail[0] != "-S":
        return False
    address = tail[1]
    host, separator, port_text = address.rpartition(":")
    if not separator or host.lower() not in {"127.0.0.1", "localhost", "[::1]"}:
        return False
    try:
        port = int(port_text)
    except ValueError:
        return False
    return 1 <= port <= 65535


def _interpreter_script_operand(base: str, args: list[str]) -> str | None:
    """Return a proven script operand; reject program-text and ambiguous options."""
    if base in _PYTHON_INTERPRETERS:
        index = 0
        safe_short_flags = frozenset("bBdEhIOPqRsSuvVx")
        while index < len(args):
            token = args[index]
            if token == "--":
                return args[index + 1] if index + 1 < len(args) else None
            if token in {"-m", "--module"}:
                return None
            if token == "-X" or token.startswith("-X"):
                # -X options are version-extensible and include external write
                # destinations such as pycache_prefix. Unknown modes fail closed.
                return None
            if token in {"-W", "--check-hash-based-pycs"}:
                if index + 1 >= len(args):
                    return None
                index += 2
                continue
            if token.startswith("-W") or token.startswith("--check-hash-based-pycs="):
                index += 1
                continue
            if token.startswith("--"):
                if token in {"--help", "--version"}:
                    index += 1
                    continue
                return None
            if token.startswith("-"):
                if not token[1:] or any(char not in safe_short_flags for char in token[1:]):
                    return None
                index += 1
                continue
            return token
        return None

    if base in _NODE_INTERPRETERS:
        index = 0
        safe_flags = {
            "--check",
            "--no-deprecation",
            "--no-warnings",
            "--trace-deprecation",
            "--trace-warnings",
        }
        program_flags = {
            "-p",
            "--print",
            "-r",
            "--require",
            "--loader",
            "--experimental-loader",
            "--import",
        }
        while index < len(args):
            token = args[index]
            head = token.split("=", 1)[0]
            if token == "--":
                return args[index + 1] if index + 1 < len(args) else None
            if head in program_flags:
                return None
            if token in safe_flags:
                index += 1
                continue
            if token.startswith("-"):
                return None
            return token
        return None

    if base in _SHELL_INTERPRETERS:
        index = 0
        safe_short_flags = frozenset("abfhkmnptuvx")
        safe_long_flags = {
            "--noprofile",
            "--norc",
            "--posix",
            "--restricted",
            "--verbose",
        }
        while index < len(args):
            token = args[index]
            if token == "--":
                return args[index + 1] if index + 1 < len(args) else None
            if token in {"-s", "--stdin"}:
                return None
            if token in {"-O", "+O", "-o", "+o"}:
                if index + 1 >= len(args):
                    return None
                index += 2
                continue
            if token in safe_long_flags:
                index += 1
                continue
            if token.startswith("-") or token.startswith("+"):
                if not token[1:] or any(char not in safe_short_flags for char in token[1:]):
                    return None
                index += 1
                continue
            return token
        return None

    if base == "bun":
        if not args:
            return None
        if args[0] == "--":
            return args[1] if len(args) > 1 else None
        if (
            args[0].lower() in _BUN_SUBCOMMANDS
            or args[0].startswith("-")
            or not _looks_like_direct_script_path(base, args[0])
        ):
            return None
        return args[0]

    if base == "php":
        tail = _php_safe_option_tail(args)
        if tail is None or not tail:
            return None
        if tail[0] == "--":
            return tail[1] if len(tail) > 1 else None
        return tail[0] if not tail[0].startswith("-") else None

    if base in _DIRECT_SCRIPT_INTERPRETERS:
        if not args:
            return None
        if args[0] == "--":
            return args[1] if len(args) > 1 else None
        if args[0].lower() in _DIRECT_INTERPRETER_SUBCOMMANDS.get(base, frozenset()):
            return None
        if base in _DIRECT_SCRIPT_SUFFIXES and not _looks_like_direct_script_path(base, args[0]):
            return None
        return args[0] if not args[0].startswith("-") else None
    return None


def _classify_segment(
    argv: list[str],
    project_dir: str | None,
    extra_roots: list[str] | None = None,
    bindings: dict[str, str] | None = None,
    *,
    effective_cwd: str | None = None,
) -> ActionClass:
    if not argv:
        return ActionClass.IRREVERSIBLE
    if _is_assignment_only(argv):
        # A segment that is ONLY `NAME=value` bindings runs no command at all, so
        # deny-by-default would hard-stop the whole command over a variable. A
        # segment that ALSO runs something (`PATH=/evil ls`) does NOT land here —
        # it keeps its deny-by-default class, because the prefix changes the run.
        return ActionClass.READ_ONLY
    # AUTO-APPROVE 2a: strip inert env prefixes (NODE_PATH=…); refuse exec-altering ones.
    stripped = _strip_inert_env_prefix(argv)
    if stripped is None:
        return ActionClass.IRREVERSIBLE
    argv = stripped
    if not argv:
        return ActionClass.IRREVERSIBLE
    if argv[0].startswith("$"):
        return ActionClass.IRREVERSIBLE  # $VAR-as-executable is unprovable
    # Scope for path proofs: after `cd <in-scope>` the next segments use that cwd.
    scope_dir = effective_cwd or project_dir
    base = _token_basename(argv[0]).lower()
    args = argv[1:]
    # AUTO-APPROVE 2b: bare `cd <in-scope-dir>` is read-only (rebinding only).
    if base == "cd":
        parsed = _cd_target_and_rest(argv)
        if parsed is None:
            return ActionClass.IRREVERSIBLE
        target, _rest = parsed
        if _path_in_project(target, scope_dir, extra_roots, bindings):
            return ActionClass.READ_ONLY
        return ActionClass.IRREVERSIBLE
    # Shell loop / conditional keywords: classify the body after `do`/`then`.
    if base in {"for", "while", "until", "if", "elif", "else", "fi", "done", "then", "in"}:
        if base in {"do", "then", "else"} and len(argv) > 1:
            return _classify_segment(
                argv[1:],
                project_dir,
                extra_roots,
                bindings,
                effective_cwd=effective_cwd,
            )
        return ActionClass.READ_ONLY  # pure structure
    if base == "do" and len(argv) > 1:
        return _classify_segment(
            argv[1:], project_dir, extra_roots, bindings, effective_cwd=effective_cwd
        )
    # AUTO-APPROVE 2c: mkdir/touch inside granted roots are in-scope creates.
    if base in _SCOPED_CREATE_COMMANDS:
        operands = [t for t in args if not t.startswith("-") or t == "-"]
        if not operands:
            return ActionClass.IRREVERSIBLE
        for operand in operands:
            if not _path_in_project(operand, scope_dir, extra_roots, bindings):
                return ActionClass.IRREVERSIBLE
        return ActionClass.INTERNAL_REVERSIBLE
    if base == "sed":
        return ActionClass.READ_ONLY if _sed_is_read_only(args) else ActionClass.IRREVERSIBLE
    if base in {"awk", "gawk", "nawk", "mawk"}:
        return ActionClass.READ_ONLY if _awk_is_read_only(args) else ActionClass.IRREVERSIBLE
    if base in _INTERPRETERS:
        # Inline payloads are arbitrary code and are ALWAYS irreversible.
        # A denylist over Turing-complete payload (os as o, getattr, base64) cannot
        # be made sound — reject the entire inline shape instead.
        if _has_inline_payload(args):
            return ActionClass.IRREVERSIBLE
        # Script-path branch: still hard-stop on delete/RCE markers in the argv
        # (e.g. a script path that is itself a shell one-liner is rare but the
        # markers catch obvious destroyers when invoked as `python evil.py`).
        joined = " ".join(argv).lower()
        if any(marker in joined for marker in _DELETE_SUBSTRINGS):
            return ActionClass.IRREVERSIBLE
        if re.search(r"(?<![\w.-])(rm|rmdir|unlink|shred|mkfs|dd)(?![\w.-])", joined):
            return ActionClass.IRREVERSIBLE
        if any(marker in joined for marker in _DANGEROUS_INTERPRETER_MARKERS):
            return ActionClass.IRREVERSIBLE
        if re.search(
            r"(curl|wget|fetch)\b[^|;\n]{0,80}\|\s*(sh|bash|zsh|dash)\b",
            joined,
        ):
            return ActionClass.IRREVERSIBLE
        if base == "php" and _php_loopback_server(args):
            return ActionClass.INTERNAL_REVERSIBLE if scope_dir else ActionClass.IRREVERSIBLE
        # Remaining shape is `interpreter <script> [args]`. Auto-run only when a
        # project scope exists AND the script itself resolves inside that scope.
        if not scope_dir:
            return ActionClass.IRREVERSIBLE
        script = _interpreter_script_operand(base, args)
        if script is None:
            return ActionClass.IRREVERSIBLE
        if not _path_in_project(script, scope_dir, extra_roots, bindings):
            return ActionClass.IRREVERSIBLE
        return ActionClass.INTERNAL_REVERSIBLE
    if base == "find":
        return ActionClass.READ_ONLY if _find_is_read_only(args) else ActionClass.IRREVERSIBLE
    if base == "rg":
        return ActionClass.READ_ONLY if _rg_is_read_only(args) else ActionClass.IRREVERSIBLE
    if base == "git":
        # AUTO-APPROVE 2d: local write (add/commit/…) inside a granted root.
        if _git_is_read_only(args):
            return ActionClass.READ_ONLY
        if _git_is_local_write(args) and scope_dir:
            return ActionClass.INTERNAL_REVERSIBLE
        return ActionClass.IRREVERSIBLE
    if base == "sort":
        sort_target = _sort_output_target(args)
        return (
            ActionClass.READ_ONLY
            if sort_target is None
            else _write_target_class(sort_target, scope_dir, extra_roots, bindings)
        )
    if base == "uniq":
        uniq_target = _uniq_output_target(args)
        return (
            ActionClass.READ_ONLY
            if uniq_target is None
            else _write_target_class(uniq_target, scope_dir, extra_roots, bindings)
        )
    if base == "file":
        # `file -C` / `--compile` writes a magic database.
        return (
            ActionClass.IRREVERSIBLE
            if "-C" in args or "--compile" in args
            else ActionClass.READ_ONLY
        )
    if base == "sips":
        return ActionClass.READ_ONLY if _sips_is_read_only(args) else ActionClass.IRREVERSIBLE
    if base == "identify":
        return ActionClass.READ_ONLY if _identify_is_read_only(args) else ActionClass.IRREVERSIBLE
    if base in _READ_ONLY_SIMPLE:
        return ActionClass.READ_ONLY
    if base == "sleep":
        # Inert delay; common prefix before a real command (`sleep 2; node …`).
        return ActionClass.READ_ONLY
    # `cat > file <<EOF` / `tee file <<EOF`: body is a redirect write_target;
    # bare cat/tee with no operands is not a deny-by-default mystery command.
    if base in {"cat", "tee"} and not args:
        return ActionClass.READ_ONLY
    if base in _SCOPED_FETCH_COMMANDS:
        if base == "curl":
            return _curl_get_class(args, scope_dir, extra_roots, bindings)
        # wget: auto only when writing into a proven in-scope path (-o/-O).
        # No output path stays irreversible.
        out_path = None
        i = 0
        while i < len(args):
            a = args[i]
            if a in ("-o", "--output", "-O"):
                out_path = args[i + 1] if i + 1 < len(args) else ""
                break
            if a.startswith("--output="):
                out_path = a.split("=", 1)[1]
                break
            i += 1
        if out_path is None or out_path in ("", "-"):
            return ActionClass.IRREVERSIBLE
        if not _path_in_project(out_path, scope_dir, extra_roots, bindings):
            return ActionClass.IRREVERSIBLE
        return ActionClass.INTERNAL_REVERSIBLE
    if base in _WRITE_COMMANDS:
        # EVERY write-target operand must be a proven in-project path; a single
        # escaping operand (absolute OOS or a `../..` relative escape) hard-stops
        # (reviewer B3). cp/mv/install need option-aware destination extraction:
        # `-t DIR` inverts the positional layout and install `-d` makes every
        # operand a destination.
        if base in {"cp", "mv", "install"}:
            parsed_paths = _copy_move_install_targets(base, args)
            if parsed_paths is None:
                return ActionClass.IRREVERSIBLE
            writable, delete_sources = parsed_paths
        else:
            writable = [token for token in args if not token.startswith("-") or token == "-"]
            delete_sources = []
        if not writable and not delete_sources:
            return ActionClass.IRREVERSIBLE
        for operand in writable:
            if base == "tee" and (operand == "-" or _is_safe_sink(operand)):
                continue
            if not _path_in_project(operand, scope_dir, extra_roots, bindings):
                return ActionClass.IRREVERSIBLE
        for operand in delete_sources:
            if not _path_in_primary_from_cwd(operand, project_dir, scope_dir, bindings):
                return ActionClass.IRREVERSIBLE
        return ActionClass.INTERNAL_REVERSIBLE
    return ActionClass.IRREVERSIBLE  # deny-by-default: unrecognised command


def _remote_transport_name(command: str | list[str]) -> str | None:
    if isinstance(command, list):
        if not command or not isinstance(command[0], str):
            return None
        base = _token_basename(command[0]).lower()
        return base if base in {"ssh", "scp", "rsync"} else None
    tokens = _lex(command)
    if not tokens or tokens[0][0] != "WORD":
        return None
    base = _token_basename(tokens[0][1]).lower()
    return base if base in {"ssh", "scp", "rsync"} else None


def is_remote_shell_command(command: Any) -> bool:
    """Whether ``command`` begins with the controlled SSH/SCP/rsync lane."""
    if isinstance(command, list) and all(isinstance(part, str) for part in command):
        return _remote_transport_name(command) is not None
    return isinstance(command, str) and _remote_transport_name(command) is not None


def _option_consumed_inline(option: str, options_with_value: frozenset[str]) -> bool:
    return any(option.startswith(prefix) and option != prefix for prefix in options_with_value)


def _is_ssh_safe_o_option(value: str) -> bool:
    """Check if an SSH -o option value is safe (no code execution risk)."""
    lowered = value.lower()
    # Check exact matches and prefix matches (e.g. ConnectTimeout=8)
    for safe in _SSH_SAFE_O_OPTIONS:
        if safe.endswith("="):
            if lowered.startswith(safe):
                return True
        elif lowered == safe:
            return True
    return False


def _transport_operands(
    words: list[str],
    options_with_value: frozenset[str],
    *,
    safe_flag_options: frozenset[str] = frozenset(),
    rsync: bool = False,
    ssh: bool = False,
) -> list[str] | None:
    """Strip a conservative option set; return None for uncertain rsync options."""
    operands: list[str] = []
    index = 1
    options_done = False
    while index < len(words):
        word = words[index]
        if not options_done and word == "--":
            options_done = True
            index += 1
            continue
        if not options_done and word.startswith("-") and word != "-":
            head = word.split("=", 1)[0]
            if ssh and head == "-o":
                # SSH -o option: check if the value is safe
                if "=" in word:
                    value = word.split("=", 1)[1]
                    if not _is_ssh_safe_o_option(value):
                        return None
                else:
                    index += 1
                    if index >= len(words):
                        return None
                    if not _is_ssh_safe_o_option(words[index]):
                        return None
                index += 1
                continue
            if rsync:
                if head in _RSYNC_SAFE_OPTIONS_WITH_VALUE:
                    if "=" not in word:
                        index += 1
                        if index >= len(words):
                            return None
                elif head in _RSYNC_SAFE_LONG_OPTIONS:
                    pass
                elif word.startswith("--"):
                    return None
                elif not word[1:] or any(flag not in _RSYNC_SAFE_SHORT_FLAGS for flag in word[1:]):
                    return None
            elif word in options_with_value:
                index += 1
                if index >= len(words):
                    return None
            elif _option_consumed_inline(word, options_with_value):
                pass
            elif word not in safe_flag_options:
                return None
            index += 1
            continue
        options_done = True
        operands.append(word)
        index += 1
    return operands


def _remote_path(value: str) -> tuple[str, str] | None:
    """Parse OpenSSH's ``[user@]host:path`` syntax, including bracketed IPv6."""
    if value.startswith("[") or "@[" in value:
        closing = value.find("]:")
        if closing < 0:
            return None
        host = value[: closing + 1]
        return (host, value[closing + 2 :])
    colon = value.find(":")
    slash = value.find("/")
    if colon <= 0 or (slash >= 0 and slash < colon):
        return None
    host = value[:colon]
    path = value[colon + 1 :]
    if not host or not path:
        return None
    return host, path


def _remote_host_is_valid(host: str) -> bool:
    if not host.isascii():
        return False
    user, separator, hostname = host.rpartition("@")
    if separator and (not user or any(not (char.isalnum() or char in "._-") for char in user)):
        return False
    if not separator:
        hostname = host
    if hostname.startswith("[") and hostname.endswith("]"):
        inner = hostname[1:-1]
        return bool(inner) and all(char in "0123456789abcdefABCDEF:." for char in inner)
    return bool(hostname) and all(char.isalnum() or char in "._-" for char in hostname)


def _remote_command_is_ambiguous(command: str) -> bool:
    if any(marker in command for marker in _INDIRECTION_MARKERS):
        return True
    tokens = _lex(command)
    return tokens is None or any(kind == "OP" for kind, _text in tokens)


def _parse_ssh_command(command: str) -> tuple[str, str] | None:
    """Extract a destination and remote operation from ssh/scp/rsync.

    Parsing is lexical and fail-closed.  Shell control operators, redirects,
    substitutions, unsupported rsync options, multiple remote endpoints, and
    interactive ``ssh host`` invocations return ``None``.
    """
    if any(marker in command for marker in _INDIRECTION_MARKERS):
        return None
    tokens = _lex(command)
    if tokens is None or any(kind == "OP" for kind, _text in tokens):
        return None
    words = [text for kind, text in tokens if kind == "WORD"]
    if not words:
        return None
    transport = _token_basename(words[0]).lower()
    if transport == "ssh":
        operands = _transport_operands(
            words, _SSH_OPTIONS_WITH_VALUE, safe_flag_options=_SSH_SAFE_FLAG_OPTIONS, ssh=True
        )
        if operands is None or len(operands) < 1:
            return None
        host = operands[0]
        if not _remote_host_is_valid(host):
            return None
        # If no remote command, treat as interactive (ambiguous)
        if len(operands) < 2:
            return None
        remote_command = " ".join(operands[1:]).strip()
        if not remote_command or _remote_command_is_ambiguous(remote_command):
            return None
        return host, remote_command

    if transport not in {"scp", "rsync"}:
        return None
    operands = _transport_operands(
        words,
        _SCP_OPTIONS_WITH_VALUE if transport == "scp" else frozenset(),
        safe_flag_options=_SCP_SAFE_FLAG_OPTIONS if transport == "scp" else frozenset(),
        rsync=transport == "rsync",
    )
    if operands is None or len(operands) < 2:
        return None
    remote_operands = [
        (index, parsed) for index, item in enumerate(operands) if (parsed := _remote_path(item))
    ]
    if len(remote_operands) != 1:
        return None
    remote_index, (host, path) = remote_operands[0]
    if not _remote_host_is_valid(host) or _remote_command_is_ambiguous(path):
        return None
    destination_index = len(operands) - 1
    if remote_index == destination_index:
        inferred = f"{transport} write on host"
    elif transport == "scp":
        inferred = f"cat {path}"
    else:
        inferred = "rsync on host"
    return host, inferred


def _host_is_granted(host: str, grants: list[str]) -> bool:
    requested_user, requested_host = host.rsplit("@", 1) if "@" in host else (None, host)
    for grant in grants:
        grant_user, grant_host = grant.rsplit("@", 1) if "@" in grant else (None, grant)
        if requested_host.casefold() != grant_host.casefold():
            continue
        if grant_user is None or grant_user == requested_user:
            return True
    return False


# Remote SQL is "read-only" only when EVERY statement is a pure read head and
# contains no write/lock clauses. A leading SELECT used to launder later
# mutations via startswith — that is the non-result-as-favourable bug.
_REMOTE_SQL_READONLY_HEAD_RE = re.compile(
    r"^(?:select|show|describe|desc|explain)\b",
    re.IGNORECASE,
)
_REMOTE_SQL_SELECT_WRITE_RE = re.compile(
    r"\binto\b|\bfor\s+update\b",
    re.IGNORECASE,
)
# PostgreSQL EXPLAIN ANALYZE (and EXPLAIN (ANALYZE ...)) *executes* the statement.
_REMOTE_SQL_EXPLAIN_PREFIX_RE = re.compile(r"^explain\b", re.IGNORECASE)
_REMOTE_SQL_EXPLAIN_OPT_KEYWORD_RE = re.compile(
    r"^(analyze|verbose)\b\s*",
    re.IGNORECASE,
)
_REMOTE_SQL_ANALYZE_IN_OPTS_RE = re.compile(r"\banalyze\b", re.IGNORECASE)


def _remote_sql_statement_is_read_only(statement: str) -> bool:
    """True only when a single SQL statement is proven free of writes.

    ``EXPLAIN`` without ANALYZE is plan-only (read). ``EXPLAIN ANALYZE`` /
    ``EXPLAIN (ANALYZE ...)`` executes the wrapped statement, so the underlying
    statement must itself be proven read-only.
    """
    remaining = statement.strip()
    if not remaining:
        return False

    explain_match = _REMOTE_SQL_EXPLAIN_PREFIX_RE.match(remaining)
    if explain_match:
        rest = remaining[explain_match.end() :].lstrip()
        has_analyze = False
        if rest.startswith("("):
            close = rest.find(")")
            if close < 0:
                return False  # malformed options — fail closed
            opts = rest[1:close]
            has_analyze = bool(_REMOTE_SQL_ANALYZE_IN_OPTS_RE.search(opts))
            rest = rest[close + 1 :].lstrip()
        else:
            # Classic form: EXPLAIN [ANALYZE] [VERBOSE] statement
            while True:
                opt = _REMOTE_SQL_EXPLAIN_OPT_KEYWORD_RE.match(rest)
                if not opt:
                    break
                if opt.group(1).casefold() == "analyze":
                    has_analyze = True
                rest = rest[opt.end() :]
        if not has_analyze:
            # Plan-only EXPLAIN does not execute the statement.
            return True
        remaining = rest
        if not remaining:
            return False

    if not _REMOTE_SQL_READONLY_HEAD_RE.match(remaining):
        return False
    if _REMOTE_SQL_SELECT_WRITE_RE.search(remaining):
        return False
    return True


def _remote_sql_is_read_only(words: list[str]) -> bool:
    """True only when every -c/-e/--execute payload is proven free of writes.

    Fail closed: multi-statement batches and multi-flag payloads must have every
    statement read-only; ``SELECT ... INTO`` / ``FOR UPDATE`` are writes (or
    write-intent locks) and must not claim READ_ONLY just because the text
    begins with SELECT. ``EXPLAIN ANALYZE`` executes its wrapped statement, so
    a write under ANALYZE is not read-only. A first pure-SELECT ``-c`` must not
    launder a later ``-c`` write — every flag payload is inspected.
    """
    base = _token_basename(words[0]).lower() if words else ""
    if base not in {"psql", "mysql", "sqlite3"}:
        return False
    found_payload = False
    for index, word in enumerate(words[:-1]):
        if word not in {"-c", "-e", "--execute"}:
            continue
        found_payload = True
        query = words[index + 1].lstrip()
        if not query:
            return False
        statements = [part.strip() for part in query.split(";") if part.strip()]
        if not statements:
            return False
        for statement in statements:
            if not _remote_sql_statement_is_read_only(statement):
                return False
    return found_payload


def _remote_has_keyword(command: str, keywords: frozenset[str]) -> bool:
    lowered = command.casefold()
    tokens = _lex(command)
    lexical_text = " ".join(text.casefold() for kind, text in (tokens or []) if kind == "WORD")
    words = set(lexical_text.split())
    for keyword in keywords:
        needle = keyword.casefold()
        if " " in needle or needle == ">":
            if needle in lowered or needle in lexical_text:
                return True
        elif needle in words:
            return True
    return False


def _classify_remote_command(host: str, remote_command: str) -> ActionClass:
    if _remote_command_is_ambiguous(remote_command):
        return ActionClass.IRREVERSIBLE
    if _remote_has_keyword(remote_command, _REMOTE_MONEY_KEYWORDS):
        return ActionClass.IRREVERSIBLE
    tokens = _lex(remote_command)
    if tokens is None:
        return ActionClass.IRREVERSIBLE
    words = [text for kind, text in tokens if kind == "WORD"]
    if not words or _has_destructive_primitive(words):
        return ActionClass.IRREVERSIBLE
    if _remote_has_keyword(remote_command, _REMOTE_DESTRUCTIVE_KEYWORDS):
        return ActionClass.IRREVERSIBLE
    if remote_command == "rsync on host":
        return ActionClass.READ_ONLY
    if remote_command.startswith("scp write") or remote_command.startswith("rsync write"):
        return ActionClass.IRREVERSIBLE  # Remote write is serious, always escalate
    if remote_command.startswith("cat "):
        return ActionClass.READ_ONLY  # cat is read-only
    base = _token_basename(words[0]).lower()
    if base == "ps" or (base == "docker" and len(words) >= 2 and words[1] == "ps"):
        return ActionClass.READ_ONLY
    if _remote_sql_is_read_only(words):
        return ActionClass.READ_ONLY
    # The ordinary classifier is reused for its established positive allowlist.
    # Per the operator's policy: remote commands auto-approve unless they have delete/money
    # keywords (which we've already checked). Return CONSEQUENTIAL for commands that
    # made it past the safety checks (e.g., systemctl restart, docker compose up).
    classified = _classify_segment(words, host)
    if classified == ActionClass.READ_ONLY:
        return ActionClass.READ_ONLY
    # For safe remote operations (delete/money keywords already screened),
    # return CONSEQUENTIAL to auto-approve instead of hard-stopping.
    return ActionClass.CONSEQUENTIAL


def _classify_transfer_local_effect(
    command: str, project_dir: str | None, extra_roots: list[str] | None = None
) -> ActionClass:
    """Classify the local destination of a remote-to-local scp/rsync transfer."""
    tokens = _lex(command)
    if tokens is None:
        return ActionClass.IRREVERSIBLE
    words = [text for kind, text in tokens if kind == "WORD"]
    transport = _token_basename(words[0]).lower() if words else ""
    operands = _transport_operands(
        words,
        _SCP_OPTIONS_WITH_VALUE if transport == "scp" else frozenset(),
        safe_flag_options=_SCP_SAFE_FLAG_OPTIONS if transport == "scp" else frozenset(),
        rsync=transport == "rsync",
    )
    if operands is None or len(operands) < 2:
        return ActionClass.IRREVERSIBLE
    remote_indexes = [index for index, item in enumerate(operands) if _remote_path(item)]
    if len(remote_indexes) != 1 or remote_indexes[0] == len(operands) - 1:
        return ActionClass.IRREVERSIBLE
    return _write_target_class(operands[-1], project_dir, extra_roots)


def classify_shell(
    command: Any,
    project_dir: str | None = None,
    ssh_key_grant_session_id: str | None = None,
    extra_roots: list[str] | None = None,
) -> ActionClass:
    """Classify one shell command (string or argv list). Deny-by-default.

    ``project_dir`` is the scope root: for the Session Bridge it is the session's
    working dir; for the runner it is the per-run workspace. Write targets outside
    it (or unprovable) hard-stop; a ``None``/empty scope means no write can be
    proven in-scope -> writes hard-stop.

    ``extra_roots`` (P3) are a bridge session's persisted, validate_grant_dir-checked
    scope grants (the project's ``root_dirs`` + ``allowed_dirs``). They widen ONLY
    the in-scope WRITE boundary: a write proven inside a granted root classifies
    INTERNAL_REVERSIBLE instead of IRREVERSIBLE. The delete, secret-read, money, and
    remote checks run against ``project_dir`` BEFORE the write-scope test and are
    untouched -- a ``rm`` or a credential read inside a granted root still hard-stops.
    """
    if is_remote_shell_command(command):
        command_text = shlex.join(command) if isinstance(command, list) else command
        parsed = _parse_ssh_command(command_text)
        if parsed is None:
            return ActionClass.IRREVERSIBLE
        host, remote_command = parsed
        if ssh_key_grant_session_id is not None:
            from omniagentos.sessions.ssh_keys import read_ssh_key_grant

            if not _host_is_granted(host, read_ssh_key_grant(ssh_key_grant_session_id)):
                return ActionClass.IRREVERSIBLE
        remote_class = _classify_remote_command(host, remote_command)
        transport = _remote_transport_name(command)
        if remote_class == ActionClass.READ_ONLY and transport in {"scp", "rsync"}:
            return max(
                remote_class,
                _classify_transfer_local_effect(command_text, project_dir, extra_roots),
                key=_RANK.__getitem__,
            )
        return remote_class

    if isinstance(command, list) and command and all(isinstance(p, str) for p in command):
        words = [str(p) for p in command]
        if any(marker in " ".join(words).lower() for marker in _DELETE_SUBSTRINGS):
            return ActionClass.IRREVERSIBLE
        argv_delete: ActionClass | None = None
        if _has_destructive_primitive(words):
            # An argv list is not shell-parsed, so there is no assignment prefix to
            # read: the operands must stand on their own literal merit.
            proof = _scoped_delete_class(
                [words],
                [{}],
                project_dir,
                extra_roots,
                segment_controls=[None],
                segment_outgoing_controls=[None],
                segment_depths=[0],
            )
            if proof is None:
                return ActionClass.IRREVERSIBLE
            argv_delete = proof[0]
        if any(_references_secret(w, project_dir) for w in words):
            return ActionClass.IRREVERSIBLE  # reading a credential store hard-stops
        if _move_relocates_secret([words], project_dir):
            return ActionClass.IRREVERSIBLE  # renaming the store or an ancestor hard-stops
        if argv_delete is not None:
            return argv_delete
        return _classify_segment(words, project_dir, extra_roots)

    if not isinstance(command, str) or not command.strip():
        return ActionClass.IRREVERSIBLE

    command_for_lex = _lex_source_without_evaluation(command)
    if command_for_lex is None:
        return ActionClass.IRREVERSIBLE

    if any(marker in command.lower() for marker in _DELETE_SUBSTRINGS):
        return ActionClass.IRREVERSIBLE

    tokens = _lex(command_for_lex)
    if tokens is None:
        return ActionClass.IRREVERSIBLE  # unbalanced quoting -> fail closed
    (
        segments,
        write_targets,
        segment_controls,
        segment_outgoing_controls,
        segment_depths,
        grouping_valid,
    ) = _parse(tokens)
    if not grouping_valid:
        return ActionClass.IRREVERSIBLE
    all_words = [text for kind, text in tokens if kind == "WORD"]
    binding_states = _literal_binding_states(
        segments,
        segment_controls,
        segment_outgoing_controls,
        segment_depths,
    )
    # A delete is proven per-operand rather than vetoed on sight; anything outside
    # the narrow provable shape still hard-stops (see _scoped_delete_class).
    delete_class = ActionClass.IRREVERSIBLE
    proven_delete_segments: set[int] = set()
    if _has_destructive_primitive(all_words):
        proof = _scoped_delete_class(
            segments,
            binding_states,
            project_dir,
            extra_roots,
            segment_controls=segment_controls,
            segment_outgoing_controls=segment_outgoing_controls,
            segment_depths=segment_depths,
        )
        if proof is None:
            return ActionClass.IRREVERSIBLE
        delete_class, proven_delete_segments = proof
    if any(_references_secret(w, project_dir) for w in all_words):
        return ActionClass.IRREVERSIBLE  # reading a credential store hard-stops
    if _move_relocates_secret(segments, project_dir):
        return ActionClass.IRREVERSIBLE  # renaming the store or an ancestor hard-stops

    classes: list[ActionClass] = []
    write_target_segments: set[int] = set()
    for target, segment_index in write_targets:
        bindings = binding_states[segment_index] if segment_index < len(binding_states) else {}
        classes.append(_write_target_class(target, project_dir, extra_roots, bindings))
        write_target_segments.add(segment_index)
    # AUTO-APPROVE 2b: `cd <in-scope> && <cmd>` rebinds cwd for following segments
    # when the cd target is provably inside a granted root. An unprovable cd keeps
    # deny-by-default for that segment (and does not rebind).
    effective_cwd_by_depth: dict[int, str | None] = {0: project_dir}
    cwd_known_by_depth: dict[int, bool] = {0: True}
    cd_seen_by_depth: dict[int, bool] = {0: False}
    previous_depth = 0
    for index, argv in enumerate(segments):
        control = segment_controls[index]
        outgoing_control = segment_outgoing_controls[index]
        depth = segment_depths[index]
        bindings = binding_states[index]
        if depth > previous_depth:
            for nested_depth in range(previous_depth + 1, depth + 1):
                parent_depth = nested_depth - 1
                effective_cwd_by_depth[nested_depth] = effective_cwd_by_depth[parent_depth]
                cwd_known_by_depth[nested_depth] = cwd_known_by_depth[parent_depth]
                # A subshell inherits cwd, but a cd inside it must never leak back
                # into the parent shell's containment proof.
                cd_seen_by_depth[nested_depth] = False
        elif depth < previous_depth:
            for nested_depth in range(previous_depth, depth, -1):
                effective_cwd_by_depth.pop(nested_depth, None)
                cwd_known_by_depth.pop(nested_depth, None)
                cd_seen_by_depth.pop(nested_depth, None)
        previous_depth = depth
        effective_cwd = effective_cwd_by_depth[depth]
        cwd_known = cwd_known_by_depth[depth]
        cd_seen = cd_seen_by_depth[depth]
        # ONLY the segments the delete proof covered take the proven class; every
        # other segment keeps its own classification, hard-stops included.
        if index in proven_delete_segments:
            classes.append(delete_class)
            continue
        if cd_seen and control in {"||", ";", "\n", "&"}:
            # The segment can run on a path where the earlier cd failed, was
            # skipped, or did not persist. Relative-path write proof is no longer
            # available until an absolute, top-level cd re-establishes it.
            cwd_known = False
            cwd_known_by_depth[depth] = False
        # Detect `cd <dir>` and rebind when in-scope before classifying the segment.
        stripped = _strip_inert_env_prefix(argv) if argv else argv
        if stripped is not None and stripped:
            cd_parsed = _cd_target_and_rest(stripped)
            if cd_parsed is not None:
                target, _rest = cd_parsed
                allowed_controls = {None, "&&", ";", "\n"}
                if depth > 0:
                    allowed_controls.add("(")
                if control not in allowed_controls or outgoing_control in {"|", "|&", "&"}:
                    classes.append(ActionClass.IRREVERSIBLE)
                    continue
                expanded = _expand_literal(target.strip().strip("\"'"), bindings)
                if expanded is None or any(ch in expanded for ch in _UNPROVABLE_PATH_CHARS):
                    classes.append(ActionClass.IRREVERSIBLE)
                    continue
                try:
                    candidate = Path(expanded).expanduser()
                    if not candidate.is_absolute():
                        if not cwd_known or effective_cwd is None:
                            raise ValueError("relative cd from an unproven cwd")
                        candidate = Path(effective_cwd) / candidate
                    resolved_cwd = candidate.resolve(strict=False)
                except (OSError, RuntimeError, ValueError):
                    classes.append(ActionClass.IRREVERSIBLE)
                    continue
                if _path_in_project(
                    str(resolved_cwd),
                    project_dir,
                    extra_roots,
                    bindings,
                ):
                    effective_cwd_by_depth[depth] = str(resolved_cwd)
                    cwd_known_by_depth[depth] = True
                    cd_seen_by_depth[depth] = True
                    classes.append(ActionClass.READ_ONLY)
                    continue
                # Unprovable cd: hard-stop and do not rebind.
                classes.append(ActionClass.IRREVERSIBLE)
                continue
        segment_class = _classify_segment(
            argv,
            project_dir,
            extra_roots,
            bindings,
            effective_cwd=effective_cwd,
        )
        if not cwd_known and (
            segment_class is not ActionClass.READ_ONLY or index in write_target_segments
        ):
            segment_class = ActionClass.IRREVERSIBLE
        classes.append(segment_class)
    if not classes:
        return ActionClass.IRREVERSIBLE
    return max(classes, key=_RANK.__getitem__)


__all__ = ["classify_shell", "is_remote_shell_command"]
