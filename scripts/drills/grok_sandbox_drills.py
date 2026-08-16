"""Sandboxed reliability drills for OmniAgentOS.

S1-S3 are implemented and run entirely inside throwaway temporary directories.
S4-S12 are operator-only skeletons and are never executed from here: they would
need PID, network, provider or disk actions that require the operator's explicit
approval.

Nothing in this module contacts a network, a provider, a live service, a live
database or launchd state. Every temporary root is resolved with ``realpath``
before use because the production scripts refuse a symlink at any path
component, and the platform temporary directory is itself reached through one.
"""

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

BACKUP_SCRIPT = os.path.join(REPO_ROOT, "scripts", "backup", "db-backup.sh")
RESTORE_SCRIPT = os.path.join(REPO_ROOT, "scripts", "backup", "grok-db-restore.sh")
GIT_BACKUP_SCRIPT = os.path.join(REPO_ROOT, "scripts", "backup", "git-backup.sh")

DB_RECEIPT_KEYS = {
    "source",
    "destination",
    "integrity",
    "user_version",
    "migration_head",
    "row_counts",
    "schema",
    "sha256",
    "identity",
    "source_identity",
    "evidence_stage",
}

GIT_RECEIPT_KEYS = {
    "source",
    "destination",
    "object_format",
    "head_oid",
    "head_ref",
    "refs",
    "refs_count",
    "sha256",
    "identity",
    "source_identity",
    "repository",
    "evidence_stage",
}

RENDEZVOUS_TIMEOUT_SECONDS = 90.0
RENDEZVOUS_POLL_SECONDS = 0.005

FAKE_GIT_SOURCE = '''#!/usr/bin/env python3
import json
import os
import sys

with open(os.environ["GROK_FAKE_GIT_CONFIG"]) as handle:
    config = json.load(handle)
if config["match"] in " ".join(sys.argv[1:]):
    with open(config["fired"], "w") as marker:
        marker.write(config["match"])
    sys.stdout.write(config["stdout"])
    sys.stderr.write(config["stderr"])
    sys.stdout.flush()
    sys.stderr.flush()
    sys.exit(config["exit_code"])
os.execv(config["real"], [config["real"], *sys.argv[1:]])
'''


class DrillError(RuntimeError):
    """A drill oracle was not satisfied."""


def reject_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate key: " + key)
        result[key] = value
    return result


def load_receipt(text, label):
    if isinstance(text, bytes):
        try:
            text = text.decode("utf-8", "strict")
        except UnicodeDecodeError as exc:
            raise DrillError(label + ": output is not strict UTF-8.") from exc
    try:
        return json.loads(text, object_pairs_hook=reject_duplicates)
    except json.JSONDecodeError as exc:
        raise DrillError(label + ": output is not valid JSON: " + repr(text)) from exc
    except ValueError as exc:
        raise DrillError(label + ": output has duplicate keys: " + str(exc)) from exc


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(65536)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def run_script(script, args, env=None):
    return subprocess.run(
        ["bash", script, *args],
        capture_output=True,
        env=env,
        check=False,
    )


def run_ok(script, args, label, env=None):
    result = run_script(script, args, env=env)
    if result.returncode != 0:
        raise DrillError(
            label + " failed: rc=" + str(result.returncode) + " "
            + result.stderr.decode("utf-8", "backslashreplace")
        )
    return result


def expect_refusal(result, needle, label):
    if result.returncode == 0:
        raise DrillError(label + ": expected a refusal, got success: " + repr(result.stdout))
    expected = needle.encode("utf-8") if isinstance(needle, str) else needle
    if expected not in result.stderr:
        raise DrillError(label + ": expected " + repr(expected) + ", got: " + repr(result.stderr))
    if result.stdout != b"":
        raise DrillError(label + ": a refusal still wrote stdout: " + repr(result.stdout))


def expect_exact_refusal(result, expected_stderr, label):
    """Require a complete, stable bytes diagnostic and byte-empty stdout."""
    if not isinstance(expected_stderr, bytes):
        raise TypeError("expect_exact_refusal requires bytes for expected_stderr")
    if result.returncode == 0:
        raise DrillError(label + ": expected a refusal, got success: " + repr(result.stdout))
    if result.stderr != expected_stderr:
        raise DrillError(
            label + ": exact stderr mismatch: expected " + repr(expected_stderr) + ", got "
            + repr(result.stderr)
        )
    if result.stdout != b"":
        raise DrillError(label + ": a refusal still wrote stdout: " + repr(result.stdout))


def assert_no_residue(directory, destination, label):
    if os.path.lexists(destination):
        raise DrillError(label + ": the destination survived a failed run.")
    residue = sorted(name for name in os.listdir(directory) if name.startswith(".grok-"))
    if residue:
        raise DrillError(label + ": temporary or quarantine residue remains: " + str(residue))


def touch(path):
    with open(path, "w") as handle:
        handle.write("go")


def new_sandbox(prefix):
    """A temporary directory whose path has no symlink component."""
    return os.path.realpath(tempfile.mkdtemp(prefix=prefix))


def run_with_hook(script, args, phase, action, injection=None, env=None):
    """Run a production script with the deterministic test hook armed.

    The hook can only pause or fail. The rendezvous marker files it leaves
    behind are what prove the injection point was actually reached, so a run
    that failed earlier for an unrelated reason cannot masquerade as a passing
    race or rollback oracle.
    """
    hook_dir = new_sandbox("grok-hook-")
    child_env = dict(os.environ if env is None else env)
    child_env["GROK_DRILL_HOOK"] = phase + ":" + action
    child_env["GROK_DRILL_HOOK_DIR"] = hook_dir
    ready = os.path.join(hook_dir, phase + ".ready")
    fired = os.path.join(hook_dir, phase + ".fired")
    try:
        process = subprocess.Popen(
            ["bash", script, *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=child_env,
        )
        if action in ("pause", "pause_fail"):
            deadline = time.monotonic() + RENDEZVOUS_TIMEOUT_SECONDS
            while not os.path.exists(ready):
                if process.poll() is not None:
                    stdout, stderr = process.communicate()
                    raise DrillError(
                        "hook " + phase + ": the run ended before the rendezvous: "
                        + stderr.decode("utf-8", "backslashreplace")
                    )
                if time.monotonic() > deadline:
                    process.kill()
                    process.communicate()
                    raise DrillError("hook " + phase + ": the rendezvous never happened.")
                time.sleep(RENDEZVOUS_POLL_SECONDS)
            if injection is not None:
                injection()
            touch(os.path.join(hook_dir, phase + ".go"))
        stdout, stderr = process.communicate(timeout=RENDEZVOUS_TIMEOUT_SECONDS)
        result = subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)
        if action in ("pause", "pause_fail") and not os.path.exists(ready):
            raise DrillError("hook " + phase + ": the pause never fired.")
        if action in ("fail", "pause_fail") and not os.path.exists(fired):
            raise DrillError("hook " + phase + ": the injected abort never fired.")
        return result
    finally:
        shutil.rmtree(hook_dir, ignore_errors=True)


