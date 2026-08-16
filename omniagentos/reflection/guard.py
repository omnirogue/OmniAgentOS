"""One resolution policy for every reflection write, enforced at the writer.

``kinds.py`` answers *which path does this proposal name*.  This module answers
the two questions that come after it, and it is the only place either is
answered:

1. **What file does that path actually mean?**  and
2. **May anything be written there at all?**

Both used to be answered twice, differently.  ``validate.is_hard_stop`` decided
(1) with :func:`posixpath.normpath` — a purely textual collapse that cannot see
a symlink — while ``apply.apply_yaml_change`` and ``apply.write_document_change``
decided it with :meth:`pathlib.Path.resolve`, which follows every link.  So with
``docs/configs_link`` pointing at ``configs``, the validator was shown
``docs/configs_link/governance.yaml`` (harmless, ``ok=True``) and the writer
opened ``configs/governance.yaml``.  The check and the write were looking at two
different files.

And (2) was answered only inside ``validate.validate_proposal``, which
``POST /reflection/{id}/approve`` never calls — it invokes
``apply.apply_proposal`` directly — so on the human-approval path the hard-stop
set was not enforced at all.

The fix is structural rather than additive: a writer does not compute its own
absolute path and then consult a checker.  It **obtains** its absolute path from
:func:`authorise_write`, which refuses before it returns one.  There is no
second way to get a path, so there is no path that skipped the check, and the
path that was checked is by construction the path that gets opened.

:func:`authorise_write` refuses on five grounds, in order, and every one of them
is a refusal to *return a path* rather than a flag some caller may ignore:

* an absolute, drive-ish, ``~``-rooted or NUL-bearing spelling — refused before
  the join, because ``Path(root) / "/etc/passwd"`` is ``/etc/passwd``: pathlib
  discards the left operand when the right one is absolute;
* the hard-stop rules against the path **as declared**, textually normalised, so
  a link *from* a protected name cannot launder it;
* filesystem truth — the path is anchored to the repo root by inode
  (:mod:`omniagentos.path_containment`), which follows symlinks and supports a
  leaf that does not exist yet; anything that resolves outside the repo, or
  whose containment cannot be proved, is refused;
* the hard-stop rules **again**, against the resolved path, which is what closes
  the symlink bypass; and
* an optional per-lane confinement (the YAML lane may only write under
  ``configs/``), likewise decided on filesystem truth.

Declared-and-resolved are both checked on purpose: neither implies the other.
A link *to* a protected file is caught by the resolved check; a link *from* a
protected name to somewhere harmless is caught by the declared check.

This module deliberately owns the hard-stop rule set itself, so that the rules
and the resolution policy cannot be moved apart.  ``validate.py`` re-exports
them for its existing importers; it does not keep a copy.
"""

from __future__ import annotations

import json
import os
import posixpath
import stat
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

from omniagentos.path_containment import inode_relative_parts_anchored

__all__ = [
    "CONTENT_KIND_SUFFIXES",
    "GOVERNANCE_PATH",
    "MAX_PAYLOAD_DEPTH",
    "POLICY_DIR",
    "PROTECTED_NAMES",
    "PROTECTED_PREFIXES",
    "REJECTED_WORDS",
    "RESTRICTED_KEY_WORDS",
    "AuthorisedWrite",
    "ContentKind",
    "WriteRefused",
    "authorise_write",
    "coerce_payload",
    "examine_payload",
    "introduced_key_paths",
    "is_hard_stop",
    "normalise_target_path",
    "reflection_repo_root",
]

# Hard-stop restricted files and key patterns
REJECTED_WORDS = ["external", "deploy", "destructive"]
GOVERNANCE_PATH = "configs/governance.yaml"
POLICY_DIR = "omniagentos/policy"

