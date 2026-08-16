"""Swarm planner (WP4): brief → validated DAG plan → transactional provisioning.

Pipeline (:func:`plan_swarm` / :func:`plan_swarm_bundles`):

1. **Bounded NON-BLOCKING clarify** — one ``clarify_intake`` pass. In swarm mode
   clarification NEVER waits on a human: any questions the clarifier would have
   asked become recorded ``assumptions`` in the plan.
2. **Prior lessons** (~1200-token cap) — recent ``swarm_runs`` outcomes via
   :class:`SwarmDal`, a ``knowledge.recall`` block (discipline ``"swarm"``), and
   the optimizer's advisory playbook file (``var/swarm/learned.json``) when
   present. All best-effort; a missing source never blocks planning.
3. **DAG plan** via the config/env-selected planner (default: Qwen 3.7 /
   ``qwen37-plus`` at low effort through the local LiteLLM proxy; Fable-path
   aliases still available). Schema is :class:`SwarmTaskSpec`. The same pass
   detects unrelated-task **bundles**: a brief containing N independent asks
   returns ``bundles: [...]`` and each bundle becomes its own plan/card set,
   routed independently.
4. **Validation** (pure functions, unit-tested): topo sort with one repair pass
   then clean fail; ownership-overlap → dep edge (later task in planner output
   depends on the earlier); owned_paths workspace-relative + containment-checked;
   shared files default to integration ownership; bootstrap task when installs
   are needed; task cap ≤ 30; auto-appended integration task; parallelism ratio
   → ``target_n``; solo mode when tasks ≤ 2 or ratio < 1.5.
5. **Provisioning** — :func:`provision_run` writes the run row + root board card
   + child cards + ``swarm_deps`` in ONE transaction (``SwarmDal.provision_run``),
   with the plan version + sha256 hash stored in ``plan_json`` and stamped into
   every card's ``swarm_json`` (brief-hash verification downstream).
6. **PLAN.md** — a derived projection of ``plan_json`` written to the working
   dir via tmp+fsync+rename; :func:`render_plan_md` is pure so the coordinator
   regenerates it on completion/split/resize.

Offline degradation remains inspectable as a flat single-task solo-shaped
result, but it carries no ownership and an explicit failure marker.  The
central safety decision therefore makes it non-executable at every side-effect
boundary. Planning never broadens scope to recover from model failure.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

from omniagentos.contracts import new_id
from omniagentos.roles import job_role_from_swarm_json
from omniagentos.swarm.contracts import SwarmPlan, SwarmPlanDecision, SwarmTaskSpec
from omniagentos.swarm.dal import SwarmDal
from omniagentos.swarm.plan_safety import (
    assert_plan_safe_for_provision,
    evaluate_plan_safety,
)

LOG = logging.getLogger(__name__)


def _intake_planning() -> tuple[Any, Any, Any]:
    """Resolve the intake reuse seams lazily, API-package-first.

    ``omniagentos.intake.__init__`` imports ``intake.service``, which imports
    the ``omniagentos.api`` package, whose routes import ``intake.service``
    back — the cycle only resolves when ``omniagentos.api`` initializes FIRST
    (the same order-sensitivity ``intake/planner.py`` documents). A module-load
    intake import here would break any process that touches the swarm planner
    before the API package.
    """
    import omniagentos.api  # noqa: F401 -- must initialize before intake
    from omniagentos.intake import fable, planner, service

    return fable, planner, service


# --- policy constants --------------------------------------------------------

MAX_TASKS = 30  # planner-emitted worker tasks (auto tasks exempt)
SOLO_MAX_TASKS = 2  # ≤2 worker tasks → solo mode
SOLO_RATIO_THRESHOLD = 1.5  # ratio below this → solo mode

# Rate-limit-aware auto decision (swarm auto-default): fleet headroom raises
# the solo threshold when capacity is scarce. LOW headroom = fewer than
# ``auto.low_headroom_slots`` swarm session slots free OR mean provider
# pressure across ENABLED providers ≥ LOW_HEADROOM_PRESSURE (most providers
# cooling/saturated); a LOW-headroom plan swarms only at ratio ≥
# ``auto.low_headroom_ratio`` — parallelism must pay harder before a swarm
# spends scarce rate-limit budget. Config knobs live under ``auto:`` in
# configs/swarm.yaml; these are the code-side floors.
DEFAULT_LOW_HEADROOM_SLOTS = 5  # < this many free swarm slots → LOW
DEFAULT_LOW_HEADROOM_RATIO = 2.5  # LOW-headroom solo threshold
LOW_HEADROOM_PRESSURE = 0.75  # mean enabled-provider pressure ≥ this → LOW
TARGET_N_MIN = 2
TARGET_N_MAX = 5  # default cap per the fleet capacity model
TARGET_N_HARD_CEILING = 20  # env override may not exceed this (fleet-scale-200)


def _target_cap() -> int:
    """Per-run slot cap: default 5, env-overridable up to the hard fleet
    ceiling of :data:`TARGET_N_HARD_CEILING` (OMNIAGENTOS_SWARM_TARGET_CAP —
    used by the wide drills and by operators who want wider runs).

    The ceiling is 20 (was 10) for the 200-agent fleet. It is deliberately the
    SAME number as ``swarm.scheduler.MAX_SLOTS``: this function caps what the
    planner may WRITE into ``swarm_runs.target_concurrency``, and MAX_SLOTS caps
    what the coordinator will actually run. A planner ceiling above MAX_SLOTS
    would just be silently clamped at runtime, so they move together.
    """
    raw = os.environ.get("OMNIAGENTOS_SWARM_TARGET_CAP", "").strip()
    try:
        value = int(raw) if raw else TARGET_N_MAX
    except ValueError:
        value = TARGET_N_MAX
    return max(TARGET_N_MIN, min(value, TARGET_N_HARD_CEILING))


LESSONS_TOKEN_CAP = 1200
_CHARS_PER_TOKEN = 4  # same coarse budget rule knowledge recall uses
DEFAULT_PLAYBOOK_PATH = Path("var/swarm/learned.json")

INTEGRATION_TASK_ID = "integration"
BOOTSTRAP_TASK_ID = "bootstrap"
_RESERVED_TASK_IDS = frozenset({INTEGRATION_TASK_ID, BOOTSTRAP_TASK_ID})

# Directories that must never win an owned-path auto-qualification match.
_AUTO_QUALIFY_SKIP = frozenset(
    {"node_modules", "__pycache__", ".git", "var", "build", "dist", ".venv", "site-packages"}
)

# P0.4 / v4-Q2: pytest reads configuration from more than conftest.py and
# pytest.ini. A worker that may edit any of these can inject `--ignore=tests/...`
# or register a plugin and neuter the very suite that is supposed to judge it,
# without touching a single test file.
PYTEST_CONFIG_SURFACES = frozenset(
    {
        "conftest.py",
        "pytest.ini",
        "tox.ini",
        "setup.cfg",
        "pyproject.toml",
        "setup.py",
    }
)


def is_pytest_config_surface(path: str) -> bool:
    """True if ``path`` can influence pytest collection or plugin loading."""
    return Path(str(path)).name in PYTEST_CONFIG_SURFACES


PLAN_MD_FILENAME = "PLAN.md"

_DEFAULT_EST_AGENT_MINUTES = 10
_DEFAULT_EST_MANUAL_MINUTES = 30

# Well-known shared files default to integration-task ownership (Phase 1
# same-directory model: N sessions share one working dir; these files are the
# classic merge-conflict magnets).
_SHARED_BASENAMES = frozenset(
    {
        "package.json",
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "uv.lock",
        "poetry.lock",
        "pipfile.lock",
        "cargo.lock",
        "gemfile.lock",
        "composer.lock",
        "go.mod",
        "go.sum",
    }
)
_SHARED_SEGMENTS = frozenset({"migrations"})
_TOP_LEVEL_CONFIG_SUFFIXES = frozenset({".json", ".yaml", ".yml", ".toml", ".ini", ".cfg"})
_TOP_LEVEL_CONFIG_NAMES = frozenset(
    {"makefile", "dockerfile", "docker-compose.yml", ".gitignore", ".env.example"}
)
_CI_PREFIXES = (".github/", ".gitlab-ci", ".circleci/")


class SwarmPlanError(ValueError):
    """A plan that cannot be made safe: unrepairable cycle, escaping owned path,
    duplicate/reserved task ids, or a task count over the cap."""


# --- LLM seams ----------------------------------------------------------------

# (prompt, schema, effort) → parsed JSON dict or None. Same shape as the intake
# planner seam so tests inject a fake and assert the effort chosen.
SwarmPlannerLLM = Callable[[str, dict[str, Any], str], dict[str, Any] | None]
# clarify seam: (prompt, schema) → dict|None — passed straight to clarify_intake.
ClarifyLLM = Callable[[str, dict[str, Any]], dict[str, Any] | None]
# lessons recall seam: goal → rendered block ("" when nothing/unavailable).
RecallFn = Callable[[str], str]


# --- Planner model/effort selection (config + env; default Qwen for speed) ----
#
# Pattern mirrors ``spawn.parse_role_pack_mode`` / ``role_pack_mode``: a module
# constant default, an env override, a strict parser that falls back on any
# unrecognised value (never half-applies), plus an optional ``configs/swarm.yaml``
# ``planner:`` block (same env-over-config shape as ``allocation.config``).
#
# DEFAULT is Qwen 3.7 via the local LiteLLM proxy (OpenAI-compatible, no Anthropic
# rate-limit burn). Concurrent simulations need this path; the old hard-wired
# Fable/Opus chain burned ~300s and two Anthropic rungs per plan.

SWARM_PLANNER_MODEL_ENV = "OMNIAGENTOS_SWARM_PLANNER_MODEL"
SWARM_PLANNER_EFFORT_ENV = "OMNIAGENTOS_SWARM_PLANNER_EFFORT"

DEFAULT_SWARM_PLANNER_MODEL = "qwen37-flash"
DEFAULT_SWARM_PLANNER_EFFORT = "low"

# Sentinel: when model resolves to this, use the formation's declared planner
# (O-4: formation.planner must be able to affect planning, not only telemetry).
SWARM_PLANNER_FORMATION_SENTINEL = "formation"

# Strict allow-list. Unrecognised spellings fall back to the module default —
# enabling a planner model must take an exact known word, never a typo that
# silently half-routes to an unintended provider.
SWARM_PLANNER_PROXY_MODELS: frozenset[str] = frozenset(
    {
        "qwen",
        "qwen37-plus",
        "qwen37-flash",
        "qwen37-max",
        "qwen3-coder",
        "qwen3-coder-flash",
        "qwen3-coder-plus",
        "gemini-3.6-flash",
        "deepseek-v4-flash",
        "deepseek-v4-pro",
    }
)
# Short aliases → LiteLLM proxy model ids.
_SWARM_PLANNER_PROXY_ALIASES: dict[str, str] = {
    "qwen": "qwen37-plus",
}
# Fusion/CLI planner aliases → Anthropic-chain path via run_fable_json.
SWARM_PLANNER_FABLE_ALIASES: frozenset[str] = frozenset({"fable", "opus", "claude"})
SWARM_PLANNER_MODELS: frozenset[str] = (
    SWARM_PLANNER_PROXY_MODELS
    | SWARM_PLANNER_FABLE_ALIASES
    | frozenset({SWARM_PLANNER_FORMATION_SENTINEL})
)
SWARM_PLANNER_EFFORTS: frozenset[str] = frozenset({"low", "medium", "high", "xhigh", "max"})

# Proxy call budget: a full multi-task DAG JSON is larger than a short classify
# call; keep wall time under a minute while leaving room for cold starts.
_SWARM_PLANNER_PROXY_TIMEOUT_S = 120.0
_SWARM_PLANNER_PROXY_MAX_TOKENS = 6144


def parse_swarm_planner_model(value: object) -> str:
    """Parse a planner model spelling; anything unrecognised is the default.

    Absent, non-string, misspelled, and empty values all resolve to
    :data:`DEFAULT_SWARM_PLANNER_MODEL` — selecting a non-default planner must
    take an exact known word (same contract as ``parse_role_pack_mode``).
    """
    if not isinstance(value, str):
        return DEFAULT_SWARM_PLANNER_MODEL
    normalized = value.strip().lower()
    if not normalized:
        return DEFAULT_SWARM_PLANNER_MODEL
    if normalized not in SWARM_PLANNER_MODELS:
        return DEFAULT_SWARM_PLANNER_MODEL
    if normalized == SWARM_PLANNER_FORMATION_SENTINEL:
        return SWARM_PLANNER_FORMATION_SENTINEL
    return _SWARM_PLANNER_PROXY_ALIASES.get(normalized, normalized)


def parse_swarm_planner_effort(value: object) -> str:
    """Parse a planner effort spelling; anything unrecognised is the default."""
    if not isinstance(value, str):
        return DEFAULT_SWARM_PLANNER_EFFORT
    normalized = value.strip().lower()
    if normalized not in SWARM_PLANNER_EFFORTS:
        return DEFAULT_SWARM_PLANNER_EFFORT
    return normalized


def _swarm_planner_config_section() -> Mapping[str, Any]:
    """``configs/swarm.yaml`` → ``planner:`` block (best-effort, never raises)."""
    try:
        from omniagentos.routing.limit_state import load_swarm_config

        raw = load_swarm_config().get("planner")
        return raw if isinstance(raw, Mapping) else {}
    except Exception:  # noqa: BLE001 -- config faults must not block planning.
        return {}


def swarm_planner_model(
    env: Mapping[str, str] | None = None,
    *,
    config: Mapping[str, Any] | None = None,
    formation_planner: str | None = None,
) -> str:
    """Resolve the swarm planner model: env → config → formation → default.

    Priority (first *recognised* value wins):

    1. ``OMNIAGENTOS_SWARM_PLANNER_MODEL``
    2. ``configs/swarm.yaml`` ``planner.model``
    3. Formation's declared planner (O-4) when it is a known model, or when
       env/config selected the ``formation`` sentinel
    4. :data:`DEFAULT_SWARM_PLANNER_MODEL` (``qwen37-plus``)

    Unrecognised env/config spellings are ignored (fall through) rather than
    half-applied — same contract as ``parse_role_pack_mode``. Garbage formation
    values never override the module default.
    """
    source: Mapping[str, str] = os.environ if env is None else env
    raw_env = source.get(SWARM_PLANNER_MODEL_ENV)
    if raw_env is not None and str(raw_env).strip():
        token = str(raw_env).strip().lower()
        if token in SWARM_PLANNER_MODELS:
            if token == SWARM_PLANNER_FORMATION_SENTINEL:
                return _formation_planner_or_default(formation_planner, force=True)
            return parse_swarm_planner_model(token)
        # Unrecognised env spelling → fall through (do not pin the default yet).

    section = config if config is not None else _swarm_planner_config_section()
    raw_cfg = section.get("model") if isinstance(section, Mapping) else None
    if raw_cfg is not None and str(raw_cfg).strip():
        token = str(raw_cfg).strip().lower()
        if token in SWARM_PLANNER_MODELS:
            if token == SWARM_PLANNER_FORMATION_SENTINEL:
                return _formation_planner_or_default(formation_planner, force=True)
            return parse_swarm_planner_model(token)

    return _formation_planner_or_default(formation_planner, force=False)


def _formation_planner_or_default(
    formation_planner: str | None,
    *,
    force: bool = False,
) -> str:
    """Use formation.planner when it can safely drive planning; else default.

    Without ``force`` (the automatic fall-through when env/config are unset),
    only *proxy* formation planners apply — so a formation that still declares
    ``planner: sol`` does not silently reintroduce Anthropic rate-limit burn
    on the concurrent-sim default path. Operators who want formation.planner
    to pick Fable/Opus/Sol set ``OMNIAGENTOS_SWARM_PLANNER_MODEL=formation``
    (or ``planner.model: formation`` in swarm.yaml), which passes ``force=True``.
    """
    if formation_planner is None:
        return DEFAULT_SWARM_PLANNER_MODEL
    token = str(formation_planner).strip().lower()
    if not token or token == SWARM_PLANNER_FORMATION_SENTINEL:
        return DEFAULT_SWARM_PLANNER_MODEL
    if token not in SWARM_PLANNER_MODELS:
        return DEFAULT_SWARM_PLANNER_MODEL
    if force:
        return parse_swarm_planner_model(token)
    # Auto path: proxy models only (qwen / litellm ids).
    if token in SWARM_PLANNER_PROXY_MODELS or token in _SWARM_PLANNER_PROXY_ALIASES:
        return parse_swarm_planner_model(token)
    return DEFAULT_SWARM_PLANNER_MODEL


def swarm_planner_effort(
    env: Mapping[str, str] | None = None,
    *,
    config: Mapping[str, Any] | None = None,
) -> str:
    """Resolve planner effort: env → config → default (``low``)."""
    source: Mapping[str, str] = os.environ if env is None else env
    raw_env = source.get(SWARM_PLANNER_EFFORT_ENV)
    if raw_env is not None and str(raw_env).strip():
        token = str(raw_env).strip().lower()
        if token in SWARM_PLANNER_EFFORTS:
            return token
        # Unrecognised → fall through (same strict-fallback contract).
    section = config if config is not None else _swarm_planner_config_section()
    raw_cfg = section.get("effort") if isinstance(section, Mapping) else None
    if raw_cfg is not None and str(raw_cfg).strip():
        token = str(raw_cfg).strip().lower()
        if token in SWARM_PLANNER_EFFORTS:
            return token
    return DEFAULT_SWARM_PLANNER_EFFORT


def _is_proxy_planner_model(model: str) -> bool:
    """True when ``model`` is served by the local LiteLLM proxy path."""
    token = str(model).strip().lower()
    return token in SWARM_PLANNER_PROXY_MODELS or token in _SWARM_PLANNER_PROXY_ALIASES.values()


def _proxy_swarm_planner_llm(
    prompt: str,
    schema: dict[str, Any],
    effort: str,
    *,
    model: str,
    client: Any | None = None,
) -> dict[str, Any] | None:
    """Run the swarm-plan prompt through ShortCallClient (OpenAI-compatible proxy).

    Returns a parsed dict honouring the planner contract (dict | None). Soft-
    validates that the response is a JSON object with a ``tasks`` or ``bundles``
    list — the same structural gates :func:`plan_swarm_bundles` already applies
    via :func:`build_plan` / :func:`_extract_bundles`. Never raises.
    """
    try:
        from omniagentos.llm.client import ShortCallClient
    except Exception:  # noqa: BLE001
        LOG.warning("swarm planner: ShortCallClient import failed", exc_info=True)
        return None

    proxy_client = client or ShortCallClient(
        default_model=model,
        timeout=_SWARM_PLANNER_PROXY_TIMEOUT_S,
    )
    schema_text = json.dumps(schema, sort_keys=True)
    system = (
        "You are a structured-output swarm planner. Respond with a single JSON "
        "object that matches the provided schema. No markdown fences, no "
        "commentary, no prose outside the JSON object.\n\n"
        f"JSON SCHEMA:\n{schema_text}"
    )
    # Effort is a Fable/Claude concept; for proxy models map it to temperature
    # only (low effort → more deterministic). The caller's effort arg still
    # reaches this function so the SwarmPlannerLLM contract is unchanged.
    temperature = 0.15 if str(effort).strip().lower() in ("low", "") else 0.3
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": prompt},
    ]
    try:
        # complete_json retries once on invalid JSON / missing keys. Empty
        # required_keys: top-level schema has no required fields (tasks vs
        # bundles is a soft choice); we gate structure below.
        parsed = proxy_client.complete_json(
            messages,
            required_keys=[],
            model=model,
            temperature=temperature,
            max_tokens=_SWARM_PLANNER_PROXY_MAX_TOKENS,
            purpose="swarm_planner",
        )
    except Exception:  # noqa: BLE001 -- seam must return None, never crash planning.
        LOG.warning("swarm planner proxy call failed (model=%s)", model, exc_info=True)
        return None
    if not isinstance(parsed, dict):
        return None
    has_tasks = isinstance(parsed.get("tasks"), list)
    has_bundles = isinstance(parsed.get("bundles"), list)
    if not has_tasks and not has_bundles:
        LOG.warning(
            "swarm planner proxy returned JSON without tasks/bundles (model=%s)",
            model,
        )
        return None
    return parsed


def _fable_swarm_planner_llm(
    prompt: str, schema: dict[str, Any], effort: str, *, model: str
) -> dict[str, Any] | None:
    """Run the swarm-plan prompt through the Fable/Claude fallback chain.

    ``model`` is a fusion alias (fable/opus/sol/claude). ``run_fable_json`` already
    honours ``OMNIAGENTOS_PLANNER_FALLBACKS``; we pin the initial model when the
    alias is not the default Fable id so formation.planner=sol actually starts
    on Sol rather than always entering at Fable.
    """
    fable, _, _ = _intake_planning()
    # run_fable_json's model arg is the Claude --model id. Fusion aliases map
    # 1:1 for fable/opus/sol; "claude" keeps the ambient default.
    model_arg = model if model in ("fable", "opus", "sol") else fable.FABLE_MODEL
    return fable.run_fable_json(
        prompt,
        schema,
        model=model_arg,
        effort=effort,
        max_turns=3,
        wall_ms=300_000,
    )


def make_swarm_planner_llm(
    model: str | None = None,
    *,
    formation_planner: str | None = None,
    client: Any | None = None,
    env: Mapping[str, str] | None = None,
    config: Mapping[str, Any] | None = None,
) -> SwarmPlannerLLM:
    """Build a :data:`SwarmPlannerLLM` for the resolved model.

    ``model`` pins the backend when provided; otherwise resolution follows
    :func:`swarm_planner_model` (env → config → formation → default). The
    returned callable satisfies the ``(prompt, schema, effort) -> dict|None``
    contract exactly. ``client`` injects a ShortCallClient (or compatible) for
    tests that assert the selected model reaches the call.
    """
    if model is not None:
        resolved = parse_swarm_planner_model(model)
        if resolved == SWARM_PLANNER_FORMATION_SENTINEL:
            resolved = _formation_planner_or_default(formation_planner, force=True)
    else:
        resolved = swarm_planner_model(env, config=config, formation_planner=formation_planner)

    def _llm(prompt: str, schema: dict[str, Any], effort: str) -> dict[str, Any] | None:
        if _is_proxy_planner_model(resolved):
            return _proxy_swarm_planner_llm(prompt, schema, effort, model=resolved, client=client)
        return _fable_swarm_planner_llm(prompt, schema, effort, model=resolved)

    # Surface the resolved model for tests / telemetry without breaking the
    # callable contract (attribute on the function object).
    _llm.model = resolved  # type: ignore[attr-defined]
    return _llm


def default_swarm_planner_llm(
    prompt: str, schema: dict[str, Any], effort: str
) -> dict[str, Any] | None:
    """Default swarm-plan seam: config/env-selected model (default Qwen 3.7).

    Reads :func:`swarm_planner_model` / :func:`swarm_planner_effort` at call
    time so env overrides apply without process restart. Does **not** invoke
    Fable/Claude unless the resolved model is a Fable-path alias.
    """
    # When no formation context is available (bare default call), formation
    # cannot contribute — env/config/default only. plan_swarm_bundles builds a
    # formation-aware runner via make_swarm_planner_llm instead.
    return make_swarm_planner_llm()(prompt, schema, effort)


def default_recall_lessons(goal: str) -> str:
    """Best-effort ``knowledge.recall`` block for discipline "swarm".

    ``run_id=None`` keeps the recall side-effect free (a plan preview must not
    reinforce facts). Any failure — no Postgres, no embedder — returns "".
    """
    try:
        from omniagentos.knowledge.config import knowledge_enabled

        if not knowledge_enabled():
            # Default-off subsystem: skip the store entirely rather than eating
            # a connect timeout on every plan.
            return ""

        from omniagentos.knowledge.recall import _get_store, recall, render_recall_block

        result = recall(_get_store(), prompt=goal, discipline="swarm", run_id=None)
        return render_recall_block(result) or ""
    except Exception:  # noqa: BLE001 -- lessons are advisory; recall must never block planning.
        return ""


def default_recall_capabilities(
    goal: str, *, company_id: str | None, domains: Sequence[str] | None = None
) -> str:
    """Ambient small-k recall across exactly company + estate namespaces."""
    try:
        from omniagentos.knowledge.config import knowledge_enabled

        if not knowledge_enabled():
            return ""
        from omniagentos.knowledge.capabilities import safe_ambient_capability_block

        return safe_ambient_capability_block(
            goal,
            company_id=company_id,
            domains=domains,
        )
    except Exception:  # noqa: BLE001 -- capability recall must never block planning.
        return ""


# ---------------------------------------------------------------------------
# Pure validation helpers
# ---------------------------------------------------------------------------


def resolve_owned_path(path: str, workspace_dir: str | Path) -> tuple[str, str | None]:
    """Bind a normalized owned path to something that actually exists on disk.

    ``normalize_owned_path`` only validates SHAPE. A path that is syntactically
    perfect but names nothing (``swarm/`` when the package is
    ``omniagentos/swarm/``) sails through planning, and then every edit the
    worker makes is judged out-of-scope and REVERTED by the coordinator while
    the suite still passes against the unchanged tree — a silent no-op branch.

    Returns ``(resolved_path, note)``. A path that exists, or whose parent
    directory exists (a new file), is returned unchanged with ``note=None``.
    Otherwise a UNIQUE directory elsewhere in the tree with the same trailing
    segments is adopted and reported; ambiguity or no match raises.
    """
    root = Path(workspace_dir)
    normalized = normalize_owned_path(path)
    if normalized == ".":
        return normalized, None
    candidate = root / normalized
    if candidate.exists():
        return normalized, None

    # Auto-qualify BEFORE accepting a non-existent path: a top-level name like
    # "swarm" always has an existing parent (the workspace root), so an
    # early "parent exists, must be a new file" return would mask exactly the
    # mis-scoping this function exists to catch.
    tail = normalized.rstrip("/")
    matches: list[str] = []
    for found in root.glob(f"**/{tail}"):
        if not found.is_dir():
            continue
        rel_path = found.relative_to(root)
        # Check the WORKSPACE-RELATIVE parts only: an absolute temp path on
        # macOS lives under /private/var/..., and matching "var" against the
        # absolute parts would skip every candidate.
        if any(part.startswith(".") or part in _AUTO_QUALIFY_SKIP for part in rel_path.parts):
            continue
        matches.append(rel_path.as_posix())
    unique = sorted(set(matches))
    if len(unique) == 1:
        return unique[0], f"owned path {normalized!r} auto-qualified to {unique[0]!r}"
    if len(unique) > 1:
        raise SwarmPlanError(
            f"owned path {normalized!r} is ambiguous — candidates: {', '.join(unique[:5])}; "
            "declare the fully-qualified path"
        )
    # No better-qualified match: a genuinely new file or directory tree, which
    # is legitimate. The no-op-branch risk is caught downstream by the
    # ownership-revert check, which fails an attempt whose entire substantive
    # diff was reverted.
    return normalized, None


def normalize_owned_path(path: str) -> str:
    """Normalize one owned path to workspace-relative form; reject escapes.

    Rejects absolute paths, home-dir paths, drive-letter paths, and any path
    whose ``..`` segments climb out of the workspace. ``./a//b/`` → ``a/b``;
    a path that resolves to the workspace root returns ``"."``.
    """
    raw = str(path).strip().replace("\\", "/")
    if not raw:
        raise SwarmPlanError("owned path is empty")
    if raw.startswith(("/", "~")) or re.match(r"^[A-Za-z]:", raw):
        raise SwarmPlanError(f"owned path must be workspace-relative: {path!r}")
    parts: list[str] = []
    for segment in raw.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            if not parts:
                raise SwarmPlanError(f"owned path escapes the workspace: {path!r}")
            parts.pop()
        else:
            parts.append(segment)
    return "/".join(parts) if parts else "."


def is_shared_path(path: str) -> bool:
    """True when a (normalized) path is a well-known shared file that defaults
    to integration-task ownership: manifests/lockfiles, migration dirs,
    top-level configs, and CI config."""
    lowered = path.lower()
    basename = lowered.rsplit("/", 1)[-1]
    # P0.4 / v4-Q2: pytest configuration surfaces are never worker-owned. A
    # worker able to edit pyproject.toml/setup.cfg/tox.ini/setup.py can inject
    # `--ignore=tests/...` or register a plugin and neuter the suite that
    # judges it, without touching a test file.
    if is_pytest_config_surface(basename):
        return True
    if basename in _SHARED_BASENAMES:
        return True
    if any(segment in _SHARED_SEGMENTS for segment in lowered.split("/")):
        return True
    if lowered.startswith(_CI_PREFIXES):
        return True
    if "/" not in lowered and lowered != ".":
        if basename in _TOP_LEVEL_CONFIG_NAMES:
            return True
        dot = basename.rfind(".")
        if dot > 0 and basename[dot:] in _TOP_LEVEL_CONFIG_SUFFIXES:
            return True
    return False


def paths_overlap(a: str, b: str) -> bool:
    """Two normalized workspace-relative paths overlap when equal or nested."""
    if a == "." or b == ".":
        return True
    return a == b or b.startswith(a + "/") or a.startswith(b + "/")


def topo_sort_with_repair(
    tasks: Sequence[SwarmTaskSpec],
) -> tuple[list[str], list[tuple[str, str]]]:
    """Topologically order task ids; one repair pass on a cycle, then clean fail.

    The repair removes the single earliest *forward* dependency inside the
    cyclic residue — a dep on a task that appears LATER in planner output order
    (planners emit roughly topological order, so a forward dep is the suspect
    edge). If a cycle survives that one repair, raise :class:`SwarmPlanError`.

    Returns ``(ordered_ids, removed_edges)`` where each removed edge is
    ``(task_id, depends_on_id)``.
    """
    index = {task.id: position for position, task in enumerate(tasks)}
    deps: dict[str, set[str]] = {task.id: set(task.depends_on) for task in tasks}
    removed: list[tuple[str, str]] = []

    for attempt in (0, 1):
        order = _kahn(deps, index)
        if len(order) == len(tasks):
            return order, removed
        if attempt == 1:
            break
        leftover = set(deps) - set(order)
        candidates = sorted(
            (
                (index[task_id], index[dep], task_id, dep)
                for task_id in leftover
                for dep in deps[task_id]
                if dep in leftover and index[dep] > index[task_id]
            ),
        )
        if not candidates:  # pragma: no cover -- a cycle always has a forward edge
            break
        _, _, task_id, dep = candidates[0]
        deps[task_id].discard(dep)
        removed.append((task_id, dep))

    cyclic = sorted(set(deps) - set(_kahn(deps, index)), key=lambda tid: index[tid])
    raise SwarmPlanError(
        "dependency cycle could not be repaired in one pass; involved tasks: " + ", ".join(cyclic)
    )


def _kahn(deps: Mapping[str, set[str]], index: Mapping[str, int]) -> list[str]:
    """Kahn's algorithm, prerequisite-first, stable on planner output order."""
    remaining = {task_id: set(dep_ids) for task_id, dep_ids in deps.items()}
    dependents: dict[str, list[str]] = {task_id: [] for task_id in deps}
    for task_id, dep_ids in deps.items():
        for dep in dep_ids:
            dependents[dep].append(task_id)
    ready = sorted(
        (task_id for task_id, dep_ids in remaining.items() if not dep_ids),
        key=lambda tid: index[tid],
    )
    order: list[str] = []
    while ready:
        current = ready.pop(0)
        order.append(current)
        newly_ready = []
        for dependent in dependents[current]:
            remaining[dependent].discard(current)
            if not remaining[dependent] and dependent not in order:
                newly_ready.append(dependent)
        if newly_ready:
            ready = sorted(set(ready) | set(newly_ready), key=lambda tid: index[tid])
    return order


