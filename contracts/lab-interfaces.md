# H2 lab interfaces (FROZEN, Wave 0)

Per-package signatures for the self-improvement lab. Packages implement these verbatim;
they depend on `omniagentos/lab/contracts.py` + H1's `omniagentos/contracts.py` (Store)
+ H1 runner/ledger/vault/bench. Reward-hacking invariants are BINDING (blueprint §11.7).

## L01-domain — omniagentos/lab/db
```python
class LabStore:  # composes/extends the H1 SqliteStore connection (same db, migration 003)
    def __init__(self, db_path: str) -> None: ...   # ensure_migrated() covers 003
    # surfaces + champions
    def create_surface(self, s: Surface) -> None: ...
    def get_surface(self, surface_id: str) -> dict | None: ...
    def list_surfaces(self, discipline: str, kind: str | None = None) -> list[dict]: ...
    def set_surface_status(self, surface_id: str, status: str) -> None: ...
    def get_champion(self, discipline: str, kind: str) -> dict | None: ...
    def set_champion(self, entry: ChampionEntry) -> None: ...            # + append champion_history
    def champion_history(self, discipline: str, kind: str) -> list[dict]: ...
    # experiments + eval
    def create_experiment(self, e: Experiment) -> None: ...
    def update_experiment(self, exp_id: str, fields: dict) -> None: ...
    def get_experiment(self, exp_id: str) -> dict | None: ...
    def list_experiments(self, discipline: str | None = None, status: str | None = None, limit: int = 100) -> list[dict]: ...
    def create_eval_suite(self, s: EvalSuite) -> None: ...
    def add_eval_case(self, c: EvalCase) -> None: ...
    def get_eval_suite(self, suite_id: str) -> dict | None: ...
    def record_eval_result(self, r: EvalResult) -> None: ...
    def eval_results(self, exp_id: str) -> list[dict]: ...
    def record_judge(self, j: JudgeRecord) -> None: ...
    # tournament + elo + playbook + leaderboard
    def create_tournament(self, t: Tournament) -> None: ...
    def update_tournament(self, tid: str, fields: dict) -> None: ...
    def record_match(self, m: MatchResult) -> None: ...
    def get_elo(self, subject: str, config_id: str) -> dict | None: ...
    def upsert_elo(self, e: Elo) -> None: ...
    def leaderboard(self, subject: str) -> list[dict]: ...        # ordered by rank
    def upsert_leaderboard_entry(self, row: LeaderboardEntry) -> None: ...
    def add_playbook_entry(self, p: PlaybookEntry) -> None: ...
    def list_playbook(self, discipline: str | None = None) -> list[dict]: ...
    # PROTECTED — L01 exposes candidate-safe eval cases; held-out expected NEVER here
    def candidate_cases(self, suite_id: str, split: str) -> list[CandidateEvalCase]: ...  # drops expected
```
**BINDING:** `candidate_cases()` is the ONLY store method any candidate/challenger-facing
code may call to fetch eval inputs. Fetching `eval_cases.expected_json` for split='held_out'
is done ONLY inside L02-eval's scoring path (never returned to a candidate, API, or note).

## L02-eval — omniagentos/lab/eval  (COMPETE; the reward-hacking crux)
STRUCTURAL held-out isolation (design review HD-001/HD-ALT/HD-009), not context-stripping:
- Held-out `expected` lives ONLY in a **protected store** `var/eval_protected.db` (env
  `OMNIAGENTOS_EVAL_PROTECTED`, default `<repo var>/eval_protected.db`) — a SEPARATE file
  the L02 grader owns. It is NEVER in the shared `var/omniagentos.db`, a vault note, the
  ledger, or any API response. The compete's #1 judging criterion is "can a candidate reach
  expected via ANY path — filesystem, DB file, env, vault — not just the prompt?"
