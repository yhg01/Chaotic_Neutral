-- Administrator controls are separate from participant jobs and model history.
CREATE TABLE console_executions (
    id uuid PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES environment_runs(id),
    request_id uuid NOT NULL,
    mode text NOT NULL CHECK (mode IN ('step','continue')),
    payload_hash text NOT NULL,
    status text NOT NULL DEFAULT 'queued'
      CHECK (status IN ('queued','running','paused','completed','failed','interrupted')),
    pause_requested boolean NOT NULL DEFAULT false,
    pid integer,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    started_at timestamptz,
    heartbeat_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at timestamptz,
    error text,
    UNIQUE (run_id,request_id)
);
CREATE UNIQUE INDEX console_one_active_execution ON console_executions(run_id)
    WHERE status IN ('queued','running');
CREATE TABLE console_pause_requests (
    run_id uuid NOT NULL REFERENCES environment_runs(id),
    request_id uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (run_id,request_id)
);
CREATE TABLE console_interventions (
    id uuid PRIMARY KEY,
    run_id uuid NOT NULL,
    request_id uuid NOT NULL,
    owner_id text NOT NULL,
    target text NOT NULL CHECK (target IN ('actor','agent')),
    prompt text NOT NULL CHECK (length(prompt) BETWEEN 1 AND 12000),
    expected_window integer NOT NULL CHECK (expected_window BETWEEN 0 AND 99),
    effective_window integer NOT NULL CHECK (effective_window BETWEEN 0 AND 99),
    payload_hash text NOT NULL,
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','applied')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    applied_at timestamptz,
    UNIQUE (run_id,request_id),
    FOREIGN KEY (run_id,owner_id) REFERENCES run_participants(run_id,owner_id)
);
CREATE INDEX console_pending_interventions ON console_interventions(run_id,effective_window)
    WHERE status='pending';
