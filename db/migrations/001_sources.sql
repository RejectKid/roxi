CREATE TABLE exercise (
    id text PRIMARY KEY,
    name text NOT NULL,
    started_at_utc timestamptz,
    manifest_sha256 text NOT NULL CHECK (manifest_sha256 ~ '^[a-f0-9]{64}$'),
    glossary_sha256 text NOT NULL CHECK (glossary_sha256 ~ '^[a-f0-9]{64}$'),
    glossary jsonb NOT NULL,
    imported_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE source_file (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    exercise_id text NOT NULL REFERENCES exercise (id),
    source_key text NOT NULL,
    kind text NOT NULL CHECK (kind IN ('audio', 'chat')),
    original_name text NOT NULL,
    content_sha256 text NOT NULL CHECK (content_sha256 ~ '^[a-f0-9]{64}$'),
    byte_size bigint NOT NULL CHECK (byte_size > 0),
    import_profile_sha256 text NOT NULL CHECK (import_profile_sha256 ~ '^[a-f0-9]{64}$'),
    transcript_name text,
    transcript_sha256 text CHECK (transcript_sha256 ~ '^[a-f0-9]{64}$'),
    exercise_offset_ms bigint CHECK (exercise_offset_ms >= 0),
    duration_ms bigint CHECK (duration_ms > 0),
    imported_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (exercise_id, source_key, content_sha256, import_profile_sha256),
    UNIQUE (exercise_id, source_key, id),
    CHECK (
        (kind = 'audio' AND transcript_name IS NOT NULL AND transcript_sha256 IS NOT NULL AND duration_ms IS NOT NULL)
        OR (kind = 'chat' AND transcript_name IS NULL AND transcript_sha256 IS NULL AND duration_ms IS NULL AND exercise_offset_ms IS NULL)
    )
);

CREATE TABLE exercise_source (
    exercise_id text NOT NULL,
    source_key text NOT NULL,
    source_file_id bigint NOT NULL,
    position integer NOT NULL CHECK (position > 0),
    PRIMARY KEY (exercise_id, source_key),
    UNIQUE (exercise_id, position),
    FOREIGN KEY (exercise_id, source_key, source_file_id) REFERENCES source_file (exercise_id, source_key, id)
);

CREATE TABLE source_record (
    source_file_id bigint NOT NULL REFERENCES source_file (id),
    ordinal integer NOT NULL CHECK (ordinal > 0),
    cue_id text,
    speaker text,
    body text NOT NULL,
    original_text text NOT NULL,
    capture_offset_ms bigint CHECK (capture_offset_ms >= 0),
    audio_start_ms bigint CHECK (audio_start_ms >= 0),
    audio_end_ms bigint,
    PRIMARY KEY (source_file_id, ordinal),
    CHECK (
        (audio_start_ms IS NULL AND audio_end_ms IS NULL AND capture_offset_ms IS NOT NULL)
        OR (audio_start_ms IS NOT NULL AND audio_end_ms IS NOT NULL AND audio_end_ms > audio_start_ms)
    )
);

CREATE INDEX source_record_timeline ON source_record (source_file_id, capture_offset_ms, ordinal);

CREATE TABLE import_issue (
    source_file_id bigint NOT NULL REFERENCES source_file (id),
    line_number integer NOT NULL CHECK (line_number > 0),
    reason text NOT NULL,
    original_text text NOT NULL,
    PRIMARY KEY (source_file_id, line_number)
);

GRANT SELECT ON exercise, source_file, exercise_source, source_record, import_issue TO review_reader;
