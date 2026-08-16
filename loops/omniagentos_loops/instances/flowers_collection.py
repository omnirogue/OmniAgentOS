"""``flowers_collection`` — four flowers, rendered by Replicate, filed for the operator.

Built on the shape ``render_probe`` proves: the worker DECLARES a typed effect,
the parent performs it with a credential this process never sees, and a
third-party decoder — not the renderer — says whether it happened.

WHAT WAS HERE BEFORE, AND WHY IT COULD NOT RUN
----------------------------------------------

This module previously registered four ``LoopTool``s named ``render_rose`` …
``render_blue_rose``, each passing ``capability="replicate.generate"`` to a
constructor that has no such parameter. ``register(ctx)`` therefore raised
``TypeError`` on the first tool, so the instance had NEVER been in a registry.
Underneath that, the four ``generate_*`` functions returned a dict of their own
arguments and called nothing: even with registration repaired, no byte would
ever have reached Replicate. That is the mechanical reason an earlier worker's
"output" was four fabricated gradients — fabricating was the only way this code
could put a file on disk.

Two rules from ``render_probe`` are restored here and must stay:

* the capability is named AT CALL TIME to :func:`parent_seam.request_effect`;
  the GRANT lives in ``loop_effects.INSTANCE_CAPABILITIES``, in source, and a
  ``LoopTool`` never carries one;
* :func:`verify_render` takes ``(result, args)`` — two positionals, the shape
  ``receipts._judge`` calls — and reads ONLY ``args``. Reading *result* would be
  the bug: it is the actor's own account of itself.

ONE ``act``, FOUR EFFECTS — THE DESIGN CHOICE, AND ITS COST
------------------------------------------------------------

The four flowers are rendered by ONE parametrised ``act`` tool driven by an item
brief, not by four near-identical ``LoopTool`` registrations. Four registrations
would be dead code: the templates in this repo invoke ``poll``/``classify``/
``act``/``verify``, so nothing would ever call ``render_rose``, and the module
would look finished and do nothing — which is exactly the failure being repaired.

Each flower is still its own EFFECT: its own :func:`parent_seam.request_effect`
call, its own parent-side grant check, its own budget reservation, its own
``broker_calls`` audit row, its own artifact and its own independent decode.

BE CLEAR ABOUT WHAT IS NOT PER-FLOWER: the idempotency RECEIPT. A receipt is
minted by the template's effect node (``templates.common.add_effect``), one per
node execution, and ``poll_classify_act_verify`` has exactly one such node —
so this tick's four effects share one receipt keyed on the collection. Four
receipts would require a template with four effect nodes, which does not exist
and is not this change's to invent. The tick must render all four anyway,
because the objective gate (``tests/test_flowers_gate.py``) demands four files
from the run it judges; a one-flower-per-tick loop would settle ADVERSE three
times before the collection was complete and trip the auto-pause floor.

What is preserved without the per-flower receipt:

* **partial failure is visible** — ``act``'s result carries one entry per flower
  with its own state and detail, and the verification predicate names exactly
  which flowers are missing;
* **a retry does not pay twice** — before declaring a render, :func:`act` asks
  the SAME independent-decoder channel the verifier uses whether the artifact it
  is about to buy is already on disk and already meets the post-condition
  declared before the call. If it is, that flower is skipped and no money moves;
  the next attempt buys only what is genuinely missing.

That skip is honest about its own limit: a worker that wrote a plausible file
into the artifact directory itself would suppress the render. It could do that
with or without this check — the artifact directory is worker-readable and the
worker runs as the operator — so the skip adds no fabrication surface. The
gate's colour-histogram heuristic is the answer to fabrication; this is the
answer to double billing.

WHY THE ARTIFACT NAME CARRIES THE DAY
--------------------------------------

``<flower>-<YYYY-MM-DD>.png`` in ``var/loops/artifacts/``, filed as
``<flower>.png`` in the operator's dated directory. A day-less name would make
the skip above permanent: on day two every artifact from day one is still on
disk, every flower would be skipped, and yesterday's images would be filed into
today's directory while the gate certified them green. That is precisely the
defect ``grandfather_clock_html``'s docstring records paying for — a dead loop
settling FAVOURABLE forever — and the day in the name is what makes "today's
collection" a statement with content.

WHAT THE PROMPTS ARE, AND WHERE THEY LIVE
------------------------------------------

In SOURCE, in :data:`PROMPTS`, never model-authored and never read from the
routine row. A row is data; ``loop_effects.INSTANCE_CAPABILITIES`` states the
same rule for grants and for the same reason. Params may NARROW the collection
(``{"flowers": ["rose"]}``) and may name a filing ``destination``, and that is
the whole of their influence: the business key stays a pure function of the
flowers plus their prompt digests plus the day, so it does not drift between
ticks.

TIER
----

``act`` is T1 / ``sandboxed_creation`` — the class ``configs/connectors.yaml``
gives ``replicate.generate`` and the only class the parent seam will perform
unattended. It was declared T2 here before, which parks for a human on every
tick and allows exactly one attempt: a four-image collection would have needed
an approval click it was never going to get. The approval floor is not weakened
by this — the same seam call declared at T2 still parks, and
``tests/test_parent_seam.py::test_a_t2_seam_tool_still_parks_for_a_human``
measures it — and the spend is bounded twice over: by
:data:`MAX_SPEND_USD` here, before anything is declared, and by the parent's own
budget reservation per call.
"""

