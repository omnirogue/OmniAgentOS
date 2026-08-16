"""Gate for the flowers_collection loop.

MECHANICAL HEURISTIC VERIFICATION — NOT CRYPTOGRAPHIC PROOF.

GATE DESIGN: Structural Heuristics
===================================
Real Replicate-generated images have properties that fabricated filler lacks:

1. COLOUR-HISTOGRAM SHAPE: real imagery allocates pixels wildly unevenly across
   colours; cheap synthetic filler is a deterministic (x, y) -> colour sweep that
   visits every colour equally and so has a DEAD FLAT histogram. Calibrated
   2026-08-02 against this loop's OWN output: fabricated 0.0000, real
   flux-schnell 3.39-7.79, floor 1.0. (1080p video frames score 14.96-32.96;
   they were the first calibration set and are NOT representative.)

2. MAGIC BYTES & DIMENSIONS: Real images have valid PNG/JPEG structure and
   decodable dimensions. Fabricated gradients pass these trivially, so these are
   INTEGRITY checks (is this a readable image?), never authenticity checks.

3. TWO DISCRIMINATORS WERE MEASURED AND REJECTED. Do not re-derive them:
   * FILE-SIZE VARIANCE — defeated by one line, because bytes appended after a
     PNG's IEND chunk are ignored by every decoder (four identical gradients
     padded to a 234 KB spread beat a 50 KB threshold). It also risked falsely
     condemning four real images that happened to compress alike.
   * LOCAL STRUCTURE (mean neighbour delta) — the fabricated gradients scored
     1.459 while real frames scored 0.63-2.08, so the fakes sat INSIDE the real
     range. It does not separate them at all.

ARCHITECTURAL NOTE — THIS GATE IS INCOMPLETE
=============================================
The parent seam (loop_effects.py:~700-774) OBSERVES the real Replicate
prediction_id and downloaded bytes at the moment of arrival. EffectServer
runs in the scheduler process and is the only party that ever sees both.
It does NOT persist the prediction_id outside the worker-writable idempotency
table. Until it does, no gate can verify authenticity.

THIS GATE CATCHES COMMON FABRICATION PATTERNS BUT IS NOT UNFORGEABLE.

To achieve true provenance:
- Add prediction_id column to broker_calls table (or new parent_audit table)
- In loop_effects.py _handle_replicate_generate (~line 700), record prediction_id
  in broker_calls after successful download
- Gate reads prediction_id from broker_calls (parent-side, worker cannot write)
- Bind: prediction_id + sha256(artifact) = proof of authenticity

Files to change:
  - omniagentos/scheduler/loop_effects.py: Record prediction_id in broker_calls
  - omniagentos/db/store.py or omniagentos/db/migrations/: Add prediction_id column
  - tests/test_flowers_gate.py: Read prediction_id from broker_calls

The loop publishes artifacts to:
  ~/omniagentos-output/Flowers-Collection-YYYY-MM-DD/{rose,tulip,sunflower,blue_rose}.png
"""

import hashlib
import re
import statistics
import subprocess
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.e2e

_OUTPUT_DIR_PREFIX = "Flowers-Collection-"

INSTANCE_ID = "flowers_collection"
FLOWERS = ("rose", "tulip", "sunflower", "blue_rose")

# Heuristic thresholds.
#
# RECALIBRATED 2026-08-02 against this loop's OWN first real output, which is the
# only calibration that counts. The original range came from 1080p video frames
# and was misleading for this gate:
#
#     fabricated colour sweeps       cv = 0.0000        (uniq 65536)
#     video frames (original set)    cv = 14.96-32.96   (uniq ~6000)
#     REAL flux-schnell 1024x1024    cv =  3.39- 7.79   (uniq 223k-297k)
#
# Real generated imagery scores SEVERAL TIMES lower than the video frames
# did, because a diffusion sample spreads a million pixels over ~250k colours
# while a video frame concentrates them in a few thousand. The floor of 1.0 still
# separates cleanly -- the closest real sample (blue_rose, 3.39) clears it by
# 3.4x -- but do NOT raise this floor toward the video-frame range: doing so
# would reject genuine Replicate output. If anything, the honest margin here is
# 3.4x, not the 15x the first calibration implied.
COLOUR_HISTOGRAM_CV_FLOOR = 1.0

