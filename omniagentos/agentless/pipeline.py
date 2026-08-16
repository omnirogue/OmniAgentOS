"""Orchestrate the Agentless loop: localize -> sample -> apply -> verify -> select.

Compute-optimal sampling (Snell et al. 2408.03314) is the reason this loop samples
in small BATCHES with early stopping rather than a fixed best-of-N: as soon as a
batch produces a single passing candidate, further sampling buys nothing (the
verifier already found a winner), so we stop and save the compute. Sequential,
adaptive sampling like this systematically beats spending the same budget on one
giant batch up front.

Every generation failure (adapter exception, no diff extracted, apply failure) is
recorded as a normal (failing) ``VerifiedCandidate`` — never raised — so one bad
sample can't take down the whole run. This extends to the per-candidate VERIFY step
too: an unborn-HEAD, locked-index, or disk-full ``git worktree add`` (raised by
:mod:`omniagentos.agentless.patch`'s ``check=True`` scratch checkout) is caught and
recorded as a failing candidate, so ``run_agentless`` NEVER raises out to its caller.

``gen_timeout_s`` is a HARD wall on each batch: the generation ThreadPoolExecutor is
shut down with ``cancel_futures=True`` on timeout instead of being joined, so a truly
hung adapter thread may linger in the background but the pipeline still returns within
the wall (``gen_timeout_s + 5``) rather than blocking on ``__exit__``.
"""

from __future__ import annotations

import os
import tempfile
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FutureTimeoutError

from omniagentos.adapters.registry import resolve_adapter as _default_resolve_adapter
from omniagentos.agentless.contracts import (
    AgentlessResult,
    CandidatePatch,
    LocalizationResult,
    SampleSpec,
    VerifiedCandidate,
)
from omniagentos.agentless.localize import localize
from omniagentos.agentless.patch import apply_candidate, cleanup_checkout
from omniagentos.agentless.patch import extract_diff as _extract_diff
from omniagentos.agentless.prompts import build_patch_prompt
from omniagentos.agentless.select import select_candidate
from omniagentos.agentless.verify import run_tests
from omniagentos.contracts import AgentAdapter, AgentInput, BudgetSpec, HarnessType
from omniagentos.promptshape.segments import render

ResolveFn = Callable[[str], AgentAdapter]


def _load_focus_file_contents(repo_dir: str, focus_files: list[str]) -> dict[str, str]:
    contents: dict[str, str] = {}
    for rel_path in focus_files:
        abs_path = os.path.join(repo_dir, rel_path)
        try:
            with open(abs_path, encoding="utf-8", errors="ignore") as handle:
                contents[rel_path] = handle.read()
        except OSError:
            continue
    return contents


def _default_samples(n: int, adapter: str, model: str | None) -> list[SampleSpec]:
    return [SampleSpec(adapter=adapter, model=model) for _ in range(n)]


def _batched(items: list[SampleSpec], batch_size: int) -> list[list[tuple[int, SampleSpec]]]:
    indexed = list(enumerate(items))
    return [indexed[i : i + batch_size] for i in range(0, len(indexed), max(1, batch_size))]


def _generate_one(
    index: int,
    spec: SampleSpec,
    prompt: str,
    resolve: ResolveFn,
    gen_timeout_s: int,
) -> CandidatePatch:
    started = time.monotonic()
    try:
        adapter = resolve(spec.adapter)
        agent_input = AgentInput(
            run_id=f"agentless-{uuid.uuid4().hex[:8]}-{index}",
            task_id="agentless",
            prompt=prompt,
            model=spec.model,
            budget=BudgetSpec(wall_ms_max=gen_timeout_s * 1000),
            metadata=spec.metadata or {},
        )
        result = adapter.run(agent_input)
        elapsed = time.monotonic() - started
        usage = result.usage.model_dump() if result.usage is not None else {}
        raw_output = result.output_text or ""
        diff = _extract_diff(raw_output)
        return CandidatePatch(
            index=index,
            diff=diff,
            adapter=spec.adapter,
            model=spec.model,
            raw_output=raw_output,
            gen_seconds=elapsed,
            usage=usage,
        )
    except Exception as exc:  # noqa: BLE001 -- a bad sample must not kill the run
        elapsed = time.monotonic() - started
        return CandidatePatch(
            index=index,
            diff=None,
            adapter=spec.adapter,
            model=spec.model,
            raw_output=f"[adapter exception: {exc!r}]",
            gen_seconds=elapsed,
            usage={},
        )


def _verify_one(
    candidate: CandidatePatch,
    repo_dir: str,
    test_cmd: str,
    test_timeout_s: int,
    scratch_root: str | None,
) -> VerifiedCandidate:
    if not candidate.diff:
        return VerifiedCandidate(
            patch=candidate,
            applied=False,
            tests_passed=None,
            test_output_tail="no diff extracted from candidate output",
            returncode=None,
            verify_seconds=0.0,
        )

    workdir = tempfile.mkdtemp(prefix=f"agentless-{candidate.index}-", dir=scratch_root)
    try:
        applied = apply_candidate(repo_dir, candidate.diff, workdir)
        if not applied:
            return VerifiedCandidate(
                patch=candidate,
                applied=False,
                tests_passed=None,
                test_output_tail="diff did not apply (git apply and --3way both failed)",
                returncode=None,
                verify_seconds=0.0,
            )
        returncode, tail, seconds = run_tests(workdir, test_cmd, test_timeout_s)
        tests_passed = returncode == 0
        return VerifiedCandidate(
            patch=candidate,
            applied=True,
            tests_passed=tests_passed,
            test_output_tail=tail,
            returncode=returncode,
            verify_seconds=seconds,
        )
    finally:
        cleanup_checkout(repo_dir, workdir)


