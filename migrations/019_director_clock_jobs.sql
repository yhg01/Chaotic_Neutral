-- Separate elapsed-time dispatch profile; old windows retain their original schema.
ALTER TABLE simulation_clocks ADD COLUMN manager_run_id uuid UNIQUE REFERENCES environment_runs(id);
CREATE TABLE director_clock_bindings (
    run_id uuid PRIMARY KEY REFERENCES environment_runs(id),
    clock_id uuid NOT NULL UNIQUE REFERENCES simulation_clocks(id),
    version text NOT NULL CHECK (version='director-clock-jobs-v1'),
    max_jobs integer NOT NULL CHECK (max_jobs BETWEEN 1 AND 10000),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(run_id,clock_id)
);

CREATE TABLE director_clock_jobs (
    id uuid PRIMARY KEY,
    run_id uuid NOT NULL,
    clock_id uuid NOT NULL,
    process_id text NOT NULL,
    owner_id text NOT NULL,
    role text NOT NULL CHECK (role IN ('actor','agent')),
    request_id uuid NOT NULL,
    input jsonb NOT NULL,
    input_hash text NOT NULL,
    output jsonb,
    output_hash text,
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','running','uncertain','saved','timed','applied')),
    claim_token uuid,
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 1),
    error text,
    event_key text,
    applied_at_seconds integer,
    owner_message_id uuid REFERENCES owner_messages(id),
    FOREIGN KEY (run_id,clock_id) REFERENCES director_clock_bindings(run_id,clock_id),
    FOREIGN KEY (clock_id,process_id) REFERENCES simulation_clock_processes(clock_id,process_id),
    FOREIGN KEY (run_id,owner_id) REFERENCES run_participants(run_id,owner_id),
    FOREIGN KEY (clock_id,event_key) REFERENCES simulation_clock_events(clock_id,event_key),
    UNIQUE(run_id,request_id)
);
CREATE UNIQUE INDEX director_clock_one_active_lane ON director_clock_jobs(clock_id,process_id)
    WHERE status<>'applied';

CREATE TABLE director_clock_deliveries (
    clock_id uuid NOT NULL,
    event_key text NOT NULL,
    process_id text NOT NULL,
    job_id uuid NOT NULL REFERENCES director_clock_jobs(id),
    PRIMARY KEY (clock_id,event_key,process_id),
    FOREIGN KEY (clock_id,event_key) REFERENCES simulation_clock_events(clock_id,event_key),
    FOREIGN KEY (clock_id,process_id) REFERENCES simulation_clock_processes(clock_id,process_id)
);

CREATE TABLE director_clock_recovery (
    job_id uuid PRIMARY KEY REFERENCES director_clock_jobs(id),
    evidence_reference text NOT NULL CHECK (length(evidence_reference) BETWEEN 1 AND 240),
    output_hash text NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