- Grading is **out-of-process**: the grader receives candidate OUTPUTS + case IDs and returns
  scores/JudgeRecords; expected never enters the campaign/challenger/judge/curator address space.
```python
class ProtectedGrader:
    """Owns var/eval_protected.db. The ONLY component that reads held-out expected."""
    def __init__(self, protected_path: str | None = None) -> None: ...
    def put_expected(self, case_id: str, expected: dict) -> None: ...     # ingest (admin/seed only)
    def score_outputs(self, suite_id: str, split: str, arm: str,
                      outputs: dict[str, dict]) -> EvalResult: ...          # deterministic vs expected
    def audit_metric_jump(self, exp_id: str, champ: EvalResult, chal: EvalResult) -> list[str]: ...

class ProtectedEvaluator:
    """Composes ProtectedGrader (deterministic) + blind judging. Given ONLY outputs."""
    def __init__(self, store: LabStore, grader: ProtectedGrader) -> None: ...
    def run_deterministic(self, suite_id, split, arm, outputs) -> EvalResult: ...   # delegates to grader
    def judge_blind(self, suite_id, split, pairs: list[BlindPair], judges: list[str]) -> list[JudgeRecord]: ...
    def score_experiment(self, exp_id: str, dev_only: bool) -> Scorecard: ...       # dev first, held_out gated
# BlindPair = {case_id, blind_token, output} ONLY. blind_token is CRYPTO-RANDOM per case×arm
# (HD-015), unlinkable to arm; presentation order randomized with a recorded seed; the
# arm↔blind_token map is held by the evaluator and written to JudgeRecord AFTER judging.
```
**BINDING env-scrub (HD-001):** any subprocess that runs a challenger/judge/curator agent is
launched with an env that DOES NOT contain `OMNIAGENTOS_EVAL_PROTECTED` and (for the grading
boundary) is denied read of the protected file — L04/L05/L06 pass a scrubbed env to the H1
adapter/executor. score_experiment computes audit_flags → any non-empty forces HUMAN_REVIEW.

## L03-surfaces — omniagentos/lab/surfaces
```python
def version_prompt(store, discipline, role, content, *, parent_version=None) -> Surface: ...   # writes vault/prompts/<role>/vNN.md + Surface row
def version_genome(store, discipline, genome: dict, *, parent_version=None) -> Surface: ...     # writes configs/genomes/<id>.json + Surface row (improveswarm genome schema)
def load_surface_content(surface: dict) -> str | dict: ...
def seed_champion(store, discipline, kind, surface_id) -> ChampionEntry: ...   # champion #0 (rollback_to None)
def promote(store, exp_id: str, challenger_surface_id: str, *, human_decided_by: str | None = None) -> ChampionEntry: ...
def rollback(store, discipline, kind) -> ChampionEntry: ...
```
promote() (HD-003/HD-006/HD-011): REFUSES unless — the challenger passed its experiment's
gate; the surface does NOT require human unless `human_decided_by` is a real non-runner
identity; it sets `rollback_to` to the OUTGOING champion's surface_id and ARCHIVES that
surface immutably (asserts it exists); and it uses a compare-and-swap on champions.cas_version
so a concurrent curator/campaign cannot lose the known-good pointer. Genome schema is FROZEN
in `omniagentos/lab/contracts.py` (GenomeSpec), not a later file; L03 writes each genome to
`configs/genomes/<surface_id>.json` and validates it against GenomeSpec.

