-- Opt-in explicit contracts. Existing runs and all historical mail remain unchanged.
CREATE TABLE confirmation_tasks (
    id uuid PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES clock_runtime_profiles(run_id),
    task_key text NOT NULL,
    owner_id text NOT NULL,
    peer_owner_id text NOT NULL,
    terms text NOT NULL,
    condition text NOT NULL,
    source_message_id uuid REFERENCES owner_messages(id),
    request_mail_id uuid REFERENCES inbox_messages(id),
    question_message_id uuid REFERENCES owner_messages(id),
    decision text CHECK (decision IN ('confirm','decline')),
    decision_message_id uuid REFERENCES owner_messages(id),
    reply_mail_id uuid REFERENCES inbox_messages(id),
    reply_body_read boolean NOT NULL DEFAULT false,
    owner_report_id uuid REFERENCES owner_messages(id),
    peer_report_id uuid REFERENCES owner_messages(id),
    UNIQUE(run_id,owner_id,task_key),
    FOREIGN KEY(run_id,owner_id) REFERENCES run_participants(run_id,owner_id),
    FOREIGN KEY(run_id,peer_owner_id) REFERENCES run_participants(run_id,owner_id),
    CHECK (owner_id <> peer_owner_id),
    CHECK ((decision IS NULL) = (decision_message_id IS NULL))
);
CREATE TABLE confirmation_operations (
    run_id uuid NOT NULL,
    owner_id text NOT NULL,
    request_id uuid NOT NULL,
    payload_hash text NOT NULL,
    result jsonb NOT NULL,
    PRIMARY KEY(run_id,owner_id,request_id),
    FOREIGN KEY(run_id,owner_id) REFERENCES run_participants(run_id,owner_id)
);