# Surfaces that EXECUTE, or that decide what may execute. A reflection proposal
# appends text to its target, so any of these is arbitrary-code-execution on the
# next run: the gate scripts are run by the merge gate, CI workflows by the
# runner, git hooks by every commit, and AGENTS.md/CLAUDE.md are the
# instructions agents act on — for an LLM-driven system the prompt IS the policy.
PROTECTED_PREFIXES = (
    ".git/",
    ".github/workflows/",
    "devtasks/",
    "omniagentos/gates/",
    "omniagentos/policy/",
    "ops/launchd/",
    "scripts/",
)
PROTECTED_NAMES = frozenset(
    {"agents.md", "claude.md", "makefile", "pyproject.toml", "conftest.py"}
)


RESTRICTED_KEY_WORDS = [
    "budget",
    "credential",
    "password",
    "token",
    "secret",
    "auth",
    "api_key",
    "accounts",
    "connectors",
]


class WriteRefused(ValueError):
    """A reflection proposal may not be written to the target it named.

    Deriving from :class:`ValueError` is deliberate: every existing caller of
    the writers already treats ``ValueError`` as a refusal (``apply_proposal``
    turns it into a failed result, the tests assert ``pytest.raises(ValueError)``),
    so the guard cannot be introduced and then quietly not caught anywhere.
    """


class ContentKind(Enum):
    """What a writer is about to put on disk.

    Declaring this is mandatory, and that is the point.  A writer that must say
    what it is writing cannot write prose into a config file or a config into a
    Python module, because the answer is checked against the target's type.
    """

    CONFIG = "config"
    DOCUMENT = "document"


#: The file types each content kind may land in.
#:
#: A reflection proposal chooses BOTH its target and its content, so without
#: this the two are independent and every cross-product is reachable: a
#: ``lesson`` (prose, appended) naming ``configs/swarm.yaml`` corrupted a config,
#: naming ``omniagentos/reflection/__init__.py`` appended executable code to a
#: module every import runs, and naming ``sitecustomize.py`` created a file
#: Python imports automatically in any process started from the repo. None of
#: those are path escapes — each target is inside the repo and outside the
#: protected set — so no amount of path checking can see them. They are type
#: errors, and this is the type.
#: ``.txt`` is DELIBERATELY absent. It was here, and it was wrong: a document
#: write's content is unexamined by design, so admitting ``.txt`` gave
#: ``requirements.txt`` — a package manifest, in neither PROTECTED_NAMES nor
#: PROTECTED_PREFIXES — exactly the scrutiny a lesson note gets. Nothing in this
#: repo runs ``pip install -r`` today, so that was not a live chain; it was a
#: manifest-shaped file accepting arbitrary appended content, which is enough.
#: The set is narrow on purpose. A producer that needs a suffix back will say so
#: in a refusal message naming itself, and widening then is a deliberate act;
#: a silent bypass is not recoverable the same way.
CONTENT_KIND_SUFFIXES: Mapping[ContentKind, tuple[str, ...]] = {
    ContentKind.CONFIG: (".yaml", ".yml"),
    ContentKind.DOCUMENT: (".md", ".markdown", ".rst"),
}

#: Proof that an :class:`AuthorisedWrite` came out of :func:`authorise_write`.
#:
#: The whole design rests on "you cannot hold a writable handle to an unexamined
#: write", and a plain dataclass with a public constructor lets a future
#: refactor hand-build one in a single line — not reachable from the proposal
#: surface (a JSON row cannot construct a Python object), but the invariant
#: should not depend on nobody trying. Reaching for this sentinel by name is
#: explicit enough that it cannot happen by accident.
_AUTHORISED = object()

#: How deep a proposed payload may nest before it is refused unexamined.
MAX_PAYLOAD_DEPTH = 32


