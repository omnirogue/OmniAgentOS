# Requirement → Test Traceability Matrix

**Product:** OmniAgentOS  
**Updated:** 2026-07-25  
**Scope:** Graph Runtime V2, CBM, Multidim org/taxonomy, Metacognition, multi-provider swarm

| Requirement ID | Requirement | Automated tests |
|---|---|---|
| CBM-01 | Initial fast-model allocation | `tests/cbm/test_cbm.py`, `tests/cbm/test_cbm_progressive_escalation.py::test_initial_fast_model_allocation_fields` |
| CBM-02 | Reasoning-effort selection | `test_reasoning_effort_and_candidate_count_by_rung` |
| CBM-03 | Context allocation | `test_initial_fast_model_allocation_fields` (`context_mode`) |
| CBM-04 | Candidate-count allocation | `test_reasoning_effort_and_candidate_count_by_rung` |
| CBM-05 | Verifier reservation / rung | `test_verifier_rung_escalates_with_ladder`, `test_verification_stage_mechanical_first` |
| CBM-06 | Runtime expansion (escalate) | `test_model_family_escalation_path_material` |
| CBM-07 | Runtime contraction | `test_redundant_candidates_cancelled_on_contract`, `decide_gate` contract |
| CBM-08 | Model-family escalation | `test_model_family_escalation_path_material` |
| CBM-09 | Reasoning-level escalation | same + rung 1→2 effort low→high |
| CBM-10 | Specialist / expert swarm | `test_specialist_and_expert_swarm_and_break_glass` |
| CBM-11 | ETAR-based routing | `test_etar_recommend_rung_adjusts_when_quality_low` |
| CBM-12 | Cancel redundant candidates | `test_redundant_candidates_cancelled_on_contract` |
| CBM-13 | Max rung / attempt limits | `test_cannot_exceed_max_rung`, `test_decide_gate_unknown_at_break_glass` |
| CBM-14 | No-progress / repeated-failure | escalate `repeated_failure` forces diverse |
| CBM-15 | Quality-threshold enforcement | `test_decide_gate_escalates_on_failure_stops_at_quality` |
| CBM-16 | No candidate passes → UNKNOWN | `test_decide_gate_unknown_at_break_glass` |
| CBM-17 | Gate pass stops immediately | `test_decide_gate_contracts_on_accepted_quality` |
| META-01 | Progress detection | `tests/metacognition/test_metacognition_control.py::test_progress_detection_improving` |
| META-02 | Stall detection | `test_stall_and_no_progress_refuses_continue` |
| META-03 | Repetition detection | `test_repetition_triggers_switch` |
| META-04 | Confidence changes | `test_confidence_changes_with_progress` |
| META-05 | Strategy switching | `test_strategy_switch_and_replan_path` + phases |
| META-06 | Replanning / stop | `test_stall_*`, `test_criteria_met_stops`, `test_stop_on_exhausted_strategy_age` |
| META-07 | Escalation / prune fan-out | `test_fanout_without_verifier_prunes` |
| META-08 | Failed memory not promoted | `test_failed_memory_candidates_not_promoted` |
| META-09 | Executor/evaluator/promoter separation | `test_executor_evaluator_promoter_separation` |
| META-10 | Artifact hash + provenance | `test_artifact_provenance_integrity_hash` |
| META-11 | Checkpoint recovery | `test_checkpoint_recovery_and_unsafe_side_effects` |
| META-12 | Reflection evidence | `tests/metacog/test_metacog_phases.py` phase 6 |
| TAX-01 | Company/product/workstream seed | `tests/taxonomy/test_taxonomy_and_classification.py` |
| TAX-02 | Inheritance + protected fields | `test_inheritance_company_product_protected` |
| TAX-03 | Confidence thresholds | `test_confidence_thresholds_and_irreversible_review` |
| TAX-04 | Human corrections + locks | `test_human_correction_override_persists` |
| TAX-05 | Reclassify after context change | `test_reclassify_after_context_change` |
| TAX-06 | Cross-company isolation | `test_cross_company_isolation_of_classification` |
| TAX-07 | Skills/Agents/Loops dims | `test_skills_agents_loops_dimensions` |
| TAX-08 | Matrix/portfolio views | `test_matrix_portfolio_filters` |
| TAX-09 | Goal/initiative/epic | `test_goal_initiative_epic_assignment_via_org_context` |
| GRF-01 | Graph compile + order | `tests/graph_runtime/test_graph_compiler_and_runtime.py` |
| GRF-02 | Cycle detection | `test_cycle_detection_blocks_start` |
| GRF-03 | Parallel-ready nodes | `test_parallel_ready_nodes` |
| GRF-04 | Failed dependency propagation | `test_failed_dependency_propagation` |
| GRF-05 | Independent verification | `test_unverified_blocks_synthesis` |
| GRF-06 | Multi-project isolation | `test_multi_project_graph_isolation` |
| GRF-07 | Artifact edge binding | `test_artifact_hash_and_edge_binding` |
| GRF-08 | Fan-out/fan-in diamond | `tests/graph_runtime/test_graph_runtime.py` |
| SWM-01 | Multi-provider spawn | `tests/swarm/test_all_providers_swarm.py` |
| SWM-02 | Spawn CBM+orgdims wire | `tests/swarm/test_spawn_integrations.py` |
| SWM-03 | Live providers (opt-in) | `tests/swarm/test_live_all_providers.py` (`-m live`) |
| PAR-01 | Max parallel diamonds/locks | `tests/comprehensive/test_max_parallelism.py` |
| PAR-02 | Integrated wave | `tests/comprehensive/test_integrated_wave.py` |
| API-01 | New route contracts | `tests/api/test_new_surfaces_contracts.py`, `test_graph_cbm_routes.py` |
| MIG-01 | Migrations 060–063 | `tests/db/test_migrations_060_063.py` |
| FEAT-01 | Full feature matrix | `tests/comprehensive/test_feature_matrix.py` |

## Implementation name map

| Spec name | Code module |
|---|---|
| Cognitive Budget Manager / CognitiveAllocator | `omniagentos.cbm.service.CognitiveBudgetService` |
| GraphCompiler | `omniagentos.graph_runtime.contracts.compile_template` + `GraphRuntimeService.compile` |
| ClassificationService | `omniagentos.orgdims.classify.ClassificationService` |
| Taxonomy | `omniagentos.orgdims.taxonomy` |
| Metacognition | `omniagentos.metacog.service.MetacogService` |
