-- Opt-in scripted booking assistance. No reservations or spending authority.
CREATE TABLE booking_inquiry_profiles (
    run_id uuid PRIMARY KEY REFERENCES environment_runs(id),
    configuration jsonb NOT NULL,
    configuration_hash text NOT NULL
);
CREATE TABLE booking_inquiries (
    run_id uuid NOT NULL REFERENCES booking_inquiry_profiles(run_id),
    id uuid NOT NULL,
    owner_id text NOT NULL,
    contact_id text NOT NULL,
    meeting_task_id text NOT NULL,
    request_id uuid NOT NULL,
    payload_hash text NOT NULL,
    due_s bigint NOT NULL CHECK (due_s >= 0),
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'ready')),
    options jsonb,
    PRIMARY KEY (run_id, id),
    UNIQUE (run_id, owner_id, request_id),
    FOREIGN KEY (run_id, owner_id) REFERENCES run_participants(run_id, owner_id),
    FOREIGN KEY (run_id, contact_id) REFERENCES run_participants(run_id, owner_id),
    CHECK (owner_id <> contact_id),
    CHECK ((status = 'pending' AND options IS NULL) OR (status = 'ready' AND options IS NOT NULL))
);
