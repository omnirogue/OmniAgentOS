#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export OMNIAGENTOS_SCRIPT_DIR="$SCRIPT_DIR"

# Prefer the repo interpreter: the live-database guard imports
# omniagentos.contracts and fails closed if it cannot, and a stale system
# python3 would turn that guard into a hard refusal of every run.
PYTHON_BIN="python3"
if [ -x "$SCRIPT_DIR/../../.venv/bin/python3" ]; then
  PYTHON_BIN="$SCRIPT_DIR/../../.venv/bin/python3"
fi

# --- Scheduling helpers (additive, bash-only; the descriptor-pinned Python
# backup below is untouched and still requires exactly two positional args).
#
# --emit-launchd-plist [label]
#   Prints a RENDER-ONLY launchd plist to stdout. Nothing is installed or
#   loaded. Per estate convention (see scripts/gate-watch/*.plist.template),
#   installing is always a separate operator action:
#     db-backup.sh --emit-launchd-plist > /tmp/label.plist
#     cp /tmp/label.plist ~/Library/LaunchAgents/<label>.plist
#     launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/<label>.plist
if [ "${1:-}" = "--emit-launchd-plist" ]; then
  LABEL="${2:-com.omniagentos.db-backup}"
  SOURCE_DB_PLACEHOLDER="/ABSOLUTE/PATH/TO/source.db"
  BACKUP_DIR_PLACEHOLDER="/ABSOLUTE/PATH/TO/backup/dir"
  cat <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<!--
  ${LABEL} — nightly SQLite backup via db-backup.sh --nightly.

  RENDER-ONLY: this is emitted to stdout, never loaded from here. Installing
  is an OPERATOR action (see docs/runbooks/backup-restore.md section 6):
      db-backup.sh --emit-launchd-plist > ~/Library/LaunchAgents/${LABEL}.plist
  Then edit the two REPLACE-WITH placeholders below before bootstrapping.

  EVERY PATH BELOW MUST BE ABSOLUTE. launchd strips PATH and does not source
  a shell profile, so a bare "db-backup.sh" or relative source/dest path
  here silently fails at every scheduled run.

  Failure alerting: StandardErrorPath below is a plain log file. To also push
  a phone alert on failure, wrap ProgramArguments in a small shell command
  that tees stderr and calls the estate's existing ntfy transport (see
  scripts/gate-watch/gate_watch.py's OMNI_NTFY_URL convention) only on a
  non-zero exit; this plist intentionally ships without that wrapper so a
  first install cannot silently start pushing to a channel nobody configured.