def build_sample_db(path):
    """Create a deterministic source database and return its expected evidence."""
    conn = sqlite3.connect(path)
    conn.isolation_level = None
    cur = conn.cursor()
    cur.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT);")
    cur.execute(
        "INSERT INTO schema_migrations (version, applied_at) VALUES (104, '2026-08-02T12:00:00Z');"
    )
    cur.execute("CREATE TABLE test_data (id INTEGER PRIMARY KEY, value TEXT);")
    cur.execute("CREATE INDEX idx_test_data_value ON test_data(value);")
    cur.execute("CREATE VIEW v_test AS SELECT * FROM test_data;")
    cur.execute(
        "CREATE TRIGGER trg_test AFTER INSERT ON test_data "
        "BEGIN UPDATE schema_migrations SET applied_at = 'now'; END;"
    )
    cur.execute("INSERT INTO test_data (id, value) VALUES (1, 'alice'), (2, 'bob'), (3, 'carol');")
    cur.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name, tbl_name;"
    )
    schema = [list(row) for row in cur.fetchall()]
    conn.close()
    return schema, {"schema_migrations": 1, "test_data": 3}


def read_db_evidence(path, expected_schema, expected_counts, label):
    conn = sqlite3.connect("file:" + path + "?mode=ro", uri=True)
    try:
        cur = conn.cursor()
        cur.execute("PRAGMA integrity_check;")
        if cur.fetchone()[0] != "ok":
            raise DrillError(label + ": integrity check failed.")
        cur.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name, tbl_name;"
        )
        if [list(row) for row in cur.fetchall()] != expected_schema:
            raise DrillError(label + ": schema mismatch.")
        for table, count in expected_counts.items():
            cur.execute('SELECT COUNT(*) FROM "' + table + '";')
            if cur.fetchone()[0] != count:
                raise DrillError(label + ": row-count mismatch on " + table + ".")
    finally:
        conn.close()


def check_identity(receipt_identity, path, label):
    st = os.lstat(path)
    if receipt_identity["dev"] != st.st_dev or receipt_identity["ino"] != st.st_ino:
        raise DrillError(label + ": receipt identity does not match the object.")
    if receipt_identity["mode"] != st.st_mode:
        raise DrillError(label + ": receipt mode does not match the object.")
    if receipt_identity["nlink"] != st.st_nlink:
        raise DrillError(label + ": receipt link count does not match the object.")
    if receipt_identity["size"] != st.st_size:
        raise DrillError(label + ": receipt size does not match the object.")
    if receipt_identity["path"] != os.path.realpath(path):
        raise DrillError(label + ": receipt path is not the pinned path.")


def drill_s1_database(sandbox):
    """The S1 database happy path plus its refusal surface."""
    source = os.path.join(sandbox, "source.db")
    backup = os.path.join(sandbox, "backup.db")
    restored = os.path.join(sandbox, "restored.db")
    schema, counts = build_sample_db(source)
    source_sha_before = sha256_of(source)
    source_st_before = os.lstat(source)

    receipt = load_receipt(
        run_ok(BACKUP_SCRIPT, [source, backup], "S1 backup").stdout, "S1 backup"
    )
    if set(receipt) != DB_RECEIPT_KEYS:
        raise DrillError("S1 backup receipt keys mismatch: " + str(sorted(receipt)))
    if receipt["evidence_stage"] != "post_unlink":
        raise DrillError("S1 backup receipt does not describe the post-unlink state.")
    if receipt["migration_head"] != 104:
        raise DrillError("S1 backup migration head mismatch: " + str(receipt["migration_head"]))
    if receipt["user_version"] != 0:
        raise DrillError("S1 backup user_version mismatch.")
    if receipt["integrity"] != "ok":
        raise DrillError("S1 backup integrity mismatch.")
    if receipt["row_counts"] != counts:
        raise DrillError("S1 backup row counts mismatch: " + str(receipt["row_counts"]))
    if receipt["schema"] != schema:
        raise DrillError("S1 backup schema mismatch.")
    if receipt["sha256"] != sha256_of(backup):
        raise DrillError("S1 backup checksum does not match the published file.")
    check_identity(receipt["identity"], backup, "S1 backup")
    check_identity(receipt["source_identity"], source, "S1 backup source")
    if (os.lstat(backup).st_mode & 0o777) != 0o600:
        raise DrillError("S1 backup file mode is not 0600.")
    if os.lstat(backup).st_nlink != 1:
        raise DrillError("S1 backup file link count is not 1.")
    read_db_evidence(backup, schema, counts, "S1 backup file")

    expect_refusal(
        run_script(BACKUP_SCRIPT, [source, backup]), "already exists", "S1 no-clobber"
    )

    hardlink = os.path.join(sandbox, "hardlink.db")
    os.link(source, hardlink)
    expect_refusal(
        run_script(BACKUP_SCRIPT, [source, hardlink]), "hardlink/inode match", "S1 hardlink"
    )
    expect_refusal(
        run_script(BACKUP_SCRIPT, [source, source]), "cannot be the same", "S1 source=destination"
    )
    expect_refusal(
        run_script(RESTORE_SCRIPT, [backup, backup]),
        "cannot be the same",
        "S1 restore source=destination",
    )

    live_env = dict(os.environ)
    live_env["OMNIAGENTOS_DB"] = os.path.join(sandbox, "live.db")
    expect_refusal(
        run_script(BACKUP_SCRIPT, [source, os.path.join(sandbox, "live.db")], env=live_env),
        "Cannot write to default/live database",
        "S1 default-db backup",
    )
    expect_refusal(
        run_script(RESTORE_SCRIPT, [backup, os.path.join(sandbox, "live.db")], env=live_env),
        "Cannot overwrite default/live database",
        "S1 default-db restore",
    )

    restore_receipt = load_receipt(
        run_ok(RESTORE_SCRIPT, [backup, restored], "S1 restore").stdout, "S1 restore"
    )
    if set(restore_receipt) != DB_RECEIPT_KEYS:
        raise DrillError("S1 restore receipt keys mismatch: " + str(sorted(restore_receipt)))
    if restore_receipt["evidence_stage"] != "post_unlink":
        raise DrillError("S1 restore receipt does not describe the post-unlink state.")
    if restore_receipt["schema"] != schema:
        raise DrillError("S1 restore schema mismatch.")
    if restore_receipt["row_counts"] != counts:
        raise DrillError("S1 restore row counts mismatch.")
    if restore_receipt["sha256"] != sha256_of(restored):
        raise DrillError("S1 restore checksum does not match the published file.")
    check_identity(restore_receipt["identity"], restored, "S1 restore")
    check_identity(restore_receipt["source_identity"], backup, "S1 restore source")
    read_db_evidence(restored, schema, counts, "S1 restored file")

    if sha256_of(source) != source_sha_before:
        raise DrillError("S1: the source database bytes changed during the drill.")
    after = os.lstat(source)
    if (after.st_dev, after.st_ino) != (source_st_before.st_dev, source_st_before.st_ino):
        raise DrillError("S1: the source database inode changed during the drill.")

    plain = os.path.join(sandbox, "plain.db")
    conn = sqlite3.connect(plain)
    conn.execute("CREATE TABLE foo (a INT);")
    conn.commit()
    conn.close()
    plain_receipt = load_receipt(
        run_ok(BACKUP_SCRIPT, [plain, os.path.join(sandbox, "plain-backup.db")],
               "S1 no-migrations backup").stdout,
        "S1 no-migrations backup",
    )
    if plain_receipt["migration_head"] is not None:
        raise DrillError("S1: expected a null migration head without schema_migrations.")
    return backup