#: Must match the loop's own pre-declared post-condition
#: (``flowers_collection.DEFAULT_MIN_EDGE``). A gate floor LOOSER than the
#: condition the loop already promised is a gap: it accepts an artifact the loop
#: itself would have called wrong.
MIN_EDGE_PX = 512



def _destination_root() -> Path:
    """The operator tree. ALWAYS. The gate does not follow a `destination`.

    A previous version read a ``FLOWERS_DESTINATION`` env var, on the theory that
    it mirrored the loop's ``params["destination"]``. That was WRONG in two ways
    at once, and the docstring claiming the two sides "read the same override"
    was simply false:

      * CHANNEL MISMATCH. The loop reads DB routine params; the gate read an
        environment variable. Nothing ever bridged them -- no code anywhere maps
        ``params["destination"]`` to that variable.
      * THE PRODUCTION PATH DELETES IT ANYWAY. Settlement runs the gate under
        ``gate_runner._sanitized_env``, whose allowlist is PATH, HOME, LANG,
        LC_ALL, TMPDIR, SYSTEMROOT plus four fixed keys. ``FLOWERS_DESTINATION``
        is stripped. The "fix" worked only in a developer shell -- exactly the
        environment that is not the one that matters.

    Bridging it properly would need either a worker-written pointer file (which
    is actor-controlled evidence, and therefore not evidence) or widening the
    sanitized-env allowlist (a trust boundary every gate shares). Both are worse
    than removing the divergence.

    So: the gate grades the operator tree, full stop. If a routine row sets a
    ``destination`` elsewhere, the gate does not follow it.

    BE PRECISE ABOUT WHAT THAT MEANS, because an earlier draft of this docstring
    overstated it as "and REFUSES". Usually it does refuse -- there is no
    collection for today in the tree it grades, so it fails loudly, which is the
    honest outcome. But if the operator tree ALREADY holds today's or yesterday's
    collection from an earlier default-path run, the gate grades THAT instead,
    and a tick filing elsewhere can free-ride on it. That is the same residual
    attribution class as the yesterday fallback below: narrow, bounded, and only
    closable by binding artifacts to the run that produced them. It is NOT the
    forever-green hole this replaced, and it is written down here rather than
    left for the next reader to discover.
    """
    return Path.home() / "Work" / "OmniAgentOS" / "Development"


def get_artifacts_dir() -> Path:
    """The directory for THIS run's day -- never "whatever is newest".

    THIS FUNCTION USED TO END ``return candidates[-1]`` AND THAT WAS A HOLE.
    Proven, not theorised: planting
    ``Flowers-Collection-9999-12-31/`` containing four real photographs made the
    gate grade that tree forever, and it returned **16 passed** while the actual
    artifacts for today were fabricated colour sweeps. A dead or lying loop would
    have settled FAVOURABLE indefinitely and the auto-pause floor could never
    trip.

    This is the SAME defect class ``tests/test_grandfather_clock_gate.py``
    documents fixing earlier the same day ("latest dated directory wins"), and it
    was reintroduced here by the same author who had just fixed it there. Worth
    recording plainly: knowing a bug intimately is not the same as not writing it
    again, which is exactly why the gate is not allowed to be written by whoever
    it grades.

    The loop stamps its day with ``flowers_day()`` = ``datetime.now(UTC).date()``
    and names the directory ``Flowers-Collection-<day>``. The gate binds to that
    same rule. Yesterday is accepted ONLY so a tick that renders at 23:59 and
    settles at 00:01 is not condemned for the calendar -- that is a settlement
    boundary, not a licence to read old work. Nothing older, and nothing dated in
    the future, is eligible at any price.

    RESIDUAL, stated rather than hidden: the yesterday fallback buys a dead loop
    ONE free green day -- if it succeeds on day D and no-ops on D+1, the D+1
    settlement can grade D's collection. On D+2 it cannot. That is far narrower
    than the forever-green hole this replaced, but it does soften the auto-pause
    floor by one day, and closing it needs run-level attribution the artifacts do
    not currently carry.
    """
    base_dir = _destination_root()
    today = datetime.now(UTC).date()

    eligible = [today, today - timedelta(days=1)]
    for day in eligible:
        candidate = base_dir / f"{_OUTPUT_DIR_PREFIX}{day.isoformat()}"
        if candidate.is_dir():
            return candidate

    present = sorted(
        d.name for d in base_dir.iterdir()
        if d.is_dir() and re.match(r'^Flowers-Collection-\d{4}-\d{2}-\d{2}$', d.name)
    ) if base_dir.is_dir() else []
    raise FileNotFoundError(
        f"No flowers collection for {today.isoformat()} (or the preceding day) in "
        f"{base_dir}. Directories present: {present or 'none'}. A collection dated "
        f"otherwise is NOT graded -- it is not this run's work, and grading it is "
        f"how a dead loop settles green forever."
    )


