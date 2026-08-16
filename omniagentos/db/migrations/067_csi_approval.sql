-- 067_csi_approval.sql
-- CSI-native human approval (isolated from reliability improvements apply path).

ALTER TABLE csi_runs ADD COLUMN approval_status TEXT NOT NULL DEFAULT 'none';
-- none | proposed | approved | rejected
ALTER TABLE csi_runs ADD COLUMN approved_by TEXT;
ALTER TABLE csi_runs ADD COLUMN approved_at TEXT;
ALTER TABLE csi_runs ADD COLUMN implement_json TEXT NOT NULL DEFAULT '{}';
