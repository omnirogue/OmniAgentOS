# C1 Engine Consolidation — Engine & Caller Inventory (Phase 1)

This document provides an exhaustive inventory of the three legacy engines targeted for consolidation: Continuous Self-Improvement (CSI), Longhaul, and the V2 Self-Improving Reliability System.

Under the C1 Engine Consolidation mandate, Phase 1 enforces a **FREEZE-ONLY** scope: legacy engine directories are retained, deprecation shims and warnings have been added to their entrypoints (logging on import and on invocation), and no code is deleted on this branch. This document maps all callers and establishes a porting plan to transition live configurations to the **Swarm Scheduler** in Phase 2.

---

## 1. Continuous Self-Improvement (CSI) Engine

Continuous Self-Improvement (CSI) is a library-style system off by default in production configurations (`configs/self_improvement.yaml` contains `enabled: false`), where routines can propose automated changes but require human approval to merge.

### 1.1 Scope & Entrypoints
* **Directory:** `omniagentos/csi/`
* **Python Entrypoint:** `omniagentos/csi/__init__.py`, exposing `CsiEngine`, `run_routine`, and `run_self_learning`.
* **Execution Entrypoint:** `omniagentos/csi/__main__.py` / `python -m omniagentos.csi`
* **Frozen Write Verification:** `omniagentos/csi/frozen.py`, specifically `assert_writable()`, `is_frozen_path()`, and `reject_frozen_paths()`.

### 1.2 Exhaustive Caller Inventory (rg-based)
* **`scripts/csi-human-pipeline.sh`**:
  - *Context:* Interactive CLI helper script facilitating human operators to run, approve, implement, recover, and clean up CSI routine runs.
  - *Calls:*
    - `python -m omniagentos.csi run -r <routine_id>`
    - `python -m omniagentos.csi run-all`
    - `python -m omniagentos.csi approve --run-id <run_id>`
    - `python -m omniagentos.csi implement --run-id <run_id>`
* **`omniagentos/reliability/pipeline.py`**:
  - *Context:* The Reliability engine's main loop references the CSI frozen surfaces config dynamically to ensure that proposed reliability rollback/apply operations do not attempt to write to immutable/frozen paths.
  - *Calls:*
    - `from omniagentos.csi import frozen as csi_frozen` (invoked inside `l09_containment_ready()` gate checks)
* **`HANDOFF/L04-audit-stages.md`**:
  - *Context:* Documentation recording how L04 audit stages leverage `omniagentos.csi.observe` / `sweep_post_merge`.
* **`tests/csi/`**:
  - *Context:* Test coverage containing comprehensive unit tests checking config caching, frozen containment validation, approval cards, and routine runs.

---

## 2. Longhaul Engine

Longhaul is the long-horizon agentic coding lane designed to route and process planned coding tasks through isolated processes using dedicated provider adapters and cooldown/crash protection.

### 2.1 Scope & Entrypoints
* **Directory:** `omniagentos/longhaul/`
* **Python Entrypoint:** `omniagentos/longhaul/__init__.py`, exposing `LonghaulStore`, `LonghaulEngine`, `Category`, and `TaskSession`.
* **Execution Entrypoint:** `LonghaulEngine` in `omniagentos/longhaul/engine.py`.

### 2.2 Exhaustive Caller Inventory (rg-based)
* **`omniagentos/sessions/supervisor.py`**:
  - *Context:* The central supervisor managing background execution/sessions utilizes longhaul to load configuration, initialize its engine, and query the DAL store.
  - *Calls:*
    - `from omniagentos.longhaul.config import load_config`
    - `from omniagentos.longhaul.engine import LonghaulEngine`
    - `from omniagentos.longhaul.store import LonghaulStore`
* **`omniagentos/intake/service.py`**:
  - *Context:* Central intake and onboarding services instantiate `LonghaulStore` and run tasks through its engine lifecycle.
  - *Calls:*
    - `from omniagentos.longhaul.config import load_config`
    - `from omniagentos.longhaul.engine import LonghaulEngine`
    - `from omniagentos.longhaul.store import LonghaulStore`
* **`omniagentos/swarm/planner.py`**:
  - *Context:* The Swarm system's planning layer integrates with the longhaul category system to categorize incoming board tasks.
  - *Calls:*
    - `from omniagentos.longhaul.store import LonghaulStore`
* **`omniagentos/api/routes/categories.py`**:
  - *Context:* REST API routing exposes endpoints for managing longhaul categories and category cooldown slots.
  - *Calls:*
    - `from omniagentos.longhaul import LonghaulStore`
* **`omniagentos/api/routes/sessions.py`**:
  - *Context:* API endpoints for managing and inspecting active longhaul sessions, appending steering instructions, and fetching workbook trees.
  - *Calls:*
    - `from omniagentos.longhaul.prompts import steering_wrap`
    - `from omniagentos.longhaul.store import LonghaulStore`
* **`omniagentos/api/routes/intake.py`**:
  - *Context:* Main endpoint handler checking if conversations require steering or workbook reads.
  - *Calls:*
    - `from omniagentos.longhaul.steering import SteeringManager`
    - `from omniagentos.longhaul.workbook import read_workbook`