def _get_artifact_path(flower_name: str) -> Path:
    """Get the full path to a flower artifact."""
    artifacts_dir = get_artifacts_dir()
    return artifacts_dir / f"{flower_name}.png"


def _is_valid_image(path: Path) -> tuple[bool, str | None]:
    """Check if file has valid image magic bytes. Returns (is_valid, error_message)."""
    try:
        content = path.read_bytes()

        if len(content) == 0:
            return False, "File is empty"

        is_png = content.startswith(b'\x89PNG\r\n\x1a\n')
        is_jpeg = content.startswith(b'\xff\xd8\xff')

        if not (is_png or is_jpeg):
            magic_hex = content[:8].hex() if len(content) >= 8 else content.hex()
            return False, f"Invalid magic bytes: {magic_hex} (not PNG or JPEG)"

        return True, None
    except Exception as e:
        return False, f"Error reading file: {e}"


def _get_image_dimensions(path: Path) -> tuple[int | None, int | None, str | None]:
    """Get image dimensions via sips. Returns (width, height, error_message)."""
    try:
        result = subprocess.run(
            ["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(path)],
            capture_output=True,
            text=True,
            timeout=10,
        )

        if result.returncode != 0:
            if "command not found" in result.stderr.lower():
                return None, None, "sips command not found (macOS required)"
            return None, None, f"sips failed: {result.stderr}"

        # Parse sips output
        width = None
        height = None
        for line in result.stdout.split("\n"):
            if "pixelWidth" in line:
                parts = line.split(":")
                if len(parts) >= 2:
                    try:
                        width = int(parts[1].strip())
                    except ValueError:
                        pass
            if "pixelHeight" in line:
                parts = line.split(":")
                if len(parts) >= 2:
                    try:
                        height = int(parts[1].strip())
                    except ValueError:
                        pass

        if width is None or height is None:
            return None, None, f"Could not parse dimensions from sips output: {result.stdout}"

        return width, height, None

    except FileNotFoundError:
        return None, None, "sips command not found (macOS required)"
    except subprocess.TimeoutExpired:
        return None, None, "sips command timed out"
    except Exception as e:
        return None, None, f"Error getting dimensions: {e}"


def _colour_histogram_cv(path: Path) -> float:
    """Coefficient of variation of the colour-count histogram.

    A photograph or a diffusion-model sample allocates its pixels wildly
    unevenly across colours. Cheap synthetic filler is typically a deterministic
    enumeration of a colour space -- an (x, y) -> colour sweep -- which visits
    every colour the same number of times and so has a DEAD FLAT histogram.
    That flatness is the property being measured; the fabrication this check was
    written against scored exactly 0.0.
    """
    from PIL import Image

    with Image.open(path) as im:
        counts = Counter(im.convert("RGB").getdata()).values()
    counts = list(counts)
    mean = statistics.fmean(counts)
    if mean == 0:  # pragma: no cover - an empty image cannot reach here
        return 0.0
    return statistics.pstdev(counts) / mean


def _colour_histogram_cv_of_image(im: Any) -> float:
    """The same metric as :func:`_colour_histogram_cv`, on an in-memory image.

    Exists so the noise property can be pinned without writing files.
    """
    counts = list(Counter(im.convert("RGB").getdata()).values())
    mean = statistics.fmean(counts)
    return 0.0 if mean == 0 else statistics.pstdev(counts) / mean


def _check_colour_histogram() -> tuple[bool, str | None]:
    """HEURISTIC: reject artifacts whose colour histogram is implausibly flat.

    This REPLACED a file-size-variance check that was both unsound and unsafe,
    and the reasons are worth keeping because they generalise:

      * It was defeated by one line. Bytes appended after a PNG's IEND chunk are
        ignored by every decoder, so padding four identical gradients to a 234 KB
        spread sailed past a 50 KB threshold while the images stayed identical.
      * It risked the OPPOSITE failure, which is worse here. Four real images
        from one model at one resolution can easily land within 50 KB, and a
        false ADVERSE settlement caused by the grader rather than the artifact is
        a failure mode this repo has already paid for.

    A local-structure metric (mean neighbour delta) was measured and REJECTED:
    the fabricated gradients scored 1.459 while real frames scored 0.63-2.08, so
    the fakes sat INSIDE the real range. Recording that here so nobody re-derives
    a discriminator that was already disproven.

    Calibrated 2026-08-02 against six real frames and the fabricated set:

        fabricated (and padded)   cv = 0.0000
        video frames (first set)  cv = 14.96 .. 32.96
        REAL flux-schnell output  cv =  3.39 ..  7.79   <- the one that matters

    The floor sits well below the lowest real sample, so a
    genuine image would have to be ~3.4x flatter than anything measured to trip
    it. Returns (passes_heuristic, reason_if_failed).

    HONEST LIMIT, corrected 2026-08-02 after measuring it. An earlier version of
    this docstring claimed "an adversary defeats it by adding noise". That is
    FALSE, and the correction matters more than the error: noise walks INTO this
    check rather than around it. Measured on this machine:

        uniform RGB noise          cv = 0.0873   REFUSED
        gradient + or- 3 noise     cv = 0.3877   REFUSED
        gradient + full-range      cv = 0.0764   REFUSED
        real photograph            cv = 32.9563  accepted

    The reason is the metric's shape: noise scatters ~262k pixels across ~260k
    distinct colours, so nearly every colour present has a count of 1 and the
    histogram is DEAD FLAT. Real imagery concentrates its pixels into a few
    thousand colours very unevenly. Adding entropy makes filler MORE obviously
    synthetic, not less.

    The real limit is narrower and harder to reach: to pass, a forgery needs a
    genuinely uneven colour distribution -- which in practice means a real image, a
    histogram-matched construction, OR anything else with uneven colour mass --
    a screenshot, a multi-fill geometric drawing, a palette-reduced synthetic. The
    property measured is PLAUSIBILITY OF COLOUR MASS, not photo-likeness. That is a much higher bar
    than "add noise", and it is the bar an operator should assume, because a
    stolen real photograph passes this check trivially and always will.

    So this establishes PLAUSIBILITY, never ORIGIN. Only the parent-side
    provenance described in the module docstring can establish origin.
    """
    artifacts_dir = get_artifacts_dir()
    measured: list[tuple[str, float]] = []

    for flower in FLOWERS:
        path = artifacts_dir / f"{flower}.png"
        if not path.is_file():
            return False, f"{flower}: artifact file not found"
        try:
            measured.append((flower, _colour_histogram_cv(path)))
        except ImportError:
            # Absence of the grader's own dependency is not evidence about the
            # artifact. Say so out loud rather than pass quietly -- a silent
            # downgrade is indistinguishable from a real grade, which is the
            # exact vacuum this gate exists to close.
            return False, (
                "HEURISTIC UNAVAILABLE: Pillow is not importable in the gate "
                "interpreter, so colour-histogram grading could not run. This is "
                "a defect in the grader's environment, not in the artifact -- "
                "install Pillow or grade this artifact by another route."
            )

    detail = ", ".join(f"{flower}={cv:.4f}" for flower, cv in measured)
    flat = [flower for flower, cv in measured if cv < COLOUR_HISTOGRAM_CV_FLOOR]

    if flat:
        return False, (
            f"HEURISTIC FAILED: {', '.join(flat)} have an implausibly flat colour "
            f"histogram (floor {COLOUR_HISTOGRAM_CV_FLOOR}; real imagery measured "
            f"3.39-7.79). A flat histogram means every colour appears about "
            f"equally often, which is what a generated colour sweep looks like and "
            f"what a photograph or diffusion sample never looks like. Measured: {detail}"
        )

    return True, None


# ============================================================================
# TEST SUITE
# ============================================================================


def test_rose_file_exists():
    """Rose artifact must exist and be non-empty."""
    path = _get_artifact_path("rose")
    assert path.is_file(), f"Rose artifact not found at {path}"
    content = path.read_bytes()
    assert len(content) > 0, f"Rose artifact is empty: {path}"


def test_tulip_file_exists():
    """Tulip artifact must exist and be non-empty."""
    path = _get_artifact_path("tulip")
    assert path.is_file(), f"Tulip artifact not found at {path}"
    content = path.read_bytes()
    assert len(content) > 0, f"Tulip artifact is empty: {path}"


def test_sunflower_file_exists():
    """Sunflower artifact must exist and be non-empty."""
    path = _get_artifact_path("sunflower")
    assert path.is_file(), f"Sunflower artifact not found at {path}"
    content = path.read_bytes()
    assert len(content) > 0, f"Sunflower artifact is empty: {path}"


def test_blue_rose_file_exists():
    """Blue rose artifact must exist and be non-empty."""
    path = _get_artifact_path("blue_rose")
    assert path.is_file(), f"Blue rose artifact not found at {path}"
    content = path.read_bytes()
    assert len(content) > 0, f"Blue rose artifact is empty: {path}"


def test_rose_valid_image():
    """Rose must be a valid PNG or JPEG with correct magic bytes."""
    path = _get_artifact_path("rose")
    is_valid, error = _is_valid_image(path)
    assert is_valid, f"Rose image invalid: {error}"


def test_tulip_valid_image():
    """Tulip must be a valid PNG or JPEG with correct magic bytes."""
    path = _get_artifact_path("tulip")
    is_valid, error = _is_valid_image(path)
    assert is_valid, f"Tulip image invalid: {error}"


def test_sunflower_valid_image():
    """Sunflower must be a valid PNG or JPEG with correct magic bytes."""
    path = _get_artifact_path("sunflower")
    is_valid, error = _is_valid_image(path)
    assert is_valid, f"Sunflower image invalid: {error}"


def test_blue_rose_valid_image():
    """Blue rose must be a valid PNG or JPEG with correct magic bytes."""
    path = _get_artifact_path("blue_rose")
    is_valid, error = _is_valid_image(path)
    assert is_valid, f"Blue rose image invalid: {error}"


def test_rose_dimensions():
    """Rose dimensions must be decodable and meet minimum floor."""
    path = _get_artifact_path("rose")
    width, height, error = _get_image_dimensions(path)

    if error and "not found" in error.lower() and "sips" in error.lower():
        raise AssertionError(f"sips unavailable: cannot verify dimensions: {error}")

    assert error is None, f"Rose dimensions error: {error}"
    assert width is not None, "Rose width is None"
    assert height is not None, "Rose height is None"
    assert width >= MIN_EDGE_PX, f"Rose width {width} below minimum {MIN_EDGE_PX}"
    assert height >= MIN_EDGE_PX, f"Rose height {height} below minimum {MIN_EDGE_PX}"


def test_tulip_dimensions():
    """Tulip dimensions must be decodable and meet minimum floor."""
    path = _get_artifact_path("tulip")
    width, height, error = _get_image_dimensions(path)

    if error and "not found" in error.lower() and "sips" in error.lower():
        raise AssertionError(f"sips unavailable: cannot verify dimensions: {error}")

    assert error is None, f"Tulip dimensions error: {error}"
    assert width is not None, "Tulip width is None"
    assert height is not None, "Tulip height is None"
    assert width >= MIN_EDGE_PX, f"Tulip width {width} below minimum {MIN_EDGE_PX}"
    assert height >= MIN_EDGE_PX, f"Tulip height {height} below minimum {MIN_EDGE_PX}"


def test_sunflower_dimensions():
    """Sunflower dimensions must be decodable and meet minimum floor."""
    path = _get_artifact_path("sunflower")
    width, height, error = _get_image_dimensions(path)

    if error and "not found" in error.lower() and "sips" in error.lower():
        raise AssertionError(f"sips unavailable: cannot verify dimensions: {error}")

    assert error is None, f"Sunflower dimensions error: {error}"
    assert width is not None, "Sunflower width is None"
    assert height is not None, "Sunflower height is None"
    assert width >= MIN_EDGE_PX, f"Sunflower width {width} below minimum {MIN_EDGE_PX}"
    assert height >= MIN_EDGE_PX, f"Sunflower height {height} below minimum {MIN_EDGE_PX}"


def test_blue_rose_dimensions():
    """Blue rose dimensions must be decodable and meet minimum floor."""
    path = _get_artifact_path("blue_rose")
    width, height, error = _get_image_dimensions(path)

    if error and "not found" in error.lower() and "sips" in error.lower():
        raise AssertionError(f"sips unavailable: cannot verify dimensions: {error}")

    assert error is None, f"Blue rose dimensions error: {error}"
    assert width is not None, "Blue rose width is None"
    assert height is not None, "Blue rose height is None"
    assert width >= MIN_EDGE_PX, f"Blue rose width {width} below minimum {MIN_EDGE_PX}"
    assert height >= MIN_EDGE_PX, f"Blue rose height {height} below minimum {MIN_EDGE_PX}"


def test_all_four_distinct_flowers():
    """All four distinct flowers must be present (not just four copies of one)."""
    artifacts_dir = get_artifacts_dir()

    flowers_found = set()
    for flower_name in FLOWERS:
        path = artifacts_dir / f"{flower_name}.png"
        if path.is_file():
            content = path.read_bytes()
            file_hash = hashlib.sha256(content).hexdigest()
            flowers_found.add(file_hash)

    assert len(flowers_found) == 4, (
        f"Expected 4 distinct flower files, but found {len(flowers_found)} unique hashes. "
        f"This could mean duplicate files or missing flowers."
    )


def test_colour_histogram_heuristic():
    """HEURISTIC: each artifact's colour histogram must not be implausibly flat.

    This is the check that actually separates real imagery from the synthetic
    filler a lying worker produced. It is a heuristic, not a proof, and the
    module docstring says why proof is unavailable here.

    THRESHOLD: cv >= 1.0, calibrated 2026-08-02 against six real frames
    real flux-schnell output (3.39-7.79) and the fabricated set (0.0000). The
    closest real sample clears the floor by 3.4x -- not the 15x the first
    calibration against video frames implied. Do not raise this floor.

    FALSE POSITIVE RISK: very low, and low in the SAFE direction. A real image
    would have to be ~3.4x flatter than any measured sample to trip this. That
    matters more than usual here: a false ADVERSE settlement caused by the grader
    rather than the artifact is a failure mode this repo has already paid for.

    FALSE NEGATIVE RISK: real, but NOT the one previously claimed here. Noise
    does not defeat this check -- measured, it scores 0.0764-0.3877, well under
    the floor, because scattering pixels across every colour is what "flat"
    means. What defeats it is a genuinely uneven colour distribution: a stolen
    real photograph passes trivially. This check establishes PLAUSIBILITY, never
    ORIGIN. See _check_colour_histogram for the measurements.
    """
    passes, reason = _check_colour_histogram()
    assert passes, reason or "Colour-histogram heuristic failed"


def test_no_model_opinion_in_gate():
    """This gate is mechanical: assert it against the AST, do not claim it in prose.

    This test was a bare ``pass`` -- cosmetic green, and precisely the
    "check that observes nothing" this file exists to prevent. It now parses its
    own module and fails if the gate imports anything that could ask a model for
    an opinion. A model judging the artifact would be the actor narrating itself,
    which is the class of evidence this system refuses.
    """
    import ast

    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    model_sdks = {"openai", "anthropic", "litellm", "transformers", "replicate", "httpx", "requests"}
    offenders = sorted(imported & model_sdks)
    assert not offenders, (
        f"this gate imports {offenders}, which means it could ask a model -- or the "
        f"provider -- what it thinks of the artifact. It must grade the FILE, using "
        f"only local decoding. Replicate's own success response is the actor's "
        f"account of itself and is deliberately not evidence here."
    )


def test_noise_does_not_defeat_the_histogram_check():
    """Pin the correction: adding entropy makes filler MORE detectable, not less.

    A previous docstring claimed an adversary defeats this check by adding
    noise. Measuring it showed the opposite. This test exists so that claim
    cannot come back: if someone "fixes" the metric such that noise starts
    passing, they have broken the property, not improved it.
    """
    import random

    from PIL import Image

    random.seed(7)
    size = 128
    cases = {}

    noise = Image.new("RGB", (size, size))
    noise.putdata([
        (random.randrange(256), random.randrange(256), random.randrange(256))
        for _ in range(size * size)
    ])
    cases["uniform noise"] = noise

    sweep = Image.new("RGB", (size, size))
    sweep.putdata([
        (x % 256, y % 256, 40 + random.randrange(-3, 4))
        for y in range(size) for x in range(size)
    ])
    cases["sweep plus low-amplitude noise"] = sweep

    for label, im in cases.items():
        cv = _colour_histogram_cv_of_image(im)
        assert cv < COLOUR_HISTOGRAM_CV_FLOOR, (
            f"{label} scored cv={cv:.4f}, at or above the floor "
            f"{COLOUR_HISTOGRAM_CV_FLOOR}. Noise is supposed to FAIL this "
            f"check -- scattering pixels across every colour is what a flat "
            f"histogram is. If this now passes, the metric changed meaning."
        )
