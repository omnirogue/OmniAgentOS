"""Versioned, read-only integration surface for external engine consumers.

LoopDeck remains a separate product.  This router exposes a deliberately small
projection over the existing swarm and metacognition authorities; it does not
create work, mutate state, or disclose host filesystem locations.

Adapted from the original LoopDeck engine surface for this branch's focused
contract (``docs/attempt-1-implementation-design.md`` SS2):

- ``include_in_schema=False`` keeps the generated ``contracts/openapi.json``
  artifact (a contractually untouchable path) free of drift.  Because the
  route inventory in ``tests/api/test_auth_posture.py`` walks ``app.openapi()``
  and therefore cannot see these routes, authentication is asserted directly
  in ``tests/api/test_engine_routes.py``.  This is a compensated blind spot,
  not a hidden one.
- ``GET /api/engine/runs`` is the bounded, authoritative run-binding source,
  replacing the original's task-lifecycle binding table (no migration is
  permitted in this contract).
- Snapshots additionally project ``context``/``evidence``/``approval``, each
  fail-closed: values that do not validate become ``None`` rather than being
  echoed raw, and approval is never inferred from artifacts or passing tests.
"""

from __future__ import annotations

import json
import re
from typing import Annotated, Any, Protocol, cast

from fastapi import APIRouter, Depends, Path, Query

from omniagentos.api.deps import StoreDep
from omniagentos.api.routes._receipt_projection import project_receipt_payload
from omniagentos.api.routes.control import _authorized, fail
from omniagentos.api.routes.metacog import get_metacog_service
from omniagentos.api.routes.swarm import get_swarm_dal
from omniagentos.metacog.service import MetacogService
from omniagentos.swarm.dal import SwarmDal
from omniagentos.swarm.scheduler import swarm_execute_enabled
from omniagentos.swarm.worktrees import swarm_worktrees_enabled

API_VERSION = "loopdeck-engine/v1"

router = APIRouter(
    prefix="/api/engine",
    tags=["engine"],
    dependencies=[Depends(_authorized)],
    include_in_schema=False,
)

SwarmDalDep = Annotated[SwarmDal, Depends(get_swarm_dal)]
MetacogDep = Annotated[MetacogService, Depends(get_metacog_service)]


class _EventStore(Protocol):
    def get_events_for_target(
        self, target_type: str, target_id: str, after_id: int = 0, limit: int = 500
    ) -> list[dict[str, Any]]: ...


# --------------------------------------------------------------------------
# Input validation (fail-closed)
# --------------------------------------------------------------------------

#: Applied by FastAPI *before* the handler body, so a malformed identifier can
#: never reach the DAL or be echoed back inside an error envelope.
RUN_ID_PATTERN = r"^[A-Za-z0-9_.-]{1,64}$"

_REPOSITORY_RE = re.compile(r"^[\w.-]+/[\w.-]+$")
_BRANCH_RE = re.compile(r"^[\w./-]{1,255}$")
_HEAD_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")

_GITHUB_URL_PREFIX = "https://github.com/"


def _valid_repository(value: Any) -> str | None:
    return value if isinstance(value, str) and _REPOSITORY_RE.match(value) else None


def _valid_branch(value: Any) -> str | None:
    if not isinstance(value, str) or not _BRANCH_RE.match(value):
        return None
    # Reject traversal and the ambiguous leading/trailing separator forms even
    # though the character class alone would accept them.
    if ".." in value or value.startswith("/") or value.endswith("/"):
        return None
    return value


def _valid_head_sha(value: Any) -> str | None:
    return value if isinstance(value, str) and _HEAD_SHA_RE.match(value) else None


#: A URL longer than this is refused outright (nulled, never truncated — a
#: truncated URL would be a different destination than the one reported).
_MAX_URL_CHARS = 2048


def _github_url(value: Any) -> str | None:
    """Keep only links this product can vouch for; everything else is dropped.

    The surrounding entry is preserved so the UI still shows the evidence, it
    just renders it as inert text instead of a navigable link.
    """
    if not isinstance(value, str) or not value.startswith(_GITHUB_URL_PREFIX):
        return None
    if len(value) > _MAX_URL_CHARS:
        return None
    return value


# --------------------------------------------------------------------------
# Redaction
# --------------------------------------------------------------------------

