# Package interfaces (FROZEN, Wave 0 revision 2 — adopts design finding D-ALT)

The runner and API depend on ONE stateful seam — `contracts.Store` (implemented by
p01 as `omniagentos.db.store.SqliteStore(db_path)`), plus the pure functions below.
Leaf packages are LIBRARIES, not services: no globals, no direct DB access, no IO
beyond what is stated. Exact signatures; packages implement them verbatim.

## p01 — omniagentos.db
```python
class SqliteStore:  # implements contracts.Store over contracts/schema.sql
    def __init__(self, db_path: str) -> None: ...       # applies pragmas per schema.sql header
def migrate(db_path: str) -> int                        # applies omniagentos/db/migrations/NNN_*.sql, returns version
```
Connection discipline: WAL, busy_timeout=5000, foreign_keys=ON, synchronous=NORMAL;
writers BEGIN IMMEDIATE; short transactions; one connection per Store instance,
safe for one process each (API and runner hold separate instances).

## p07 — omniagentos.policy (pure) + omniagentos.audit
```python
def load_policy(path: str = "configs/policy.yaml") -> PolicyConfig          # pydantic model, p07-defined
def evaluate_action(action_class: ActionClass, cfg: PolicyConfig) -> PolicyDecision
def validate_tools(tools_allowed: list[str], cfg: PolicyConfig) -> None    # raises PolicyError on unknown tool
def sandbox_for_tools(harness: HarnessType, tools_allowed: list[str], cfg: PolicyConfig) -> SandboxSpec
def audit(store: Store, actor: str, action: str, *, target_type: str = "", target_id: str = "",
          payload: dict | None = None, trace_id: str = "") -> int          # thin wrapper over store.insert_event(type=Events.AUDIT)
```
Tools→sandbox mapping (D-005; the B4 test asserts the actual subprocess flags):
| tools_allowed contains | SandboxSpec.level | cli-codex | cli-grok | cli-claude |
|---|---|---|---|---|
| neither shell nor file_write | read_only | `--sandbox read-only` | `--sandbox read-only` | plain `-p` (no permission grants; no --add-dir) |
| file_write and/or shell | workspace_write | `--sandbox workspace-write` | `--sandbox workspace` | `-p` + `--permission-mode acceptEdits` + `--add-dir <working_dir>` |
Empty allowlist = most restrictive (read_only). Enforcement is BOTH pre-validation
(validate_tools at enqueue, p03) AND sandbox-flag selection at execution (p04
receives the SandboxSpec inside AgentInput.metadata["sandbox"] set by the runner).

## p06 — omniagentos.budget + omniagentos.ledger (pure except ledger file append)
```python
def check(spec: BudgetSpec, used_wall_ms: int, used_tokens: int, used_cost_usd: float) -> BudgetDecision
def manifest_line(m: RunManifest) -> str                                   # one JSON line, sorted keys
def append_manifest(ledger_dir: str, m: RunManifest) -> str                # returns file path ledger/runs-YYYYMM.jsonl; creates dirs; IDEMPOTENT by m.run_id — if a line for run_id already exists in the target month file, returns its path WITHOUT appending (D-007 residual, crash-safe finalization)
def read_manifests(ledger_dir: str, *, run_id: str | None = None, limit: int = 100) -> list[RunManifest]   # newest-first
```
Manifest/receipt shape: `RunManifest.receipts` is `list[IdempotencyReceipt]`
(fields key/run_id/step_name/result_json/created_at/completed_at) — the SAME shape
returned by `Store.idem_for_run`, exposed by GET /api/runs/{id}, and typed as
`Receipt` in dashboard/src/lib/contracts.ts (DV-001). The distinct `contracts.Receipt`
(key/action/target/at/result_digest) is adapter-emitted and rides only on
AgentResult — do not confuse the two.
```python
```

## p05 — omniagentos.vault
```python
def render_run_note(run: dict, steps: list[dict], manifest_path: str, receipts: list[IdempotencyReceipt]) -> tuple[str, str]
    # returns (relpath "runs/<yyyy>/<mm>/<run-id>.md", full note content)
def write_note(vault_dir: str, relpath: str, content: str, *, autocommit: bool | None = None) -> str
    # preserves any existing "## Notes (human)" section; autocommit None → env flag
def render_benchmark_note(bench_id: str, manifests: list[RunManifest]) -> tuple[str, str]
def update_home(vault_dir: str, stats: dict) -> None                       # rewrites the omniagentos:status block only
def parse_frontmatter(content: str) -> VaultFrontmatter
```
Run-note required wikilinks (D-011): `[[<discipline-slug>]]` (its discipline note —
p05 ensures `disciplines/<slug>.md` exists, generating a stub on first reference)
and `[[Home]]`. Task titles appear as PLAIN TEXT, not wikilinks (no tasks/ folder
in H1).

## p04 — omniagentos.adapters
```python
def resolve_adapter(harness: HarnessType) -> AgentAdapter   # omniagentos/adapters/registry.py
```
Registry map (lazy imports so core tests don't require the harness extra):
mock → omniagentos.mock_adapter:MockAdapter ·
cli-claude/cli-codex/cli-grok/cli-kimi/cli-gemini/cli-qwen → p04's modules ·
mini-swe → omniagentos.harnesses.miniswe.adapter:MiniSweAdapter ·
openhands → omniagentos.harnesses.openhands.adapter:OpenHandsAdapter. Unknown →
KeyError with the known list. Harness packages (p10/p11) MUST expose exactly those
module:class paths.

## p11 — omniagentos.harnesses (shared helper, owned by p11)
```python
def env_hash() -> str    # omniagentos/harnesses/envhash.py — sha256 over python version + resolved harness-extra versions + platform
```

## Runner/API composition (who constructs what)
- API: `store = SqliteStore(default_db_path())`, `cfg = load_policy()`, uses ledger
  read API for GET /api/ledger.
- Runner: same constructors; calls policy/budget/ledger/vault functions per
  contracts/statemachine.md; adapters only via resolve_adapter.
- Tests: any Store fake satisfying the Protocol; p02 twins' restart/concurrency
  tests MUST also run against the real SqliteStore (D-001 requiredValidation) —
  p01 lands on main before p02 fan-out for exactly this reason.
