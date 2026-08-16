#!/usr/bin/env python3
"""Content-equivalence landing detector: "is this PR's work already on main?"

PRs on this estate do not land by having their branch pushed. They land by
cherry-pick, by rebase, by being re-authored inside a train branch, and
sometimes by a human retyping the change with an extra step bolted on. Every
one of those breaks SHA comparison, and `gh pr list --state merged` sees none
of them. So the queue and the PR list both drift, and a human has to read
twenty diffs by hand to find out what is actually done.

This module answers the question mechanically, and it is deliberately
asymmetric: it is built to be *bad at saying yes*.

  Closing someone else's PR is outward-facing and hard to take back. A missed
  close costs one more manual triage. A wrong close costs a colleague their
  work and their trust in the tool. Every judgement call below is resolved
  toward LEAVING IT OPEN.

--------------------------------------------------------------------- ladder

For each file the branch touches (three-dot, so only the branch's own
changes), containment is established at the strongest rung that holds:

  1. ``identical``       main's blob for that path == the branch's blob.
  2. ``deleted-both``    the branch deleted it and main does not have it.
  3. ``patch-contained`` the file's diff reverse-applies cleanly onto main's
                         tree -- main already contains exactly that change.
  4. ``line-contained``  every non-blank line the branch ADDS appears on main
                         in order, and no line it DELETES survives on main.
                         This is the rung that catches a landing which added
                         further lines *inside* the block (PR #19), which
                         rungs 1-3 all miss.

Rungs 1-3 are ``strict``. Rung 4 is not, and is never sufficient on its own.

  ``absent-on-main``  the branch adds a file main does not have  -> NOT contained
  ``divergent``       none of the rungs hold                     -> NOT contained

------------------------------------------------------------------- verdicts

  landed    every touched file is contained, and there is at least one file
  partial   some contained, some not  -- REPORT, NEVER CLOSE
  unlanded  nothing contained
  empty     the branch's diff is empty -- SUSPICIOUS, never closable

``partial`` is the whole point of the exercise. PR #42 has 5 of its 9 files on
main and one commit still missing; a tool that answers "landed?" with a
boolean closes it and loses the missing commit.

--------------------------------------------------------------- landing sha

A close comment that cannot name the commit that superseded the PR is not
worth reading, and a landing we cannot name is a landing we have not really
proved. So closability additionally requires a NAMED landing sha, from one of:

  * ``patch-id``    an exact patch-id match against a commit on main. Found via
                    ``git cherry`` for the verdict and a targeted patch-id
                    search (restricted to main commits touching the same files)
                    for the sha.
  * ``attestation`` a commit message on main naming this PR's head sha, or
                    carrying a ``Closes/Fixes/Resolves #NN`` trailer. This is
                    the "train merge message" path, and it is what identifies
                    a re-authored landing.
  * ``carrier``     the most recent main commit touching the contained files.
                    Weakest; only accepted when every file is strict.

------------------------------------------------------------- instrument self-test

Two loops have already nearly closed PRs on an instrument fault rather than a
finding:

  * a verification loop written for bash was run under zsh, where an unquoted
    ``$files`` does not word-split. The loop body executed zero times, every
    multi-file PR "matched nothing", and nothing-matched was read as
    agreement. It came within one command of closing fifteen PRs.
  * two agents grepped for a symbol name nobody had read out of the source,
    found nothing, and concluded a guard was missing when it was present.

Both are the same bug: an absence produced by the instrument, reported as a
favourable finding. Defences here, in order of how much they buy:

  * ``self_test()`` runs a NEGATIVE canary (a patch that adds a random line
    nobody has ever committed MUST come back not-contained) and a POSITIVE
    canary (main's own tip diff MUST come back contained) before any verdict
    is trusted. A detector that says yes to the negative canary is broken, and
    the run aborts rather than reporting.
  * an empty file list is ``empty``, never ``landed``. Zero matches is
    suspicious, not agreement.
  * ``implausible_close_rate()`` refuses a whole run in which most of the PRs
    look landed at once. Real landing rates are a minority of the queue; a run
    where everything matches is the zsh signature, not a good day.
  * there is no shell in the hot path. Every git call is an argv list through
    subprocess, so no word-splitting rule of any shell can change a result.

This module never talks to GitHub and never writes anything. It reads a git
repository and returns verdicts.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path

# Rungs of the containment ladder, strongest first. The first three are
# `strict`; `line-contained` is corroborating evidence only.
STRICT_RUNGS = ("identical", "deleted-both", "patch-contained")
WEAK_RUNGS = ("line-contained",)
CONTAINED_RUNGS = STRICT_RUNGS + WEAK_RUNGS

# Closing keywords GitHub itself honours, plus the shapes our train messages use.
_CLOSES_RE_TEMPLATE = (
    r"(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s*#%d\b"
)
# A bare mention. Recorded, never acted on: "see #42" is not "#42 landed".
_MENTION_RE_TEMPLATE = r"#%d\b"

# git's well-known empty tree, for repositories whose main is a root commit.
_EMPTY_TREE = "39c8dba61a47d87443b82bae54ae3e0998e3a537"


class InstrumentError(RuntimeError):
    """The detector itself is broken. Never report this as a finding about a PR.

    An instrument fault reported as a candidate verdict is how a run closes
    fifteen PRs on a word-splitting bug. Callers must abort, not degrade.
    """


@dataclass
class FileVerdict:
    path: str
    rung: str  # one of CONTAINED_RUNGS, or "absent-on-main" / "divergent"
    detail: str = ""

    @property
    def contained(self) -> bool:
        return self.rung in CONTAINED_RUNGS

    @property
    def strict(self) -> bool:
        return self.rung in STRICT_RUNGS

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "rung": self.rung,
            "contained": self.contained,
            "strict": self.strict,
            "detail": self.detail,
        }


@dataclass
class Verdict:
    """One PR's answer. ``closable`` is the only field an actor may act on."""

    head: str
    number: int | None = None
    branch: str = ""
    status: str = "error"  # landed | partial | unlanded | empty | error
    closable: bool = False
    reason: str = ""
    files: list[FileVerdict] = field(default_factory=list)
    landing_shas: list[str] = field(default_factory=list)
    landing_kind: str = ""  # patch-id | attestation | carrier
    attestations: list[dict] = field(default_factory=list)
    mentions: list[str] = field(default_factory=list)
    cherry_equivalent: int = 0
    cherry_unlanded: int = 0

    @property
    def contained_count(self) -> int:
        return sum(1 for f in self.files if f.contained)

    @property
    def missing(self) -> list[FileVerdict]:
        return [f for f in self.files if not f.contained]

    @property
    def all_strict(self) -> bool:
        return bool(self.files) and all(f.strict for f in self.files)

    def as_dict(self) -> dict:
        return {
            "pr": self.number,
            "branch": self.branch,
            "head": self.head,
            "status": self.status,
            "closable": self.closable,
            "reason": self.reason,
            "files_total": len(self.files),
            "files_contained": self.contained_count,
            "files_missing": [f.path for f in self.missing],
            "all_strict": self.all_strict,
            "landing_shas": self.landing_shas,
            "landing_kind": self.landing_kind,
            "attestations": self.attestations,
            "mentions": self.mentions,
            "patch_id_equivalent_commits": self.cherry_equivalent,
            "patch_id_unlanded_commits": self.cherry_unlanded,
            "files": [f.as_dict() for f in self.files],
        }


