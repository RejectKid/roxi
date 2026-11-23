ALTER TABLE live_chat_version ADD COLUMN sent_at_utc timestamptz;

UPDATE live_chat_version AS message
SET sent_at_utc = exercise.started_at_utc + message.sent_ms * interval '1 millisecond'
FROM exercise
WHERE exercise.id = message.exercise_id AND exercise.started_at_utc IS NOT NULL;

GRANT INSERT (sent_at_utc) ON live_chat_version TO review_editor;

CREATE TABLE history_registry (
    exercise_id text NOT NULL REFERENCES exercise (id),
    version integer NOT NULL CHECK (version > 0),
    reviewed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    digest text NOT NULL CHECK (digest ~ '^[a-f0-9]{64}$'),
    registry jsonb NOT NULL CHECK (jsonb_typeof(registry) = 'object'),
    PRIMARY KEY (exercise_id, version)
);

CREATE TABLE history_layer (
    exercise_id text NOT NULL REFERENCES exercise (id),
    layer_key text NOT NULL,
    version integer NOT NULL CHECK (version > 0),
    label text NOT NULL,
    effective_at timestamptz NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    digest text NOT NULL CHECK (digest ~ '^[a-f0-9]{64}$'),
    features jsonb NOT NULL CHECK (jsonb_typeof(features) = 'array'),
    PRIMARY KEY (exercise_id, layer_key, version)
);

CREATE INDEX catalog_capture_history ON catalog_capture (exercise_id, captured_at, id);
CREATE INDEX live_chat_history ON live_chat_version (exercise_id, received_at, id)
    WHERE classified_at IS NOT NULL;

GRANT SELECT ON history_registry, history_layer TO review_reader, review_editor;
GRANT INSERT ON history_registry, history_layer TO review_editor;
