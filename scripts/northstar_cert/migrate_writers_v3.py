#!/usr/bin/env python3
"""Convert ``configs/northstar-cert/writers.yaml`` from v2 to v3, in place.

v3 changes exactly one thing: for NON-writer mask tokens (``glue:``,
``binding:``, ``launchd:``, ``fs:``, ``provider:``, ``suite:``, ``campaign:``,
and any other colon prefix that is not ``writer:``), ``gates`` stops being a
bare check-id list and becomes a per-check map::

    "glue:fold-legs-pending":
      gates:
        NSC-C01-01: {witness: null, proof: null}
        NSC-C01-02: {witness: null, proof: null}

``witness: null, proof: null`` means NOT LANDED — which is exactly what a bare
list has always meant — so this migration is BEHAVIOR-PRESERVING: every pair
stays unsatisfied until it gains a landed witness and a passing proof. Nothing
is ever removed: the migration refuses if the output would drop a single
token/check pair, because those pairs are the sticky obligation itself.

``writer:`` entries are left untouched: their evidence is token-wide by
construction (the capability lands once for every check the token gates).

Run:  python scripts/northstar_cert/migrate_writers_v3.py [--path P] [--check]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import yaml

DEFAULT_PATH = Path("configs/northstar-cert/writers.yaml")
WRITER_PREFIX = "writer:"
TARGET_VERSION = 3


class MigrationError(RuntimeError):
    """The document is not in a shape this migration can convert safely."""


def _pairs_in(document: dict[str, Any]) -> set[tuple[str, str]]:
    """Every (token, check) pair the document records, in either shape."""

    pairs: set[tuple[str, str]] = set()
    for token, entry in (document.get("tokens") or {}).items():
        if not isinstance(entry, dict):
            continue
        gates = entry.get("gates")
        if isinstance(gates, list | dict):
            pairs.update((token, gate) for gate in gates if isinstance(gate, str))
    return pairs


def migrate_document(document: dict[str, Any]) -> dict[str, Any]:
    """Return the v3 form of ``document`` without dropping a single pair."""

    if not isinstance(document, dict) or not isinstance(document.get("tokens"), dict):
        raise MigrationError("writers.yaml must contain a tokens mapping")
    before = _pairs_in(document)

    migrated: dict[str, Any] = dict(document)
    migrated["version"] = TARGET_VERSION
    tokens: dict[str, Any] = {}
    for token, entry in document["tokens"].items():
        if not isinstance(entry, dict):
            raise MigrationError(f"token {token!r} has no entry mapping")
        if token.startswith(WRITER_PREFIX):
            tokens[token] = entry  # token-wide evidence: unchanged by design
            continue
        gates = entry.get("gates")
        if isinstance(gates, dict):
            tokens[token] = entry  # already migrated; idempotent
            continue
        if not isinstance(gates, list) or not gates:
            raise MigrationError(f"token {token!r} has no gates list to convert")
        if entry.get("witness") is not None or entry.get("proof") is not None:
            # A non-writer token was never allowed token-wide evidence; if one
            # ever appears, converting it would silently spread that evidence
            # across every pair. Refuse instead.
            raise MigrationError(
                f"token {token!r} carries token-wide evidence that cannot be split per pair"
            )
        tokens[token] = {"gates": {gate: {"witness": None, "proof": None} for gate in gates}}
    migrated["tokens"] = tokens

    after = _pairs_in(migrated)
    dropped = sorted(f"{token}@{gate}" for token, gate in before - after)
    if dropped:
        raise MigrationError("migration would drop sticky pairs: " + ", ".join(dropped))
    return migrated


def _scalar(value: str | None) -> str:
    return "null" if value is None else yaml.safe_dump(value, default_flow_style=True).strip()


def render(document: dict[str, Any]) -> str:
    """Render v3 deterministically and COMPACTLY.

    One line per pair, quoted token keys, sorted throughout: this file is a
    Class-A review surface, and a landed pair has to be a one-line diff a human
    can actually read among three hundred null ones.
    """

    lines = [f"version: {document['version']}", "tokens:"]
    for token in sorted(document["tokens"]):
        entry = document["tokens"][token]
        lines.append(f'  "{token}":')
        gates = entry["gates"]
        if isinstance(gates, list):  # writer token: token-wide evidence
            lines.append("    gates: [" + ", ".join(gates) + "]")
            lines.append(f"    witness: {_scalar(entry.get('witness'))}")
            if "proof" in entry:
                lines.append(f"    proof: {_scalar(entry.get('proof'))}")
            continue
        lines.append("    gates:")
        for check_id in sorted(gates):
            pair = gates[check_id] or {}
            witness = _scalar(pair.get("witness"))
            proof = _scalar(pair.get("proof"))
            lines.append(f"      {check_id}: {{witness: {witness}, proof: {proof}}}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", type=Path, default=DEFAULT_PATH)
    parser.add_argument(
        "--check",
        action="store_true",
        help="report whether the file is already v3 and write nothing",
    )
    args = parser.parse_args(argv)

    try:
        document = yaml.safe_load(args.path.read_text(encoding="utf-8"))
        migrated = migrate_document(document)
    except (OSError, yaml.YAMLError, MigrationError) as exc:
        print(f"migration refused: {exc}", file=sys.stderr)
        return 2

    pairs = len(_pairs_in(migrated))
    if args.check:
        already = document == migrated
        print(f"{args.path}: {'already v3' if already else 'needs migration'} ({pairs} pairs)")
        return 0 if already else 1

    header = []
    for line in args.path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("#"):
            break
        header.append(line)
    args.path.write_text("\n".join([*header, render(migrated), ""]), encoding="utf-8")
    print(f"wrote {args.path} at version {TARGET_VERSION} ({pairs} pairs preserved)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
