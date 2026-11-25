ALTER TABLE review_event DROP CONSTRAINT review_event_exercise_id_source_file_id_ordinal_key;
ALTER TABLE review_event DROP CONSTRAINT review_event_kind_check;
ALTER TABLE review_event ADD COLUMN clause smallint NOT NULL DEFAULT 0 CHECK (clause IN (-1, 0));
ALTER TABLE review_event ADD COLUMN radio_speed_limit jsonb;
ALTER TABLE review_event ADD CONSTRAINT review_event_source_clause_key
    UNIQUE (exercise_id, source_file_id, ordinal, clause);
ALTER TABLE review_event ADD CONSTRAINT review_event_kind_check
    CHECK (kind IN ('status_report', 'time_report', 'radio_report', 'acknowledgment', 'correction'));
ALTER TABLE review_event ADD CONSTRAINT review_event_radio_check CHECK (
    (kind = 'radio_report') = (clause = -1)
    AND (kind <> 'radio_report' OR (radio_speed_limit IS NOT NULL AND target_id IS NULL))
    AND (radio_speed_limit IS NULL OR (
        kind IN ('radio_report', 'correction', 'acknowledgment')
        AND reported_status IS NULL AND reported_time_seconds IS NULL
        AND jsonb_typeof(radio_speed_limit) = 'object'
    ))
);