def oracle_a_wal(sandbox):
    """A: a committed, uncheckpointed WAL row survives backup and restore."""
    source = os.path.join(sandbox, "wal-source.db")
    writer = sqlite3.connect(source)
    writer.isolation_level = None
    try:
        if writer.execute("PRAGMA journal_mode=WAL;").fetchone()[0].lower() != "wal":
            raise DrillError("A: the source database did not enter WAL mode.")
        writer.execute("PRAGMA wal_autocheckpoint=0;")
        writer.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY);")
        writer.execute("INSERT INTO schema_migrations (version) VALUES (104);")
        writer.execute("CREATE TABLE wal_rows (id INTEGER PRIMARY KEY, value TEXT);")
        writer.execute("INSERT INTO wal_rows (id, value) VALUES (1, 'uncheckpointed');")
        if not os.path.exists(source + "-wal") or os.path.getsize(source + "-wal") == 0:
            raise DrillError("A: no uncheckpointed WAL frames were produced.")
        main_size = os.path.getsize(source)

        backup = os.path.join(sandbox, "wal-backup.db")
        receipt = load_receipt(
            run_ok(BACKUP_SCRIPT, [source, backup], "A backup").stdout, "A backup"
        )
        if receipt["row_counts"].get("wal_rows") != 1:
            raise DrillError("A: the uncheckpointed row is missing from the backup receipt.")
        read_db_evidence(backup, receipt["schema"], {"wal_rows": 1}, "A backup")

        restored = os.path.join(sandbox, "wal-restored.db")
        run_ok(RESTORE_SCRIPT, [backup, restored], "A restore")
        conn = sqlite3.connect("file:" + restored + "?mode=ro", uri=True)
        try:
            rows = conn.execute("SELECT id, value FROM wal_rows ORDER BY id;").fetchall()
        finally:
            conn.close()
        if rows != [(1, "uncheckpointed")]:
            raise DrillError("A: the restored database does not contain the WAL row: " + str(rows))
        if os.path.getsize(source) != main_size:
            raise DrillError("A: the source main database file was checkpointed by the backup.")
    finally:
        writer.close()
    print("  A: WAL continuity with an open writer and wal_autocheckpoint=0 verified.")


def oracle_b_symlinks(sandbox, backup):
    """B: ancestor, leaf and default-path symlink refusals."""
    real_dir = os.path.join(sandbox, "b-real")
    os.mkdir(real_dir)
    linked_dir = os.path.join(sandbox, "b-link")
    os.symlink(real_dir, linked_dir)
    source = os.path.join(real_dir, "src.db")
    build_sample_db(source)

    expect_refusal(
        run_script(BACKUP_SCRIPT, [os.path.join(linked_dir, "src.db"),
                                   os.path.join(sandbox, "b1.db")]),
        "is a symlink",
        "B source-ancestor symlink",
    )
    expect_refusal(
        run_script(BACKUP_SCRIPT, [source, os.path.join(linked_dir, "b2.db")]),
        "is a symlink",
        "B destination-ancestor symlink",
    )

    leaf = os.path.join(sandbox, "b-leaf-source.db")
    os.symlink(backup, leaf)
    expect_refusal(
        run_script(RESTORE_SCRIPT, [leaf, os.path.join(sandbox, "b3.db")]),
        "is a symlink",
        "B restore-source leaf symlink",
    )

    repo = os.path.join(sandbox, "b-repo")
    build_git_repo(repo)
    dangling = os.path.join(sandbox, "b-dangling.bundle")
    os.symlink(os.path.join(sandbox, "no-such-target"), dangling)
    expect_refusal(
        run_script(GIT_BACKUP_SCRIPT, [repo, dangling]),
        "is a symlink",
        "B git destination leaf symlink",
    )

    alias_env = dict(os.environ)
    alias_env["OMNIAGENTOS_DB"] = os.path.join(linked_dir, "live.db")
    expect_refusal(
        run_script(BACKUP_SCRIPT, [source, os.path.join(sandbox, "b4.db")], env=alias_env),
        "is a symlink",
        "B default-path alias",
    )
    print("  B: ancestor, leaf and default-path symlink refusals verified.")


def swap_directory(path):
    """Deterministically exchange a directory for a fresh empty one."""
    moved = path + ".moved"
    os.rename(path, moved)
    os.mkdir(path)
    return moved


