-- Invented websites and online card limits for clocked runs. Other runs keep these tables empty.
-- sim_s holds story seconds from clock_state. A run with no clock_state row saves 0.

-- One row for each committed money change. The balance itself stays in world_accounts.
CREATE TABLE wallet_ledger (
    run_id uuid NOT NULL,
    owner_id text NOT NULL,
    operation_key text NOT NULL CHECK (length(operation_key) BETWEEN 1 AND 300),
    delta bigint NOT NULL CHECK (delta <> 0),
    balance_after bigint NOT NULL CHECK (balance_after >= 0),
    memo text NOT NULL CHECK (length(memo) BETWEEN 1 AND 500),
    source text NOT NULL CHECK (length(source) BETWEEN 1 AND 80),
    spend bigint NOT NULL DEFAULT 0 CHECK (spend >= 0),
    payload_hash text NOT NULL,
    sim_s bigint NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (run_id, operation_key),
    FOREIGN KEY (run_id, owner_id) REFERENCES world_accounts(run_id, owner_id)
);
CREATE INDEX wallet_ledger_owner ON wallet_ledger(run_id, owner_id);

-- Amounts an owner adds to what their assistant may spend online.
CREATE TABLE wallet_approvals (
    run_id uuid NOT NULL,
    owner_id text NOT NULL,
    operation_key text NOT NULL CHECK (length(operation_key) BETWEEN 1 AND 300),
    amount bigint NOT NULL CHECK (amount BETWEEN 1 AND 1000000000),
    sim_s bigint NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (run_id, operation_key),
    FOREIGN KEY (run_id, owner_id) REFERENCES world_accounts(run_id, owner_id)
);
CREATE INDEX wallet_approvals_owner ON wallet_approvals(run_id, owner_id);

CREATE TABLE web_sites (
    run_id uuid NOT NULL REFERENCES environment_runs(id),
    site_id text NOT NULL CHECK (site_id ~ '^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$'),
    name text NOT NULL,
    kind text NOT NULL
        CHECK (kind IN ('store','delivery','marketplace','gig','portal','booking')),
    version integer NOT NULL DEFAULT 1 CHECK (version >= 1),
    spec jsonb NOT NULL,
    source text NOT NULL,
    search_doc tsvector NOT NULL,
    sim_s bigint NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (run_id, site_id)
);
CREATE INDEX web_sites_search ON web_sites USING gin (search_doc);

-- Every version of a site, with the key of the change that made it.
CREATE TABLE web_site_versions (
    run_id uuid NOT NULL,
    site_id text NOT NULL,
    version integer NOT NULL,
    spec jsonb NOT NULL,
    spec_hash text NOT NULL,
    patch jsonb,
    patch_hash text,
    operation_key text NOT NULL,
    source text NOT NULL,
    sim_s bigint NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (run_id, site_id, version),
    UNIQUE (run_id, operation_key),
    FOREIGN KEY (run_id, site_id) REFERENCES web_sites(run_id, site_id)
);

-- Live stock of items and open places of booking slots. A site spec holds starting values.
CREATE TABLE web_inventory (
    run_id uuid NOT NULL,
    site_id text NOT NULL,
    entry_kind text NOT NULL CHECK (entry_kind IN ('item','slot')),
    entry_id text NOT NULL,
    remaining integer NOT NULL CHECK (remaining >= 0),
    PRIMARY KEY (run_id, site_id, entry_kind, entry_id),
    FOREIGN KEY (run_id, site_id) REFERENCES web_sites(run_id, site_id)
);

CREATE TABLE web_carts (
    run_id uuid NOT NULL,
    owner_id text NOT NULL,
    site_id text NOT NULL,
    item_id text NOT NULL,
    quantity integer NOT NULL CHECK (quantity BETWEEN 1 AND 99),
    PRIMARY KEY (run_id, owner_id, site_id, item_id),
    FOREIGN KEY (run_id, site_id) REFERENCES web_sites(run_id, site_id),
    FOREIGN KEY (run_id, owner_id) REFERENCES run_participants(run_id, owner_id)
);

CREATE TABLE web_orders (
    run_id uuid NOT NULL,
    order_id text NOT NULL,
    owner_id text NOT NULL,
    site_id text NOT NULL,
    lines jsonb NOT NULL CHECK (jsonb_typeof(lines) = 'array'),
    subtotal bigint NOT NULL CHECK (subtotal >= 0),
    delivery_fee bigint NOT NULL CHECK (delivery_fee >= 0),
    total bigint NOT NULL CHECK (total >= 0),
    note text,
    status text NOT NULL DEFAULT 'placed' CHECK (status IN ('placed','cancelled')),
    eta_s bigint NOT NULL,
    update_key text,
    request_id text NOT NULL,
    sim_s bigint NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (run_id, order_id),
    UNIQUE (run_id, owner_id, request_id),
    FOREIGN KEY (run_id, site_id) REFERENCES web_sites(run_id, site_id),
    FOREIGN KEY (run_id, owner_id) REFERENCES run_participants(run_id, owner_id)
);
CREATE INDEX web_orders_owner ON web_orders(run_id, owner_id, site_id);

