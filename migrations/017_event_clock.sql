-- Event clock core. Runs with no clock_state row keep their window behaviour.
-- docs/EVENT_CLOCK.md rules R1 to R5 and R16 describe these tables.

-- One activation per process per clock_seq, so one window can hold many. Legacy rows keep 0.
ALTER TABLE run_jobs ADD COLUMN clock_seq bigint NOT NULL DEFAULT 0;
ALTER TABLE run_jobs ADD COLUMN start_s bigint;
ALTER TABLE run_jobs ADD COLUMN end_s bigint;
ALTER TABLE run_jobs DROP CONSTRAINT run_jobs_run_id_owner_id_window_kind_key;
ALTER TABLE run_jobs ADD CONSTRAINT run_jobs_run_id_owner_id_window_kind_seq_key
    UNIQUE (run_id, owner_id, "window", kind, clock_seq);

-- Which process's turn committed an event. The own-effect filter reads this.
ALTER TABLE run_events ADD COLUMN cause_process text;

CREATE TABLE clock_state (
    run_id uuid PRIMARY KEY REFERENCES environment_runs(id),
    epoch timestamptz NOT NULL,
    sim_s bigint NOT NULL DEFAULT 0 CHECK (sim_s >= 0),
    stamp_base bigint NOT NULL DEFAULT 0 CHECK (stamp_base >= 0),
    pop_seq bigint NOT NULL DEFAULT 0 CHECK (pop_seq >= 0),
    turn_process text,
    config jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(config) = 'object')
);

CREATE TABLE clock_events (
    id uuid PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES environment_runs(id),
    due_s bigint NOT NULL CHECK (due_s >= 0),
    class smallint NOT NULL CHECK (class BETWEEN 0 AND 10),
    tie bytea NOT NULL,
    key text NOT NULL CHECK (char_length(key) BETWEEN 1 AND 500),
    kind text NOT NULL CHECK (char_length(kind) BETWEEN 1 AND 80),
    owner_id text,
    payload jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(payload) = 'object'),
    status text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','done','cancelled')),
    popped_seq bigint,
    source text NOT NULL DEFAULT 'engine',
    created_sim_s bigint NOT NULL DEFAULT 0,
    UNIQUE (run_id, key),
    FOREIGN KEY (run_id, owner_id) REFERENCES run_participants(run_id, owner_id)
);
-- The pop order of rule R2. Bytes for the hash and C collation for the key keep it portable.
CREATE INDEX clock_events_next ON clock_events(run_id, due_s, class, tie, key COLLATE "C")
    WHERE status = 'queued';

CREATE TABLE clock_processes (
    run_id uuid NOT NULL,
    owner_id text NOT NULL,
    kind text NOT NULL CHECK (kind IN ('actor','agent')),
    bar_s bigint NOT NULL DEFAULT 0 CHECK (bar_s >= 0),
    state text NOT NULL DEFAULT 'idle'
        CHECK (state IN ('idle','active','busy','waiting','offline','dead')),
    until_s bigint,
    wake_on jsonb,
    wait_id bigint NOT NULL DEFAULT 0,
    activation_seq bigint NOT NULL DEFAULT 0,
    last_wake_s bigint,
    PRIMARY KEY (run_id, owner_id, kind),
    FOREIGN KEY (run_id, owner_id) REFERENCES run_participants(run_id, owner_id)
);

CREATE TABLE clock_actions (
    id uuid PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES environment_runs(id),
    owner_id text,
    process text NOT NULL CHECK (process IN ('actor','agent','director')),
    label text NOT NULL,
    detail jsonb NOT NULL DEFAULT '{}',
    start_s bigint NOT NULL CHECK (start_s >= 0),
    duration_s bigint NOT NULL CHECK (duration_s >= 0),
    status text NOT NULL DEFAULT 'active'
        CHECK (status IN ('active','done','replaced','cancelled')),
    reachable boolean,
    replaced_by uuid,
    key text NOT NULL,
    UNIQUE (run_id, key),
    FOREIGN KEY (run_id, owner_id) REFERENCES run_participants(run_id, owner_id)
);
CREATE INDEX clock_actions_current ON clock_actions(run_id, owner_id, process)
    WHERE status = 'active';

CREATE TABLE clock_draws (
    run_id uuid NOT NULL REFERENCES environment_runs(id),
    action_id uuid NOT NULL REFERENCES clock_actions(id),
    level text NOT NULL,
    u numeric NOT NULL,
    v numeric NOT NULL,
    outcome text NOT NULL CHECK (outcome IN ('none','mild','extreme')),
    fire_s bigint,
    PRIMARY KEY (run_id, action_id)
);

-- Story time for clocked rows (R16). The API process writes through this trigger too.
-- The microsecond offset keeps insert order inside one simulated second, and it never
-- resets, so created_at is strictly increasing for a run while sim_s does not go back.
-- A run with no clock_state row updates nothing and keeps the column default.
CREATE FUNCTION clock_stamp() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    target uuid;
    base timestamptz;
    seconds bigint;
    offset_us bigint;
    process text;
BEGIN
    IF TG_TABLE_NAME = 'messages' THEN
        SELECT id INTO target FROM environment_runs WHERE cluster_id = NEW.cluster_id;
        IF target IS NULL THEN
            RETURN NEW;
        END IF;
    ELSE
        target := NEW.run_id;
    END IF;
    PERFORM 1 FROM clock_state WHERE run_id = target;
    IF NOT FOUND THEN
        RETURN NEW;
    END IF;
    -- Lock order is the run, then the clock. Without this, the foreign key check after this
    -- trigger can wait for a run lock while the run's holder waits for this clock row.
    PERFORM 1 FROM environment_runs WHERE id = target FOR KEY SHARE;
    UPDATE clock_state SET stamp_base = stamp_base + 1 WHERE run_id = target
        RETURNING epoch, sim_s, stamp_base, turn_process
        INTO base, seconds, offset_us, process;
    NEW.created_at := base + seconds * interval '1 second' + offset_us * interval '1 microsecond';
    IF TG_TABLE_NAME = 'run_events' THEN
        NEW.cause_process := process;
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER clock_stamp_row BEFORE INSERT ON run_events
    FOR EACH ROW EXECUTE FUNCTION clock_stamp();
CREATE TRIGGER clock_stamp_row BEFORE INSERT ON owner_messages
    FOR EACH ROW EXECUTE FUNCTION clock_stamp();
CREATE TRIGGER clock_stamp_row BEFORE INSERT ON inbox_messages
    FOR EACH ROW EXECUTE FUNCTION clock_stamp();
CREATE TRIGGER clock_stamp_row BEFORE INSERT ON messages
    FOR EACH ROW EXECUTE FUNCTION clock_stamp();
CREATE TRIGGER clock_stamp_row BEFORE INSERT ON credit_transfers
    FOR EACH ROW EXECUTE FUNCTION clock_stamp();
CREATE TRIGGER clock_stamp_row BEFORE INSERT ON transfer_tasks
    FOR EACH ROW EXECUTE FUNCTION clock_stamp();
CREATE TRIGGER clock_stamp_row BEFORE INSERT ON run_jobs
    FOR EACH ROW EXECUTE FUNCTION clock_stamp();
