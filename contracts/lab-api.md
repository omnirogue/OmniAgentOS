# H2 lab API (FROZEN, Wave 0)

Routes mounted on the H1 FastAPI app (`omniagentos.api:app`), prefix `/api/lab`. Same
localhost bind, CORS, error envelope, Store-via-deps as H1. SSE reuses H1's events feed
(new event types in lab.contracts.LabEvents). **BINDING reward-hacking rule:** NO endpoint
ever returns an eval case's held-out `expected` value — `expected` is stripped for
split=held_out on every response (a test in L07 + the council assert this).

| Method & path | → | Notes |
|---|---|---|
| GET /api/lab/disciplines | discipline summaries: champion surfaces, #experiments, top elo | |
| GET /api/lab/experiments?discipline=&status= | [experiment] | scorecard included when decided |
| POST /api/lab/experiments | {hypothesis, discipline, mutable_surface_kind, challenger_surface_id, eval_suite_id, budgets?, explore_policy?} → experiment | champion_surface_id filled from registry |
| GET /api/lab/experiments/{id} | experiment + eval_results (metrics only, NO held-out expected) + judge notes + scorecard | |
| POST /api/lab/experiments/{id}/run | {dry_run?} → {status} | enqueues the campaign run (L04) |
| POST /api/lab/experiments/{id}/disposition | {decision, note, decided_by} → experiment | human-review path; promote requires the safety+rollback checks |
| GET /api/lab/surfaces?discipline=&kind= | [surface] (content_hash, version, status) | prompt/genome content via GET /surfaces/{id} |
| GET /api/lab/surfaces/{id} | surface + content (prompt md or genome json) | |
| GET /api/lab/champions?discipline= | [champion] + history | |
| POST /api/lab/champions/{discipline}/{kind}/rollback | {} → champion | |
| GET /api/lab/tournaments?subject=&discipline= | [tournament] | |
| POST /api/lab/tournaments | {subject, discipline, config_ids, arena_task} → tournament | |
| GET /api/lab/tournaments/{id} | tournament + matches (judge_notes) + elo table | |
| GET /api/lab/leaderboard?subject= | [leaderboard rows] ordered by rank (the log-book) | |
| GET /api/lab/playbook?discipline= | [playbook entry] | validated traits |
| POST /api/lab/curate | {dry_run?} → summary | triggers L06 curation on demand (also runs 2x/day via launchd) |
| GET /api/lab/vault/tree | vault note tree (folders → notes: id,type,title,links,path) | for the vault browser/galaxy |
| GET /api/lab/vault/note?path= | {frontmatter, body, links[]} | confined read within vault_dir only |
| GET /api/lab/vault/search?q= | [{path,title,type,snippet}] | full-text over vault notes |

SSE (via H1 /api/events, types from LabEvents): experiment.updated, tournament.updated,
match.decided, surface.promoted, leaderboard.updated, playbook.updated, curation.ran —
each payload minimally carries the entity id + discipline/subject so the UI refreshes the
right view. IDs via contracts.new_id (exp/srf/evs/evc/tnm/mch/pbk/jdg). Lists newest-first,
limit default 100 max 500.
