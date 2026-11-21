DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'review_editor') THEN
        CREATE ROLE review_editor LOGIN PASSWORD 'review-local-editor';
    END IF;
END
$$;

CREATE TABLE review_draft (
    exercise_id text PRIMARY KEY REFERENCES exercise (id),
    version bigint NOT NULL DEFAULT 0 CHECK (version >= 0),
    source_file_ids bigint[] NOT NULL,
    glossary jsonb NOT NULL
);

CREATE TABLE review_event (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    exercise_id text NOT NULL REFERENCES review_draft (exercise_id),
    source_file_id bigint NOT NULL,
    ordinal integer NOT NULL,
    kind text NOT NULL CHECK (kind IN ('status_report', 'acknowledgment', 'correction')),
    subject_id text,
    reported_status text CHECK (reported_status IN ('in_service', 'out_of_service', 'unknown')),
    state text NOT NULL DEFAULT 'proposed' CHECK (state IN ('proposed', 'accepted', 'rejected')),
    target_id bigint,
    UNIQUE (exercise_id, source_file_id, ordinal),
    UNIQUE (exercise_id, id),
    FOREIGN KEY (source_file_id, ordinal) REFERENCES source_record (source_file_id, ordinal),
    FOREIGN KEY (exercise_id, target_id) REFERENCES review_event (exercise_id, id),
    CHECK (id <> target_id),
    CHECK (kind <> 'status_report' OR (reported_status IS NOT NULL AND target_id IS NULL)),
    CHECK (kind <> 'acknowledgment' OR reported_status IS NULL)
);

CREATE INDEX review_event_target ON review_event (exercise_id, target_id) WHERE target_id IS NOT NULL;

CREATE TABLE review_transcript (
    exercise_id text NOT NULL REFERENCES review_draft (exercise_id),
    source_file_id bigint NOT NULL,
    ordinal integer NOT NULL,
    body text NOT NULL CHECK (length(body) > 0),
    PRIMARY KEY (exercise_id, source_file_id, ordinal),
    FOREIGN KEY (source_file_id, ordinal) REFERENCES source_record (source_file_id, ordinal)
);

CREATE TABLE review_revision (
    exercise_id text NOT NULL REFERENCES review_draft (exercise_id),
    number integer NOT NULL CHECK (number > 0),
    draft_version bigint NOT NULL,
    published_at timestamptz NOT NULL DEFAULT now(),
    snapshot jsonb NOT NULL,
    PRIMARY KEY (exercise_id, number),
    UNIQUE (exercise_id, draft_version)
);

GRANT SELECT ON exercise, exercise_source, source_file, source_record TO review_editor;
GRANT SELECT, INSERT, UPDATE ON review_draft, review_event, review_transcript TO review_editor;
GRANT USAGE ON SEQUENCE review_event_id_seq TO review_editor;
GRANT SELECT, INSERT ON review_revision TO review_editor;
GRANT SELECT ON review_draft, review_event, review_transcript, review_revision TO review_reader;
