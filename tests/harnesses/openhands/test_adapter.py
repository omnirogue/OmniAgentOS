"""Tests for OpenHandsAdapter (omniagentos/harnesses/openhands/adapter.py).

Runs with NO provider API key, and -- in the default `uv run pytest` env used
by this package's validation command -- typically without openhands-sdk
installed either (the `harness` extra is opt-in; see docs/research/harnesses.md).
Tests that exercise the "SDK is importable" branches inject a small fake
`openhands.sdk`-shaped module by monkeypatching `_import_sdk`, so the
adapter's own mapping logic (key gating, text/usage extraction, budget
mapping) is exercised deterministically without the real package or any
network call. A single @pytest.mark.live_cli test drives the real SDK end to
end when a real key + the extra are both present; excluded by default via
pyproject's `addopts = "-m 'not live_cli'"`.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
import types
import uuid
from collections.abc import Iterator
from typing import Any

import pytest

from omniagentos.contracts import (
    AgentAdapter,
    AgentInput,
    BudgetSpec,
    HarnessProfile,
    HarnessType,
    HealthStatus,
    ResultStatus,
)
from omniagentos.harnesses.envhash import env_hash
from omniagentos.harnesses.openhands import adapter as adapter_module
from omniagentos.harnesses.openhands.adapter import (
    _PROVIDER_KEY_ENV_VARS,
    OpenHandsAdapter,
)

# ---------------------------------------------------------------------------
# Env-control fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def no_provider_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear every provider API key env var this adapter recognizes, so tests
    are deterministic regardless of the ambient shell (this agent's own
    environment may itself carry an ANTHROPIC_API_KEY)."""
    for name in _PROVIDER_KEY_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def fake_key(monkeypatch: pytest.MonkeyPatch, no_provider_keys: None) -> str:
    """A single fake ANTHROPIC_API_KEY set, every other recognized key cleared."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-test-key")
    return "ANTHROPIC_API_KEY"


class _BlockOpenHandsFinder:
    """Meta-path finder that makes `import openhands` (and any submodule)
    fail, simulating an environment without the `harness` extra installed --
    regardless of whether it actually is installed here."""

    def find_spec(self, fullname: str, path: Any, target: Any = None) -> None:
        if fullname == "openhands" or fullname.startswith("openhands."):
            raise ModuleNotFoundError(f"No module named {fullname!r} (blocked for test)")
        return None


@pytest.fixture
def block_openhands_import() -> Iterator[None]:
    saved = {
        name: mod
        for name, mod in sys.modules.items()
        if name == "openhands" or name.startswith("openhands.")
    }
    for name in saved:
        del sys.modules[name]
    finder = _BlockOpenHandsFinder()
    sys.meta_path.insert(0, finder)
    try:
        yield
    finally:
        sys.meta_path.remove(finder)
        sys.modules.update(saved)


def _openhands_installed() -> bool:
    # Non-dotted find_spec never executes module code -- safe environment probe.
    return importlib.util.find_spec("openhands") is not None


# ---------------------------------------------------------------------------
# Fake `openhands.sdk`-shaped module, for deterministic mapping-logic tests.
# Shapes empirically confirmed against the real openhands-sdk==1.35.0 install
# (see docs/research/harnesses.md and the p10-openhands recon notes).
# ---------------------------------------------------------------------------


class _FakeContentPart:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeLlmMessage:
    def __init__(self, content: list[_FakeContentPart]) -> None:
        self.content = content


class _FakeMessageEvent:
    def __init__(self, source: str, text: str) -> None:
        self.source = source
        self.llm_message = _FakeLlmMessage([_FakeContentPart(text)])


class _FakeTokenUsage:
    def __init__(self, prompt_tokens: int, completion_tokens: int) -> None:
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens


class _FakeMetrics:
    def __init__(self, cost: float, prompt_tokens: int, completion_tokens: int, calls: int) -> None:
        self.accumulated_cost = cost
        self.accumulated_token_usage = _FakeTokenUsage(prompt_tokens, completion_tokens)
        self.token_usages: list[object] = [object()] * calls


class _FakeConversationStats:
    def __init__(self, metrics: _FakeMetrics) -> None:
        self._metrics = metrics

    def get_combined_metrics(self) -> _FakeMetrics:
        return self._metrics


class _FakeState:
    def __init__(self) -> None:
        self.events: list[Any] = []
        self.id = uuid.uuid4()


class _FakeConversation:
    """Stand-in for openhands.sdk.conversation.impl.local_conversation.LocalConversation,
    covering just the surface OpenHandsAdapter.run() actually touches."""

    def __init__(self, agent: Any, workspace: Any, **kwargs: Any) -> None:
        self.agent = agent
        self.workspace = workspace
        self.kwargs = kwargs
        self.sent: list[str] = []
        self.ran = False
        self.state = _FakeState()
        self.reply_text = "fake agent reply"
        self.usage_metrics = _FakeMetrics(
            cost=0.0021, prompt_tokens=42, completion_tokens=7, calls=2
        )
        self.conversation_stats = _FakeConversationStats(_FakeMetrics(0.0, 0, 0, 0))
        self.raise_on_run: Exception | None = None

    def send_message(self, message: str) -> None:
        self.sent.append(message)

    def run(self) -> None:
        self.ran = True
        if self.raise_on_run is not None:
            raise self.raise_on_run
        self.state.events = [
            _FakeMessageEvent("user", self.sent[-1] if self.sent else ""),
            _FakeMessageEvent("agent", self.reply_text),
        ]
        self.conversation_stats = _FakeConversationStats(self.usage_metrics)


def _make_fake_sdk_module(created: list[_FakeConversation]) -> types.ModuleType:
    """Minimal fake standing in for `openhands.sdk`: LLM, Agent,
    LocalWorkspace, LocalConversation, MessageEvent -- exactly what
    OpenHandsAdapter touches."""
    fake = types.ModuleType("fake_openhands_sdk")

    class FakeLLM:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

    class FakeAgent:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

    class FakeLocalWorkspace:
        def __init__(self, *, working_dir: str) -> None:
            self.working_dir = working_dir

    def conversation_factory(agent: Any, workspace: Any, **kwargs: Any) -> _FakeConversation:
        conv = _FakeConversation(agent, workspace, **kwargs)
        created.append(conv)
        return conv

    fake.LLM = FakeLLM
    fake.Agent = FakeAgent
    fake.LocalWorkspace = FakeLocalWorkspace
    fake.LocalConversation = conversation_factory
    fake.MessageEvent = _FakeMessageEvent
    return fake


# ---------------------------------------------------------------------------
# Contract shape
# ---------------------------------------------------------------------------


def test_isinstance_agent_adapter() -> None:
    assert isinstance(OpenHandsAdapter(), AgentAdapter)
    assert OpenHandsAdapter.name == "openhands"


def test_resolves_via_registry_style_import_path() -> None:
    # contracts/interfaces.md "p04 -- omniagentos.adapters": the registry
    # (owned by p04, not present in this worktree) must resolve harness id
    # "openhands" to exactly this dotted path. Exercise the same mechanism
    # directly since the registry module doesn't exist here yet.
    module_path, class_name = "omniagentos.harnesses.openhands.adapter:OpenHandsAdapter".split(":")
    resolved = getattr(importlib.import_module(module_path), class_name)
    assert resolved is OpenHandsAdapter


def test_profile_returns_harness_profile_with_env_hash() -> None:
    profile = OpenHandsAdapter().profile()
    assert isinstance(profile, HarnessProfile)
    assert profile.harness == HarnessType.OPENHANDS
    assert profile.version == OpenHandsAdapter.version
    assert profile.env_hash == env_hash()
    assert profile.env_hash != ""


def test_cancel_is_best_effort_false() -> None:
    assert OpenHandsAdapter().cancel("some-session-ref") is False


# ---------------------------------------------------------------------------
# Import laziness (the core honesty property this adapter is built around)
# ---------------------------------------------------------------------------


def test_module_import_is_lazy(block_openhands_import: None) -> None:
    """Importing the adapter module itself must never require `openhands` to
    be importable."""
    module = importlib.reload(adapter_module)
    assert hasattr(module, "OpenHandsAdapter")
    assert module.OpenHandsAdapter.name == "openhands"


def test_health_reports_unhealthy_when_openhands_not_importable(
    block_openhands_import: None,
) -> None:
    result = OpenHandsAdapter().health()
    assert isinstance(result, HealthStatus)
    assert result.healthy is False
    assert result.capabilities == {"live_runs": False}
    assert "import failed" in result.detail


def test_run_with_key_but_blocked_import_reports_import_error_not_no_key(
    fake_key: str, block_openhands_import: None
) -> None:
    result = OpenHandsAdapter().run(AgentInput(run_id="run_1", task_id="tsk_1", prompt="hi"))
    assert result.status == ResultStatus.ERROR
    assert result.error is not None
    assert "no provider API key" not in result.error
    assert "import failed" in result.error


# ---------------------------------------------------------------------------
# health() -- shape + honesty, both live and fully-mocked
# ---------------------------------------------------------------------------


def test_health_returns_health_status_shape() -> None:
    result = OpenHandsAdapter().health()
    assert isinstance(result, HealthStatus)
    assert isinstance(result.healthy, bool)
    assert isinstance(result.detail, str) and result.detail
    assert isinstance(result.capabilities, dict)
    assert "live_runs" in result.capabilities
    assert isinstance(result.capabilities["live_runs"], bool)


def test_health_no_key_never_claims_live_runs(no_provider_keys: None) -> None:
    result = OpenHandsAdapter().health()
    assert result.capabilities["live_runs"] is False
    # healthy reflects whether the SDK is actually importable in THIS env;
    # either way live_runs must never be True without a key.
    assert result.healthy is _openhands_installed()


def test_health_installed_no_key_is_healthy_but_live_runs_false(
    no_provider_keys: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_sdk = _make_fake_sdk_module([])
    monkeypatch.setattr(adapter_module, "_import_sdk", lambda: fake_sdk)

    result = OpenHandsAdapter().health()
    assert result.healthy is True
    assert result.capabilities == {"live_runs": False}
    assert "key" in result.detail.lower()


def test_health_installed_with_key_reports_live_runs_true(
    fake_key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_sdk = _make_fake_sdk_module([])
    monkeypatch.setattr(adapter_module, "_import_sdk", lambda: fake_sdk)

    result = OpenHandsAdapter().health()
    assert result.healthy is True
    assert result.capabilities == {"live_runs": True}
    assert fake_key in result.detail


def test_health_workspace_instantiation_failure_is_unhealthy(
    no_provider_keys: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_sdk = _make_fake_sdk_module([])

    class BrokenWorkspace:
        def __init__(self, *, working_dir: str) -> None:
            raise RuntimeError("workspace boom")

    fake_sdk.LocalWorkspace = BrokenWorkspace
    monkeypatch.setattr(adapter_module, "_import_sdk", lambda: fake_sdk)

    result = OpenHandsAdapter().health()
    assert result.healthy is False
    assert result.capabilities == {"live_runs": False}
    assert "LocalWorkspace" in result.detail


# ---------------------------------------------------------------------------
# run() -- no-key path (MUST pass with no key, per the brief)
# ---------------------------------------------------------------------------


def test_run_no_key_returns_honest_error_without_importing_sdk(
    no_provider_keys: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _must_not_be_called() -> None:
        raise AssertionError("run() must not import openhands when no key is present")

    monkeypatch.setattr(adapter_module, "_import_sdk", _must_not_be_called)

    result = OpenHandsAdapter().run(
        AgentInput(run_id="run_1", task_id="tsk_1", prompt="say hi", working_dir="/tmp")
    )
    assert result.status == ResultStatus.ERROR
    assert result.error == "openhands: no provider API key; live runs unavailable"
    assert result.output_text == ""
    assert result.usage.wall_ms >= 1
    assert result.usage.estimated is True
    assert result.usage.source == "estimator"


# ---------------------------------------------------------------------------
# run() -- fully-mocked live-key path (no real SDK / network needed)
# ---------------------------------------------------------------------------


def test_run_with_key_success_maps_output_and_usage(
    fake_key: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    created: list[_FakeConversation] = []
    fake_sdk = _make_fake_sdk_module(created)
    monkeypatch.setattr(adapter_module, "_import_sdk", lambda: fake_sdk)

    result = OpenHandsAdapter().run(
        AgentInput(
            run_id="run_42",
            task_id="tsk_42",
            prompt="please say pong",
            working_dir=str(tmp_path),
            budget=BudgetSpec(max_turns=5, cost_usd_max=1.5),
        )
    )

    assert result.status == ResultStatus.OK
    assert result.error is None
    assert result.output_text == "fake agent reply"
    assert result.session_ref is not None
    assert result.usage.estimated is False
    assert result.usage.source == "imported"
    assert result.usage.input_tokens == 42
    assert result.usage.output_tokens == 7
    assert result.usage.cost_usd == pytest.approx(0.0021)
    assert result.usage.turns == 2

    assert len(created) == 1
    conv = created[0]
    assert conv.sent == ["please say pong"]
    assert conv.kwargs["max_iteration_per_run"] == 5
    # Budget policy is advisory by default (session_budget_cap returns None),
    # so the OpenHands cost cap is intentionally NOT injected unless
    # OMNIAGENTOS_BUDGET_ENFORCEMENT is set to block. See budget.policy.
    from omniagentos.budget.policy import blocks

    if blocks():
        assert conv.kwargs["max_budget_per_run"] == 1.5
    else:
        assert "max_budget_per_run" not in conv.kwargs


def test_run_with_key_uses_default_model_for_detected_provider(
    fake_key: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    created: list[_FakeConversation] = []
    fake_sdk = _make_fake_sdk_module(created)
    monkeypatch.setattr(adapter_module, "_import_sdk", lambda: fake_sdk)

    OpenHandsAdapter().run(
        AgentInput(run_id="run_1", task_id="tsk_1", prompt="hi", working_dir=str(tmp_path))
    )

    assert len(created) == 1
    llm_kwargs = created[0].agent.kwargs["llm"].kwargs
    assert llm_kwargs["model"] == adapter_module._DEFAULT_MODEL_BY_KEY_ENV[fake_key]


def test_run_with_key_honors_explicit_model(
    fake_key: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    created: list[_FakeConversation] = []
    fake_sdk = _make_fake_sdk_module(created)
    monkeypatch.setattr(adapter_module, "_import_sdk", lambda: fake_sdk)

    OpenHandsAdapter().run(
        AgentInput(
            run_id="run_1",
            task_id="tsk_1",
            prompt="hi",
            working_dir=str(tmp_path),
            model="anthropic/claude-opus-explicit",
        )
    )

    llm_kwargs = created[0].agent.kwargs["llm"].kwargs
    assert llm_kwargs["model"] == "anthropic/claude-opus-explicit"


def test_run_with_key_success_falls_back_to_estimator_when_sdk_reports_no_usage(
    fake_key: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    created: list[_FakeConversation] = []
    fake_sdk = _make_fake_sdk_module(created)

    def factory(agent: Any, workspace: Any, **kwargs: Any) -> _FakeConversation:
        conv = _FakeConversation(agent, workspace, **kwargs)
        conv.usage_metrics = _FakeMetrics(cost=0.0, prompt_tokens=0, completion_tokens=0, calls=0)
        created.append(conv)
        return conv

    fake_sdk.LocalConversation = factory
    monkeypatch.setattr(adapter_module, "_import_sdk", lambda: fake_sdk)

    result = OpenHandsAdapter().run(
        AgentInput(
            run_id="run_1", task_id="tsk_1", prompt="0123456789ab", working_dir=str(tmp_path)
        )
    )

    assert result.status == ResultStatus.OK
    assert result.usage.estimated is True
    assert result.usage.source == "estimator"
    assert result.usage.turns is None
    assert result.usage.input_tokens == 3  # estimate_tokens("0123456789ab") == len // 4


def test_run_with_key_conversation_failure_maps_to_error(
    fake_key: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    created: list[_FakeConversation] = []
    fake_sdk = _make_fake_sdk_module(created)

    def failing_factory(agent: Any, workspace: Any, **kwargs: Any) -> _FakeConversation:
        conv = _FakeConversation(agent, workspace, **kwargs)
        conv.raise_on_run = RuntimeError("boom")
        created.append(conv)
        return conv

    fake_sdk.LocalConversation = failing_factory
    monkeypatch.setattr(adapter_module, "_import_sdk", lambda: fake_sdk)

    result = OpenHandsAdapter().run(
        AgentInput(run_id="run_1", task_id="tsk_1", prompt="hi", working_dir=str(tmp_path))
    )
    assert result.status == ResultStatus.ERROR
    assert result.error is not None
    assert "run failed" in result.error
    assert "boom" in result.error
    assert result.usage.estimated is True


# ---------------------------------------------------------------------------
# Real end-to-end (needs a real provider key + the harness extra installed).
# Excluded by default: pyproject `addopts = "-m 'not live_cli'"`.
# ---------------------------------------------------------------------------


@pytest.mark.live_cli
def test_run_live_with_real_key_and_sdk(tmp_path: Any) -> None:
    """Run explicitly with a real key present:
    uv run pytest tests/harnesses/openhands -m live_cli
    """
    if not _openhands_installed():
        pytest.skip("openhands-sdk not installed (harness extra not synced)")
    if adapter_module._detect_provider_key_env() is None:
        pytest.skip("no provider API key in env")

    result = OpenHandsAdapter().run(
        AgentInput(
            run_id="run_live_1",
            task_id="tsk_live_1",
            prompt="Reply with exactly one word: pong",
            working_dir=str(tmp_path),
        )
    )
    assert result.status == ResultStatus.OK
    assert result.output_text.strip()
    assert result.usage.estimated is False
