-- Business work survives the end of an individual Agent activation.
CREATE TABLE transfer_task_tracking (
    run_id uuid PRIMARY KEY REFERENCES environment_runs(id)
);

CREATE TABLE transfer_tasks (
    id uuid PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES transfer_task_tracking(run_id),
    owner_id text NOT NULL,
    task_key text NOT NULL CHECK (length(task_key) BETWEEN 1 AND 80),
    recipient_owner_id text NOT NULL,
    description text NOT NULL CHECK (length(description) BETWEEN 1 AND 1000),
    owner_message_id uuid NOT NULL REFERENCES owner_messages(id),
    status text NOT NULL CHECK (status IN ('active','blocked','completed','cancelled')),
    blocked_on text[] NOT NULL DEFAULT '{}',
    authorization_id uuid,
    payment_request_id uuid,
    review_mail_id uuid REFERENCES inbox_messages(id),
    completion_evidence jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (run_id,owner_id,task_key),
    UNIQUE (run_id,owner_id,authorization_id),
    UNIQUE (run_id,owner_id,id),
    CHECK (owner_id <> recipient_owner_id),
    CHECK (blocked_on <@ ARRAY['owner_message','inbox_received']::text[]),
    CHECK ((status='blocked') = (cardinality(blocked_on)>0)),
    FOREIGN KEY (run_id,owner_id) REFERENCES world_accounts(run_id,owner_id),
    FOREIGN KEY (run_id,recipient_owner_id) REFERENCES world_accounts(run_id,owner_id),
    FOREIGN KEY (run_id,owner_id,authorization_id)
        REFERENCES transfer_grants(run_id,owner_id,authorization_id),
    FOREIGN KEY (run_id,owner_id,payment_request_id)
        REFERENCES credit_transfers(run_id,sender_owner_id,request_id)
);

CREATE TABLE transfer_task_operations (
    run_id uuid NOT NULL,
    owner_id text NOT NULL,
    request_id uuid NOT NULL,
    task_id uuid NOT NULL,
    request_payload jsonb NOT NULL,
    payload_hash text NOT NULL,
    result jsonb NOT NULL,
    PRIMARY KEY (run_id,owner_id,request_id),
    FOREIGN KEY (run_id,owner_id,task_id) REFERENCES transfer_tasks(run_id,owner_id,id)
);
