-- Opt-in node boundaries. Existing graph/session and native world records remain authoritative.
CREATE TABLE clock_runtime_profiles (
    run_id uuid PRIMARY KEY REFERENCES director_clock_bindings(run_id),
    configuration jsonb NOT NULL,
    configuration_hash text NOT NULL
);
CREATE TABLE clock_runtime_activations (
    id uuid PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES clock_runtime_profiles(run_id),
    process_id text NOT NULL,
    ordinal integer NOT NULL CHECK (ordinal >= 0),
    envelope jsonb NOT NULL,
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','done')),
    result jsonb,
    UNIQUE(run_id,process_id,ordinal)
);
CREATE UNIQUE INDEX clock_runtime_active_activation
    ON clock_runtime_activations(run_id,process_id) WHERE status='active';
CREATE TABLE clock_runtime_owner_deliveries (
    message_id uuid PRIMARY KEY REFERENCES owner_messages(id),
    activation_id uuid NOT NULL REFERENCES clock_runtime_activations(id)
);
CREATE TABLE clock_graph_steps (
    session_key text NOT NULL REFERENCES agent_sessions(session_key),
    task_id uuid NOT NULL,
    step_id uuid NOT NULL,
    phase text NOT NULL CHECK (phase IN ('model','tools')),
    checkpoint_id text NOT NULL,
    input_messages jsonb NOT NULL,
    model_calls integer NOT NULL,
    status text NOT NULL CHECK (status IN ('running','saved','uncertain')),
    result jsonb,
    PRIMARY KEY(session_key,step_id),
    FOREIGN KEY(session_key,task_id) REFERENCES agent_tasks(session_key,task_id),
    UNIQUE(session_key,task_id,checkpoint_id)
);
CREATE TABLE clock_runtime_tools (
    id uuid PRIMARY KEY,
    activation_id uuid NOT NULL REFERENCES clock_runtime_activations(id),
    run_id uuid NOT NULL REFERENCES clock_runtime_profiles(run_id),
    clock_id uuid NOT NULL,
    process_id text NOT NULL,
    checkpoint_id text NOT NULL,
    calls jsonb NOT NULL,
    namespace uuid NOT NULL,
    event_key text,
    due_seconds integer NOT NULL,
    status text NOT NULL CHECK (status IN ('timed','released','done')),
    FOREIGN KEY(clock_id,process_id) REFERENCES simulation_clock_processes(clock_id,process_id),
    FOREIGN KEY(clock_id,event_key) REFERENCES simulation_clock_events(clock_id,event_key),
    UNIQUE(activation_id,checkpoint_id)
);
CREATE TABLE clock_runtime_actor_outputs (
    job_id uuid PRIMARY KEY REFERENCES director_clock_jobs(id),
    output jsonb NOT NULL,
    timed_output jsonb,
    diagnostics jsonb NOT NULL
);
CREATE TABLE clock_runtime_plans (
    run_id uuid PRIMARY KEY REFERENCES director_clock_bindings(run_id),
    manifest jsonb NOT NULL,
    manifest_hash text NOT NULL
);
