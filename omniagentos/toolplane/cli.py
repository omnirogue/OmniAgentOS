"""Console entry point for the OmniAgentOS toolplane."""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

from .manifest import ManifestValidationError, load_manifest
from .observe import emit_observation, get_sink, observe_call
from .scrub import scrub_value
from .tools import dispatch


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="omniagentos-tool")
    parser.add_argument("tool_name", nargs="?")
    parser.add_argument("--manifest")
    parser.add_argument("--args-json", default="{}")
    return parser


def _read_request(argv: list[str]) -> tuple[str, Any, dict[str, Any]]:
    parsed = _parser().parse_args(argv)
    if parsed.tool_name:
        if not parsed.manifest:
            raise ManifestValidationError(
                "invalid_request", "--manifest is required with a tool name"
            )
        try:
            args = json.loads(parsed.args_json)
        except json.JSONDecodeError as exc:
            raise ManifestValidationError("invalid_request", "--args-json must be JSON") from exc
        if not isinstance(args, dict):
            raise ManifestValidationError("invalid_request", "--args-json must be an object")
        return parsed.tool_name, parsed.manifest, args
    try:
        request = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError) as exc:
        raise ManifestValidationError(
            "invalid_request", "stdin must contain one JSON object"
        ) from exc
    if not isinstance(request, dict) or not isinstance(request.get("tool"), str):
        raise ManifestValidationError("invalid_request", "stdin request requires a tool")
    args = request.get("args", {})
    if not isinstance(args, dict) or "manifest" not in request:
        raise ManifestValidationError(
            "invalid_request", "stdin request requires manifest and object args"
        )
    return request["tool"], request["manifest"], args


def main(argv: list[str] | None = None) -> int:
    """Run one tool and print exactly one scrubbed JSON object."""
    try:
        tool_name, raw_manifest, args = _read_request(sys.argv[1:] if argv is None else argv)
        manifest = load_manifest(raw_manifest)
    except ManifestValidationError as exc:
        print(json.dumps(scrub_value({"ok": False, "error": exc.error, "detail": exc.detail})))
        return 2
    started = time.monotonic()
    result = dispatch(tool_name, manifest, args)
    duration_ms = int((time.monotonic() - started) * 1000)
    observation = observe_call(tool_name, manifest, result, duration_ms)

    # M-08: the observation is recorded durably, not merely printed. Each CLI
    # invocation is its own process, so it also drains the on-disk spool left by
    # any earlier invocation whose write failed (M-28 restart recovery).
    emit_observation(observation)
    try:
        get_sink().retry_pending()
    except OSError:  # pragma: no cover - retry is best-effort, never fatal
        pass

    output = scrub_value({**result, "observation": observation})
    print(json.dumps(output, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