class LandDetector:
    """Read-only. Never writes to the repository, its index, or its worktree.

    The reverse-apply check needs an index holding main's tree. It gets one via
    ``GIT_INDEX_FILE`` pointing at a scratch file and ``git apply --cached``,
    so the real index and working tree are never touched -- which matters,
    because the checkout this runs against is usually a live serving checkout
    with a dirty tree that must not be disturbed.
    """

    def __init__(
        self,
        repo: str | Path,
        main_ref: str = "main",
        *,
        scan_limit: int = 800,
        range_spec: str | None = None,
    ) -> None:
        self.repo = str(repo)
        self.main_ref = main_ref
        self.scan_limit = scan_limit
        # Which slice of main's history may supply an attestation. For the
        # post-train step this is the just-landed range; for a full sweep it is
        # the last `scan_limit` commits.
        self.range_spec = range_spec
        self._tmpdir = tempfile.mkdtemp(prefix="land-detect-")
        self._index_path = os.path.join(self._tmpdir, "index")
        self._index_tree: str | None = None
        self._attest_cache: list[tuple[str, str]] | None = None
        self._self_tested = False
        self.main_sha = self._git("rev-parse", "--verify", f"{main_ref}^{{commit}}").strip()

    # ------------------------------------------------------------- plumbing

    def _git(self, *args: str, stdin: bytes | None = None, check: bool = True) -> str:
        """One git call. argv list, never a shell string.

        There is no shell here on purpose: the zsh word-splitting fault that
        nearly closed fifteen PRs cannot occur in a process that never asks a
        shell to split anything.
        """
        proc = subprocess.run(
            ["git", "-C", self.repo, *args],
            input=stdin,
            capture_output=True,
            check=False,
        )
        if check and proc.returncode != 0:
            raise InstrumentError(
                f"git {' '.join(args[:3])} failed ({proc.returncode}): "
                f"{proc.stderr.decode('utf-8', 'replace').strip()[:400]}"
            )
        return proc.stdout.decode("utf-8", "replace")

    def _git_ok(self, *args: str, stdin: bytes | None = None) -> tuple[bool, str]:
        proc = subprocess.run(
            ["git", "-C", self.repo, *args],
            input=stdin,
            capture_output=True,
            check=False,
        )
        return proc.returncode == 0, proc.stderr.decode("utf-8", "replace").strip()

    def _blob(self, ref: str, path: str) -> str | None:
        proc = subprocess.run(
            ["git", "-C", self.repo, "rev-parse", "--verify", f"{ref}:{path}"],
            capture_output=True,
            check=False,
        )
        if proc.returncode != 0:
            return None
        return proc.stdout.decode().strip()

    def _text(self, ref: str, path: str) -> list[str] | None:
        proc = subprocess.run(
            ["git", "-C", self.repo, "show", f"{ref}:{path}"],
            capture_output=True,
            check=False,
        )
        if proc.returncode != 0:
            return None
        return proc.stdout.decode("utf-8", "replace").splitlines()

    def _fresh_index(self) -> dict[str, str]:
        """Env pointing git at a scratch index seeded with main's tree."""
        if self._index_tree != self.main_sha:
            if os.path.exists(self._index_path):
                os.unlink(self._index_path)
            env = dict(os.environ, GIT_INDEX_FILE=self._index_path)
            proc = subprocess.run(
                ["git", "-C", self.repo, "read-tree", self.main_sha],
                env=env,
                capture_output=True,
                check=False,
            )
            if proc.returncode != 0:
                raise InstrumentError(
                    "could not seed a scratch index from "
                    f"{self.main_sha[:12]}: {proc.stderr.decode('utf-8', 'replace')[:300]}"
                )
            self._index_tree = self.main_sha
        return dict(os.environ, GIT_INDEX_FILE=self._index_path)

    def _reverse_applies(self, patch: bytes) -> bool:
        """True if main's tree already contains what this patch introduces.

        ``--cached`` and a scratch ``GIT_INDEX_FILE`` keep this off the real
        index and off disk. ``--check`` writes nothing at all.
        """
        if not patch.strip():
            return False
        env = self._fresh_index()
        proc = subprocess.run(
            ["git", "-C", self.repo, "apply", "--cached", "--reverse", "--check", "-"],
            input=patch,
            env=env,
            capture_output=True,
            check=False,
        )
        return proc.returncode == 0

    # ---------------------------------------------------------- self-test

    def self_test(self) -> None:
        """Prove the detector can still say NO, and can still say YES.

        Order matters: the negative canary runs first, because the failure this
        exists to catch is a detector that has started agreeing with
        everything. A broken detector that passes the positive canary and fails
        the negative one is still broken, and must not grade a single PR.
        """
        probe_path = self._pick_probe_file()

        # NEGATIVE: a line nobody has ever committed cannot be on main.
        nonce = uuid.uuid4().hex
        fake = (
            f"--- a/{probe_path}\n"
            f"+++ b/{probe_path}\n"
            "@@ -1,1 +1,2 @@\n"
            f" {self._first_line(probe_path)}\n"
            f"+# land-detect canary {nonce}\n"
        ).encode()
        if self._reverse_applies(fake):
            raise InstrumentError(
                "NEGATIVE CANARY FAILED: a never-committed line reported as "
                "already on main. The containment check is agreeing with "
                "everything -- exactly the fault that nearly closed fifteen "
                "PRs. Refusing to grade anything."
            )

        # POSITIVE: main's own tip change is, definitionally, on main.
        # A root commit has no parent, so the whole tree stands in as "the
        # change main made" — reverse-applying it must still check out.
        ok, _ = self._git_ok("rev-parse", "--verify", f"{self.main_sha}^{{commit}}^")
        parent = f"{self.main_sha}^" if ok else _EMPTY_TREE
        tip_patch = self._git(
            "diff", "--binary", parent, self.main_sha
        ).encode("utf-8", "replace")
        if tip_patch.strip() and not self._reverse_applies(tip_patch):
            raise InstrumentError(
                "POSITIVE CANARY FAILED: main's own tip commit does not "
                "reverse-apply onto main. The scratch index is not holding the "
                "tree we think it is. Refusing to grade anything."
            )

        # The line-level rung gets its own negative canary: it is the fuzziest
        # rung and the one most able to drift into saying yes.
        if self._lines_contained([f"# land-detect canary {nonce}"], [], probe_path):
            raise InstrumentError(
                "NEGATIVE CANARY FAILED (line rung): a never-committed line "
                "reported as present on main."
            )

        self._self_tested = True

    def _pick_probe_file(self) -> str:
        names = [
            n for n in self._git("ls-tree", "--name-only", self.main_sha).splitlines()
            if n.strip()
        ]
        for candidate in ("README.md", "AGENTS.md", "CONTRACT.md"):
            if candidate in names:
                return candidate
        for name in names:
            if self._text(self.main_sha, name):
                return name
        raise InstrumentError("no probe file at the root of main; cannot self-test")

    def _first_line(self, path: str) -> str:
        lines = self._text(self.main_sha, path) or [""]
        return lines[0]

    # ------------------------------------------------------- the ladder

    def _lines_contained(
        self, added: list[str], removed: list[str], path: str
    ) -> bool:
        """Rung 4. Every added line present on main IN ORDER; no removed line left.

        Blank and whitespace-only lines are ignored on both sides -- they match
        anything, so counting them would only inflate the evidence. If that
        leaves nothing to check, the rung does NOT hold: an empty comparison is
        not agreement.
        """
        main_lines = self._text(self.main_sha, path)
        if main_lines is None:
            return False
        add = [ln.strip() for ln in added if ln.strip()]
        rem = [ln.strip() for ln in removed if ln.strip()]
        if not add and not rem:
            return False
        haystack = [ln.strip() for ln in main_lines]
        # Ordered-subsequence walk: the added block may be interrupted on main
        # (that is the whole point) but must not be reordered or partial.
        i = 0
        for line in add:
            while i < len(haystack) and haystack[i] != line:
                i += 1
            if i >= len(haystack):
                return False
            i += 1
        present = set(haystack)
        return not any(line in present for line in rem)

    def _file_verdict(self, head: str, status: str, path: str) -> FileVerdict:
        head_blob = self._blob(head, path)
        main_blob = self._blob(self.main_sha, path)

        if head_blob is None:
            # The branch deleted it. Contained iff main has also lost it.
            if main_blob is None:
                return FileVerdict(path, "deleted-both", "deleted on the branch and absent from main")
            return FileVerdict(path, "divergent", "deleted on the branch but still present on main")

        if main_blob is None:
            return FileVerdict(
                path, "absent-on-main",
                "the branch adds this path and main does not have it",
            )

        if head_blob == main_blob:
            return FileVerdict(path, "identical", f"blob {head_blob[:12]}")

        patch = self._git(
            "diff", "--binary", f"{self.main_ref}...{head}", "--", path
        ).encode("utf-8", "replace")
        if self._reverse_applies(patch):
            return FileVerdict(path, "patch-contained", "the file's diff reverse-applies onto main")

        added, removed = _split_hunks(patch.decode("utf-8", "replace"))
        if self._lines_contained(added, removed, path):
            return FileVerdict(
                path, "line-contained",
                f"all {len([a for a in added if a.strip()])} added lines present on main in order, "
                "none of the removed lines survive (weak rung -- needs corroboration)",
            )

        return FileVerdict(path, "divergent", "main's copy is neither identical nor a superset")

    # -------------------------------------------------------- attestation

    def _attest_commits(self) -> list[tuple[str, str]]:
        if self._attest_cache is not None:
            return self._attest_cache
        args = ["log", "--format=%H%x01%B%x02"]
        if self.range_spec:
            args.append(self.range_spec)
        else:
            args += [f"-n{self.scan_limit}", self.main_ref]
        raw = self._git(*args)
        out: list[tuple[str, str]] = []
        for entry in raw.split("\x02"):
            entry = entry.strip()
            if not entry:
                continue
            sha, _, body = entry.partition("\x01")
            out.append((sha.strip(), body))
        self._attest_cache = out
        return out

    def _attestations(self, head: str, number: int | None, branch: str) -> tuple[list[dict], list[str]]:
        """Strong attestations, and (separately) bare mentions we will NOT act on."""
        strong: list[dict] = []
        mentions: list[str] = []
        closes_re = re.compile(_CLOSES_RE_TEMPLATE % number) if number else None
        mention_re = re.compile(_MENTION_RE_TEMPLATE % number) if number else None
        head_full = self._git("rev-parse", "--verify", f"{head}^{{commit}}").strip().lower()
        for sha, body in self._attest_commits():
            low = body.lower()
            # Naming the head sha is the strongest textual claim available: it
            # is specific to this tip, not to the PR in general. A token counts
            # only at >= 8 hex and only as a genuine prefix of the full head --
            # anything looser lets an unrelated short hash in the prose vouch
            # for a PR.
            named = any(
                head_full.startswith(token)
                for token in re.findall(r"\b[0-9a-f]{8,40}\b", low)
            )
            if named:
                strong.append({"sha": sha, "kind": "head-sha", "subject": body.splitlines()[0][:120]})
                continue
            if closes_re and closes_re.search(body):
                strong.append({"sha": sha, "kind": "closes-trailer", "subject": body.splitlines()[0][:120]})
                continue
            if (mention_re and mention_re.search(body)) or (branch and branch in body):
                # Recorded so a human can see the near-miss, never acted on:
                # "see #42" and "on branch fix/x" are not "#42 landed".
                mentions.append(sha)
        return strong, mentions

    # --------------------------------------------------------- patch ids

    def _patch_id(self, rev_or_patch: str | bytes) -> str | None:
        if isinstance(rev_or_patch, str):
            patch = self._git("diff-tree", "-p", "--binary", rev_or_patch).encode("utf-8", "replace")
        else:
            patch = rev_or_patch
        if not patch.strip():
            return None
        proc = subprocess.run(
            ["git", "-C", self.repo, "patch-id", "--stable"],
            input=patch,
            capture_output=True,
            check=False,
        )
        out = proc.stdout.decode().split()
        return out[0] if out else None

    def _landing_by_patch_id(self, head: str, paths: list[str]) -> list[str]:
        """Main commits whose patch-id equals one of the branch's commits'.

        Restricted to main commits that touch the same paths, so this stays a
        handful of patch-ids rather than the whole of main.
        """
        if not paths:
            return []
        branch_commits = [
            c for c in self._git(
                "rev-list", "--no-merges", "--right-only", f"{self.main_ref}...{head}"
            ).split() if c
        ]
        if not branch_commits:
            return []
        wanted = {}
        for c in branch_commits:
            pid = self._patch_id(c)
            if pid:
                wanted.setdefault(pid, c)
        if not wanted:
            return []
        candidates = [
            c for c in self._git(
                "rev-list", "--no-merges", "--left-only",
                f"{self.main_ref}...{head}", "--", *paths,
            ).split() if c
        ]
        found: list[str] = []
        for c in candidates:
            pid = self._patch_id(c)
            if pid and pid in wanted:
                found.append(c)
        return found

    def _carrier_shas(self, head: str, paths: list[str]) -> list[str]:
        """Most recent main commit touching each contained path, in the scan window."""
        if not paths:
            return []
        out: list[str] = []
        for path in paths:
            got = self._git(
                "rev-list", "--no-merges", "-n1", self.main_ref, "--", path
            ).split()
            if got:
                out.append(got[0])
        # Preserve order, drop repeats.
        seen: set[str] = set()
        return [s for s in out if not (s in seen or seen.add(s))]

    # ----------------------------------------------------------- classify

    def classify(self, head: str, *, number: int | None = None, branch: str = "") -> Verdict:
        if not self._self_tested:
            raise InstrumentError(
                "self_test() has not passed on this detector; refusing to "
                "grade a PR with an unverified instrument"
            )

        ok, err = self._git_ok("rev-parse", "--verify", f"{head}^{{commit}}")
        if not ok:
            return Verdict(head=head, number=number, branch=branch, status="error",
                           reason=f"head {head} is not a commit in this repository: {err[:160]}")

        raw = self._git("diff", "--name-status", "-z", f"{self.main_ref}...{head}")
        entries = _parse_name_status(raw)
        if not entries:
            # A branch with no changes against its base is not agreement. It is
            # either a genuinely empty PR or, far more often, an instrument
            # fault that produced an empty list. Never closable.
            return Verdict(
                head=head, number=number, branch=branch, status="empty", closable=False,
                reason="the branch's three-dot diff against main is EMPTY. Zero "
                       "matches is suspicious, not agreement -- reporting instead "
                       "of closing.",
            )

        files = [self._file_verdict(head, st, path) for st, path in entries]
        contained = [f for f in files if f.contained]
        missing = [f for f in files if not f.contained]

        cherry = self._git("cherry", self.main_ref, head).splitlines()
        equivalent = sum(1 for line in cherry if line.startswith("-"))
        unlanded = sum(1 for line in cherry if line.startswith("+"))

        strong, mentions = self._attestations(head, number, branch)

        verdict = Verdict(
            head=head, number=number, branch=branch, files=files,
            cherry_equivalent=equivalent, cherry_unlanded=unlanded,
            attestations=strong, mentions=mentions,
        )

        if missing and contained:
            verdict.status = "partial"
            verdict.closable = False
            verdict.reason = (
                f"PARTIAL LANDING: {len(contained)} of {len(files)} touched files are on "
                f"main, {len(missing)} are not ({', '.join(f.path for f in missing[:4])}"
                f"{'...' if len(missing) > 4 else ''}). "
                f"{unlanded} of {equivalent + unlanded} commits have no patch-id equivalent "
                "on main. Closing this would lose the missing work -- reporting only."
            )
            return verdict

        if missing:
            verdict.status = "unlanded"
            verdict.closable = False
            verdict.reason = (
                f"none of the {len(files)} touched files are on main; "
                f"{unlanded} commits have no patch-id equivalent"
            )
            return verdict

        # Everything is contained. Now: can we NAME the landing?
        verdict.status = "landed"
        paths = [f.path for f in files]
        by_patch = self._landing_by_patch_id(head, paths)
        if by_patch:
            verdict.landing_shas = by_patch
            verdict.landing_kind = "patch-id"
        elif strong:
            verdict.landing_shas = [a["sha"] for a in strong]
            verdict.landing_kind = "attestation"
        elif verdict.all_strict:
            verdict.landing_shas = self._carrier_shas(head, paths)
            verdict.landing_kind = "carrier"

        # Diagnose the weak rung first: it is the more specific answer, and a
        # refusal whose stated reason is vague sends the next reader to the
        # wrong question.
        if not verdict.all_strict and not strong:
            weak = [f.path for f in files if not f.strict]
            verdict.closable = False
            verdict.reason = (
                f"all {len(files)} touched files are on main, but {len(weak)} only at "
                f"the weak line rung ({', '.join(weak[:3])}) with no commit message on "
                "main attesting to this PR. Line containment alone is corroboration, "
                "not proof -- reporting instead of closing."
            )
            return verdict

        if not verdict.landing_shas:
            verdict.closable = False
            verdict.reason = (
                f"all {len(files)} touched files are on main, but no landing commit "
                "could be NAMED (no patch-id match, no attestation, no carrier). "
                "A close comment that cannot name the commit that superseded the "
                "PR is not evidence -- reporting instead of closing."
            )
            return verdict

        verdict.closable = True
        how = {
            "patch-id": "every commit has a patch-id equivalent on main",
            "attestation": "a commit on main names this PR's head sha or carries a closing trailer",
            "carrier": "every touched file is byte-identical to or a strict subset of main",
        }[verdict.landing_kind]
        verdict.reason = (
            f"LANDED: all {len(files)} touched files are on main "
            f"({', '.join(sorted({f.rung for f in files}))}); {how}; "
            f"landing sha {verdict.landing_shas[0][:12]}"
        )
        return verdict