-->
<plist version="1.0"><dict>
  <key>Label</key><string>${LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${SCRIPT_DIR}/db-backup.sh</string>
    <string>--nightly</string>
    <string>REPLACE-WITH-SOURCE-DB-${SOURCE_DB_PLACEHOLDER}</string>
    <string>REPLACE-WITH-BACKUP-DIR-${BACKUP_DIR_PLACEHOLDER}</string>
  </array>
  <key>WorkingDirectory</key><string>${SCRIPT_DIR}</string>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key><integer>3</integer>
    <key>Minute</key><integer>0</integer>
  </dict>
  <key>RunAtLoad</key><false/>
  <key>StandardOutPath</key><string>${SCRIPT_DIR}/../../var/log/${LABEL}.log</string>
  <key>StandardErrorPath</key><string>${SCRIPT_DIR}/../../var/log/${LABEL}.log</string>
</dict></plist>
PLIST
  exit 0
fi

# --nightly <source_db> <backup_dir>
#   Convenience wrapper for the launchd job above: computes a timestamped,
#   never-existing destination path inside <backup_dir> and re-execs this
#   same script with the ordinary two-argument form, so the run goes through
#   the exact same descriptor-pinned Python backup as a manual invocation --
#   nothing about the security model below is bypassed or duplicated.
if [ "${1:-}" = "--nightly" ]; then
  if [ "$#" -ne 3 ]; then
    echo "Usage: db-backup.sh --nightly <source_db> <backup_dir>" >&2
    exit 1
  fi
  NIGHTLY_SOURCE_DB="$2"
  NIGHTLY_BACKUP_DIR="$3"
  NIGHTLY_TS="$(date -u +%Y%m%dT%H%M%SZ)"
  NIGHTLY_DEST="$NIGHTLY_BACKUP_DIR/$(basename -- "$NIGHTLY_SOURCE_DB").$NIGHTLY_TS.bak"
  exec "$0" "$NIGHTLY_SOURCE_DB" "$NIGHTLY_DEST"
fi

"$PYTHON_BIN" -u - "$@" << 'PY_EOF'
"""Descriptor-pinned SQLite backup.

Every create, open, stat, link, unlink and rename below is basename-relative
through a directory descriptor that was opened component-by-component with
O_DIRECTORY | O_NOFOLLOW. No pathname is ever canonicalised with realpath and
then trusted: the pinned parents and the held inode identities are the only
authority, and they are revalidated up to and including receipt construction.
"""

import hashlib
import json
import os
import sqlite3
import stat
import sys
import time
import urllib.parse

HOOK_ENV = "GROK_DRILL_HOOK"
HOOK_DIR_ENV = "GROK_DRILL_HOOK_DIR"
HOOK_ACTIONS = ("pause", "fail", "pause_fail")
HOOK_TIMEOUT_SECONDS = 120.0
HOOK_POLL_SECONDS = 0.005
TEMP_DIR_PREFIX = ".grok-tmp-"
QUARANTINE_PREFIX = ".grok-quarantine-"
TEMP_BASENAME = "backup.sqlite"


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


def open_regular(parent, name, raw, label):
    try:
        st = os.stat(name, dir_fd=parent.fd, follow_symlinks=False)
    except FileNotFoundError:
        raise MissingPath(label + " '" + raw + "' does not exist.") from None
    if stat.S_ISLNK(st.st_mode):
        raise BackupError(label + " '" + raw + "' is a symlink.")
    if not stat.S_ISREG(st.st_mode):
        raise BackupError(label + " '" + raw + "' must be a regular file.")
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent.fd)
    try:
        return HeldFile(parent, name, fd, raw)
    except BaseException:
        os.close(fd)
        raise


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


def sqlite_uri(path, mode):
    return "file:" + urllib.parse.quote(path) + "?mode=" + mode


def stable_descriptor_path(held, label):
    """Return a stable descriptor-backed SQLite name or refuse the platform."""
    path = "/dev/fd/" + str(held.fd)
    if not os.path.exists(path):
        raise BackupError("Descriptor-backed validation is unavailable for " + label + ".")
    return path


def copy_database(src_file, tmp_file):
    """SQLite online backup against the validated source pathname.

    The online backup API is used rather than a /dev/fd handle precisely because
    a descriptor pathname loses the companion -wal and -shm names, which would
    silently drop every committed but uncheckpointed WAL frame.
    """
    src_conn = None
    dst_conn = None
    try:
        src_file.revalidate("immediately before source sqlite connect")
        src_conn = sqlite3.connect(sqlite_uri(src_file.path, "ro"), uri=True)
        src_file.revalidate("immediately after source sqlite connect")
        tmp_file.revalidate("immediately before destination sqlite connect")
        dst_conn = sqlite3.connect(sqlite_uri(tmp_file.path, "rwc"), uri=True)
        tmp_file.revalidate("immediately after destination sqlite connect")
        src_file.revalidate("immediately before sqlite backup")
        tmp_file.revalidate("immediately before sqlite backup")
        src_conn.backup(dst_conn)
        src_file.revalidate("immediately after sqlite backup")
        tmp_file.revalidate("immediately after sqlite backup")
        journal_mode = dst_conn.execute("PRAGMA journal_mode=DELETE;").fetchone()[0]
        if str(journal_mode).lower() != "delete":
            raise BackupError("Could not normalise the copy's journal mode: " + str(journal_mode))
    except sqlite3.Error as exc:
        raise BackupError("SQLite online backup failed: " + str(exc)) from exc
    finally:
        try:
            if dst_conn is not None:
                tmp_file.revalidate("immediately before destination sqlite close")
                dst_conn.close()
                tmp_file.revalidate("immediately after destination sqlite close")
        finally:
            if src_conn is not None:
                src_file.revalidate("immediately before source sqlite close")
                src_conn.close()
                src_file.revalidate("immediately after source sqlite close")


