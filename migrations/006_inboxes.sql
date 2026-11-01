-- Per-owner internal mail. History sequence is not a delivery cursor.
CREATE TABLE inbox_messages (
    id uuid PRIMARY KEY,
    sequence bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
    run_id uuid NOT NULL REFERENCES environment_runs(id),
    sender_owner_id text,
    sender_source text NOT NULL CHECK (sender_source IN ('agent','actor','director')),
    sender_key text NOT NULL,
    recipient_owner_id text NOT NULL,
    subject text NOT NULL CHECK (length(subject) BETWEEN 1 AND 240),
    body text NOT NULL CHECK (length(body) BETWEEN 1 AND 12000),
    in_reply_to uuid,
    request_id uuid NOT NULL,
    payload_hash text NOT NULL,
    "window" integer NOT NULL CHECK ("window" >= 0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK ((sender_source = 'director') = (sender_owner_id IS NULL)),
    CHECK (sender_key = sender_source || ':' || COALESCE(sender_owner_id, '')),
    UNIQUE (run_id, sender_key, request_id),
    UNIQUE (run_id, id),
    UNIQUE (run_id, id, recipient_owner_id),
    FOREIGN KEY (run_id, sender_owner_id) REFERENCES run_participants(run_id, owner_id),
    FOREIGN KEY (run_id, recipient_owner_id) REFERENCES run_participants(run_id, owner_id),
    FOREIGN KEY (run_id, in_reply_to) REFERENCES inbox_messages(run_id, id)
);
CREATE INDEX inbox_received ON inbox_messages(run_id, recipient_owner_id, sequence DESC);
CREATE INDEX inbox_sent ON inbox_messages(run_id, sender_owner_id, sequence DESC);
CREATE TABLE inbox_receipts (
    run_id uuid NOT NULL,
    message_id uuid NOT NULL,
    owner_id text NOT NULL,
    read_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (run_id, message_id, owner_id),
    FOREIGN KEY (run_id, message_id, owner_id)
        REFERENCES inbox_messages(run_id, id, recipient_owner_id)
);