# ------------------------------------------------------------- pure helpers


def _parse_name_status(raw: str) -> list[tuple[str, str]]:
    """Parse ``git diff --name-status -z`` into (status, path) pairs.

    NUL-delimited on purpose: a path with a space in it is exactly the input
    that turns a naive whitespace split into a silent under-count, and an
    under-count here reads as "fewer files to check", which reads as "landed".
    Rename/copy entries consume two path fields; the destination is the one
    the branch is asserting.
    """
    fields = [f for f in raw.split("\0") if f != ""]
    out: list[tuple[str, str]] = []
    i = 0
    while i < len(fields):
        status = fields[i]
        i += 1
        if i >= len(fields):
            break
        if status and status[0] in ("R", "C"):
            # <status>\0<source>\0<dest>
            if i + 1 >= len(fields):
                break
            out.append((status[0], fields[i + 1]))
            i += 2
        else:
            out.append((status[0] if status else "M", fields[i]))
            i += 1
    return out


def _split_hunks(patch_text: str) -> tuple[list[str], list[str]]:
    """Added and removed payload lines from a unified diff, headers excluded."""
    added: list[str] = []
    removed: list[str] = []
    in_hunk = False
    for line in patch_text.splitlines():
        if line.startswith("@@"):
            in_hunk = True
            continue
        if not in_hunk:
            continue
        if line.startswith(("+++", "---")):
            continue
        if line.startswith("+"):
            added.append(line[1:])
        elif line.startswith("-"):
            removed.append(line[1:])
    return added, removed


def implausible_close_rate(verdicts: list[Verdict], *, max_fraction: float = 0.5) -> str | None:
    """Refuse a whole run in which most PRs suddenly look landed.

    The zsh word-splitting fault did not produce one wrong verdict; it produced
    a uniform sweep of them, because a broken instrument is broken for every
    input. So the fleet-level shape is a better detector of that fault than any
    single verdict, and it is checked before anything outward-facing happens.

    Returns a refusal string, or None if the run's shape is plausible.
    """
    if len(verdicts) < 4:
        return None
    closable = sum(1 for v in verdicts if v.closable)
    if closable > max_fraction * len(verdicts):
        return (
            f"REFUSING THE RUN: {closable} of {len(verdicts)} PRs came back closable "
            f"({closable / len(verdicts):.0%} > {max_fraction:.0%}). A run where most of "
            "the queue looks landed at once is the signature of a broken comparison, "
            "not a good day. Nothing was closed. Re-run with --max-close-fraction if "
            "a genuine bulk landing really did happen."
        )
    return None