def run_agentless(
    task: str,
    repo_dir: str,
    test_cmd: str,
    *,
    samples: list[SampleSpec] | None = None,
    n: int = 4,
    adapter: str = "cli-claude",
    model: str | None = None,
    batch_size: int = 2,
    early_stop: bool = True,
    gen_timeout_s: int = 600,
    test_timeout_s: int = 600,
    resolve: ResolveFn | None = None,
    scratch_root: str | None = None,
) -> AgentlessResult:
    """Run the full Agentless loop and return the selected fix (if any).

    ``resolve`` defaults to :func:`omniagentos.adapters.registry.resolve_adapter`
    (wrapped to accept the plain ``str`` harness name from ``SampleSpec.adapter``);
    tests inject a stub here instead of driving a real CLI. ``samples`` overrides
    ``n``/``adapter``/``model`` with an explicit per-sample spec list.

    VERIFICATION BASELINE IS ``HEAD``: candidates are localized against the working
    dir but verified in scratch worktrees checked out from the COMMITTED ``HEAD`` (see
    :func:`omniagentos.agentless.patch._prepare_checkout`). A caller that then applies
    the winner back to the real working tree must ensure that tree is clean-at-``HEAD``,
    or the verify baseline and the apply target diverge (uncommitted edits are invisible
    to the verifier). ``AgentlessOrCheapRunner`` enforces this by delegating to the
    wrapped cheap runner whenever ``git status --porcelain`` is non-empty."""
    started = time.monotonic()
    repo_dir = os.path.abspath(repo_dir)

    def _resolve(name: str) -> AgentAdapter:
        return _default_resolve_adapter(HarnessType(name))

    resolve_fn: ResolveFn = resolve if resolve is not None else _resolve

    loc: LocalizationResult = localize(repo_dir, task)
    file_contents = _load_focus_file_contents(repo_dir, loc.focus_files)
    prompt_segments = build_patch_prompt(loc, file_contents, task)
    prompt = render(prompt_segments)

    sample_specs = samples if samples is not None else _default_samples(n, adapter, model)
    batches = _batched(sample_specs, batch_size)

    verified: list[VerifiedCandidate] = []
    for batch in batches:
        batch_verified: list[VerifiedCandidate] = []
        spec_by_index = dict(batch)
        # Manage the executor WITHOUT the context manager: ``__exit__`` joins hung
        # workers (wait=True), which would defeat the gen_timeout_s wall. On timeout
        # we shut down with cancel_futures=True instead, so the pipeline returns even
        # if an adapter thread is still stuck (it lingers harmlessly in the background).
        executor = ThreadPoolExecutor(max_workers=max(1, len(batch)))
        candidates_by_index: dict[int, CandidatePatch] = {}
        try:
            futures = {
                executor.submit(
                    _generate_one, index, spec, prompt, resolve_fn, gen_timeout_s
                ): index
                for index, spec in batch
            }
            try:
                for future in as_completed(futures, timeout=gen_timeout_s + 5):
                    candidate = future.result()
                    candidates_by_index[candidate.index] = candidate
            except FutureTimeoutError:
                pass  # unfinished futures below become explicit timeout candidates
            for index in futures.values():
                if index not in candidates_by_index:
                    spec = spec_by_index[index]
                    candidates_by_index[index] = CandidatePatch(
                        index=index,
                        diff=None,
                        adapter=spec.adapter,
                        model=spec.model,
                        raw_output=f"[generation exceeded {gen_timeout_s}s]",
                        gen_seconds=float(gen_timeout_s),
                        usage={},
                    )
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
        # Verify in submission order for deterministic output ordering. Any exception
        # from the scratch checkout (unborn HEAD, locked index, disk full) is caught
        # here so a single hostile-repo candidate never propagates out of the run.
        for index, _spec in batch:
            candidate = candidates_by_index[index]
            vc_started = time.monotonic()
            try:
                vc = _verify_one(candidate, repo_dir, test_cmd, test_timeout_s, scratch_root)
            except Exception as exc:  # noqa: BLE001 -- a scratch-checkout fault is a loss, not a crash
                vc = VerifiedCandidate(
                    patch=candidate,
                    applied=False,
                    tests_passed=None,
                    test_output_tail=f"scratch checkout failed: {exc}",
                    returncode=None,
                    verify_seconds=time.monotonic() - vc_started,
                )
            batch_verified.append(vc)
        verified.extend(batch_verified)

        if early_stop and any(vc.tests_passed for vc in batch_verified):
            break

    selected, reason = select_candidate(verified)
    wall_seconds = time.monotonic() - started
    return AgentlessResult(
        task=task,
        localization=loc,
        candidates=verified,
        selected=selected,
        selection_reason=reason,
        wall_seconds=wall_seconds,
    )
