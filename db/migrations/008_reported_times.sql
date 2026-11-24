ALTER TABLE review_event DROP CONSTRAINT review_event_kind_check;
ALTER TABLE review_event ADD COLUMN reported_time_seconds integer
    CHECK (reported_time_seconds BETWEEN 0 AND 3599999);
ALTER TABLE review_event ADD CONSTRAINT review_event_kind_check
    CHECK (kind IN ('status_report', 'time_report', 'acknowledgment', 'correction'));
ALTER TABLE review_event ADD CONSTRAINT review_event_reported_time_check CHECK (
    (kind <> 'time_report' OR (reported_time_seconds IS NOT NULL AND target_id IS NULL))
    AND (reported_time_seconds IS NULL OR (
        kind IN ('time_report', 'correction') AND subject_id IS NOT NULL AND reported_status IS NULL
    ))
);