def collect_db_evidence(held, stage):
    """Validate only through an already-held normalized SQLite descriptor."""
    held.revalidate(stage + " before descriptor validation")
    path = stable_descriptor_path(held, stage)
    try:
        conn = sqlite3.connect(sqlite_uri(path, "ro"), uri=True)
    except sqlite3.Error as exc:
        raise BackupError("Could not open '" + path + "' for validation: " + str(exc)) from exc
    try:
        cur = conn.cursor()
        cur.execute("PRAGMA integrity_check;")
        integrity = cur.fetchone()[0]
        cur.execute("PRAGMA user_version;")
        user_version = cur.fetchone()[0]
        cur.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name, tbl_name;"
        )
        schema = [list(row) for row in cur.fetchall()]
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;")
        tables = [row[0] for row in cur.fetchall()]
        migration_head = None
        if "schema_migrations" in tables:
            cur.execute("SELECT MAX(version) FROM schema_migrations;")
            migration_head = cur.fetchone()[0]
        row_counts = {}
        for table in tables:
            cur.execute('SELECT COUNT(*) FROM "' + table.replace('"', '""') + '";')
            row_counts[table] = cur.fetchone()[0]
    except sqlite3.Error as exc:
        raise BackupError("Validation of '" + path + "' failed: " + str(exc)) from exc
    finally:
        conn.close()
    held.revalidate(stage + " after descriptor validation")
    return {
        "integrity": integrity,
        "user_version": user_version,
        "migration_head": migration_head,
        "row_counts": row_counts,
        "schema": schema,
    }


def default_db_identity():
    """Identity of the configured default/live database, or None when it has no
    resolvable parent. A symlink anywhere in that path is refused outright, so
    an aliased default cannot be used to smuggle a live destination through."""
    script_dir = os.environ.get("OMNIAGENTOS_SCRIPT_DIR")
    if not script_dir:
        raise BackupError("OMNIAGENTOS_SCRIPT_DIR is not set.")
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(script_dir)))
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    try:
        from omniagentos.contracts import default_db_path
        raw = str(default_db_path())
    except Exception as exc:
        raise BackupError("Could not resolve default_db_path: " + str(exc)) from exc
    parts = path_components(raw, "Default database")
    try:
        parent_fd = open_dir_chain(parts[:-1], raw)
    except MissingPath as exc:
        raise BackupError(
            "Could not resolve the default database parent; refusing the destination guard."
        ) from exc
    try:
        parent_st = os.fstat(parent_fd)
        try:
            leaf = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            leaf = None
        if leaf is not None and stat.S_ISLNK(leaf.st_mode):
            raise BackupError("The default database path '" + raw + "' is a symlink.")
    finally:
        os.close(parent_fd)
    return {
        "path": join_components(parts),
        "parent": (parent_st.st_dev, parent_st.st_ino),
        "name": parts[-1],
    }


