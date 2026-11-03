-- Exact, one-use owner instructions. Agents cannot issue these grants.
CREATE TABLE transfer_grants (
    run_id uuid NOT NULL,
    owner_id text NOT NULL,
    authorization_id uuid NOT NULL,
    recipient_owner_id text NOT NULL,
    amount bigint NOT NULL CHECK (amount > 0 AND amount <= 1000000),
    memo text NOT NULL CHECK (length(memo) BETWEEN 1 AND 240),
    owner_message_id uuid NOT NULL REFERENCES owner_messages(id),
    PRIMARY KEY (run_id, owner_id, authorization_id),
    UNIQUE (run_id, owner_id, owner_message_id),
    CHECK (owner_id <> recipient_owner_id),
    FOREIGN KEY (run_id, owner_id) REFERENCES world_accounts(run_id, owner_id),
    FOREIGN KEY (run_id, recipient_owner_id) REFERENCES world_accounts(run_id, owner_id)
);

CREATE TABLE credit_transfers (
    run_id uuid NOT NULL,
    sender_owner_id text NOT NULL,
    request_id uuid NOT NULL,
    authorization_id uuid NOT NULL,
    recipient_owner_id text NOT NULL,
    amount bigint NOT NULL CHECK (amount > 0),
    memo text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (run_id, sender_owner_id, request_id),
    UNIQUE (run_id, sender_owner_id, authorization_id),
    FOREIGN KEY (run_id, sender_owner_id, authorization_id)
        REFERENCES transfer_grants(run_id, owner_id, authorization_id),
    FOREIGN KEY (run_id, sender_owner_id, request_id)
        REFERENCES world_requests(run_id, owner_id, request_id),
    FOREIGN KEY (run_id, recipient_owner_id) REFERENCES world_accounts(run_id, owner_id)
);