@dataclass(frozen=True)
class AuthorisedWrite:
    """A write that has passed every rule, and the only way to perform it.

    This is deliberately not "a path plus a checker you should remember to
    call".  Obtaining one requires supplying the payload as well as the target,
    :meth:`write` is the only method that lands bytes, and the absolute path is
    private — so *a writer cannot hold a writable handle to an unexamined
    write*.  The previous shape returned a bare ``Path`` next to a path check,
    and a bare ``Path`` accepts anything: that is precisely how a whole-file
    replacement carrying ``api_key`` and ``budget`` reached disk through a rule
    set that only ever inspected the target's own ``key``.

    ``canonical`` — not ``declared`` — is what :meth:`write` returns, because it
    is what the rest of the pipeline must use.  ``git add`` on a symlink-spelled
    path fails with *"pathspec is beyond a symbolic link"*, and
    ``git_commit_change`` swallows that exception into a log line, so a
    legitimately permitted write silently never reached the branch.
    """

    declared: str
    canonical: str
    content_kind: ContentKind
    key: str | None
    _path: Path = field(repr=False)
    _token: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._token is not _AUTHORISED:
            raise WriteRefused(
                "AuthorisedWrite may only be constructed by authorise_write(); "
                "a hand-built handle is an unexamined write by definition"
            )

    def exists(self) -> bool:
        """Whether the target file is already there.  Reading is not writing."""
        return self._path.is_file()

    def read_text(self) -> str:
        """The current contents, so a writer can merge or append into them."""
        return self._path.read_text(encoding="utf-8")

    def write(self, content: str) -> str:
        """Land ``content`` and return the canonical repo-relative path.

        THE ONLY BYTE-LANDING SITE FOR PROPOSAL CONTENT IN THIS PACKAGE. The
        final structural check lives here rather than at authorisation time
        because this is the first moment the actual bytes exist: a config lane
        that merged its payload into the existing document must still produce a
        YAML mapping, and if it does not, something between authorisation and
        here has gone wrong and the file should not be truncated to find out.
        """
        if not isinstance(content, str):
            raise WriteRefused(
                f"a write must land text, got {type(content).__name__} (fail closed)"
            )
        if self.content_kind is ContentKind.CONFIG:
            try:
                parsed = yaml.safe_load(content)
            except Exception as exc:
                raise WriteRefused(f"config write does not parse as YAML: {exc}") from None
            if not isinstance(parsed, dict):
                raise WriteRefused(
                    "a config write must land a YAML mapping, not "
                    f"{type(parsed).__name__} (fail closed)"
                )
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(content, encoding="utf-8")
        return self.canonical


def coerce_payload(content_kind: ContentKind, raw: Any) -> Any:
    """The value the WRITER will actually apply, read once for every stage.

    ``apply_proposal`` JSON-parses a config payload and stringifies a document
    payload before handing it on.  The validator has to examine the same object
    the writer applies, or it is once again ruling on something other than what
    lands — the identical mistake as reading a different ``target`` key.
    """
    if content_kind is ContentKind.DOCUMENT:
        return raw if isinstance(raw, str) else str(raw)
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            return raw
    return raw


def introduced_key_paths(key: str | None, payload: Any) -> list[str]:
    """Every config key path this write CREATES OR SETS, dotted.

    Not the resulting document's key paths — only the ones the write is
    responsible for.  Scanning the whole merged result would refuse any edit to
    a file that legitimately already contains a restricted key (``configs/
    connectors.yaml`` holds auth settings by design), which is a false positive
    that would get the rule switched off.

    The declared ``key`` is element zero, so this is a strict superset of the
    single key :func:`is_hard_stop` already inspects. What it adds is the nested
    case: ``{"key": "safe", "proposed": {"api_key": "stolen"}}`` sets
    ``safe.api_key``, which no rule about the declared key can see.
    """
    out: list[str] = []
    prefix = key or ""
    if prefix:
        out.append(prefix)

    def walk(node: Any, path: str, depth: int) -> None:
        if depth > MAX_PAYLOAD_DEPTH:
            raise WriteRefused(
                f"proposed payload nests deeper than {MAX_PAYLOAD_DEPTH} levels; "
                "refusing to write a structure this guard cannot fully examine"
            )
        if isinstance(node, Mapping):
            for raw_key, value in node.items():
                child = f"{path}.{raw_key}" if path else str(raw_key)
                out.append(child)
                walk(value, child, depth + 1)
        elif isinstance(node, Sequence) and not isinstance(node, str | bytes):
            for index, value in enumerate(node):
                child = f"{path}.{index}" if path else str(index)
                walk(value, child, depth + 1)

    walk(payload, prefix, 0)
    return out