def add_ownership_overlap_edges(tasks: Sequence[SwarmTaskSpec]) -> list[tuple[str, str]]:
    """Serialize tasks whose owned paths overlap: the LATER task in planner
    output order gains a dep on the EARLIER one (deterministic direction).

    A pair already related by an existing dependency path (either direction) is
    left alone — it is serialized already, and adding the reverse edge would
    manufacture a cycle. Mutates ``tasks`` in place; returns the added edges.
    """
    added: list[tuple[str, str]] = []
    for later_pos in range(len(tasks)):
        for earlier_pos in range(later_pos):
            earlier, later = tasks[earlier_pos], tasks[later_pos]
            if not any(paths_overlap(a, b) for a in earlier.owned_paths for b in later.owned_paths):
                continue
            if _reaches(tasks, later.id, earlier.id) or _reaches(tasks, earlier.id, later.id):
                continue
            later.depends_on.append(earlier.id)
            added.append((later.id, earlier.id))
    return added


def _reaches(tasks: Sequence[SwarmTaskSpec], source: str, target: str) -> bool:
    """True when ``source`` transitively depends on ``target``."""
    deps = {task.id: task.depends_on for task in tasks}
    frontier, seen = [source], set()
    while frontier:
        current = frontier.pop()
        if current == target:
            return True
        if current in seen:
            continue
        seen.add(current)
        frontier.extend(deps.get(current, []))
    return False


