"""Upload this machine's AI/terminal session transcripts to the team archive.

SELF-CONTAINED BY DESIGN: stdlib only, no omniagentos imports, so the file can
be copied to any teammate's laptop or server (``cp`` it into ``~/bin/``) and run
with a bare system ``python3`` — same deployment story as
``omniagentos/team/session_collector.py``, whose ``_write_atomic``/``_iso``
helpers are DELIBERATELY DUPLICATED below rather than imported for exactly that
reason.

What it does, once per run:

1. Harvests every session transcript this machine has written inside
   ``--window-days`` (Claude Code, Codex, Kimi, Gemini, Grok, plus the generic
   ``~/.ai-transcripts-spool`` drop directory any other tool can write into).
2. Skips anything already uploaded at its current size (per-file mtime+size
   watermark in ``--state``), anything binary, and anything over 50 MB.
3. REDACTS live key shapes out of the COPY — the originals are never modified,
   never moved, and never opened for writing.
4. Writes the redacted copy into an ``ai-transcripts`` clone as
   ``transcripts/<dev>/<YYYY-MM-DD>/<hostname>__<cli>__<basename>``, appends one
   line to ``transcripts/<dev>/NOTES.md``, then commits and pushes.

The clone is NOT created for you: a missing ``--dest`` exits 2 and prints the
``gh repo clone`` command to run, because silently cloning a repo onto someone's
laptop from a cron job is not a thing this should decide.

Cron, one line per dev (hourly at :17, log kept locally so a failure is
readable after the fact):

    17 * * * * /usr/bin/python3 $HOME/bin/transcript_uploader.py --employee emp_owner    >> $HOME/.transcript-uploader.log 2>&1
    17 * * * * /usr/bin/python3 $HOME/bin/transcript_uploader.py --employee emp_bob >> $HOME/.transcript-uploader.log 2>&1
    17 * * * * /usr/bin/python3 $HOME/bin/transcript_uploader.py --employee emp_alice   >> $HOME/.transcript-uploader.log 2>&1

KILL SWITCH: the machine's owner can stop uploads at any time by creating
``~/.ai-telemetry-off`` (``telemetry_ctl.py off``). While the marker exists
this script scans NOTHING and uploads NOTHING — it prints that telemetry is
off and exits 0. Only the owner removes the marker; nothing re-enables it
remotely. Privacy contract: docs/operations/team-telemetry-privacy.md.

Exit codes: 0 uploaded or nothing new, 2 bad invocation (unusable employee id,
unparseable ``--roots``, missing clone), 3 push failed. A push failure is
TERMINAL — quota, auth and non-fast-forward errors do not get better by being
retried in a loop, so the next cron tick is the retry.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from omniagentos.path_containment import inode_relative_parts

SCHEMA = 1
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
SNIFF_BYTES = 8192
GIT_TIMEOUT = 180
CLONE_REPO = "Globex/ai-transcripts"

# (cli label, glob pattern relative to $HOME). Absence is normal — a laptop with
# no Kimi installed simply contributes no kimi files.
DEFAULT_ROOTS: tuple[tuple[str, str], ...] = (
    ("claude", ".claude/projects/**/*.jsonl"),
    ("claude", ".claude-*/projects/**/*.jsonl"),
    ("codex", ".codex*/sessions/**/*.jsonl"),
    ("kimi", ".kimi-code/sessions/**/wire.jsonl"),
    ("kimi", ".kimi2/sessions/**/wire.jsonl"),
    ("gemini", ".gemini/tmp/**/*.json"),
    ("grok", ".grok/sessions/**/*.jsonl"),
    # The mixed-tooling escape hatch: any CLI that has no harvest root of its
    # own can drop a log here and it ships with everything else.
    ("spool", ".ai-transcripts-spool/**/*"),
)

# Live-key shapes. Names are the labels that appear in the redacted copy, so
# a reader of an uploaded transcript can tell WHAT was removed without seeing it.
SECRET_SHAPES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("openai-key", re.compile(r"sk-[A-Za-z0-9_\-]{8,}")),
    ("github-pat", re.compile(r"ghp_[A-Za-z0-9]{20,}")),
    ("github-fine-grained-pat", re.compile(r"github_pat_[A-Za-z0-9_]{20,}")),
    ("slack-token", re.compile(r"xox[bp]-[A-Za-z0-9\-]{10,}")),
    ("aws-access-key-id", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("google-api-key", re.compile(r"AIza[0-9A-Za-z_\-]{30,}")),
    ("facebook-token", re.compile(r"EAA[A-Za-z0-9]{30,}")),
    ("atlassian-token", re.compile(r"pit-[a-f0-9\-]{16,}")),
    ("slack-webhook", re.compile(r"hooks\.slack\.com/services/[A-Za-z0-9/]+")),
)
PRIVATE_KEY_BEGIN = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
PRIVATE_KEY_END = re.compile(r"-----END [A-Z ]*PRIVATE KEY-----")
PRIVATE_KEY_SHAPE = "private-key"

_EMPLOYEE = re.compile(r"emp_[a-z0-9][a-z0-9_-]*\Z")
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def _iso(ts: float) -> str:
    """Duplicated from session_collector for the standalone-copy contract."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


