"""Daily provider spend caps at the paid adapter call seam.

This guard is separate from :mod:`omniagentos.budget.policy`: that policy is a
per-run advisory budget, while this module is a per-billing-provider UTC-day
hard stop.  A cap refusal is terminally parked and must never be treated as a
provider outage or blind-retried.  In particular a Fireworks cap refusal never
causes Moonshot fallback; fallback is outage-only and its Moonshot call passes
through this same guard.

The two caps are intentionally independent.  If both providers are separately
exhausted, worst-case combined daily spend is their sum ($300 with the shipped
configuration), by design.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, Decimal, InvalidOperation, localcontext
from pathlib import Path
from threading import RLock
from typing import Any

import yaml

from omniagentos.adapters.common import (
    NormalizedCost,
    build_cost_observation,
    normalize_provider_cost,
)
from omniagentos.adapters.spend_db import SpendDbResolutionError, resolve_spend_db_path
from omniagentos.contracts import (
    NANO_USD_SCALE,
    AgentInput,
    CostObservation,
    CostQuality,
    ProviderRequestState,
    estimate_tokens,
    new_id,
)
from omniagentos.db.store import SqliteStore
from omniagentos.routing.api_policy import LINEAGE_UNKNOWN, model_lineage
from omniagentos.steward.notify import send_slack

OPS_ALERT_WEBHOOK_ENV = "OPS_ALERT_SLACK_WEBHOOK_URL"
OVERRIDE_ENV = "OMNIAGENTOS_SPEND_CAP_OVERRIDE"
CONFIG_ENV = "OMNIAGENTOS_SPEND_CAPS_CONFIG"
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "configs" / "spend-caps.yaml"
REQUIRED_DEFAULT_PROVIDER = "fireworks"
REQUIRED_DEFAULT_MODEL = "kimi-k3"
MILLION = Decimal(1_000_000)
# More than twice the shared HTTP adapter's 120-second default timeout. Calls
# older than this cannot still be a realistic in-flight request after restart.
STALE_RESERVATION_SECONDS = 300

LOG = logging.getLogger(__name__)


AlertSender = Callable[..., Any]
Now = Callable[[], str | datetime]


@dataclass(frozen=True, slots=True)
class ModelPricing:
    input_usd_per_million_tokens: Decimal
    output_usd_per_million_tokens: Decimal
    max_output_tokens: int
    default_output_tokens: int


@dataclass(frozen=True, slots=True)
class ProviderCap:
    daily_cap_usd_nanos: int
    models: dict[str, ModelPricing]


@dataclass(frozen=True, slots=True)
class SpendCapsConfig:
    safety_factor: Decimal
    soft_threshold: Decimal
    providers: dict[str, ProviderCap]
    revision: str


@dataclass(frozen=True, slots=True)
class SpendTicket:
    call_id: str
    provider: str
    model: str
    utc_day: str
    proposed_usd_nanos: int
    prior_usd_nanos: int
    cap_usd_nanos: int
    override_used: bool
    output_tokens_ceiling: int


class SpendGuardRefusal(RuntimeError):
    """Terminal paid-call refusal with an operator-distinguishable class."""

    def __init__(self, reason_class: str, detail: str) -> None:
        self.reason_class = reason_class
        self.detail = detail
        super().__init__(
            f"spend guard {reason_class}: terminally parked; no blind retry or "
            f"provider failover ({detail})"
        )


def classify_ledger_failure(exc: BaseException) -> str:
    """Separate identity/write conflicts from an unreadable ledger."""

    current: BaseException | None = exc
    while current is not None:
        if isinstance(current, sqlite3.IntegrityError):
            return "ledger_conflict"
        if isinstance(current, ValueError):
            detail = str(current).casefold()
            if "conflict" in detail or "unique constraint" in detail:
                return "ledger_conflict"
        current = current.__cause__ or current.__context__
    return "ledger_unreadable"


class SpendGuard:
    """Fail-closed guard backed by the canonical SQLite provider-call ledger."""

    def __init__(
        self,
        *,
        config_path: str | Path | None = None,
        db_path: str | None = None,
        alert_sender: AlertSender = send_slack,
        now: Now | None = None,
    ) -> None:
        configured = os.environ.get(CONFIG_ENV)
        self.config_path = Path(config_path or configured or DEFAULT_CONFIG_PATH)
        try:
            self.db_path = str(resolve_spend_db_path(db_path))
        except SpendDbResolutionError as exc:
            raise SpendGuardRefusal("simulation_spend_db", str(exc)) from exc
        self.alert_sender = alert_sender
        self.now = now or (lambda: datetime.now(UTC))
        self._store: SqliteStore | None = None
        self._store_lock = RLock()
        self.startup_swept_reservations = self.sweep_stale_reservations()

    def _utc_now(self) -> tuple[str, str]:
        raw = self.now()
        if isinstance(raw, datetime):
            moment = raw if raw.tzinfo is not None else raw.replace(tzinfo=UTC)
            utc = moment.astimezone(UTC)
            return utc.strftime("%Y-%m-%d"), utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        text = str(raw)
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise SpendGuardRefusal(
                "clock_unreadable", f"invalid UTC clock value {text!r}"
            ) from exc
        utc = (parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)).astimezone(UTC)
        return utc.strftime("%Y-%m-%d"), utc.strftime("%Y-%m-%dT%H:%M:%SZ")

    def _ledger(self) -> SqliteStore:
        with self._store_lock:
            if self._store is None:
                self._store = SqliteStore(self.db_path)
            return self._store

    def sweep_stale_reservations(self) -> int:
        """Recover reservations abandoned by a process crash and report count."""

        _utc_day, created_at = self._utc_now()
        now = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        before = (now - timedelta(seconds=STALE_RESERVATION_SECONDS)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        try:
            count = self._ledger().sweep_stale_provider_reservations(before=before)
        except Exception as exc:
            reason = classify_ledger_failure(exc)
            raise SpendGuardRefusal(
                reason,
                f"stale provider-call reservations could not be swept: "
                f"{type(exc).__name__}: {exc}",
            ) from exc
        if count:
            LOG.warning("settled %d stale spend reservation(s) to indeterminate", count)
        return count

    @staticmethod
    def _decimal(raw: Any, *, field: str, positive: bool = True) -> Decimal:
        if isinstance(raw, bool):
            raise ValueError(f"{field} must be decimal text or a number")
        try:
            value = Decimal(str(raw))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"{field} is not a decimal") from exc
        if not value.is_finite() or (value <= 0 if positive else value < 0):
            relation = "> 0" if positive else ">= 0"
            raise ValueError(f"{field} must be finite and {relation}")
        return value

    @staticmethod
    def _usd_to_nanos(value: Decimal, *, field: str) -> int:
        with localcontext() as context:
            context.prec = 40
            nanos = (value * NANO_USD_SCALE).to_integral_value(rounding=ROUND_CEILING)
        if nanos < 0:
            raise ValueError(f"{field} must be non-negative")
        return int(nanos)

    def _load_config(self) -> SpendCapsConfig:
        try:
            text = self.config_path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise SpendGuardRefusal(
                "config_missing", f"spend-cap config is missing at {self.config_path}"
            ) from exc
        except OSError as exc:
            raise SpendGuardRefusal(
                "config_unparseable",
                f"spend-cap config is unreadable at {self.config_path}: {type(exc).__name__}",
            ) from exc
        try:
            raw = yaml.safe_load(text)
            if not isinstance(raw, dict):
                raise ValueError("root must be a mapping")
            safety = self._decimal(raw.get("safety_factor"), field="safety_factor")
            soft = self._decimal(raw.get("soft_threshold"), field="soft_threshold")
            if soft > 1:
                raise ValueError("soft_threshold must be <= 1")
            raw_providers = raw.get("providers")
            if not isinstance(raw_providers, dict):
                raise ValueError("providers must be a mapping")
            providers: dict[str, ProviderCap] = {}
            for provider, provider_raw in raw_providers.items():
                if not isinstance(provider, str) or not isinstance(provider_raw, dict):
                    raise ValueError("every provider must be a named mapping")
                if provider_raw.get("enabled") is not True:
                    continue
                cap = self._decimal(
                    provider_raw.get("daily_cap_usd"),
                    field=f"providers.{provider}.daily_cap_usd",
                )
                raw_models = provider_raw.get("models")
                if not isinstance(raw_models, dict):
                    raise ValueError(f"providers.{provider}.models must be a mapping")
                models: dict[str, ModelPricing] = {}
                for model, model_raw in raw_models.items():
                    if not isinstance(model, str) or not isinstance(model_raw, dict):
                        raise ValueError(f"providers.{provider}.models has an invalid row")
                    max_output = model_raw.get("max_output_tokens")
                    if isinstance(max_output, bool) or not isinstance(max_output, int):
                        raise ValueError(
                            f"providers.{provider}.models.{model}.max_output_tokens "
                            "must be an integer"
                        )
                    if max_output <= 0:
                        raise ValueError(
                            f"providers.{provider}.models.{model}.max_output_tokens must be > 0"
                        )
                    default_output = model_raw.get(
                        "default_output_tokens", min(8192, max_output)
                    )
                    if isinstance(default_output, bool) or not isinstance(default_output, int):
                        raise ValueError(
                            f"providers.{provider}.models.{model}.default_output_tokens "
                            "must be an integer"
                        )
                    if default_output <= 0 or default_output > max_output:
                        raise ValueError(
                            f"providers.{provider}.models.{model}.default_output_tokens "
                            "must be > 0 and <= max_output_tokens"
                        )
                    models[model] = ModelPricing(
                        input_usd_per_million_tokens=self._decimal(
                            model_raw.get("input_usd_per_million_tokens"),
                            field=(
                                f"providers.{provider}.models.{model}.input_usd_per_million_tokens"
                            ),
                        ),
                        output_usd_per_million_tokens=self._decimal(
                            model_raw.get("output_usd_per_million_tokens"),
                            field=(
                                f"providers.{provider}.models.{model}.output_usd_per_million_tokens"
                            ),
                        ),
                        max_output_tokens=max_output,
                        default_output_tokens=default_output,
                    )
                providers[provider] = ProviderCap(
                    daily_cap_usd_nanos=self._usd_to_nanos(
                        cap, field=f"providers.{provider}.daily_cap_usd"
                    ),
                    models=models,
                )
            required = providers.get(REQUIRED_DEFAULT_PROVIDER)
            if required is None or REQUIRED_DEFAULT_MODEL not in required.models:
                raise ValueError(
                    "startup invariant failed: enabled fireworks provider must include "
                    "a kimi-k3 pricing row"
                )
            revision = str(raw.get("version") or "unversioned")
            return SpendCapsConfig(safety, soft, providers, revision)
        except SpendGuardRefusal:
            raise
        except Exception as exc:
            raise SpendGuardRefusal(
                "config_unparseable",
                f"spend-cap config at {self.config_path} is invalid: {exc}",
            ) from exc

    @staticmethod
    def _call_id(input: AgentInput, provider: str, attempt_index: int) -> str:
        existing = input.metadata.get("call_id")
        base = str(existing or new_id("call"))
        if not existing:
            # Preserve the generated root on the caller-owned metadata. Outage
            # fallback can then correlate its own unique ledger id to the
            # primary attempt instead of falling back to a process-wide literal.
            input.metadata["call_id"] = base
        return base if attempt_index == 0 else f"{base}:{provider}:{attempt_index}"

    def _base_observation(
        self,
        *,
        input: AgentInput,
        provider: str,
        model: str,
        transport: str,
        adapter_key: str,
        attempt_index: int,
        call_id: str,
        created_at: str,
        request_state: ProviderRequestState,
        provider_outcome: str | None,
        cost_source: str,
    ) -> CostObservation:
        metadata = dict(input.metadata)
        metadata["call_id"] = call_id
        shadow = input.model_copy(update={"metadata": metadata})
        base = build_cost_observation(
            input=shadow,
            normalized=normalize_provider_cost(),
            provider=provider,
            requested_model=model,
            effective_model=model,
            transport=transport,
            adapter_key=adapter_key,
            billing_provider=provider,
            model_lineage=model_lineage(model),
            attempt_index=attempt_index,
            request_state=request_state,
            provider_outcome=provider_outcome,
            input_tokens=estimate_tokens(input.prompt),
            cost_source=cost_source,
        )
        # ``build_cost_observation`` annotates UNKNOWN sources with the reason.
        # Guard marker/refusal source strings are query keys, so retain the
        # shared identity construction but pin the explicit marker verbatim.
        return base.model_copy(update={"created_at": created_at, "cost_source": cost_source})

    def _refusal_observation(
        self,
        *,
        input: AgentInput,
        provider: str,
        model: str,
        transport: str,
        adapter_key: str,
        attempt_index: int,
        call_id: str,
        created_at: str,
        reason_class: str,
    ) -> CostObservation:
        return self._base_observation(
            input=input,
            provider=provider,
            model=model,
            transport=transport,
            adapter_key=adapter_key,
            attempt_index=attempt_index,
            call_id=call_id,
            created_at=created_at,
            request_state="not_sent",
            provider_outcome="terminally_parked",
            cost_source=f"spend-guard-refusal:{reason_class}",
        )

    def _record_preledger_refusal(
        self,
        *,
        input: AgentInput,
        provider: str,
        model: str,
        transport: str,
        adapter_key: str,
        attempt_index: int,
        call_id: str,
        created_at: str,
        reason_class: str,
    ) -> None:
        try:
            self._ledger().record_provider_call(
                self._refusal_observation(
                    input=input,
                    provider=provider,
                    model=model,
                    transport=transport,
                    adapter_key=adapter_key,
                    attempt_index=attempt_index,
                    call_id=call_id,
                    created_at=created_at,
                    reason_class=reason_class,
                )
            )
        except Exception:
            # The refusal itself remains authoritative.  In particular a broken
            # ledger must not turn an unknown provider/model into an allowed call.
            pass

    def _marker(
        self,
        *,
        provider: str,
        model: str,
        created_at: str,
        call_id: str,
        execution_id: str,
        cost_source: str,
        outcome: str,
    ) -> CostObservation:
        synthetic = AgentInput(
            run_id="",
            task_id="",
            prompt="spend guard audit marker",
            model=model,
            metadata={
                "call_id": call_id,
                "request_id": f"{execution_id}:request",
                "execution_id": execution_id,
                "provider_call_stage": "worker",
            },
        )
        return self._base_observation(
            input=synthetic,
            provider=provider,
            model=model,
            transport="internal",
            adapter_key="spend-guard",
            attempt_index=0,
            call_id=call_id,
            created_at=created_at,
            request_state="not_sent",
            provider_outcome=outcome,
            cost_source=cost_source,
        )

    def _alert_once(
        self,
        *,
        store: SqliteStore,
        provider: str,
        model: str,
        utc_day: str,
        created_at: str,
        projected: int,
        cap: int,
    ) -> None:
        execution_id = f"spend-alert:{provider}:{utc_day}"
        marker = self._marker(
            provider=provider,
            model=model,
            created_at=created_at,
            call_id=execution_id,
            execution_id=execution_id,
            cost_source="spend-cap-alert:delivered",
            outcome="soft_threshold_alert_delivered",
        )

        def deliver() -> bool:
            result = self.alert_sender(
                (
                    f"OmniAgentOS spend warning: {provider} projected UTC-day spend "
                    f"is ${projected / NANO_USD_SCALE:.6f} of "
                    f"${cap / NANO_USD_SCALE:.2f} on {utc_day}."
                ),
                webhook_env=OPS_ALERT_WEBHOOK_ENV,
            )
            return bool(getattr(result, "ok", False))

        store.deliver_provider_alert_once(
            provider=provider,
            utc_day=utc_day,
            marker=marker,
            deliver=deliver,
        )

    def _override_active(self, provider: str, utc_day: str) -> bool:
        return os.environ.get(OVERRIDE_ENV, "") == f"{provider}:{utc_day}"

    def _audit_override(
        self,
        *,
        store: SqliteStore,
        provider: str,
        model: str,
        utc_day: str,
        created_at: str,
        projected: int,
        cap: int,
    ) -> None:
        marker_id = new_id("spend_override")
        store.record_provider_call(
            self._marker(
                provider=provider,
                model=model,
                created_at=created_at,
                call_id=marker_id,
                execution_id=f"spend-override:{provider}:{utc_day}:{marker_id}",
                cost_source="spend-cap-override:audit",
                outcome="emergency_override_used",
            )
        )
        self.alert_sender(
            (
                f"OmniAgentOS EMERGENCY SPEND OVERRIDE used for {provider} on {utc_day}: "
                f"projected ${projected / NANO_USD_SCALE:.6f}, cap "
                f"${cap / NANO_USD_SCALE:.2f}."
            ),
            webhook_env=OPS_ALERT_WEBHOOK_ENV,
        )

    def preflight(
        self,
        input: AgentInput,
        *,
        provider: str,
        model: str,
        transport: str,
        adapter_key: str,
        attempt_index: int = 0,
    ) -> SpendTicket:
        """Reserve a conservative call ceiling or raise a terminal refusal."""

        utc_day, created_at = self._utc_now()
        call_id = self._call_id(input, provider, attempt_index)
        try:
            config = self._load_config()
        except SpendGuardRefusal as exc:
            self._record_preledger_refusal(
                input=input,
                provider=provider,
                model=model,
                transport=transport,
                adapter_key=adapter_key,
                attempt_index=attempt_index,
                call_id=call_id,
                created_at=created_at,
                reason_class=exc.reason_class,
            )
            raise

        provider_cap = config.providers.get(provider)
        if provider_cap is None:
            reason = "unknown_provider"
            self._record_preledger_refusal(
                input=input,
                provider=provider,
                model=model,
                transport=transport,
                adapter_key=adapter_key,
                attempt_index=attempt_index,
                call_id=call_id,
                created_at=created_at,
                reason_class=reason,
            )
            raise SpendGuardRefusal(reason, f"paid provider {provider!r} has no enabled cap row")
        pricing = provider_cap.models.get(model)
        if pricing is None:
            reason = "unknown_model"
            self._record_preledger_refusal(
                input=input,
                provider=provider,
                model=model,
                transport=transport,
                adapter_key=adapter_key,
                attempt_index=attempt_index,
                call_id=call_id,
                created_at=created_at,
                reason_class=reason,
            )
            raise SpendGuardRefusal(
                reason, f"paid model {model!r} has no pricing row under provider {provider!r}"
            )
        lineage = model_lineage(model)
        if lineage == LINEAGE_UNKNOWN:
            reason = "unknown_model_lineage"
            self._record_preledger_refusal(
                input=input,
                provider=provider,
                model=model,
                transport=transport,
                adapter_key=adapter_key,
                attempt_index=attempt_index,
                call_id=call_id,
                created_at=created_at,
                reason_class=reason,
            )
            raise SpendGuardRefusal(
                reason, f"paid model {model!r} has no authoritative model lineage"
            )

        input_tokens = estimate_tokens(input.prompt)
        output_ceiling = input.budget.tokens_max
        if output_ceiling is None:
            output_ceiling = pricing.default_output_tokens
        elif output_ceiling > pricing.max_output_tokens:
            output_ceiling = pricing.max_output_tokens
        if isinstance(output_ceiling, bool) or output_ceiling <= 0:
            raise SpendGuardRefusal("token_ceiling_invalid", "call has no positive token ceiling")
        with localcontext() as context:
            context.prec = 40
            usd = (
                Decimal(input_tokens) * pricing.input_usd_per_million_tokens
                + Decimal(output_ceiling) * pricing.output_usd_per_million_tokens
            ) / MILLION
            ceiling_nanos = self._usd_to_nanos(
                usd * config.safety_factor,
                field="proposed_call_ceiling",
            )

        proposed = self._base_observation(
            input=input,
            provider=provider,
            model=model,
            transport=transport,
            adapter_key=adapter_key,
            attempt_index=attempt_index,
            call_id=call_id,
            created_at=created_at,
            request_state="not_sent",
            provider_outcome="spend_reserved",
            cost_source="spend-cap-upper-bound",
        ).model_copy(
            update={
                "cost_quality": CostQuality.ESTIMATED,
                "cost_upper_bound_usd_nanos": ceiling_nanos,
                "pricing_revision": config.revision,
            }
        )
        refused = self._refusal_observation(
            input=input,
            provider=provider,
            model=model,
            transport=transport,
            adapter_key=adapter_key,
            attempt_index=attempt_index,
            call_id=call_id,
            created_at=created_at,
            reason_class="daily_cap_exceeded",
        )
        override_requested = self._override_active(provider, utc_day)
        try:
            store = self._ledger()
            decision = store.reserve_provider_spend(
                provider=provider,
                utc_day=utc_day,
                cap_usd_nanos=provider_cap.daily_cap_usd_nanos,
                proposed=proposed,
                refused=refused,
                override=override_requested,
            )
        except Exception as exc:
            reason = classify_ledger_failure(exc)
            raise SpendGuardRefusal(
                reason,
                f"provider_call_usage/spend_day could not be read: {type(exc).__name__}: {exc}",
            ) from exc

        projected = int(decision["projected_usd_nanos"])
        cap = int(decision["cap_usd_nanos"])
        override_used = bool(decision["override"])
        allowed = bool(decision["allowed"])

        def _best_effort_alerting() -> None:
            # B1/B2 re-open fix (2026-08-06 review): alerting must NEVER be
            # able to defeat, delay, or re-type the terminal refusal below.
            # The prior build ran this before the raise, unwrapped; an
            # over-cap call hits this path on every refusal, so any real
            # write/network failure here (a live contention exception, a
            # dead webhook) escaped un-classified and fell into a caller's
            # generic ``except Exception`` -- exactly the non-terminal
            # treatment B1 exists to close, reached through a different
            # door. This helper is best-effort only: every exception is
            # logged and swallowed, never re-raised.
            try:
                if override_used:
                    self._audit_override(
                        store=store,
                        provider=provider,
                        model=model,
                        utc_day=utc_day,
                        created_at=created_at,
                        projected=projected,
                        cap=cap,
                    )
                else:
                    soft_nanos = int(
                        (Decimal(cap) * config.soft_threshold).to_integral_value(
                            rounding=ROUND_CEILING
                        )
                    )
                    if projected >= soft_nanos:
                        self._alert_once(
                            store=store,
                            provider=provider,
                            model=model,
                            utc_day=utc_day,
                            created_at=created_at,
                            projected=projected,
                            cap=cap,
                        )
            except Exception:
                LOG.exception(
                    "spend guard: best-effort alert/audit side channel failed for "
                    "%s on %s (allowed=%s, override=%s); the reservation decision "
                    "itself is unaffected",
                    provider,
                    utc_day,
                    allowed,
                    override_used,
                )

        try:
            if not allowed:
                raise SpendGuardRefusal(
                    "daily_cap_exceeded",
                    (
                        f"{provider} prior={decision['prior_usd_nanos']}ns + "
                        f"proposed={decision['proposed_usd_nanos']}ns > cap={cap}ns "
                        f"for UTC day {utc_day}"
                    ),
                )
        finally:
            # ``finally`` runs the best-effort side channel whether the
            # reservation was allowed or refused, but nothing it does can
            # suppress or re-type the SpendGuardRefusal being propagated --
            # it is caught entirely inside ``_best_effort_alerting``.
            _best_effort_alerting()
        return SpendTicket(
            call_id=call_id,
            provider=provider,
            model=model,
            utc_day=utc_day,
            proposed_usd_nanos=ceiling_nanos,
            prior_usd_nanos=int(decision["prior_usd_nanos"]),
            cap_usd_nanos=cap,
            override_used=override_used,
            output_tokens_ceiling=output_ceiling,
        )

    def settle_exact(
        self,
        ticket: SpendTicket,
        normalized: NormalizedCost,
        *,
        provider_outcome: str,
        provider_request_id: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
    ) -> dict[str, Any]:
        """Replace an estimate with the provider's exact response-time cost.

        TRUE-COST ACCOUNTING: the ledger records the ACTUAL billed cost, always,
        with ``cost_quality="exact"``. Understating a real charge would corrupt
        every dashboard, audit and projection that reads ``spend_day``.

        SCOPE (SI lane, 2026-08-12) — cap-safe IN PRACTICE, not by a proven
        invariant. SI uses a ~$0.00003/call model (qwen3.7-flash), so daily spend
        is fractions of a cent and the $50 cap is never approached. The admission
        cap (:meth:`preflight` refuses unless ``prior + reserved <= cap``, or the
        ``store.py`` emergency override) and this true-cost recording are
        BEST-EFFORT: the reservation prices input at ``estimate_tokens`` (an
        estimate, not a ceiling), so ``settled <= reserved`` is NOT guaranteed —
        a token-dense prompt can under-reserve input and settle over-cap even when
        the provider honours ``max_tokens``. A conservative input-token CEILING
        (so ``settled <= reserved`` by construction), the shared DAL boundary,
        credential-scoped enforcement, override-audit ordering, and settle-retry
        understatement are ESTATE-WIDE spend-safety concerns consolidated in
        loopqueue proposal 9f7448b1 — deliberately NOT re-implemented per-adapter
        here (a second, bespoke enforcement copy was fail-open and was removed).
        """

        if normalized.quality is not CostQuality.EXACT:
            raise ValueError("settle_exact requires an exact normalized provider cost")

        total_tokens = (
            input_tokens + output_tokens
            if input_tokens is not None and output_tokens is not None
            else None
        )
        return self._ledger().settle_provider_call(
            ticket.call_id,
            {
                "request_state": "sent",
                "provider_outcome": provider_outcome,
                "provider_request_id": provider_request_id,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": total_tokens,
                "cost_usd_decimal": normalized.cost_usd_decimal,
                "cost_usd_nanos": normalized.cost_usd_nanos,
                "cost_upper_bound_usd_nanos": None,
                "cost_quality": "exact",
                "cost_source": "provider-report",
            },
        )

    def settle_unknown(
        self,
        ticket: SpendTicket,
        *,
        provider_outcome: str,
        request_state: ProviderRequestState = "indeterminate",
    ) -> dict[str, Any]:
        """Settle request state while retaining its conservative upper bound.

        Use this only when whether the provider billed is genuinely unknown.
        Unknown is never a favourable value -- the reservation stays in
        place at its full upper bound. For an outcome that PROVES the
        provider never billed (a 401/403/404 that never reached model
        execution, a refused connection, an unresolved DNS name), use
        :meth:`settle_released` instead; retaining a reservation for those
        is not conservatism, it is a cap that a rotated credential can burn
        through to a self-inflicted full-day outage (M4, 2026-08-06 review).
        """

        return self._ledger().settle_provider_call(
            ticket.call_id,
            {
                "request_state": request_state,
                "provider_outcome": provider_outcome,
                "cost_source": "spend-cap-upper-bound:provider-cost-unavailable",
            },
        )

    def settle_released(
        self,
        ticket: SpendTicket,
        *,
        provider_outcome: str,
        request_state: ProviderRequestState = "sent",
    ) -> dict[str, Any]:
        """Release a reservation whose outcome PROVES no billing occurred.

        M4 (2026-08-06 review): a rotated credential or a dead endpoint
        produces a provably-not-billed outcome (401/403/404 with no usage
        payload, connection refused, DNS failure) on every retry. Retaining
        the conservative upper bound for each of those -- the ``settle_unknown``
        default -- means N such failures burn N reservations out of the cap
        for $0 of real spend, self-inflicting a full-day outage once the cap
        is exhausted. This is explicitly NOT the "unknown reads as zero"
        favourable-absence bug the money boundary forbids: the caller here
        provides no discretion, it is only ever reached for an outcome class
        that is provably $0 by construction (an HTTP error before model
        execution, or no connection at all), never for an ambiguous one.
        """

        return self._ledger().settle_provider_call(
            ticket.call_id,
            {
                "request_state": request_state,
                "provider_outcome": provider_outcome,
                "cost_usd_decimal": "0",
                "cost_usd_nanos": 0,
                "cost_upper_bound_usd_nanos": 0,
                "cost_quality": "exact",
                "cost_source": "spend-cap-release:provably-not-billed",
            },
        )

    def close(self) -> None:
        with self._store_lock:
            if self._store is not None:
                self._store.close()
                self._store = None


_DEFAULT_GUARD: SpendGuard | None = None
_DEFAULT_LOCK = RLock()


def default_spend_guard() -> SpendGuard:
    """Process-local guard; config is still re-read on every preflight call."""

    global _DEFAULT_GUARD
    with _DEFAULT_LOCK:
        if _DEFAULT_GUARD is None:
            _DEFAULT_GUARD = SpendGuard()
        return _DEFAULT_GUARD


__all__ = [
    "OPS_ALERT_WEBHOOK_ENV",
    "OVERRIDE_ENV",
    "SpendGuard",
    "SpendGuardRefusal",
    "SpendTicket",
    "classify_ledger_failure",
    "default_spend_guard",
]