from __future__ import annotations

import hashlib
import os
import secrets
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from omniagentos.contracts import ActionClass
from omniagentos_loops import parent_seam
from omniagentos_loops.artifacts import image_verification
from omniagentos_loops.contracts import RiskTier
from omniagentos_loops.parent_seam import artifact_path, var_dir
from omniagentos_loops.tools import LoopTool, Verification

#: Instance id this module is registered under. It must match the key in
#: ``omniagentos.scheduler.loop_effects.INSTANCE_CAPABILITIES``: the parent
#: resolves the grant by ``instance_id``, and a mismatch is denial, not an error.
INSTANCE_ID = "flowers_collection"

#: ``~/omniagentos-output`` — the operator's declared output tree.
OPERATOR_OUTPUT_SUBPATH = ("Work", "OmniAgentOS", "Development")

OUTPUT_DIR_PREFIX = "Flowers-Collection-"

#: The collection, in the order the operator asked for it. Declared in SOURCE:
#: no prompt is model-authored and none is read from a routine row.
FLOWER_ORDER: tuple[str, ...] = ("rose", "tulip", "sunflower", "blue_rose")

PROMPTS: dict[str, str] = {
    "rose": (
        "A beautiful red rose in full bloom, macro photography, soft lighting, "
        "dewdrops on petals, vibrant details, high quality 4K image"
    ),
    "tulip": (
        "A stunning yellow tulip in full bloom, garden photography, natural sunlight, "
        "sharp focus on petals, vibrant colors, high quality 4K image"
    ),
    "sunflower": (
        "A radiant golden sunflower with a large yellow bloom, head facing towards bright "
        "sunlight, bee visiting the center, vibrant and cheerful, high quality 4K image"
    ),
    "blue_rose": (
        "A rare and beautiful blue rose in full bloom, soft romantic lighting, delicate "
        "petals with blue hues, garden setting, ethereal elegance, high quality 4K image"
    ),
}

#: Kept under its historical name so anything importing it still resolves.
FLOWERS = PROMPTS

#: The model. A closed choice here AND on the parent side
#: (``loop_effects.REPLICATE_MODELS``): a model id is code someone else wrote
#: that we are billed for.
REPLICATE_MODEL = "black-forest-labs/flux-schnell"

#: Declared post-condition, minted BEFORE the call and never sent upstream, so
#: the API cannot move the goalposts it is graded against. flux-schnell at 1:1
#: returns 1024x1024; 512 is a deliberately loose floor because a verifier's job
#: is to catch "not an image" and "not the thing we asked for", not to
#: re-specify the model.
DEFAULT_MIN_EDGE = 512

