-- Agent View enrollment and operator-selected company labels for Sessions.
-- All fields are nullable: historical rows and non-Claude sessions remain valid.
ALTER TABLE sessions ADD COLUMN company_override TEXT;
ALTER TABLE sessions ADD COLUMN agent_name TEXT;
ALTER TABLE sessions ADD COLUMN agent_status TEXT;
ALTER TABLE sessions ADD COLUMN agent_profile TEXT;
ALTER TABLE sessions ADD COLUMN agent_session_id TEXT;