def oracle_c_parent_exchange(sandbox):
    """C: both parents at all phases for backup, restore and Git publication."""
    subjects = (
        ("backup", BACKUP_SCRIPT, "source.db", "out.db"),
        ("restore", RESTORE_SCRIPT, "source.backup", "out.db"),
        ("git", GIT_BACKUP_SCRIPT, "repo", "out.bundle"),
    )
    phases = ("pre_open", "pre_link", "post_unlink")
    for subject, script, source_name, destination_name in subjects:
        for phase in phases:
            for target in ("source", "destination"):
                case = os.path.join(sandbox, "c-" + subject + "-" + phase + "-" + target)
                os.mkdir(case)
                src_dir = os.path.join(case, "src")
                dst_dir = os.path.join(case, "dst")
                os.mkdir(src_dir)
                os.mkdir(dst_dir)
                source = os.path.join(src_dir, source_name)
                if subject == "git":
                    build_git_repo(source)
                else:
                    build_sample_db(source)
                destination = os.path.join(dst_dir, destination_name)
                victim = src_dir if target == "source" else dst_dir
                moved = {}

                def injection(victim=victim, moved=moved):
                    moved["path"] = swap_directory(victim)

                result = run_with_hook(script, [source, destination], phase, "pause", injection)
                label = "C " + subject + " " + phase + " " + target + "-parent exchange"
                expect_refusal(result, "no longer resolves to the held directory", label)
                if os.listdir(victim):
                    raise DrillError(label + ": the substituted parent received output.")
                pinned_parent = moved["path"] if target == "destination" else dst_dir
                if target == "destination" and phase in ("pre_link", "post_unlink"):
                    entries = set(os.listdir(pinned_parent))
                    preserved = sorted(
                        entry for entry in entries
                        if entry.startswith(".grok-tmp-")
                    )
                    if len(preserved) != 1:
                        raise DrillError(label + ": expected one preserved pinned temp directory.")
                    quarantines = sorted(
                        entry for entry in entries
                        if entry.startswith(".grok-quarantine-")
                    )
                    expected_quarantines = 1 if phase == "post_unlink" else 0
                    if len(quarantines) != expected_quarantines:
                        raise DrillError(label + ": preserved quarantine residue mismatch.")
                    allowed = set(preserved + quarantines)
                    if entries != allowed:
                        raise DrillError(label + ": unexpected pinned-parent residue set.")
                    temp_path = os.path.join(pinned_parent, preserved[0])
                    expected_child = {
                        "backup": "backup.sqlite",
                        "restore": "restore.sqlite",
                        "git": "backup.bundle",
                    }[subject]
                    children = set(os.listdir(temp_path))
                    expected_children = {expected_child} if phase == "pre_link" else set()
                    if children != expected_children:
                        raise DrillError(label + ": preserved temp contents mismatch.")
                    if quarantines:
                        quarantine_st = os.lstat(os.path.join(pinned_parent, quarantines[0]))
                        if not stat.S_ISREG(quarantine_st.st_mode) or quarantine_st.st_nlink != 1:
                            raise DrillError(
                                label + ": quarantined publication is not a single-link regular file."
                            )
                else:
                    assert_no_residue(
                        pinned_parent,
                        os.path.join(pinned_parent, destination_name),
                        label + " (pinned parent)",
                    )
                if target == "destination" and os.path.lexists(destination):
                    raise DrillError(label + ": output appeared outside the pinned parent.")
    print("  C: both parents at all three phases are refused for backup, restore and Git.")


def oracle_d_async_abort(sandbox):
    """D: post-link BaseException rollback is shared by all publication paths."""
    for subject, script, source_name, destination_name in (
        ("backup", BACKUP_SCRIPT, "source.db", "out.db"),
        ("restore", RESTORE_SCRIPT, "source.db", "out.db"),
        ("git", GIT_BACKUP_SCRIPT, "repo", "out.bundle"),
    ):
        case = os.path.join(sandbox, "d-" + subject)
        os.mkdir(case)
        source = os.path.join(case, source_name)
        if subject == "git":
            build_git_repo(source)
        else:
            build_sample_db(source)
        destination = os.path.join(case, destination_name)
        result = run_with_hook(script, [source, destination], "post_link", "fail")
        label = "D " + subject + " post-link abort"
        expect_refusal(result, "Injected asynchronous abort", label)
        assert_no_residue(case, destination, label)
    print("  D: post-link BaseException rolls back backup, restore and Git publication.")


def oracle_e_foreign_replacement(sandbox):
    """E: a foreign object that replaces the destination is preserved, never removed."""
    for subject, script, source_name, destination_name in (
        ("backup", BACKUP_SCRIPT, "source.db", "out.db"),
        ("restore", RESTORE_SCRIPT, "source.db", "out.db"),
        ("git", GIT_BACKUP_SCRIPT, "repo", "out.bundle"),
    ):
        for kind in ("regular", "symlink", "directory"):
            _oracle_e_case(sandbox, subject, script, source_name, destination_name, kind)
    print("  E: foreign replacements are preserved across backup, restore and Git.")


def _oracle_e_case(sandbox, subject, script, source_name, destination_name, kind):
    case = os.path.join(sandbox, "e-" + subject + "-" + kind)
    os.mkdir(case)
    source = os.path.join(case, source_name)
    destination = os.path.join(case, destination_name)
    if subject == "git":
        build_git_repo(source)
    else:
        build_sample_db(source)
    state = {}

    def injection():
        os.unlink(destination)
        if kind == "regular":
            with open(destination, "wb") as handle:
                handle.write(b"foreign-bytes")
        elif kind == "symlink":
            os.symlink("/nonexistent-foreign-target", destination)
        else:
            os.mkdir(destination)
            with open(os.path.join(destination, "inside.txt"), "w") as handle:
                handle.write("foreign-tree")
        state["stat"] = os.lstat(destination)

    result = run_with_hook(script, [source, destination], "post_link", "pause_fail", injection)
    label = "E " + subject + " foreign " + kind
    expect_refusal(result, "Injected asynchronous abort", label)
    quarantine = sorted(name for name in os.listdir(case) if name.startswith(".grok-quarantine-"))
    if len(quarantine) != 1:
        raise DrillError(label + ": expected one reported preserved quarantine entry.")
    preserved_path = os.path.join(case, quarantine[0])
    if not os.path.lexists(preserved_path):
        raise DrillError(label + ": the foreign object was removed.")
    after = os.lstat(preserved_path)
    before = state["stat"]
    if (after.st_dev, after.st_ino, after.st_mode) != (before.st_dev, before.st_ino, before.st_mode):
        raise DrillError(label + ": the foreign object was not preserved byte-for-byte.")
    if kind == "regular":
        with open(preserved_path, "rb") as handle:
            if handle.read() != b"foreign-bytes":
                raise DrillError(label + ": the foreign file contents changed.")
    elif kind == "symlink":
        if os.readlink(preserved_path) != "/nonexistent-foreign-target":
            raise DrillError(label + ": the foreign symlink target changed.")
    else:
        with open(os.path.join(preserved_path, "inside.txt")) as handle:
            if handle.read() != "foreign-tree":
                raise DrillError(label + ": the foreign tree contents changed.")
    residue = sorted(name for name in os.listdir(case) if name.startswith(".grok-"))
    allowed = quarantine
    if residue != allowed:
        raise DrillError(label + ": temporary or quarantine residue remains: " + str(residue))


def oracle_f_post_unlink_mutation(sandbox):
    """F: a same-size mutation after the temporary unlink is rejected and rolled back."""
    for subject, script, source_name, destination_name in (
        ("backup", BACKUP_SCRIPT, "source.db", "out.db"),
        ("restore", RESTORE_SCRIPT, "source.db", "out.db"),
        ("git", GIT_BACKUP_SCRIPT, "repo", "out.bundle"),
    ):
        case = os.path.join(sandbox, "f-" + subject)
        os.mkdir(case)
        source = os.path.join(case, source_name)
        destination = os.path.join(case, destination_name)
        if subject == "git":
            build_git_repo(source)
        else:
            build_sample_db(source)
        state = {}

        def injection(destination=destination, state=state):
            state["size"] = os.path.getsize(destination)
            with open(destination, "r+b") as handle:
                handle.seek(state["size"] - 1)
                original = handle.read(1)
                handle.seek(state["size"] - 1)
                handle.write(bytes([original[0] ^ 0xFF]))
            if os.path.getsize(destination) != state["size"]:
                raise DrillError("F: the injected mutation changed the file size.")

        result = run_with_hook(script, [source, destination], "post_unlink", "pause", injection)
        label = "F " + subject + " post-unlink mutation"
        expect_refusal(result, "checksum mismatch after the unlink", label)
        assert_no_residue(case, destination, label)
    print("  F: same-size mutations are rejected across backup, restore and Git.")


