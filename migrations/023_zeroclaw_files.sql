-- Private scratch notes of an assistant session whose tool loop runs in an external program.
-- The graph backend keeps these files in its checkpoint. This backend has no checkpoint.
CREATE TABLE zeroclaw_files (
    session_key text PRIMARY KEY REFERENCES agent_sessions(session_key) ON DELETE CASCADE,
    files jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
