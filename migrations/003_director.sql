-- Frozen scenarios and durable per-run orchestration. Board writes never lock a run.
CREATE TABLE environment_runs (
    id uuid PRIMARY KEY,
    cluster_id uuid NOT NULL UNIQUE REFERENCES clusters(id),
    scenario jsonb NOT NULL,
    scenario_hash text NOT NULL,
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','running','paused','completed','incomplete','invalid')),
    current_window integer NOT NULL DEFAULT 0 CHECK (current_window >= 0),
    phase text NOT NULL DEFAULT 'actors' CHECK (phase IN ('actors','agents','settle')),
    seed integer NOT NULL,
    max_windows integer NOT NULL CHECK (max_windows BETWEEN 1 AND 100),
    result jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE run_participants (
    run_id uuid NOT NULL REFERENCES environment_runs(id),
    owner_id text NOT NULL,
    agent_id uuid NOT NULL REFERENCES agents(id),
    session_id uuid NOT NULL UNIQUE,
    PRIMARY KEY (run_id, owner_id),
    UNIQUE (run_id, agent_id)
);
CREATE TABLE run_events (
    id uuid PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES environment_runs(id),
    kind text NOT NULL,
    "window" integer NOT NULL CHECK ("window" >= 0),
    payload jsonb NOT NULL,
    owner_id text,
    operation_key text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (run_id, operation_key),
    FOREIGN KEY (run_id, owner_id) REFERENCES run_participants(run_id, owner_id)
);
CREATE INDEX run_events_lookup ON run_events(run_id, "window");
CREATE TABLE owner_messages (
    id uuid PRIMARY KEY,
    run_id uuid NOT NULL,
    owner_id text NOT NULL,
    source text NOT NULL CHECK (source IN ('owner','agent')),
    content text NOT NULL CHECK (length(content) BETWEEN 1 AND 12000),
    request_id uuid NOT NULL,
    payload_hash text NOT NULL,
    "window" integer NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (run_id, owner_id) REFERENCES run_participants(run_id, owner_id),
    UNIQUE (run_id, owner_id, source, request_id)
);
CREATE TABLE run_jobs (
    id uuid PRIMARY KEY,
    run_id uuid NOT NULL,
    owner_id text NOT NULL,
    "window" integer NOT NULL,
    kind text NOT NULL CHECK (kind IN ('actor','agent')),
    input jsonb NOT NULL,
    input_hash text NOT NULL,
    output jsonb,
    output_meta jsonb NOT NULL DEFAULT '{}',
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','running','saved','completed','failed')),
    attempts integer NOT NULL DEFAULT 0,
    lease_token uuid,
    lease_until timestamptz,
    error text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (run_id, owner_id) REFERENCES run_participants(run_id, owner_id),
    UNIQUE (run_id, owner_id, "window", kind)
);
CREATE TABLE event_deliveries (
    run_id uuid NOT NULL,
    event_id uuid NOT NULL REFERENCES run_events(id),
    owner_id text NOT NULL,
    recipient_kind text NOT NULL CHECK (recipient_kind IN ('actor','agent')),
    job_id uuid NOT NULL REFERENCES run_jobs(id),
    PRIMARY KEY (event_id, owner_id, recipient_kind),
    FOREIGN KEY (run_id, owner_id) REFERENCES run_participants(run_id, owner_id)
);
CREATE TABLE run_beats (
    run_id uuid NOT NULL REFERENCES environment_runs(id),
    beat_id text NOT NULL,
    use_count integer NOT NULL DEFAULT 0,
    last_window integer,
    source_event_ids uuid[] NOT NULL DEFAULT '{}',
    PRIMARY KEY (run_id, beat_id)
);
CREATE TABLE run_reports (
    run_id uuid PRIMARY KEY REFERENCES environment_runs(id),
    report jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