def oracle_g_temp_replacement(sandbox):
    """G: replaced temps are preserved; pinned creation failures clean up safely."""
    for subject, script, source_name, destination_name in (
        ("backup", BACKUP_SCRIPT, "source.db", "out.db"),
        ("restore", RESTORE_SCRIPT, "source.db", "out.db"),
        ("git", GIT_BACKUP_SCRIPT, "repo", "out.bundle"),
    ):
        case = os.path.join(sandbox, "g-" + subject)
        os.mkdir(case)
        source = os.path.join(case, source_name)
        destination = os.path.join(case, destination_name)
        if subject == "git":
            build_git_repo(source)
        else:
            build_sample_db(source)
        state = {}

        def injection(case=case, state=state):
            names = [name for name in os.listdir(case) if name.startswith(".grok-tmp-")]
            if len(names) != 1:
                raise DrillError("G: expected exactly one temporary directory, found " + str(names))
            temp = os.path.join(case, names[0])
            os.rename(temp, temp + ".moved")
            os.mkdir(temp)
            with open(os.path.join(temp, "foreign.txt"), "w") as handle:
                handle.write("foreign-temp-tree")
            state["temp"] = temp
            state["moved"] = temp + ".moved"
            state["temp_stat"] = os.lstat(temp)
            state["moved_stat"] = os.lstat(temp + ".moved")

        result = run_with_hook(script, [source, destination], "pre_cleanup", "pause", injection)
        label = "G " + subject + " temp replacement"
        expect_refusal(result, "Temporary-state cleanup failed", label)
        if b"was replaced" not in result.stderr:
            raise DrillError(label + ": the replacement was not reported: " + repr(result.stderr))
        with open(os.path.join(state["temp"], "foreign.txt")) as handle:
            if handle.read() != "foreign-temp-tree":
                raise DrillError(label + ": the foreign temporary tree was not preserved.")
        if not os.path.isdir(state["moved"]):
            raise DrillError(label + ": the original temporary directory was lost.")
        for key in ("temp", "moved"):
            before = state[key + "_stat"]
            after = os.lstat(state[key])
            if (after.st_dev, after.st_ino, after.st_mode) != (
                before.st_dev,
                before.st_ino,
                before.st_mode,
            ):
                raise DrillError(label + ": preserved " + key + " tree identity changed.")
        if os.listdir(state["moved"]):
            raise DrillError(label + ": original temp tree has unexpected contents.")
        if os.path.lexists(destination):
            raise DrillError(label + ": the publication was not rolled back after cleanup failure.")
        allowed = {os.path.basename(state["temp"]), os.path.basename(state["moved"])}
        observed = set(os.listdir(case)) - {source_name}
        if observed != allowed:
            raise DrillError(label + ": unexpected residue set: " + repr(sorted(observed)))

    for subject, script, source_name, destination_name in (
        ("backup", BACKUP_SCRIPT, "source.db", "out.db"),
        ("restore", RESTORE_SCRIPT, "source.db", "out.db"),
        ("git", GIT_BACKUP_SCRIPT, "repo", "out.bundle"),
    ):
        case = os.path.join(sandbox, "g-pinned-creation-" + subject)
        os.mkdir(case)
        source = os.path.join(case, source_name)
        destination = os.path.join(case, destination_name)
        if subject == "git":
            build_git_repo(source)
        else:
            build_sample_db(source)
        result = run_with_hook(script, [source, destination], "post_temp_pin", "fail")
        label = "G " + subject + " pinned temp-creation failure"
        expect_exact_refusal(
            result,
            b"Error: Temporary directory creation failed after pinning; removed safely.\n",
            label,
        )
        assert_no_residue(case, destination, label)
    print(
        "  G: replaced temp trees are preserved and pinned creation failures clean up "
        "across backup, restore and Git."
    )


def git(args, cwd=None, env=None):
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        env=env,
        check=False,
    )
    if result.returncode != 0:
        raise DrillError(
            "git " + " ".join(args) + " failed: "
            + result.stderr.decode("utf-8", "backslashreplace")
        )
    return result.stdout.decode("utf-8", "strict")


def build_git_repo(path):
    os.makedirs(path, exist_ok=True)
    git(["-C", path, "init", "-b", "main"])
    git(["-C", path, "config", "user.name", "Drill"])
    git(["-C", path, "config", "user.email", "drill@example.invalid"])
    with open(os.path.join(path, "test.txt"), "w") as handle:
        handle.write("hello\n")
    git(["-C", path, "add", "test.txt"])
    git(["-C", path, "commit", "-m", "init"])
    git(["-C", path, "branch", "feature"])
    return path


def expected_git_refs(repo):
    refs = {}
    for line in git(["-C", repo, "show-ref"]).splitlines():
        oid, name = line.split(" ", 1)
        refs[name] = oid
    refs["HEAD"] = git(["-C", repo, "rev-parse", "HEAD"]).strip()
    return refs