# The leading \b matters: without it the ``sk`` alternative matches inside
# ordinary words (``task_counter``) and would corrupt legitimate values.
_SECRET_RE = re.compile(
    r"\b(?:sk|ghp|gho|ghs|github_pat|xox[a-z]|AKIA)[A-Za-z0-9_-]{8,}"
)
_BEARER_RE = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE)
_HOST_PATH_RE = re.compile(r"(?:/home|/Users|/tmp)/[^\s\"']*")

_REDACTED = "[redacted]"


def _redact(text: str) -> str:
    for pattern in (_SECRET_RE, _BEARER_RE, _HOST_PATH_RE):
        text = pattern.sub(_REDACTED, text)
    return text


def _redact_value(value: Any) -> Any:
    """Apply :func:`_redact` to every string anywhere in a projected value.

    The field allow-lists below are the primary control; this is the
    belt-and-braces pass that also covers operator-authored free text
    (commit messages, report names) which no allow-list can vet.
    """
    if isinstance(value, str):
        return _redact(value)
    if isinstance(value, dict):
        return {key: _redact_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    return value


# --------------------------------------------------------------------------
# Projection helpers
# --------------------------------------------------------------------------


def _json_object(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _select(row: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    """Return a strict allow-list so new DB columns cannot leak by accident."""
    return {name: row.get(name) for name in fields if name in row}


_RUN_FIELDS = (
    "id",
    "status",
    "goal",
    "project_id",
    "board_task_id",
    "budget_usd_max",
    "cost_usd",
    "created_at",
    "updated_at",
    "started_at",
    "finished_at",
    "error",
)
_TASK_FIELDS = (
    "id",
    "task_key",
    "title",
    "description",
    "status",
    "parent_task_id",
    "created_at",
    "updated_at",
)
_ATTEMPT_FIELDS = (
    "id",
    "board_task_id",
    "status",
    "provider",
    "model",
    "session_id",
    "started_at",
    "finished_at",
    "end_reason",
)
_DEP_FIELDS = ("task_id", "depends_on_task_id")
# ``created_at`` is deliberately not selected from the row: the real events
# table stores the event time as ``ts`` (001_init.sql), so the timestamp is
# normalized separately by :func:`_event_created_at` (F002).
_EVENT_FIELDS = ("id", "action", "target_type", "target_id")
# Operator-facing timeline labels (role / account label / phase) ride the same
# allow-list as the original scalar fields. ``account_id`` is deliberately
# absent — only the human account LABEL may project. ``evidence_links`` is
# projected separately so each URL can be GitHub-validated.
_EVENT_PAYLOAD_FIELDS = (
    "task_id",
    "attempt_id",
    "status",
    "reason",
    "provider",
    "model",
    "note",
    "role",
    "account",
    "phase",
)
#: Timeline label fields: these project ONLY as plain strings. A dict or list
#: smuggled under an allow-listed name would carry nested private fields past
#: the allow-list and into the Raw-payload UI (F001).
_EVENT_PAYLOAD_LABEL_FIELDS = frozenset({"role", "account", "phase"})

# --- bounds on projected evidence (F003) -----------------------------------
#: One event may not expand into an unbounded response/DOM payload: both the
#: collection size and every projected string are bounded, and truncation is
#: always explicit — never silent.
_MAX_EVIDENCE_LINKS_PER_EVENT = 20
#: Per event, per evidence section (commits/files/tests/reports).
_MAX_EVIDENCE_ENTRIES_PER_SECTION = 50
_MAX_EVIDENCE_TEXT_CHARS = 256
_TRUNCATION_SUFFIX = "… [truncated]"
_ARTIFACT_FIELDS = (
    "id",
    "artifact_type",
    "run_id",
    "task_id",
    "attempt_id",
    "format",
    "schema_version",
    "content_hash",
    "quality",
    "created_at",
)

#: The projector never emits ``base_sha`` — it is not a validated run field.
#: Consumers must not assume more than this tuple actually produces.
_CONTEXT_FIELDS = ("repository", "branch", "head_sha")

#: Evidence action, section name -> the field that gives an entry its identity.
_EVIDENCE_ACTION = "evidence.reported"
_EVIDENCE_IDENTITY = {
    "commits": "sha",
    "files": "path",
    "tests": "name",
    "reports": "name",
}

_APPROVAL_ACTION = "approval.recorded"


def _progress(tasks: list[dict[str, Any]]) -> dict[str, int]:
    result: dict[str, int] = {}
    for task in tasks:
        status = str(task.get("status") or "unknown")
        result[status] = result.get(status, 0) + 1
    return result


def _context(run: dict[str, Any]) -> dict[str, str | None]:
    """Project the run's repository binding, nulling anything that fails its rule."""
    plan = _json_object(run.get("plan_json"))
    fallback = _json_object(run.get("metrics_json"))
    raw = {field: plan.get(field, fallback.get(field)) for field in _CONTEXT_FIELDS}
    return {
        "repository": _valid_repository(raw["repository"]),
        "branch": _valid_branch(raw["branch"]),
        "head_sha": _valid_head_sha(raw["head_sha"]),
    }


def _bounded_text(value: str) -> str:
    """Cap a projected string, marking the cut explicitly (F003)."""
    if len(value) <= _MAX_EVIDENCE_TEXT_CHARS:
        return value
    return value[:_MAX_EVIDENCE_TEXT_CHARS] + _TRUNCATION_SUFFIX


#: Closed per-section schema for the top-level ``evidence`` projection —
#: exactly the fields the dashboard client consumes (``normalizeEvidence``).
_EVIDENCE_SECTION_FIELDS = {
    "commits": ("sha", "message"),
    "files": ("path",),
    "tests": ("name", "status"),
    "reports": ("name", "status"),
}


def _evidence_entry(entry: dict[str, Any], fields: tuple[str, ...] = ()) -> dict[str, Any]:
    """Project one evidence entry to a closed schema plus a validated link.

    Only the named fields project, and only when their value is a plain
    string — a container smuggled into a scalar slot must never ride an
    allow-listed key past redaction (F001). Strings are bounded (F003).

    ``url`` carries a three-way signal the client must not collapse: the key
    absent (or null) on the reporter payload means no link was ever supplied;
    present-but-invalid (an explicit non-``github.com`` value) is an active
    refusal and is flagged with ``url_refused`` so the client can never derive
    a link to paper over it. Only a truly absent url may be derived
    client-side. ``url_refused`` is exclusively backend-owned: a reporter
    cannot manufacture the flag by supplying it directly without a url (F001)
    — the flag is only ever set here, from a rejected non-null url.
    """
    projected: dict[str, Any] = {}
    for key in fields:
        value = entry.get(key)
        if isinstance(value, str):
            projected[key] = _bounded_text(value)
    raw_url = entry.get("url")
    valid_url = _github_url(raw_url)
    projected["url"] = valid_url
    if raw_url is not None and valid_url is None:
        projected["url_refused"] = True
    return projected


_ADVERSE_STATUSES = {"failed", "error"}


def _is_adverse(status: Any) -> bool:
    return isinstance(status, str) and status.lower() in _ADVERSE_STATUSES


def _evidence_link_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """Exactly ``label``/``name``/``url`` — the closed schema the timeline
    consumes. Anything else in the entry (ids, credentials, containers) is
    dropped, not copied (F001)."""
    return _evidence_entry(entry, ("label", "name"))


def _project_activity_evidence_links(raw: Any) -> list[dict[str, Any]]:
    """Project in-payload evidence links: closed entry schema, bounded count.

    At most ``_MAX_EVIDENCE_LINKS_PER_EVENT`` entries project; the remainder
    collapses into one explicit marker entry so truncation is visible to the
    operator, never silent (F003).
    """
    if not isinstance(raw, list):
        return []
    entries = [entry for entry in raw if isinstance(entry, dict)]
    projected = [
        _evidence_link_entry(entry) for entry in entries[:_MAX_EVIDENCE_LINKS_PER_EVENT]
    ]
    dropped = len(entries) - _MAX_EVIDENCE_LINKS_PER_EVENT
    if dropped > 0:
        projected.append({"label": f"[{dropped} more evidence links truncated]", "url": None})
    return projected


def _project_event_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Strict, type-checked allow-list plus validated evidence_links.

    Label fields (role/account/phase) project only as plain strings; every
    other allow-listed field projects only as a scalar. A dict or list under
    an allow-listed key is dropped so nested private fields can never cross
    the API boundary inside an allowed name (F001).
    """
    projected: dict[str, Any] = {}
    for key in _EVENT_PAYLOAD_FIELDS:
        if key not in payload:
            continue
        value = payload[key]
        if key in _EVENT_PAYLOAD_LABEL_FIELDS:
            if isinstance(value, str):
                projected[key] = value
        elif value is None or isinstance(value, (str, int, float, bool)):
            projected[key] = value
    if "evidence_links" in payload:
        projected["evidence_links"] = _project_activity_evidence_links(
            payload.get("evidence_links")
        )
    return projected


def _event_created_at(event: dict[str, Any]) -> str | None:
    """Normalize the event timestamp for the client contract's ``created_at``.

    The real events table stores the event time as ``ts`` (001_init.sql), so
    that column is authoritative; ``created_at`` is kept as a fallback for
    feeds that already emit the client-side name. Only a plain non-empty
    string ever projects (F002).
    """
    for field in ("ts", "created_at"):
        value = event.get(field)
        if isinstance(value, str) and value:
            return value
    return None


def _evidence(events: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Accumulate evidence across the window, deduplicated by identity.

    Reporters emit partial, overlapping payloads (a later poll re-reports a
    commit it already mentioned), so the identity field decides membership.
    Most sections are order-stable (the earliest report keeps its position and
    values), but a later report carrying an adverse status (``failed`` /
    ``error``) always overrides an earlier non-adverse one for that identity —
    a later failure must never be masked by an earlier pass.
    """
    result: dict[str, list[dict[str, Any]]] = {section: [] for section in _EVIDENCE_IDENTITY}
    seen: dict[str, dict[str, int]] = {section: {} for section in _EVIDENCE_IDENTITY}
    for event in events:
        if str(event.get("action") or "") != _EVIDENCE_ACTION:
            continue
        payload = _json_object(event.get("payload_json"))
        for section, identity_field in _EVIDENCE_IDENTITY.items():
            entries = payload.get(section)
            if not isinstance(entries, list):
                continue
            # Bounded per event and per section: one hostile event must not
            # expand into an unbounded evidence accumulation (F003).
            for entry in entries[:_MAX_EVIDENCE_ENTRIES_PER_SECTION]:
                if not isinstance(entry, dict):
                    continue
                identity = entry.get(identity_field)
                if not isinstance(identity, str) or not identity:
                    continue
                projected = _evidence_entry(entry, _EVIDENCE_SECTION_FIELDS[section])
                existing_index = seen[section].get(identity)
                if existing_index is not None:
                    existing = result[section][existing_index]
                    # Adverse information must merge FIELD-WISE, never via
                    # whole-entry replacement — replacing the entry lets a
                    # later duplicate silently drop an earlier adverse fact
                    # it does not itself carry (round-3: a later passed+
                    # refused duplicate was clobbering an earlier failed
                    # status, and vice versa). Two adverse carriers merge
                    # independently: an adverse status (tests/reports) wins
                    # if EITHER carrier has one, and url_refused is sticky if
                    # EITHER carrier set it (CP-003 round 2: a later real
                    # refusal must not be swallowed by an earlier merely-
                    # absent URL). Every other field keeps its first-
                    # occurrence value.
                    merged = dict(existing)
                    if _is_adverse(projected.get("status")) and not _is_adverse(
                        existing.get("status")
                    ):
                        merged["status"] = projected.get("status")
                    if bool(projected.get("url_refused")) and not bool(
                        existing.get("url_refused")
                    ):
                        merged["url_refused"] = True
                    if merged != existing:
                        result[section][existing_index] = merged
                    continue
                seen[section][identity] = len(result[section])
                result[section].append(projected)
    return result



def _approval(
    events: list[dict[str, Any]], run_id: str, head_sha: str | None
) -> dict[str, Any]:
    """Report approval ONLY from an explicit receipt bound to this run.

    Artifacts, review documents and passing tests are evidence, not consent;
    none of them may flip this. When the run's context carries a head SHA the
    receipt must name the same one, so a receipt cannot outlive the commit it
    approved.
    """
    for event in events:
        if str(event.get("action") or "") != _APPROVAL_ACTION:
            continue
        payload = _json_object(event.get("payload_json"))
        if str(payload.get("run_id") or "") != run_id:
            continue
        if head_sha is not None and payload.get("head_sha") != head_sha:
            continue
        receipt = project_receipt_payload(payload)
        return {"approved": True, "receipt": receipt}
    return {"approved": False, "receipt": None}


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------


@router.get("/capabilities")
def capabilities() -> dict[str, Any]:
    """Describe the stable adapter surface without host or credential details."""
    return {
        "api_version": API_VERSION,
        "product": "OmniAgentOS",
        "read_only": True,
        "capabilities": {
            "swarm": True,
            "parallel_execution": True,
            "execution_enabled": swarm_execute_enabled(),
            "worktree_isolation_enabled": swarm_worktrees_enabled(),
            "memory": True,
            "reflection": True,
            "improvements": True,
            "activity_cursor": True,
        },
        "links": {
            "create_run": "/api/swarm",
            "run": "/api/swarm/{run_id}",
            "activity": "/api/swarm/{run_id}/activity",
            "snapshot": "/api/engine/runs/{run_id}/snapshot",
            "memory_search": "/api/metacog/memory/search",
            "improvements": "/api/improvements",
        },
    }


@router.get("/runs")
def list_engine_runs(
    swarm_dal: SwarmDalDep,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> dict[str, Any]:
    """Return the bounded, newest-first run list a consumer binds against."""
    rows = sorted(
        swarm_dal.list_runs(),
        key=lambda row: str(row.get("created_at") or ""),
        reverse=True,
    )
    runs = [_select(dict(row), _RUN_FIELDS) for row in rows[:limit]]
    return {"runs": cast(list[dict[str, Any]], _redact_value(runs))}


@router.get("/runs/{run_id}/snapshot")
def run_snapshot(
    run_id: Annotated[str, Path(pattern=RUN_ID_PATTERN)],
    swarm_dal: SwarmDalDep,
    metacog: MetacogDep,
    store: StoreDep,
    after: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> dict[str, Any]:
    """Return one bounded, run-scoped evidence snapshot for a remote UI.

    Strictly scoped to ``run_id``: nothing from another run is ever merged in,
    which is the server half of resetting evidence when the authoritative run
    binding changes (the client half is ``mergeEngineRunSnapshot``).
    """
    run = swarm_dal.get_run(run_id)
    if run is None:
        fail(404, "not_found", "engine run not found", {"id": run_id})

    root_id = str(run.get("board_task_id") or "")
    tasks = [
        dict(task)
        for task in swarm_dal.tasks_for_run(run_id)
        if str(task.get("id") or "") != root_id
    ]
    attempts = {
        str(task["id"]): [
            _select(dict(attempt), _ATTEMPT_FIELDS)
            for attempt in swarm_dal.list_attempts(str(task["id"]))
        ]
        for task in tasks
        if task.get("id") is not None
    }
    events = cast(_EventStore, store).get_events_for_target(
        "swarm_run", run_id, after_id=after, limit=limit
    )
    artifacts = metacog.store.list_artifacts(run_id=run_id, limit=100)

    safe_events = []
    for event in events:
        projected = _select(dict(event), _EVENT_FIELDS)
        projected["created_at"] = _event_created_at(dict(event))
        payload = _json_object(event.get("payload_json"))
        projected["payload"] = _project_event_payload(payload)
        safe_events.append(projected)

    safe_artifacts = []
    for artifact in artifacts:
        data = artifact.model_dump() if hasattr(artifact, "model_dump") else dict(artifact)
        safe_artifacts.append(_select(data, _ARTIFACT_FIELDS))

    metrics = _json_object(run.get("metrics_json"))
    safe_metrics = {
        key: metrics[key]
        for key in (
            "task_count",
            "completed_count",
            "failed_count",
            "elapsed_seconds",
            "cost_usd",
            "parallelism_ratio",
            "speedup",
        )
        if key in metrics
    }
    context = _context(run)
    body = {
        "api_version": API_VERSION,
        "run": _select(dict(run), _RUN_FIELDS),
        "tasks": [_select(task, _TASK_FIELDS) for task in tasks],
        "deps": [
            _select(dict(dependency), _DEP_FIELDS) for dependency in swarm_dal.deps_for_run(run_id)
        ],
        "attempts": attempts,
        "progress": _progress(tasks),
        "metrics": safe_metrics,
        "activity": safe_events,
        "next_activity_cursor": max((int(row.get("id") or 0) for row in events), default=after),
        "artifacts": safe_artifacts,
        "context": context,
        "evidence": _evidence(events),
        "approval": _approval(events, run_id, context["head_sha"]),
    }
    return cast(dict[str, Any], _redact_value(body))
