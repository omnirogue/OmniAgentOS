"""Artifact evidence: prove a render happened WITHOUT asking the thing that rendered it.

Rule E (PLAN-R2-R5-R6): "the verification channel must differ from the actor".
An image generator's own 200 OK, its ``status: "succeeded"`` and its output URL
are all :class:`~omniagentos_loops.contracts.EvidenceGrade.ACTOR_NARRATIVE` —
the actor narrating itself, which is the least trustworthy evidence in this
system and never a verdict. What this module produces instead is grade
``INDEPENDENT_DECODER``: a third-party image parser is asked to open the bytes
on disk and report properties **the loop never asserted**, and the verdict is
that report measured against a post-condition the loop declared BEFORE the call.

The three-part predicate, in order, each falsifiable:

1. the file exists at the path DERIVED FROM THE ARGUMENTS (never from the
   response) and is non-empty;
2. its leading bytes are the magic number of the format the loop asked for — a
   JSON error page saved with a ``.png`` name fails here;
3. an independent decoder opens it and reports pixel dimensions that meet the
   declared minimums.

When no independent decoder is available at all, this module RAISES rather than
downgrading to "the file exists". A predicate that raises is
``EffectStateUnknown`` by ``receipts._judge`` — the effect ran and its outcome
could not be established — which is the fail-closed answer. Silently accepting
grade-1 evidence where grade 2 was promised is precisely the creep Rule E
exists to stop.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from omniagentos_loops.contracts import EvidenceGrade

#: Magic numbers, keyed by the ``output_format`` a loop may declare. Byte
#: prefixes only: the point is to catch "an error page written to image.png",
#: not to re-implement a format detector.
MAGIC: dict[str, tuple[bytes, ...]] = {
    "png": (b"\x89PNG\r\n\x1a\n",),
    "jpg": (b"\xff\xd8\xff",),
    "jpeg": (b"\xff\xd8\xff",),
    "webp": (b"RIFF",),
}

#: Pinned absolute path. ``loop_jobs._worker_env`` sets ``PATH=os.defpath`` —
#: ``/bin:/usr/bin`` on this machine — so a decoder found by name might not be
#: the one review looked at. (The same lesson the plan records about
#: ``scraper`` not being on a loop worker's PATH.)
SIPS = "/usr/bin/sips"


class NoIndependentDecoder(RuntimeError):
    """No third-party decoder is installed, so grade-2 evidence is unobtainable."""


@dataclass(frozen=True)
class ImageFacts:
    """What an independent decoder said. Nothing here came from the actor."""

    path: Path
    size_bytes: int
    width: int
    height: int
    sha256: str
    decoder: str
    grade: EvidenceGrade = EvidenceGrade.INDEPENDENT_DECODER

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "size_bytes": self.size_bytes,
            "width": self.width,
            "height": self.height,
            "sha256": self.sha256,
            "decoder": self.decoder,
            "evidence_grade": int(self.grade),
        }

@dataclass(frozen=True)
class HTMLFacts:
    """What an independent HTML parser said. Nothing here came from the actor."""

    path: Path
    size_bytes: int
    tag_count: int
    is_valid: bool
    sha256: str
    decoder: str
    grade: EvidenceGrade = EvidenceGrade.INDEPENDENT_DECODER

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "size_bytes": self.size_bytes,
            "tag_count": self.tag_count,
            "is_valid": self.is_valid,
            "sha256": self.sha256,
            "decoder": self.decoder,
            "evidence_grade": int(self.grade),
        }


class _HTMLCounter(HTMLParser):
    """Count tags in an HTML document to verify it is non-trivial."""

    def __init__(self) -> None:
        super().__init__()
        self.tag_count = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tag_count += 1


def _parse_html(path: Path) -> tuple[int, str]:
    """Parse HTML and count tags, or raise.

    Returns ``(tag_count, decoder_info)`` if valid HTML is found.
    Raises ``ValueError`` if the content is not valid HTML.
    """
    content = path.read_text(encoding='utf-8', errors='replace')
    if not content.strip():
        raise ValueError(f"{path.name} is empty")

    parser = _HTMLCounter()
    try:
        parser.feed(content)
    except Exception as exc:
        raise ValueError(f"HTML parser failed on {path.name}: {exc}") from exc

    # Require at least one tag for non-triviality
    if parser.tag_count < 1:
        raise ValueError(f"{path.name} contains no HTML tags")

    return parser.tag_count, "html.parser"


def probe_html(path: Path) -> HTMLFacts:
    """Open *path* with the HTML parser and report what it says. May raise.

    Raises ``FileNotFoundError`` / ``ValueError`` for a decisive negative that a
    caller may render as "did not take effect".
    """
    if not path.is_file():
        raise FileNotFoundError(str(path))

    payload = path.read_bytes()
    if not payload:
        raise ValueError(f"{path.name} is zero bytes")

    tag_count, decoder = _parse_html(path)
    return HTMLFacts(
        path=path,
        size_bytes=len(payload),
        tag_count=tag_count,
        is_valid=True,
        sha256=hashlib.sha256(payload).hexdigest(),
        decoder=decoder,
    )



def _decode_dimensions(path: Path) -> tuple[int, int, str]:
    """``(width, height, decoder)`` from a third-party parser, or raise.

    Pillow first (a real decoder, in-process, no subprocess), then Apple's
    ``sips`` (ImageIO), which is the decoder PLAN-R2-R5-R6 nominates for this
    platform.

    The two ways this raises are the absence/failure split again, one level
    down, and they must not be confused:

    * **no decoder is installed at all** — the CHANNEL is missing, grade-2
      evidence is unobtainable, and neither "yes" nor "no" was established:
      :class:`NoIndependentDecoder`, which the receipt guard turns into
      ``EffectStateUnknown``. "We could not look" is not "it is fine".
    * **a decoder looked and said this is not an image** — a decisive negative
      about the artifact: ``ValueError``, which the caller renders as
      ``verified: False`` so the attempt is recorded failed and the retry budget
      applies. Calling that an absence would fail the effect closed forever over
      a file we can positively say is wrong.
    """
    try:
        from PIL import Image  # type: ignore[import-not-found]
    except ImportError:
        pass
    else:
        try:
            with Image.open(path) as image:
                image.verify()
            with Image.open(path) as image:
                width, height = image.size
        except Exception as exc:  # noqa: BLE001 — Pillow's own refusal is a verdict
            raise ValueError(f"PIL could not decode {path.name}: {exc}") from exc
        return int(width), int(height), f"PIL.Image {getattr(Image, '__version__', '')}".strip()

    if shutil.which(SIPS) or Path(SIPS).exists():
        completed = subprocess.run(
            [SIPS, "-g", "pixelWidth", "-g", "pixelHeight", str(path)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if completed.returncode != 0:
            raise ValueError(
                f"{SIPS} could not decode {path.name}: {completed.stderr.strip()[:200]}"
            )
        values: dict[str, int] = {}
        for line in completed.stdout.splitlines():
            key, _, value = line.strip().partition(":")
            key = key.strip()
            if key in ("pixelWidth", "pixelHeight"):
                try:
                    values[key] = int(value.strip())
                except ValueError:
                    continue
        if "pixelWidth" not in values or "pixelHeight" not in values:
            raise ValueError(f"{SIPS} reported no pixel dimensions for {path.name}")
        return values["pixelWidth"], values["pixelHeight"], "sips"

    raise NoIndependentDecoder(
        "no independent image decoder is available (neither Pillow nor sips) — "
        "grade-2 evidence cannot be produced, so this effect's outcome is UNKNOWN"
    )


def probe_image(path: Path, *, expect_format: str) -> ImageFacts:
    """Open *path* with a third-party decoder and report what it says. May raise.

    Raises ``FileNotFoundError`` / ``ValueError`` for a decisive negative that a
    caller may render as "did not take effect", and
    :class:`NoIndependentDecoder` when the channel itself is missing.
    """
    if not path.is_file():
        raise FileNotFoundError(str(path))
    payload = path.read_bytes()
    if not payload:
        raise ValueError(f"{path.name} is zero bytes")
    prefixes = MAGIC.get(expect_format.lower())
    if prefixes is None:
        raise ValueError(f"no magic number is declared for format {expect_format!r}")
    if not any(payload.startswith(prefix) for prefix in prefixes):
        raise ValueError(
            f"{path.name} does not begin with the {expect_format} magic number "
            f"(got {payload[:8]!r})"
        )
    width, height, decoder = _decode_dimensions(path)
    return ImageFacts(
        path=path,
        size_bytes=len(payload),
        width=width,
        height=height,
        sha256=hashlib.sha256(payload).hexdigest(),
        decoder=decoder,
    )


def image_verification(args: Mapping[str, Any], *, instance_id: str) -> dict[str, Any]:
    """The verdict on one rendered image, computed from ARGUMENTS alone.

    ``args`` is the declaration the loop made before the effect ran: which file
    it would create, in what format, and the minimum dimensions it would accept.
    Nothing the renderer returned is read here — that is the property that makes
    this a *different channel* from the actor, and it is pinned mechanically by
    ``tests/test_parent_seam.py::test_image_verdict_ignores_a_fabricated_result``,
    which swaps a glowing fake result in and asserts the verdict is unchanged.

    Returns a mapping with ``verified`` (the verdict), ``evidence_grade`` and
    the decoder's facts. Raising propagates: a channel that could not answer is
    ``EffectStateUnknown``, never a pass.
    """
    from omniagentos_loops import parent_seam

    name = str(args.get("artifact_name") or "")
    expect_format = str(args.get("output_format") or "png")
    min_width = int(args.get("expect_min_width") or 1)
    min_height = int(args.get("expect_min_height") or 1)
    path = parent_seam.artifact_path(parent_seam.var_dir(), instance_id, name)

    try:
        facts = probe_image(path, expect_format=expect_format)
    except NoIndependentDecoder:
        raise
    except (FileNotFoundError, OSError, ValueError) as exc:
        return {
            "verified": False,
            "detail": f"{type(exc).__name__}: {exc}",
            "evidence_grade": int(EvidenceGrade.INDEPENDENT_DECODER),
            "path": str(path),
        }

    if facts.width < min_width or facts.height < min_height:
        return {
            "verified": False,
            "detail": (
                f"{facts.decoder} decoded {facts.width}x{facts.height}, below the declared "
                f"minimum {min_width}x{min_height}"
            ),
            **facts.as_dict(),
        }
    return {
        "verified": True,
        "detail": (
            f"{facts.decoder} decoded {facts.width}x{facts.height} from {facts.size_bytes} bytes"
        ),
        **facts.as_dict(),
    }




def html_verification(args: Mapping[str, Any], *, instance_id: str) -> dict[str, Any]:
    """The verdict on one rendered HTML artifact, computed from ARGUMENTS alone.

    ``args`` is the declaration the loop made before the effect ran: which file
    it would create and what format to expect. Nothing the renderer returned is
    read here — that is the property that makes this a *different channel* from
    the actor.

    Returns a mapping with ``verified`` (the verdict), ``evidence_grade`` and
    the parser's facts. Raising propagates: a channel that could not answer is
    ``EffectStateUnknown``, never a pass.
    """
    from omniagentos_loops import parent_seam

    name = str(args.get("artifact_name") or "")
    path = parent_seam.artifact_path(parent_seam.var_dir(), instance_id, name)

    try:
        facts = probe_html(path)
    except (FileNotFoundError, OSError, ValueError) as exc:
        return {
            "verified": False,
            "detail": f"{type(exc).__name__}: {exc}",
            "evidence_grade": int(EvidenceGrade.INDEPENDENT_DECODER),
            "path": str(path),
        }

    return {
        "verified": True,
        "detail": f"html.parser found {facts.tag_count} tag(s) from {facts.size_bytes} bytes",
        **facts.as_dict(),
    }

__all__ = [
    "MAGIC",
    "SIPS",
    "ImageFacts",
    "HTMLFacts",
    "NoIndependentDecoder",
    "html_verification",
    "image_verification",
    "probe_html",
    "probe_image",
]