#: What one flux-schnell image costs at the outside. Same figure the parent
#: reserves in ``loop_effects._estimate_cost_for_capability``; restated here
#: because this side has to decide whether to DECLARE the work at all.
ESTIMATED_USD_PER_IMAGE = 0.10

#: Hard per-tick spend ceiling for this instance. Four images at the estimate
#: above is $0.40; the ceiling leaves headroom without leaving a blank cheque.
#: Params may lower it and may not raise it.
MAX_SPEND_USD = 1.00


# --------------------------------------------------------------------------
# Naming: what is on disk, and where
# --------------------------------------------------------------------------


def operator_output_root() -> Path:
    """``~/omniagentos-output`` — the default filing destination."""
    return Path.home().joinpath(*OPERATOR_OUTPUT_SUBPATH)


def output_dir_name(day: date) -> str:
    """``Flowers-Collection-YYYY-MM-DD`` for *day*."""
    return f"{OUTPUT_DIR_PREFIX}{day.isoformat()}"


def flowers_day(moment: datetime | None = None) -> date:
    """The calendar day of *moment* in UTC (or now).

    UTC rather than local: the directory name is read back by the gate, by the
    verification predicate and by the manual tick harness, and all three must
    derive it the same way from the same instant. Every caller in this repo goes
    through this function for exactly that reason.
    """
    if moment is None:
        return datetime.now(UTC).date()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).date()


def artifact_name(flower: str, day: date) -> str:
    """``<flower>-<YYYY-MM-DD>.png`` — the name declared to the parent seam.

    The day is in the name so "already rendered" means "already rendered TODAY".
    See the module docstring: a day-less name makes yesterday's images look like
    today's work forever.
    """
    return f"{flower}-{day.isoformat()}.png"


def filed_name(flower: str) -> str:
    """``<flower>.png`` — the name in the operator's dated directory.

    Undated, because the DIRECTORY carries the date and the gate reads
    ``<dir>/rose.png``.
    """
    return f"{flower}.png"


# --------------------------------------------------------------------------
# The brief. Data in, typed declaration out.
# --------------------------------------------------------------------------


def _requested(params: Mapping[str, Any]) -> tuple[str, ...]:
    """Which flowers this tick briefs. Params may NARROW, never widen.

    Only a MISSING (or non-list) ``flowers`` key means "the whole collection".
    An explicit list means exactly what it says, including the empty list, which
    briefs nothing and idles the tick — the alternative reading ("empty means
    everything") turns a row that meant *stop* into four paid renders.

    An unknown name is dropped rather than raising: the authority question ("may
    this loop render a flower nobody declared?") is already answered by the fact
    that only :data:`PROMPTS` can supply a prompt, and a row naming garbage
    should idle, not fail.
    """
    raw = params.get("flowers")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return FLOWER_ORDER
    wanted = {str(name) for name in raw}
    return tuple(name for name in FLOWER_ORDER if name in wanted)


def _brief(flower: str, day: date) -> dict[str, Any]:
    """The typed declaration for ONE flower. Nothing here came from a model."""
    return {
        "flower": flower,
        "model": REPLICATE_MODEL,
        "prompt": PROMPTS[flower],
        "artifact_name": artifact_name(flower, day),
        "filed_name": filed_name(flower),
        "output_format": "png",
        "aspect_ratio": "1:1",
        "expect_min_width": DEFAULT_MIN_EDGE,
        "expect_min_height": DEFAULT_MIN_EDGE,
    }


def _render_args(brief: Mapping[str, Any]) -> dict[str, Any]:
    """Exactly the arguments ``replicate.generate`` accepts, and nothing else.

    ``flower`` and ``filed_name`` are loop state, not capability arguments, so
    they are dropped here: what crosses the wire is a declaration, never a
    passthrough. The parent validates this shape again against ``_REPLICATE_ARGS``.
    """
    return {
        "model": brief["model"],
        "prompt": brief["prompt"],
        "artifact_name": brief["artifact_name"],
        "output_format": brief["output_format"],
        "aspect_ratio": brief["aspect_ratio"],
        "expect_min_width": brief["expect_min_width"],
        "expect_min_height": brief["expect_min_height"],
    }