def check_git_receipt(receipt, repo, bundle, label):
    if set(receipt) != GIT_RECEIPT_KEYS:
        raise DrillError(label + ": receipt keys mismatch: " + str(sorted(receipt)))
    if receipt["evidence_stage"] != "post_unlink":
        raise DrillError(label + ": receipt does not describe the post-unlink state.")
    if receipt["object_format"] not in ("sha1", "sha256"):
        raise DrillError(label + ": unexpected object format.")
    refs = expected_git_refs(repo)
    got = {entry["refname"]: entry["oid"] for entry in receipt["refs"]}
    if len(receipt["refs"]) != len(got):
        raise DrillError(label + ": receipt contains duplicate ref entries.")
    if got != refs:
        raise DrillError(label + ": ref map mismatch: " + str(got) + " vs " + str(refs))
    if receipt["refs_count"] != len(refs):
        raise DrillError(label + ": ref count mismatch.")
    git(["-C", repo, "bundle", "verify", bundle])
    listed = {}
    for line in git(["-C", repo, "bundle", "list-heads", bundle]).splitlines():
        oid, refname = line.split(" ", 1)
        if refname in listed:
            raise DrillError(label + ": independently listed bundle refs are not unique.")
        listed[refname] = oid
    if listed != refs:
        raise DrillError(label + ": independent bundle list-heads map mismatch.")
    if receipt["head_oid"] != refs["HEAD"]:
        raise DrillError(label + ": HEAD object id mismatch.")
    if receipt["sha256"] != sha256_of(bundle):
        raise DrillError(label + ": bundle checksum mismatch.")
    check_identity(receipt["identity"], bundle, label)
    check_identity(receipt["source_identity"], repo, label + " source")
    if (os.lstat(bundle).st_mode & 0o777) != 0o600:
        raise DrillError(label + ": bundle mode is not 0600.")
    repository = receipt["repository"]
    if set(repository) != {"toplevel", "git_dir", "common_dir", "dot_git"}:
        raise DrillError(label + ": repository evidence keys mismatch.")
    for key, path in (
        ("toplevel", git(["-C", repo, "rev-parse", "--show-toplevel"]).strip()),
        ("git_dir", git(["-C", repo, "rev-parse", "--absolute-git-dir"]).strip()),
    ):
        st = os.lstat(path)
        if (repository[key]["dev"], repository[key]["ino"]) != (st.st_dev, st.st_ino):
            raise DrillError(label + ": " + key + " identity mismatch.")
    common = os.path.realpath(
        os.path.join(repo, git(["-C", repo, "rev-parse", "--git-common-dir"]).strip())
    )
    st = os.lstat(common)
    if (repository["common_dir"]["dev"], repository["common_dir"]["ino"]) != (
        st.st_dev,
        st.st_ino,
    ):
        raise DrillError(label + ": common directory identity mismatch.")
    marker_path = os.path.join(
        git(["-C", repo, "rev-parse", "--show-toplevel"]).strip(), ".git"
    )
    marker_st = os.lstat(marker_path)
    if repository["dot_git"] is None:
        raise DrillError(label + ": the '.git' marker evidence is missing.")
    if (repository["dot_git"]["dev"], repository["dot_git"]["ino"]) != (
        marker_st.st_dev,
        marker_st.st_ino,
    ):
        raise DrillError(label + ": '.git' marker identity mismatch.")
    marker = repository["dot_git"]
    if marker["mode"] != marker_st.st_mode:
        raise DrillError(label + ": '.git' marker mode mismatch.")
    if marker.get("nlink") != marker_st.st_nlink:
        raise DrillError(label + ": '.git' marker link-count mismatch.")
    if stat.S_ISREG(marker_st.st_mode):
        if marker["size"] != marker_st.st_size:
            raise DrillError(label + ": regular '.git' marker size mismatch.")
        with open(marker_path, "rb") as handle:
            marker_bytes = handle.read()
        if repository["dot_git"].get("content_sha256") != hashlib.sha256(marker_bytes).hexdigest():
            raise DrillError(label + ": '.git' marker content evidence mismatch.")
        marker_text = marker_bytes.decode("utf-8", "strict")
        if marker.get("content") != marker_text or not marker_text.startswith("gitdir: "):
            raise DrillError(label + ": '.git' marker grammar mismatch.")
        target = marker_text[len("gitdir: "):-1]
        resolved = target if os.path.isabs(target) else os.path.join(
            repository["toplevel"]["path"], target
        )
        if os.path.abspath(resolved) != repository["git_dir"]["path"]:
            raise DrillError(label + ": '.git' marker target does not match git_dir evidence.")
    elif marker["size"] is not None:
        raise DrillError(label + ": non-regular '.git' marker size is not null.")
    return refs


def drill_s1_git_core(sandbox):
    """The S1 git happy path plus its refusal surface."""
    repo = build_git_repo(os.path.join(sandbox, "repo"))
    bundle = os.path.join(sandbox, "repo.bundle")
    before_refs = expected_git_refs(repo)
    receipt = load_receipt(
        run_ok(GIT_BACKUP_SCRIPT, [repo, bundle], "S1 git backup").stdout, "S1 git backup"
    )
    check_git_receipt(receipt, repo, bundle, "S1 git backup")
    if receipt["head_ref"] != "refs/heads/main":
        raise DrillError("S1 git: expected an attached HEAD on refs/heads/main.")
    expect_refusal(
        run_script(GIT_BACKUP_SCRIPT, [repo, bundle]), "already exists", "S1 git no-clobber"
    )
    expect_refusal(
        run_script(GIT_BACKUP_SCRIPT, [repo, os.path.join(repo, ".git", "x.bundle")]),
        "Destination cannot be inside",
        "S1 git inside-git-dir",
    )
    expect_refusal(
        run_script(GIT_BACKUP_SCRIPT, [repo, os.path.join(repo, "x.bundle")]),
        "Destination cannot be inside",
        "S1 git inside-worktree",
    )
    if expected_git_refs(repo) != before_refs:
        raise DrillError("S1 git: the source refs changed during the backup.")
    return repo, bundle


def oracle_h_ambient_git(sandbox, repo):
    """H: ambient GIT_* selectors are ignored."""
    poison = os.path.join(sandbox, "h-poison")
    os.makedirs(poison, exist_ok=True)
    env = dict(os.environ)
    env["GIT_DIR"] = poison
    env["GIT_WORK_TREE"] = poison
    env["GIT_COMMON_DIR"] = poison
    env["GIT_OBJECT_DIRECTORY"] = poison
    env["GIT_ALTERNATE_OBJECT_DIRECTORIES"] = poison
    env["GIT_INDEX_FILE"] = os.path.join(poison, "index")
    env["GIT_NAMESPACE"] = "poison"
    bundle = os.path.join(sandbox, "h.bundle")
    receipt = load_receipt(
        run_ok(GIT_BACKUP_SCRIPT, [repo, bundle], "H poisoned environment", env=env).stdout,
        "H poisoned environment",
    )
    check_git_receipt(receipt, repo, bundle, "H poisoned environment")
    print("  H: ambient GIT_* selectors are stripped from every invocation.")


