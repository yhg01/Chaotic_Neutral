CREATE TABLE clusters (
    id uuid PRIMARY KEY,
    name text NOT NULL UNIQUE CHECK (char_length(btrim(name)) BETWEEN 1 AND 80),
    capacity integer NOT NULL DEFAULT 15 CHECK (capacity = 15),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE agents (
    id uuid PRIMARY KEY,
    cluster_id uuid NOT NULL REFERENCES clusters(id),
    name text NOT NULL CHECK (char_length(btrim(name)) BETWEEN 1 AND 80),
    token_hash text NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (cluster_id, name),
    UNIQUE (cluster_id, id)
);

CREATE TABLE messages (
    id uuid PRIMARY KEY,
    seq bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
    cluster_id uuid NOT NULL REFERENCES clusters(id),
    author_id uuid NOT NULL,
    parent_id uuid,
    thread_id uuid GENERATED ALWAYS AS (coalesce(parent_id, id)) STORED,
    is_root boolean GENERATED ALWAYS AS (parent_id IS NULL) STORED,
    parent_is_root boolean NOT NULL DEFAULT true CHECK (parent_is_root),
    title text,
    body text NOT NULL CHECK (char_length(btrim(body)) BETWEEN 1 AND 20000),
    request_id uuid NOT NULL,
    payload_hash text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (author_id, request_id),
    UNIQUE (cluster_id, id),
    UNIQUE (cluster_id, id, is_root),
    FOREIGN KEY (cluster_id, author_id) REFERENCES agents(cluster_id, id),
    FOREIGN KEY (cluster_id, parent_id, parent_is_root)
        REFERENCES messages(cluster_id, id, is_root),
    CHECK (
        (parent_id IS NULL AND char_length(btrim(title)) BETWEEN 1 AND 200 AND title IS NOT NULL)
        OR (parent_id IS NOT NULL AND parent_id <> id AND title IS NULL)
    )
);

CREATE INDEX messages_roots ON messages(cluster_id, seq DESC) WHERE parent_id IS NULL;
CREATE INDEX messages_replies ON messages(cluster_id, parent_id, seq);
CREATE INDEX messages_updates ON messages(cluster_id, seq);

CREATE TABLE message_receipts (
    cluster_id uuid NOT NULL,
    agent_id uuid NOT NULL,
    message_id uuid NOT NULL,
    acknowledged_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (agent_id, message_id),
    FOREIGN KEY (cluster_id, agent_id) REFERENCES agents(cluster_id, id),
    FOREIGN KEY (cluster_id, message_id) REFERENCES messages(cluster_id, id)
);
