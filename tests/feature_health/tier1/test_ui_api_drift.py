"""Tier1 mechanical UI→API drift test (feature: api_ui).

Enumerates every backend API path the dashboard calls — string literals under
``dashboard/src`` starting with ``/api/`` (Next.js route handlers proxying via
``lib/serverProxy.ts``, ``features/**`` clients, hooks) — and asserts each one
exists in ``contracts/openapi.json``.

Scanner rules (deliberately conservative):

* Only quoted/backticked literals beginning with ``/api/`` are counted; test
  files (``*.test.ts[x]``) and literals containing ``*`` (doc-comment globs
  like the catch-all's ``/api/**``) are ignored.
* Template interpolations ``${...}`` occupying a whole segment become
  ``{param}`` and match any OpenAPI ``{...}`` segment positionally.
* A literal that ENDS in an interpolation (``/api/lab${path}``,
  ``/api/tasks${request.nextUrl.search}``, ``/api/updates${qs(...)}``) is
  open-ended: it may be a prefix builder or a query-string append, so it
  passes when the base is an exact spec path OR a proper prefix of one.
* Next.js app-router API routes that do NOT proxy to the FastAPI backend
  (no ``serverProxy``/``API_BASE`` reference in their ``route.ts``) are the
  dashboard's own endpoints; UI calls matching them are excluded. As of
  2026-07-31 every ``app/api/**/route.ts`` proxies, so this set is empty —
  the exclusion is computed, not assumed, so a future local-only route
  cannot create false phantoms.

EXPECTED RED today (KNOWN-ISSUES FH-002): the dashboard calls the phantom
endpoints ``/api/judges/panel``, ``/api/judges/panel/reseat``,
``/api/judges/stats``, ``/api/judges/votes`` (``features/reliability/api.ts``)
and ``/api/updates`` (``features/skills/api.ts``; flagged only while absent
from the spec). The assertion failure message lists the exact offending paths.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DASH_SRC = _REPO_ROOT / "dashboard" / "src"
_OPENAPI_PATH = _REPO_ROOT / "contracts" / "openapi.json"

# Sentinel substituted for `${...}` template interpolations before extraction.
_INTERP = "\x00interp\x00"

# `${...}` with one non-nested level of inner braces (covers `${qs({ a })}`).
_INTERP_RE = re.compile(r"\$\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}")
_LITERAL_RE = re.compile(r"""["'`](/api/[^"'`\s]*)["'`]""")


def _iter_source_files() -> list[Path]:
    files = [
        path
        for pattern in ("*.ts", "*.tsx")
        for path in _DASH_SRC.rglob(pattern)
        if ".test." not in path.name
    ]
    return sorted(files)


def _normalize(raw: str) -> tuple[str, bool] | None:
    """Return (normalized_path, open_ended) or None when not a countable path."""
    if "*" in raw:
        return None  # doc-comment glob like "/api/**", never a real call
    open_ended = raw.endswith(_INTERP)
    path = raw.split("?", 1)[0].split("#", 1)[0]
    segments: list[str] = []
    for segment in path.strip("/").split("/"):
        if not segment:
            continue
        if segment == _INTERP:
            segments.append("{param}")
        elif _INTERP in segment:
            # Interpolation glued to a literal (e.g. `updates${qs(...)}`):
            # keep the literal part; the glued piece is a query/suffix builder.
            stripped = segment.replace(_INTERP, "")
            segments.append(stripped if stripped else "{param}")
        else:
            segments.append(segment)
    if len(segments) < 2:  # bare "/api" or empty — not a callable backend path
        return None
    return "/" + "/".join(segments), open_ended


def _scan_ui_called_paths() -> dict[str, dict[str, object]]:
    """Map normalized path -> {"open_ended": bool, "files": set[str]}.

    A path is open-ended only if EVERY literal producing it was open-ended;
    one exact-call occurrence makes the whole path require an exact match.
    """
    collected: dict[str, dict[str, object]] = {}
    for source in _iter_source_files():
        text = source.read_text(errors="replace")
        text = _INTERP_RE.sub(_INTERP, text)
        for match in _LITERAL_RE.finditer(text):
            normalized = _normalize(match.group(1))
            if normalized is None:
                continue
            path, open_ended = normalized
            entry = collected.setdefault(path, {"open_ended": True, "files": set()})
            entry["open_ended"] = bool(entry["open_ended"]) and open_ended
            files = entry["files"]
            assert isinstance(files, set)
            files.add(str(source.relative_to(_DASH_SRC)))
    return collected


def _local_next_route_segments() -> list[list[str]]:
    """Segment lists for dashboard-local Next.js API routes (non-proxying)."""
    local: list[list[str]] = []
    app_dir = _DASH_SRC / "app"
    for route_file in sorted((app_dir / "api").rglob("route.ts")):
        text = route_file.read_text(errors="replace")
        if "serverProxy" in text or "API_BASE" in text:
            continue  # proxies to the backend — its target literals are scanned
        segments: list[str] = []
        catch_all = False
        for part in route_file.parent.relative_to(app_dir).parts:
            if part.startswith("[..."):
                catch_all = True
                break
            segments.append("{param}" if part.startswith("[") else part)
        if not catch_all:
            local.append(segments)
    return local


def _segments_match(ui_segments: list[str], spec_segments: list[str]) -> bool:
    if len(ui_segments) != len(spec_segments):
        return False
    return all(
        ui == spec
        or (spec.startswith("{") and spec.endswith("}"))
        or ui == "{param}"
        for ui, spec in zip(ui_segments, spec_segments, strict=True)
    )


@pytest.mark.fh_known_issue(id="FH-002")
def test_every_ui_called_api_path_exists_in_openapi() -> None:
    called = _scan_ui_called_paths()
    # Scanner self-check: the dashboard calls well over 100 distinct backend
    # paths; a collapse here means the scanner broke, not that drift vanished.
    assert len(called) >= 50, (
        f"UI path scanner found only {len(called)} /api/ literals under "
        f"{_DASH_SRC} — scanner or checkout is broken"
    )

    spec_segments = [
        path.strip("/").split("/") for path in json.loads(_OPENAPI_PATH.read_text())["paths"]
    ]
    local_routes = _local_next_route_segments()

    missing: list[str] = []
    for path in sorted(called):
        ui_segments = path.strip("/").split("/")
        if any(_segments_match(ui_segments, local) for local in local_routes):
            continue  # served by the dashboard itself, no backend contract
        exact = any(_segments_match(ui_segments, spec) for spec in spec_segments)
        if exact:
            continue
        if called[path]["open_ended"]:
            # Prefix builder (`/api/lab${path}`): passes if some spec path
            # extends it positionally.
            prefix_hit = any(
                len(spec) > len(ui_segments) and _segments_match(ui_segments, spec[: len(ui_segments)])
                for spec in spec_segments
            )
            if prefix_hit:
                continue
        files = sorted(called[path]["files"])  # type: ignore[arg-type]
        missing.append(f"{path}  (called from: {', '.join(files[:3])})")

    assert not missing, (
        "UI→API drift: the dashboard calls these /api/ paths but "
        "contracts/openapi.json does not define them (known phantoms are "
        "tracked as FH-002 in docs/testing/KNOWN-ISSUES.yaml):\n  "
        + "\n  ".join(missing)
    )
