-- Authoritative reservation domain. Only this domain takes the short run lock.
CREATE TABLE world_state (
    run_id uuid PRIMARY KEY REFERENCES environment_runs(id),
    initialization_hash text NOT NULL,
    treasury bigint NOT NULL DEFAULT 0 CHECK (treasury >= 0),
    revision integer NOT NULL DEFAULT 0
);
CREATE TABLE world_accounts (
    run_id uuid NOT NULL,
    owner_id text NOT NULL,
    balance bigint NOT NULL CHECK (balance >= 0),
    consent_budget bigint NOT NULL DEFAULT 0 CHECK (consent_budget >= 0),
    consent_assets text[] NOT NULL DEFAULT '{}',
    consent_version integer NOT NULL DEFAULT 0 CHECK (consent_version >= 0),
    consent_expires_window integer,
    PRIMARY KEY (run_id, owner_id),
    FOREIGN KEY (run_id, owner_id) REFERENCES run_participants(run_id, owner_id)
);
CREATE TABLE world_assets (
    run_id uuid NOT NULL REFERENCES world_state(run_id),
    asset_id text NOT NULL,
    name text NOT NULL,
    price bigint NOT NULL CHECK (price >= 0),
    capacity integer NOT NULL CHECK (capacity BETWEEN 1 AND 15),
    version integer NOT NULL DEFAULT 0 CHECK (version >= 0),
    PRIMARY KEY (run_id, asset_id)
);
CREATE TABLE world_reservations (
    run_id uuid NOT NULL,
    owner_id text NOT NULL,
    asset_id text NOT NULL,
    paid bigint NOT NULL CHECK (paid >= 0),
    PRIMARY KEY (run_id, owner_id, asset_id),
    FOREIGN KEY (run_id, owner_id) REFERENCES world_accounts(run_id, owner_id),
    FOREIGN KEY (run_id, asset_id) REFERENCES world_assets(run_id, asset_id)
);
CREATE TABLE world_requests (
    run_id uuid NOT NULL,
    owner_id text NOT NULL,
    request_id uuid NOT NULL,
    payload_hash text NOT NULL,
    result jsonb NOT NULL,
    PRIMARY KEY (run_id, owner_id, request_id),
    FOREIGN KEY (run_id, owner_id) REFERENCES world_accounts(run_id, owner_id)
);
CREATE TABLE world_intents (
    run_id uuid NOT NULL,
    owner_id text NOT NULL,
    request_id uuid NOT NULL,
    window_index integer NOT NULL CHECK (window_index >= 0),
    kind text NOT NULL CHECK (kind IN ('reserve', 'cancel')),
    asset_id text NOT NULL,
    expected_version integer,
    consent_version integer NOT NULL,
    PRIMARY KEY (run_id, owner_id, request_id),
    UNIQUE (run_id, owner_id, window_index, asset_id),
    FOREIGN KEY (run_id, owner_id, request_id)
        REFERENCES world_requests(run_id, owner_id, request_id),
    FOREIGN KEY (run_id, asset_id) REFERENCES world_assets(run_id, asset_id)
);
CREATE TABLE world_settlements (
    run_id uuid NOT NULL REFERENCES world_state(run_id),
    window_index integer NOT NULL CHECK (window_index >= 0),
    seed integer NOT NULL,
    results jsonb NOT NULL,
    PRIMARY KEY (run_id, window_index)
);
CREATE TABLE world_ledger (
    id uuid PRIMARY KEY,
    run_id uuid NOT NULL,
    owner_id text NOT NULL,
    request_id uuid NOT NULL,
    asset_id text NOT NULL,
    amount bigint NOT NULL,
    balance_after bigint NOT NULL CHECK (balance_after >= 0),
    treasury_after bigint NOT NULL CHECK (treasury_after >= 0),
    window_index integer NOT NULL,
    UNIQUE (run_id, owner_id, request_id),
    FOREIGN KEY (run_id, owner_id, request_id)
        REFERENCES world_requests(run_id, owner_id, request_id)
);
CREATE TABLE world_journal (
    run_id uuid NOT NULL REFERENCES world_state(run_id),
    revision integer NOT NULL,
    kind text NOT NULL,
    payload jsonb NOT NULL,
    PRIMARY KEY (run_id, revision)
);
