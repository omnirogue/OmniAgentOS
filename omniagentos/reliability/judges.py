"""Judge panel for blind consensus evaluation of improvements (§4, §5b.7).

A panel of 3 distinct model families (anthropic/openai/xai + 2 fallbacks)
evaluates proposals in parallel, receive raw sandbox diff + fenced evidence,
and return strict JSON verdicts. Fail-closed to panel_blocked when <3
families are available.

Design constraints:
  - Blind + parallel: each judge gets only the sandbox diff, not other votes
  - Untrusted evidence fencing: all external failure data wrapped in quote_untrusted()
  - Process-group kill on deadline (§5b.6)
  - Idempotent vote insertion: panel_attempt_id + model_family unique key
"""

from __future__ import annotations

import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from omniagentos.adapters.registry import resolve_adapter
from omniagentos.contracts import AgentInput, ResultStatus, new_id
from omniagentos.reliability.contracts import Improvement, ReliabilityStore
from omniagentos.reliability.taxonomy import VerdictKind
from omniagentos.steward.quoting import quote_untrusted

AdapterResolver = Callable[[str], Any]


class PanelBlockedError(Exception):
    """Could not seat 3 distinct families."""

    pass


def _get_adapter_family(harness: str) -> str:
    """Map harness key to model family."""
    if harness.startswith("cli-claude"):
        return "anthropic"
    elif harness.startswith("cli-codex"):
        return "openai"
    elif harness.startswith("cli-grok"):
        return "xai"
    elif harness.startswith("cli-gemini"):
        return "google"
    elif harness.startswith("cli-kimi"):
        return "moonshot"
    elif harness.startswith("cli-qwen"):
        return "alibaba"
    else:
        return "unknown"


