ALTER TABLE catalog_source ADD COLUMN registered_at timestamptz;

UPDATE catalog_source AS source
SET registered_at = COALESCE(
    LEAST(
        (SELECT min(captured_at) FROM catalog_capture AS capture
         WHERE capture.exercise_id = source.exercise_id AND capture.source_key = source.source_key),
        (SELECT min(attempted_at) FROM catalog_attempt AS attempt
         WHERE attempt.exercise_id = source.exercise_id AND attempt.source_key = source.source_key)
    ),
    clock_timestamp()
);

ALTER TABLE catalog_source ALTER COLUMN registered_at SET DEFAULT clock_timestamp();
ALTER TABLE catalog_source ALTER COLUMN registered_at SET NOT NULL;

CREATE TABLE history_change_clock (
    exercise_id text PRIMARY KEY REFERENCES exercise (id),
    sequence bigint NOT NULL CHECK (sequence > 0)
);

CREATE TABLE history_change (
    exercise_id text NOT NULL REFERENCES exercise (id),
    sequence bigint NOT NULL CHECK (sequence > 0),
    event text NOT NULL CHECK (event IN ('catalog.derived', 'chat.classified', 'registry.published', 'layer.published')),
    ref text NOT NULL,
    version integer NOT NULL CHECK (version > 0),
    available_at timestamptz NOT NULL,
    PRIMARY KEY (exercise_id, sequence)
);

WITH previous AS (
    SELECT c.exercise_id, 'catalog.derived'::text AS event, d.capture_id::text AS ref,
           d.parser_version AS version, d.derived_at AS available_at
    FROM catalog_derivation AS d JOIN catalog_capture AS c ON c.id = d.capture_id
    UNION ALL
    SELECT exercise_id, 'chat.classified', id::text, version, classified_at
    FROM live_chat_version WHERE classified_at IS NOT NULL
    UNION ALL
    SELECT exercise_id, 'registry.published', 'registry', version, reviewed_at FROM history_registry
    UNION ALL
    SELECT exercise_id, 'layer.published', layer_key, version, recorded_at FROM history_layer
)
INSERT INTO history_change (exercise_id, sequence, event, ref, version, available_at)
SELECT exercise_id, row_number() OVER (
    PARTITION BY exercise_id ORDER BY available_at, event, ref, version
), event, ref, version, available_at
FROM previous;

INSERT INTO history_change_clock (exercise_id, sequence)
SELECT exercise_id, max(sequence) FROM history_change GROUP BY exercise_id;

CREATE FUNCTION append_history_change() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public
AS $$
DECLARE
    target_exercise text;
    change_event text;
    change_ref text;
    change_version integer;
    change_at timestamptz;
    change_sequence bigint;
BEGIN
    CASE TG_TABLE_NAME
        WHEN 'catalog_derivation' THEN
            SELECT exercise_id INTO STRICT target_exercise FROM public.catalog_capture WHERE id = NEW.capture_id;
            change_event := 'catalog.derived';
            change_ref := NEW.capture_id::text;
            change_version := NEW.parser_version;
            change_at := NEW.derived_at;
        WHEN 'live_chat_version' THEN
            target_exercise := NEW.exercise_id;
            change_event := 'chat.classified';
            change_ref := NEW.id::text;
            change_version := NEW.version;
            change_at := NEW.classified_at;
        WHEN 'history_registry' THEN
            target_exercise := NEW.exercise_id;
            change_event := 'registry.published';
            change_ref := 'registry';
            change_version := NEW.version;
            change_at := NEW.reviewed_at;
        WHEN 'history_layer' THEN
            target_exercise := NEW.exercise_id;
            change_event := 'layer.published';
            change_ref := NEW.layer_key;
            change_version := NEW.version;
            change_at := NEW.recorded_at;
    END CASE;

    -- The exercise row lock orders committed changes. A rolled-back transaction consumes no sequence.
    INSERT INTO public.history_change_clock (exercise_id, sequence) VALUES (target_exercise, 1)
    ON CONFLICT (exercise_id) DO UPDATE SET sequence = history_change_clock.sequence + 1
    RETURNING sequence INTO change_sequence;

    INSERT INTO public.history_change (exercise_id, sequence, event, ref, version, available_at)
    VALUES (target_exercise, change_sequence, change_event, change_ref, change_version, change_at);
    RETURN NEW;
END;
$$;

REVOKE EXECUTE ON FUNCTION append_history_change() FROM PUBLIC;

CREATE TRIGGER history_catalog_change AFTER INSERT ON catalog_derivation
FOR EACH ROW EXECUTE FUNCTION append_history_change();

CREATE TRIGGER history_chat_change AFTER UPDATE OF classified_at ON live_chat_version
FOR EACH ROW WHEN (OLD.classified_at IS NULL AND NEW.classified_at IS NOT NULL)
EXECUTE FUNCTION append_history_change();

CREATE TRIGGER history_registry_change AFTER INSERT ON history_registry
FOR EACH ROW EXECUTE FUNCTION append_history_change();

CREATE TRIGGER history_layer_change AFTER INSERT ON history_layer
FOR EACH ROW EXECUTE FUNCTION append_history_change();

GRANT SELECT ON history_change, history_change_clock TO review_reader;
