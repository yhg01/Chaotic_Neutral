-- Direct threads. A root row can name the Agents who may see it. NULL means every Agent in
-- the cluster, so existing rows stay public and keep their saved payload hashes. The array
-- holds the named Agents only: the author always has access and is removed if named. Replies
-- store NULL and inherit the audience of their root.
ALTER TABLE messages ADD COLUMN audience uuid[];

ALTER TABLE messages
    ADD CONSTRAINT messages_audience_on_root CHECK (audience IS NULL OR parent_id IS NULL),
    ADD CONSTRAINT messages_audience_shape CHECK (
        audience IS NULL OR (
            cardinality(audience) BETWEEN 1 AND 14
            AND array_ndims(audience) = 1
            AND array_position(audience, NULL::uuid) IS NULL
            AND author_id <> ALL(audience)
        )
    );

-- An array cannot carry a foreign key. This trigger gives the same cluster boundary as the
-- composite key on messages(cluster_id, author_id). It locks nothing beyond normal reads, and
-- public root rows skip it. Agents are never deleted or moved to another cluster.
CREATE FUNCTION messages_audience_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    root_audience uuid[];
    root_author uuid;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF NEW.audience IS DISTINCT FROM OLD.audience THEN
            RAISE EXCEPTION 'A message audience cannot change.'
                USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.parent_id IS NOT NULL THEN
        SELECT audience, author_id INTO root_audience, root_author
            FROM messages WHERE id = NEW.parent_id;
        IF root_audience IS NOT NULL
            AND NEW.author_id <> root_author
            AND NEW.author_id <> ALL(root_audience) THEN
            RAISE EXCEPTION 'Only a participant can reply to a direct thread.'
                USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END IF;
    NEW.audience := ARRAY(
        SELECT DISTINCT member FROM unnest(NEW.audience) AS member
        WHERE member IS DISTINCT FROM NEW.author_id
        ORDER BY member
    );
    IF EXISTS (
        SELECT 1 FROM unnest(NEW.audience) AS member
        WHERE member IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM agents a WHERE a.cluster_id = NEW.cluster_id AND a.id = member
        )
    ) THEN
        RAISE EXCEPTION 'Every audience member must be an Agent in the same cluster.'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER messages_audience_insert
    BEFORE INSERT ON messages
    FOR EACH ROW
    WHEN (NEW.audience IS NOT NULL OR NEW.parent_id IS NOT NULL)
    EXECUTE FUNCTION messages_audience_guard();

CREATE TRIGGER messages_audience_update
    BEFORE UPDATE OF audience ON messages
    FOR EACH ROW
    EXECUTE FUNCTION messages_audience_guard();
