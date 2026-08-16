#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export OMNIAGENTOS_SCRIPT_DIR="$SCRIPT_DIR"

# Prefer the repo interpreter so the bundle tooling runs on the same Python the
# rest of the repo is validated against.
PYTHON_BIN="python3"
if [ -x "$SCRIPT_DIR/../../.venv/bin/python3" ]; then
  PYTHON_BIN="$SCRIPT_DIR/../../.venv/bin/python3"
fi

"$PYTHON_BIN" -u - "$@" << 'PY_EOF'
"""Descriptor-pinned local Git bundle backup.

Every create, open, stat, link, unlink and rename below is basename-relative
through a directory descriptor that was opened component-by-component with
O_DIRECTORY | O_NOFOLLOW. Git is invoked with every ambient GIT_* selector
removed, its output is parsed under a strict grammar, and the worktree, Git
directory, common directory and linked-worktree `.git` marker identities are
revalidated around every pathname invocation. Nothing is pushed and no network
operation is attempted: the only output is a local bundle file.
"""

import hashlib
import json
import os
import stat
import subprocess
import sys
import time

HOOK_ENV = "GROK_DRILL_HOOK"
HOOK_DIR_ENV = "GROK_DRILL_HOOK_DIR"
HOOK_ACTIONS = ("pause", "fail", "pause_fail")
HOOK_TIMEOUT_SECONDS = 120.0
HOOK_POLL_SECONDS = 0.005
TEMP_DIR_PREFIX = ".grok-tmp-"
QUARANTINE_PREFIX = ".grok-quarantine-"
TEMP_BASENAME = "backup.bundle"
HEX_DIGITS = "0123456789abcdef"
_GIT_REVALIDATOR = None

# Removed by name in addition to the GIT_ prefix sweep, so the intent is
# documented rather than implied by a prefix test alone.
GIT_SELECTORS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_NAMESPACE",
    "GIT_CEILING_DIRECTORIES",
    "GIT_DISCOVERY_ACROSS_FILESYSTEM",
    "GIT_CONFIG",
    "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_SYSTEM",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
)


class BackupError(Exception):
    """A fail-closed refusal or an evidence mismatch."""


class MissingPath(BackupError):
    """A component of a supplied path does not exist."""


class HookAbort(BaseException):
    """Deterministic test-only asynchronous abort."""


def hook(phase):
    """Test-only rendezvous.

    It can only pause or raise. It never skips, weakens or substitutes a
    control, and it is inert unless the caller supplies BOTH an existing
    GROK_DRILL_HOOK_DIR and a GROK_DRILL_HOOK spec spelled
    ``<phase>:<pause|fail|pause_fail>``. Each firing leaves a marker file so a
    drill can prove the injection actually happened.
    """
    spec = os.environ.get(HOOK_ENV)
    hook_dir = os.environ.get(HOOK_DIR_ENV)
    if not spec or not hook_dir or not os.path.isdir(hook_dir):
        return
    parts = spec.split(":")
    if len(parts) != 2 or parts[0] != phase or parts[1] not in HOOK_ACTIONS:
        return
    action = parts[1]
    if action in ("pause", "pause_fail"):
        with open(os.path.join(hook_dir, phase + ".ready"), "w") as handle:
            handle.write(phase)
        release = os.path.join(hook_dir, phase + ".go")
        deadline = time.monotonic() + HOOK_TIMEOUT_SECONDS
        while not os.path.exists(release):
            if time.monotonic() > deadline:
                raise BackupError("Test hook rendezvous timed out at phase " + phase + ".")
            time.sleep(HOOK_POLL_SECONDS)
    if action in ("fail", "pause_fail"):
        with open(os.path.join(hook_dir, phase + ".fired"), "w") as handle:
            handle.write(phase)
        raise HookAbort("Injected asynchronous abort at phase " + phase + ".")


def unique_name(prefix):
    return prefix + str(os.getpid()) + "-" + os.urandom(8).hex()


def path_components(raw, label):
    if not raw:
        raise BackupError(label + " path is empty.")
    absolute = raw if os.path.isabs(raw) else os.path.join(os.getcwd(), raw)
    parts = []
    for comp in absolute.split(os.sep):
        if comp in ("", "."):
            continue
        if comp == "..":
            raise BackupError(label + " path '" + raw + "' contains a '..' component.")
        parts.append(comp)
    if not parts:
        raise BackupError(label + " path '" + raw + "' does not name an entry.")
    return parts


def join_components(parts):
    return os.sep + os.sep.join(parts)


