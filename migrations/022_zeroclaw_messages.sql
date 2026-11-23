-- Messages of an assistant session whose tool loop runs in an external program.
-- The harness makes every model call and runs every tool, so these rows are the full
-- history of the session. The clock reads its stamps from them, as it reads a graph checkpoint.
CREATE TABLE zeroclaw_messages (
    session_key text NOT NULL REFERENCES agent_sessions(session_key) ON DELETE CASCADE,
    seq integer NOT NULL CHECK (seq >= 0),
    task_id uuid NOT NULL,
    message jsonb NOT NULL,
    -- Harness data that the model never reads, such as mail the assistant was told about.
    note jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (session_key, seq)
);
