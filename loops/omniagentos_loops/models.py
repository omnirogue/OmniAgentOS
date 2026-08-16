"""Model calls for loop nodes — ONLY through the ``llm/`` short-call client.

That client is the one working hard spend cap in this system ($10/day UTC,
fail-closed when the ledger is unreadable). Loops inherit it unchanged: this
module adds a per-call cost row on the events ledger and nothing else. It does
not add a second budget, a second provider path, or a LangChain chat model with
its own credentials — LangChain never sees an API key here.

WHERE THE CALL HAPPENS
----------------------

``ShortCallClient`` needs a provider credential, and the loop worker has none:
``loop_jobs._worker_env`` deletes every credential-shaped name, so
``os.environ.get("GEMINI_API_KEY")`` is empty and the client silently fell back
to the local proxy secret — i.e. a loop could not reliably call a model at all.

So when the scheduler opened a parent effect seam for this tick (it always does
in production), the completion is executed THERE, by
``omniagentos.scheduler.loop_effects``, under the same single budget guard and
against the same ledger. Consistency with ``replicate.generate`` is deliberate:
one place holds credentials, one place decides, and the worker only ever
declares. The per-call cost comes back over the wire so the ``loop.model_call``
event still describes the call that really ran.

Without a seam (a bare ``loop-worker`` invocation, a unit test), the local
client is used exactly as before. That is the only fallback, and it is a
fallback to the OLD behaviour, never to a weaker one.
"""

from __future__ import annotations

from typing import Any

from omniagentos.llm.budget import BudgetGuard
from omniagentos.llm.client import ShortCallClient
from omniagentos_loops import parent_seam
from omniagentos_loops.observability import LoopEvents, emit


class _CapturingGuard(BudgetGuard):
    """BudgetGuard that remembers the cost of the call it just recorded.

    ``ShortCallClient`` records spend internally and returns only text, so the
    per-call cost is captured at the point it is computed rather than
    reconstructed by diffing the ledger (which races other callers).
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.last_call: dict[str, Any] | None = None

    def record_spend(
        self,
        model: str,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        purpose: str = "default",
    ) -> float:
        cost = super().record_spend(
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            purpose=purpose,
        )
        self.last_call = {
            "model": model,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "estimated_usd_cost": cost,
            "purpose": purpose,
            "cost_quality": "estimated",
            "cost_source": "llm.budget.rates",
        }
        return cost


class _SeamClient:
    """Speaks ``ShortCallClient``'s shape, executes in the scheduler process.

    Only ``complete`` crosses the seam. ``complete_json`` composes on top of it
    here rather than adding a second parent-side capability: the JSON contract
    (clean the fences, parse, check required keys) is worker-side logic with no
    credential in it, and duplicating it across the wire would mean two places
    that can disagree about what "valid" means.
    """

    def __init__(self, instance_id: str, model: str | None = None) -> None:
        self._instance_id = instance_id
        self._model = model
        self.last_call: dict[str, Any] | None = None

    def complete(self, messages: list[dict[str, str]], *, purpose: str, **kwargs: Any) -> str:
        args: dict[str, Any] = {
            "messages": [dict(message) for message in messages],
            # The seam's ``purpose`` is an anchored identifier, so the caller's
            # free-text tag is normalised rather than passed through: an
            # identifier is data the parent can validate, a label is not.
            "purpose": _safe_purpose(purpose),
        }
        if self._model:
            args["model"] = self._model
        if kwargs.get("max_tokens") is not None:
            args["max_tokens"] = int(kwargs["max_tokens"])
        if kwargs.get("temperature") is not None:
            args["temperature_milli"] = int(round(float(kwargs["temperature"]) * 1000))

        result = parent_seam.request_effect(self._instance_id, parent_seam.MODEL_COMPLETE, args)
        if not result.get("success", True):
            # A reached authority refused (budget, provider 4xx). It is not a
            # string, so it must not masquerade as model output.
            from omniagentos.llm.budget import LLMClientError

            raise LLMClientError(str(result.get("error") or "the model call was refused"))
        text = result.get("text")
        if not isinstance(text, str):
            from omniagentos.llm.budget import LLMInvalidResponseError

            raise LLMInvalidResponseError("the parent seam returned no completion text")
        cost = result.get("cost")
        self.last_call = dict(cost) if isinstance(cost, dict) else None
        return text

    def complete_json(
        self,
        messages: list[dict[str, str]],
        required_keys: list[str],
        *,
        purpose: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        from omniagentos.llm.budget import LLMInvalidResponseError
        from omniagentos.llm.client import _clean_and_parse_json

        text = self.complete(messages, purpose=purpose, **kwargs)
        try:
            payload = _clean_and_parse_json(text)
        except ValueError as exc:
            raise LLMInvalidResponseError(f"model did not return JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise LLMInvalidResponseError("model returned JSON that is not an object")
        missing = [key for key in required_keys if key not in payload]
        if missing:
            raise LLMInvalidResponseError(f"model response is missing keys: {missing}")
        return payload


def _safe_purpose(purpose: str) -> str:
    cleaned = "".join(char if char.isalnum() or char in "_.:-" else "_" for char in purpose.lower())
    cleaned = cleaned.lstrip("_.:-")[:64]
    return cleaned or "default"


class LoopModel:
    """Thin, budget-capped, cost-observed model seam for loop nodes."""

    def __init__(self, ctx: Any, *, client: Any = None, model: str | None = None) -> None:
        self._ctx = ctx
        self._guard: Any = _CapturingGuard()
        if client is not None:
            self._client: Any = client
        elif parent_seam.available():
            # The parent holds the credential and the guard; this worker holds
            # neither. The seam client doubles as the cost source.
            self._client = _SeamClient(str(ctx.instance_id), model)
            self._guard = self._client
        else:
            self._client = ShortCallClient(budget_guard=self._guard, default_model=model)

    def complete(self, messages: list[dict[str, str]], *, purpose: str, **kwargs: Any) -> str:
        tagged = f"loop:{self._ctx.template}:{purpose}"
        text = self._client.complete(messages, purpose=tagged, **kwargs)
        self._record(tagged)
        return text

    def complete_json(
        self,
        messages: list[dict[str, str]],
        required_keys: list[str],
        *,
        purpose: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        tagged = f"loop:{self._ctx.template}:{purpose}"
        payload = self._client.complete_json(messages, required_keys, purpose=tagged, **kwargs)
        self._record(tagged)
        return dict(payload)

    def _record(self, purpose: str) -> None:
        call = getattr(self._guard, "last_call", None)
        emit(
            self._ctx,
            LoopEvents.MODEL_CALL,
            "loop.model_call",
            call or {"purpose": purpose, "cost_quality": "unknown"},
        )


__all__ = ["LoopModel"]