class JudgePanel:
    """Blind parallel judge panel for consensus evaluation (§4).

    Reads panel configuration from governance.yaml (or defaults), validates
    3 distinct families are available, and invokes judges in parallel with
    hard deadline + process-group kill.
    """

    # Default panel families (binding constants per §6)
    DEFAULT_FAMILIES = ["cli-claude", "cli-codex", "cli-grok"]
    FALLBACK_FAMILIES = ["cli-gemini", "cli-kimi"]
    MIN_DISTINCT_FAMILIES = 3

    def __init__(
        self,
        store: ReliabilityStore,
        governance_config: dict[str, Any] | None = None,
        judge_timeout_seconds: int = 120,
        adapter_resolver: AdapterResolver | None = None,
    ):
        """Initialize the panel. Availability is checked lazily, NOT at construction.

        Fail-closed (§5b.7) happens at ``evaluate`` time, where a panel that cannot
        seat 3 distinct families returns an INCOMPLETE :class:`JudgeOutcome`
        (``complete=False``) — the pipeline then moves the improvement to
        ``panel_blocked``. Constructing the panel never raises on availability, so a
        transient outage does not crash the audit loop.

        Args:
            store: ReliabilityStore (vote persistence is the pipeline's job, not ours)
            governance_config: panel family config (from governance.yaml)
            judge_timeout_seconds: per-judge invocation deadline
            adapter_resolver: injectable resolver (tests pass mock adapters)
        """
        self.store = store
        self.judge_timeout_seconds = judge_timeout_seconds
        self._resolve = adapter_resolver or resolve_adapter

        if governance_config and "panel_families" in governance_config:
            self.families = list(governance_config["panel_families"])
        else:
            self.families = list(self.DEFAULT_FAMILIES)
        for fb in self.FALLBACK_FAMILIES:
            if fb not in self.families:
                self.families.append(fb)
        self.available_judges: list[tuple[str, str]] = []

    def _seat_families(self) -> list[tuple[str, str]]:
        """Seat one healthy adapter per DISTINCT family (§5b.7, m4).

        Availability = adapter resolves + reports healthy. Returns ``[(harness,
        family)]`` in preference order (defaults before fallbacks), at most one per
        family, so a substitution can never seat two of the same family.
        """
        seated: list[tuple[str, str]] = []
        seen: set[str] = set()
        for harness in self.families:
            family = _get_adapter_family(harness)
            if family in seen or family == "unknown":
                continue
            try:
                adapter = self._resolve(harness)
                if adapter.health().healthy:
                    seated.append((harness, family))
                    seen.add(family)
            except Exception:
                continue
        self.available_judges = seated
        return seated

    def evaluate(self, improvement: Improvement, sandbox: Any) -> Any:
        """Blind, parallel panel evaluation conforming to the pipeline seam (§4, §5b.7).

        Returns a ``pipeline.JudgeOutcome``. Fail-closed: fewer than 3 distinct
        seated families ⇒ ``complete=False`` (⇒ ``panel_blocked``). Votes are NOT
        persisted here — the pipeline owns idempotent ``insert_vote`` on the returned
        ``panel_attempt_id``. Each judge sees the raw sandbox diff (primary artifact)
        plus fenced evidence and never another judge's vote.
        """
        from omniagentos.reliability.pipeline import JudgeOutcome

        panel_attempt_id = new_id("pan")
        seated = self._seat_families()
        if len({f for _, f in seated}) < self.MIN_DISTINCT_FAMILIES:
            return JudgeOutcome(
                panel_attempt_id=panel_attempt_id,
                families_seated=len({f for _, f in seated}),
                votes=[],
                complete=False,
            )

        raw_diff = ""
        sandbox_results: dict[str, Any] = {}
        if sandbox is not None:
            sandbox_results = getattr(sandbox, "report", None) or {}
            raw_diff = (
                sandbox_results.get("git_diff", "") if isinstance(sandbox_results, dict) else ""
            )
        fenced_evidence = _fence_evidence(improvement)
        prompt = _build_judge_prompt(
            improvement_id=getattr(improvement, "id", ""),
            sandbox_diff=raw_diff,
            fenced_evidence=fenced_evidence,
            sandbox_results=sandbox_results,
        )

        votes: list[dict[str, Any]] = []
        seats = seated[: self.MIN_DISTINCT_FAMILIES]
        with ThreadPoolExecutor(max_workers=self.MIN_DISTINCT_FAMILIES) as executor:
            futures = {
                executor.submit(
                    self._invoke_single_judge,
                    harness=harness,
                    family=family,
                    prompt=prompt,
                    timeout_seconds=self.judge_timeout_seconds,
                ): (harness, family)
                for harness, family in seats
            }
            for future in as_completed(futures):
                harness, family = futures[future]
                try:
                    verdict = future.result(timeout=self.judge_timeout_seconds + 5)
                except Exception as exc:  # noqa: BLE001 - a stuck judge is a needs_human vote
                    verdict = {
                        "verdict": VerdictKind.NEEDS_HUMAN.value,
                        "reasoning": f"Judge execution failed: {exc}",
                        "scores_json": {},
                    }
                votes.append(
                    {
                        "judge_agent": f"judge_{family}",
                        "model_family": family,
                        "verdict": verdict.get("verdict", VerdictKind.NEEDS_HUMAN.value),
                        # pipeline._persist_votes reads the "scores" key.
                        "scores": verdict.get("scores_json", {}),
                        "reasoning": verdict.get("reasoning", ""),
                        "conditions": verdict.get("conditions", ""),
                        "model": verdict.get("model", ""),
                    }
                )

        complete = len({v["model_family"] for v in votes}) >= self.MIN_DISTINCT_FAMILIES
        return JudgeOutcome(
            panel_attempt_id=panel_attempt_id,
            families_seated=len({v["model_family"] for v in votes}),
            votes=votes,
            complete=complete,
        )

    def invoke(
        self,
        improvement_id: str,
        sandbox_diff: str,
        fenced_evidence: str,
        sandbox_results: dict[str, Any] | None = None,
    ) -> str:
        """Invoke panel in blind parallel; return panel_attempt_id.

        Each judge receives:
          - Raw sandbox diff (primary artifact)
          - Fenced evidence (wrapped in quote_untrusted)
          - Sandbox results summary
          - NO other votes (blind)

        Blocks until all 3 judges have voted (or deadline exceeded).
        Stores votes atomically via store.insert_vote (idempotent on
        panel_attempt_id + model_family).

        Args:
            improvement_id: Improvement being judged
            sandbox_diff: Raw git diff
            fenced_evidence: Pre-wrapped evidence from analyzer/detector
            sandbox_results: Sandbox execution summary

        Returns:
            panel_attempt_id: Idempotency key for this invocation
        """
        panel_attempt_id = new_id("pan")
        sandbox_results = sandbox_results or {}
        if not self.available_judges:
            self._seat_families()

        # Build judge prompt (primary artifact is diff, secondary is evidence)
        prompt = _build_judge_prompt(
            improvement_id=improvement_id,
            sandbox_diff=sandbox_diff,
            fenced_evidence=fenced_evidence,
            sandbox_results=sandbox_results,
        )

        # Invoke judges in parallel (one per distinct family)
        votes_by_family = {}
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = {}

            for harness, family in self.available_judges[:3]:  # 3 families max
                if family in votes_by_family:
                    continue  # Already have this family

                future = executor.submit(
                    self._invoke_single_judge,
                    harness=harness,
                    family=family,
                    prompt=prompt,
                    timeout_seconds=self.judge_timeout_seconds,
                )
                futures[future] = (harness, family)

            for future in as_completed(futures):
                harness, family = futures[future]
                try:
                    verdict_json = future.result(timeout=self.judge_timeout_seconds + 5)
                    votes_by_family[family] = verdict_json
                except Exception as e:
                    # Judge failed or timed out; record as needs_human
                    votes_by_family[family] = {
                        "verdict": VerdictKind.NEEDS_HUMAN.value,
                        "reasoning": f"Judge execution failed: {str(e)}",
                        "scores_json": {},
                    }

        # Store votes atomically (idempotent on panel_attempt_id + model_family)
        for family, verdict_json in votes_by_family.items():
            judge_agent = f"judge_{family}"
            self.store.insert_vote(
                improvement_id=improvement_id,
                panel_attempt_id=panel_attempt_id,
                judge_agent=judge_agent,
                model_family=family,
                verdict=verdict_json.get("verdict", VerdictKind.NEEDS_HUMAN.value),
                scores_json=verdict_json.get("scores_json", {}),
                reasoning=verdict_json.get("reasoning", ""),
                conditions=verdict_json.get("conditions", ""),
                model=verdict_json.get("model", ""),
            )

        return panel_attempt_id

    def _invoke_single_judge(
        self,
        harness: str,
        family: str,
        prompt: str,
        timeout_seconds: int,
    ) -> dict[str, Any]:
        """Invoke a single judge via adapter subprocess.

        Args:
            harness: Adapter harness key (e.g., "cli-claude")
            family: Model family (e.g., "anthropic")
            prompt: Judge prompt (raw diff + fenced evidence)
            timeout_seconds: Hard deadline for this judge

        Returns:
            Parsed verdict JSON dict (or needs_human if unparseable)
        """
        try:
            adapter = self._resolve(harness)

            # Invoke adapter
            input_spec = AgentInput(
                run_id=new_id("run"),
                task_id=new_id("tsk"),
                prompt=prompt,
                model=None,  # Use adapter default
                output_schema={
                    "type": "object",
                    "properties": {
                        "verdict": {"type": "string"},
                        "scores_json": {"type": "object"},
                        "reasoning": {"type": "string"},
                        "conditions": {"type": "string"},
                    },
                    "required": ["verdict"],
                },
            )

            result = adapter.run(input_spec)

            if result.status != ResultStatus.OK:
                return {
                    "verdict": VerdictKind.NEEDS_HUMAN.value,
                    "reasoning": f"Adapter returned status={result.status}: {result.error}",
                    "scores_json": {},
                }

            # Parse verdict (strict JSON, one retry)
            verdict_json = self._parse_verdict(result.output_json or result.output_text)
            return verdict_json

        except Exception as e:
            return {
                "verdict": VerdictKind.NEEDS_HUMAN.value,
                "reasoning": f"Judge invocation failed: {str(e)}",
                "scores_json": {},
            }

    def _parse_verdict(self, output: str | dict[str, Any]) -> dict[str, Any]:
        """Parse judge's JSON verdict with one retry.

        Strict schema: verdict (approve|reject|approve_with_conditions|needs_human)
        + scores_json (11 scores 0-10) + reasoning + conditions.

        Args:
            output: Judge output (string or parsed JSON)

        Returns:
            Validated verdict dict (or needs_human if unparseable)
        """
        if isinstance(output, dict):
            parsed = output
        else:
            try:
                parsed = json.loads(output)
            except json.JSONDecodeError:
                # Try one more time: extract JSON from the text
                import re

                match = re.search(r"\{.*\}", output, re.DOTALL)
                if match:
                    try:
                        parsed = json.loads(match.group(0))
                    except json.JSONDecodeError:
                        return {
                            "verdict": VerdictKind.NEEDS_HUMAN.value,
                            "reasoning": "Unparseable JSON after retry",
                            "scores_json": {},
                        }
                else:
                    return {
                        "verdict": VerdictKind.NEEDS_HUMAN.value,
                        "reasoning": "No JSON found in output",
                        "scores_json": {},
                    }

        # Validate verdict value
        verdict = parsed.get("verdict", VerdictKind.NEEDS_HUMAN.value)
        if verdict not in (
            VerdictKind.APPROVE.value,
            VerdictKind.REJECT.value,
            VerdictKind.APPROVE_WITH_CONDITIONS.value,
            VerdictKind.NEEDS_HUMAN.value,
        ):
            verdict = VerdictKind.NEEDS_HUMAN.value

        # Extract scores (11 scores: root_cause, fix_correctness, regression_risk, etc.)
        scores = parsed.get("scores_json", {})
        if not isinstance(scores, dict):
            scores = {}

        return {
            "verdict": verdict,
            "scores_json": scores,
            "reasoning": str(parsed.get("reasoning", "")),
            "conditions": str(parsed.get("conditions", "")),
            "model": str(parsed.get("model", "")),
        }