## L04x-executor — omniagentos/lab/executor  (design review HD-002/HD-007; NEW)
The missing runtime that runs BOTH a prompt surface and a multi-agent orchestration genome
over eval cases. Frozen so L04 (campaign) and L05 (tournament) both call it identically.
```python
def run_surface_over_cases(
    store, surface: dict, suite_id: str, split: str, budgets: Budgets, *,
    env_scrub: bool = True, dry_run: bool = False,
) -> dict[str, Any]:
    """Run one surface (PROMPT or ORCHESTRATION_GENOME) over candidate_cases(suite,split).
    Returns {"outputs": {case_id: {"text":..,"json":..}}, "manifests": [run_id,...]}.
    - PROMPT surface → the prompt is the system prompt for a single agent step per case.
    - ORCHESTRATION_GENOME surface → GenomeSpec is executed: roles resolved to adapters,
      flow stages run in order (generate/review/judge/iterate/synthesize), blind judging
      inside the genome where flow says so; the final synthesized output is the case output.
    Runs through the H1 runner (ledger/manifest provenance) with env_scrub so the child
    process cannot open the protected store. dry_run → mock adapter, offline."""

def execute_genome(genome: GenomeSpec, case_input: dict, budgets: Budgets, *, dry_run=False) -> dict: ...
```
`candidate_cases()` (L01) is the ONLY input source; there is ONE eval-input source of truth
(eval_cases), NOT bench task files — the bench keeps its B0/B1 role, the lab uses this executor.

## L04-campaign — omniagentos/lab/campaign
```python
def run_experiment(store, evaluator: ProtectedEvaluator, exp_id: str, *, dry_run=False) -> Disposition: ...
    # snapshot hashes → run champion baseline + challenger via H1 runner/bench on candidate_cases(dev)
    # → deterministic + blind judge → replicate → held_out (gated) → Scorecard+utility → disposition
def propose_experiments(store, discipline: str, policy_mix: dict | None = None) -> list[Experiment]: ...  # explore/exploit 60/25/10/5, from failures + ledger
def stall_check(store, discipline: str) -> bool: ...   # no challenger beats champion after N valid trials
```
Disposition rules (§11.4/11.5 + design review HD-003/HD-005/HD-010): PROMOTE only if ALL of —
primary_delta ≥ primary_delta_min; no safety/hard-constraint regression; reproducible across
replicates; utility ≥ min_utility AND complexity_delta ≤ max_complexity_delta; a valid
rollback target exists; AND the surface does NOT require human (SURFACE_REQUIRES_HUMAN or
safety_relevant) — those always route to HUMAN_REVIEW even when every numeric gate passes.
A NON-EMPTY audit_flags (metric-jump) forces HUMAN_REVIEW regardless of the numbers. Else
REJECT (valid, insufficient) / INVALID (crash/contaminated/over-budget). Challengers run
through the H1 runner + L04x executor with the SAME budgets and env_scrub; they get
candidate_cases only. B0/B1 re-proof: a champion that fails to beat B1 net of penalties is
flagged 'unjustified' → feeds the 10% simplify budget in propose_experiments.

## L05-tournament — omniagentos/lab/tournament
```python
def run_tournament(store, evaluator, subject, discipline, config_ids: list[str], arena_task: dict, *, dry_run=False) -> Tournament: ...
    # each config runs the SAME fair arena_task (hash-pinned); blind cross-lineage judge panel scores pairs; elo_update per match; winner set
def mutate_single_trait(genome: dict) -> list[dict]: ...   # single-variable challengers (§11.6 ablation)
def accumulate_playbook(store, tournament_id: str) -> list[PlaybookEntry]: ...  # validated traits from decisive wins
```

## L06-curator — omniagentos/lab/curator
```python
def curate(store, ledger_dir, vault_dir, *, dry_run=False) -> dict: ...
    # reads experiment ledger + run ledger + tournament/elo + relevant vault files → recomputes
    # leaderboard rows (top orchestrations per subject, by elo) + judge-notes digest + playbook →
    # writes vault leaderboard/log-book + playbook notes (via L08). Returns a summary dict.
def curation_prompt(context: dict) -> str: ...   # the Sonnet-high curator agent prompt (reads logs, distills judge notes)
```
**BINDING (design review HD-006):** the curator NEVER writes the champions table directly.
If curation surfaces a config that warrants promotion, it may only RECOMMEND — actual
promotion goes through `surfaces.promote()` with the identical §11.4 gate + human-review for
SURFACE_REQUIRES_HUMAN/safety_relevant. The curator agent runs with a SCRUBBED env (no
protected path) and reads only sanitized data (no held-out expected). The curator PROMPT is
NOT itself a mutable surface (it cannot be optimized).
The 2x-daily loop: `scripts/curator/run.sh` (launchd, 2×/day) → `python -m omniagentos.lab.curator`
drives a Sonnet-high agent with curation_prompt over the ledgers/files, then persists notes.

