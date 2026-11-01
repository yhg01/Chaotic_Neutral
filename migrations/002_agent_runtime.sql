-- Agent conversation identities and durable user-task receipts. Graph state is
-- stored by the versioned LangGraph PostgreSQL checkpointer migrations.
CREATE TABLE agent_sessions (
    session_key text PRIMARY KEY,
    cluster_id uuid NOT NULL,
    agent_id uuid NOT NULL,
    session_id uuid NOT NULL,
    instructions_hash text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (cluster_id, agent_id, session_id),
    FOREIGN KEY (cluster_id, agent_id) REFERENCES agents(cluster_id, id)
);

CREATE TABLE agent_tasks (
    session_key text NOT NULL REFERENCES agent_sessions(session_key),
    task_id uuid NOT NULL,
    prompt_hash text NOT NULL,
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'completed')),
    result text,
    model_calls integer NOT NULL DEFAULT 0 CHECK (model_calls >= 0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    completed_at timestamptz,
    PRIMARY KEY (session_key, task_id),
    CHECK ((status = 'completed') = (result IS NOT NULL)),
    CHECK ((status = 'completed') = (completed_at IS NOT NULL))
);
CREATE UNIQUE INDEX one_pending_task_per_session ON agent_tasks(session_key)
    WHERE status = 'pending';