# Owner kill switch — duplicated from session_collector (standalone-copy contract).
OPT_OUT_MARKER = ".ai-telemetry-off"


def _opt_out_since(home: Path) -> str | None:
    """ISO time telemetry was switched off, or None when it is on.

    Any marker file counts as OFF — a hand-made empty file must work, not just
    the JSON telemetry_ctl writes — so an unparseable marker falls back to the
    file's mtime rather than being ignored (fail toward the owner's choice).
    A marker that EXISTS but cannot be read (permissions, I/O error) also
    counts as OFF: an unreadable off-switch is still an off-switch — only a
    genuinely absent marker means ON (cross-lineage review finding 1).
    """
    path = home / OPT_OUT_MARKER
    try:
        raw = path.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError:
        return "unknown (marker unreadable)"
    try:
        data = json.loads(raw)
        if isinstance(data, dict) and data.get("off_since"):
            return str(data["off_since"])
    except ValueError:
        pass
    try:
        return _iso(path.stat().st_mtime)
    except OSError:
        return _iso(time.time())


def _day(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


def _safe_segment(value: str) -> str:
    """A single path segment that cannot escape the directory it is joined to."""
    cleaned = _UNSAFE_NAME.sub("-", value).strip("-.")
    return cleaned or "unnamed"


def redact_text(text: str) -> tuple[str, int]:
    """Return *text* with every known key shape replaced, and the hit count.

    PRIVATE KEY blocks redact from the BEGIN marker to the matching END line —
    the body carries the key material and is dropped from the copy entirely,
    counted as ONE redaction for the block rather than one per base64 line.
    An unterminated block redacts to end of file (fail closed).
    """
    output: list[str] = []
    redactions = 0
    inside_key = False
    for line in text.splitlines(keepends=True):
        if inside_key:
            if PRIVATE_KEY_END.search(line):
                inside_key = False
            continue
        begin = PRIVATE_KEY_BEGIN.search(line)
        if begin:
            redactions += 1
            end = PRIVATE_KEY_END.search(line, begin.end())
            if end:
                tail = line[end.end() :]
            else:
                inside_key = True
                stripped = line.rstrip("\r\n")
                tail = line[len(stripped) :]
            line = f"{line[: begin.start()]}[REDACTED:{PRIVATE_KEY_SHAPE}]{tail}"
        for name, pattern in SECRET_SHAPES:
            line, hits = pattern.subn(f"[REDACTED:{name}]", line)
            redactions += hits
        output.append(line)
    return "".join(output), redactions


# The clone is pulled from a repo the other devs can write, so a hostile commit
# could make transcripts/<victim>/<date> (or the NOTES path) a symlink to a
# writable local dir — the victim's next cron would then write outside the clone.
# A committed symlink is refused, and every write is bound to O_NOFOLLOW directory
# descriptors walked one component at a time, so a name cannot be re-resolved
# through a symlink swapped in AFTER a check (the TOCTOU a pathname check leaves
# open). When the platform lacks dir_fd support we fall back to a pathname check
# that still refuses a statically committed symlink.
_DIRFD_OK = {os.open, os.mkdir, os.rename, os.unlink} <= os.supports_dir_fd


def _bad_segment(segment: str) -> bool:
    return segment in ("", ".", "..") or "/" in segment or os.sep in segment or "\0" in segment


def _open_clone_dir(dest: Path, dir_parts: list[str]) -> int | None:
    """Open ``dest``/<dir_parts> via an O_NOFOLLOW component walk, creating
    missing dirs. Returns a directory fd (caller closes it) or None if any
    component is unsafe, a symlink, or not a directory."""
    if any(_bad_segment(part) for part in dir_parts):
        return None
    try:
        # dest itself is the user's own --dest; allow it to be a symlink they
        # made, but every component BELOW it is walked with O_NOFOLLOW.
        dir_fd = os.open(dest, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return None
    try:
        for part in dir_parts:
            try:
                os.mkdir(part, 0o755, dir_fd=dir_fd)
            except FileExistsError:
                pass
            except OSError:
                return None
            try:
                nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)
            except OSError:
                return None  # ELOOP: the component is a symlink → refuse
            os.close(dir_fd)
            dir_fd = nxt
        keep, dir_fd = dir_fd, -1
        return keep
    finally:
        if dir_fd >= 0:
            os.close(dir_fd)


def _write_into_clone(dest: Path, relative: str, text: str, *, append: bool) -> bool:
    """Write (or append) *text* to ``dest``/*relative*, bound to O_NOFOLLOW
    descriptors so no path component can be a symlink at any point. Returns
    False (never raises on a refusal) when the target is unsafe."""
    *dir_parts, filename = relative.split("/")
    if _bad_segment(filename):
        return False
    if not _DIRFD_OK:
        target = _safe_repo_target_fallback(dest, relative)
        if target is None:
            return False
        if append:
            _append_notes(target, text)
        else:
            _write_atomic(target, text)
        return True
    dir_fd = _open_clone_dir(dest, dir_parts)
    if dir_fd is None:
        return False
    try:
        if append:
            fd = os.open(
                filename,
                os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW,
                0o644,
                dir_fd=dir_fd,
            )
            with os.fdopen(fd, "a", encoding="utf-8") as handle:
                handle.write(text)
            return True
        tmp = f".{filename}.tmp-{os.getpid()}"
        try:
            fd = os.open(
                tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=dir_fd
            )
        except OSError:
            return False
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            # rename replaces the NAME, never following a symlink sitting at it.
            os.replace(tmp, filename, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        except BaseException:
            try:
                os.unlink(tmp, dir_fd=dir_fd)
            except OSError:
                pass
            raise
        return True
    except OSError:
        return False
    finally:
        os.close(dir_fd)


def _safe_repo_target_fallback(dest: Path, relative: str) -> Path | None:
    """Pathname containment check for platforms without dir_fd support.

    Refuses a statically committed symlink (every existing component must be a
    real entry and the result must stay under ``dest``). It cannot close the
    check-to-use race — that is what the O_NOFOLLOW path above is for — but it
    keeps the committed-symlink attack closed everywhere.
    """
    base = dest.resolve()
    current = base
    for segment in Path(relative).parts:
        if _bad_segment(segment):
            return None
        current = current / segment
        if current.is_symlink():
            return None
    # Inode-proved containment (the shared primitive, ratchet test_19): the
    # deepest existing ancestor is compared to ``base`` by inode, and the
    # not-yet-created suffix rides its missing-parts walk — a lexical
    # parents-membership check here was the exact class the registry exists to
    # retire. Fails closed on any unmeasurable state.
    #
    # The primitive documents that the CALLER must pre-resolve (a raw
    # ``symlink/../x`` candidate is lexically flattened by abspath before the
    # walk — a real latent bypass in the shared helper, filed for its own lane).
    # This call site cannot produce that shape: the loop above rejected every
    # ".."/symlink segment, so ``current`` is already canonical — but pass its
    # ``resolve()`` explicitly so a future weakening of that loop cannot quietly
    # inherit the primitive's masking, rather than relying on a neighbour's guard.
    if inode_relative_parts(current.resolve(), base) is None:
        return None
    return current


def _write_atomic(path: Path, text: str) -> None:
    """Duplicated from session_collector (see the module docstring)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle_fd, temp = tempfile.mkstemp(dir=str(path.parent), prefix=".transcript-upload-")
    try:
        with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, 0o644)
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def load_state(path: Path) -> dict[str, dict[str, float]]:
    """Per-file watermarks from the last run; unreadable state starts empty."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    files = raw.get("files") if isinstance(raw, dict) else None
    if not isinstance(files, dict):
        return {}
    result: dict[str, dict[str, float]] = {}
    for key, value in files.items():
        if isinstance(value, dict) and "mtime" in value and "size" in value:
            result[str(key)] = {"mtime": float(value["mtime"]), "size": float(value["size"])}
    return result


def save_state(path: Path, watermarks: dict[str, dict[str, float]]) -> None:
    payload = {"schema": SCHEMA, "updated_at": _iso(time.time()), "files": watermarks}
    _write_atomic(path, json.dumps(payload, indent=1, sort_keys=True) + "\n")


def parse_roots(raw: str | None) -> list[tuple[str, str]]:
    """``--roots`` override: ``[{"cli": "claude", "glob": ".claude/**/*.jsonl"}]``.

    Patterns are relative to $HOME (absolute patterns are honoured as-is), which
    keeps one JSON usable across machines with different home directories.
    """
    if raw is None:
        return list(DEFAULT_ROOTS)
    entries = json.loads(raw)
    if not isinstance(entries, list) or not entries:
        raise ValueError("--roots must be a non-empty JSON list")
    roots: list[tuple[str, str]] = []
    for entry in entries:
        if isinstance(entry, dict):
            cli, pattern = entry.get("cli"), entry.get("glob")
        elif isinstance(entry, (list, tuple)) and len(entry) == 2:
            cli, pattern = entry[0], entry[1]
        else:
            raise ValueError(f"--roots entry must be an object or [cli, glob] pair: {entry!r}")
        if not cli or not pattern:
            raise ValueError(f"--roots entry needs both cli and glob: {entry!r}")
        roots.append((str(cli), str(pattern)))
    return roots


def harvest(
    roots: list[tuple[str, str]], home: Path, *, since: float
) -> list[tuple[str, Path, float, int]]:
    """(cli, path, mtime, size) for every readable file inside the window.

    First pattern wins for a file matched by several roots, so ``.codex*`` does
    not report ``.codex`` twice and the generic spool never relabels a file a
    dedicated root already claimed.
    """
    found: list[tuple[str, Path, float, int]] = []
    claimed: set[str] = set()
    for cli, pattern in roots:
        expanded = pattern if os.path.isabs(pattern) else str(home / pattern)
        for match in sorted(glob.glob(expanded, recursive=True)):
            if match in claimed:
                continue
            try:
                info = os.stat(match)
            except OSError:
                continue
            if not os.path.isfile(match) or info.st_mtime < since:
                continue
            claimed.add(match)
            found.append((cli, Path(match), info.st_mtime, info.st_size))
    return found


def is_new_or_grown(
    watermarks: dict[str, dict[str, float]], key: str, mtime: float, size: int
) -> bool:
    previous = watermarks.get(key)
    if previous is None:
        return True
    return size > previous["size"] or mtime > previous["mtime"]


def target_name(host: str, cli: str, path: Path) -> str:
    """``<hostname>__<cli>__<basename>``.

    Kimi names every transcript ``wire.jsonl`` and puts the session id in the
    parent directory, so the parent supplies the basename there — otherwise one
    machine's kimi sessions would all collide on one filename.
    """
    basename = f"{path.parent.name}.jsonl" if path.name == "wire.jsonl" else path.name
    return f"{_safe_segment(host)}__{_safe_segment(cli)}__{_safe_segment(basename)}"


def _read_for_upload(path: Path, size: int) -> tuple[str, int] | str:
    """Redacted text plus its redaction count, or a string skip reason."""
    if size > MAX_UPLOAD_BYTES:
        return f"larger than {MAX_UPLOAD_BYTES} bytes ({size})"
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return f"unreadable ({exc})"
    if b"\x00" in raw[:SNIFF_BYTES]:
        return "binary"
    return redact_text(raw.decode("utf-8", errors="replace"))


def plan_uploads(
    *,
    roots: list[tuple[str, str]],
    home: Path,
    dev_short: str,
    host: str,
    window_days: float,
    watermarks: dict[str, dict[str, float]],
    now: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(uploads, skips). Reads originals only; writes nothing anywhere."""
    uploads: list[dict[str, Any]] = []
    skips: list[dict[str, Any]] = []
    since = now - window_days * 86400.0
    for cli, path, mtime, size in harvest(roots, home, since=since):
        key = str(path)
        if not is_new_or_grown(watermarks, key, mtime, size):
            continue
        outcome = _read_for_upload(path, size)
        if isinstance(outcome, str):
            # Skips are watermarked too: one core dump parked in the spool must
            # be reported once, not re-reported on every cron tick forever.
            skips.append({"source": key, "reason": outcome, "mtime": mtime, "size": size})
            continue
        text, redactions = outcome
        uploads.append(
            {
                "source": key,
                "cli": cli,
                "mtime": mtime,
                "size": size,
                "redactions": redactions,
                "relative_target": (
                    f"transcripts/{dev_short}/{_day(mtime)}/{target_name(host, cli, path)}"
                ),
                "_text": text,
            }
        )
    return uploads, skips


def notes_line(host: str, uploaded: int, redactions: int, skipped: int, now: float) -> str:
    return (
        f"- {_iso(now)} {host}: uploaded {uploaded} files "
        f"({redactions} redactions, {skipped} skipped) via transcript_uploader\n"
    )


def _append_notes(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)


def _git(dest: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(dest), *args],
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT,
        check=False,
    )


def _git_pull(dest: Path) -> None:
    """Rebase onto the other devs' uploads BEFORE this run writes its copies.

    Ordering matters: ``git pull --rebase`` refuses outright on a dirty working
    tree, so pulling after the copies land means the pull never runs, the local
    branch drifts from origin, and every subsequent push is rejected. Failure is
    tolerated with a warning — a laptop offline at :17, or a clone with no
    upstream yet, must still be able to bank its work locally.
    """
    pulled = _git(dest, "pull", "--rebase", "--quiet")
    if pulled.returncode:
        print(
            f"transcript-uploader: warning: git pull --rebase failed: "
            f"{(pulled.stderr or pulled.stdout).strip()}",
            file=sys.stderr,
        )


def _git_publish(dest: Path, dev_short: str, uploaded: int, host: str, *, push: bool) -> int:
    """add → commit-if-staged → push. Returns the process exit code."""
    subtree = f"transcripts/{dev_short}"
    added = _git(dest, "add", "--", subtree)
    if added.returncode:
        print(
            f"transcript-uploader: git add failed: {(added.stderr or added.stdout).strip()}",
            file=sys.stderr,
        )
        return 3
    staged = _git(dest, "diff", "--cached", "--quiet", "--", subtree)
    if staged.returncode:
        message = f"transcripts: {dev_short} {_day(time.time())} +{uploaded} files from {host}"
        committed = _git(dest, "commit", "--quiet", "-m", message, "--", subtree)
        if committed.returncode:
            print(
                f"transcript-uploader: git commit failed: "
                f"{(committed.stderr or committed.stdout).strip()}",
                file=sys.stderr,
            )
            return 3
        print(f"transcript-uploader: committed {message}")
    if not push:
        return 0
    pushed = _git(dest, "push", "--quiet")
    if pushed.returncode:
        # Terminal on purpose: auth/quota/non-fast-forward do not resolve by
        # being retried in a loop, and the commit is already safe locally.
        print(
            f"transcript-uploader: push failed: {(pushed.stderr or pushed.stdout).strip()}",
            file=sys.stderr,
        )
        return 3
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--employee", required=True, help="employee id, e.g. emp_alice")
    parser.add_argument(
        "--dest", default="~/ai-transcripts", help="ai-transcripts clone (default ~/ai-transcripts)"
    )
    parser.add_argument("--window-days", type=float, default=3, help="upload window (days)")
    parser.add_argument(
        "--state",
        default="~/.transcript-uploader-state.json",
        help="per-file mtime+size watermark file",
    )
    parser.add_argument("--no-push", action="store_true", help="commit locally, never push")
    parser.add_argument(
        "--print",
        dest="print_json",
        action="store_true",
        help="dry run: print the plan as JSON, write nothing",
    )
    parser.add_argument(
        "--print-text",
        dest="print_text",
        action="store_true",
        help=(
            "with --print: include each file's full REDACTED text — the exact "
            "bytes an upload would push — so the owner can self-audit the payload"
        ),
    )
    parser.add_argument("--roots", help='JSON override, e.g. [{"cli":"claude","glob":"..."}]')
    args = parser.parse_args(argv)

    if not _EMPLOYEE.fullmatch(args.employee):
        print(
            f"transcript-uploader: --employee must look like emp_owner, got {args.employee!r}",
            file=sys.stderr,
        )
        return 2
    dev_short = args.employee.removeprefix("emp_")
    try:
        roots = parse_roots(args.roots)
    except ValueError as exc:
        print(f"transcript-uploader: bad --roots: {exc}", file=sys.stderr)
        return 2

    opted_out_since = _opt_out_since(Path.home())
    if opted_out_since is not None:
        # OFF means off: nothing is globbed, opened, copied, or pushed.
        if args.print_json:
            print(
                json.dumps(
                    {
                        "schema": SCHEMA,
                        "employee_id": args.employee,
                        "opted_out": True,
                        "opted_out_since": opted_out_since,
                        "would_upload": [],
                        "skipped": [],
                    },
                    indent=1,
                    sort_keys=True,
                )
            )
            return 0
        print(
            f"transcript-uploader: telemetry is OFF (since {opted_out_since}) — "
            "nothing scanned, nothing uploaded. `telemetry_ctl.py on` re-enables."
        )
        return 0

    now = time.time()
    host = socket.gethostname().split(".")[0]
    state_file = Path(args.state).expanduser()
    watermarks = load_state(state_file)
    uploads, skips = plan_uploads(
        roots=roots,
        home=Path.home(),
        dev_short=dev_short,
        host=host,
        window_days=args.window_days,
        watermarks=watermarks,
        now=now,
    )
    redactions = sum(int(item["redactions"]) for item in uploads)
    dest = Path(args.dest).expanduser()

    if args.print_json:
        print(
            json.dumps(
                {
                    "schema": SCHEMA,
                    "employee_id": args.employee,
                    "dev": dev_short,
                    "host": host,
                    "dest": str(dest),
                    "generated_at": _iso(now),
                    "window_days": args.window_days,
                    "would_upload": [
                        # --print-text exposes the exact redacted bytes an upload
                        # would push — the self-audit is not honest without them
                        # (cross-lineage review finding 5).
                        {
                            **{key: value for key, value in item.items() if key != "_text"},
                            **({"text": item["_text"]} if args.print_text else {}),
                        }
                        for item in uploads
                    ],
                    "skipped": skips,
                    "redactions": redactions,
                    "push": not args.no_push,
                },
                indent=1,
                sort_keys=True,
            )
        )
        return 0

    if not dest.is_dir() or not (dest / ".git").exists():
        print(
            f"transcript-uploader: {dest} is not an ai-transcripts clone. Clone it first:\n"
            f"  gh repo clone {CLONE_REPO} {dest}",
            file=sys.stderr,
        )
        return 2

    # Mid-run kill switch: an owner who flips it while the harvest was running
    # gets everything already read DISCARDED before a byte lands in the clone
    # (cross-lineage review finding 2).
    if _opt_out_since(Path.home()) is not None:
        print(
            "transcript-uploader: telemetry switched OFF mid-run — harvested data "
            "discarded, nothing written or pushed."
        )
        return 0

    _git_pull(dest)
    written = 0
    for item in uploads:
        if _write_into_clone(dest, str(item["relative_target"]), str(item["_text"]), append=False):
            written += 1
        else:
            print(
                f"transcript-uploader: refusing unsafe target {item['relative_target']} "
                "(symlink or escape in the pulled clone)",
                file=sys.stderr,
            )
    if uploads or skips:
        # NOTES reflects what actually landed (written), not what was attempted.
        _write_into_clone(
            dest,
            f"transcripts/{dev_short}/NOTES.md",
            notes_line(host, written, redactions, len(skips), now),
            append=True,
        )
    for item in (*uploads, *skips):
        watermarks[str(item["source"])] = {
            "mtime": float(item["mtime"]),
            "size": float(item["size"]),
        }
    # Watermarks are banked BEFORE git runs: the copies are on disk, and a
    # failed push leaves a local commit the next run pushes — re-copying the
    # same bytes to prove that is pure churn.
    save_state(state_file, {key: value for key, value in watermarks.items() if os.path.exists(key)})
    print(
        f"transcript-uploader: {written} file(s), {redactions} redaction(s), {len(skips)} skipped"
    )
    for skip in skips:
        print(f"transcript-uploader: skipped {skip['source']}: {skip['reason']}", file=sys.stderr)
    # Last kill-switch check before egress: copies already in the local clone
    # stay local (the owner's own disk) — nothing is committed or pushed.
    if _opt_out_since(Path.home()) is not None:
        print(
            "transcript-uploader: telemetry switched OFF mid-run — local copies kept "
            "in your clone but NOT committed or pushed."
        )
        return 0
    return _git_publish(dest, dev_short, written, host, push=not args.no_push)


if __name__ == "__main__":
    raise SystemExit(main())
