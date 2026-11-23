CREATE TABLE live_room (
    exercise_id text PRIMARY KEY REFERENCES exercise (id),
    room_key text NOT NULL,
    watermark_ms bigint NOT NULL DEFAULT 0 CHECK (watermark_ms >= 0),
    change_seq bigint NOT NULL DEFAULT 0 CHECK (change_seq >= 0)
);

CREATE TABLE live_chat_version (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    exercise_id text NOT NULL REFERENCES live_room (exercise_id),
    message_id text NOT NULL,
    version integer NOT NULL CHECK (version > 0),
    sent_ms bigint NOT NULL CHECK (sent_ms >= 0),
    sender text NOT NULL,
    body text NOT NULL,
    original bytea NOT NULL CHECK (octet_length(original) BETWEEN 1 AND 16384),
    payload_sha256 text NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
    received_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    late boolean NOT NULL,
    change_seq bigint CHECK (change_seq > 0),
    classified_at timestamptz,
    kind text CHECK (kind IN ('status_report', 'acknowledgment', 'correction', 'chatter')),
    subject_id text,
    reported_status text CHECK (reported_status IN ('in_service', 'out_of_service', 'unknown')),
    UNIQUE (exercise_id, message_id, version),
    CHECK ((change_seq IS NULL AND classified_at IS NULL AND kind IS NULL)
        OR (change_seq IS NOT NULL AND classified_at IS NOT NULL AND kind IS NOT NULL))
);

CREATE INDEX live_chat_pending ON live_chat_version (exercise_id, id) WHERE change_seq IS NULL;
CREATE UNIQUE INDEX live_chat_changes ON live_chat_version (exercise_id, change_seq) WHERE change_seq IS NOT NULL;

GRANT SELECT ON live_room, live_chat_version TO review_reader, review_editor;
GRANT INSERT (exercise_id, room_key), UPDATE (watermark_ms, change_seq) ON live_room TO review_editor;
GRANT INSERT (exercise_id, message_id, version, sent_ms, sender, body, original, payload_sha256, late),
      UPDATE (change_seq, classified_at, kind, subject_id, reported_status) ON live_chat_version TO review_editor;
GRANT USAGE ON SEQUENCE live_chat_version_id_seq TO review_editor;
