-- Track exact cache metrics to calculate effective cost per 1M tokens
ALTER TABLE runs ADD COLUMN cached_tokens_read INTEGER DEFAULT 0;
ALTER TABLE runs ADD COLUMN cached_tokens_written INTEGER DEFAULT 0;
ALTER TABLE runs ADD COLUMN cache_miss_tokens INTEGER DEFAULT 0;

-- Support Waku's TurnBudget and cost-containment on direct interfaces
ALTER TABLE runs ADD COLUMN gateway_source VARCHAR(64); -- 'discord', 'telegram', 'voice', 'api'
ALTER TABLE runs ADD COLUMN hourly_turn_count INTEGER DEFAULT 0;

-- KDA State Vector (Recursive Summary Stats replacing long transcript appendages)
ALTER TABLE steps ADD COLUMN kda_state_vector TEXT; -- JSON block storing sufficient stats

-- Loss-Safe Memory Consolidation State
ALTER TABLE sessions ADD COLUMN last_consolidated_at TIMESTAMP;
ALTER TABLE sessions ADD COLUMN unconsolidated_chat_count INTEGER DEFAULT 0;