* **`omniagentos/swarm/spawn.py` & `omniagentos/swarm/provider_exec.py`**:
  - *Context:* Swarm's process spawn wrappers reuse the robust limit-parsing/terminal classification logic written for Longhaul.
  - *Calls:*
    - `from omniagentos.longhaul.limits import classify_limit_text, parse_reset_time`
    - `from omniagentos.longhaul.prompts import continuation_prompt`
* **`scripts/provider-sentinel/sentinel.py`**:
  - *Context:* Background monitor validating provider rate limits.
  - *Calls:*
    - `from omniagentos.longhaul.limits import classify_limit_text`
* **`omniagentos/intake/fastlane.py`**:
  - *Context:* Bypasses longhaul's planning delay using static configurations when fastlane triggers.
  - *Calls:*
    - `from omniagentos.longhaul.config import load_config`

### 2.3 Live Configurations (Migration 072)
* **Migration 072** (`omniagentos/db/migrations/072_longhaul_provider_harnesses.sql`):
  - *Detail:* Alters the legacy 043 schema's `task_sessions` check constraint. It widens the allowed `harness` types to explicitly include `'cli-claude', 'cli-codex', 'cli-grok', 'cli-gemini', 'cli-kimi'` so that the Longhaul engine can durably route tasks across diverse LLM providers.
* **`configs/longhaul.yaml`**:
  - *Detail:* Live static fallback configurations, quality/speed/cost ranking weights, and review/prep gates:
    - fallback harnesses for Gemini, Codex, Claude, Grok, and Kimi.
    - `weights: { quality: 0.6, speed: 0.25, cost: 0.15 }`
    - review harness config: `review: { enabled: true, harness: cli-codex, deny_respawns: 2 }`

---

## 3. Self-Improving Reliability Engine

The V2 Self-Improving Reliability System automatically detects failure classes, designs/sandboxes proposed corrections, judges them via an LLM consensus panel, and applies/monitors the changes in git worktrees.

### 3.1 Scope & Entrypoints
* **Directory:** `omniagentos/reliability/`
* **Python Entrypoint:** `omniagentos/reliability/__init__.py`, exposing submodules `taxonomy`, `contracts`, and `store`.
* **Execution Entrypoint:** `omniagentos/reliability/cli.py` / `python -m omniagentos.reliability`
* **Pipeline Engine:** `ImprovementPipeline` in `omniagentos/reliability/pipeline.py`.

### 3.2 Exhaustive Caller Inventory (rg-based)
* **`scripts/reliability/` plists**:
  - *Context:* macOS system launchdaemons periodically trigger scheduled reliability tasks.
  - *Calls (via template/install.sh):*
    - `python -m omniagentos.reliability watch --once`
    - `python -m omniagentos.reliability audit --once`
    - `python -m omniagentos.reliability daily --once`
    - `python -m omniagentos.reliability weekly --once`
* **`omniagentos/api/routes/reliability.py`**:
  - *Context:* REST endpoints enabling administrative reads and manual resolution of reliability events.
  - *Calls:*
    - Spawns `python -m omniagentos.reliability audit --once --kind <kind>` on demand.
* **`omniagentos/api/routes/improvements.py`**:
  - *Context:* Exposes the status of reliability improvements, and spawns detached workers to apply or rollback specific corrections.
  - *Calls:*
    - Spawns `python -m omniagentos.reliability apply --improvement <id>` or `python -m omniagentos.reliability rollback --improvement <id>`.
* **`omniagentos/company/org.py` & `omniagentos/company/cto.py`**:
  - *Context:* The corporate decision/hierarchy models consume reliability contracts to check team assignments and authorize resource proposals.
  - *Calls:*
    - `from omniagentos.reliability.contracts import ReliabilityStore`

---

## 4. Phase 2 Consolidation & Swarm Scheduler Porting Plan

To transition fully from Phase 1 (Freeze) to Phase 2 (Consolidation) on future branches, the active configurations and behaviors must be ported cleanly into **Swarm Scheduler** configurations.

### 4.1 Porting the CSI Gated Approval
* **Current Behavior:** The CSI engine halts on `global_halt: true` and blocks any direct write to frozen surfaces (`omniagentos/csi/frozen.py`).
* **Swarm Transition:** Port frozen surface write block policies to the Swarm executor sandbox hooks (via standard policies defined in `configs/policy.yaml`). Swarm can run planning-agent routines with human-in-the-loop approvals handled through the Swarm/Dashboard interactions API.

### 4.2 Porting the 072 Longhaul Provider-Harness Configurations
* **Current Behavior:** `configs/longhaul.yaml` manages fallback worker order, ranking weights (quality, speed, cost), and review gates for specific coding tasks.
* **Swarm Transition:**
  - Port fallback models and provider support (Gemini, Grok, Kimi, Claude) into `configs/modelintel.yaml` and `configs/parallelism.yaml`.
  - Port quality/speed/cost ranking criteria to the central Cognitive Budget Manager (CBM) and model intelligence selectors.
  - Port review/prep phases into standard multi-agent swarm configurations (`configs/formations.yaml` and swarm routing templates).

### 4.3 Porting Reliability Fail-closed Gates
* **Current Behavior:** Reliability pipeline gates (L03 lease fencing, L09 surface containment checks) run inside `assess_pipeline_activation()`.
* **Swarm Transition:** Integrate lease checks directly into the Swarm Scheduler's transaction management and executor environment validation. Move fail-closed assertion logic into the Swarm Toolplane to avoid bypass risks.
