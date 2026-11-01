-- Owner resources and a simulated market share the existing world credit accounts.
CREATE TABLE resource_state (
    run_id uuid PRIMARY KEY REFERENCES environment_runs(id),
    initialization_hash text NOT NULL,
    revision integer NOT NULL DEFAULT 0
);
CREATE TABLE owner_resources (
    run_id uuid NOT NULL REFERENCES resource_state(run_id),
    resource_id text NOT NULL,
    owner_id text NOT NULL,
    origin_id text NOT NULL,
    title text NOT NULL,
    kind text NOT NULL CHECK (kind IN ('document', 'item')),
    content text NOT NULL DEFAULT '',
    quantity integer NOT NULL DEFAULT 0 CHECK (quantity >= 0),
    visibility text NOT NULL DEFAULT 'private' CHECK (visibility IN ('private','shared','public')),
    shared_owner_ids text[] NOT NULL DEFAULT '{}',
    agent_writable boolean NOT NULL DEFAULT true,
    version integer NOT NULL DEFAULT 0 CHECK (version >= 0),
    PRIMARY KEY (run_id, resource_id),
    FOREIGN KEY (run_id, owner_id) REFERENCES run_participants(run_id, owner_id),
    CHECK (kind = 'item' OR quantity = 0),
    CHECK (visibility = 'shared' OR cardinality(shared_owner_ids) = 0)
);
CREATE TABLE resource_requests (
    run_id uuid NOT NULL,
    owner_id text NOT NULL,
    request_id uuid NOT NULL,
    payload_hash text NOT NULL,
    result jsonb NOT NULL,
    PRIMARY KEY (run_id, owner_id, request_id),
    FOREIGN KEY (run_id, owner_id) REFERENCES run_participants(run_id, owner_id)
);
CREATE TABLE resource_journal (
    run_id uuid NOT NULL REFERENCES resource_state(run_id),
    revision integer NOT NULL,
    kind text NOT NULL,
    payload jsonb NOT NULL,
    PRIMARY KEY(run_id, revision)
);
CREATE TABLE market_permissions (
    run_id uuid NOT NULL,
    owner_id text NOT NULL,
    buy_budget bigint NOT NULL DEFAULT 0 CHECK (buy_budget >= 0),
    buy_items text[] NOT NULL DEFAULT '{}',
    sell_limits jsonb NOT NULL DEFAULT '[]',
    version integer NOT NULL DEFAULT 0,
    expires_window integer,
    PRIMARY KEY(run_id, owner_id),
    FOREIGN KEY(run_id, owner_id) REFERENCES world_accounts(run_id, owner_id)
);
CREATE TABLE market_requests (
    run_id uuid NOT NULL,
    owner_id text NOT NULL,
    request_id uuid NOT NULL,
    payload_hash text NOT NULL,
    result jsonb NOT NULL,
    PRIMARY KEY(run_id, owner_id, request_id),
    FOREIGN KEY(run_id, owner_id) REFERENCES world_accounts(run_id, owner_id)
);
CREATE TABLE market_intents (
    run_id uuid NOT NULL,
    owner_id text NOT NULL,
    request_id uuid NOT NULL,
    window_index integer NOT NULL,
    kind text NOT NULL CHECK(kind IN ('sell','buy')),
    target_id text NOT NULL,
    quantity integer NOT NULL CHECK(quantity > 0),
    unit_price bigint CHECK(unit_price >= 0),
    expected_version integer CHECK(expected_version >= 0),
    consent_version integer NOT NULL,
    PRIMARY KEY(run_id, owner_id, request_id),
    UNIQUE(run_id, owner_id, window_index, target_id),
    FOREIGN KEY(run_id, owner_id, request_id) REFERENCES market_requests(run_id, owner_id, request_id)
);
CREATE TABLE market_listings (
    run_id uuid NOT NULL,
    listing_id uuid NOT NULL,
    seller_id text NOT NULL,
    resource_id text NOT NULL,
    origin_id text NOT NULL,
    title text NOT NULL,
    quantity integer NOT NULL CHECK(quantity >= 0),
    unit_price bigint NOT NULL CHECK(unit_price >= 0),
    version integer NOT NULL DEFAULT 0,
    consent_version integer NOT NULL,
    PRIMARY KEY(run_id, listing_id),
    FOREIGN KEY(run_id, resource_id) REFERENCES owner_resources(run_id, resource_id),
    FOREIGN KEY(run_id, seller_id) REFERENCES world_accounts(run_id, owner_id)
);
CREATE TABLE market_settlements (
    run_id uuid NOT NULL REFERENCES environment_runs(id),
    window_index integer NOT NULL,
    seed integer NOT NULL,
    results jsonb NOT NULL,
    PRIMARY KEY(run_id, window_index)
);
CREATE TABLE market_ledger (
    run_id uuid NOT NULL,
    owner_id text NOT NULL,
    request_id uuid NOT NULL,
    seller_id text NOT NULL,
    listing_id uuid NOT NULL,
    resource_id text NOT NULL,
    quantity integer NOT NULL CHECK(quantity > 0),
    amount bigint NOT NULL CHECK(amount >= 0),
    buyer_balance bigint NOT NULL CHECK(buyer_balance >= 0),
    seller_balance bigint NOT NULL CHECK(seller_balance >= 0),
    window_index integer NOT NULL,
    PRIMARY KEY(run_id, owner_id, request_id),
    FOREIGN KEY(run_id, owner_id, request_id) REFERENCES market_requests(run_id, owner_id, request_id),
    FOREIGN KEY(run_id, listing_id) REFERENCES market_listings(run_id, listing_id)
);