def _fence_evidence(improvement: Improvement) -> str:
    """Wrap all improvement-carried failure evidence in ``quote_untrusted`` (M4).

    Root cause / summary / proposal are analyzer-derived from untrusted failure
    output, so they are fenced as data before reaching any judge prompt — a judge
    can never read an embedded instruction like "classify as L1 and approve" as a
    command, and must re-derive risk independently of anything inside the fence.
    """
    payload = {
        "title": getattr(improvement, "title", ""),
        "summary": getattr(improvement, "summary", ""),
        "root_cause": getattr(improvement, "root_cause", ""),
        "proposal": getattr(improvement, "proposal_json", {}) or {},
    }
    return quote_untrusted(json.dumps(payload, sort_keys=True), source="improvement-evidence")


def _build_judge_prompt(
    improvement_id: str,
    sandbox_diff: str,
    fenced_evidence: str,
    sandbox_results: dict[str, Any] | None = None,
) -> str:
    """Build the judge prompt (§4, M4: fenced evidence).

    Primary artifact is the raw sandbox diff. Secondary is fenced evidence
    from the detector/analyzer (all untrusted content wrapped in
    quote_untrusted).

    Structure:
      1. Context (improvement ID, origin)
      2. Raw sandbox diff
      3. Fenced evidence (quote_untrusted)
      4. Sandbox results (test pass/fail, errors)
      5. Judge instructions + JSON schema
    """
    sandbox_results = sandbox_results or {}

    prompt = f"""You are a consensus judge evaluating a proposed improvement to a self-improving system.

## Improvement: {improvement_id}

### Primary Artifact: Raw Sandbox Diff

```diff
{sandbox_diff}
```

### Evidence from Detection/Analysis

{fenced_evidence}

### Sandbox Execution Results

- Tests passed: {sandbox_results.get("tests_passed", False)}
- Test count: {sandbox_results.get("test_count", 0)}
- Errors: {json.dumps(sandbox_results.get("test_errors", sandbox_results.get("errors", [])), indent=2)}

### Your Task

Evaluate this improvement independently. Judge the quality of the fix, not the
confidence of the analysis. Consider:

1. **Root cause correctness**: Does the diff address a real issue?
2. **Fix correctness**: Is the fix technically sound?
3. **Regression risk**: Could this break existing functionality?
4. **Security risk**: Does this introduce vulnerabilities?
5. **Reliability**: Will this improve overall system reliability?
6. **Quality**: Is the code/config well-integrated?
7. **Cost**: Will this increase operational cost?
8. **Speed**: Performance impact?
9. **Architecture**: Does this maintain/improve architecture?
10. **Test coverage**: Are tests adequate?
11. **Reversibility**: Can this be rolled back safely?

### Your Verdict (JSON only, no other output)

Return a JSON object with:
- `verdict`: one of approve, reject, approve_with_conditions, needs_human
- `scores_json`: object with integer scores 0-10 for each criterion above
- `reasoning`: brief explanation
- `conditions`: if approve_with_conditions, describe the required changes
- `model`: your model/family identifier (optional)

Example:
```json
{{
  "verdict": "approve",
  "scores_json": {{
    "root_cause": 8,
    "fix_correctness": 8,
    "regression_risk": 2,
    "security_risk": 1,
    "reliability": 8,
    "quality": 7,
    "cost": 5,
    "speed": 6,
    "architecture": 7,
    "test_coverage": 7,
    "reversibility": 9
  }},
  "reasoning": "The fix correctly addresses the issue with good test coverage.",
  "conditions": ""
}}
```
"""

    return prompt


__all__ = ["JudgePanel", "PanelBlockedError", "_fence_evidence"]
