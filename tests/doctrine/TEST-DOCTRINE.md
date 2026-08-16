# Test doctrine (one page)

**Every behavioural claim needs a revert-test AND a named counterfeit.** A test that still passes with its fix reverted is **decoration** — report it as such; never count it.

## Helpers (executable, not prose)

| Helper | Module | What it proves |
|--------|--------|----------------|
| `revert_test` | `tests/doctrine/revert.py` | Mutate target → named test **must fail** (verbatim text kept) → restore → must pass. **Fails loudly** if still green under mutation. |
| `counterfeit_test` | `tests/doctrine/counterfeit.py` | Apply a realistic fake of the fix → named test **must fail**. Catches suites that bind to the wrong thing. |
| `assert_no_pytest_piped_to_tail` | `tests/doctrine/traps.py` | `pytest … \| tail` replaces pytest’s exit code. Banned. |
| `assert_assertion_count_not_dropped` | same | Modified test file must not lose asserts. |
| `assert_no_new_suppressions` | same | No new `# type: ignore` / `# noqa` in the claiming change. |

```python
from tests.doctrine import TextReplace, counterfeit_test, revert_test

revert_test(target=path, mutation=TextReplace(old=..., new=...), nodeid="tests/...::test_claim")
counterfeit_test(target=path, counterfeit=TextReplace(old=..., new=...), nodeid="tests/...::test_claim")
```

## Rules

1. **Revert + counterfeit** for every claim that authorises a merge or lane “done”.
2. **Decoration** (green under revert) is a finding, not evidence.
3. **Never** `pytest … | tail` / `| head` — use the process exit code.
4. **Never** `git stash` in worktrees (repo-wide; destroys other lanes).
5. Self-suite: helpers must detect their own no-op (`make test-doctrine`).

## Worked shapes (copy these)

| Claim | Revert | Counterfeit | Anchor |
|-------|--------|-------------|--------|
| Rate over empty denominator → `None` | `return 0.0` | `return 1.0` (other favourable constant) | `tracelab/metrics.py`, `pulse/aggregator.py` |
| Wide DAG → `worker_count >= 2` | root-layer only (`return 1`) | `max(worker_count, 2)` | allocation simulator / fan-out 10/10 miss |
| Wiring REACHABLE | delete real caller edge | grep-for-name (docstring hit) | AT-18 registry |

Fixture twins live under `tests/doctrine/_fixtures/` and are exercised in `test_examples_real_cases.py`.

## Run

```bash
make test-doctrine          # doctrine helpers + self-tests + examples only
# also collected by: make test
```

Evidence from one day without this: five lanes green with fix reverted; two suites 5/13 with production path broken; one gate disabled with 73 still green; fan-out counterfeit 10/10.
