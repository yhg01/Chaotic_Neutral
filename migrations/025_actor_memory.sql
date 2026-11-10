-- Opt-in private cognition. Existing run, Clock, wallet and cost rows are unchanged.
CREATE TABLE actor_memory_profiles (
    run_id uuid PRIMARY KEY REFERENCES director_clock_bindings(run_id),
    configuration jsonb NOT NULL,
    configuration_hash text NOT NULL
);

CREATE TABLE actor_memories (
    id uuid PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES actor_memory_profiles(run_id),
    owner_id text NOT NULL,
    source_key text NOT NULL,
    source_job_id uuid NOT NULL REFERENCES director_clock_jobs(id),
    kind text NOT NULL CHECK (kind IN ('observation','thought')),
    text text NOT NULL CHECK (length(text) BETWEEN 1 AND 2000),
    provenance jsonb NOT NULL,
    evidence jsonb NOT NULL,
    importance integer NOT NULL CHECK (importance BETWEEN 1 AND 10),
    created_seconds integer NOT NULL CHECK (created_seconds >= 0),
    expires_seconds integer NOT NULL CHECK (expires_seconds > created_seconds),
    last_accessed_seconds integer NOT NULL CHECK (last_accessed_seconds >= created_seconds),
    supersedes_id uuid,
    UNIQUE(run_id,owner_id,id),
    UNIQUE(run_id,owner_id,source_key),
    FOREIGN KEY (run_id,owner_id) REFERENCES run_participants(run_id,owner_id),
    FOREIGN KEY (run_id,owner_id,supersedes_id) REFERENCES actor_memories(run_id,owner_id,id)
);
CREATE INDEX actor_memory_scope ON actor_memories(run_id,owner_id,expires_seconds);

CREATE TABLE actor_memory_snapshots (
    job_id uuid PRIMARY KEY REFERENCES director_clock_jobs(id),
    run_id uuid NOT NULL REFERENCES actor_memory_profiles(run_id),
    owner_id text NOT NULL,
    original_view_hash text NOT NULL,
    context jsonb NOT NULL,
    context_hash text NOT NULL,
    accepted_output jsonb,
    diagnostics jsonb NOT NULL DEFAULT '[]',
    output_tokens integer NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
    applied_seconds integer,
    FOREIGN KEY (run_id,owner_id) REFERENCES run_participants(run_id,owner_id)
);

CREATE TABLE actor_agendas (
    id uuid PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES actor_memory_profiles(run_id),
    owner_id text NOT NULL,
    source_job_id uuid NOT NULL REFERENCES actor_memory_snapshots(job_id),
    day date NOT NULL,
    slots jsonb NOT NULL,
    created_seconds integer NOT NULL CHECK (created_seconds >= 0),
    expires_seconds integer NOT NULL CHECK (expires_seconds > created_seconds),
    UNIQUE(run_id,owner_id,source_job_id,day),
    FOREIGN KEY (run_id,owner_id) REFERENCES run_participants(run_id,owner_id)
);
CREATE INDEX actor_agenda_scope ON actor_agendas(run_id,owner_id,day,created_seconds);

CREATE FUNCTION freeze_actor_memory_snapshot() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.job_id,NEW.run_id,NEW.owner_id,NEW.original_view_hash,NEW.context,NEW.context_hash)
       IS DISTINCT FROM
       (OLD.job_id,OLD.run_id,OLD.owner_id,OLD.original_view_hash,OLD.context,OLD.context_hash)
       OR (OLD.accepted_output IS NOT NULL AND
           (NEW.accepted_output,NEW.output_tokens,NEW.diagnostics) IS DISTINCT FROM
           (OLD.accepted_output,OLD.output_tokens,OLD.diagnostics))
       OR (OLD.applied_seconds IS NOT NULL AND NEW.applied_seconds IS DISTINCT FROM OLD.applied_seconds)
    THEN
        RAISE EXCEPTION 'Actor memory snapshot and accepted decision are immutable';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER immutable_actor_memory_snapshot BEFORE UPDATE
ON actor_memory_snapshots FOR EACH ROW EXECUTE FUNCTION freeze_actor_memory_snapshot();