## L07-labapi — omniagentos/lab/api  (routes in contracts/lab-api.md)
Mounts on the H1 FastAPI app. BINDING: no route returns held_out `expected` (a test asserts it).

## L08-labvault — omniagentos/lab/vault
```python
def render_experiment_note(exp: dict, results: list[dict], scorecard: dict) -> tuple[str, str]: ...
def render_tournament_note(tnm: dict, matches: list[dict], elo: list[dict]) -> tuple[str, str]: ...
def render_leaderboard_note(subject: str, rows: list[dict]) -> tuple[str, str]: ...   # the human-readable log-book
def render_playbook_note(discipline: str, entries: list[dict]) -> tuple[str, str]: ...
def render_prompt_note(surface: dict, content: str) -> tuple[str, str]: ...
```
All reuse H1's vault write_note (confined + frontmatter). Notes wikilink experiments↔
tournaments↔surfaces↔leaderboard↔playbook so the vault is a navigable graph.

## L09-collab — omniagentos/collab (agent board + messaging + conversation log)
```python
class CollabStore:  # composes the H1 SqliteStore connection (migration 004 auto-applied)
    def __init__(self, db_path: str) -> None: ...
    # agent registry
    def register_agent(self, a: Agent) -> None: ...          # UNIQUE(name) upsert
    def list_agents(self) -> list[dict]: ...
    def set_agent_status(self, agent_id: str, status: str) -> None: ...
    # self-assignable board
    def create_board_task(self, t: BoardTask) -> None: ...
    def list_board_tasks(self, status: str | None = None, expertise: list[str] | None = None) -> list[dict]: ...
    def open_tasks_for(self, agent_expertise: list[str]) -> list[dict]: ...   # OPEN tasks the agent may claim (can_claim)
    def claim_task(self, task_id: str, agent_id: str, expect_version: int) -> bool: ...  # CAS: only if OPEN & version matches; exactly one agent wins
    def update_board_task(self, task_id: str, fields: dict) -> None: ...
    # messaging + conversation log
    def create_channel(self, c: Channel) -> None: ...        # DIRECT dedupes by member pair
    def add_member(self, channel_id: str, agent_id: str) -> None: ...
    def list_channels(self, agent_id: str | None = None) -> list[dict]: ...
    def post_message(self, m: Message) -> None: ...
    def list_messages(self, channel_id: str, limit: int = 200) -> list[dict]: ...
    def search_messages(self, q: str, limit: int = 100) -> list[dict]: ...
```
BINDING: `claim_task` is a compare-and-swap (only OPEN + matching claim_version wins) so two
agents racing for the same board task cannot both get it — mirrors the champion CAS.
`open_tasks_for` uses collab.contracts.can_claim (expertise overlap). Every message is
persisted; the curator/L08 renders per-channel conversation-log notes to the vault
(human-readable). Agents self-assign by: list open_tasks_for(my expertise) → claim_task.

## API (collab, mounted on H1 app, prefix /api/collab)
GET /agents · POST /agents · GET /board?status=&expertise= · POST /board · POST /board/{id}/claim
{agent_id} · PATCH /board/{id} · GET /channels?agent= · POST /channels · GET /channels/{id}/messages
· POST /channels/{id}/messages · GET /messages/search?q= . SSE via CollabEvents. No held-out
data ever crosses here (collab is orthogonal to eval).
