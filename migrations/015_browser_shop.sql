-- The browser shop is a frontend to the existing consent-bound simulated market.
CREATE TABLE shop_profiles (
    run_id uuid PRIMARY KEY REFERENCES environment_runs(id)
);
CREATE TABLE shop_tasks (
    id uuid PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES shop_profiles(run_id),
    owner_id text NOT NULL,
    task_key text NOT NULL CHECK (length(task_key) BETWEEN 1 AND 80),
    description text NOT NULL CHECK (length(description) BETWEEN 1 AND 1000),
    item_ids text[] NOT NULL CHECK (cardinality(item_ids) BETWEEN 1 AND 20),
    quantity integer NOT NULL CHECK (quantity BETWEEN 1 AND 100),
    max_total bigint NOT NULL CHECK (max_total BETWEEN 1 AND 1000000),
    owner_message_id uuid NOT NULL REFERENCES owner_messages(id),
    status text NOT NULL DEFAULT 'open' CHECK (status IN ('open','submitted','completed','cancelled')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (run_id,owner_id,task_key),
    UNIQUE (run_id,owner_id,id),
    FOREIGN KEY (run_id,owner_id) REFERENCES world_accounts(run_id,owner_id)
);
CREATE TABLE shop_carts (
    run_id uuid NOT NULL REFERENCES shop_profiles(run_id),
    owner_id text NOT NULL,
    task_id uuid,
    version bigint NOT NULL DEFAULT 0 CHECK (version >= 0),
    PRIMARY KEY (run_id,owner_id),
    FOREIGN KEY (run_id,owner_id) REFERENCES world_accounts(run_id,owner_id),
    FOREIGN KEY (run_id,owner_id,task_id) REFERENCES shop_tasks(run_id,owner_id,id)
);
CREATE TABLE shop_cart_lines (
    run_id uuid NOT NULL,
    owner_id text NOT NULL,
    listing_id uuid NOT NULL,
    quantity integer NOT NULL CHECK (quantity BETWEEN 1 AND 100),
    quoted_price bigint NOT NULL CHECK (quoted_price >= 0),
    quoted_version integer NOT NULL,
    PRIMARY KEY (run_id,owner_id,listing_id),
    FOREIGN KEY (run_id,owner_id) REFERENCES shop_carts(run_id,owner_id),
    FOREIGN KEY (run_id,listing_id) REFERENCES market_listings(run_id,listing_id)
);
CREATE TABLE shop_orders (
    id uuid PRIMARY KEY,
    run_id uuid NOT NULL,
    owner_id text NOT NULL,
    task_id uuid NOT NULL,
    cart_version bigint NOT NULL,
    window_index integer NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (run_id,owner_id,cart_version),
    UNIQUE (run_id,owner_id,id),
    FOREIGN KEY (run_id,owner_id,task_id) REFERENCES shop_tasks(run_id,owner_id,id)
);
CREATE TABLE shop_order_lines (
    run_id uuid NOT NULL,
    owner_id text NOT NULL,
    order_id uuid NOT NULL,
    request_id uuid NOT NULL,
    listing_id uuid NOT NULL,
    origin_id text NOT NULL,
    title text NOT NULL,
    quantity integer NOT NULL CHECK (quantity > 0),
    unit_price bigint NOT NULL CHECK (unit_price >= 0),
    PRIMARY KEY (run_id,owner_id,order_id,request_id),
    FOREIGN KEY (run_id,owner_id,order_id) REFERENCES shop_orders(run_id,owner_id,id),
    FOREIGN KEY (run_id,owner_id,request_id) REFERENCES market_requests(run_id,owner_id,request_id),
    FOREIGN KEY (run_id,listing_id) REFERENCES market_listings(run_id,listing_id)
);
CREATE TABLE shop_operations (
    run_id uuid NOT NULL REFERENCES shop_profiles(run_id),
    owner_id text NOT NULL,
    request_id uuid NOT NULL,
    request_payload jsonb NOT NULL,
    payload_hash text NOT NULL,
    action_hash text,
    result jsonb NOT NULL,
    PRIMARY KEY (run_id,owner_id,request_id),
    FOREIGN KEY (run_id,owner_id) REFERENCES world_accounts(run_id,owner_id)
);