def critical_path_minutes(tasks: Sequence[SwarmTaskSpec]) -> int:
    """Longest dependency-chain sum of ``est_agent_minutes`` (the DAG's floor
    on wall-clock). Assumes an acyclic, known-deps task list."""
    by_id = {task.id: task for task in tasks}
    memo: dict[str, int] = {}

    def chain(task_id: str) -> int:
        if task_id in memo:
            return memo[task_id]
        task = by_id[task_id]
        best_prefix = max((chain(dep) for dep in task.depends_on if dep in by_id), default=0)
        memo[task_id] = best_prefix + max(0, task.est_agent_minutes)
        return memo[task_id]

    return max((chain(task.id) for task in tasks), default=0)


def parallelism_stats(tasks: Sequence[SwarmTaskSpec]) -> tuple[float, int]:
    """``(parallelism_ratio, target_n)`` over the worker tasks.

    ratio = Σ est_agent_minutes / critical-path minutes;
    target_n = clamp(round-half-up(ratio), TARGET_N_MIN, _target_cap()).
    """
    total = sum(max(0, task.est_agent_minutes) for task in tasks)
    critical = critical_path_minutes(tasks)
    ratio = (total / critical) if critical > 0 else 1.0
    target_n = min(_target_cap(), max(TARGET_N_MIN, int(ratio + 0.5)))
    return ratio, target_n


@dataclass(frozen=True)
class SwarmHeadroom:
    """Fleet headroom snapshot driving the rate-limit-aware solo threshold.

    ``level`` is ``"low"`` when swarming would fight scarce capacity (few free
    swarm session slots, or most enabled providers cooling/saturated) and
    ``"high"`` otherwise. ``solo_ratio_threshold`` is the parallelism ratio a
    plan must reach to swarm under this headroom — the standard 1.5 rule at
    HIGH, the raised ``auto.low_headroom_ratio`` at LOW."""

    level: Literal["low", "high"]
    available_for_swarm: int
    mean_provider_pressure: float
    low_headroom_slots: int
    solo_ratio_threshold: float


def swarm_headroom(
    available_for_swarm: int | None = None,
    provider_pressures: Sequence[float] | None = None,
    *,
    config: Mapping[str, Any] | None = None,
    db_path: str | None = None,
) -> SwarmHeadroom:
    """Classify fleet headroom for the auto solo-vs-swarm decision.

    Pure given its inputs: pass ``available_for_swarm`` (free swarm session
    slots, ``limit_state.fleet_available().available_for_swarm``) and
    ``provider_pressures`` (one ``[0,1]`` pressure per ENABLED provider) and
    the classification is deterministic — tests fake both. Either omitted
    input is read live from the limit-state authority (``fleet_available`` /
    ``provider_pressure`` over ``enabled_providers``), scoped to ``db_path``
    when given so intake can point it at its own control-plane DB.

    LOW headroom — ``available_for_swarm < auto.low_headroom_slots`` OR mean
    pressure ≥ :data:`LOW_HEADROOM_PRESSURE` (no enabled provider at all
    counts as fully pressured) — RAISES the solo threshold to
    ``auto.low_headroom_ratio`` (default 2.5). HIGH headroom keeps the
    standard :data:`SOLO_RATIO_THRESHOLD` rule.
    """
    from omniagentos.routing import limit_state

    if config is None:
        config = limit_state.load_swarm_config()
    auto_raw = config.get("auto")
    auto_cfg: Mapping[str, Any] = auto_raw if isinstance(auto_raw, Mapping) else {}
    try:
        low_slots = int(auto_cfg.get("low_headroom_slots", DEFAULT_LOW_HEADROOM_SLOTS))
    except (TypeError, ValueError):
        low_slots = DEFAULT_LOW_HEADROOM_SLOTS
    try:
        low_ratio = float(auto_cfg.get("low_headroom_ratio", DEFAULT_LOW_HEADROOM_RATIO))
    except (TypeError, ValueError):
        low_ratio = DEFAULT_LOW_HEADROOM_RATIO

    if available_for_swarm is None:
        available_for_swarm = limit_state.fleet_available(db_path=db_path).available_for_swarm
    if provider_pressures is None:
        provider_pressures = [
            limit_state.provider_pressure(provider, db_path=db_path)
            for provider in limit_state.enabled_providers(db_path=db_path)
        ]
    pressures = [min(1.0, max(0.0, float(p))) for p in provider_pressures]
    mean_pressure = (sum(pressures) / len(pressures)) if pressures else 1.0

    low = available_for_swarm < low_slots or mean_pressure >= LOW_HEADROOM_PRESSURE
    return SwarmHeadroom(
        level="low" if low else "high",
        available_for_swarm=int(available_for_swarm),
        mean_provider_pressure=round(mean_pressure, 3),
        low_headroom_slots=low_slots,
        solo_ratio_threshold=low_ratio if low else SOLO_RATIO_THRESHOLD,
    )


def fast_speed_headroom(headroom: SwarmHeadroom | None) -> SwarmHeadroom | None:
    """Fastest-dial topology bias: ``speed=="fast"`` raises the solo threshold
    to the low-headroom bar (``auto.low_headroom_ratio``, default 2.5) — on the
    fastest lane a DAG's coordination overhead must pay off harder before
    swarming beats one quick session. Never lowers an already-raised threshold;
    ``None`` (headroom fault) keeps the standard rule untouched."""
    if headroom is None:
        return None
    from omniagentos.routing import limit_state

    try:
        auto_raw = limit_state.load_swarm_config().get("auto")
        auto_cfg: Mapping[str, Any] = auto_raw if isinstance(auto_raw, Mapping) else {}
        ratio = float(auto_cfg.get("low_headroom_ratio", DEFAULT_LOW_HEADROOM_RATIO))
    except Exception:  # noqa: BLE001 -- a config fault keeps the code-side floor.
        ratio = DEFAULT_LOW_HEADROOM_RATIO
    if headroom.solo_ratio_threshold >= ratio:
        return headroom
    return replace(headroom, solo_ratio_threshold=ratio)


def is_solo(
    tasks: Sequence[SwarmTaskSpec], ratio: float, headroom: SwarmHeadroom | None = None
) -> bool:
    """Solo rule: ≤2 worker tasks or ratio below the solo threshold → sequential
    Orchestrator. The threshold is the standard 1.5 without fleet context;
    a ``headroom`` snapshot substitutes its own (raised to
    ``auto.low_headroom_ratio`` when headroom is LOW)."""
    threshold = headroom.solo_ratio_threshold if headroom is not None else SOLO_RATIO_THRESHOLD
    return len(tasks) <= SOLO_MAX_TASKS or ratio < threshold


def _task_shape_value(task: Any, field: str, default: Any) -> Any:
    if isinstance(task, Mapping):
        return task.get(field, default)
    return getattr(task, field, default)


def _shape_tasks(tasks: Sequence[Any]) -> list[Any]:
    """Exclude planner-owned serialization tasks from task-shape evidence."""
    return [
        task
        for task in tasks
        if str(_task_shape_value(task, "id", "") or "") not in _RESERVED_TASK_IDS
    ]


