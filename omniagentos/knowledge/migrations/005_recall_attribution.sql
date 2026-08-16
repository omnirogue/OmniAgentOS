-- Migration 005 — per-fact recall attribution (T6.3, knowledge feedback loop).
--
-- Before this, record_helped(run_id) credited EVERY fact id in the run's recall_log
-- row. But recall() ranks up to 50 candidates and record_recall() logs all of them,
-- while render_recall_block() injects only the prefix that fits the character budget.
-- The truncated tail never reached the agent, so crediting it attributed "this fact
-- helped" to facts nothing ever read — noise that gets worse the tighter the budget is.
--
-- surfaced_fact_ids records the exact subset the renderer emitted. Deliberately
-- NULLABLE, with three distinct meanings that a NOT NULL DEFAULT '{}' would collapse:
--   NULL  -> attribution unknown (row written before 005, or by an older process);
--            record_helped falls back to fact_ids, preserving the previous behaviour.
--   '{}'  -> known to have surfaced nothing (block was empty / over budget);
--            record_helped credits nothing, which is the correct answer.
--   {...} -> the facts that actually made it into the injected block.
--
-- No CREATE TABLE here, but the 001 migration discipline note asks every migration that
-- adds a column to carry its GRANT stanza. recall_log's table-level INSERT/SELECT grant
-- already covers new columns; this restates it explicitly so the privilege surface of
-- the new column is visible in this file rather than inferred from 001.
ALTER TABLE recall_log ADD COLUMN surfaced_fact_ids bigint[];

GRANT SELECT, INSERT ON recall_log TO knowledge_agent;
-- Still NO UPDATE on recall_log for the agent role: attribution is written once, in the
-- same INSERT as the rest of the row, and can never be rewritten afterwards to inflate a
-- chosen fact's helped_count. (knowledge_admin retains ALL from 001.)