def examine_payload(content_kind: ContentKind, key: str | None, payload: Any) -> None:
    """Apply the rules to the CONTENT.  Raises :class:`WriteRefused`.

    For :attr:`ContentKind.CONFIG` this is two rules:

    1. **A config write must name the key it changes.** A keyless write replaces
       the entire file, and a rule set written about *named* keys structurally
       cannot see what a bulk payload contains — that is how
       ``{"api_key": "stolen", "budget": 999999}`` landed intact. It also makes
       ``fable_gate``'s ``PROTECTED_KEY_MARKERS`` check vacuous (it inspects
       ``target["key"]``, which is absent) and leaves a human reviewer nothing
       to review. The alternatives are worse: scanning a bulk payload's keys
       still leaves unbounded blast radius in the shape of the operation, and
       requiring an explicit key list is the same restriction with more
       machinery. Note the validator ALREADY refused this for an existing
       target; refusing it here is the writer agreeing with the validator, not
       new policy.
    2. **No key path the write introduces may be restricted**, checked over the
       payload recursively, which is what catches the nested-value form.

    For :attr:`ContentKind.DOCUMENT` the payload is prose and is DELIBERATELY
    unexamined beyond being text: a lesson *is* content, and there is no rule
    set in this system about English. The protection for documents is entirely
    positional — which file may receive prose — which is why the content-kind
    and mode rules in :func:`authorise_write` matter so much more here.
    """
    if content_kind is ContentKind.DOCUMENT:
        if not isinstance(payload, str):
            raise WriteRefused(
                f"a document write must be text, got {type(payload).__name__} (fail closed)"
            )
        return

    if not key:
        raise WriteRefused(
            "a config write must name the key it changes; a keyless write "
            "replaces the whole file, so no rule about named keys can see what "
            "it carries (fail closed)"
        )

    for key_path in introduced_key_paths(key, payload):
        folded = _fold(key_path)
        for word in RESTRICTED_KEY_WORDS:
            if word in folded:
                raise WriteRefused(
                    f"config write introduces key {key_path!r}, which touches a "
                    f"restricted resource ({word!r})"
                )


def reflection_repo_root() -> Path:
    """The repository root every reflection stage resolves against.

    A second divergence, smaller than the symlink one but the same shape: the
    writers honoured ``OMNIAGENTOS_HOME`` while the validator resolved relative
    paths against the process CWD, so the two could be checking and writing in
    different trees.  One reader, so they cannot.
    """
    home = os.environ.get("OMNIAGENTOS_HOME")
    return Path(home) if home else Path(__file__).resolve().parents[2]


def _fold(text: str) -> str:
    """Compatibility-normalise and case-fold, so lookalikes cannot evade a rule.

    ``NFKC`` collapses the compatibility forms — fullwidth ``／`` is a solidus,
    fullwidth ``ＡＧＥＮＴＳ`` is ``AGENTS`` — and ``casefold`` is the unicode-aware
    form of ``lower`` (it maps ``K`` U+212A and ``ẞ`` the way a case-insensitive
    filesystem does; APFS is case-insensitive by default).  Both directions only
    ever make MORE spellings match a protected literal, never fewer: the
    literals are plain ASCII and therefore fixed points of both operations.
    """
    return unicodedata.normalize("NFKC", text).casefold()


