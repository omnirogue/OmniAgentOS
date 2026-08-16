"""Extract structured, untrusted source content into knowledge candidates."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import ValidationError

from omniagentos.adapters.claude import ClaudeAdapter
from omniagentos.contracts import AgentInput, BudgetSpec, ResultStatus
from omniagentos.knowledge.config import extract_model
from omniagentos.knowledge.contracts import (
    CandidateEdge,
    CandidateEntity,
    CandidateFact,
    EdgeType,
    ExtractionResult,
)
from omniagentos.knowledge.prompts import get_extraction_prompt

logger = logging.getLogger(__name__)


def _coerce_edge_type(value: object) -> EdgeType:
    """Return a member of the closed edge enum, accepting harmless spellings."""
    text = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    return EdgeType(text)


def _parse_extraction(payload: object) -> ExtractionResult:
    """Validate an extraction payload, logging and dropping malformed members."""
    if not isinstance(payload, Mapping):
        logger.warning("Extraction response was not a JSON object")
        return ExtractionResult()

    facts: list[CandidateFact] = []
    entities: list[CandidateEntity] = []
    edges: list[CandidateEdge] = []
    for item in payload.get("facts", []):
        try:
            facts.append(CandidateFact.model_validate(item))
        except ValidationError as exc:
            logger.warning("Skipping malformed extracted fact: %s", exc)
    for item in payload.get("entities", []):
        try:
            entities.append(CandidateEntity.model_validate(item))
        except ValidationError as exc:
            logger.warning("Skipping malformed extracted entity: %s", exc)
    for item in payload.get("edges", []):
        if not isinstance(item, Mapping):
            logger.warning("Skipping malformed extracted edge: expected object")
            continue
        try:
            edges.append(
                CandidateEdge(
                    src_statement_idx=int(item["src_statement_idx"]),
                    dst_statement_idx=int(item["dst_statement_idx"]),
                    edge_type=_coerce_edge_type(item.get("edge_type", "")),
                )
            )
        except (KeyError, TypeError, ValueError, ValidationError) as exc:
            logger.warning("Skipping malformed extracted edge: %s", exc)
    return ExtractionResult(facts=facts, entities=entities, edges=edges)


def _decode_response(stdout: str) -> object:
    """Decode Claude's JSON envelope and its JSON extraction result."""
    stripped = stdout.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*|\s*```$", "", stripped).strip()
    decoded = json.loads(stripped)
    if isinstance(decoded, Mapping) and isinstance(decoded.get("result"), str):
        return _decode_response(decoded["result"])
    return decoded


class LLMExtractor:
    """Extraction implementation backed by the local ``cli-claude`` adapter."""

    def __init__(self, *, adapter: ClaudeAdapter | None = None, prompt_version: str = "v1") -> None:
        self._adapter = adapter or ClaudeAdapter()
        self._prompt_version = prompt_version

    def extract(self, content: str, *, discipline: str | None = None) -> ExtractionResult:
        """Extract facts, entities, and edges from untrusted episode content."""
        response = self._adapter.run(
            AgentInput(
                run_id="knowledge-extract",
                task_id="knowledge-extract",
                prompt=get_extraction_prompt(content, self._prompt_version),
                model=extract_model(),
                output_schema={"type": "object", "required": ["facts", "entities", "edges"]},
                budget=BudgetSpec(max_turns=1),
                metadata={"discipline": discipline, "sandbox": {"level": "read_only"}},
            )
        )
        if response.status is not ResultStatus.OK:
            logger.warning("Claude extraction failed: %s", response.error or response.status)
            return ExtractionResult()
        if response.output_json is not None:
            return _parse_extraction(response.output_json)
        try:
            return _parse_extraction(_decode_response(response.output_text))
        except json.JSONDecodeError as exc:
            logger.warning("Claude extraction returned invalid JSON: %s", exc)
            return ExtractionResult()


class FixtureExtractor:
    """Deterministically parse compact ``FACT:``, ``ENTITY:``, and ``EDGE:`` fixtures."""

    def __init__(self, result: ExtractionResult | None = None) -> None:
        self._result = result

    def extract(self, content: str, *, discipline: str | None = None) -> ExtractionResult:
        """Return the same extraction result for every identical fixture string."""
        del discipline
        if self._result is not None:
            return self._result.model_copy(deep=True)
        facts: list[CandidateFact] = []
        entities: list[CandidateEntity] = []
        edges: list[CandidateEdge] = []
        for raw_line in content.splitlines():
            line = raw_line.strip()
            if line.startswith("FACT:"):
                statement, separator, entity_text = line[5:].strip().partition("|")
                names = (
                    [name.strip() for name in entity_text.split(",") if name.strip()]
                    if separator
                    else []
                )
                if statement:
                    facts.append(CandidateFact(statement=statement, entities=names))
                else:
                    logger.warning("Skipping fixture fact without a statement")
            elif line.startswith("ENTITY:"):
                parts = [part.strip() for part in line[7:].split("|")]
                if parts and parts[0]:
                    entities.append(
                        CandidateEntity(
                            name=parts[0],
                            kind=parts[1] if len(parts) > 1 and parts[1] else "concept",
                            summary=parts[2] if len(parts) > 2 and parts[2] else None,
                        )
                    )
                else:
                    logger.warning("Skipping fixture entity without a name")
            elif line.startswith("EDGE:"):
                match = re.fullmatch(r"EDGE:\s*(\d+)\s*(?:,|->)\s*(\d+)\s*(?:,|:|\|)\s*(.+)", line)
                if not match:
                    logger.warning("Skipping malformed fixture edge: %s", line)
                    continue
                try:
                    edges.append(
                        CandidateEdge(
                            src_statement_idx=int(match.group(1)),
                            dst_statement_idx=int(match.group(2)),
                            edge_type=_coerce_edge_type(match.group(3)),
                        )
                    )
                except (ValueError, ValidationError) as exc:
                    logger.warning("Skipping malformed fixture edge: %s", exc)
        return ExtractionResult(facts=facts, entities=entities, edges=edges)


class DeterministicReflectionExtractor:
    """Generate a stable, LLM-free extraction for a workflow run reflection."""

    def __init__(
        self,
        run_row: Mapping[str, Any] | None = None,
        steps: Sequence[Mapping[str, Any]] | None = None,
    ) -> None:
        """Bind optional run data used by :meth:`extract`."""
        self._run_row = run_row
        self._steps = list(steps or [])

    def extract(self, content: str, *, discipline: str | None = None) -> ExtractionResult:
        """Extract one outcome fact and, when present, one lesson fact."""
        del discipline
        if self._run_row is None:
            return ExtractionResult(facts=[CandidateFact(statement=content)] if content else [])
        title = str(self._run_row.get("task_title") or self._run_row.get("id") or "unknown")
        state = str(self._run_row.get("state") or "unknown")
        lesson = _lesson_text(self._steps)
        facts = [CandidateFact(statement=f"Task '{title}' ended with outcome {state}.")]
        if lesson:
            facts.append(CandidateFact(statement=f"Lesson from task '{title}': {lesson}"))
        return ExtractionResult(facts=facts)

    def reflection_content(self) -> str:
        """Render the stable run-reflection text consumed by the ingest episode."""
        run_row = self._run_row or {}
        title = str(run_row.get("task_title") or run_row.get("id") or "unknown")
        state = str(run_row.get("state") or "unknown")
        completed_count = sum(step.get("status") == "completed" for step in self._steps)
        return (
            f"Executed task '{title}' with outcome {state}. {len(self._steps)} steps; "
            f"completed={completed_count}. Lessons: {_lesson_text(self._steps)}"
        )


def _lesson_text(steps: Sequence[Mapping[str, Any]]) -> str:
    """Derive a concise lesson from completed-step result payloads."""
    lessons: list[str] = []
    for step in steps:
        if step.get("status") != "completed":
            continue
        result = step.get("result_json")
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except json.JSONDecodeError:
                pass
        if isinstance(result, Mapping):
            value = result.get("lesson") or result.get("summary") or result.get("result")
            if value:
                lessons.append(str(value))
        elif result:
            lessons.append(str(result))
    return " ".join(lessons) if lessons else "No explicit lesson recorded."
