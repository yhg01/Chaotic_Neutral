-- Complete simulation boundaries. Real model spending is never copied or rewound.
CREATE TABLE run_boundaries (
    run_id uuid NOT NULL REFERENCES environment_runs(id),
    window_index integer NOT NULL CHECK (window_index BETWEEN 0 AND 99),
    version integer NOT NULL DEFAULT 1 CHECK (version = 1),
    snapshot jsonb NOT NULL,
    snapshot_hash text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (run_id, window_index)
);
CREATE TABLE run_branches (
    parent_run_id uuid NOT NULL REFERENCES environment_runs(id),
    request_id uuid NOT NULL,
    payload_hash text NOT NULL,
    child_run_id uuid NOT NULL UNIQUE REFERENCES environment_runs(id),
    window_index integer NOT NULL CHECK (window_index BETWEEN 0 AND 99),
    source text NOT NULL CHECK (source IN ('initial', 'checkpoint')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (parent_run_id, request_id)
);
-- The original normalized payload permits hashes to be rebuilt after identity mapping.
ALTER TABLE world_requests ADD COLUMN request_payload jsonb;
ALTER TABLE resource_requests ADD COLUMN request_payload jsonb;
ALTER TABLE market_requests ADD COLUMN request_payload jsonb;