def _compute_disjoint_dag_width(tasks: Sequence[Any]) -> int:
    """Return the DAG's maximum antichain width (maximum safe fan-out).

    Dilworth's theorem computes this as ``node count - maximum matching`` on
    the dependency graph's transitive closure. Planner-added integration and
    bootstrap tasks are deliberately excluded because they serialize every
    plan equally and are not independently owned worker sections.
    """
    worker_tasks = _shape_tasks(tasks)
    task_ids = {
        str(_task_shape_value(task, "id", "") or "")
        for task in worker_tasks
        if str(_task_shape_value(task, "id", "") or "")
    }
    if not task_ids:
        return 1

    edges: dict[str, set[str]] = {task_id: set() for task_id in task_ids}
    for task in worker_tasks:
        task_id = str(_task_shape_value(task, "id", "") or "")
        deps = _task_shape_value(task, "depends_on", ()) or ()
        edges[task_id].update(str(dep) for dep in deps if str(dep) in task_ids)

    reachable: dict[str, set[str]] = {}
    for task_id in task_ids:
        seen: set[str] = set()
        stack = list(edges[task_id])
        while stack:
            dependency = stack.pop()
            if dependency in seen:
                continue
            seen.add(dependency)
            stack.extend(edges[dependency] - seen)
        reachable[task_id] = seen

    matched_right: dict[str, str] = {}

    def augment(left: str, seen_right: set[str]) -> bool:
        for right in reachable[left]:
            if right in seen_right:
                continue
            seen_right.add(right)
            current = matched_right.get(right)
            if current is None or augment(current, seen_right):
                matched_right[right] = left
                return True
        return False

    matching = sum(1 for task_id in task_ids if augment(task_id, set()))
    return max(1, len(task_ids) - matching)


def _check_disjoint_owned_paths(tasks: Sequence[Any]) -> bool:
    """True iff worker tasks have pairwise-disjoint explicit owned paths."""
    all_paths = [
        {str(path) for path in (_task_shape_value(task, "owned_paths", ()) or ()) if str(path)}
        for task in _shape_tasks(tasks)
    ]
    # Containment, not set equality: {"src"} vs {"src/a.py"} are NOT disjoint,
    # but `isdisjoint` on raw strings reports that they are — which would call a
    # genuinely overlapping pair of tasks safe to run concurrently.
    return all(
        not paths_overlap(left_path, right_path)
        for left in range(len(all_paths))
        for right in range(left + 1, len(all_paths))
        for left_path in all_paths[left]
        for right_path in all_paths[right]
    )


# ---------------------------------------------------------------------------
# Plan assembly (pure)
# ---------------------------------------------------------------------------