def _collection_id(flowers: Sequence[str], day: date) -> str:
    """The business key: these flowers, these prompts, this day.

    A pure function of the collection — the flower names paired with the digests
    of the prompts that will be paid for — plus the calendar day. Re-running the
    same collection on the same day replays the receipt instead of buying four
    more images; an edited prompt is a genuinely different effect and gets its
    own key; tomorrow is a new collection.
    """
    material = [(name, hashlib.sha256(PROMPTS[name].encode("utf-8")).hexdigest()[:12])
                for name in flowers]
    digest = hashlib.sha256(repr(material).encode("utf-8")).hexdigest()[:12]
    return f"flowers:{day.isoformat()}:{digest}"


def _destination_root(source: Mapping[str, Any] | None) -> Path:
    """The filing root named by *source*, or the operator's declared tree.

    Explicit rather than constant so a caller — a test, the manual tick, a
    future operator preference — names the tree it is filing into, and so
    nothing can be proved by writing into the operator's real delivery folder.
    """
    destination = (source or {}).get("destination")
    if destination:
        return Path(str(destination)).expanduser()
    return operator_output_root()


def _day_of(item: Mapping[str, Any]) -> date:
    """The day this item was briefed for, from the item — never from ``now``.

    A tick that starts at 23:59:59 and files at 00:00:01 must not file into two
    directories, and the verification predicate must look where ``act`` wrote.
    """
    raw = str(item.get("day") or "")
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return flowers_day()


# --------------------------------------------------------------------------
# Nodes
# --------------------------------------------------------------------------


