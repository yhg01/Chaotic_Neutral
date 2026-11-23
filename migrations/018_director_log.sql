-- The live world's memory on clocked runs: lexical retrieval with no embedding cost.
-- Window runs never write here. Board text never enters this table.
CREATE TABLE director_log (
    id uuid PRIMARY KEY,
    -- Reading order inside one story second. This is not a delivery cursor.
    seq bigint GENERATED ALWAYS AS IDENTITY,
    run_id uuid NOT NULL REFERENCES environment_runs(id),
    sim_s bigint NOT NULL CHECK (sim_s >= 0),
    kind text NOT NULL CHECK (length(kind) BETWEEN 1 AND 80),
    owner_id text,
    title text NOT NULL DEFAULT '' CHECK (length(title) <= 300),
    body text NOT NULL DEFAULT '' CHECK (length(body) <= 4000),
    meta jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(meta) = 'object'),
    tsv tsvector GENERATED ALWAYS AS (
        setweight(to_tsvector('english', title), 'A')
        || setweight(to_tsvector('english', body), 'B')
    ) STORED,
    FOREIGN KEY (run_id, owner_id) REFERENCES run_participants(run_id, owner_id)
);
CREATE INDEX director_log_recent ON director_log(run_id, sim_s DESC, seq DESC);
CREATE INDEX director_log_owner ON director_log(run_id, owner_id, sim_s DESC, seq DESC);
CREATE INDEX director_log_search ON director_log USING gin(tsv);

-- Top-level plain values as "key: value" lines. Keys that describe the machinery are
-- dropped, because a language model reads this text.
CREATE FUNCTION director_log_plain(doc jsonb) RETURNS text
LANGUAGE sql IMMUTABLE AS $$
    SELECT COALESCE(string_agg(key || ': ' || left(value #>> '{}', 400), E'\n' ORDER BY key), '')
    FROM jsonb_each(CASE WHEN jsonb_typeof(doc) = 'object' THEN doc ELSE '{}'::jsonb END)
    WHERE jsonb_typeof(value) IN ('string', 'number', 'boolean')
      AND key NOT IN ('message_id', 'thread_id')
      AND key !~* '(^|_)(window|tick|run|beat|cue|director|actor|scenario|simulation|sim|sequence|hash|token|request|cause|operation|key|version|source)(_|$)'
$$;

-- Default deny for text: a new event kind gets a title only until it is listed below, so
-- it cannot carry board or tool text into the log by accident.
CREATE FUNCTION director_log_from_event() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    now_s bigint;
    heading text;
    content text := '';
BEGIN
    SELECT sim_s INTO now_s FROM clock_state WHERE run_id = NEW.run_id;
    IF now_s IS NULL THEN
        RETURN NULL;
    END IF;
    -- Tool traces and activation records can quote the board. Bookkeeping has no story.
    IF NEW.kind ~ '(board|direct_message|tool_call|activation|task_context|task_stalled|simulator|infrastructure|job_|window|run_|recovery|trial_|operator|console|control_audit|boundary|lifecycle_check|director_|^agent_|_handled$|beat|tick|scenario|simulation|clock|epoch|^process_|^action_replaced$)'
        OR (NEW.kind ~ '^actor_' AND NEW.kind NOT IN ('actor_cue', 'actor_turn')) THEN
        RETURN NULL;
    END IF;
    CASE
    WHEN NEW.kind = 'actor_cue' THEN
        heading := 'Something this person just learned';
        content := COALESCE(NEW.payload->>'cue', '');
    WHEN NEW.kind = 'owner_message' THEN
        heading := CASE WHEN NEW.payload->>'source' = 'owner'
            THEN 'This person told their assistant' ELSE 'Their assistant told them' END;
        content := COALESCE(NEW.payload->>'content', '');
    WHEN NEW.kind = 'actor_turn' THEN
        heading := 'What this person set out to do';
        content := concat_ws(E'\n',
            NEW.payload #>> '{output,next_action,description}',
            NEW.payload #>> '{output,next_action,seeks}',
            (SELECT string_agg(item, E'\n') FROM jsonb_array_elements_text(
                CASE WHEN jsonb_typeof(NEW.payload #> '{output,director_requests}') = 'array'
                THEN NEW.payload #> '{output,director_requests}' ELSE '[]'::jsonb END) AS item));
        IF content = '' THEN
            RETURN NULL;
        END IF;
    WHEN NEW.kind = 'inbox_received' THEN
        heading := 'Mail: ' || COALESCE(NEW.payload->>'subject', '(no subject)');
        SELECT m.body INTO content FROM inbox_messages m
            WHERE m.id = (NEW.payload->>'message_id')::uuid;
    WHEN NEW.kind = 'resource_updated' THEN
        SELECT 'Record changed: ' || r.title, r.content INTO heading, content
            FROM owner_resources r
            WHERE r.run_id = NEW.run_id AND r.resource_id = NEW.payload->>'resource_id';
        heading := COALESCE(heading, 'A record changed');
    WHEN NEW.kind = 'world_changed' AND NEW.payload->>'kind' = 'balance_changed' THEN
        heading := 'Money changed: ' || COALESCE(NEW.payload->>'memo', '');
        content := 'Change: ' || COALESCE(NEW.payload->>'delta', '?')
            || '. Balance now: ' || COALESCE(NEW.payload->>'balance', '?') || '.';
    WHEN NEW.kind = 'assistant_expired' THEN
        heading := 'Their assistant service has ended';
        content := director_log_plain(NEW.payload);
    WHEN NEW.kind LIKE 'site\_%' OR NEW.kind LIKE 'web\_%' OR NEW.kind LIKE 'order\_%'
        OR NEW.kind LIKE 'listing\_%' OR NEW.kind LIKE 'wallet\_%' OR NEW.kind LIKE 'bill\_%'
        OR NEW.kind LIKE 'market\_%' OR NEW.kind LIKE 'shop\_%' OR NEW.kind LIKE 'transfer\_%'
        OR NEW.kind LIKE 'shopping\_%' OR NEW.kind LIKE 'reservation\_%'
        OR NEW.kind LIKE 'world\_%' OR NEW.kind LIKE '%consent\_changed' THEN
        heading := initcap(replace(NEW.kind, '_', ' '));
        content := concat_ws(E'\n',
            NULLIF(director_log_plain(NEW.payload), ''),
            NULLIF(director_log_plain(NEW.payload->'fields'), ''));
    ELSE
        heading := initcap(replace(NEW.kind, '_', ' '));
    END CASE;
    INSERT INTO director_log(id, run_id, sim_s, kind, owner_id, title, body, meta)
    VALUES (
        NEW.id, NEW.run_id, now_s, NEW.kind, NEW.owner_id,
        left(heading, 300), left(COALESCE(content, ''), 1500),
        jsonb_build_object('event_id', NEW.id, 'operation_key', NEW.operation_key)
    )
    ON CONFLICT (id) DO NOTHING;
    RETURN NULL;
END $$;

CREATE TRIGGER run_events_director_log
    AFTER INSERT ON run_events
    FOR EACH ROW
    WHEN (NEW.kind NOT IN ('board_posted', 'board_batch'))
    EXECUTE FUNCTION director_log_from_event();