def build_plan(
    goal: str,
    raw_tasks: Sequence[Mapping[str, Any]],
    *,
    assumptions: Sequence[str] = (),
    needs_install: bool = False,
    install_command: str = "",
    suite_command: str = "",
    headroom: SwarmHeadroom | None = None,
    category: str | None = None,
    decision_sink: Callable[[Mapping[str, Any]], None] | None = None,
    workspace_dir: str | Path | None = None,
) -> SwarmPlan:
    """Validate raw planner tasks into a :class:`SwarmPlan` (pure, deterministic).

    Raises :class:`SwarmPlanError` on anything that cannot be made safe; the
    caller treats that like invalid plan JSON (one re-prompt, then the offline
    fallback). Validation repairs — dropped deps, cycle repairs, shared-file
    reassignment — are recorded as assumptions, never silent.

    ``headroom`` (a :class:`SwarmHeadroom` snapshot) makes the solo-vs-swarm
    rule rate-limit-aware: LOW headroom raises the swarm bar to its
    ``solo_ratio_threshold``, and the decision plus its inputs are recorded as
    an assumption (visible in plan_json / the UI summary). ``None`` keeps the
    standard rule with no note — pure planning stays fleet-independent.
    """
    notes: list[str] = list(assumptions)
    tasks = [spec for spec in (_parse_task_spec(raw) for raw in raw_tasks) if spec]
    if not tasks:
        raise SwarmPlanError("plan contains no usable tasks")
    if len(tasks) > MAX_TASKS:
        raise SwarmPlanError(f"task cap exceeded: {len(tasks)} > {MAX_TASKS}")

    seen_ids: set[str] = set()
    for task in tasks:
        if task.id in _RESERVED_TASK_IDS:
            raise SwarmPlanError(f"task id {task.id!r} is reserved for the planner")
        if task.id in seen_ids:
            raise SwarmPlanError(f"duplicate task id: {task.id!r}")
        seen_ids.add(task.id)

    # Owned paths: workspace-relative + contained (raises), then shared files
    # move to integration ownership (recorded).
    for task in tasks:
        if workspace_dir is not None:
            resolved: list[str] = []
            for raw_path in task.owned_paths:
                bound, note = resolve_owned_path(raw_path, workspace_dir)
                if note:
                    notes.append(f"task '{task.id}': {note}")
                resolved.append(bound)
            normalized = list(dict.fromkeys(resolved))
        else:
            normalized = list(dict.fromkeys(normalize_owned_path(p) for p in task.owned_paths))
        shared = [p for p in normalized if is_shared_path(p)]
        if shared:
            notes.append(
                f"shared file(s) moved from task '{task.id}' to integration "
                f"ownership: {', '.join(shared)}"
            )
        task.owned_paths = [p for p in normalized if p not in shared]

    # Deps: drop unknown/self references (recorded), then topo + one repair pass.
    for task in tasks:
        kept: list[str] = []
        dropped: list[str] = []
        for dep in dict.fromkeys(task.depends_on):
            (kept if dep in seen_ids and dep != task.id else dropped).append(dep)
        if dropped:
            notes.append(
                f"dropped invalid dependency(ies) on task '{task.id}': "
                + ", ".join(str(d) for d in dropped)
            )
        task.depends_on = kept

    _, removed_edges = topo_sort_with_repair(tasks)
    for task_id, dep in removed_edges:
        notes.append(f"cycle repaired: removed dependency '{task_id}' -> '{dep}'")
        for task in tasks:
            if task.id == task_id and dep in task.depends_on:
                task.depends_on.remove(dep)

    for task_id, dep in add_ownership_overlap_edges(tasks):
        notes.append(
            f"ownership overlap: task '{task_id}' now depends on '{dep}' "
            "(later task serialized behind the earlier)"
        )

    # Parallelism + mode over the worker tasks only (auto tasks serialize every
    # plan equally and would just blur the signal).
    ratio, target_n = parallelism_stats(tasks)
    solo = is_solo(tasks, ratio, headroom)
    if headroom is not None:
        # The rate-limit-aware decision AND its inputs, recorded where the UI
        # and run summary already surface assumptions (plan_json.assumptions).
        notes.append(
            f"auto headroom {headroom.level.upper()} "
            f"(available_for_swarm={headroom.available_for_swarm}, "
            f"mean_provider_pressure={headroom.mean_provider_pressure}, "
            f"low_headroom_slots={headroom.low_headroom_slots}): "
            f"swarm requires >{SOLO_MAX_TASKS} tasks and parallelism ratio >= "
            f"{headroom.solo_ratio_threshold}; tasks={len(tasks)}, "
            f"ratio={round(ratio, 3)} -> {'solo' if solo else 'swarm'}"
        )
    # Volume-III allocation brain (advisory): audit DAG + quality-first fan-out.
    char = None
    alloc = None
    try:
        from omniagentos.allocation.audit import audit_decomposition
        from omniagentos.allocation.capacity import DEFAULT_REPO_WRITER_SLOTS
        from omniagentos.allocation.characterize import characterize
        from omniagentos.allocation.fanout import decide_fanout

        raw_for_audit = [
            {
                "id": t.id,
                "title": t.title,
                "depends_on": list(t.depends_on),
                "owned_paths": list(t.owned_paths),
                "acceptance": t.acceptance,
                "verify_command": t.verify_command or "",
                "est_agent_minutes": t.est_agent_minutes,
            }
            for t in tasks
        ]
        audit = audit_decomposition(raw_for_audit)
        if not audit.ok:
            notes.append(
                "decomposition_audit: FAIL "
                + "; ".join(
                    f"{f.code}:{f.message}" for f in audit.findings if f.severity == "error"
                )[:400]
            )
        elif audit.findings:
            notes.append(
                "decomposition_audit: " + "; ".join(f"{f.code}" for f in audit.findings[:8])
            )
        free = headroom.available_for_swarm if headroom is not None else max(target_n, 1)
        # Root-layer count alone is not DAG width: a single setup root can still
        # open a wide antichain of independent work later in the plan.
        disjoint_dag_width = _compute_disjoint_dag_width(tasks)
        units = max(1, len(audit.independent_units) or len(tasks), disjoint_dag_width)
        # Real signals (HANDOFF Phase 3): risk class, acceptance coverage, est time.
        risk_scores = []
        accept_scores = []
        est_minutes = []
        for t in tasks:
            rc = str(getattr(t, "risk_class", None) or "reversible_internal").lower()
            if "irreversible" in rc:
                risk_scores.append(0.9)
            elif "external" in rc or "bounded" in rc:
                risk_scores.append(0.6)
            else:
                risk_scores.append(0.3)
            acc = str(getattr(t, "acceptance", None) or "").strip()
            accept_scores.append(0.9 if acc else 0.4)
            est_minutes.append(float(getattr(t, "est_agent_minutes", None) or 30.0))
        uncertainty = 1.0 - (sum(accept_scores) / max(1, len(accept_scores)))
        risk = sum(risk_scores) / max(1, len(risk_scores))
        verifiable = sum(accept_scores) / max(1, len(accept_scores))
        work_volume = sum(est_minutes) / 30.0  # normalize ~agent-hours of 0.5h units

        # Formation FIRST (F4): when confident, formation topology owns team shape.
        form_topo: str | None = None
        form_low = False
        try:
            from omniagentos.formation import (
                CONFIDENCE_THRESHOLD,
                is_low_confidence,
                select_formation_with_confidence,
                topology_for_formation,
            )

            goal_text = (
                goal
                if isinstance(goal, str)
                else str(getattr(goal, "title", None) or getattr(goal, "text", None) or "")
            )
            _pre_sel = select_formation_with_confidence(
                goal=goal_text,
                task_class=(category or "").strip() or None,
            )
            form_low = is_low_confidence(_pre_sel)
            form_topo = topology_for_formation(_pre_sel.formation.id)
            if form_low:
                form_topo = "sequential"  # F5/F6: unsure → do not invent parallel shape
        except Exception:  # noqa: BLE001
            form_topo = None

        sequential_flag = solo or ratio < SOLO_RATIO_THRESHOLD
        # Only confident formation topology overrides ratio math (F4).
        # Low-confidence must not collapse a high-ratio plan to sequential.
        if form_topo == "sequential" and not form_low:
            sequential_flag = True
        elif form_topo and form_topo != "sequential" and not form_low:
            sequential_flag = False  # formation wants parallel structure

        char = characterize(
            {
                "has_partitions": units > 1 and ratio >= SOLO_RATIO_THRESHOLD,
                "sequential": sequential_flag,
                "uncertainty": uncertainty,
                "risk": risk,
                "verifiable": verifiable,
                "independent_units": units,
                "work_volume": float(work_volume),
                # Prefer formation topology when confident (single owner for team shape).
                "preferred_topology": form_topo if form_topo and not form_low else None,
            }
        )
        alloc = decide_fanout(
            char,
            free_slots=max(1, int(free)),
            writer_slots=DEFAULT_REPO_WRITER_SLOTS,
            verifier_capacity=2,
            independent_units=units,
            preferred_topology=form_topo if form_topo and not form_low else None,
            disjoint_dag_width=disjoint_dag_width,
        )
        # F4: when formation is confident, record formation topology as authoritative
        # even if allocate returns a different label (execution still uses fanout sizing).
        topology_note: str = alloc.topology
        if form_topo and not form_low:
            topology_note = form_topo
        notes.append(
            f"allocation: topology={topology_note} workers={alloc.worker_count} "
            f"verifiers={alloc.verifier_count} hard_cap={alloc.hard_capacity} "
            f"risk={risk:.2f} uncertainty={uncertainty:.2f} verifiable={verifiable:.2f} "
            f"— {alloc.rationale}" + (f" (formation_topology={form_topo})" if form_topo else "")
        )
        if not solo and alloc.worker_count >= 1:
            prior = target_n
            target_n = min(max(target_n, 1), alloc.worker_count)
            if target_n < prior:
                notes.append(
                    f"fanout_cap: reduced target_n {prior}->{target_n} "
                    f"(alloc.worker_count={alloc.worker_count}, units={units})"
                )
    except Exception:  # noqa: BLE001 — planning must never fail on advisory allocation
        notes.append("allocation: unavailable (characterization skipped)")

    # Formation Engine (Phase B): bind team structure to the plan (not notes-only).
    # Notes remain human-readable; SwarmPlan.formation is the source of truth.
    formation_binding = None
    try:
        from omniagentos.formation import (
            CONFIDENCE_THRESHOLD,
            is_low_confidence,
            select_formation_with_confidence,
            topology_for_formation,
        )
        from omniagentos.swarm.contracts import FormationBinding

        goal_text = (
            goal
            if isinstance(goal, str)
            else str(getattr(goal, "title", None) or getattr(goal, "text", None) or "")
        )
        selection = select_formation_with_confidence(
            goal=goal_text,
            task_class=(category or "").strip() or None,
        )
        form = selection.formation
        low = is_low_confidence(selection)
        topo = topology_for_formation(form.id)
        if low:
            topo = "sequential"
        formation_binding = FormationBinding(
            id=form.id,
            # Low confidence: keep formation id for diagnostics but do NOT bias
            # routing implementers — T1 forbids silent sure routing (R2-F2).
            implementers=[] if low else list(form.implementers),
            reviewer=form.reviewer,
            planner=form.planner,
            mechanical_gate=False if low else form.mechanical_gate,
            confidence=selection.confidence,
            topology=topo,
            reason=selection.reason + (f"; below_threshold={CONFIDENCE_THRESHOLD}" if low else ""),
            low_confidence=low,
        )
        # Empty implementers= (not a placeholder token) so notes-only parsers
        # get [] rather than treating "(none-low-conf)" as a model name.
        impl_note = ",".join(formation_binding.implementers)
        notes.append(
            f"formation: {formation_binding.id} "
            f"implementers={impl_note} "
            f"reviewer={formation_binding.reviewer} "
            f"mechanical={formation_binding.mechanical_gate} "
            f"confidence={selection.confidence} reason={selection.reason}"
            + ("; LOW_CONFIDENCE" if low else "")
        )
        if low:
            notes.append(
                f"formation_low_confidence: threshold={CONFIDENCE_THRESHOLD} "
                f"— surface in UI; prefer sequential execution"
            )
    except Exception:  # noqa: BLE001 — binding is preferred but must not fail planning
        notes.append("formation: unavailable")

    # Task-shape router block (A2+A3)
    try:
        from omniagentos.allocation.config import (
            router_config_version,
            router_min_confidence,
            task_shape_router_mode,
        )

        mode = task_shape_router_mode()
        if mode != "off" and char is not None and alloc is not None:
            import time

            from omniagentos.allocation.arbiter import decide_route

            start_t = time.perf_counter()
            min_conf = router_min_confidence()
            decision = decide_route(
                char,
                alloc,
                formation_binding,
                ratio,
                solo,
                min_confidence=min_conf,
            )
            latency_ms = (time.perf_counter() - start_t) * 1000.0

            # Advisory CBM probe
            cbm_parallel_candidates = None
            try:
                from omniagentos.cbm.service import CognitiveBudgetService

                cbm_service = CognitiveBudgetService()
                cbm_res = cbm_service.recommend_rung()
                cbm_parallel_candidates = cbm_res.get("parallel_candidates")
            except Exception:
                pass

            applied = False
            if mode == "enforce":
                applied = True
                if not solo:
                    topo = decision.topology
                    h_cap = getattr(alloc, "hard_capacity", 1)
                    from omniagentos.allocation.fanout import TOPOLOGY_CAPS

                    topo_cap = TOPOLOGY_CAPS.get(topo, h_cap)

                    target_n = min(decision.worker_count, h_cap, topo_cap)

                if formation_binding is not None:
                    formation_binding.topology = decision.topology

                notes.append(
                    f"router: applied task-shape route decision: route={decision.route} "
                    f"topology={decision.topology} worker_count={decision.worker_count} "
                    f"— {decision.rationale}"
                )

            # Build payload
            payload = {
                "brief": goal.strip() if goal else "",
                "route": decision.route,
                "topology": decision.topology,
                "worker_count": decision.worker_count,
                "rationale": decision.rationale,
                "applied": applied,
                "char": char,
                "confidence": getattr(char, "confidence", None),
                "task_class": getattr(char, "task_class", None),
                "config_version": router_config_version(),
                "latency_ms": latency_ms,
                "cbm_parallel_candidates": cbm_parallel_candidates,
            }

            if decision_sink is not None:
                try:
                    decision_sink(payload)
                except Exception:
                    pass

    except Exception:
        pass

    integration_task_id: str | None = None

    if solo:
        target_n = 1
    else:
        if needs_install:
            bootstrap = SwarmTaskSpec(
                id=BOOTSTRAP_TASK_ID,
                title="Bootstrap: install dependencies",
                description=(
                    "Install project dependencies before any worker starts. "
                    "Every other task depends on this one. The requested installer "
                    f"is recorded for the worker to review: {install_command or '(unspecified)'}"
                ),
                complexity="simple",
                risk_class="none",
                est_manual_minutes=15,
                est_agent_minutes=5,
                owned_paths=[],
                acceptance="Dependencies installed; the workspace builds/imports cleanly.",
                # install_command is planner-authored setup guidance, not a
                # verifier. It must never be promoted into an automatic gate.
                verify_command="",
            )
            for task in tasks:
                task.depends_on.insert(0, BOOTSTRAP_TASK_ID)
            tasks.insert(0, bootstrap)

        worker_ids = [task.id for task in tasks]
        depended_on = {dep for task in tasks for dep in task.depends_on}
        leaves = [task_id for task_id in worker_ids if task_id not in depended_on]
        total_agent = sum(max(0, task.est_agent_minutes) for task in tasks)
        integration = SwarmTaskSpec(
            id=INTEGRATION_TASK_ID,
            title="Integration: merge, verify, full suite",
            description=(
                "Integrate all completed task results in the shared workspace, "
                "resolve cross-task friction, and run the full verification suite."
            ),
            depends_on=leaves,
            complexity="standard",
            risk_class="none",
            est_manual_minutes=max(30, (total_agent * 3 + 4) // 5),
            est_agent_minutes=max(10, (total_agent + 4) // 5),
            owned_paths=["."],
            acceptance=(
                "All task verify commands pass on the integrated workspace and "
                "the full suite is green."
            ),
            verify_command=suite_command,
        )
        tasks.append(integration)
        integration_task_id = INTEGRATION_TASK_ID

    # Independent off -> shadow -> enforce gates. Unset/invalid values are off.
    creative_mode = os.environ.get("OMNIAGENTOS_CREATIVE_TOPOLOGY_MODE", "off").strip().lower()
    fanout_mode = os.environ.get("OMNIAGENTOS_TASK_SHAPE_FANOUT_MODE", "off").strip().lower()
    creative_mode = creative_mode if creative_mode in {"off", "shadow", "enforce"} else "off"
    fanout_mode = fanout_mode if fanout_mode in {"off", "shadow", "enforce"} else "off"

    is_research = False
    is_coding = False
    if formation_binding is not None:
        if formation_binding.id == "research":
            is_research = True
        elif formation_binding.id == "coding":
            is_coding = True
    else:
        goal_lower = goal.lower()
        if "research" in goal_lower or "evidence" in goal_lower:
            is_research = True
        elif "code" in goal_lower or "bug" in goal_lower or "implement" in goal_lower:
            is_coding = True

    from omniagentos.allocation.fanout import (
        PARALLEL_SECTIONS_FLOOR,
        RESEARCH_SHAPED_TARGET,
        TOPOLOGY_CAPS,
    )

    # Baselines are captured BEFORE any enforce mutation so shadow/enforce
    # evidence rows always compare the true pre-change plan to the challenger.
    baseline_topology = formation_binding.topology if formation_binding else None
    baseline_worker_count = target_n

    # Creative enforce is applied by the EARLY path only: selector's
    # topology_for_formation returns parallel_sections (confidence-guarded via
    # form_low -> preferred_topology=None), decide_fanout sizes it with the
    # topology floor/cap, and the router block clamps through TOPOLOGY_CAPS.
    # A second late mutation here would bypass those coordinated caps and the
    # low-confidence guard, so there deliberately is none.

    research_target = min(
        max(RESEARCH_SHAPED_TARGET, PARALLEL_SECTIONS_FLOOR),
        TOPOLOGY_CAPS["parallel_sections"],
    )
    if fanout_mode == "enforce":
        from omniagentos.allocation.config import task_shape_router_mode

        router_enforced = task_shape_router_mode() == "enforce"
        if is_research and not solo and not form_low and not router_enforced:
            # Widening default for open-ended research only: never un-solos a
            # plan, never exceeds real capacity, never shrinks a wider plan —
            # and never overrides the task-shape router's own enforce sizing
            # (when both gates are live, the router's clamp is authoritative).
            h_cap = getattr(alloc, "hard_capacity", 1) if alloc is not None else 1
            widened = max(target_n, min(research_target, max(1, h_cap)))
            if widened != target_n:
                target_n = widened
                notes.append("fanout_default: research task shape targeted ~5 workers")
        elif is_research and router_enforced:
            notes.append("fanout_default: research widen deferred to task-shape router enforce")
        elif is_coding:
            notes.append("fanout_default: coding task shape set to plan-then-implement")

    # Emit one JSONL evidence row in shadow and enforce so promotion can require
    # observed width > 2 plus disjoint ownership, without changing the plan in
    # shadow mode.
    if creative_mode != "off" or fanout_mode != "off":
        dag_width = _compute_disjoint_dag_width(tasks)
        disjoint_paths = _check_disjoint_owned_paths(tasks)
        cap = TOPOLOGY_CAPS["parallel_sections"]

        log_row = {
            "event": "task_shape_topology",
            "goal": goal.strip(),
            "creative_mode": creative_mode,
            "fanout_mode": fanout_mode,
            "disjoint_dag_width": dag_width,
            "disjoint_owned_paths": disjoint_paths,
            "task_count": len(tasks),
            "baseline_topology": baseline_topology,
            "baseline_worker_count": baseline_worker_count,
            "challenger_creative_topology": "parallel_sections",
            "challenger_creative_generator_count": min(
                max(dag_width, PARALLEL_SECTIONS_FLOOR), cap
            ),
            "challenger_creative_critic_count": 1,
            "applied_worker_count": target_n,
        }

        if is_research:
            log_row["challenger_route"] = "research-fanout"
            log_row["challenger_worker_count"] = research_target
        elif is_coding:
            log_row["challenger_route"] = "plan-then-implement"
            log_row["challenger_worker_count"] = target_n

        try:
            log_file = Path("var/swarm/shadow_topology.jsonl")
            log_file.parent.mkdir(parents=True, exist_ok=True)
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(log_row) + "\n")
        except OSError as exc:
            LOG.warning("failed to write task-shape topology row: %s", exc)

    return SwarmPlan(
        goal=goal.strip(),
        tasks=tasks,
        assumptions=notes,
        parallelism_ratio=round(ratio, 3),
        integration_task_id=integration_task_id,
        mode="solo" if solo else "swarm",
        version=1,
        target_n=target_n,
        category=(category or "").strip() or None,
        formation=formation_binding,
    )


def _parse_task_spec(raw: Mapping[str, Any]) -> SwarmTaskSpec | None:
    if not isinstance(raw, Mapping):
        return None
    task_id = str(raw.get("id", "")).strip()
    title = str(raw.get("title", "")).strip()
    if not task_id and not title:
        return None
    if not task_id:
        task_id = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:48] or "task"
    complexity = str(raw.get("complexity", "standard")).strip().lower()
    if complexity not in ("simple", "standard", "complex"):
        complexity = "standard"
    risk = str(raw.get("risk_class", "none")).strip().lower()
    if risk not in ("none", "external", "deploy", "destructive"):
        risk = "none"
    deps_raw = raw.get("depends_on")
    deps = (
        [str(d).strip() for d in deps_raw if str(d).strip()] if isinstance(deps_raw, list) else []
    )
    paths_raw = raw.get("owned_paths")
    paths = (
        [str(p).strip() for p in paths_raw if str(p).strip()] if isinstance(paths_raw, list) else []
    )
    return SwarmTaskSpec(
        id=task_id,
        title=title or task_id,
        description=str(raw.get("description", "")).strip(),
        depends_on=deps,
        complexity=complexity,
        risk_class=risk,  # type: ignore[arg-type]
        est_manual_minutes=_coerce_minutes(
            raw.get("est_manual_minutes"), _DEFAULT_EST_MANUAL_MINUTES
        ),
        est_agent_minutes=_coerce_minutes(raw.get("est_agent_minutes"), _DEFAULT_EST_AGENT_MINUTES),
        owned_paths=paths,
        tier_hint=(str(raw.get("tier_hint", "")).strip() or None),
        acceptance=str(raw.get("acceptance", "")).strip(),
        verify_command=str(raw.get("verify_command", "")).strip(),
        category=(str(raw.get("category", "")).strip() or None),
    )


def _coerce_minutes(value: Any, default: int) -> int:
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        return default
    return minutes if minutes > 0 else default


# ---------------------------------------------------------------------------
# Plan fingerprint — version + sha256 for brief-hash verification
# ---------------------------------------------------------------------------