def open_dir_chain(parts, raw):
    """Open the directory named by ``parts``, refusing a symlink at every step."""
    fd = os.open(os.sep, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for comp in parts:
            try:
                st = os.stat(comp, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                raise MissingPath(
                    "Path component '" + comp + "' of '" + raw + "' does not exist."
                ) from None
            if stat.S_ISLNK(st.st_mode):
                raise BackupError("Path component '" + comp + "' of '" + raw + "' is a symlink.")
            if not stat.S_ISDIR(st.st_mode):
                raise BackupError(
                    "Path component '" + comp + "' of '" + raw + "' is not a directory."
                )
            nfd = os.open(comp, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nfd
    except BaseException:
        os.close(fd)
        raise
    return fd


class HeldDir:
    """A directory descriptor held for the whole operation."""

    def __init__(self, fd, path, parts=None, anchor=None):
        self.fd = fd
        self.path = path
        self.parts = parts
        self.anchor = anchor
        st = os.fstat(fd)
        self.dev = st.st_dev
        self.ino = st.st_ino
        self.mode = st.st_mode

    @classmethod
    def walk(cls, parts, raw):
        return cls(open_dir_chain(parts, raw), join_components(parts), parts=list(parts))

    @classmethod
    def child(cls, parent, name, raw):
        try:
            st = os.stat(name, dir_fd=parent.fd, follow_symlinks=False)
        except FileNotFoundError:
            raise MissingPath("'" + raw + "' does not exist.") from None
        if stat.S_ISLNK(st.st_mode):
            raise BackupError("'" + raw + "' is a symlink.")
        if not stat.S_ISDIR(st.st_mode):
            raise BackupError("'" + raw + "' must be a directory.")
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent.fd)
        try:
            return cls(fd, os.path.join(parent.path, name), anchor=(parent, name))
        except BaseException:
            os.close(fd)
            raise

    def revalidate(self, stage):
        st = os.fstat(self.fd)
        if (st.st_dev, st.st_ino, st.st_mode) != (self.dev, self.ino, self.mode):
            raise BackupError("Held directory " + self.path + " changed identity at " + stage + ".")
        if self.parts is not None:
            probe = open_dir_chain(self.parts, self.path)
            try:
                probe_st = os.fstat(probe)
            finally:
                os.close(probe)
            if (probe_st.st_dev, probe_st.st_ino) != (self.dev, self.ino):
                raise BackupError(
                    "Held directory " + self.path
                    + " no longer resolves to the held directory at " + stage + "."
                )
        if self.anchor is not None:
            parent, name = self.anchor
            parent.revalidate(stage)
            anchor_st = os.stat(name, dir_fd=parent.fd, follow_symlinks=False)
            if stat.S_ISLNK(anchor_st.st_mode) or (
                anchor_st.st_dev,
                anchor_st.st_ino,
            ) != (self.dev, self.ino):
                raise BackupError(
                    "Held directory " + self.path
                    + " no longer resolves to the held directory at " + stage + "."
                )

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


class HeldFile:
    """A file descriptor held together with its pinned parent and basename."""

    def __init__(self, parent, name, fd, raw):
        self.parent = parent
        self.name = name
        self.fd = fd
        self.raw = raw
        self.path = os.path.join(parent.path, name)
        st = os.fstat(fd)
        self.dev = st.st_dev
        self.ino = st.st_ino
        self.mode = st.st_mode

    def revalidate(self, stage):
        self.parent.revalidate(stage)
        held = os.fstat(self.fd)
        if (held.st_dev, held.st_ino, held.st_mode) != (self.dev, self.ino, self.mode):
            raise BackupError("Held file " + self.path + " changed identity at " + stage + ".")
        entry = os.stat(self.name, dir_fd=self.parent.fd, follow_symlinks=False)
        if stat.S_ISLNK(entry.st_mode) or (entry.st_dev, entry.st_ino) != (self.dev, self.ino):
            raise BackupError(
                "Held file " + self.path + " no longer resolves to the held inode at "
                + stage + "."
            )
        return held

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


def fd_sha256(held, stage):
    held.revalidate(stage)
    digest = hashlib.sha256()
    os.lseek(held.fd, 0, os.SEEK_SET)
    while True:
        chunk = os.read(held.fd, 65536)
        if not chunk:
            break
        digest.update(chunk)
    os.lseek(held.fd, 0, os.SEEK_SET)
    held.revalidate(stage)
    return digest.hexdigest()


def stable_descriptor_path(held, label):
    path = "/dev/fd/" + str(held.fd)
    if not os.path.exists(path):
        raise BackupError("Descriptor-backed Git bundle access is unavailable for " + label + ".")
    return path


def identity_of(st, path):
    return {
        "dev": st.st_dev,
        "ino": st.st_ino,
        "mode": st.st_mode,
        "nlink": st.st_nlink,
        "size": st.st_size,
        "path": path,
    }


def emit_receipt(receipt_text):
    """The terminal success action; a partial write is a rollback failure."""
    payload = receipt_text + "\n"
    if sys.stdout.write(payload) != len(payload):
        raise BackupError("Receipt stdout write was partial.")
    sys.stdout.flush()


def make_temp_dir(parent):
    for _ in range(64):
        name = unique_name(TEMP_DIR_PREFIX)
        held = None
        mkdir_attempted = False
        mkdir_succeeded = False
        mkdir_failure = None
        try:
            mkdir_attempted = True
            try:
                os.mkdir(name, 0o700, dir_fd=parent.fd)
            except FileExistsError:
                continue
            except OSError as known_failure:
                mkdir_failure = known_failure
                raise
            mkdir_succeeded = True
            held = HeldDir.child(parent, name, os.path.join(parent.path, name))
            hook("post_temp_pin")
            os.fchmod(held.fd, 0o700)
            held.mode = os.fstat(held.fd).st_mode
            held.revalidate("temporary-directory creation")
            return name, held
        except BaseException as exc:
            report = []
            if held is not None:
                if cleanup_temp_dir(parent, name, held, report):
                    report.append(
                        "Temporary directory creation failed after pinning; removed safely."
                    )
                else:
                    report.append(
                        "Temporary directory creation failed after pinning; "
                        "identity-authoritative cleanup did not complete and no further removal "
                        "was attempted."
                    )
            elif mkdir_succeeded:
                report.append("Temporary directory was created before it could be pinned; preserved.")
            elif mkdir_failure is not None:
                report.append(
                    "Temporary directory mkdir failed with a known failure: "
                    + (str(mkdir_failure) or mkdir_failure.__class__.__name__)
                    + "."
                )
            elif mkdir_attempted:
                report.append(
                    "Temporary directory mkdir was interrupted before its outcome was known; "
                    "any created entry was preserved."
                )
            else:
                report.append("Temporary directory creation failed before mkdir was attempted.")
            if held is not None:
                held.close()
            raise BackupError("; ".join(report)) from exc
    raise BackupError("Could not create a private temporary directory.")


def cleanup_temp_dir(parent, name, held, report, expected_file=None, remove_expected=False):
    """Remove the private temporary directory, but only if it is still the
    exact object this invocation created. A replacement is preserved."""
    try:
        parent.revalidate("temporary cleanup")
        st = os.fstat(held.fd)
        if (st.st_dev, st.st_ino) != (held.dev, held.ino):
            report.append("Temporary directory descriptor changed identity; refusing removal.")
            return False
        entry = os.stat(name, dir_fd=parent.fd, follow_symlinks=False)
        if stat.S_ISLNK(entry.st_mode) or (entry.st_dev, entry.st_ino) != (held.dev, held.ino):
            report.append("Temporary directory '" + name + "' was replaced; refusing removal.")
            return False
        if expected_file is not None:
            expected_file.revalidate("temporary artifact cleanup")
            if remove_expected:
                os.unlink(expected_file.name, dir_fd=held.fd)
                held.revalidate("temporary artifact removal")
                expected_st = os.fstat(expected_file.fd)
                if (expected_st.st_dev, expected_st.st_ino) != (
                    expected_file.dev,
                    expected_file.ino,
                ):
                    report.append("Held temporary artifact changed during removal.")
                    return False
            else:
                report.append("Expected temporary artifact remains; refusing directory removal.")
                return False
        children = os.listdir(held.fd)
        if children:
            report.append(
                "Unexpected temporary entries were preserved: " + ", ".join(sorted(children)) + "."
            )
            return False
        os.rmdir(name, dir_fd=parent.fd)
        os.fsync(parent.fd)
    except BaseException as exc:
        report.append("Could not remove the temporary directory safely: " + str(exc))
        return False
    return True


def verify_removed_temp_dir(parent, name, held):
    """Prove the held private directory was the object removed from its parent."""
    parent.revalidate("post-cleanup temporary-directory validation")
    st = os.fstat(held.fd)
    if (st.st_dev, st.st_ino, st.st_mode) != (held.dev, held.ino, held.mode):
        raise BackupError("Held temporary directory changed during cleanup.")
    try:
        os.stat(name, dir_fd=parent.fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    raise BackupError("Temporary directory name remains after cleanup.")


def preserve_quarantined_foreign(quarantined, report):
    """Report a foreign object retained under its exact quarantine basename."""
    report.append(
        "Foreign object preserved as '" + quarantined.name
        + "'; portable identity-bound no-replace restoration is unavailable."
    )


def quarantine_expected(parent, name, expected_dev, expected_ino, report):
    """Atomically move ``name`` aside and delete it only when it is exactly the
    inode this invocation published. Anything else is preserved, never removed."""
    qname = unique_name(QUARANTINE_PREFIX)
    try:
        os.rename(name, qname, src_dir_fd=parent.fd, dst_dir_fd=parent.fd)
    except FileNotFoundError:
        return
    except OSError as exc:
        report.append("Could not quarantine '" + name + "': " + str(exc))
        return
    quarantined = None
    try:
        qfd = os.open(qname, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent.fd)
        quarantined = HeldFile(parent, qname, qfd, qname)
        qst = os.fstat(quarantined.fd)
        if stat.S_ISREG(qst.st_mode) and (qst.st_dev, qst.st_ino) == (expected_dev, expected_ino):
            quarantined.revalidate("immediately before quarantined published-object removal")
            os.unlink(qname, dir_fd=parent.fd)
            report.append("Published object '" + name + "' was rolled back.")
            return
        preserve_quarantined_foreign(quarantined, report)
    except BaseException as exc:
        report.append(
            "Quarantined entry '" + qname + "' was preserved (could not pin/remove safely: "
            + str(exc) + ")."
        )
    finally:
        if quarantined is not None:
            quarantined.close()


def git_env():
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    for name in GIT_SELECTORS:
        env.pop(name, None)
    return env


def run_git(args, stdout_fd=None, pass_fds=()):
    """Run one Git command between two repository identity snapshots.

    stdout remains bytes: universal-newline translation would turn hostile CRLF
    or bare-CR output into accepted LF records. An inherited output descriptor is
    explicit so Git never creates a bundle via a reopenable pathname.
    """
    global _GIT_REVALIDATOR
    label = "git invocation: " + " ".join(args)
    if _GIT_REVALIDATOR is not None:
        _GIT_REVALIDATOR("before " + label)
    kwargs = {
        "stderr": subprocess.PIPE,
        "env": git_env(),
        "check": False,
        "pass_fds": tuple(pass_fds),
    }
    if stdout_fd is None:
        kwargs["stdout"] = subprocess.PIPE
    else:
        kwargs["stdout"] = stdout_fd
    try:
        result = subprocess.run(["git", *args], **kwargs)
    except BaseException as primary:
        try:
            if _GIT_REVALIDATOR is not None:
                _GIT_REVALIDATOR("after " + label)
        except BaseException as secondary:
            raise primary from secondary
        raise
    try:
        if _GIT_REVALIDATOR is not None:
            _GIT_REVALIDATOR("after " + label)
    except BaseException:
        raise
    return result


def strict_text(raw, what):
    if b"\r" in raw:
        raise BackupError("Malformed " + what + ": carriage return in the output.")
    try:
        return raw.decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise BackupError("Malformed " + what + ": non-UTF-8 output.") from exc


def git_stderr(raw):
    return raw.decode("utf-8", "backslashreplace").strip()


def single_record(raw, what):
    raw = strict_text(raw, what)
    if not raw.endswith("\n"):
        raise BackupError("Malformed " + what + ": missing the terminal newline.")
    body = raw[:-1]
    if "\n" in body or "\r" in body:
        raise BackupError("Malformed " + what + ": expected exactly one record.")
    if body == "":
        raise BackupError("Malformed " + what + ": empty record.")
    return body


def exact_records(raw, count, what):
    raw = strict_text(raw, what)
    if not raw.endswith("\n"):
        raise BackupError("Malformed " + what + ": missing the terminal newline.")
    body = raw[:-1]
    if "\r" in body:
        raise BackupError("Malformed " + what + ": carriage return in the output.")
    records = body.split("\n")
    if len(records) != count:
        raise BackupError(
            "Malformed " + what + ": expected " + str(count) + " records, got "
            + str(len(records)) + "."
        )
    for record in records:
        if record == "":
            raise BackupError("Malformed " + what + ": empty record.")
    return records


def parse_ref_records(raw, oid_len, what, allow_head):
    raw = strict_text(raw, what)
    refs = {}
    if raw == "":
        return refs
    if not raw.endswith("\n"):
        raise BackupError("Malformed " + what + ": missing the terminal newline.")
    body = raw[:-1]
    if "\r" in body:
        raise BackupError("Malformed " + what + ": carriage return in the output.")
    for line in body.split("\n"):
        if line == "":
            raise BackupError("Malformed " + what + ": empty record.")
        if len(line) < oid_len + 2 or line[oid_len] != " ":
            raise BackupError("Malformed git record grammar in " + what + ": " + repr(line))
        oid = line[:oid_len]
        name = line[oid_len + 1:]
        for char in oid:
            if char not in HEX_DIGITS:
                raise BackupError("Non-hexadecimal OID in " + what + ": " + repr(oid))
        for char in name:
            if char.isspace():
                raise BackupError("Malformed refname whitespace in " + what + ": " + repr(name))
        if name == "HEAD":
            if not allow_head:
                raise BackupError("Invalid refname (pseudoref) in " + what + ": " + repr(name))
        elif not name.startswith("refs/"):
            raise BackupError("Invalid refname (pseudoref) in " + what + ": " + repr(name))
        else:
            check = run_git(["check-ref-format", name])
            if check.returncode != 0:
                raise BackupError("Invalid refname in " + what + ": " + repr(name))
        if name in refs:
            raise BackupError("Duplicate refname in " + what + ": " + repr(name))
        refs[name] = oid
    return refs


def object_format(repo):
    res = run_git(["-C", repo, "rev-parse", "--show-object-format"])
    if res.returncode != 0:
        raise BackupError(
            "git rev-parse --show-object-format failed: " + git_stderr(res.stderr)
        )
    fmt = single_record(res.stdout, "object format output")
    if fmt not in ("sha1", "sha256"):
        raise BackupError("Unsupported object format: " + repr(fmt))
    return fmt, 40 if fmt == "sha1" else 64


def repository_paths(repo):
    res = run_git(["-C", repo, "rev-parse", "--show-toplevel", "--git-dir", "--git-common-dir"])
    if res.returncode != 0:
        raise BackupError("'" + repo + "' is not a valid git repository or worktree.")
    records = exact_records(res.stdout, 3, "repository path records")
    # Git legitimately emits lexical `../.git` paths when the supplied source
    # is a subdirectory of a worktree.  Normalise only dot components here;
    # HeldDir.walk still opens and pins every resulting component with
    # O_NOFOLLOW, so this is not a symlink-resolving trust boundary.
    return [
        os.path.normpath(record if os.path.isabs(record) else os.path.join(repo, record))
        for record in records
    ]


def read_marker(src_dir):
    try:
        st = os.stat(".git", dir_fd=src_dir.fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(st.st_mode):
        raise BackupError("The repository's '.git' entry is a symlink.")
    marker = {
        "path": os.path.join(src_dir.path, ".git"),
        "dev": st.st_dev,
        "ino": st.st_ino,
        "mode": st.st_mode,
        "nlink": st.st_nlink,
        "size": st.st_size if stat.S_ISREG(st.st_mode) else None,
    }
    if stat.S_ISREG(st.st_mode):
        fd = os.open(".git", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=src_dir.fd)
        try:
            content = os.read(fd, st.st_size + 1)
        finally:
            os.close(fd)
        try:
            marker_text = content.decode("utf-8", "strict")
        except UnicodeDecodeError as exc:
            raise BackupError("The repository '.git' marker is not strict UTF-8.") from exc
        if "\r" in marker_text or not marker_text.endswith("\n"):
            raise BackupError("The repository '.git' marker has noncanonical newlines.")
        if marker_text.count("\n") != 1 or not marker_text.startswith("gitdir: "):
            raise BackupError("The repository '.git' marker has invalid gitdir grammar.")
        marker["content_sha256"] = hashlib.sha256(content).hexdigest()
        marker["content"] = marker_text
        marker["gitdir_target"] = marker_text[len("gitdir: "):-1]
    return marker


def validate_marker_target(top_dir, git_dir, marker):
    if marker is None or marker["size"] is None:
        return
    target = marker["gitdir_target"]
    if not target or ".." in target.split(os.sep):
        raise BackupError("The repository '.git' marker has an unsafe gitdir target.")
    resolved = target if os.path.isabs(target) else os.path.join(top_dir.path, target)
    if os.path.abspath(resolved) != git_dir.path:
        raise BackupError("The repository '.git' marker gitdir target disagrees with Git discovery.")


def revalidate_marker(src_dir, marker, stage):
    try:
        st = os.stat(".git", dir_fd=src_dir.fd, follow_symlinks=False)
    except FileNotFoundError:
        if marker is None:
            return
        raise BackupError("The repository's '.git' marker vanished at " + stage + ".") from None
    if marker is None:
        raise BackupError("The repository's '.git' marker appeared at " + stage + ".")
    if stat.S_ISLNK(st.st_mode) or (st.st_dev, st.st_ino, st.st_mode) != (
        marker["dev"],
        marker["ino"],
        marker["mode"],
    ):
        raise BackupError("The repository's '.git' marker changed identity at " + stage + ".")
    if marker["size"] is not None and st.st_size != marker["size"]:
        raise BackupError("The repository's '.git' marker changed size at " + stage + ".")
    if marker["size"] is not None:
        fd = os.open(".git", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=src_dir.fd)
        try:
            content = os.read(fd, st.st_size + 1)
        finally:
            os.close(fd)
        if hashlib.sha256(content).hexdigest() != marker["content_sha256"]:
            raise BackupError("The repository's '.git' marker changed content at " + stage + ".")


def is_within(child, parent):
    if child == parent:
        return True
    return child.startswith(parent.rstrip(os.sep) + os.sep)


def bundle_ref_map(repo, held_bundle, oid_len):
    bundle_path = stable_descriptor_path(held_bundle, "bundle verification")
    held_bundle.revalidate("before descriptor-bound bundle verification")
    try:
        # /dev/fd/N reopens the inherited file description on macOS.  Git reads
        # from its current offset, so reset it before *each* independent bundle
        # command; otherwise a successful first read leaves list-heads at EOF.
        os.lseek(held_bundle.fd, 0, os.SEEK_SET)
        res = run_git(
            ["-C", repo, "bundle", "verify", bundle_path], pass_fds=(held_bundle.fd,)
        )
    except BaseException as primary:
        try:
            held_bundle.revalidate("after descriptor-bound bundle verify")
        except BaseException as secondary:
            raise primary from secondary
        raise
    held_bundle.revalidate("after descriptor-bound bundle verify")
    if res.returncode != 0:
        raise BackupError("Git bundle verification failed: " + git_stderr(res.stderr))
    held_bundle.revalidate("between descriptor-bound bundle verification commands")
    try:
        os.lseek(held_bundle.fd, 0, os.SEEK_SET)
        res = run_git(
            ["-C", repo, "bundle", "list-heads", bundle_path], pass_fds=(held_bundle.fd,)
        )
    except BaseException as primary:
        try:
            held_bundle.revalidate("after descriptor-bound bundle list-heads")
        except BaseException as secondary:
            raise primary from secondary
        raise
    held_bundle.revalidate("after descriptor-bound bundle list-heads")
    if res.returncode != 0:
        raise BackupError("git bundle list-heads failed: " + git_stderr(res.stderr))
    refs = parse_ref_records(res.stdout, oid_len, "git bundle list-heads output", True)
    held_bundle.revalidate("after descriptor-bound bundle verification")
    return refs


def source_ref_map(repo, oid_len):
    res = run_git(["-C", repo, "show-ref"])
    if res.returncode not in (0, 1):
        raise BackupError("git show-ref failed: " + git_stderr(res.stderr))
    if res.returncode == 1 and res.stdout != b"":
        raise BackupError("Malformed git show-ref output: exit 1 requires canonical empty stdout.")
    return parse_ref_records(res.stdout, oid_len, "git show-ref output", False)


def head_state(repo, oid_len):
    res = run_git(["-C", repo, "rev-parse", "--verify", "--quiet", "HEAD"])
    if res.returncode != 0:
        raise BackupError("Could not resolve HEAD; refusing to bundle an unborn HEAD.")
    oid = single_record(res.stdout, "rev-parse HEAD output")
    if len(oid) != oid_len:
        raise BackupError("Malformed HEAD object id length: " + repr(oid))
    for char in oid:
        if char not in HEX_DIGITS:
            raise BackupError("Non-hexadecimal OID in rev-parse HEAD output: " + repr(oid))
    sym = run_git(["-C", repo, "symbolic-ref", "--quiet", "HEAD"])
    if sym.returncode == 0:
        name = single_record(sym.stdout, "symbolic-ref HEAD output")
        if not name.startswith("refs/"):
            raise BackupError("Invalid refname (pseudoref) for HEAD: " + repr(name))
        return oid, name
    if sym.returncode == 1:
        return oid, "DETACHED"
    raise BackupError("git symbolic-ref HEAD failed: " + git_stderr(sym.stderr))


def source_evidence(repo, oid_len):
    # show-ref is parsed with allow_head=False, so a pseudoref smuggled into its
    # output is already refused; HEAD only enters the map from head_state().
    refs = source_ref_map(repo, oid_len)
    head_oid, head_ref = head_state(repo, oid_len)
    if head_ref != "DETACHED":
        checked = run_git(["check-ref-format", head_ref])
        if checked.returncode != 0:
            raise BackupError("Attached HEAD ref is invalid: " + repr(head_ref))
        if refs.get(head_ref) != head_oid:
            raise BackupError("Attached HEAD ref is missing or does not match the recorded HEAD OID.")
    refs["HEAD"] = head_oid
    return refs, head_oid, head_ref


def validate_source_ref_map(repo, refs, oid_len):
    """Prove the parsed ref snapshot still names the advertised objects.

    This makes the explicit `bundle create` revision list fail closed before
    Git receives a synthetic-but-lexically-valid ref from a raced or poisoned
    `show-ref` result.
    """
    for name, expected_oid in refs.items():
        if name == "HEAD":
            continue
        resolved = run_git(["-C", repo, "rev-parse", "--verify", "--quiet", name])
        if resolved.returncode != 0:
            raise BackupError("Source ref is absent or unverifiable: " + repr(name))
        actual_oid = single_record(resolved.stdout, "rev-parse ref output")
        if len(actual_oid) != oid_len or actual_oid != expected_oid:
            raise BackupError("Source ref does not match its pinned object id: " + repr(name))


def run(argv):
    global _GIT_REVALIDATOR
    src_raw = argv[1]
    dst_raw = argv[2]
    src_parts = path_components(src_raw, "Source")
    dst_parts = path_components(dst_raw, "Destination")
    src_name = src_parts[-1]
    dst_name = dst_parts[-1]

    src_parent = None
    src_dir = None
    dst_parent = None
    top_dir = None
    git_dir = None
    common_dir = None
    tmp_dir = None
    tmp_file = None
    published = None
    try:
        src_parent = HeldDir.walk(src_parts[:-1], src_raw)
        dst_parent = HeldDir.walk(dst_parts[:-1], dst_raw)

        if (src_parent.dev, src_parent.ino, src_name) == (dst_parent.dev, dst_parent.ino, dst_name):
            raise BackupError("Source and destination cannot be the same object.")

        hook("pre_open")
        src_parent.revalidate("pre-open hook")
        dst_parent.revalidate("pre-open hook")

        src_dir = HeldDir.child(src_parent, src_name, src_raw)

        try:
            dst_entry = os.stat(dst_name, dir_fd=dst_parent.fd, follow_symlinks=False)
        except FileNotFoundError:
            dst_entry = None
        if dst_entry is not None:
            if stat.S_ISLNK(dst_entry.st_mode):
                raise BackupError("Destination '" + dst_raw + "' is a symlink.")
            raise BackupError("Destination path '" + dst_raw + "' already exists.")

        src_parent.revalidate("before bootstrap repository discovery")
        src_dir.revalidate("before bootstrap repository discovery")
        discovery_paths = repository_paths(src_dir.path)
        src_parent.revalidate("after bootstrap repository discovery")
        src_dir.revalidate("after bootstrap repository discovery")
        top_path, git_path, common_path = discovery_paths
        top_dir = HeldDir.walk(path_components(top_path, "Repository worktree"), top_path)
        git_dir = HeldDir.walk(path_components(git_path, "Git directory"), git_path)
        common_dir = HeldDir.walk(path_components(common_path, "Git common directory"), common_path)
        marker = read_marker(top_dir)
        validate_marker_target(top_dir, git_dir, marker)

        def revalidate_repo(stage):
            src_parent.revalidate(stage)
            src_dir.revalidate(stage)
            top_dir.revalidate(stage)
            git_dir.revalidate(stage)
            common_dir.revalidate(stage)
            revalidate_marker(top_dir, marker, stage)
            validate_marker_target(top_dir, git_dir, marker)

        revalidate_repo("repository discovery")
        _GIT_REVALIDATOR = revalidate_repo
        repeated_paths = repository_paths(src_dir.path)
        if repeated_paths != discovery_paths:
            raise BackupError("Repository discovery paths changed after pinning authorities.")
        revalidate_repo("repeated repository discovery")

        dst_path = os.path.join(dst_parent.path, dst_name)
        for label, pinned in (
            ("worktree", top_dir.path),
            ("git directory", git_dir.path),
            ("git common directory", common_dir.path),
        ):
            if is_within(dst_path, pinned):
                raise BackupError(
                    "Destination cannot be inside the repository's " + label + "."
                )

        fmt, oid_len = object_format(src_dir.path)
        revalidate_repo("object-format discovery")
        expected_refs, head_oid, head_ref = source_evidence(src_dir.path, oid_len)
        validate_source_ref_map(src_dir.path, expected_refs, oid_len)
        revalidate_repo("source ref snapshot")

        tmp_dir_name, tmp_dir = make_temp_dir(dst_parent)

        # Everything from here on is covered by the cleanup handler, so no
        # bundle-creation or grammar failure can leak the private temporary
        # directory into the operator's destination directory.
        report = []
        temp_cleanup_done = False
        temp_name_unlinked = False
        linked = False
        receipt_text = None
        try:
            tmp_fd = os.open(
                TEMP_BASENAME,
                os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW,
                0o600,
                dir_fd=tmp_dir.fd,
            )
            try:
                os.fchmod(tmp_fd, 0o600)
                tmp_file = HeldFile(tmp_dir, TEMP_BASENAME, tmp_fd, TEMP_BASENAME)
            except BaseException:
                os.close(tmp_fd)
                raise
            tmp_file.revalidate("precreated descriptor-bound bundle output")
            # Pass the exact validated ref snapshot rather than `--all`.
            # `--all` also serializes Git's per-worktree pseudo-HEAD entries
            # (for example `main-worktree/HEAD`), which are not source refs and
            # make a linked-worktree receipt non-portable.  The explicit list
            # preserves every real `refs/*` record plus HEAD, exactly the map
            # that source_evidence() pins and verifies before and after publish.
            bundle_revisions = [name for name in sorted(expected_refs) if name != "HEAD"]
            created = run_git(
                ["-C", src_dir.path, "bundle", "create", "-", *bundle_revisions, "HEAD"],
                stdout_fd=tmp_file.fd,
                pass_fds=(tmp_file.fd,),
            )
            if created.returncode != 0:
                raise BackupError("Git bundle creation failed: " + git_stderr(created.stderr))
            revalidate_repo("bundle creation")
            tmp_file.revalidate("descriptor-bound bundle creation")
            if not stat.S_ISREG(tmp_file.mode):
                raise BackupError("The temporary bundle is not a regular file.")

            tmp_refs = bundle_ref_map(src_dir.path, tmp_file, oid_len)
            revalidate_repo("temporary bundle verification")
            tmp_file.revalidate("temporary bundle verification")
            if tmp_refs != expected_refs or len(tmp_refs) != len(expected_refs):
                raise BackupError("Bundle list-heads does not exactly match source refs.")

            refs_after, head_oid_after, head_ref_after = source_evidence(src_dir.path, oid_len)
            validate_source_ref_map(src_dir.path, refs_after, oid_len)
            if refs_after != expected_refs:
                raise BackupError("Git refs drifted during the backup.")
            if (head_oid_after, head_ref_after) != (head_oid, head_ref):
                raise BackupError("Git HEAD state drifted during the backup.")
            revalidate_repo("post-creation source revalidation")

            expected_sha = fd_sha256(tmp_file, "temporary artifact checksum")
            os.fsync(tmp_file.fd)

            revalidate_repo("before publication")
            tmp_file.revalidate("before publication")
            dst_parent.revalidate("before publication")
            hook("pre_link")
            revalidate_repo("after the pre-link hook")
            tmp_file.revalidate("after the pre-link hook")
            dst_parent.revalidate("after the pre-link hook")

            # The publication transaction starts here. `linked` is set BEFORE the
            # link so an asynchronous exception delivered between the syscall and
            # the assignment cannot leave an unrolled-back publication; rollback is
            # identity-checked, so running it when no link happened is a no-op.
            linked = True
            os.link(TEMP_BASENAME, dst_name, src_dir_fd=tmp_dir.fd, dst_dir_fd=dst_parent.fd)
            hook("post_link")

            published_fd = os.open(dst_name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dst_parent.fd)
            try:
                published = HeldFile(dst_parent, dst_name, published_fd, dst_raw)
            except BaseException:
                os.close(published_fd)
                raise
            if (published.dev, published.ino) != (tmp_file.dev, tmp_file.ino):
                raise BackupError("Final published object identity mismatch.")
            pub_st = os.fstat(published.fd)
            if not stat.S_ISREG(pub_st.st_mode):
                raise BackupError("Final published object is not a regular file.")
            if (pub_st.st_mode & 0o777) != 0o600:
                raise BackupError("Final published file mode is not 0600: " + oct(pub_st.st_mode))
            if pub_st.st_nlink != 2:
                raise BackupError("Final published link count is not 2: " + str(pub_st.st_nlink))
            if pub_st.st_size != os.fstat(tmp_file.fd).st_size:
                raise BackupError("Final published file size mismatch.")
            if fd_sha256(published, "published pre-unlink checksum") != expected_sha:
                raise BackupError("Final published object checksum mismatch before unlink.")

            os.unlink(TEMP_BASENAME, dir_fd=tmp_dir.fd)
            temp_name_unlinked = True
            hook("post_unlink")

            # Complete revalidation of the published bundle AFTER the temporary
            # link is gone, through the held descriptor and pinned parents.
            final_st = published.revalidate("post-unlink validation")
            if final_st.st_nlink != 1:
                raise BackupError(
                    "Final published link count is not 1 after the temporary unlink: "
                    + str(final_st.st_nlink)
                )
            if (final_st.st_mode & 0o777) != 0o600:
                raise BackupError("Final published file mode is not 0600 after unlink.")
            final_sha = fd_sha256(published, "post-unlink checksum")
            if final_sha != expected_sha:
                raise BackupError("Final published object checksum mismatch after the unlink.")
            revalidate_repo("post-unlink bundle verification")
            final_refs = bundle_ref_map(src_dir.path, published, oid_len)
            published.revalidate("post-unlink bundle verification")
            if final_refs != expected_refs or len(final_refs) != len(expected_refs):
                raise BackupError(
                    "Final published bundle list-heads does not exactly match source refs."
                )
            final_source_refs, final_head_oid, final_head_ref = source_evidence(
                src_dir.path, oid_len
            )
            validate_source_ref_map(src_dir.path, final_source_refs, oid_len)
            if final_source_refs != expected_refs:
                raise BackupError("Git refs drifted after the bundle publication.")
            if (final_head_oid, final_head_ref) != (head_oid, head_ref):
                raise BackupError("Git HEAD state drifted after the bundle publication.")
            revalidate_repo("post-unlink source revalidation")
            dst_parent.revalidate("post-unlink destination revalidation")

            hook("pre_cleanup")
            if not cleanup_temp_dir(dst_parent, tmp_dir_name, tmp_dir, report):
                raise BackupError("Temporary-state cleanup failed: " + "; ".join(report))
            temp_cleanup_done = True
            verify_removed_temp_dir(dst_parent, tmp_dir_name, tmp_dir)

            # The receipt describes only the terminal state after private-temp
            # removal, so all evidence is regenerated through held objects now.
            final_sha = fd_sha256(published, "terminal published checksum")
            if final_sha != expected_sha:
                raise BackupError("Final published object checksum changed after cleanup.")
            final_refs = bundle_ref_map(src_dir.path, published, oid_len)
            if final_refs != expected_refs or len(final_refs) != len(expected_refs):
                raise BackupError("Terminal bundle list-heads does not exactly match source refs.")
            final_source_refs, final_head_oid, final_head_ref = source_evidence(
                src_dir.path, oid_len
            )
            validate_source_ref_map(src_dir.path, final_source_refs, oid_len)
            if final_source_refs != expected_refs or (final_head_oid, final_head_ref) != (
                head_oid,
                head_ref,
            ):
                raise BackupError("Git source evidence changed after cleanup.")
            revalidate_repo("terminal repository revalidation")
            dst_parent.revalidate("terminal destination-parent revalidation")
            final_st = published.revalidate("terminal publication revalidation")
            src_st = os.fstat(src_dir.fd)
            os.fsync(published.fd)
            os.fsync(dst_parent.fd)
            refs_list = [
                {"oid": oid, "refname": name} for name, oid in sorted(expected_refs.items())
            ]
            receipt_text = json.dumps(
                {
                    "source": src_dir.path,
                    "destination": published.path,
                    "object_format": fmt,
                    "head_oid": head_oid,
                    "head_ref": head_ref,
                    "refs": refs_list,
                    "refs_count": len(refs_list),
                    "sha256": final_sha,
                    "identity": identity_of(final_st, published.path),
                    "source_identity": identity_of(src_st, src_dir.path),
                    "repository": {
                        "toplevel": {
                            "path": top_dir.path,
                            "dev": top_dir.dev,
                            "ino": top_dir.ino,
                        },
                        "git_dir": {
                            "path": git_dir.path,
                            "dev": git_dir.dev,
                            "ino": git_dir.ino,
                        },
                        "common_dir": {
                            "path": common_dir.path,
                            "dev": common_dir.dev,
                            "ino": common_dir.ino,
                        },
                        "dot_git": marker,
                    },
                    "evidence_stage": "post_unlink",
                },
                indent=2,
            )
            revalidate_repo("immediately before receipt emission")
            dst_parent.revalidate("immediately before receipt emission")
            published.revalidate("immediately before receipt emission")
            emit_receipt(receipt_text)
        except BaseException as exc:
            rollback = []
            if linked and tmp_file is not None:
                quarantine_expected(dst_parent, dst_name, tmp_file.dev, tmp_file.ino, rollback)
            if not temp_cleanup_done:
                cleanup_temp_dir(
                    dst_parent,
                    tmp_dir_name,
                    tmp_dir,
                    rollback,
                    tmp_file if not temp_name_unlinked else None,
                    not temp_name_unlinked,
                )
            try:
                sys.stderr.write("Error: " + (str(exc) or exc.__class__.__name__) + "\n")
                for line in report + rollback:
                    sys.stderr.write("Rollback: " + line + "\n")
                sys.stderr.flush()
            except BaseException:
                return 1
            return 1
        return 0
    finally:
        _GIT_REVALIDATOR = None
        for held in (
            published,
            tmp_file,
            tmp_dir,
            common_dir,
            git_dir,
            top_dir,
            src_dir,
            src_parent,
            dst_parent,
        ):
            if held is not None:
                held.close()


def main():
    if len(sys.argv) != 3:
        sys.stderr.write(
            "Usage: git-backup.sh <source_git_repo> <destination_bundle_file>\n"
        )
        return 1
    try:
        return run(sys.argv)
    except BackupError as exc:
        sys.stderr.write("Error: " + str(exc) + "\n")
        return 1
    except HookAbort as exc:
        sys.stderr.write("Error: " + str(exc) + "\n")
        return 1
    except OSError as exc:
        sys.stderr.write("Error: " + str(exc) + "\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
PY_EOF