CREATE TABLE web_listings (
    run_id uuid NOT NULL,
    listing_id text NOT NULL,
    owner_id text NOT NULL,
    site_id text NOT NULL,
    title text NOT NULL,
    description text NOT NULL DEFAULT '',
    category text NOT NULL DEFAULT '',
    price bigint NOT NULL CHECK (price >= 0),
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','removed','sold')),
    version integer NOT NULL DEFAULT 1 CHECK (version >= 1),
    request_id text NOT NULL,
    sim_s bigint NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (run_id, listing_id),
    UNIQUE (run_id, owner_id, request_id),
    FOREIGN KEY (run_id, site_id) REFERENCES web_sites(run_id, site_id),
    FOREIGN KEY (run_id, owner_id) REFERENCES run_participants(run_id, owner_id)
);
CREATE INDEX web_listings_site ON web_listings(run_id, site_id, status);

CREATE TABLE web_applications (
    run_id uuid NOT NULL,
    application_id text NOT NULL,
    owner_id text NOT NULL,
    site_id text NOT NULL,
    posting_id text NOT NULL,
    message text NOT NULL,
    status text NOT NULL DEFAULT 'submitted'
        CHECK (status IN ('submitted','accepted','declined')),
    request_id text NOT NULL,
    sim_s bigint NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (run_id, application_id),
    UNIQUE (run_id, owner_id, site_id, posting_id),
    FOREIGN KEY (run_id, site_id) REFERENCES web_sites(run_id, site_id),
    FOREIGN KEY (run_id, owner_id) REFERENCES run_participants(run_id, owner_id)
);

CREATE TABLE web_bookings (
    run_id uuid NOT NULL,
    booking_id text NOT NULL,
    owner_id text NOT NULL,
    site_id text NOT NULL,
    slot_id text NOT NULL,
    title text NOT NULL,
    price bigint NOT NULL CHECK (price >= 0),
    status text NOT NULL DEFAULT 'booked' CHECK (status IN ('booked','cancelled')),
    note text,
    request_id text NOT NULL,
    sim_s bigint NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (run_id, booking_id),
    FOREIGN KEY (run_id, site_id) REFERENCES web_sites(run_id, site_id),
    FOREIGN KEY (run_id, owner_id) REFERENCES run_participants(run_id, owner_id)
);
CREATE UNIQUE INDEX web_bookings_one_active ON web_bookings(run_id, owner_id, site_id, slot_id)
    WHERE status = 'booked';

CREATE TABLE web_bills (
    run_id uuid NOT NULL,
    site_id text NOT NULL,
    bill_id text NOT NULL,
    owner_id text NOT NULL,
    title text NOT NULL,
    amount bigint NOT NULL CHECK (amount >= 0),
    due_text text NOT NULL DEFAULT '',
    status text NOT NULL DEFAULT 'unpaid' CHECK (status IN ('unpaid','paid')),
    paid_request text,
    paid_sim_s bigint,
    sim_s bigint NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (run_id, site_id, bill_id),
    FOREIGN KEY (run_id, site_id) REFERENCES web_sites(run_id, site_id),
    FOREIGN KEY (run_id, owner_id) REFERENCES run_participants(run_id, owner_id)
);
CREATE INDEX web_bills_owner ON web_bills(run_id, owner_id);

-- Saved results of write actions. A retry returns this result and changes nothing.
CREATE TABLE web_requests (
    run_id uuid NOT NULL,
    owner_id text NOT NULL,
    request_id text NOT NULL CHECK (length(request_id) BETWEEN 1 AND 128),
    payload_hash text NOT NULL,
    payload jsonb NOT NULL,
    result jsonb NOT NULL,
    sim_s bigint NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (run_id, owner_id, request_id),
    FOREIGN KEY (run_id, owner_id) REFERENCES run_participants(run_id, owner_id)
);

-- One bounded request for a new site for each normalized search with no result.
CREATE TABLE web_search_asks (
    run_id uuid NOT NULL REFERENCES environment_runs(id),
    query_key text NOT NULL,
    owner_id text NOT NULL,
    query text NOT NULL,
    status text NOT NULL DEFAULT 'asked' CHECK (status IN ('asked','created','none')),
    attempts integer NOT NULL DEFAULT 1 CHECK (attempts >= 1),
    site_id text,
    sim_s bigint NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (run_id, query_key),
    FOREIGN KEY (run_id, owner_id) REFERENCES run_participants(run_id, owner_id)
);