def plan_fingerprint(plan: SwarmPlan) -> str:
    """sha256 over the canonical JSON dump of the plan (stable across identical
    plans; any material change — task, dep, estimate, version — changes it)."""
    payload = json.dumps(plan.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def plan_payload(plan: SwarmPlan) -> dict[str, Any]:
    """The authoritative ``swarm_runs.plan_json`` value: the plan dump plus its
    own version (already a field) and content hash."""
    dump = plan.model_dump(mode="json")
    dump["plan_hash"] = plan_fingerprint(plan)
    return dump


def formation_swarm_json_fields(
    formation: Any | None,
    *,
    role: str = "implementer",
) -> dict[str, Any]:
    """Stamp keys for child/root ``swarm_json`` from a plan formation binding.

    Pure helper so provision and tests share one shape. Empty when unbound.
    """
    if formation is None:
        return {}
    implementers = list(getattr(formation, "implementers", None) or [])
    low = bool(getattr(formation, "low_confidence", False))
    return {
        "formation_id": str(getattr(formation, "id", "") or ""),
        "formation_implementers": implementers,
        "formation_reviewer": str(getattr(formation, "reviewer", "") or ""),
        "formation_mechanical_gate": bool(getattr(formation, "mechanical_gate", True)),
        "formation_role": role,
        "formation_low_confidence": low,
        **({"formation_topology": topo} if (topo := getattr(formation, "topology", None)) else {}),
        **(
            {"formation_confidence": conf}
            if (conf := getattr(formation, "confidence", None)) is not None
            else {}
        ),
    }


# ---------------------------------------------------------------------------
# Prior-lessons block
# ---------------------------------------------------------------------------


# The optimizer playbook remains DISARMED by default as an explicit rollout
# brake. C1 repaired the historical empty-set and requested-width objective
# inversions in summary.py/optimize.py; an operator can now opt back in with
# OMNIAGENTOS_SWARM_PLAYBOOK_FEED=1 after validating learned data for the
# deployment rather than having a code change silently arm the feedback loop.
#
# The guard makes enablement explicit rather than dependent on whether a
# learned-data file or optimizer job happens to exist.
#
# The dominance corpus (W4-01) and empty-set null discipline (W4-03) are the
# mechanical preconditions for enabling the feed.
PLAYBOOK_FEED_ENV = "OMNIAGENTOS_SWARM_PLAYBOOK_FEED"


def playbook_feed_enabled() -> bool:
    """Whether the optimizer playbook may enter a planning prompt."""
    return os.environ.get(PLAYBOOK_FEED_ENV, "0") == "1"


def build_lessons_block(
    goal: str,
    *,
    dal: SwarmDal | None = None,
    recall_fn: RecallFn | None = None,
    playbook_path: str | Path | None = None,
    history_limit: int = 5,
    token_cap: int = LESSONS_TOKEN_CAP,
    company_id: str | None = None,
    domain_tags: Sequence[str] | None = None,
) -> str:
    """Assemble the prior-lessons context block (hard-capped, never raises).

    Sources, in priority order: small-k ambient capabilities, last
    ``history_limit`` terminal swarm runs (goal/status/metrics/error), a general
    knowledge-recall block for discipline "swarm", and the optimizer playbook.
    """
    sections: list[str] = []
    try:
        capabilities = default_recall_capabilities(goal, company_id=company_id, domains=domain_tags)
        if capabilities:
            sections.append("AMBIENT CAPABILITIES (company + estate):\n" + capabilities)
    except Exception:  # noqa: BLE001 -- advisory and fail-open.
        LOG.debug("swarm planner capability recall failed", exc_info=True)

    try:
        if dal is not None:
            terminal = [
                run
                for run in dal.list_runs()
                if str(run.get("status")) in ("completed", "failed", "cancelled")
            ][-max(0, history_limit) :]
            if terminal:
                lines = ["PRIOR SWARM RUNS (newest last):"]
                for run in terminal:
                    line = (
                        f"- [{run.get('status')}] {str(run.get('goal', ''))[:120]}"
                        f" | metrics: {str(run.get('metrics_json', '{}'))[:160]}"
                    )
                    error = str(run.get("error") or "").strip()
                    if error:
                        line += f" | error: {error[:120]}"
                    lines.append(line)
                sections.append("\n".join(lines))
    except Exception:  # noqa: BLE001 -- lessons are advisory.
        LOG.debug("swarm planner could not read run history", exc_info=True)

    try:
        block = (recall_fn or default_recall_lessons)(goal)
        if block:
            sections.append(block)
    except Exception:  # noqa: BLE001 -- lessons are advisory.
        LOG.debug("swarm planner recall failed", exc_info=True)

    try:
        path = Path(playbook_path) if playbook_path is not None else DEFAULT_PLAYBOOK_PATH
        if path.is_file() and playbook_feed_enabled():
            text = path.read_text(encoding="utf-8").strip()
            if text:
                sections.append("OPTIMIZER PLAYBOOK (advisory):\n" + text)
    except Exception:  # noqa: BLE001 -- lessons are advisory.
        LOG.debug("swarm planner could not read playbook", exc_info=True)

    combined = "\n\n".join(sections)
    return combined[: max(0, token_cap) * _CHARS_PER_TOKEN]


# ---------------------------------------------------------------------------
# Fable schema + prompt
# ---------------------------------------------------------------------------

_SWARM_TASK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["id", "title"],
    "properties": {
        "id": {"type": "string"},
        "title": {"type": "string"},
        "description": {"type": "string"},
        "depends_on": {"type": "array", "items": {"type": "string"}},
        "complexity": {"type": "string", "enum": ["simple", "standard", "complex"]},
        "risk_class": {
            "type": "string",
            "enum": ["none", "external", "deploy", "destructive"],
        },
        "est_manual_minutes": {"type": "integer"},
        "est_agent_minutes": {"type": "integer"},
        "owned_paths": {"type": "array", "items": {"type": "string"}},
        "tier_hint": {"type": "string"},
        "acceptance": {"type": "string"},
        "verify_command": {
            "type": "string",
            "description": (
                "Strict verifier only: pytest/python -m pytest/ruff check/mypy/pyright "
                "with repo-relative targets, or exact git diff --check"
            ),
        },
        "category": {"type": "string"},
    },
}

_BUNDLE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["goal", "tasks"],
    "properties": {
        "goal": {"type": "string"},
        "tasks": {"type": "array", "items": _SWARM_TASK_SCHEMA},
        "assumptions": {"type": "array", "items": {"type": "string"}},
        "needs_install": {"type": "boolean"},
        "install_command": {"type": "string"},
        "suite_command": {"type": "string"},
        "category": {"type": "string"},
    },
}

_SWARM_PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "goal": {"type": "string"},
        "assumptions": {"type": "array", "items": {"type": "string"}},
        "tasks": {"type": "array", "items": _SWARM_TASK_SCHEMA},
        "needs_install": {"type": "boolean"},
        "install_command": {"type": "string"},
        "suite_command": {"type": "string"},
        "category": {"type": "string"},
        "bundles": {"type": "array", "items": _BUNDLE_SCHEMA},
    },
}


def _build_delegation_costmodel_block(target_width: int) -> str:
    """Build the delegation cost-model decision block for the planner prompt.

    Explains the BENEFIT vs COST tradeoff for parallel delegation, HARD VETOES
    that prevent fan-out, and the dispatch protocol (smallest useful batch →
    wait → re-evaluate → next batch). The real configured width value is included
    so the prompt and enforcer always agree.
    """
    lines = [
        "DELEGATION COST-MODEL: Before fanning out tasks in parallel, apply this",
        "explicit decision block:",
        "",
        "  BENEFIT of delegation (parallel wall-clock speedup):",
        "  - Shorter wall-clock time when independent work can run concurrently",
        "  - Specialist capability isolation: each agent focuses on one task",
        "  - Context isolation: independent execution spaces reduce state conflicts",
        "",
        "  COST of delegation (sequential overhead):",
        "  - Agent startup overhead (seconds per session)",
        "  - Duplicate discovery: each agent re-learns the codebase context",
        "  - Coordination overhead: merging multiple agents' changes",
        "  - State-conflict risk: overlapping edits or dependent operations",
        "",
        "  HARD VETOES that block parallel dispatch (NEVER fan-out if ANY apply):",
        "  1. Inter-agent task dependency: when task_B depends_on task_A",
        "  2. Overlapping owned_paths: when two tasks' owned_paths overlap or nest",
        "  3. Shared critical state: when the work modifies the same files/structures",
        "",
        f"  Dispatch protocol (real configured width: {target_width}):",
        "  - Launch the SMALLEST useful batch (typically 2-3 independent tasks)",
        "  - Wait for all tasks in the batch to complete",
        "  - Re-evaluate remaining work against HARD VETOES",
        "  - Only then launch the next batch",
        "  - Never fan-out to more than " + str(target_width) + " parallel agents",
        "",
        "Apply this decision BEFORE returning task dependencies in your plan. If any",
        "task pair triggers a HARD VETO, add an explicit depends_on edge so they",
        "serialize (the planner will NOT fan them out in parallel).",
    ]
    return "\n".join(lines)


