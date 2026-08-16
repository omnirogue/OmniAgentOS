"""Stage 2 — spawn the executor with injected context.

Two pieces:

* :func:`build_executor_prompt` ASSEMBLES the executor's prompt from the spec + the
  project's memory/context (reusing ``memory.assemble_context``) + the scoped file
  list + the matched skills (reusing ``skills.search`` / ``skills.get_skill``).
* the default :class:`ExecutorRunner` implementations spawn every tier as a
  MONITORED session (``SessionSupervisor.spawn``) so it shows in the dashboard,
  participates in hook approvals, and has a durable terminal result. ``CHEAP``
  keeps the inexpensive model choice while ``FUSION`` / ``FUSION_ULTRA`` use the
  Fable-led path.

Every collaborator is a seam so a test drives the whole stage without a live process.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any, Protocol, cast

from omniagentos.contracts import HarnessType
from omniagentos.orchestrator.contracts import (
    ApprovalGatewayProto,
    ExecutorRequest,
    ExecutorResult,
    ExecutorTier,
    FileLister,
    SkillGet,
    SkillSearch,
)

if TYPE_CHECKING:
    from omniagentos.intake.planner import PlannedTask
    from omniagentos.memory.contracts import ConversationReader

LOG = logging.getLogger(__name__)

# Token budget for the injected memory/context block.
_MEMORY_BUDGET_TOKENS = 1200
# How many matched skills to inject, and how many scoped files to list.
_MAX_SKILLS = 4
_MAX_FILES = 40
_DEFAULT_CHEAP_MODEL = "sonnet"
_DEFAULT_SESSION_TIMEOUT_SECONDS = 30 * 60.0
_DEFAULT_SESSION_POLL_INTERVAL_SECONDS = 0.5


# ---------------------------------------------------------------------------
# Context injection
# ---------------------------------------------------------------------------


def _memory_block(reader: ConversationReader | None, scope_type: str, scope_id: str | None) -> str:
    if reader is None or not scope_id:
        return ""
    try:
        from omniagentos.memory.assemble import assemble_context

        context = assemble_context(scope_type, scope_id, _MEMORY_BUDGET_TOKENS, reader=reader)
        return context.block or ""
    except Exception:  # noqa: BLE001 -- context is best-effort; never fail the spawn.
        LOG.debug("orchestrator memory assembly failed", exc_info=True)
        return ""


def _matched_skills(
    task: PlannedTask,
    search: SkillSearch | None,
    get: SkillGet | None,
) -> list[str]:
    if search is None:
        return []
    query = " ".join(p for p in (task.title, task.suggested_discipline or "") if p).strip()
    if not query:
        return []
    try:
        hits = search(query, limit=_MAX_SKILLS)
    except Exception:  # noqa: BLE001
        LOG.debug("orchestrator skill search failed", exc_info=True)
        return []
    rendered: list[str] = []
    for hit in hits[:_MAX_SKILLS]:
        title = str(hit.get("title") or hit.get("slug") or hit.get("id") or "").strip()
        summary = str(hit.get("summary") or "").strip()
        if get is not None and hit.get("id"):
            try:
                full = get(str(hit["id"]))
                summary = (
                    str(full.get("summary") or summary).strip()
                    or str(full.get("preferred_method") or "").strip()
                )
            except Exception:  # noqa: BLE001
                LOG.debug("orchestrator skill get failed", exc_info=True)
        line = f"- {title}" if title else "- (skill)"
        if summary:
            line += f": {summary}"
        rendered.append(line)
    return rendered


def _scoped_files(lister: FileLister | None, working_dir: str) -> Sequence[str]:
    if lister is not None:
        try:
            return list(lister(working_dir))[:_MAX_FILES]
        except Exception:  # noqa: BLE001
            LOG.debug("orchestrator file lister failed", exc_info=True)
            return []
    return default_file_lister(working_dir)


def default_file_lister(working_dir: str) -> list[str]:
    """List up to ``_MAX_FILES`` project-relative files, skipping hidden/vcs dirs."""
    root = working_dir
    out: list[str] = []
    if not working_dir or not os.path.isdir(root):
        return out
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d != "__pycache__"]
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            rel = os.path.relpath(os.path.join(dirpath, name), root)
            out.append(rel)
            if len(out) >= _MAX_FILES:
                return out
    return out


def build_executor_prompt(
    task: PlannedTask,
    spec_markdown: str,
    working_dir: str,
    *,
    context_reader: ConversationReader | None = None,
    context_scope_type: str = "task",
    context_scope_id: str | None = None,
    skill_search: SkillSearch | None = None,
    skill_get: SkillGet | None = None,
    file_lister: FileLister | None = None,
    review_feedback: str | None = None,
    granted_roots: list[str] | None = None,
) -> str:
    """Assemble the executor prompt: spec + memory + scoped files + matched skills.

    ``review_feedback`` is appended on a corrective iteration so the executor sees
    exactly what the reviewer denied. ``granted_roots`` (P3) NAMES the project's
    in-scope directories beyond ``working_dir`` so the executor knows every path it
    may touch (machine mounts -- iCloud, Google Drive, Dropbox, ... -- exist in the
    mounts registry but are in scope only where a project granted a dir under one).
    """
    sections: list[str] = [
        "You are the EXECUTOR for an OmniAgentOS orchestration. Deliver the task below",
        "against the spec and its acceptance criteria. Work only inside your granted scope.",
        "",
        "## Task",
        task.title,
    ]
    if task.description:
        sections += ["", task.description]
    if task.acceptance_criteria:
        sections += ["", "## Acceptance criteria"]
        sections += [f"- {c}" for c in task.acceptance_criteria]
    if task.commits_expected:
        sections += ["", "Commit the completed work and report the commit hash."]
    if task.validation_command:
        sections += [
            "",
            "## Required validation",
            task.validation_command,
            "Run this command and report its result.",
        ]

    if granted_roots:
        sections += [
            "",
            "## Granted scope (you may read/write here, using absolute paths)",
            f"- {working_dir}  (primary working dir)",
            *[f"- {root}" for root in granted_roots],
            "Machine mounts (iCloud, Google Drive, Dropbox, ~/Work, ...) are registered",
            "in configs/mounts.yaml but are OUT of scope unless listed above.",
        ]

    sections += ["", "## Spec", spec_markdown.strip()]

    memory = _memory_block(context_reader, context_scope_type, context_scope_id)
    if memory:
        sections += ["", "## Project context (memory)", memory]

    # Ambient capability recall uses the task as its query and the project as its
    # company boundary. This is additive to conversation memory and always fail-open.
    try:
        from omniagentos.knowledge.capabilities import (
            infer_domains,
            resolve_company_id,
            safe_ambient_capability_block,
        )
        from omniagentos.knowledge.config import knowledge_enabled

        if knowledge_enabled():
            scope_store = getattr(context_reader, "_store", context_reader)
            company_id = resolve_company_id(scope_store, context_scope_id)
            capability_query = "\n".join(part for part in (task.title, task.description) if part)
            capabilities = safe_ambient_capability_block(
                capability_query,
                company_id=company_id,
                paths=task.owned_paths if hasattr(task, "owned_paths") else (),
                domains=infer_domains(capability_query),
            )
            if capabilities:
                sections += ["", "## Ambient capabilities", capabilities]
    except Exception:  # noqa: BLE001 -- capability recall never blocks spawn.
        LOG.debug("orchestrator capability recall failed", exc_info=True)

    files = _scoped_files(file_lister, working_dir)
    if files:
        sections += ["", "## Scoped files", *[f"- {f}" for f in files]]

    skills = _matched_skills(task, skill_search, skill_get)
    if skills:
        sections += ["", "## Relevant skills", *skills]

    if review_feedback and review_feedback.strip():
        sections += [
            "",
            "## Reviewer feedback (address this — a prior attempt was DENIED)",
            review_feedback.strip(),
        ]
    return "\n".join(sections)


# ---------------------------------------------------------------------------
# Executor spawn — tiered runners
# ---------------------------------------------------------------------------


class SessionSpawner(Protocol):
    """The slice of ``SessionSupervisor.spawn`` a monitored executor needs.

    Matches ``omniagentos.sessions.supervisor.SessionSupervisor.spawn`` exactly (and the
    intake dispatch path's ``SessionSpawner`` Protocol), so the real supervisor satisfies
    it structurally and the spawn signature stays CONSISTENT across both call sites. The
    monitored executor only passes ``project_dir``/``model``/``prompt``/``budget``/
    ``title``/``granted_roots``; the remaining parameters keep their supervisor defaults.

    ``granted_roots`` (P3 / FIX 6) is threaded so an orchestrate-mode executor session is
    frozen with the project's full validated scope, exactly as an intake session is.
    """

    def spawn(
        self,
        project_dir: str,
        model: str,
        prompt: str,
        budget_usd_max: float | None = None,
        title: str | None = None,
        extra_write_roots: list[str] | None = None,
        title_prefix: str | None = None,
        resume_session_ref: str | None = None,
        orchestrator_owned: bool = False,
        orchestrator_run_id: str | None = None,
        granted_roots: list[str] | None = None,
    ) -> str: ...


class SessionStatusStore(Protocol):
    """Durable ownership + point-lookup slice used while monitoring a session."""

    def mark_orchestrator_session(self, session_id: str, run_id: str | None = None) -> None: ...

    def get_session(self, session_id: str) -> dict[str, Any] | None: ...


class AdapterRunProto(Protocol):
    """Compatibility type for the retired, untracked CHEAP adapter injection."""

    def run(self, input: Any) -> Any: ...


class _MonitoredSessionRunner:
    """Shared spawn/ownership/terminal-wait discipline for every executor tier."""

    def __init__(
        self,
        *,
        default_model: str,
        spawner: SessionSpawner | None = None,
        session_store: SessionStatusStore | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        timeout_seconds: float = _DEFAULT_SESSION_TIMEOUT_SECONDS,
        poll_interval_seconds: float = _DEFAULT_SESSION_POLL_INTERVAL_SECONDS,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        self._default_model = default_model
        self._spawner = spawner
        self._session_store = session_store
        self._monotonic = monotonic
        self._sleep = sleep
        self._timeout_seconds = timeout_seconds
        self._poll_interval_seconds = poll_interval_seconds
        self._owned_spawner: SessionSpawner | None = None
        self._owned_spawner_lock = threading.Lock()

    def _resolve_spawner(self) -> SessionSpawner:
        if self._spawner is not None:
            return self._spawner

        with self._owned_spawner_lock:
            if self._owned_spawner is None:
                from omniagentos.sessions.supervisor import SessionSupervisor

                self._owned_spawner = SessionSupervisor()
            return self._owned_spawner

    def close(
        self,
        *,
        terminate_children: bool = False,
        join_timeout: float | None = None,
    ) -> None:
        """Dispose only the supervisor this runner constructed itself.

        This strict API deliberately preserves ``SessionSupervisor.close`` errors
        for explicit callers.  A failed close keeps the owned supervisor attached
        so the ordered close remains retryable.
        """
        with self._owned_spawner_lock:
            spawner = self._owned_spawner
            if spawner is None:
                return
            from omniagentos.sessions.lifecycle import DEFAULT_JOIN_TIMEOUT_S

            close = getattr(spawner, "close", None)
            if callable(close):
                close(
                    terminate_children=terminate_children,
                    join_timeout=(
                        DEFAULT_JOIN_TIMEOUT_S if join_timeout is None else max(0.0, join_timeout)
                    ),
                )
            self._owned_spawner = None

    def _dispose_owned_spawner(self, *, terminate_children: bool) -> None:
        """Best-effort cleanup for paths that have an executor result to return."""
        try:
            self.close(terminate_children=terminate_children)
        except Exception:  # noqa: BLE001 -- disposal must never replace the executor result.
            LOG.exception(
                "could not close runner-owned session supervisor; preserving executor result"
            )

    def _resolve_store(self, spawner: SessionSpawner) -> tuple[SessionStatusStore, bool]:
        if self._session_store is not None:
            return self._session_store, False

        # A real SessionSupervisor exposes its DAL. Reusing it gives the ownership
        # marker and lifecycle lookup one serialized connection and is also the
        # cleanest injectable seam for integration tests.
        candidate = getattr(spawner, "dal", None)
        if callable(getattr(candidate, "mark_orchestrator_session", None)) and callable(
            getattr(candidate, "get_session", None)
        ):
            return cast(SessionStatusStore, candidate), False

        from omniagentos.contracts import default_db_path
        from omniagentos.sessions.dal import SessionsDal

        return SessionsDal(default_db_path()), True

    def _await_terminal(self, store: SessionStatusStore, session_id: str) -> ExecutorResult:
        from omniagentos.sessions.dal import TERMINAL_SESSION_STATES, SessionState

        deadline = self._monotonic() + self._timeout_seconds
        while True:
            try:
                row = store.get_session(session_id)
            except Exception as exc:  # noqa: BLE001 -- a DAL fault is an executor failure.
                return ExecutorResult(
                    status="error",
                    session_id=session_id,
                    error=f"could not read session {session_id}: {type(exc).__name__}: {exc}",
                )
            if row is None:
                return ExecutorResult(
                    status="error",
                    session_id=session_id,
                    error=f"spawned session {session_id} is missing from the session store",
                )
            try:
                state = SessionState(str(row.get("state") or ""))
            except ValueError:
                return ExecutorResult(
                    status="error",
                    session_id=session_id,
                    error=f"session {session_id} has invalid state {row.get('state')!r}",
                )
            if state in TERMINAL_SESSION_STATES:
                output_text = str(row.get("output_text") or "")
                if state is SessionState.COMPLETED:
                    return ExecutorResult(
                        status="ok",
                        session_id=session_id,
                        output_text=output_text,
                    )
                detail = str(row.get("error") or "").strip()
                suffix = f": {detail}" if detail else ""
                return ExecutorResult(
                    status="error",
                    session_id=session_id,
                    output_text=output_text,
                    error=f"session {session_id} ended in {state.value}{suffix}",
                )

            now = self._monotonic()
            if now >= deadline:
                # A2 zombie fix: a timed-out await used to simply walk away,
                # leaving the live session running unsupervised for hours. Request
                # a durable kill so the supervisor reaps it. Guarded: injected
                # stores may not implement request_kill (SessionStatusStore only
                # promises mark/get); a missing method or a kill failure never
                # changes the timeout result, it is only logged.
                kill_note = ""
                request_kill = getattr(store, "request_kill", None)
                if callable(request_kill):
                    try:
                        request_kill(session_id)
                        kill_note = "; kill requested"
                    except Exception:  # noqa: BLE001 -- best-effort cleanup
                        LOG.exception(
                            "could not request kill for timed-out session %s",
                            session_id,
                        )
                else:
                    LOG.warning(
                        "session store %s cannot request_kill; timed-out session "
                        "%s may keep running until the idle reaper acts",
                        type(store).__name__,
                        session_id,
                    )
                return ExecutorResult(
                    status="error",
                    session_id=session_id,
                    error=(
                        f"session {session_id} timed out after "
                        f"{self._timeout_seconds:g}s in state {state.value}{kill_note}"
                    ),
                )
            self._sleep(min(self._poll_interval_seconds, deadline - now))

    def run(self, request: ExecutorRequest, gateway: ApprovalGatewayProto) -> ExecutorResult:
        # Approval decisions for bridge sessions are made by hook-eval using the
        # durable orchestrator marker. The in-process gateway remains in the runner
        # contract for injected/mock executors.
        del gateway
        try:
            spawner = self._resolve_spawner()
            store, owns_store = self._resolve_store(spawner)
        except Exception as exc:  # noqa: BLE001
            self._dispose_owned_spawner(terminate_children=True)
            return ExecutorResult(
                status="error",
                error=f"could not initialize session execution: {type(exc).__name__}: {exc}",
            )
        model = request.model or self._default_model
        try:
            try:
                session_id = spawner.spawn(
                    project_dir=request.working_dir,
                    model=model,
                    prompt=request.prompt,
                    budget_usd_max=request.budget_usd_max,
                    title=request.title,
                    # P3 (FIX 6): freeze the project's full granted scope onto the
                    # session row so an orchestrate-mode executor honors the SAME
                    # secondary root_dirs/allowed_dirs an intake session gets --
                    # resolved server-side upstream (never a hook/tool payload).
                    granted_roots=request.granted_roots or None,
                )
            except Exception as exc:  # noqa: BLE001
                spawned_id = getattr(exc, "session_id", None)
                return ExecutorResult(
                    status="error",
                    session_id=str(spawned_id) if spawned_id else None,
                    error=f"{type(exc).__name__}: {exc}",
                )

            marker_error: str | None = None
            try:
                # This is required, not best-effort: without the marker hook-eval
                # cannot auto-approve safe work and the session will park forever.
                store.mark_orchestrator_session(session_id, request.run_id)
            except Exception as exc:  # noqa: BLE001
                marker_error = (
                    f"could not mark session {session_id} orchestrator-owned: "
                    f"{type(exc).__name__}: {exc}"
                )
            if marker_error is None and request.on_spawn is not None:
                try:
                    request.on_spawn(session_id)
                except Exception:  # noqa: BLE001 -- reporting cannot fail a spawn.
                    LOG.debug("orchestrator spawn callback failed", exc_info=True)
            terminal_result = self._await_terminal(store, session_id)
            if marker_error is not None:
                monitor_detail = f"; {terminal_result.error}" if terminal_result.error else ""
                return ExecutorResult(
                    status="error",
                    session_id=session_id,
                    output_text=terminal_result.output_text,
                    error=f"{marker_error}{monitor_detail}",
                )
            return terminal_result
        finally:
            if owns_store:
                close = getattr(store, "close", None)
                if callable(close):
                    close()
            self._dispose_owned_spawner(terminate_children=True)

    def attach(self, session_id: str) -> ExecutorResult:
        """Wait for an existing executor session without spawning a replacement."""
        try:
            spawner = self._resolve_spawner()
            store, owns_store = self._resolve_store(spawner)
        except Exception as exc:  # noqa: BLE001
            self._dispose_owned_spawner(terminate_children=False)
            return ExecutorResult(
                status="error",
                session_id=session_id,
                error=f"could not initialize session execution: {type(exc).__name__}: {exc}",
            )
        try:
            return self._await_terminal(store, session_id)
        finally:
            if owns_store:
                close = getattr(store, "close", None)
                if callable(close):
                    close()


class CheapAdapterRunner(_MonitoredSessionRunner):
    """``CHEAP`` tier — a monitored bridge session pinned to the cheap model.

    ``harness`` and ``adapter`` remain accepted for source compatibility, but the
    unsafe untracked adapter lane is deliberately retired: every CHEAP execution is
    now a real orchestrator-owned session with hook approvals and terminal state.
    """

    def __init__(
        self,
        *,
        harness: HarnessType = HarnessType.CLI_CLAUDE,
        adapter: AdapterRunProto | None = None,
        spawner: SessionSpawner | None = None,
        session_store: SessionStatusStore | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        timeout_seconds: float = _DEFAULT_SESSION_TIMEOUT_SECONDS,
        poll_interval_seconds: float = _DEFAULT_SESSION_POLL_INTERVAL_SECONDS,
    ) -> None:
        del harness, adapter
        super().__init__(
            default_model=_DEFAULT_CHEAP_MODEL,
            spawner=spawner,
            session_store=session_store,
            monotonic=monotonic,
            sleep=sleep,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )


class FusionSessionRunner(_MonitoredSessionRunner):
    """``FUSION`` / ``FUSION_ULTRA`` tier — the Fable-led FUSION path as a MONITORED
    session (``SessionSupervisor.spawn``), so the executor shows in the dashboard.

    The fusion lane is carried via ``OMNIAGENTOS_FUSION_ENTRYPOINT`` on the spawned
    session's environment expectation (``/fusion`` vs ``/ultrabuild``); the model
    defaults to the Fable model so the lead stays Fable-led and cheap coders fan out
    underneath. The live Session Bridge owns the PreToolUse approval interception; the
    orchestrator's gateway is the SAME approve-safe/escalate policy that decision
    consults (see :func:`omniagentos.orchestrator.approvals.resolve_approval`).
    """

    def __init__(
        self,
        spawner: SessionSpawner | None = None,
        *,
        session_store: SessionStatusStore | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        timeout_seconds: float = _DEFAULT_SESSION_TIMEOUT_SECONDS,
        poll_interval_seconds: float = _DEFAULT_SESSION_POLL_INTERVAL_SECONDS,
    ) -> None:
        from omniagentos.intake.fable import FABLE_MODEL

        super().__init__(
            default_model=FABLE_MODEL,
            spawner=spawner,
            session_store=session_store,
            monotonic=monotonic,
            sleep=sleep,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )


def runner_for_tier(
    tier: ExecutorTier,
    *,
    spawner: SessionSpawner | None = None,
    cheap_adapter: AdapterRunProto | None = None,
    cheap_harness: HarnessType = HarnessType.CLI_CLAUDE,
    session_store: SessionStatusStore | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    timeout_seconds: float = _DEFAULT_SESSION_TIMEOUT_SECONDS,
    poll_interval_seconds: float = _DEFAULT_SESSION_POLL_INTERVAL_SECONDS,
) -> Any:
    """Select a monitored-session runner while preserving the resolved tier."""
    if tier.is_fusion:
        return FusionSessionRunner(
            spawner=spawner,
            session_store=session_store,
            monotonic=monotonic,
            sleep=sleep,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )
    return CheapAdapterRunner(
        harness=cheap_harness,
        adapter=cheap_adapter,
        spawner=spawner,
        session_store=session_store,
        monotonic=monotonic,
        sleep=sleep,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
    )


__all__ = [
    "CheapAdapterRunner",
    "FusionSessionRunner",
    "SessionSpawner",
    "SessionStatusStore",
    "build_executor_prompt",
    "default_file_lister",
    "runner_for_tier",
]
