"""Version-controlled capability registry for capmap.

Each capability is a JSON document so it can be reviewed independently of the
runtime state.  This module deliberately does not infer health: an entry must
declare its exit/status semantics before the store can call it healthy.
"""

import json
import os

CAPABILITIES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "capabilities")
REQUIRED_FIELDS = (
    "id", "company", "kind", "host", "repo", "what_it_does", "why_it_matters",
    "blast_radius", "verification", "exit_semantics", "cadence_seconds",
    "staleness_slo_seconds", "escalation_tier", "owner", "last_verified",
    "last_status",
)
VALID_COMPANIES = {"omniagentos", "initech", "globex", "acmeuni", "hooli", "estate"}
VALID_KINDS = {"mechanical-automation", "llm-loop", "external-service", "data-store", "human-process"}
VALID_SEMANTICS = {"ok", "degraded", "cannot_evaluate", "down"}
VALID_VERIFICATION_TYPES = {"command", "snapshot_field", "unset"}


def _validate(capability, source):
    """Raise ValueError when a registry document cannot safely be evaluated."""
    missing = [name for name in REQUIRED_FIELDS if name not in capability]
    if missing:
        raise ValueError("{} missing required field(s): {}".format(source, ", ".join(missing)))
    if not capability.get("id"):
        raise ValueError(f"{source} has an empty id")
    if capability["company"] not in VALID_COMPANIES:
        raise ValueError("{} has invalid company {!r}".format(source, capability["company"]))
    if capability["kind"] not in VALID_KINDS:
        raise ValueError("{} has invalid kind {!r}".format(source, capability["kind"]))
    verification = capability["verification"]
    if not isinstance(verification, dict) or verification.get("type") not in VALID_VERIFICATION_TYPES:
        raise ValueError(f"{source} has invalid verification spec")
    semantics = capability["exit_semantics"]
    if not isinstance(semantics, dict):
        raise ValueError(f"{source} exit_semantics must be a dict")
    invalid = [value for value in semantics.values() if value not in VALID_SEMANTICS]
    if invalid:
        raise ValueError(f"{source} has invalid exit_semantics value(s): {invalid}")
    if verification["type"] == "command" and "argv" not in verification and "command" not in verification:
        raise ValueError(f"{source} command verification needs argv or command")
    if verification["type"] == "snapshot_field":
        if not verification.get("source") or not verification.get("field"):
            raise ValueError(f"{source} snapshot_field verification needs source and field")
    for field in ("cadence_seconds", "staleness_slo_seconds"):
        if not isinstance(capability[field], (int, float)) or capability[field] <= 0:
            raise ValueError(f"{source} {field} must be a positive number")


def load_registry(dir=CAPABILITIES_DIR):
    """Load and validate every ``*.json`` capability document in *dir*."""
    entries = []
    seen = set()
    try:
        filenames = sorted(name for name in os.listdir(dir) if name.endswith(".json"))
    except OSError as exc:
        raise ValueError(f"cannot read capability directory {dir}: {exc}") from None
    for filename in filenames:
        path = os.path.join(dir, filename)
        try:
            with open(path, encoding="utf-8") as fh:
                capability = json.load(fh)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError(f"cannot load {path}: {exc}") from None
        if not isinstance(capability, dict):
            raise ValueError(f"{path} must contain a JSON object")
        _validate(capability, path)
        if capability["id"] in seen:
            raise ValueError("duplicate capability id {!r}".format(capability["id"]))
        seen.add(capability["id"])
        entries.append(capability)
    return entries


def get(id, dir=CAPABILITIES_DIR):
    """Return a capability by id, or raise KeyError."""
    for capability in load_registry(dir):
        if capability["id"] == id:
            return capability
    raise KeyError(id)


def filter(company=None, kind=None, dir=CAPABILITIES_DIR):
    """Return registry entries matching the supplied optional dimensions."""
    return [capability for capability in load_registry(dir)
            if (company is None or capability["company"] == company)
            and (kind is None or capability["kind"] == kind)]
