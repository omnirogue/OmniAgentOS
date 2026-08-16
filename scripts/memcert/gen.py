#!/usr/bin/env python3
"""memcert deterministic fixture-world generator (DESIGN §4, §12).

Generates a fictitious operator-estate world — people, projects, services,
machines, budgets, schedules — with every proper noun drawn from seeded
syllable generators (zero real-world lexical anchors) on a virtual timeline
(base 2027-03-01). Emits chat-turn session transcripts in the claude-bridge
stream-json line shape (``{"type": role, "message": {"role", "content":
[{"type": "text", "text"}]}, "timestamp"}``) plus probe items covering axes
MEM-A..MEM-H at difficulty levels 1-3.

Answers never touch the fixture tree: ``World.write_fixtures`` writes
transcripts, answer-free items (``Item.public_json``), and a seed-hashed
metadata file only. Graders re-derive answer specs by calling
``generate_world`` again with the same ``(seed, scale, split)``.

CLI::

    python scripts/memcert/gen.py --seed N --scale S --split dev --out DIR \
        [--emit-answers PATH] [--run-uuid UUID]

``--emit-answers`` refuses (exit 2) any path inside a git checkout — answer
specs may only land on out-of-repo protected-store paths (the
``scripts/northstar_cert/seed_holdout.py`` guard idea).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

try:  # imported as a package member: `scripts.memcert.gen`
    from .core import ABSTAIN_TOKEN, AnswerSpec, Item, canary_line, rng_for
except ImportError:
    # Run as a script (the Makefile / CLI path): sys.path[0] is this directory
    # and there is no parent package for the relative import to resolve
    # against — same fallback idiom as scripts/northstar_cert/emit_gaps.py.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from core import ABSTAIN_TOKEN, AnswerSpec, Item, canary_line, rng_for

BASE_DATE = date(2027, 3, 1)
#: scale -> (main sessions, distractor sessions). Only "S" is exercised by v1
#: tests; M/L exist for the MEM-I capacity-curve follow-up.
SCALES = {"S": (8, 6), "M": (16, 12), "L": (32, 24)}

_WEEKDAYS = ("Mondays", "Tuesdays", "Wednesdays", "Thursdays", "Fridays")
_CONSONANTS = "bdfghklmnprstvz"
_VOWELS = "aeiou"

_FILLER_PLAIN = (
    "Morning sync wrapped up with nothing urgent left on the board.",
    "The weekly digest went out on time and nobody replied with corrections.",
    "Nobody was paged today; the queues drained on their own before lunch.",
    "Notes from the standup were filed without any follow-up items.",
    "No changes shipped today; the change freeze held through the evening.",
    "The on-call handoff for the week was quiet and uneventful.",
    "Backlog grooming moved a handful of cards without much debate.",
    "The retro produced two small action items and both are already closed.",
    "Log volumes stayed flat compared with the same stretch last week.",
    "A tabletop drill ran end to end without turning up any surprises.",
)
_FILLER_ENTITY = (
    "Dashboards for {e} stayed quiet through the whole afternoon.",
    "Reviewed the runbook for {e}; no edits were needed anywhere.",
    "Health checks for {e} reported green across the entire fleet.",
    "Tidied a few stray labels in the tracker for {e}.",
    "A routine sweep over {e} found nothing worth opening a ticket for.",
    "Someone asked for a walkthrough of {e}; it is scheduled for later.",
)
_USER_OPENERS = (
    "Quick check-in: anything new since yesterday?",
    "What is the latest from the overnight window?",
    "Give me a short status rundown, please.",
    "Anything I should know before the afternoon sync?",
    "How are things looking across the estate today?",
)

# MEM-G task-type banks. Placebo phrases are hand-matched in character length
# to the real phrases (±≤4 chars) so token-matched placebo lessons land within
# the ±10% whole-lesson budget by construction.
_G_TASKS = (
    "restarting a stalled ingest pipeline",
    "rotating expired access keys",
    "resizing an overloaded database",
    "draining a stuck job queue",
    "renewing an expiring certificate",
    "archiving cold log volumes",
)
_G_PLACEBO_TASKS = (
    "repainting a weathered loading dock",
    "reordering the pantry stock",
    "relabeling an untidy storeroom",
    "restacking a supply shelf",
    "rescheduling a delayed delivery",
    "reprinting visitor badges",
)
_G_REASONS = (
    "caches go stale midway",
    "locks are never released",
    "quotas reset too early",
    "buffers overflow quietly",
    "tokens expire in flight",
    "indexes drift out of step",
)


def _lesson(bad: str, good: str, phrase: str, reason: str) -> str:
    return (
        f"Lesson: the {bad} approach fails when {phrase} because {reason}; "
        f"use the {good} approach instead."
    )


def _make_names(
    rng: random.Random, count: int, syllables: int, capitalize: bool, seen: set[str]
) -> list[str]:
    """Unique consonant+vowel syllable names; ``seen`` stores lowercase forms."""
    names: list[str] = []
    while len(names) < count:
        base = "".join(rng.choice(_CONSONANTS) + rng.choice(_VOWELS) for _ in range(syllables))
        if base in seen:
            continue
        seen.add(base)
        names.append(base.capitalize() if capitalize else base)
    return names


class _Pool:
    """Deterministic pool of pre-generated unique names, consumed in order."""

    def __init__(self, names: list[str]) -> None:
        self._names = names
        self._next = 0

    def take(self, n: int) -> list[str]:
        if self._next + n > len(self._names):
            raise ValueError("memcert gen name pool exhausted; enlarge the pool")
        out = self._names[self._next : self._next + n]
        self._next += n
        return out


@dataclass
class Session:
    """One fixture chat session: alternating user/assistant turns on one day."""

    session_id: str
    day: date
    turns: list[tuple[str, str]]  # (role, text)

    def to_jsonl(self) -> str:
        """Stream-json transcript lines (the shape sessions/transcript.py reads)."""
        base = datetime.combine(self.day, time(9, 0))
        lines: list[str] = []
        for i, (role, text) in enumerate(self.turns):
            stamp = (base + timedelta(minutes=3 * i)).isoformat() + "Z"
            entry = {
                "type": role,
                "message": {"role": role, "content": [{"type": "text", "text": text}]},
                "timestamp": stamp,
            }
            lines.append(json.dumps(entry, separators=(",", ":")))
        return "\n".join(lines) + "\n"


@dataclass
class World:
    """A generated fixture world: sessions + items + private audit metadata."""

    seed: int
    scale: str
    split: str
    sessions: list[Session]
    _items: list[Item]
    #: item_id -> audit metadata (e.g. MEM-A source phrasing). NEVER written
    #: into the fixture tree; exported only via --emit-answers.
    audit: dict[str, dict[str, Any]]

    def items(self) -> list[Item]:
        return list(self._items)

    def approx_tokens(self) -> int:
        """Corpus size estimate: transcript text chars / 4."""
        chars = sum(len(text) for s in self.sessions for _, text in s.turns)
        return chars // 4

    def answer_rows(self) -> list[dict[str, Any]]:
        """Answer-spec rows for the protected store (--emit-answers only)."""
        return [
            {
                "item_id": item.item_id,
                "answer_spec": item.answer_spec.to_json(),
                "audit": self.audit.get(item.item_id, {}),
            }
            for item in self._items
        ]

    def write_fixtures(self, out_dir: Path, run_uuid: str | None = None) -> Path:
        """Write sessions/, items.jsonl, world-meta.json. No answers, ever.

        Every emitted file starts with the canary: jsonl files carry a
        ``{"type": "canary", "text": ...}`` comment record; world-meta.json is
        a single line whose first key is the canary text.
        """
        out_dir = Path(out_dir)
        rid = run_uuid or uuid.uuid4().hex
        canary = json.dumps(
            {"type": "canary", "text": canary_line(rid)}, separators=(",", ":")
        )
        sess_dir = out_dir / "sessions"
        sess_dir.mkdir(parents=True, exist_ok=True)
        for session in self.sessions:
            path = sess_dir / f"{session.session_id}.jsonl"
            path.write_text(canary + "\n" + session.to_jsonl(), encoding="utf-8")
        item_lines = [canary] + [
            json.dumps(item.public_json(), separators=(",", ":")) for item in self._items
        ]
        (out_dir / "items.jsonl").write_text("\n".join(item_lines) + "\n", encoding="utf-8")
        per_axis: dict[str, int] = {}
        for item in self._items:
            per_axis[item.axis] = per_axis.get(item.axis, 0) + 1
        days = [s.day for s in self.sessions]
        meta = {
            "canary": canary_line(rid),
            # seed hash only — cert-split raw seeds live in the protected
            # store, never in fixture output (DESIGN §4).
            "seed_sha256": hashlib.sha256(str(self.seed).encode("utf-8")).hexdigest(),
            "scale": self.scale,
            "split": self.split,
            "counts": {
                "sessions": len(self.sessions),
                "main_sessions": sum(
                    1 for s in self.sessions if s.session_id.startswith("main-")
                ),
                "distractor_sessions": sum(
                    1 for s in self.sessions if s.session_id.startswith("dist-")
                ),
                "turns": sum(len(s.turns) for s in self.sessions),
                "items": len(self._items),
                "per_axis": dict(sorted(per_axis.items())),
                "approx_tokens": self.approx_tokens(),
            },
            "virtual_dates": {
                "start": min(days).isoformat(),
                "end": max(days).isoformat(),
            },
        }
        # Single-line JSON so the canary text sits on the file's first line.
        (out_dir / "world-meta.json").write_text(
            json.dumps(meta, separators=(",", ":")) + "\n", encoding="utf-8"
        )
        return out_dir


class _Builder:
    """Deterministic world construction: every draw via rng_for(seed, name)."""

    def __init__(self, seed: int, scale: str, split: str) -> None:
        if scale not in SCALES:
            raise ValueError(f"unknown scale {scale!r}; expected one of {sorted(SCALES)}")
        if split not in ("dev", "cert"):
            raise ValueError(f"unknown split {split!r}; expected dev or cert")
        self.seed = seed
        self.seed_sha = hashlib.sha256(str(seed).encode("utf-8")).hexdigest()
        self.scale = scale
        self.split = split
        self.n_main, self.n_dist = SCALES[scale]

        self._seen: set[str] = set()
        self.people = _Pool(_make_names(rng_for(seed, "names.people"), 30, 3, True, self._seen))
        self.projects = _Pool(
            _make_names(rng_for(seed, "names.projects"), 56, 3, True, self._seen)
        )
        self.services = _Pool(
            _make_names(rng_for(seed, "names.services"), 44, 3, True, self._seen)
        )
        self.machines = _Pool(
            _make_names(rng_for(seed, "names.machines"), 40, 4, True, self._seen)
        )
        self.tokens = _Pool(_make_names(rng_for(seed, "names.tokens"), 40, 3, False, self._seen))

        # Distractor entity slices — confusable surface vocabulary lands on
        # these so distractor statements never contradict real facts.
        self.d_people = self.people.take(4)
        self.d_projects = self.projects.take(6)
        self.d_services = self.services.take(6)
        self.d_machines = self.machines.take(4)

        rng_numbers = rng_for(seed, "numbers")
        self.ports = _Pool([str(p) for p in rng_numbers.sample(range(4000, 5000), 20)])
        self.d_budget_values = _Pool(
            [str(b) for b in rng_numbers.sample(range(50000, 60000), 9)]
        )
        self.caps = _Pool([str(c) for c in rng_numbers.sample(range(7000, 8000), 3)])

        self.main_days = [BASE_DATE + timedelta(days=2 * i) for i in range(self.n_main)]
        self.dist_days = [BASE_DATE + timedelta(days=2 * j + 1) for j in range(self.n_dist)]
        self.main_statements: list[list[str]] = [[] for _ in range(self.n_main)]
        self.dist_statements: list[list[str]] = [[] for _ in range(self.n_dist)]

        self.items: list[Item] = []
        self.audit: dict[str, dict[str, Any]] = {}
        self._axis_seq: dict[str, int] = {}

    # -- helpers ----------------------------------------------------------

    def _msid(self, i: int) -> str:
        return f"main-{i + 1:02d}"

    def _dsid(self, j: int) -> str:
        return f"dist-{j + 1:02d}"

    def _place(self, i: int, text: str) -> None:
        self.main_statements[i].append(text)

    def _place_dist(self, j: int, text: str) -> None:
        self.dist_statements[j].append(text)

    def _add(
        self,
        axis: str,
        level: int,
        question: str,
        spec: AnswerSpec,
        scope: tuple[str, ...],
        overrides: dict[str, Any] | None = None,
        audit: dict[str, Any] | None = None,
        evidence: tuple[str, ...] = (),
    ) -> None:
        """``evidence`` is the minimal set of source statements sufficient to
        answer the item (memcert v2 sufficiency metric, DESIGN-v2.md §4). It is
        AUDIT-side data: never serialized into fixtures — the grader re-derives
        it from the seed exactly like answer specs. Abstain items carry none."""
        if evidence:
            audit = {**(audit or {}), "evidence": list(evidence)}
        seq = self._axis_seq.get(axis, 0) + 1
        self._axis_seq[axis] = seq
        # Cert-split ids must NOT carry the raw seed: the seed IS the answer
        # key (grade-time re-derivation, DESIGN §4/§7), and items.jsonl is arm-
        # visible. Dev splits keep the raw seed for debuggability. Found in the
        # 2026-08-12 build review (gen builder flag).
        wtag = f"w{self.seed}" if self.split == "dev" else f"wh{self.seed_sha[:10]}"
        item_id = f"MEM-{axis}{level}-{seq:02d}-{wtag}"
        self.items.append(
            Item(
                item_id=item_id,
                axis=axis,
                level=level,
                split=self.split,
                question=question,
                answer_spec=spec,
                session_scope=scope,
                cluster_id=f"world-{wtag}",
                arm_overrides=overrides or {},
            )
        )
        if audit:
            self.audit[item_id] = audit

    # -- axes -------------------------------------------------------------

    def axis_a(self) -> None:
        """Retrieval under distraction; probes share no content words with
        their source sentence (NoLiMa control) — source kept in audit meta."""
        rng = rng_for(self.seed, "axis.A")
        # L1: backup location, stated exactly once.
        svcs = self.services.take(3)
        machs = self.machines.take(3)
        conf_machs = self.machines.take(4)
        for svc, mach in zip(svcs, machs, strict=True):
            i = rng.randrange(self.n_main)
            src = f"For the record, {svc} keeps its nightly copies on {mach}."
            self._place(i, src)
            q = f"Which machine holds the backup archives produced by {svc}?"
            self._add(
                "A", 1, q, AnswerSpec("exact", mach), (self._msid(i),),
                audit={"source_phrasing": src}, evidence=(src,),
            )
            j = rng.randrange(self.n_dist)
            self._place_dist(
                j,
                f"Backup archives for {rng.choice(self.d_services)} are held on "
                f"{rng.choice(conf_machs)}.",
            )
        # L2: project steward, with wrong-person confusables near the project.
        projs = self.projects.take(3)
        stewards = self.people.take(3)
        for proj, person in zip(projs, stewards, strict=True):
            i = rng.randrange(self.n_main)
            src = f"Day-to-day care of {proj} sits with {person}."
            self._place(i, src)
            q = f"Who is responsible for looking after the {proj} project?"
            self._add(
                "A", 2, q, AnswerSpec("exact", person), (self._msid(i),),
                audit={"source_phrasing": src}, evidence=(src,),
            )
            j = rng.randrange(self.n_dist)
            self._place_dist(
                j,
                f"{rng.choice(self.d_people)} fielded a couple of visitor questions "
                f"about {proj} last week.",
            )
        # L3: service port, with same-template confusables on distractor services.
        for svc in self.services.take(2):
            port = self.ports.take(1)[0]
            i = rng.randrange(self.n_main)
            src = f"{svc} answers requests at {port}."
            self._place(i, src)
            q = f"Which network port number is used to reach {svc}?"
            self._add(
                "A", 3, q,
                AnswerSpec("exact", port, aliases=(f"port {port}",)),
                (self._msid(i),),
                audit={"source_phrasing": src}, evidence=(src,),
            )
            for _ in range(2):
                j = rng.randrange(self.n_dist)
                conf_port = self.ports.take(1)[0]
                self._place_dist(
                    j, f"{rng.choice(self.d_services)} answers requests at {conf_port}."
                )

    def axis_b(self) -> None:
        """Multi-session integration: answer derivable only from a join of
        facts stated in two different sessions."""
        rng = rng_for(self.seed, "axis.B")
        people = self.people.take(8)
        projs = self.projects.take(8)
        machs = self.machines.take(8)
        levels = (1, 1, 1, 2, 2, 2, 3, 3)
        for person, proj, mach, level in zip(people, projs, machs, levels, strict=True):
            if level == 1:
                a = rng.randrange(self.n_main - 1)
                b = a + 1
            else:
                a = rng.randrange(2)
                b = a + 4 + rng.randrange(2)
            ev_a = f"{person} leads the {proj} effort."
            ev_b = f"{proj} runs its workloads on {mach}."
            self._place(a, ev_a)
            self._place(b, ev_b)
            if level == 3:
                mid = (a + b) // 2
                self._place(
                    mid,
                    f"{rng.choice(self.d_people)} also filed some notes about {proj} "
                    "this week.",
                )
            q = f"Which machine carries the workloads of the project that {person} leads?"
            self._add(
                "B", level, q, AnswerSpec("exact", mach), (self._msid(a), self._msid(b)),
                evidence=(ev_a, ev_b),
            )

    def axis_c(self) -> None:
        """Temporal ordering: which-first (L1), as-of (L2), chronology (L3)."""
        rng = rng_for(self.seed, "axis.C")
        offsets = rng.sample(range(5, 80), 12)
        oi = 0
        # L1: which happened first (two dated past events, same session).
        for svc, proj in zip(self.services.take(3), self.projects.take(3), strict=True):
            d1 = BASE_DATE - timedelta(days=offsets[oi])
            d2 = BASE_DATE - timedelta(days=offsets[oi + 1])
            oi += 2
            i = rng.randrange(self.n_main)
            ev_1 = f"Reminder: the {svc} cutover happened on {d1.isoformat()}."
            ev_2 = f"The {proj} kickoff took place on {d2.isoformat()}."
            self._place(i, ev_1)
            self._place(i, ev_2)
            first = f"the {svc} cutover" if d1 < d2 else f"the {proj} kickoff"
            q = f"Which happened first: the {svc} cutover or the {proj} kickoff?"
            spec = AnswerSpec("exact", first, aliases=(first.removeprefix("the "),))
            self._add("C", 1, q, spec, (self._msid(i),), evidence=(ev_1, ev_2))
        # L2: as-of a mid-session date, before a later change.
        contacts = self.people.take(6)
        for k, svc in enumerate(self.services.take(3)):
            p1, p2 = contacts[2 * k], contacts[2 * k + 1]
            a, mid, b = sorted(rng.sample(range(self.n_main), 3))
            ev_1 = f"As of today, {p1} is the on-call contact for {svc}."
            ev_2 = f"{p2} has taken over as the on-call contact for {svc}."
            self._place(a, ev_1)
            self._place(b, ev_2)
            q = (
                f"As of {self.main_days[mid].isoformat()}, who was the on-call contact "
                f"for {svc}?"
            )
            # As-of items are answerable only when the EVIDENCE TURNS' dates are
            # visible in context (the statements carry no in-text dates) — the
            # sufficiency grader requires these alongside the statements, so an
            # unstamped context can never falsely certify temporal ordering
            # (gemini-critic F1, 2026-08-13).
            self._add(
                "C", 2, q, AnswerSpec("exact", p1), (self._msid(a), self._msid(b)),
                evidence=(ev_1, ev_2),
                audit={
                    "evidence_dates": [
                        self.main_days[a].isoformat(),
                        self.main_days[b].isoformat(),
                    ]
                },
            )
        # L3: order three dated events scattered across three sessions.
        projs = self.projects.take(2)
        svcs = self.services.take(4)
        for k in range(2):
            labels = [
                f"the {svcs[2 * k]} upgrade",
                f"the {projs[k]} freeze",
                f"the {svcs[2 * k + 1]} audit",
            ]
            dates = [BASE_DATE - timedelta(days=offsets[oi + n]) for n in range(3)]
            oi += 3
            idxs = sorted(rng.sample(range(self.n_main), 3))
            ev_lines: list[str] = []
            for (label, d), i in zip(zip(labels, dates, strict=True), idxs, strict=True):
                line = f"For the timeline: {label} wrapped on {d.isoformat()}."
                ev_lines.append(line)
                self._place(i, line)
            ordered = [label for _, label in sorted(zip(dates, labels, strict=True))]
            q = (
                "List in chronological order, earliest first and comma-separated: "
                f"{labels[1]}, {labels[2]}, {labels[0]}."
            )
            self._add(
                "C", 3, q, AnswerSpec("ordered", ordered),
                tuple(self._msid(i) for i in idxs),
                evidence=tuple(ev_lines),
            )

    def axis_d(self) -> None:
        """Knowledge update: current value asked while superseded values are
        mentioned more often (the MEM-D asymmetry)."""
        rng = rng_for(self.seed, "axis.D")
        # L1: weekday change, v1 mentioned twice vs v2 once.
        for svc in self.services.take(3):
            w1, w2 = rng.sample(_WEEKDAYS, 2)
            a, m, b = sorted(rng.sample(range(self.n_main), 3))
            ev_new = f"Deploys for {svc} now run on {w2} instead."
            self._place(a, f"Deploys for {svc} run on {w1}.")
            self._place(m, f"The {w1} deploy slot for {svc} is holding steady.")
            self._place(b, ev_new)
            q = f"On which day of the week do deploys for {svc} currently run?"
            spec = AnswerSpec(
                "exact", w2, aliases=(w2[:-1], f"on {w2}"), stale_values=(w1, w1[:-1])
            )
            self._add(
                "D", 1, q, spec, (self._msid(a), self._msid(m), self._msid(b)),
                evidence=(ev_new,),
            )
        # L2: budget v1->v2->v3 with mentions 3/2/1 (old strictly more often).
        for proj in self.projects.take(3):
            v1, v2, v3 = self.d_budget_values.take(3)
            a, b, c, d, e, f = sorted(rng.sample(range(self.n_main), 6))
            self._place(a, f"The monthly budget for {proj} is {v1} dollars.")
            self._place(b, f"We are still tracking to the {v1} dollar budget for {proj}.")
            self._place(c, f"The monthly budget for {proj} was revised to {v2} dollars.")
            self._place(
                d, f"Reminder: the {v2} dollar budget for {proj} covers contractor time too."
            )
            ev_new = (
                f"The monthly budget for {proj} is now {v3} dollars, superseding earlier "
                "figures."
            )
            self._place(e, ev_new)
            self._place(
                f, f"Some dashboards still show the old {v1} dollar figure for {proj}; "
                "ignore them.",
            )
            q = f"What is the current monthly budget for {proj}, in dollars?"
            spec = AnswerSpec(
                "exact", v3,
                aliases=(f"{v3} dollars",),
                stale_values=(v1, v2, f"{v1} dollars", f"{v2} dollars"),
            )
            scope = tuple(self._msid(i) for i in (a, b, c, d, e, f))
            self._add("D", 2, q, spec, scope, evidence=(ev_new,))
        # L3: machine reassignment v1->v2->v3 with stale chatter after updates.
        for svc in self.services.take(2):
            m1, m2, m3 = self.machines.take(3)
            a, b, c, d, e, f = sorted(rng.sample(range(self.n_main), 6))
            self._place(a, f"{svc} is pinned to {m1}.")
            self._place(b, f"Runbooks still list {m1} as the home of {svc}.")
            self._place(c, f"{svc} moved to {m2} over the weekend.")
            self._place(d, f"Capacity notes: {m2} carries {svc} for now.")
            ev_new = f"{svc} moved again and now sits on {m3}."
            self._place(e, ev_new)
            self._place(f, f"Old tickets that reference {m1} for {svc} can be closed.")
            q = f"Which machine currently hosts {svc}?"
            spec = AnswerSpec("exact", m3, stale_values=(m1, m2))
            scope = tuple(self._msid(i) for i in (a, b, c, d, e, f))
            self._add("D", 3, q, spec, scope, evidence=(ev_new,))

    def axis_e(self) -> None:
        """Abstention: plausible-but-never-stated attributes of real entities
        (the entity appears in sessions; the asked fact never does)."""
        rng = rng_for(self.seed, "axis.E")
        for svc in self.services.take(3):
            i, j = sorted(rng.sample(range(self.n_main), 2))
            self._place(i, f"Dashboards for {svc} stayed green all week.")
            self._place(j, f"A routine restart of {svc} completed cleanly.")
            q = f"Which network port number is used to reach {svc}?"
            self._add(
                "E", 1, q, AnswerSpec("abstain", ABSTAIN_TOKEN),
                (self._msid(i), self._msid(j)),
            )
        for proj in self.projects.take(3):
            i, j = sorted(rng.sample(range(self.n_main), 2))
            self._place(i, f"Standup notes touched on {proj} briefly.")
            self._place(j, f"The {proj} milestones were reviewed without changes.")
            q = f"What is the current monthly budget for {proj}, in dollars?"
            self._add(
                "E", 2, q, AnswerSpec("abstain", ABSTAIN_TOKEN),
                (self._msid(i), self._msid(j)),
            )
        for proj in self.projects.take(2):
            i = rng.randrange(self.n_main)
            self._place(i, f"A short demo of {proj} ran during the open hour.")
            j = rng.randrange(self.n_dist)
            self._place_dist(
                j,
                f"Day-to-day care of {rng.choice(self.d_projects)} sits with "
                f"{rng.choice(self.d_people)}.",
            )
            q = f"Who is responsible for looking after the {proj} project?"
            self._add("E", 3, q, AnswerSpec("abstain", ABSTAIN_TOKEN), (self._msid(i),))

    def axis_f(self) -> None:
        """Forgetting hygiene: probe for the current plan after a retraction;
        the retracted plan is the stale (-1.0) answer."""
        rng = rng_for(self.seed, "axis.F")
        # L1: rollout approach replaced.
        for proj in self.projects.take(3):
            plan_a, plan_b = (t.capitalize() for t in self.tokens.take(2))
            a, m, b = sorted(rng.sample(range(self.n_main), 3))
            ev_new = (
                f"We reverted the {plan_a} plan for {proj}; the new approach is the "
                f"{plan_b} plan."
            )
            self._place(a, f"Rollout approach for {proj}: the {plan_a} plan.")
            self._place(m, f"Progress on the {plan_a} plan for {proj} was reviewed.")
            self._place(b, ev_new)
            q = f"What is the current rollout approach for {proj}? Answer with the plan name."
            spec = AnswerSpec(
                "exact", plan_b,
                aliases=(f"the {plan_b} plan", f"{plan_b} plan"),
                stale_values=(plan_a, f"the {plan_a} plan", f"{plan_a} plan"),
            )
            self._add(
                "F", 1, q, spec, (self._msid(a), self._msid(m), self._msid(b)),
                evidence=(ev_new,),
            )
        # L2/L3: migration announced, referenced, then cancelled. L3 adds a
        # post-retraction archived mention of the dead plan (the trap).
        for k, svc in enumerate(self.services.take(5)):
            level = 2 if k < 3 else 3
            m_old, m_new = self.machines.take(2)
            need = 5 if level == 3 else 4
            idxs = sorted(rng.sample(range(self.n_main), need))
            self._place(idxs[0], f"{svc} currently lives on {m_old}.")
            self._place(idxs[1], f"Plan: {svc} will be migrated to {m_new} next month.")
            self._place(idxs[2], f"Prep for the {svc} move to {m_new} continues.")
            ev_new = f"Update: we cancelled the {svc} migration; it stays on {m_old}."
            self._place(idxs[3], ev_new)
            if level == 3:
                self._place(
                    idxs[4],
                    f"Archived for reference: the old proposal to move {svc} to {m_new}.",
                )
            q = f"Where will {svc} be hosted going forward?"
            spec = AnswerSpec("exact", m_old, stale_values=(m_new,))
            self._add(
                "F", level, q, spec, tuple(self._msid(i) for i in idxs), evidence=(ev_new,)
            )

    def axis_g(self) -> None:
        """Self-learning transfer: phase-1 lessons in early sessions; phase-2
        held-out tasks answerable only by applying the lesson. Each item
        carries real / placebo (token-matched, different seeded world) /
        shuffled (real lessons of other task types) routings."""
        rng = rng_for(self.seed, "axis.G")
        real: list[tuple[str, str, str]] = []  # (bad, good, lesson)
        lesson_sessions: list[int] = []
        for k in range(6):
            bad = _make_names(rng, 1, 3, False, self._seen)[0]
            good = _make_names(rng, 1, 3, False, self._seen)[0]
            lesson = _lesson(bad, good, _G_TASKS[k], _G_REASONS[k])
            real.append((bad, good, lesson))
            i = rng.randrange(max(1, self.n_main // 2))
            self._place(i, lesson)
            lesson_sessions.append(i)
        # Placebo lessons come from a DIFFERENT seeded world: same templates,
        # same-length syllable names, different derived seed.
        placebo_seed = rng_for(self.seed, "axis.G.placebo").getrandbits(63)
        prng = rng_for(placebo_seed, "axis.G")
        placebo_seen = set(self._seen)
        placebo: list[str] = []
        for k in range(6):
            pbad = _make_names(prng, 1, 3, False, placebo_seen)[0]
            pgood = _make_names(prng, 1, 3, False, placebo_seen)[0]
            placebo.append(_lesson(pbad, pgood, _G_PLACEBO_TASKS[k], _G_REASONS[k]))
        projs = self.projects.take(6)
        levels = (1, 1, 2, 2, 3, 3)
        for k in range(6):
            bad, good, lesson = real[k]
            q = (
                f"A new case of {_G_TASKS[k]} has come up for {projs[k]}. "
                "Which approach should be used? Answer with the approach name."
            )
            overrides = {
                "lessons_real": [lesson],
                "lessons_placebo": [placebo[k]],
                "lessons_shuffled": [real[(k + 1) % 6][2]],
            }
            spec = AnswerSpec(
                "exact", good, aliases=(f"the {good} approach", f"{good} approach")
            )
            self._add(
                "G", levels[k], q, spec, (self._msid(lesson_sessions[k]),),
                overrides=overrides,
                audit={"task_type": _G_TASKS[k], "bad": bad, "good": good},
                evidence=(lesson,),
            )

    def axis_h(self) -> None:
        """Memory→action grounding: emit {tool, args} JSON whose required arg
        values are constraints dispersed across >=2 sessions."""
        rng = rng_for(self.seed, "axis.H")
        projs = self.projects.take(8)
        # L1: schedule_job — naming rule + window in two sessions.
        svcs = self.services.take(3)
        prefixes = self.tokens.take(3)
        wins = self.tokens.take(3)
        for k in range(3):
            proj, svc, prefix, win = projs[k], svcs[k], prefixes[k], wins[k]
            a, b = sorted(rng.sample(range(self.n_main), 2))
            ev_a = (
                f"Naming rule for {proj}: every scheduled job is named {prefix}-<service>, "
                "with the service name lowercased."
            )
            ev_b = f"All {proj} jobs must run inside the window called {win}."
            self._place(a, ev_a)
            self._place(b, ev_b)
            q = (
                "Emit the JSON tool call (tool schedule_job, args: name and window) to "
                f"schedule the recurring job for the {svc} service under {proj}."
            )
            value = {
                "tool": "schedule_job",
                "args": {"name": f"{prefix}-{svc.lower()}", "window": win},
            }
            self._add(
                "H", 1, q, AnswerSpec("params", value), (self._msid(a), self._msid(b)),
                evidence=(ev_a, ev_b),
            )
        # L2: set_alert — spend cap + paging contact, sessions far apart.
        contacts = self.people.take(3)
        for k in range(3):
            proj, person = projs[3 + k], contacts[k]
            cap = self.caps.take(1)[0]
            a = rng.randrange(2)
            b = a + 4 + rng.randrange(2)
            ev_a = f"The hard spend cap for {proj} is {cap} dollars."
            ev_b = f"Alerts about {proj} should page {person}."
            self._place(a, ev_a)
            self._place(b, ev_b)
            q = (
                "Emit the JSON tool call (tool set_alert, args: cap and contact) "
                f"implementing the alert policy for {proj}."
            )
            value = {"tool": "set_alert", "args": {"cap": cap, "contact": person}}
            self._add(
                "H", 2, q, AnswerSpec("params", value), (self._msid(a), self._msid(b)),
                evidence=(ev_a, ev_b),
            )
        # L3: provision_host — three constraints across three sessions.
        sizes = self.tokens.take(2)
        zones = self.tokens.take(2)
        tags = self.tokens.take(2)
        for k in range(2):
            proj = projs[6 + k]
            a, b, c = sorted(rng.sample(range(self.n_main), 3))
            ev_a = f"New hosts for {proj} use the {sizes[k]} size class."
            ev_b = f"{proj} hosts are placed in the {zones[k]} zone."
            ev_c = f"Every {proj} host gets the {tags[k]} tag."
            self._place(a, ev_a)
            self._place(b, ev_b)
            self._place(c, ev_c)
            q = (
                "Emit the JSON tool call (tool provision_host, args: size, zone and tag) "
                f"to provision a new {proj} host."
            )
            value = {
                "tool": "provision_host",
                "args": {"size": sizes[k], "zone": zones[k], "tag": tags[k]},
            }
            self._add(
                "H", 3, q, AnswerSpec("params", value),
                (self._msid(a), self._msid(b), self._msid(c)),
                evidence=(ev_a, ev_b, ev_c),
            )

    def distractor_sessions(self) -> None:
        """Fill distractor sessions with fact-shaped statements about
        distractor-pool entities (shared surface vocabulary, zero overlap with
        real answers)."""
        rng = rng_for(self.seed, "distractors")
        for j in range(self.n_dist):
            for _ in range(3):
                t = rng.randrange(5)
                if t == 0:
                    s = (
                        f"For the record, {rng.choice(self.d_services)} keeps its nightly "
                        f"copies on {rng.choice(self.d_machines)}."
                    )
                elif t == 1:
                    s = (
                        f"Day-to-day care of {rng.choice(self.d_projects)} sits with "
                        f"{rng.choice(self.d_people)}."
                    )
                elif t == 2:
                    s = (
                        f"The monthly budget for {rng.choice(self.d_projects)} is "
                        f"{rng.randrange(30000, 40000)} dollars."
                    )
                elif t == 3:
                    s = (
                        f"{rng.choice(self.d_services)} answers requests at "
                        f"{rng.randrange(6000, 7000)}."
                    )
                else:
                    s = (
                        f"Deploys for {rng.choice(self.d_services)} run on "
                        f"{rng.choice(_WEEKDAYS)}."
                    )
                self._place_dist(j, s)

    # -- rendering ----------------------------------------------------------

    def _render_session(
        self, session_id: str, day: date, statements: list[str], kind: str
    ) -> Session:
        rng = rng_for(self.seed, f"render.{session_id}")
        lo, hi = (18, 26) if kind == "main" else (12, 18)
        n = rng.randint(lo, hi)
        slots: list[list[str]] = [[] for _ in range(n)]
        for i, statement in enumerate(statements):
            # Monotonic spread preserves cross-statement ordering.
            slots[i * n // max(1, len(statements))].append(statement)
        mentions = [*self.d_services, *self.d_projects, *self.d_machines]
        turns: list[tuple[str, str]] = []
        for k in range(n):
            role = "user" if k % 2 == 0 else "assistant"
            parts: list[str] = []
            if role == "user":
                parts.append(rng.choice(_USER_OPENERS))
            parts.extend(slots[k])
            for _ in range(rng.randint(4, 6)):
                if rng.random() < 0.4:
                    parts.append(
                        rng.choice(_FILLER_ENTITY).format(e=rng.choice(mentions))
                    )
                else:
                    parts.append(rng.choice(_FILLER_PLAIN))
            turns.append((role, " ".join(parts)))
        return Session(session_id, day, turns)

    def build(self) -> World:
        self.axis_a()
        self.axis_b()
        self.axis_c()
        self.axis_d()
        self.axis_e()
        self.axis_f()
        self.axis_g()
        self.axis_h()
        self.distractor_sessions()
        sessions = [
            self._render_session(self._msid(i), self.main_days[i], self.main_statements[i], "main")
            for i in range(self.n_main)
        ]
        sessions.extend(
            self._render_session(self._dsid(j), self.dist_days[j], self.dist_statements[j], "dist")
            for j in range(self.n_dist)
        )
        return World(
            seed=self.seed,
            scale=self.scale,
            split=self.split,
            sessions=sessions,
            _items=self.items,
            audit=self.audit,
        )


def generate_world(seed: int, scale: str = "S", split: str = "dev") -> World:
    """Build the deterministic fixture world for ``(seed, scale, split)``."""
    return _Builder(seed, scale, split).build()


def _checkout_root(path: Path) -> Path | None:
    """First ancestor (or the path itself) that is a git checkout, else None.

    Walks parents looking for ``.git`` (dir for a clone, file for a worktree)
    — the seed_holdout.py guard idea: answer material must never be writable
    at a nameable in-repo location.
    """
    p = path if path.is_absolute() else Path.cwd() / path
    p = p.resolve()
    for candidate in (p, *p.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _write_answers(world: World, path: Path, run_uuid: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps({"type": "canary", "text": canary_line(run_uuid)}, separators=(",", ":"))
    ]
    lines.extend(json.dumps(row, separators=(",", ":")) for row in world.answer_rows())
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="memcert deterministic fixture-world generator (answers never in-repo)"
    )
    parser.add_argument("--seed", type=int, required=True, help="world seed")
    parser.add_argument("--scale", default="S", choices=sorted(SCALES), help="world scale")
    parser.add_argument("--split", default="dev", choices=("dev", "cert"), help="item split")
    parser.add_argument("--out", type=Path, required=True, help="fixture output directory")
    parser.add_argument(
        "--emit-answers",
        type=Path,
        default=None,
        help=(
            "also write answer specs to this OUT-OF-CHECKOUT path; refused (exit 2) "
            "for any path inside a git checkout"
        ),
    )
    parser.add_argument(
        "--run-uuid",
        default=None,
        help="canary run uuid (default: fresh uuid4; pass explicitly for reproducible bytes)",
    )
    args = parser.parse_args(argv)

    if args.emit_answers is not None:
        root = _checkout_root(args.emit_answers)
        if root is not None:
            print(
                f"refused: --emit-answers path {args.emit_answers} resolves inside the git "
                f"checkout {root}; answer specs must never land in a repository — use an "
                "out-of-checkout protected-store path (do not retry unchanged)",
                file=sys.stderr,
            )
            return 2

    world = generate_world(args.seed, scale=args.scale, split=args.split)
    rid = args.run_uuid or uuid.uuid4().hex
    world.write_fixtures(args.out, run_uuid=rid)
    if args.emit_answers is not None:
        _write_answers(world, args.emit_answers, rid)
    counts = {
        "sessions": len(world.sessions),
        "items": len(world.items()),
        "approx_tokens": world.approx_tokens(),
    }
    print(f"wrote memcert fixtures to {args.out} {json.dumps(counts, separators=(',', ':'))}")
    return 0


__all__ = ["BASE_DATE", "SCALES", "Session", "World", "generate_world", "main"]

if __name__ == "__main__":
    raise SystemExit(main())
