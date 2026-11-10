-- Opt-in timed simulated-credit effects over the existing exact transfer ledger.
CREATE TABLE clock_transfer_profiles (
    run_id uuid PRIMARY KEY REFERENCES director_clock_bindings(run_id),
    version text NOT NULL CHECK (version='clock-transfer-v1'),
    max_operations integer NOT NULL CHECK (max_operations BETWEEN 1 AND 10000),
    max_calls integer NOT NULL CHECK (max_calls BETWEEN 1 AND 10000)
);
CREATE TABLE clock_transfer_operations (
    id uuid PRIMARY KEY,
    run_id uuid NOT NULL,
    clock_id uuid NOT NULL,
    process_id text NOT NULL,
    owner_id text NOT NULL,
    authorization_id uuid NOT NULL,
    arguments jsonb NOT NULL,
    event_key text NOT NULL,
    due_seconds integer NOT NULL CHECK (due_seconds BETWEEN 1 AND 31536000),
    admission jsonb NOT NULL,
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','applied','rejected')),
    result jsonb,
    applied_at_seconds integer,
    receipt_request_id uuid,
    FOREIGN KEY (run_id,clock_id) REFERENCES director_clock_bindings(run_id,clock_id),
    FOREIGN KEY (clock_id,process_id) REFERENCES simulation_clock_processes(clock_id,process_id),
    FOREIGN KEY (clock_id,event_key) REFERENCES simulation_clock_events(clock_id,event_key),
    FOREIGN KEY (run_id,owner_id,authorization_id) REFERENCES transfer_grants(run_id,owner_id,authorization_id),
    FOREIGN KEY (run_id,owner_id,receipt_request_id) REFERENCES credit_transfers(run_id,sender_owner_id,request_id),
    UNIQUE(run_id,owner_id,authorization_id),
    UNIQUE(run_id,owner_id,id),
    CHECK ((status='applied')=(receipt_request_id IS NOT NULL)),
    CHECK ((status='pending')=(result IS NULL)),
    CHECK ((status='pending')=(applied_at_seconds IS NULL))
);
CREATE TABLE clock_transfer_calls (
    run_id uuid NOT NULL,
    owner_id text NOT NULL,
    request_id uuid NOT NULL,
    operation_id uuid NOT NULL,
    input_hash text NOT NULL,
    PRIMARY KEY(run_id,owner_id,request_id),
    FOREIGN KEY (run_id,owner_id,operation_id) REFERENCES clock_transfer_operations(run_id,owner_id,id)
);