def normalise_target_path(file_path: str) -> str:
    """Collapse a target path to the form the hard-stop rules are written against.

    ``docs/../scripts/merge-gate.sh``, ``./scripts/merge-gate.sh``,
    ``scripts//merge-gate.sh`` and ``SCRIPTS/MERGE-GATE.SH`` all denote the same
    file; comparing raw strings would let three of the four through.

    This is TEXTUAL ONLY — no filesystem access, no symlink following — and that
    is a deliberate limit, not an oversight: it is one half of the check.  The
    other half is :func:`authorise_write`, which re-runs these same rules against
    the inode-resolved path.  Nothing may call this on its own and conclude a
    write is safe.
    """
    fp = unicodedata.normalize("NFKC", str(file_path).strip()).replace("\\", "/")
    # posixpath.normpath resolves '.', '..' and duplicate separators textually,
    # which is what we want here: the symlink-aware half runs separately.
    fp = posixpath.normpath(fp)
    return _fold(fp)


def is_hard_stop(file_path: str, key_path: str | None = None) -> bool:
    """Check if the target path or key belongs to the hard-stop refusal set.

    Callers that are about to WRITE must not call this directly — they call
    :func:`authorise_write`, which runs it against both the declared and the
    resolved spelling.  This stays public for the read-only auditors
    (``watchdog`` checks committed paths after the fact, where there is no
    write to authorise).
    """
    fp = normalise_target_path(file_path)
    kp = _fold(str(key_path or ""))

    # 0. Absolute paths and anything that escaped the repo textually.
    if fp.startswith("/") or fp.startswith("../"):
        return True

    # 0b. Surfaces that execute, or that decide what executes. Checked on the
    # NORMALISED path so "docs/../scripts/x.sh" cannot slip past.
    if fp.startswith(PROTECTED_PREFIXES):
        return True
    if posixpath.basename(fp) in PROTECTED_NAMES:
        return True

    # 1. Paths containing external, deploy, or destructive
    for word in REJECTED_WORDS:
        if word in fp:
            return True

    # 2. configs/governance.yaml
    if GOVERNANCE_PATH in fp:
        return True

    # 3. omniagentos/policy/**
    if POLICY_DIR in fp:
        return True

    # 4. Budgets and credentials
    for word in RESTRICTED_KEY_WORDS:
        if word in fp or word in kp:
            return True

    return False