def run(argv):
    src_raw = argv[1]
    dst_raw = argv[2]
    src_parts = path_components(src_raw, "Source")
    dst_parts = path_components(dst_raw, "Destination")
    src_name = src_parts[-1]
    dst_name = dst_parts[-1]

    src_parent = None
    dst_parent = None
    src_file = None
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

        src_file = open_regular(src_parent, src_name, src_raw, "Source")

        try:
            dst_entry = os.stat(dst_name, dir_fd=dst_parent.fd, follow_symlinks=False)
        except FileNotFoundError:
            dst_entry = None
        if dst_entry is not None:
            if stat.S_ISLNK(dst_entry.st_mode):
                raise BackupError("Destination '" + dst_raw + "' is a symlink.")
            if (dst_entry.st_dev, dst_entry.st_ino) == (src_file.dev, src_file.ino):
                raise BackupError(
                    "Source and destination are the same physical file (hardlink/inode match)."
                )
            raise BackupError("Destination path '" + dst_raw + "' already exists.")

        default = default_db_identity()
        if default is not None and default["name"] == dst_name and default["parent"] == (
            dst_parent.dev,
            dst_parent.ino,
        ):
            raise BackupError(
                "Cannot write to default/live database destination '" + dst_raw + "'."
            )

        tmp_dir_name, tmp_dir = make_temp_dir(dst_parent)

        # Everything from here on is covered by the cleanup handler, so no
        # failure between staging and publication can leak the private
        # temporary directory into the operator's destination directory.
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
            if not stat.S_ISREG(tmp_file.mode):
                raise BackupError("The temporary destination is not a regular file.")

            src_file.revalidate("before the source connection")
            tmp_file.revalidate("before the destination connection")
            copy_database(src_file, tmp_file)
            src_file.revalidate("immediately after the online backup")
            tmp_file.revalidate("immediately after the online backup")

            evidence = collect_db_evidence(tmp_file, "temporary database evidence")
            if evidence["integrity"] != "ok":
                raise BackupError(
                    "Integrity check failed on the copy: " + str(evidence["integrity"])
                )
            expected_sha = fd_sha256(tmp_file, "temporary artifact checksum")
            os.fsync(tmp_file.fd)

            src_file.revalidate("before publication")
            tmp_file.revalidate("before publication")
            dst_parent.revalidate("before publication")
            hook("pre_link")
            src_file.revalidate("after the pre-link hook")
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
            if collect_db_evidence(published, "published pre-unlink database evidence") != evidence:
                raise BackupError("Final published database evidence mismatch before unlink.")

            os.unlink(TEMP_BASENAME, dir_fd=tmp_dir.fd)
            temp_name_unlinked = True
            hook("post_unlink")

            # Complete revalidation of the published object AFTER the temporary
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
            final_evidence = collect_db_evidence(published, "published post-unlink database evidence")
            if final_evidence != evidence:
                raise BackupError("Final published database evidence mismatch after the unlink.")
            if final_evidence["integrity"] != "ok":
                raise BackupError("Final published database failed its integrity check.")
            src_file.revalidate("post-unlink source revalidation")
            src_parent.revalidate("post-unlink source revalidation")
            dst_parent.revalidate("post-unlink destination revalidation")

            hook("pre_cleanup")
            if not cleanup_temp_dir(dst_parent, tmp_dir_name, tmp_dir, report):
                raise BackupError("Temporary-state cleanup failed: " + "; ".join(report))
            temp_cleanup_done = True
            verify_removed_temp_dir(dst_parent, tmp_dir_name, tmp_dir)

            # Cleanup changes the represented terminal state. Recompute every
            # published evidence value only after it succeeds, then fsync it.
            final_evidence = collect_db_evidence(published, "terminal database evidence")
            if final_evidence != evidence or final_evidence["integrity"] != "ok":
                raise BackupError("Final published database evidence changed after cleanup.")
            final_sha = fd_sha256(published, "terminal published checksum")
            if final_sha != expected_sha:
                raise BackupError("Final published object checksum changed after cleanup.")
            src_st = src_file.revalidate("terminal source revalidation")
            src_parent.revalidate("terminal source-parent revalidation")
            dst_parent.revalidate("terminal destination-parent revalidation")
            final_st = published.revalidate("terminal publication revalidation")
            os.fsync(published.fd)
            os.fsync(dst_parent.fd)

            receipt_text = json.dumps(
                {
                    "source": src_file.path,
                    "destination": published.path,
                    "integrity": final_evidence["integrity"],
                    "user_version": final_evidence["user_version"],
                    "migration_head": final_evidence["migration_head"],
                    "row_counts": final_evidence["row_counts"],
                    "schema": final_evidence["schema"],
                    "sha256": final_sha,
                    "identity": identity_of(final_st, published.path),
                    "source_identity": identity_of(src_st, src_file.path),
                    "evidence_stage": "post_unlink",
                },
                indent=2,
            )
            src_parent.revalidate("immediately before receipt emission")
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
        for held in (published, tmp_file, src_file, tmp_dir, src_parent, dst_parent):
            if held is not None:
                held.close()


def main():
    if len(sys.argv) != 3:
        sys.stderr.write("Usage: db-backup.sh <source_db> <destination_db>\n")
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
