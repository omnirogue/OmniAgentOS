#!/usr/bin/env python3
"""Measure what a change to :mod:`omniagentos.orchestrator.approvals` costs.

WHY THIS FILE EXISTS. The unrecognised-action floor (LS-022 / H4) is an INVERTED
rule: on the money / customer / production surface an action must be RECOGNISED to
auto-approve. Inverted rules trade a false PARK (visible, recoverable, one human
decision) for a false APPROVE (invisible, irreversible), so the only honest way to
draw the boundary is to measure both directions against real traffic. The first
version of that floor was justified by a number produced by a throwaway script,
and a number nobody can re-derive is not evidence. This is that script, committed.

THE TWO NUMBERS THAT MATTER, in this order:

    un-parked     requests that PARK on the base tree and APPROVE on the head tree.
                  This must be ZERO. Anything else is a safety regression and no
                  false-park saving can pay for it.
    newly parked  requests that APPROVE on the base tree and PARK on the head tree.
                  This is the price. It is paid by a human clicking approve.

USAGE

    # one shot: harvest, measure base and head, print the delta
    python scripts/approval_corpus_probe.py ab --base-rev origin/main

    # or the three steps separately (a corpus is reusable and hashed)
    python scripts/approval_corpus_probe.py extract --out /tmp/corpus.jsonl
    python scripts/approval_corpus_probe.py measure --corpus /tmp/corpus.jsonl --out /tmp/head.json
    python scripts/approval_corpus_probe.py compare /tmp/base.json /tmp/head.json

THE CORPUS IS NOT COMMITTED, ON PURPOSE. It is ~34k real shell commands harvested
from this operator's own agent transcripts; they carry paths, hostnames and
occasionally credentials-shaped operands. What is committed is the harvester, so
the population is reproducible, plus a manifest (roots, file count, distinct
count, sha256 of the sorted corpus) written next to every corpus so a reported
number can be tied to the exact population it came from.

THE IMPORT TRAP, which has already produced one false measurement on this estate:
this repo is checked out in ~90 worktrees, ``PYTHONPATH`` on this machine pins the
SERVING checkout, and a worktree's ``.venv`` adds its own tree only at the END of
``sys.path``. A probe run from the wrong cwd silently measures the serving tree
and reports "no change". ``measure`` therefore pins the tree at ``sys.path[0]``
and then ASSERTS that the imported module actually came from it, refusing to
produce a number it cannot attribute.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

DEFAULT_TRANSCRIPT_GLOBS: tuple[str, ...] = (
    "~/.claude/projects",
    "~/.claude-account-*/projects",
    "~/.claude-account*/projects",
)
_TRIGGER_RE = re.compile(r"trigger:\s*([a-z0-9-]+)")


# ---------------------------------------------------------------------------
# extract
# ---------------------------------------------------------------------------
def _transcript_roots(patterns: Iterable[str]) -> list[Path]:
    roots: list[Path] = []
    seen: set[Path] = set()
    for pattern in patterns:
        expanded = Path(pattern).expanduser()
        candidates = (
            sorted(Path(expanded.anchor or ".").glob(str(expanded).lstrip("/")))
            if "*" in str(expanded)
            else [expanded]
        )
        for candidate in candidates:
            resolved = candidate.resolve()
            if resolved.is_dir() and resolved not in seen:
                seen.add(resolved)
                roots.append(resolved)
    return roots


def _bash_commands(path: Path) -> Iterator[str]:
    """Every Bash ``tool_use`` command recorded in one transcript file."""
    try:
        handle = path.open(encoding="utf-8", errors="replace")
    except OSError:
        return
    with handle:
        for line in handle:
            line = line.strip()
            if not line or '"Bash"' not in line:
                continue
            try:
                record = json.loads(line)
            except (ValueError, TypeError):
                continue
            message = record.get("message")
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                if block.get("name") != "Bash":
                    continue
                payload = block.get("input")
                if not isinstance(payload, dict):
                    continue
                command = payload.get("command")
                if isinstance(command, str) and command.strip():
                    yield command


def _tool_calls(path: Path) -> Iterator[dict[str, Any]]:
    """Every NON-Bash ``tool_use`` call recorded in one transcript file.

    WHY THIS EXISTS SEPARATELY FROM :func:`_bash_commands`. A Bash request is by
    construction never a TOOL-LABEL request -- ``_is_tool_label_request`` excludes
    anything carrying a ``command`` key -- so the Bash corpus cannot price a change
    to the tool-name rule at all. It reports a flat zero and reads exactly like
    "this change is free". GCV-01 lived on the path the Bash corpus cannot see, so
    that path needs its own population.
    """
    try:
        handle = path.open(encoding="utf-8", errors="replace")
    except OSError:
        return
    with handle:
        for line in handle:
            line = line.strip()
            if not line or '"tool_use"' not in line:
                continue
            try:
                record = json.loads(line)
            except (ValueError, TypeError):
                continue
            message = record.get("message")
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                name = block.get("name")
                payload = block.get("input")
                if not isinstance(name, str) or not name or name == "Bash":
                    continue
                if not isinstance(payload, dict):
                    continue
                yield {"tool_name": name, "tool_input": payload}


def _corpus_key(record: dict[str, Any]) -> str:
    """The stable identity of one corpus entry, and its dedupe key."""
    command = record.get("command")
    if isinstance(command, str):
        return command
    return json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def cmd_extract(args: argparse.Namespace) -> int:
    roots = _transcript_roots(args.transcripts or DEFAULT_TRANSCRIPT_GLOBS)
    if not roots:
        print("no transcript roots matched; nothing to extract", file=sys.stderr)
        return 2
    kind = getattr(args, "kind", None) or "bash"
    files = 0
    entries: dict[str, dict[str, Any]] = {}
    for root in roots:
        for path in root.rglob("*.jsonl"):
            files += 1
            if args.project_filter and args.project_filter not in str(path):
                continue
            harvested: Iterable[dict[str, Any]] = (
                ({"command": command} for command in _bash_commands(path))
                if kind == "bash"
                else _tool_calls(path)
            )
            for record in harvested:
                entries[_corpus_key(record)] = record
    ordered = sorted(entries)
    out = Path(args.out).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as handle:
        for key in ordered:
            handle.write(json.dumps(entries[key], ensure_ascii=False) + "\n")
    digest = hashlib.sha256("\n".join(ordered).encode("utf-8")).hexdigest()
    manifest = {
        "roots": [str(root) for root in roots],
        "kind": kind,
        "project_filter": args.project_filter,
        "transcript_files": files,
        "distinct_commands": len(ordered),
        "corpus_sha256": digest,
    }
    Path(str(out) + ".manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest, indent=2))
    return 0


# ---------------------------------------------------------------------------
# measure
# ---------------------------------------------------------------------------
def _pin_tree(tree: Path) -> Any:
    """Import ``omniagentos`` from ``tree`` or die loudly.

    Never "fall back" to whatever happens to be importable: a measurement of the
    wrong tree reports "no change" and reads exactly like a clean result.
    """
    tree = tree.resolve()
    sys.path.insert(0, str(tree))
    import omniagentos  # noqa: PLC0415 -- the pin has to happen before the import

    origin = Path(omniagentos.__file__ or "").resolve()
    if tree not in origin.parents:
        raise SystemExit(
            f"REFUSING TO MEASURE: asked for the tree at {tree} but 'omniagentos' "
            f"imported from {origin}. PYTHONPATH={os.environ.get('PYTHONPATH', '')!r}. "
            "Re-run with PYTHONPATH cleared and cwd inside the tree."
        )
    return origin


def _load_corpus(path: Path) -> list[str | dict[str, Any]]:
    """Corpus entries: a Bash command STRING, or a ``{tool_name, tool_input}`` record."""
    commands: list[str | dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(record, dict):
                continue
            command = record.get("command")
            if isinstance(command, str) and command.strip():
                commands.append(command)
            elif isinstance(record.get("tool_name"), str) and record["tool_name"].strip():
                commands.append(record)
    return commands


_WORKER_STATE: dict[str, Any] = {}


def _worker_init(tree: str, action_class: str) -> None:
    _pin_tree(Path(tree))
    from omniagentos.orchestrator.approvals import resolve_approval  # noqa: PLC0415
    from omniagentos.orchestrator.contracts import ApprovalRequest  # noqa: PLC0415

    _WORKER_STATE["resolve"] = resolve_approval
    _WORKER_STATE["request"] = ApprovalRequest
    _WORKER_STATE["action_class"] = action_class


# The keys ``api/routes/sessions.py::_format_proposed_action`` renders a tool call
# by, in its order. Kept in step with that function on purpose: a probe that
# renders the label differently from the live route measures a request that never
# happens.
_LABEL_KEYS: tuple[str, ...] = (
    "command",
    "path",
    "file_path",
    "old_string",
    "new_string",
    "url",
    "query",
)


def _proposed_action(tool_name: str, tool_input: dict[str, Any]) -> str:
    """``_format_proposed_action`` for the probe, including its 400-char truncation."""
    for key in _LABEL_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            text = value.strip()
            if len(text) > 400:
                text = text[:397] + "..."
            return text if key == "command" else f"{tool_name} {key}={text}"
    try:
        compact = json.dumps(tool_input, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        compact = ""
    if compact and compact not in ("{}", "null"):
        if len(compact) > 400:
            compact = compact[:397] + "..."
        return f"{tool_name} {compact}"
    return tool_name


def _judge(entry: str | dict[str, Any]) -> tuple[str, bool, str]:
    resolve = _WORKER_STATE["resolve"]
    request_cls = _WORKER_STATE["request"]
    if isinstance(entry, dict):
        key = _corpus_key(entry)
        tool_name = str(entry.get("tool_name") or "")
        tool_input = entry.get("tool_input")
        tool_input = tool_input if isinstance(tool_input, dict) else {}
        proposed_action = _proposed_action(tool_name, tool_input)
    else:
        key = entry
        tool_name, tool_input, proposed_action = "Bash", {"command": entry}, entry
    try:
        decision = resolve(
            request_cls(
                proposed_action=proposed_action,
                action_class=_WORKER_STATE["action_class"],
                tool_name=tool_name,
                tool_input=tool_input,
            )
        )
        approved, reason = bool(decision.approved), str(decision.reason or "")
    except Exception as exc:  # noqa: BLE001 -- a crash is a result, not a skip
        approved, reason = False, f"EXCEPTION: {type(exc).__name__}: {exc}"
    match = _TRIGGER_RE.search(reason)
    return key, approved, match.group(1) if match else ""


def cmd_measure(args: argparse.Namespace) -> int:
    tree = Path(args.tree or Path(__file__).resolve().parent.parent)
    origin = _pin_tree(tree)

    corpus_path = Path(args.corpus).expanduser()
    commands = _load_corpus(corpus_path)
    jobs = args.jobs if args.jobs and args.jobs > 0 else (os.cpu_count() or 1)
    results: dict[str, dict[str, Any]] = {}
    if jobs > 1 and len(commands) > 512:
        import multiprocessing as mp  # noqa: PLC0415

        with mp.get_context("fork").Pool(
            processes=jobs,
            initializer=_worker_init,
            initargs=(str(tree), args.action_class),
        ) as pool:
            for key, approved, trigger in pool.imap_unordered(_judge, commands, chunksize=256):
                results[key] = {"approved": approved, "trigger": trigger}
    else:
        _worker_init(str(tree), args.action_class)
        for entry in commands:
            key, approved, trigger = _judge(entry)
            results[key] = {"approved": approved, "trigger": trigger}
    count = len(results)
    parked = sum(1 for entry in results.values() if not entry["approved"])
    payload: dict[str, Any] = {
        "tree": str(tree.resolve()),
        "module": str(origin),
        "corpus": str(corpus_path),
        "corpus_sha256": hashlib.sha256("\n".join(sorted(results)).encode("utf-8")).hexdigest(),
        "count": count,
        "parked": parked,
        "results": results,
    }
    out = Path(args.out).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload) + "\n")
    print(
        f"measured {count} commands from {origin}: "
        f"parked {parked} ({100.0 * parked / max(count, 1):.2f}%)"
    )
    return 0


# ---------------------------------------------------------------------------
# compare
# ---------------------------------------------------------------------------
def _compare(base: dict[str, Any], head: dict[str, Any], samples: int) -> dict[str, Any]:
    base_results: dict[str, dict[str, Any]] = base["results"]
    head_results: dict[str, dict[str, Any]] = head["results"]
    shared = sorted(set(base_results) & set(head_results))
    newly_parked = [
        command
        for command in shared
        if base_results[command]["approved"] and not head_results[command]["approved"]
    ]
    un_parked = [
        command
        for command in shared
        if not base_results[command]["approved"] and head_results[command]["approved"]
    ]
    triggers = Counter(head_results[command]["trigger"] for command in newly_parked)
    return {
        "corpus_size": len(shared),
        "base_parked": sum(1 for command in shared if not base_results[command]["approved"]),
        "head_parked": sum(1 for command in shared if not head_results[command]["approved"]),
        "newly_parked": len(newly_parked),
        "newly_parked_pct": round(100.0 * len(newly_parked) / max(len(shared), 1), 3),
        "un_parked": len(un_parked),
        "newly_parked_triggers": dict(triggers.most_common()),
        "newly_parked_samples": newly_parked[:samples],
        "un_parked_samples": un_parked[:samples],
    }


def cmd_compare(args: argparse.Namespace) -> int:
    base = json.loads(Path(args.base).expanduser().read_text())
    head = json.loads(Path(args.head).expanduser().read_text())
    report = _compare(base, head, args.samples)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    # An un-park is a safety regression: make it a non-zero exit so a harness that
    # only checks the status code still catches it.
    return 1 if report["un_parked"] else 0


# ---------------------------------------------------------------------------
# ab -- harvest, measure both trees, compare, in one command
# ---------------------------------------------------------------------------
def _export_rev(repo: Path, rev: str, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    archive = subprocess.run(  # noqa: S603
        ["git", "-C", str(repo), "archive", rev, "omniagentos"],
        check=True,
        capture_output=True,
    )
    subprocess.run(["tar", "-x", "-C", str(dest)], input=archive.stdout, check=True)  # noqa: S603


def _run_measure(tree: Path, corpus: Path, out: Path, jobs: int = 0) -> None:
    env = dict(os.environ)
    # Both halves of the import trap: the machine-wide PYTHONPATH pin and cwd.
    env.pop("PYTHONPATH", None)
    subprocess.run(  # noqa: S603
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "measure",
            "--tree",
            str(tree),
            "--corpus",
            str(corpus),
            "--out",
            str(out),
            "--jobs",
            str(jobs),
        ],
        check=True,
        cwd=str(tree),
        env=env,
    )


def cmd_ab(args: argparse.Namespace) -> int:
    repo = Path(args.tree or Path(__file__).resolve().parent.parent).resolve()
    workdir = (
        Path(args.workdir).expanduser()
        if args.workdir
        else Path(tempfile.mkdtemp(prefix="approval-ab-"))
    )
    workdir.mkdir(parents=True, exist_ok=True)
    corpus = Path(args.corpus).expanduser() if args.corpus else workdir / "corpus.jsonl"
    if not corpus.exists():
        extract_args = argparse.Namespace(
            transcripts=args.transcripts,
            project_filter=args.project_filter,
            kind=getattr(args, "kind", "bash"),
            out=str(corpus),
        )
        if cmd_extract(extract_args) != 0:
            return 2
    base_tree = workdir / "base-tree"
    if not (base_tree / "omniagentos").exists():
        _export_rev(repo, args.base_rev, base_tree)
    base_out, head_out = workdir / "base.json", workdir / "head.json"
    _run_measure(base_tree, corpus, base_out, args.jobs)
    _run_measure(repo, corpus, head_out, args.jobs)
    print(f"\nbase = {args.base_rev} ({base_tree})\nhead = {repo}\n")
    status = cmd_compare(argparse.Namespace(base=base_out, head=head_out, samples=args.samples))
    if not args.keep and not args.workdir:
        shutil.rmtree(workdir, ignore_errors=True)
    else:
        print(f"\nartifacts kept in {workdir}")
    return status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)

    extract = sub.add_parser("extract", help="harvest a corpus from agent transcripts")
    extract.add_argument("--out", required=True)
    extract.add_argument(
        "--kind",
        choices=("bash", "tools"),
        default="bash",
        help="bash: distinct Bash commands. tools: distinct non-Bash tool calls, "
        "the TOOL-LABEL population the bash corpus is structurally blind to",
    )
    extract.add_argument(
        "--transcripts", action="append", help="transcript root (repeatable, globs allowed)"
    )
    extract.add_argument(
        "--project-filter", default=None, help="only files whose path contains this substring"
    )
    extract.set_defaults(func=cmd_extract)

    measure = sub.add_parser("measure", help="run resolve_approval over a corpus")
    measure.add_argument("--corpus", required=True)
    measure.add_argument("--out", required=True)
    measure.add_argument("--tree", default=None, help="repo tree to import omniagentos from")
    measure.add_argument("--action-class", default="consequential")
    measure.add_argument(
        "--jobs", type=int, default=0, help="worker processes (default: all cores)"
    )
    measure.set_defaults(func=cmd_measure)

    compare = sub.add_parser("compare", help="diff two measure outputs")
    compare.add_argument("base")
    compare.add_argument("head")
    compare.add_argument("--samples", type=int, default=25)
    compare.set_defaults(func=cmd_compare)

    ab = sub.add_parser("ab", help="harvest + measure base and head + compare")
    ab.add_argument("--base-rev", required=True)
    ab.add_argument("--tree", default=None)
    ab.add_argument("--corpus", default=None, help="reuse an existing corpus instead of harvesting")
    ab.add_argument("--workdir", default=None)
    ab.add_argument("--transcripts", action="append")
    ab.add_argument("--project-filter", default=None)
    ab.add_argument("--kind", choices=("bash", "tools"), default="bash")
    ab.add_argument("--samples", type=int, default=25)
    ab.add_argument("--jobs", type=int, default=0)
    ab.add_argument("--keep", action="store_true")
    ab.set_defaults(func=cmd_ab)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
