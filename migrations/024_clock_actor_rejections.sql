-- Explicit operator recovery of a fully observed, rejected Actor response.
-- Unknown provider output is never made retryable by this table.
CREATE TABLE clock_runtime_actor_rejections (
    id uuid PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES clock_runtime_profiles(run_id),
    job_id uuid NOT NULL REFERENCES director_clock_jobs(id),
    source_before text NOT NULL,
    source_after text NOT NULL,
    input_hash text NOT NULL,
    rejected_output jsonb NOT NULL,
    diagnostics jsonb NOT NULL,
    configuration_before jsonb NOT NULL,
    configuration_after jsonb NOT NULL,
    carried_output_tokens integer NOT NULL CHECK (carried_output_tokens >= 0),
    evidence_reference text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(job_id,source_after)
);
CREATE FUNCTION preserve_clock_actor_rejection() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'Clock Actor rejection evidence is immutable';
END;
$$;
CREATE TRIGGER immutable_clock_actor_rejection BEFORE UPDATE OR DELETE
ON clock_runtime_actor_rejections FOR EACH ROW EXECUTE FUNCTION preserve_clock_actor_rejection();
