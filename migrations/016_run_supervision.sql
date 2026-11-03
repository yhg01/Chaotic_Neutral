-- Opt-in recovery policy and progress observations. Existing runs stay disabled.
ALTER TABLE run_jobs ADD COLUMN failure_kind text;
ALTER TABLE run_jobs ADD COLUMN failure_retryable boolean NOT NULL DEFAULT false;

CREATE TABLE run_supervision (
    run_id uuid PRIMARY KEY REFERENCES environment_runs(id),
    policy jsonb NOT NULL CHECK (jsonb_typeof(policy)='object'),
    retry_signature text,
    retry_due_at timestamptz,
    recoveries integer NOT NULL DEFAULT 0 CHECK (recoveries>=0),
    last_health_window integer NOT NULL DEFAULT -1,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE run_task_health (
    run_id uuid NOT NULL REFERENCES run_supervision(run_id),
    owner_id text NOT NULL,
    task_id uuid NOT NULL,
    progress_hash text NOT NULL,
    seen_progress_hashes text[] NOT NULL DEFAULT '{}'
        CHECK (cardinality(seen_progress_hashes)<=256),
    last_progress_window integer NOT NULL,
    last_checked_window integer NOT NULL,
    stagnant_windows integer NOT NULL DEFAULT 0 CHECK (stagnant_windows>=0),
    blocked_reason text,
    PRIMARY KEY (run_id,owner_id,task_id),
    FOREIGN KEY (run_id,owner_id,task_id) REFERENCES shop_tasks(run_id,owner_id,id)
);