def oracle_i_git_grammar(sandbox, repo):
    """I: byte-exact grammar faults for show-ref and bundle list-heads."""
    real_git = shutil.which("git")
    if real_git is None:
        raise DrillError("I: git is not on PATH.")
    fake_dir = os.path.join(sandbox, "i-fake")
    os.makedirs(fake_dir, exist_ok=True)
    fake_git = os.path.join(fake_dir, "git")
    with open(fake_git, "w") as handle:
        handle.write(FAKE_GIT_SOURCE)
    os.chmod(fake_git, 0o755)
    config_path = os.path.join(fake_dir, "config.json")
    head = git(["-C", repo, "rev-parse", "HEAD"]).strip()
    out_dir = os.path.join(sandbox, "i-out")
    os.makedirs(out_dir, exist_ok=True)

    cases = [
        ("object-format failure", "--show-object-format", "", "boom", 2,
         b"Error: git rev-parse --show-object-format failed: boom\n"),
        ("object-format garbage", "--show-object-format", "sha3\n", "", 0,
         b"Error: Unsupported object format: 'sha3'\n"),
        ("object-format newline", "--show-object-format", "sha1", "", 0,
         b"Error: Malformed object format output: missing the terminal newline.\n"),
        ("repository-path newline", "--show-toplevel", "/a\n/b\n/c", "", 0,
         b"Error: Malformed repository path records: missing the terminal newline.\n"),
    ]
    valid = head + " refs/heads/main\n"
    for command, prefix in (("show-ref", "git show-ref"), ("list-heads", "git bundle list-heads")):
        diagnostic = "git show-ref output" if command == "show-ref" else "git bundle list-heads output"
        cases.extend(
            [
                (command + " failure", command, "", "boom", 2,
                 ("Error: " + prefix + " failed: boom\n").encode()),
                (command + " garbage", command, "garbage\n", "", 0,
                 ("Error: Malformed git record grammar in " + diagnostic + ": 'garbage'\n").encode()),
                (command + " missing-lf", command, valid[:-1], "", 0,
                 ("Error: Malformed " + diagnostic + ": missing the terminal newline.\n").encode()),
                (command + " extra-record", command, valid + "\n", "", 0,
                 ("Error: Malformed " + diagnostic + ": empty record.\n").encode()),
                (command + " crlf", command, valid[:-1] + "\r\n", "", 0,
                 ("Error: Malformed " + diagnostic + ": carriage return in the output.\n").encode()),
                (command + " bare-cr", command, valid[:-1] + "\r", "", 0,
                 ("Error: Malformed " + diagnostic + ": carriage return in the output.\n").encode()),
                (command + " whitespace", command, head + "  refs/heads/main\n", "", 0,
                 ("Error: Malformed refname whitespace in " + diagnostic + ": ' refs/heads/main'\n").encode()),
                (command + " duplicate", command, valid + valid, "", 0,
                 ("Error: Duplicate refname in " + diagnostic + ": 'refs/heads/main'\n").encode()),
                (command + " nonhex", command, ("z" * len(head)) + " refs/heads/main\n", "", 0,
                 ("Error: Non-hexadecimal OID in " + diagnostic + ": '" + ("z" * len(head)) + "'\n").encode()),
                (command + " short-oid", command, head[:-1] + " refs/heads/main\n", "", 0,
                 ("Error: Malformed git record grammar in " + diagnostic + ": "
                  + repr(head[:-1] + " refs/heads/main") + "\n").encode()),
                (command + " long-oid", command, head + "a refs/heads/main\n", "", 0,
                 ("Error: Malformed git record grammar in " + diagnostic + ": "
                  + repr(head + "a refs/heads/main") + "\n").encode()),
                (command + " invalid-ref", command, head + " refs/heads/../main\n", "", 0,
                 ("Error: Invalid refname in " + diagnostic + ": 'refs/heads/../main'\n").encode()),
                (command + " pseudoref", command, head + " ORIG_HEAD\n", "", 0,
                 ("Error: Invalid refname (pseudoref) in " + diagnostic + ": 'ORIG_HEAD'\n").encode()),
                (command + " missing-ref", command, head + " HEAD\n", "", 0,
                 (("Error: Invalid refname (pseudoref) in " + diagnostic + ": 'HEAD'\n")
                  if command == "show-ref"
                  else "Error: Bundle list-heads does not exactly match source refs.\n").encode()),
                (command + " extra-ref", command, valid + head + " refs/heads/extra\n", "", 0,
                 (("Error: Source ref is absent or unverifiable: 'refs/heads/extra'\n")
                  if command == "show-ref"
                  else "Error: Bundle list-heads does not exactly match source refs.\n").encode()),
            ]
        )

    for index, (name, match, out, err, code, expected_stderr) in enumerate(cases):
        fired = os.path.join(fake_dir, "fired-" + str(index))
        with open(config_path, "w") as handle:
            json.dump(
                {
                    "match": match,
                    "stdout": out,
                    "stderr": err,
                    "exit_code": code,
                    "real": real_git,
                    "fired": fired,
                },
                handle,
            )
        env = dict(os.environ)
        env["PATH"] = fake_dir + os.pathsep + env.get("PATH", "")
        env["GROK_FAKE_GIT_CONFIG"] = config_path
        destination = os.path.join(out_dir, "i" + str(index) + ".bundle")
        result = run_script(GIT_BACKUP_SCRIPT, [repo, destination], env=env)
        if not os.path.exists(fired):
            raise DrillError("I " + name + ": fake Git injection did not fire.")
        expect_exact_refusal(result, expected_stderr, "I " + name)
        assert_no_residue(out_dir, destination, "I " + name)
    print("  I: " + str(len(cases)) + " Git grammar defects each fail closed with no output.")


def oracle_j_worktrees(sandbox, repo, bundle):
    """J: linked-worktree, attached and detached receipts are exact."""
    linked = os.path.join(sandbox, "linked-worktree")
    git(["-C", repo, "branch", "wt"])
    git(["-C", repo, "worktree", "add", linked, "wt"])
    with open(os.path.join(linked, "test.txt"), "a") as handle:
        handle.write("worktree\n")
    git(["-C", linked, "add", "test.txt"])
    git(["-C", linked, "commit", "-m", "worktree commit"])

    linked_bundle = os.path.join(sandbox, "linked.bundle")
    refs_before = expected_git_refs(linked)
    receipt = load_receipt(
        run_ok(GIT_BACKUP_SCRIPT, [linked, linked_bundle], "J linked worktree").stdout,
        "J linked worktree",
    )
    if expected_git_refs(linked) != refs_before:
        raise DrillError("J: the linked worktree refs changed during the backup.")
    check_git_receipt(receipt, linked, linked_bundle, "J linked worktree")
    if receipt["head_ref"] != "refs/heads/wt":
        raise DrillError("J: the linked worktree HEAD is not refs/heads/wt.")
    repository = receipt["repository"]
    if repository["git_dir"]["ino"] == repository["common_dir"]["ino"]:
        raise DrillError("J: a linked worktree must not share its git dir with the common dir.")
    if repository["dot_git"]["size"] is None:
        raise DrillError("J: a linked worktree '.git' marker must be a regular file.")
    if not stat.S_ISREG(repository["dot_git"]["mode"]):
        raise DrillError("J: the linked worktree '.git' marker is not a regular file.")
    expect_refusal(
        run_script(GIT_BACKUP_SCRIPT, [linked, os.path.join(linked, "x.bundle")]),
        "Destination cannot be inside",
        "J linked worktree inside-worktree",
    )

    nested = os.path.join(repo, "nested")
    os.mkdir(nested)
    nested_bundle = os.path.join(sandbox, "nested.bundle")
    nested_before = expected_git_refs(nested)
    nested_receipt = load_receipt(
        run_ok(GIT_BACKUP_SCRIPT, [nested, nested_bundle], "J nested source").stdout,
        "J nested source",
    )
    check_git_receipt(nested_receipt, nested, nested_bundle, "J nested source")
    if nested_receipt["repository"]["dot_git"] is None:
        raise DrillError("J: nested source did not pin the top-level '.git' marker.")
    if expected_git_refs(nested) != nested_before:
        raise DrillError("J: nested source refs changed during the backup.")

    head = git(["-C", repo, "rev-parse", "HEAD"]).strip()
    git(["-C", repo, "checkout", "--detach", head])
    detached_bundle = os.path.join(sandbox, "detached.bundle")
    detached_before = expected_git_refs(repo)
    detached = load_receipt(
        run_ok(GIT_BACKUP_SCRIPT, [repo, detached_bundle], "J detached HEAD").stdout,
        "J detached HEAD",
    )
    check_git_receipt(detached, repo, detached_bundle, "J detached HEAD")
    if detached["head_ref"] != "DETACHED":
        raise DrillError("J: a detached HEAD must be recorded as DETACHED.")
    if detached["head_oid"] != head:
        raise DrillError("J: the detached HEAD object id is wrong.")
    if expected_git_refs(repo) != detached_before:
        raise DrillError("J: the detached source refs changed during the backup.")
    git(["-C", repo, "checkout", "main"])

    for path in (bundle, linked_bundle, nested_bundle, detached_bundle):
        if os.lstat(path).st_nlink != 1:
            raise DrillError("J: a published bundle has a link count other than 1.")
    print("  J: linked-worktree, attached and detached receipts verified exactly.")