def poll(params: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """The brief for this tick: ONE item, the day's collection.

    One item because ``poll_classify_act_verify`` acts on the head of the list
    and the gate judges the whole collection. The four per-flower declarations
    ride inside it.
    """
    params = dict(params or {})
    day = flowers_day()
    names = _requested(params)
    if not names:
        return []

    try:
        cap = float(params.get("max_spend_usd") or MAX_SPEND_USD)
    except (TypeError, ValueError):
        cap = MAX_SPEND_USD
    destination = params.get("destination")

    return [
        {
            "id": _collection_id(names, day),
            "day": day.isoformat(),
            "flowers": [_brief(name, day) for name in names],
            "destination": str(destination) if destination else "",
            # min(): a row may lower the ceiling and may not raise it.
            "max_spend_usd": min(cap, MAX_SPEND_USD),
        }
    ]


def classify(item: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Render the collection, or skip when there is nothing to render."""
    briefs = list((item or {}).get("flowers") or [])
    return {"action": "render" if briefs else "skip", "count": len(briefs)}


def _already_rendered(brief: Mapping[str, Any]) -> tuple[bool, str]:
    """Is the artifact this brief would buy already on disk and already right?

    Asked through the SAME channel the verifier uses — an independent decoder
    reading a path derived from the arguments — so "we already have it" means
    the same thing here as it does at settlement. Only a file that meets the
    post-condition declared before the call counts; a truncated download or an
    error page saved with a ``.png`` name is re-rendered, not skipped.

    ``NoIndependentDecoder`` is deliberately NOT caught: a file exists that we
    cannot grade, and buying a second copy of an image we may already own is the
    wrong way to resolve that. It propagates, the receipt records the effect's
    state as unknown, and the tick parks for a human.
    """
    args = _render_args(brief)
    path = artifact_path(var_dir(), INSTANCE_ID, args["artifact_name"])
    if not path.is_file():
        return False, ""
    verdict = image_verification(args, instance_id=INSTANCE_ID)
    detail = str(verdict.get("detail") or "")
    return bool(verdict.get("verified")), detail


def write_without_following_symlinks(target: Path, payload: bytes) -> None:
    """Write *payload* AT *target*, replacing a symlink rather than its target.

    ``Path.write_bytes`` opens for writing and FOLLOWS a symlink, so a
    pre-planted ``rose.png -> ~/.ssh/authorized_keys`` in the dated directory
    would redirect this loop's write to that file. No privilege boundary is
    crossed — the loop runs as the operator — but it runs UNATTENDED ON A TIMER
    into a date-predictable path, which is exactly when the redirection would
    not be noticed. Writing a temp sibling and :func:`os.replace`-ing it over
    the name fixes it: ``rename(2)`` operates on the LINK, never on what it
    points at. The atomic swap is a bonus with its own value — the gate never
    observes a half-written image.

    The same reasoning, and the same shape, as
    ``grandfather_clock_html.write_without_following_symlinks``; that one writes
    text, this one bytes.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{secrets.token_hex(8)}.tmp")
    try:
        tmp.write_bytes(payload)
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)


def act(**kwargs: Any) -> dict[str, Any]:
    """Render the collection through the parent seam, then FILE it. T1 effect.

    Two writes make up the effect, and both are load-bearing:

    * the parent writes ``<var>/loops/artifacts/flowers_collection/<name>`` when
      it performs ``replicate.generate`` — the artifact convention every
      verification predicate in this repo derives from arguments alone;
    * this function copies each verified artifact into
      ``<destination>/Flowers-Collection-<day>/<flower>.png``, which is the
      directory the objective gate reads ONLY when ``destination`` is unset. The
      gate grades the operator tree unconditionally and does not follow this
      param, so a non-default ``destination`` is for the MANUAL HARNESS ONLY --
      under a real routine it means the gate cannot see this run's work.

    The second write lives HERE, in the effect, and not in a verification
    predicate or a hand-run script. When it lived outside the loop, production
    ticks never wrote the gated directory at all and the gate certified files no
    run had produced — the defect ``grandfather_clock_html`` records.

    This function holds no credential and names no URL, header or directory.
    """
    item = dict(kwargs.get("item") or {})
    briefs = [dict(brief) for brief in item.get("flowers") or []]
    if not briefs:
        return {"success": False, "error": "act was called with no brief"}

    day = _day_of(item)
    root = _destination_root(item)
    filed_dir = root / output_dir_name(day)
    try:
        ceiling = float(item.get("max_spend_usd") or MAX_SPEND_USD)
    except (TypeError, ValueError):
        ceiling = MAX_SPEND_USD
    ceiling = min(ceiling, MAX_SPEND_USD)

    # Ask what is already paid for BEFORE deciding what this tick may spend, so
    # a retry is priced on what it will actually buy.
    presence = [_already_rendered(brief) for brief in briefs]
    outstanding = sum(1 for present, _ in presence if not present)
    estimate = round(outstanding * ESTIMATED_USD_PER_IMAGE, 4)
    if estimate > ceiling:
        return {
            "success": False,
            "error": (
                f"refusing to declare {outstanding} render(s) at an estimated ${estimate} "
                f"against this tick's ${ceiling} ceiling"
            ),
            "estimated_usd": estimate,
            "max_spend_usd": ceiling,
        }

    results: list[dict[str, Any]] = []
    for brief, (present, presence_detail) in zip(briefs, presence, strict=True):
        flower = str(brief["flower"])
        entry: dict[str, Any] = {"flower": flower, "artifact_name": brief["artifact_name"]}

        if present:
            entry.update(state="already_rendered", ok=True, detail=presence_detail)
        else:
            answer = parent_seam.request_effect(
                INSTANCE_ID, parent_seam.REPLICATE_GENERATE, _render_args(brief)
            )
            if answer.get("success") is False:
                entry.update(
                    state="refused",
                    ok=False,
                    detail=str(answer.get("error") or "the seam refused this render"),
                )
                results.append(entry)
                continue
            entry.update(
                state="rendered",
                ok=True,
                # ACTOR NARRATIVE. Recorded for the operator's audit trail and
                # read by nothing that grades this effect.
                prediction_id=str(answer.get("prediction_id") or ""),
                bytes=answer.get("bytes"),
            )

        source = artifact_path(var_dir(), INSTANCE_ID, str(brief["artifact_name"]))
        try:
            payload = source.read_bytes()
        except OSError as exc:
            entry.update(state="artifact_unreadable", ok=False, detail=f"{type(exc).__name__}: {exc}")
            results.append(entry)
            continue

        target = filed_dir / str(brief["filed_name"])
        try:
            write_without_following_symlinks(target, payload)
        except OSError as exc:
            entry.update(state="filing_failed", ok=False, detail=f"{type(exc).__name__}: {exc}")
            results.append(entry)
            continue

        entry.update(artifact_path=str(source), filed_path=str(target), size_bytes=len(payload))
        results.append(entry)

    failed = [str(entry["flower"]) for entry in results if not entry.get("ok")]
    return {
        # A partial collection is a FAILED effect: the receipt records the
        # attempt as failed, the next attempt buys only what is missing, and the
        # verify node renders the tick's status.
        "success": not failed,
        "error": f"flowers that did not render: {', '.join(failed)}" if failed else "",
        "day": day.isoformat(),
        "filed_dir": str(filed_dir),
        "flowers": results,
        "estimated_usd": estimate,
    }


def collection_verdict(item: Mapping[str, Any] | None) -> dict[str, Any]:
    """Grade the collection from the ARTIFACTS. Nothing the renderer said is read.

    Three questions per flower, each falsifiable and each answered by a channel
    the renderer does not control:

    1. does an independent decoder open the artifact at the path derived from
       the arguments and report dimensions meeting the minimums this loop
       declared BEFORE the call (``artifacts.image_verification``);
    2. is the operator's copy present at the path derived from the same
       arguments, and byte-identical to the artifact;
    3. are the four files distinct — four copies of one image is a collection
       that was never rendered.

    Raising propagates: a channel that could not answer is ``EffectStateUnknown``,
    never a pass.
    """
    item = dict(item or {})
    briefs = [dict(brief) for brief in item.get("flowers") or []]
    if not briefs:
        return {"verified": False, "state": "no_brief", "detail": "no collection was briefed"}

    day = _day_of(item)
    filed_dir = _destination_root(item) / output_dir_name(day)

    flowers: list[dict[str, Any]] = []
    digests: set[str] = set()
    failures: list[str] = []

    for brief in briefs:
        flower = str(brief["flower"])
        args = _render_args(brief)
        verdict = image_verification(args, instance_id=INSTANCE_ID)
        entry: dict[str, Any] = {
            "flower": flower,
            "verified": bool(verdict.get("verified")),
            "detail": str(verdict.get("detail") or ""),
            "path": str(verdict.get("path") or ""),
            "width": verdict.get("width"),
            "height": verdict.get("height"),
            "sha256": verdict.get("sha256"),
        }
        if not entry["verified"]:
            failures.append(f"{flower}: {entry['detail']}")
            flowers.append(entry)
            continue

        digest = str(verdict.get("sha256") or "")
        if digest:
            digests.add(digest)

        target = filed_dir / str(brief["filed_name"])
        entry["filed_path"] = str(target)
        if not target.is_file():
            entry["verified"] = False
            entry["detail"] = f"not filed to {target}"
            failures.append(f"{flower}: not filed to {target}")
        elif hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            entry["verified"] = False
            entry["detail"] = f"filed copy at {target} differs from the artifact"
            failures.append(f"{flower}: filed copy differs from the artifact")
        flowers.append(entry)

    if not failures and len(digests) != len(briefs):
        failures.append(
            f"{len(briefs)} flowers produced {len(digests)} distinct image(s) — "
            "a collection of copies is not a collection"
        )

    verified = not failures
    return {
        "verified": verified,
        "state": "collection_complete" if verified else "collection_incomplete",
        "filed_dir": str(filed_dir),
        "day": day.isoformat(),
        "flowers": flowers,
        "detail": (
            f"{len(briefs)} distinct images decoded and filed to {filed_dir}"
            if verified
            else "; ".join(failures)
        ),
    }


def verify_render(_result: Any, args: Mapping[str, Any]) -> Verification:
    """``LoopTool.verify`` for ``act``. Two positionals, and *result* is ignored.

    ``receipts._judge`` calls this as ``tool.verify(result, dict(args))``.
    ``_result`` is named with a leading underscore because reading it would be
    the bug: it is the actor's own account of itself. Everything this predicate
    needs — which files, what format, what minimum size, which directory they
    were filed into — is in ``args``, which was minted before the call and is
    covered by the approval's ``args_digest``.
    """
    verdict = collection_verdict(dict(args.get("item") or {}))
    return Verification(ok=bool(verdict.get("verified")), detail=str(verdict.get("detail") or ""))


def verify(**kwargs: Any) -> dict[str, Any]:
    """The verify NODE: a second, independent read of the same artifacts.

    Separate from :func:`verify_render` on purpose. That predicate decides
    whether the RECEIPT may be marked succeeded; this decides the TICK's status
    through ``templates.common.verification_outcome``. Both read the files on
    disk, neither reads the API, and neither reads ``act``'s result — which is
    passed in as ``result`` and deliberately unused.
    """
    return collection_verdict(dict(kwargs.get("item") or {}))


def _collection_key(args: Mapping[str, Any]) -> str:
    """Business key: this collection, this day.

    Kept exact although the loops runtime never invokes
    ``LoopTool.idempotency_key`` today — ``templates.common.add_effect`` takes
    the key from the TEMPLATE's ``key_fn``, which for
    ``poll_classify_act_verify`` is the item's ``id``, i.e. the same
    :func:`_collection_id` value this returns. A key that is dead today is a
    trap tomorrow.
    """
    item = dict((args or {}).get("item") or {})
    key = str(item.get("id") or "")
    if key:
        return key
    briefs = [str(brief.get("flower") or "") for brief in item.get("flowers") or []]
    known = tuple(name for name in briefs if name in PROMPTS)
    return _collection_id(known or FLOWER_ORDER, _day_of(item))


def register(ctx: Any) -> None:
    """Register this instance's tools. NOTE what is not here: any credential.

    No work happens, no tool is invoked, and no capability is declared on a
    tool. The grant lives in ``loop_effects.INSTANCE_CAPABILITIES``; the
    capability is named at call time inside :func:`act`.
    """
    ctx.tools.register(
        LoopTool(
            name="poll",
            tier=RiskTier.T0,
            idempotency_key=lambda args: "poll",
            call=lambda **kwargs: poll(kwargs.get("params")),
            description="the day's flower collection brief",
        )
    )
    ctx.tools.register(
        LoopTool(
            name="classify",
            tier=RiskTier.T0,
            idempotency_key=lambda args: "classify",
            call=lambda **kwargs: classify(kwargs.get("item")),
            description="render the collection or skip",
        )
    )
    ctx.tools.register(
        LoopTool(
            name="act",
            tier=RiskTier.T1,
            # Declared, not inherited: exactly the class
            # configs/connectors.yaml gives replicate.generate, and the parent
            # seam refuses anything that is not auto-class.
            action_class=ActionClass.SANDBOXED_CREATION,
            idempotency_key=_collection_key,
            call=act,
            verify=verify_render,
            description="render four flowers through the parent credential seam and file them",
        )
    )
    ctx.tools.register(
        LoopTool(
            name="verify",
            tier=RiskTier.T0,
            idempotency_key=lambda args: "verify",
            call=verify,
            description="independent decode of the artifacts this tick claims to have made",
        )
    )


TOOLS = ("poll", "classify", "act", "verify")

__all__ = [
    "DEFAULT_MIN_EDGE",
    "ESTIMATED_USD_PER_IMAGE",
    "FLOWERS",
    "FLOWER_ORDER",
    "INSTANCE_ID",
    "MAX_SPEND_USD",
    "OUTPUT_DIR_PREFIX",
    "PROMPTS",
    "REPLICATE_MODEL",
    "TOOLS",
    "act",
    "artifact_name",
    "classify",
    "collection_verdict",
    "filed_name",
    "flowers_day",
    "operator_output_root",
    "output_dir_name",
    "poll",
    "register",
    "verify",
    "verify_render",
    "write_without_following_symlinks",
]