def authorise_write(
    repo_root: str | os.PathLike[str],
    declared: str,
    *,
    content_kind: ContentKind,
    payload: Any,
    key: str | None = None,
    confine_to: str | None = None,
) -> AuthorisedWrite:
    """Authorise a write in full, or refuse it.  The only way to obtain a writer.

    Raises :class:`WriteRefused` — never returns a handle that failed a check,
    and never returns one a caller could have obtained without supplying the
    content as well as the target.

    ``content_kind`` and ``payload`` are REQUIRED, and that is the whole
    correction over the previous shape. This used to return a bare ``Path``
    after checking the path alone, which made "examined path, unexamined
    content" not merely possible but the default: the caller held an object
    that would accept any bytes at all. Three findings in two review rounds
    were that same absence — the hard-stop unwired from the writers, the
    symlink the check could not resolve, and the payload nothing ever looked
    at. Requiring the content to get the handle is what makes the unexamined
    write unrepresentable rather than merely currently-absent.

    ``key`` is the proposal's ``target["key"]``; ``confine_to`` is an optional
    repo-relative directory the resolved file must sit under (the YAML lane
    passes ``"configs"``).
    """
    root = Path(repo_root)
    text = unicodedata.normalize("NFKC", str(declared).strip()).replace("\\", "/")

    if not text:
        raise WriteRefused("proposal names no target path (fail closed)")
    if "\x00" in text:
        raise WriteRefused(f"target path contains a NUL byte: {declared!r}")

    # (a) Spellings that must never reach the join. `Path(root) / "/etc/passwd"`
    # is `/etc/passwd` — pathlib drops the left operand for an absolute right
    # one — so an absolute target would silently escape containment by being
    # joined, not by traversing.
    if posixpath.isabs(text) or text.startswith("~"):
        raise WriteRefused(
            f"target path must be repo-relative, got {declared!r} (fail closed)"
        )

    # (b) The rules against the path AS DECLARED. A symlink pointing FROM a
    # protected name TO somewhere harmless would otherwise launder the write.
    if is_hard_stop(text, key):
        raise WriteRefused(
            f"target file {text} (key: {key}) touches a restricted resource (hard-stop rule)"
        )

    # (c) Filesystem truth. Inode ancestry, so a symlink is followed rather than
    # spelled around, and a leaf that does not exist yet is still anchored.
    # `None` means "escaped the repo, or containment could not be proved" — both
    # refuse.
    parts = inode_relative_parts_anchored(root / text, root)
    if parts is None:
        raise WriteRefused(
            f"target file {text} resolves outside the repository root {root} (fail closed)"
        )
    if not parts:
        raise WriteRefused(f"target file {text} resolves to the repository root itself")
    canonical = posixpath.join(*parts)

    # (d) The rules AGAIN, against what the path RESOLVES to. This is the half
    # the textual check structurally cannot do, and the half whose absence let
    # docs/configs_link/governance.yaml validate and then overwrite governance.
    if is_hard_stop(canonical, key):
        raise WriteRefused(
            f"target file {text} resolves to {canonical} (key: {key}), "
            "which touches a restricted resource (hard-stop rule)"
        )

    # (e) Per-lane confinement, also on filesystem truth rather than a prefix
    # string, so `configs/link/..`-style spellings cannot fake membership.
    if confine_to is not None:
        if inode_relative_parts_anchored(root / text, root / confine_to) is None:
            raise WriteRefused(f"edit path must live under {confine_to}/: {text}")

    abspath = root.joinpath(*parts)

    # (f) TYPE. The target's file type must match what is about to be written.
    # Path rules cannot express this: configs/swarm.yaml and
    # omniagentos/reflection/__init__.py are both unprotected, in-repo,
    # non-traversing paths, and both accepted appended prose — corrupting a
    # config in the first case and executing on the next import in the second.
    allowed_suffixes = CONTENT_KIND_SUFFIXES[content_kind]
    if _fold(posixpath.splitext(canonical)[1]) not in allowed_suffixes:
        raise WriteRefused(
            f"a {content_kind.value} write may only land in {'/'.join(allowed_suffixes)}, "
            f"but {canonical} is not one of those (fail closed)"
        )

    # (g) MODE. Never write into something the system executes, whatever it is
    # called. PROTECTED_PREFIXES enumerates the executable surfaces we know
    # about by NAME; this catches the ones we do not, by asking the filesystem.
    #
    # A directory is refused here rather than at the open(): landing on one used
    # to surface as IsADirectoryError out of `.write()`, which is an OSError and
    # therefore NOT a ValueError. Every caller and every test in this package
    # treats ValueError as "refused", so an escaping OSError is a refusal nobody
    # is catching by contract. Refusals have one type.
    if abspath.is_dir():
        raise WriteRefused(
            f"target {canonical} is a directory, not a writable file (fail closed)"
        )
    if abspath.is_file():
        try:
            mode = abspath.stat().st_mode
        except OSError as exc:
            raise WriteRefused(f"cannot stat {canonical} to check its mode: {exc}") from None
        if mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
            raise WriteRefused(
                f"{canonical} is executable; a reflection proposal may not write "
                "into something the system runs (fail closed)"
            )

    # (h) CONTENT. The payload's own rules — see examine_payload. Runs last
    # because the earlier failures are cheaper, but it is not optional: the
    # handle below does not exist unless this returns.
    examine_payload(content_kind, key, payload)

    return AuthorisedWrite(
        declared=str(declared),
        canonical=canonical,
        content_kind=content_kind,
        key=key,
        _path=abspath,
        _token=_AUTHORISED,
    )