def run_drill_s1():
    print("Running Drill S1: backup/restore integrity...")
    sandbox = new_sandbox("grok-s1-")
    try:
        backup = drill_s1_database(sandbox)
        oracle_a_wal(sandbox)
        oracle_b_symlinks(sandbox, backup)
        oracle_c_parent_exchange(sandbox)
        oracle_d_async_abort(sandbox)
        oracle_e_foreign_replacement(sandbox)
        oracle_f_post_unlink_mutation(sandbox)
        oracle_g_temp_replacement(sandbox)
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)
    print("Drill S1: complete (database receipts, WAL continuity and oracles A-G).")


def run_drill_s1_git():
    print("Running Drill S1 Git: bundle integrity...")
    sandbox = new_sandbox("grok-s1-git-")
    try:
        repo, bundle = drill_s1_git_core(sandbox)
        oracle_h_ambient_git(sandbox, repo)
        oracle_i_git_grammar(sandbox, repo)
        oracle_j_worktrees(sandbox, repo, bundle)
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)
    print("Drill S1 Git: complete (bundle receipts and oracles H-J).")


def run_drill_s2():
    print("Running Drill S2: interrupted-work recovery...")
    from omniagentos.toolplane.observe import (
        DEADLETTER_SUBDIR,
        OBSERVATIONS_SUBDIR,
        SPOOL_SUBDIR,
        ObservationSink,
    )

    sandbox = new_sandbox("grok-s2-")
    try:
        spool_dir = os.path.join(sandbox, SPOOL_SUBDIR)
        os.makedirs(spool_dir, exist_ok=True)
        payload = {
            "attempts": 1,
            "observation": {
                "version": "1",
                "ts": "2026-08-02T12:00:00Z",
                "tool": "read_file",
                "correlation_id": "corr_valid_12345",
                "status": "success",
                "ok": True,
                "duration_ms": 15,
                "source": "toolplane",
            },
        }
        with open(os.path.join(spool_dir, "corr_valid_12345.json"), "w") as handle:
            json.dump(payload, handle)
        with open(os.path.join(spool_dir, "corr_corrupt_67890.json"), "w") as handle:
            handle.write("{corrupt json content")

        sink = ObservationSink(ledger_dir=sandbox)
        pending = sink.recover_pending()
        if pending != 1:
            raise DrillError("S2: expected 1 recoverable pending entry, got " + str(pending))
        still_pending = sink.retry_pending()
        if still_pending != 0:
            raise DrillError("S2: expected 0 pending entries after retry, got "
                             + str(still_pending))

        obs_dir = os.path.join(sandbox, OBSERVATIONS_SUBDIR)
        obs_files = os.listdir(obs_dir)
        if len(obs_files) != 1:
            raise DrillError("S2: expected 1 recorded observation, got " + str(len(obs_files)))
        with open(os.path.join(obs_dir, obs_files[0])) as handle:
            recorded = json.load(handle)
        if recorded.get("correlation_id") != "corr_valid_12345":
            raise DrillError("S2: correlation id mismatch in the stored observation.")

        dead_files = os.listdir(os.path.join(sandbox, DEADLETTER_SUBDIR))
        if len(dead_files) != 1:
            raise DrillError("S2: expected 1 quarantined file, got " + str(len(dead_files)))
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)
    print("Drill S2: complete (product-path backlog recovery and quarantine verified).")


def run_drill_s3():
    print("Running Drill S3: idempotent replay...")
    from omniagentos.toolplane.observe import (
        OBSERVATIONS_SUBDIR,
        SPOOL_SUBDIR,
        ObservationSink,
    )

    sandbox = new_sandbox("grok-s3-")
    try:
        observation = {
            "version": "1",
            "ts": "2026-08-02T12:00:00Z",
            "tool": "read_file",
            "correlation_id": "idem_restart",
            "status": "success",
            "ok": True,
            "duration_ms": 10,
            "source": "toolplane",
        }
        obs_dir = os.path.join(sandbox, OBSERVATIONS_SUBDIR)
        os.makedirs(obs_dir, exist_ok=True)
        final_name = observation["ts"].replace(":", "-") + "_" + observation["correlation_id"]
        with open(os.path.join(obs_dir, final_name + ".json"), "w") as handle:
            json.dump(observation, handle)

        spool_dir = os.path.join(sandbox, SPOOL_SUBDIR)
        os.makedirs(spool_dir, exist_ok=True)
        with open(os.path.join(spool_dir, observation["correlation_id"] + ".json"), "w") as handle:
            json.dump({"attempts": 1, "observation": observation}, handle)

        sink = ObservationSink(ledger_dir=sandbox)
        recovered = sink.recover_pending()
        if recovered != 1:
            raise DrillError("S3: expected 1 recovered pending entry, got " + str(recovered))
        still_pending = sink.retry_pending()
        if still_pending != 0:
            raise DrillError("S3: expected 0 pending entries, got " + str(still_pending))
        obs_files = os.listdir(obs_dir)
        if len(obs_files) != 1:
            raise DrillError("S3: observations were duplicated on replay: " + str(obs_files))
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)
    print("Drill S3: complete (replay recovery is idempotent through the real sink).")


def self_test():
    run_drill_s1()
    run_drill_s1_git()
    run_drill_s2()
    run_drill_s3()
    print("")
    print("S4-S12 are operator-only and unimplemented; they need the operator's explicit approval")
    print("before any PID, network, provider or disk action.")
    print("S1-S3 completed in throwaway temporary directories.")


if __name__ == "__main__":
    if "--self-test" not in sys.argv:
        print("Usage: python grok_sandbox_drills.py --self-test")
        sys.exit(1)
    try:
        self_test()
    except Exception as exc:
        print("Drill failed: " + str(exc))
        sys.exit(1)
    sys.exit(0)
