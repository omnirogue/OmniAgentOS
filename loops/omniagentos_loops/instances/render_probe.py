"""``render_probe`` — the credential seam's end-to-end control, and its template.

One image per tick, rendered by a real external API through the parent-side
seam, verified by a decoder that never spoke to that API. It exists to make the
whole chain falsifiable in one tick — declaration, parent authorization, broker
call, artifact on disk, independent verification, receipt outcome, settlement —
with a blast radius of one file under ``var/loops/artifacts/``.

It is deliberately NOT the operator's four-flower loop or the clock loop. Those
are built on top of this shape; this is the shape.

WHAT THE WORKER SENDS vs WHAT THE PARENT DECIDES
------------------------------------------------

The worker sends a typed declaration and nothing else::

    {"capability": "replicate.generate",
     "args": {"model": "black-forest-labs/flux-schnell",
              "prompt": "...", "artifact_name": "rose.png",
              "output_format": "png", "aspect_ratio": "1:1",
              "expect_min_width": 512, "expect_min_height": 512}}

No URL, no header, no path, no credential, and no directory: the parent derives
``<var>/loops/artifacts/<instance>/<artifact_name>`` itself, checks the model
against a reviewed allowlist, checks THIS instance's grant, refuses anything
that is not auto-class, resolves ``REPLICATE_API_TOKEN`` through
``connectors.broker`` (so the HTTP method/path allowlist and the ``broker_calls``
audit row both apply), and writes the bytes.

WHY THE VERIFIER IS A DIFFERENT CHANNEL
---------------------------------------

``verify_render`` takes ``(result, args)`` — the signature every ``VerifyFn``
has — and reads ONLY ``args``. The renderer's ``prediction_id``, its
``succeeded`` status and its byte count are the actor narrating itself
(``EvidenceGrade.ACTOR_NARRATIVE``) and grade nothing. The path is re-derived
from the arguments, the magic number must match the format the loop DECLARED,
and a third-party decoder must report dimensions meeting minimums the loop
declared BEFORE the call — which the API was never told and cannot move.
``tests/test_parent_seam.py::test_image_verdict_ignores_a_fabricated_result``
swaps in a glowing fake result and asserts the verdict does not move.

TIERS
-----

``render`` is T1 / ``sandboxed_creation``: it costs money and creates a file in
our own sandbox, and it is not externally visible, so it runs unattended — which
is what makes a four-image tick possible without four approval clicks.

The approval floor is untouched and is not weakened by moving the call: the same
seam call declared at T2 still parks for a human on every tick, and
``tests/test_parent_seam.py::test_a_t2_seam_tool_still_parks_for_a_human``
builds exactly that and measures it.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

from omniagentos.contracts import ActionClass
from omniagentos_loops import parent_seam
from omniagentos_loops.artifacts import image_verification
from omniagentos_loops.contracts import RiskTier
from omniagentos_loops.tools import LoopTool, Verification

#: Instance id this module is registered under. It must match the key in
#: ``omniagentos.scheduler.loop_effects.INSTANCE_CAPABILITIES``: the parent
#: resolves the grant by ``instance_id``, and a mismatch is denial, not an error.
INSTANCE_ID = "render_probe"

#: Default declared post-condition. flux-schnell at 1 megapixel returns
#: 1024x1024 for a 1:1 aspect ratio; 512 is a deliberately loose floor, because
#: a verifier's job is to catch "not an image" and "not the thing we asked for",
#: not to re-specify the model.
DEFAULT_MIN_EDGE = 512


def _brief(params: Mapping[str, Any]) -> dict[str, Any]:
    """The one image this tick renders, taken from the routine row's params.

    The params are DATA — a row is data — so every field is re-derived into a
    typed shape here and the parent validates it again. Nothing from this
    mapping becomes a path, a host, a header or a command.
    """
    prompt = str(params.get("prompt") or "").strip()
    name = str(params.get("artifact_name") or "").strip()
    if not prompt or not name:
        return {}
    return {
        "id": name,
        "model": str(params.get("model") or "black-forest-labs/flux-schnell"),
        "prompt": prompt,
        "artifact_name": name,
        "output_format": str(params.get("output_format") or "png"),
        "aspect_ratio": str(params.get("aspect_ratio") or "1:1"),
        "expect_min_width": int(params.get("expect_min_width") or DEFAULT_MIN_EDGE),
        "expect_min_height": int(params.get("expect_min_height") or DEFAULT_MIN_EDGE),
    }


def _render_args(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "model": item["model"],
        "prompt": item["prompt"],
        "artifact_name": item["artifact_name"],
        "output_format": item["output_format"],
        "aspect_ratio": item["aspect_ratio"],
        "expect_min_width": item["expect_min_width"],
        "expect_min_height": item["expect_min_height"],
    }


def poll(params: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """The brief for this tick. One item, or none."""
    item = _brief(params or {})
    return [item] if item else []


def classify(item: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return {"action": "render" if item else "skip"}


def render(**kwargs: Any) -> dict[str, Any]:
    """Declare the render to the parent. This function holds no credential.

    ``item``/``decision`` is the shape ``poll_classify_act_verify`` hands an act
    tool; the typed capability arguments are rebuilt from ``item`` here so that
    what crosses the wire is a declaration, never a passthrough of loop state.
    """
    item = dict(kwargs.get("item") or {})
    if not item:
        return {"success": False, "error": "render was called with no brief"}
    return parent_seam.request_effect(
        INSTANCE_ID, parent_seam.REPLICATE_GENERATE, _render_args(item)
    )


def verify_render(_result: Any, args: Mapping[str, Any]) -> Verification:
    """Grade the effect from the ARTIFACT, never from the renderer's answer.

    ``_result`` is named with a leading underscore because reading it would be
    the bug: it is the actor's own account of itself. Everything this predicate
    needs — which file, what format, what minimum size — is in ``args``, which
    was minted before the call and is covered by the approval's ``args_digest``.
    """
    item = dict(args.get("item") or {})
    verdict = image_verification(_render_args(item) if item else {}, instance_id=INSTANCE_ID)
    return Verification(ok=bool(verdict.get("verified")), detail=str(verdict.get("detail") or ""))


def verify(**kwargs: Any) -> dict[str, Any]:
    """The template's verify NODE: a second, independent read of the same file.

    Separate from ``LoopTool.verify`` on purpose. That predicate decides whether
    the RECEIPT may be marked succeeded; this decides the TICK's status through
    ``verification_outcome``. Both read the artifact, neither reads the API.
    """
    item = dict(kwargs.get("item") or {})
    if not item:
        return {"verified": False, "state": "no_brief"}
    verdict = image_verification(_render_args(item), instance_id=INSTANCE_ID)
    verdict.setdefault("state", "rendered" if verdict.get("verified") else "artifact_missing")
    return verdict


def _render_key(args: Mapping[str, Any]) -> str:
    """Business key: this instance rendering THIS brief.

    The artifact name plus a digest of the prompt, so re-running the same brief
    replays the receipt instead of paying for a second image, while an edited
    prompt is a genuinely different effect and gets its own key.
    """
    item = dict(args.get("item") or {})
    digest = hashlib.sha256(str(item.get("prompt") or "").encode("utf-8")).hexdigest()[:12]
    return f"{item.get('artifact_name') or 'artifact'}:{digest}"


def register(ctx: Any) -> None:
    """Register this instance's tools. NOTE what is not here: any credential."""
    ctx.tools.register(
        LoopTool(
            name="poll",
            tier=RiskTier.T0,
            idempotency_key=lambda args: "poll",
            call=lambda **kwargs: poll(kwargs.get("params")),
            description="the image brief for this tick",
        )
    )
    ctx.tools.register(
        LoopTool(
            name="classify",
            tier=RiskTier.T0,
            idempotency_key=lambda args: "classify",
            call=lambda **kwargs: classify(kwargs.get("item")),
            description="render or skip",
        )
    )
    ctx.tools.register(
        LoopTool(
            name="act",
            tier=RiskTier.T1,
            # Declared, not inherited: this is exactly the class
            # configs/connectors.yaml gives replicate.generate, and the parent
            # seam refuses anything the broker would call hard-human.
            action_class=ActionClass.SANDBOXED_CREATION,
            idempotency_key=_render_key,
            call=render,
            verify=verify_render,
            description="render one image through the parent credential seam",
        )
    )
    ctx.tools.register(
        LoopTool(
            name="verify",
            tier=RiskTier.T0,
            idempotency_key=lambda args: "verify",
            call=verify,
            description="independent decode of the artifact this tick claims to have made",
        )
    )


TOOLS = ("poll", "classify", "act", "verify")

__all__ = [
    "DEFAULT_MIN_EDGE",
    "INSTANCE_ID",
    "TOOLS",
    "classify",
    "poll",
    "register",
    "render",
    "verify",
    "verify_render",
]
