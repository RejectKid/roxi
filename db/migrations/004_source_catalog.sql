CREATE TABLE catalog_source (
    exercise_id text NOT NULL REFERENCES exercise (id),
    source_key text NOT NULL,
    label text NOT NULL,
    adapter_key text NOT NULL,
    stale_after_seconds integer NOT NULL CHECK (stale_after_seconds > 0),
    expected_ids text[] NOT NULL,
    nws_station text CHECK (nws_station ~ '^[A-Z0-9]{3,8}$'),
    UNIQUE (exercise_id, label),
    PRIMARY KEY (exercise_id, source_key)
);

CREATE TABLE catalog_capture (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    exercise_id text NOT NULL,
    source_key text NOT NULL,
    digest text NOT NULL CHECK (digest ~ '^[a-f0-9]{64}$'),
    byte_size integer NOT NULL CHECK (byte_size >= 0),
    original_name text NOT NULL,
    captured_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (exercise_id, source_key) REFERENCES catalog_source (exercise_id, source_key),
    UNIQUE (exercise_id, source_key, digest),
    UNIQUE (exercise_id, source_key, id)
);

CREATE TABLE catalog_derivation (
    capture_id bigint NOT NULL REFERENCES catalog_capture (id),
    parser_version integer NOT NULL CHECK (parser_version > 0),
    contract_version integer NOT NULL CHECK (contract_version > 0),
    input_schema text NOT NULL,
    state text NOT NULL CHECK (state IN ('accepted', 'partial', 'quarantined')),
    observed_at timestamptz,
    records jsonb NOT NULL CHECK (jsonb_typeof(records) = 'array'),
    issues jsonb NOT NULL CHECK (jsonb_typeof(issues) = 'array'),
    derived_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (capture_id, parser_version),
    CHECK ((state = 'quarantined') = (jsonb_array_length(records) = 0)),
    CHECK ((state = 'accepted') = (jsonb_array_length(issues) = 0)),
    CHECK ((state = 'quarantined') = (observed_at IS NULL))
);

CREATE TABLE catalog_attempt (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    exercise_id text NOT NULL,
    source_key text NOT NULL,
    channel text NOT NULL CHECK (channel IN ('file', 'nws')),
    attempted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    capture_id bigint,
    parser_version integer,
    failure text,
    FOREIGN KEY (exercise_id, source_key) REFERENCES catalog_source (exercise_id, source_key),
    FOREIGN KEY (exercise_id, source_key, capture_id) REFERENCES catalog_capture (exercise_id, source_key, id),
    FOREIGN KEY (capture_id, parser_version) REFERENCES catalog_derivation (capture_id, parser_version) MATCH FULL,
    CHECK ((capture_id IS NULL) = (failure IS NOT NULL))
);

CREATE INDEX catalog_attempt_latest ON catalog_attempt (exercise_id, source_key, id DESC);
CREATE INDEX catalog_attempt_fetched ON catalog_attempt (exercise_id, source_key, id DESC) WHERE capture_id IS NOT NULL;

GRANT SELECT ON catalog_source, catalog_capture, catalog_derivation, catalog_attempt TO review_reader, review_editor;
GRANT INSERT ON catalog_source, catalog_capture, catalog_derivation, catalog_attempt TO review_editor;
-- Row locking serializes capture/derivation commits for each source. The grant covers only the lock column.
GRANT UPDATE (stale_after_seconds) ON catalog_source TO review_editor;
GRANT USAGE ON SEQUENCE catalog_capture_id_seq, catalog_attempt_id_seq TO review_editor;
