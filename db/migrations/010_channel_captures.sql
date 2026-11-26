CREATE TABLE channel_capture (
    capture_id bigint PRIMARY KEY,
    exercise_id text NOT NULL,
    source_key text NOT NULL,
    item_key text NOT NULL,
    item_version integer NOT NULL CHECK (item_version > 0),
    observed_at timestamptz NOT NULL,
    sender text NOT NULL,
    payload_sha256 text NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
    audio_digest text CHECK (audio_digest ~ '^[0-9a-f]{64}$'),
    duration_ms integer CHECK (duration_ms BETWEEN 1 AND 30000),
    CHECK ((audio_digest IS NULL) = (duration_ms IS NULL)),
    UNIQUE (exercise_id, source_key, item_key, item_version),
    FOREIGN KEY (exercise_id, source_key, capture_id)
        REFERENCES catalog_capture (exercise_id, source_key, id)
);

CREATE INDEX channel_capture_audio ON channel_capture (capture_id) WHERE audio_digest IS NOT NULL;
GRANT SELECT ON channel_capture TO review_reader, review_editor;
GRANT INSERT, UPDATE (duration_ms) ON channel_capture TO review_editor;