def _plan_prompt(
    goal: str, assumptions: Sequence[str], lessons: str, target_width: int | None = None
) -> str:
    lines = [
        "You are the SWARM PLANNER for OmniAgentOS: decompose a brief into a DAG of",
        "tasks that run CONCURRENTLY against ONE shared workspace (the scheduler",
        "may isolate each task in its own git worktree and merge on completion —",
        "either way, disjoint owned_paths are what keep tasks conflict-free).",
        "",
        f"BRIEF:\n{goal.strip() or '(empty)'}",
    ]
    if assumptions:
        lines += ["", "RECORDED ASSUMPTIONS:"]
        lines += [f"- {item}" for item in assumptions]
    if lessons:
        lines += ["", "PRIOR LESSONS (advisory):", lessons]

    if target_width is None:
        target_width = _target_cap()
    costmodel_block = _build_delegation_costmodel_block(target_width)
    lines += ["", costmodel_block, ""]

    lines += [
        "Rules:",
        f"- 2-{MAX_TASKS} tasks; each independently completable by one agent.",
        "- Per task: id (short slug), title, description (a self-contained worker",
        "  brief), depends_on (ids), complexity (simple|standard|complex),",
        "  risk_class (none|external|deploy|destructive), est_manual_minutes,",
        "  est_agent_minutes, owned_paths (workspace-RELATIVE files/dirs ONLY this",
        "  task edits — keep them DISJOINT across tasks), tier_hint, acceptance",
        "  (concrete done criteria), verify_command (a strict non-shell verifier:",
        "  pytest or python -m pytest, ruff check, mypy, or pyright with",
        "  repository-relative targets; or exact `git diff --check`), and",
        "  OPTIONALLY category (a short reusable label",
        "  grouping related work on the board, e.g. 'Backend', 'Dashboard'; a",
        "  top-level `category` for the whole goal fans out to tasks without one).",
        "- Do NOT create install/setup or final-integration tasks; instead set",
        "  needs_install (+ install_command) when dependencies must be installed",
        "  first, and suite_command to a strict verifier command using the grammar",
        "  above. Never include shell operators, redirection, environment assignments,",
        "  response files, absolute paths, traversal, or arbitrary executables. The planner",
        "  inserts bootstrap/integration tasks itself.",
        "- Never claim shared files (package manifests, lockfiles, migration dirs,",
        "  top-level configs) in owned_paths — integration owns them.",
        "- NEVER ask the human questions. Record every ambiguity as an entry in",
        "  `assumptions` and proceed on best judgment.",
        "- If the brief bundles SEVERAL UNRELATED asks, return one entry per ask in",
        "  `bundles` (each with its own goal + tasks) and leave top-level `tasks`",
        "  empty. Otherwise return a single task list and omit `bundles`.",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Clarify (bounded, non-blocking) + bundle extraction
# ---------------------------------------------------------------------------


def _clarify_nonblocking(
    brief_or_spec: Any, clarify_llm: ClarifyLLM | None
) -> tuple[str, list[str]]:
    """One bounded clarify pass that can NEVER wait on a human.

    A refined spec comes back → its text becomes the goal. Questions come back
    → each is recorded as an assumption and the raw brief stays the goal.
    Accepts a RefinedSpec-shaped object directly (already clarified upstream).
    """
    if hasattr(brief_or_spec, "title") and hasattr(brief_or_spec, "description"):
        spec = brief_or_spec
        parts = [str(spec.title).strip(), str(spec.description).strip()]
        criteria = list(getattr(spec, "acceptance_criteria", []) or [])
        if criteria:
            parts.append("Acceptance criteria:\n" + "\n".join(f"- {c}" for c in criteria))
        return "\n\n".join(p for p in parts if p), []

    brief = str(brief_or_spec)
    try:
        _, _, service = _intake_planning()
        result = service.clarify_intake(brief, None, llm=clarify_llm)
    except Exception:  # noqa: BLE001 -- clarify is best-effort; the brief stands on its own.
        LOG.debug("swarm planner clarify failed; using raw brief", exc_info=True)
        return brief, []

    if result.mode == "spec" and result.spec is not None:
        spec = result.spec
        parts = [spec.title.strip(), spec.description.strip()]
        if spec.acceptance_criteria:
            parts.append(
                "Acceptance criteria:\n" + "\n".join(f"- {c}" for c in spec.acceptance_criteria)
            )
        return "\n\n".join(p for p in parts if p), []

    assumptions = [
        f"Unresolved (swarm mode never blocks on a human — proceeding on best judgment): {q}"
        for q in (result.questions or [])
    ]
    return brief, assumptions


def _extract_bundles(raw: Mapping[str, Any], fallback_goal: str) -> list[dict[str, Any]]:
    """Normalize the model response into ≥1 bundle dicts (goal/tasks/flags)."""

    def _bundle(source: Mapping[str, Any], default_goal: str) -> dict[str, Any]:
        raw_assumptions = source.get("assumptions")
        assumptions_list = raw_assumptions if isinstance(raw_assumptions, list) else []
        return {
            "goal": str(source.get("goal", "")).strip() or default_goal,
            "tasks": source.get("tasks") if isinstance(source.get("tasks"), list) else [],
            "assumptions": [str(a).strip() for a in assumptions_list if str(a).strip()],
            "needs_install": bool(source.get("needs_install")),
            "install_command": str(source.get("install_command", "")).strip(),
            "suite_command": str(source.get("suite_command", "")).strip(),
            "category": str(source.get("category", "")).strip() or None,
        }

    # Top-level `assumptions` are merged by the caller for every bundle; strip
    # them here so the single-bundle paths do not double-count them.
    top_level = {key: value for key, value in raw.items() if key != "assumptions"}
    bundles_raw = raw.get("bundles")
    bundles = (
        [b for b in bundles_raw if isinstance(b, Mapping) and b.get("tasks")]
        if isinstance(bundles_raw, list)
        else []
    )
    if len(bundles) >= 2:
        return [_bundle(b, fallback_goal) for b in bundles]
    if len(bundles) == 1:
        return [_bundle({**top_level, **bundles[0]}, fallback_goal)]
    return [_bundle(top_level, fallback_goal)]


def _fallback_plan(goal: str, assumptions: Sequence[str], reason: str) -> SwarmPlan:
    """Represent planner failure without minting executable root-wide scope.

    Kept as a ``SwarmPlan`` for compatibility with read-only callers, but its
    empty mutating ownership and explicit failure marker make the central
    safety authority reject it at every provision/activation boundary.
    """
    first_line = next((ln.strip() for ln in goal.splitlines() if ln.strip()), goal.strip())
    title = (first_line or "Swarm task")[:120]
    try:
        _, planner_mod, _ = _intake_planning()
        complexity = planner_mod.estimate_complexity(goal)
    except Exception:  # noqa: BLE001 -- the fallback must survive anything.
        complexity = "standard"
    task = SwarmTaskSpec(
        id="task-1",
        title=title,
        description=goal.strip(),
        complexity=complexity,
        est_manual_minutes=60,
        est_agent_minutes=20,
        owned_paths=[],
        acceptance=f"Delivers on: {title}",
    )
    return SwarmPlan(
        goal=goal.strip(),
        tasks=[task],
        assumptions=[
            *assumptions,
            f"planner degraded to flat solo plan: {reason}",
            f"planner failed closed: {reason}",
        ],
        parallelism_ratio=1.0,
        integration_task_id=None,
        mode="solo",
        version=1,
        target_n=1,
    )


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def plan_swarm_bundles(
    brief_or_spec: Any,
    working_dir: str,
    *,
    planner_llm: SwarmPlannerLLM | None = None,
    clarify_llm: ClarifyLLM | None = None,
    dal: SwarmDal | None = None,
    recall_fn: RecallFn | None = None,
    playbook_path: str | Path | None = None,
    headroom: SwarmHeadroom | None = None,
    project_id: str | None = None,
    company_id: str | None = None,
) -> list[SwarmPlan]:
    """Plan a brief end-to-end; one :class:`SwarmPlan` per detected bundle.

    A brief with one coherent ask returns a single plan. A brief bundling N
    unrelated asks returns N plans, each with its own validated DAG — the
    caller provisions and routes each independently. Never raises and never
    blocks on a human: the model unavailable or invalid twice degrades to a
    flat single-task solo plan with an assumption recorded.

    ``headroom`` threads the caller's fleet snapshot (:func:`swarm_headroom`)
    into every bundle's solo-vs-swarm rule; ``None`` (the default) keeps
    planning fleet-independent so unit callers stay deterministic — intake's
    dispatch path is the one that measures and passes it.
    """
    # working_dir is now consumed: owned paths are bound to real filesystem
    # entries so a syntactically-valid-but-nonexistent path cannot produce a
    # silently reverted no-op branch (see resolve_owned_path).
    goal, assumptions = _clarify_nonblocking(brief_or_spec, clarify_llm)
    resolved_company = company_id
    if resolved_company is None and dal is not None and project_id:
        try:
            from omniagentos.knowledge.capabilities import resolve_company_id

            resolved_company = resolve_company_id(dal._connection, project_id)
        except Exception:  # noqa: BLE001 -- absence means estate-only recall.
            resolved_company = None
    try:
        from omniagentos.knowledge.capabilities import infer_domains

        domain_tags = infer_domains(goal)
    except Exception:  # noqa: BLE001 -- domain boost is advisory.
        domain_tags = ["general"]
    lessons = build_lessons_block(
        goal,
        dal=dal,
        recall_fn=recall_fn,
        playbook_path=playbook_path,
        company_id=resolved_company,
        domain_tags=domain_tags,
    )

    # O-4: select formation BEFORE the planner LLM so formation.planner can
    # affect model selection (env/config still win when set). build_plan will
    # re-select deterministically for the binding stamp; selection is pure.
    formation_planner_name: str | None = None
    try:
        from omniagentos.formation import select_formation_with_confidence

        pre_sel = select_formation_with_confidence(goal=goal, task_class=None)
        formation_planner_name = str(pre_sel.formation.planner or "").strip() or None
    except Exception:  # noqa: BLE001 -- formation is advisory for model choice.
        LOG.debug("swarm planner pre-select formation failed", exc_info=True)

    # Effort: config/env default (low for Qwen speed) wins. Complexity-derived
    # Fable effort is only a fallback when config/env are unset AND we land on
    # a Fable-path model — proxy planners ignore deep-reasoning rungs.
    effort = swarm_planner_effort()
    resolved_model = swarm_planner_model(formation_planner=formation_planner_name)
    if _is_proxy_planner_model(resolved_model):
        pass  # keep config/env effort (default low)
    elif effort == DEFAULT_SWARM_PLANNER_EFFORT:
        # Fable path with no explicit effort pin: restore complexity-derived
        # effort so a deliberate fable/opus/sol plan still reasons deeply.
        try:
            _, planner_mod, _ = _intake_planning()
            effort = planner_mod.effort_for(planner_mod.estimate_complexity(goal))
        except Exception:  # noqa: BLE001 -- planning proceeds at the effort floor.
            effort = "high"

    if planner_llm is not None:
        runner = planner_llm
    else:
        runner = make_swarm_planner_llm(formation_planner=formation_planner_name)
        assumptions = [
            *assumptions,
            f"swarm_planner: model={getattr(runner, 'model', resolved_model)} effort={effort}",
        ]
    prompt = _plan_prompt(goal, assumptions, lessons, target_width=_target_cap())

    failure = "planner model unavailable"
    for attempt in (1, 2):
        attempt_prompt = (
            prompt
            if attempt == 1
            else prompt
            + "\n\nYour previous plan was rejected: "
            + failure
            + "\nReturn a corrected plan that satisfies every rule above."
        )
        try:
            raw = runner(attempt_prompt, _SWARM_PLAN_SCHEMA, effort)
        except Exception:  # noqa: BLE001 -- the seam must never crash planning.
            LOG.warning("swarm planner LLM raised; degrading", exc_info=True)
            raw = None
        if raw is None:
            failure = "planner model unavailable or returned no structured output"
            continue
        raw_assumptions = raw.get("assumptions")
        model_assumptions = raw_assumptions if isinstance(raw_assumptions, list) else []
        plan_assumptions = assumptions + [
            str(a).strip() for a in model_assumptions if str(a).strip()
        ]
        try:
            plans = [
                build_plan(
                    bundle["goal"],
                    bundle["tasks"],
                    assumptions=[*plan_assumptions, *bundle["assumptions"]],
                    needs_install=bundle["needs_install"],
                    install_command=bundle["install_command"],
                    suite_command=bundle["suite_command"],
                    headroom=headroom,
                    category=bundle["category"],
                    workspace_dir=working_dir,
                )
                for bundle in _extract_bundles(raw, goal)
            ]
        except SwarmPlanError as exc:
            failure = str(exc)
            LOG.warning("swarm plan attempt %d invalid: %s", attempt, failure)
            continue
        # O-4: formation.planner must describe the model that actually planned,
        # not a post-hoc preference recorded after a hard-wired Fable call.
        if planner_llm is None:
            actual = str(getattr(runner, "model", resolved_model) or resolved_model)
            for plan in plans:
                if plan.formation is not None and actual:
                    plan.formation.planner = actual
        return plans

    return [_fallback_plan(goal, assumptions, failure)]


def plan_swarm(
    brief_or_spec: Any,
    working_dir: str,
    *,
    planner_llm: SwarmPlannerLLM | None = None,
    clarify_llm: ClarifyLLM | None = None,
    dal: SwarmDal | None = None,
    recall_fn: RecallFn | None = None,
    playbook_path: str | Path | None = None,
    headroom: SwarmHeadroom | None = None,
    project_id: str | None = None,
    company_id: str | None = None,
) -> SwarmPlan:
    """Plan a brief into a single validated :class:`SwarmPlan`.

    The convenience single-plan entry point. When the brief bundles several
    unrelated asks, the FIRST bundle's plan is returned with the remaining
    bundle goals recorded as assumptions — callers that route bundles
    independently (WP10 intake) use :func:`plan_swarm_bundles` instead.
    """
    plans = plan_swarm_bundles(
        brief_or_spec,
        working_dir,
        planner_llm=planner_llm,
        clarify_llm=clarify_llm,
        dal=dal,
        recall_fn=recall_fn,
        playbook_path=playbook_path,
        headroom=headroom,
        project_id=project_id,
        company_id=company_id,
    )
    primary = plans[0]
    if len(plans) > 1:
        primary.assumptions.append(
            "brief bundles additional independent asks (plan each via "
            "plan_swarm_bundles): "
            + "; ".join(plan.goal.splitlines()[0][:80] for plan in plans[1:])
        )
    return primary


def plan_swarm_decision(
    brief_or_spec: Any,
    working_dir: str,
    *,
    planner_llm: SwarmPlannerLLM | None = None,
    clarify_llm: ClarifyLLM | None = None,
    dal: SwarmDal | None = None,
    recall_fn: RecallFn | None = None,
    playbook_path: str | Path | None = None,
    headroom: SwarmHeadroom | None = None,
    allow_multi_bundle: bool = False,
    project_id: str | None = None,
    company_id: str | None = None,
) -> SwarmPlanDecision:
    """Plan all bundles and return their typed, fail-closed safety decision."""
    plans = plan_swarm_bundles(
        brief_or_spec,
        working_dir,
        planner_llm=planner_llm,
        clarify_llm=clarify_llm,
        dal=dal,
        recall_fn=recall_fn,
        playbook_path=playbook_path,
        headroom=headroom,
        project_id=project_id,
        company_id=company_id,
    )
    return evaluate_plan_safety(
        plans=plans,
        workspace_dir=working_dir,
        allow_multi_bundle=allow_multi_bundle,
    )


# ---------------------------------------------------------------------------
# Provisioning
# ---------------------------------------------------------------------------


CATEGORY_LABEL_MAX_CHARS = 60
"""Planner category labels are trimmed to this length before resolution."""

MAX_NEW_CATEGORIES_PER_RUN = 8
"""Hard cap on task_categories rows one provisioning run may create.

A runaway planner that invents a label per task must not flood the shared
taxonomy; excess labels degrade to uncategorized cards (logged)."""


def _category_resolver(db_path: str) -> Callable[[str | None], str | None]:
    """Memoized category-label → ``task_categories.id`` resolver (FB4+ taxonomy).

    Create-or-get via ``LonghaulStore.create_category`` (slug-deduped; the
    default ``wip_limit=1`` is harmless metadata for swarm cards because
    longhaul WIP counting is lane-scoped). Failures degrade to ``None``
    (uncategorized card) — taxonomy must never block provisioning. A
    ``:memory:`` DAL cannot share a connectionless store, so it resolves
    nothing.

    Hygiene (planner output is untrusted): labels are trimmed to
    ``CATEGORY_LABEL_MAX_CHARS``, labels whose slug is empty are skipped,
    and at most ``MAX_NEW_CATEGORIES_PER_RUN`` NEW categories may be created
    per run — excess labels resolve to ``None`` (uncategorized, logged).
    Pre-existing categories always resolve and never count against the cap.
    """
    cache: dict[str, str | None] = {}
    created_count = 0

    def resolve(name: str | None) -> str | None:
        nonlocal created_count
        label = (name or "").strip()[:CATEGORY_LABEL_MAX_CHARS].strip()
        if not label or db_path == ":memory:":
            return None
        if label not in cache:
            try:
                from omniagentos.longhaul.store import LonghaulStore

                slug = LonghaulStore._slugify(label)
                if not slug:
                    LOG.warning(
                        "swarm card category %r slugifies to nothing; provisioning uncategorized",
                        label,
                    )
                    cache[label] = None
                    return None
                store = LonghaulStore(db_path)
                try:
                    existing = store.get_category(slug)
                    if existing is not None:
                        cache[label] = str(existing["id"])
                    elif created_count >= MAX_NEW_CATEGORIES_PER_RUN:
                        LOG.warning(
                            "swarm run category cap (%d new) reached; label %r "
                            "provisioned uncategorized",
                            MAX_NEW_CATEGORIES_PER_RUN,
                            label,
                        )
                        cache[label] = None
                    else:
                        cache[label] = str(store.create_category(label)["id"])
                        created_count += 1
                finally:
                    store.close()
            except Exception:  # noqa: BLE001 -- category is metadata; never block provisioning.
                LOG.warning(
                    "could not resolve swarm card category %r; provisioning uncategorized",
                    label,
                    exc_info=True,
                )
                cache[label] = None
        return cache[label]

    return resolve


def provision_run(
    plan: SwarmPlan,
    *,
    dal: SwarmDal,
    working_dir: str,
    project_id: str | None = None,
    budget_usd_max: float | None = None,
    # The run row's UPPER bound on coordinator width (scheduler._run_cap clamps
    # target_n to min(this, MAX_SLOTS)). The intake dispatch path does not pass
    # it, so this default WAS the real per-run ceiling for every production
    # swarm — a hardcoded 10 that would have silently negated the raised
    # MAX_SLOTS. Kept equal to the hard ceiling; the coordinator's resize
    # (demand / fleet fair-share) is what actually decides the live width.
    max_concurrency: int = TARGET_N_HARD_CEILING,
    priority: str = "normal",
    write_plan_doc: bool = True,
    status: str = "planning",
    category: str | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Materialize a plan: run row + root card + child cards + deps, atomically.

    One ``SwarmDal.provision_run`` transaction (status ``planning`` by default,
    full plan_json) — a crash mid-provision leaves nothing. ``status`` is WP10's
    fleet-admission seam: intake passes ``"queued"`` when the fleet is at
    ``max_concurrent_swarms`` so an over-cap run parks (no coordinator, started
    oldest-first as capacity frees) instead of blocking dispatch. Every child
    card's ``swarm_json`` carries the plan version + sha256 hash so a worker
    brief can be verified against the plan doc it was cut from. ``lane`` stays
    NULL — swarm membership is ``swarm_run_id`` only.

    Project axis (M1): ``project_id`` is written to the run row AND to every
    board card in the same transaction (``SwarmDal.provision_run``), so the
    cards a run creates are scoped to the project the run was created for.
    ``None`` (the default) provisions an unscoped run — no default project is
    invented for a caller that named none.

    Board taxonomy (FB4+): each task's optional ``category`` label — falling
    back to the root-goal category (the ``category`` kwarg, then
    ``plan.category``) — is resolved to a ``task_categories`` row via
    :class:`~omniagentos.longhaul.store.LonghaulStore` (slug-deduped
    create-or-get) and stamped on the card as ``category_id``. Category is
    cross-lane metadata: longhaul WIP counting is lane-scoped, so these
    stamps are inert to the longhaul engine, and resolution failures degrade
    to uncategorized cards (never block provisioning). Returns ``{"run": row,
    "root_card_id": ..., "card_ids": {task_key: card_id}, "plan_hash": ...}``.

    ``params`` (D10 Mode dial): execution-mode metadata — ``priority``/``pins``
    /``speed`` from intake — recorded ADDITIVELY as ``plan_json["params"]``.
    Old rows without the key keep current behavior everywhere;
    ``SwarmPlan.model_validate`` ignores the extra key exactly as it does
    ``plan_hash``, and the fingerprint is computed over the plan alone, so the
    brief-hash verification chain is untouched. A valid ``params["speed"]``
    (fast|auto|ultra) is additionally stamped into the root card's and every
    child card's ``swarm_json`` so the router's speed tier floor can read it
    per task without a run-row join. (A mid-run task split re-derives child
    swarm_json in the scheduler and may drop the stamp — the floor then simply
    does not apply to those subtasks; degradation, never breakage.)
    """
    assert_plan_safe_for_provision(plan, workspace_dir=working_dir)
    fingerprint = plan_fingerprint(plan)
    payload = plan_payload(plan)
    if params:
        payload["params"] = dict(params)
    run_speed = str((params or {}).get("speed") or "").strip().lower()
    if run_speed not in ("fast", "auto", "ultra"):
        run_speed = ""
    goal_line = next((ln.strip() for ln in plan.goal.splitlines() if ln.strip()), plan.goal.strip())

    root_category = (category or "").strip() or plan.category
    resolve_category = _category_resolver(dal.db_path)

    formation_root_fields = formation_swarm_json_fields(plan.formation, role="summary")
    root_card_id = new_id("btk")
    root_card = {
        "id": root_card_id,
        "title": f"Swarm: {goal_line[:100]}" if goal_line else "Swarm run",
        "description": plan.goal.strip(),
        "status": "in_progress",  # never eligible: the run itself, not a work unit
        "priority": priority,
        "swarm_json": {
            "root": True,
            "plan_version": plan.version,
            "plan_hash": fingerprint,
            "mode": plan.mode,
            **({"speed": run_speed} if run_speed else {}),
            **formation_root_fields,
        },
        "category_id": resolve_category(root_category),
    }

    card_ids: dict[str, str] = {task.id: new_id("btk") for task in plan.tasks}
    cards = []
    for task in plan.tasks:
        description = task.description.strip()
        if task.acceptance:
            description = f"{description}\n\nAcceptance: {task.acceptance}".strip()

        child_swarm_json = {
            "task_key": task.id,
            "plan_version": plan.version,
            "plan_hash": fingerprint,
            "complexity": task.complexity,
            "risk_class": task.risk_class,
            "est_manual_minutes": task.est_manual_minutes,
            "est_agent_minutes": task.est_agent_minutes,
            "owned_paths": task.owned_paths,
            "tier_hint": task.tier_hint,
            "acceptance": task.acceptance,
            "verify_command": task.verify_command,
            "integration": task.id == plan.integration_task_id,
            "bootstrap": task.id == BOOTSTRAP_TASK_ID,
            **({"speed": run_speed} if run_speed else {}),
        }
        role = job_role_from_swarm_json(child_swarm_json)
        child_swarm_json["job_role"] = str(role)
        formation_fields = formation_swarm_json_fields(plan.formation, role=str(role))
        child_swarm_json.update(formation_fields)

        cards.append(
            {
                "id": card_ids[task.id],
                "title": task.title or task.id,
                "description": description,
                "status": "open",
                "priority": priority,
                "swarm_json": child_swarm_json,
                "category_id": resolve_category(task.category or root_category),
            }
        )

    edges = [
        (card_ids[task.id], card_ids[dep])
        for task in plan.tasks
        for dep in task.depends_on
        if dep in card_ids
    ]

    run_row = dal.provision_run(
        run={
            "board_task_id": root_card_id,
            "project_id": project_id,
            "working_dir": working_dir,
            "goal": plan.goal,
            "status": status,
            "plan": payload,
            "target_concurrency": plan.target_n,
            "max_concurrency": max_concurrency,
            "budget_usd_max": budget_usd_max,
        },
        root_card=root_card,
        cards=cards,
        edges=edges,
    )

    if write_plan_doc:
        try:
            write_plan_md(plan, working_dir)
        except Exception:  # noqa: BLE001 -- PLAN.md is a derived projection; DB truth already landed.
            LOG.warning("could not write PLAN.md for run %s", run_row["id"], exc_info=True)

    # M-13: Graph Runtime V2 — observable gated default-path soak.
    # Unset OMNIAGENTOS_GRAPH_RUNTIME → shadow soak for multi-task plans.
    # Explicit off/0 disables. Explicit on/1/true enables live link.
    # Never claims the graph is the completed default executor.
    graph_run_id: str | None = None
    graph_soak: dict[str, Any] | None = None
    try:
        from omniagentos.swarm.graph_soak import maybe_link_graph_for_swarm

        soak = maybe_link_graph_for_swarm(
            db_path=getattr(dal, "db_path", None),
            swarm_run_id=str(run_row["id"]),
            project_id=project_id,
            target_n=int(plan.target_n or 1),
            title=f"swarm:{run_row['id']}",
        )
        graph_soak = soak.to_dict()
        graph_run_id = soak.graph_run_id
    except Exception:  # noqa: BLE001 — graph is additive; never block provision
        LOG.warning("graph_runtime soak provision hook failed", exc_info=True)
        graph_soak = {
            "mode": "error",
            "enabled": False,
            "graph_run_id": None,
            "status": "error",
            "claim": "none",
            "reason": "hook_exception",
        }

    # Phase B6: production formation_selections telemetry (predicted arm at provision).
    if plan.formation is not None:
        try:
            from omniagentos.formation.telemetry import record_selection

            conn = getattr(dal, "_connection", None)
            if conn is not None:
                form = plan.formation
                record_selection(
                    conn,
                    task_id=str(run_row.get("id") or root_card_id),
                    goal=str(plan.goal or "")[:2000],
                    arm="formation",
                    formation_id=form.id,
                    confidence=form.confidence,
                    low_confidence=bool(form.low_confidence),
                    topology=form.topology,
                    implementers=list(form.implementers or []),
                    reviewer=form.reviewer,
                    planner=form.planner,
                    mechanical_gate=bool(form.mechanical_gate),
                    models={
                        "implementers": list(form.implementers or []),
                        "reviewer": form.reviewer,
                        "planner": form.planner,
                    },
                    outcome="predicted",
                    source="production",
                    task_fingerprint=fingerprint,
                )
        except Exception:  # noqa: BLE001 — telemetry never blocks provision
            LOG.warning("formation_selections production write failed", exc_info=True)

    result: dict[str, Any] = {
        "run": run_row,
        "root_card_id": root_card_id,
        "card_ids": card_ids,
        "plan_hash": fingerprint,
    }
    if graph_run_id:
        result["graph_run_id"] = graph_run_id
    if graph_soak is not None:
        result["graph_soak"] = graph_soak
    return result


# ---------------------------------------------------------------------------
# PLAN.md — derived projection (pure render + atomic write)
# ---------------------------------------------------------------------------


def render_plan_md(plan: SwarmPlan, statuses: Mapping[str, str] | None = None) -> str:
    """Render the PLAN.md projection: Goal → Task DAG → Interface Contracts →
    File Ownership Map → status. Pure — the coordinator re-calls this on every
    completion/split/resize and writes via :func:`write_plan_md`."""
    statuses = statuses or {}
    fingerprint = plan_fingerprint(plan)
    goal_line = next((ln.strip() for ln in plan.goal.splitlines() if ln.strip()), "Swarm run")

    def status_of(task_id: str) -> str:
        return statuses.get(task_id, "open")

    lines = [
        f"# Swarm Plan — {goal_line}",
        "",
        f"> Derived from `swarm_runs.plan_json` v{plan.version} "
        f"(hash `{fingerprint[:12]}`). Machine truth lives in the database; the "
        "coordinator regenerates this file. Workers: verify the hash in your "
        "brief matches, and never edit this file.",
        "",
        "## Goal",
        "",
        plan.goal.strip() or "(none)",
        "",
    ]
    if plan.assumptions:
        lines += ["## Assumptions", ""]
        lines += [f"- {item}" for item in plan.assumptions]
        lines.append("")

    lines += [
        "## Task DAG",
        "",
        "| Task | Title | Depends on | Complexity | Risk | Est (agent min) | Status |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for task in plan.tasks:
        deps = ", ".join(task.depends_on) if task.depends_on else "—"
        lines.append(
            f"| {task.id} | {task.title or task.id} | {deps} | {task.complexity} "
            f"| {task.risk_class} | {task.est_agent_minutes} | {status_of(task.id)} |"
        )
    lines.append("")

    lines += ["## Interface Contracts", ""]
    for task in plan.tasks:
        lines.append(f"### {task.id}")
        lines.append(f"- Acceptance: {task.acceptance or '(none recorded)'}")
        lines.append(
            f"- Verify: `{task.verify_command}`" if task.verify_command else "- Verify: (none)"
        )
        lines.append("")

    lines += [
        "## File Ownership Map",
        "",
        "| Task | Owned paths |",
        "| --- | --- |",
    ]
    for task in plan.tasks:
        owned = ", ".join(f"`{p}`" for p in task.owned_paths) if task.owned_paths else "—"
        lines.append(f"| {task.id} | {owned} |")
    lines.append("")

    done = sum(1 for task in plan.tasks if status_of(task.id) == "done")
    lines += [
        "## Status",
        "",
        f"- Mode: {plan.mode}",
        f"- Parallelism ratio: {plan.parallelism_ratio}",
        f"- Target concurrency: {plan.target_n}",
        f"- Tasks done: {done}/{len(plan.tasks)}",
        "",
    ]
    return "\n".join(lines)


def write_plan_md(
    plan: SwarmPlan,
    working_dir: str | Path,
    statuses: Mapping[str, str] | None = None,
) -> Path:
    """Write PLAN.md to the working dir via tmp + fsync + rename.

    Workers may be mid-read in the shared working directory (Phase 1
    same-directory model), so the file must never be observable half-written.
    """
    target = Path(working_dir) / PLAN_MD_FILENAME
    target.parent.mkdir(parents=True, exist_ok=True)
    content = render_plan_md(plan, statuses)
    tmp = target.with_name(f".{PLAN_MD_FILENAME}.tmp-{os.getpid()}")
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, target)
    return target


__all__ = [
    "BOOTSTRAP_TASK_ID",
    "DEFAULT_LOW_HEADROOM_RATIO",
    "DEFAULT_LOW_HEADROOM_SLOTS",
    "DEFAULT_SWARM_PLANNER_EFFORT",
    "DEFAULT_SWARM_PLANNER_MODEL",
    "INTEGRATION_TASK_ID",
    "LESSONS_TOKEN_CAP",
    "LOW_HEADROOM_PRESSURE",
    "MAX_TASKS",
    "PLAN_MD_FILENAME",
    "SOLO_MAX_TASKS",
    "SOLO_RATIO_THRESHOLD",
    "SWARM_PLANNER_EFFORT_ENV",
    "SWARM_PLANNER_EFFORTS",
    "SWARM_PLANNER_FABLE_ALIASES",
    "SWARM_PLANNER_FORMATION_SENTINEL",
    "SWARM_PLANNER_MODEL_ENV",
    "SWARM_PLANNER_MODELS",
    "SWARM_PLANNER_PROXY_MODELS",
    "SwarmHeadroom",
    "SwarmPlanError",
    "SwarmPlannerLLM",
    "TARGET_N_HARD_CEILING",
    "add_ownership_overlap_edges",
    "build_lessons_block",
    "build_plan",
    "critical_path_minutes",
    "default_recall_capabilities",
    "default_recall_lessons",
    "default_swarm_planner_llm",
    "formation_swarm_json_fields",
    "is_shared_path",
    "is_solo",
    "make_swarm_planner_llm",
    "normalize_owned_path",
    "parallelism_stats",
    "parse_swarm_planner_effort",
    "parse_swarm_planner_model",
    "paths_overlap",
    "plan_fingerprint",
    "plan_payload",
    "plan_swarm",
    "plan_swarm_bundles",
    "plan_swarm_decision",
    "provision_run",
    "render_plan_md",
    "swarm_headroom",
    "swarm_planner_effort",
    "swarm_planner_model",
    "topo_sort_with_repair",
    "write_plan_md",
]
