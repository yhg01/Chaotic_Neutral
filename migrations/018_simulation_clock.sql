-- Opt-in elapsed-time Clock component. Legacy window runs do not use these tables.
CREATE TABLE simulation_clocks (
    id uuid PRIMARY KEY,
    contract jsonb NOT NULL,
    contract_hash text NOT NULL,
    now_seconds integer NOT NULL DEFAULT 0 CHECK (now_seconds >= 0),
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','paused','horizon')),
    revision bigint NOT NULL DEFAULT 0 CHECK (revision >= 0),
    last_event_second integer NOT NULL DEFAULT -1,
    last_event_priority integer NOT NULL DEFAULT 0,
    last_event_key text COLLATE "C" NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE simulation_clock_processes (
    clock_id uuid NOT NULL REFERENCES simulation_clocks(id),
    process_id text NOT NULL,
    owner_id text NOT NULL,
    role text NOT NULL CHECK (role IN ('actor','agent')),
    state text NOT NULL DEFAULT 'ready' CHECK (state IN ('ready','sleeping','busy')),
    ready_at integer NOT NULL DEFAULT 0 CHECK (ready_at >= 0),
    event_key text,
    interrupt_on jsonb NOT NULL DEFAULT '[]',
    PRIMARY KEY (clock_id,process_id),
    UNIQUE (clock_id,owner_id,role)
);

CREATE TABLE simulation_clock_events (
    clock_id uuid NOT NULL REFERENCES simulation_clocks(id),
    event_key text COLLATE "C" NOT NULL,
    at_seconds integer NOT NULL CHECK (at_seconds >= 0),
    priority integer NOT NULL CHECK (priority BETWEEN -100 AND 100),
    kind text NOT NULL,
    recipients jsonb NOT NULL,
    payload jsonb NOT NULL,
    input_hash text NOT NULL,
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','delivered','cancelled')),
    delivered_revision bigint,
    PRIMARY KEY (clock_id,event_key)
);
CREATE INDEX simulation_clock_pending ON simulation_clock_events
    (clock_id,at_seconds,priority,event_key) WHERE status='pending';

CREATE TABLE simulation_clock_operations (
    clock_id uuid NOT NULL REFERENCES simulation_clocks(id),
    request_id uuid NOT NULL,
    input_hash text NOT NULL,
    response jsonb NOT NULL,
    PRIMARY KEY (clock_id,request_id)
);
