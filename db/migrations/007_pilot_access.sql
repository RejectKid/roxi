CREATE TABLE pilot_workspace (
    id text PRIMARY KEY,
    name text NOT NULL,
    retention_days integer CHECK (retention_days BETWEEN 1 AND 3650)
);

CREATE TABLE pilot_account (
    username text PRIMARY KEY,
    password_hash text NOT NULL
);

CREATE TABLE pilot_member (
    workspace_id text REFERENCES pilot_workspace (id) ON DELETE CASCADE,
    username text REFERENCES pilot_account (username) ON DELETE CASCADE,
    role text NOT NULL CHECK (role IN ('admin', 'reviewer', 'reader', 'ingest')),
    all_sources boolean NOT NULL DEFAULT false,
    PRIMARY KEY (workspace_id, username)
);

CREATE TABLE pilot_exercise (
    exercise_id text PRIMARY KEY REFERENCES exercise (id) ON DELETE CASCADE,
    workspace_id text NOT NULL REFERENCES pilot_workspace (id),
    closed_at timestamptz,
    UNIQUE (workspace_id, exercise_id)
);

CREATE TABLE pilot_source_grant (
    workspace_id text,
    username text,
    exercise_id text,
    source text NOT NULL,
    PRIMARY KEY (workspace_id, username, exercise_id, source),
    FOREIGN KEY (workspace_id, username) REFERENCES pilot_member ON DELETE CASCADE,
    FOREIGN KEY (workspace_id, exercise_id) REFERENCES pilot_exercise (workspace_id, exercise_id) ON DELETE CASCADE
);

CREATE TABLE pilot_credential (
    digest text PRIMARY KEY,
    username text NOT NULL REFERENCES pilot_account (username) ON DELETE CASCADE,
    kind text NOT NULL CHECK (kind IN ('session', 'api')),
    expires_at timestamptz NOT NULL
);

CREATE INDEX pilot_credential_expiry ON pilot_credential (expires_at);

CREATE TABLE pilot_login_budget (
    peer text PRIMARY KEY,
    reset_at timestamptz NOT NULL,
    attempts integer NOT NULL
);

CREATE TABLE pilot_archive_gc (
    digest text PRIMARY KEY CHECK (digest ~ '^[a-f0-9]{64}$')
);

CREATE TABLE pilot_import_archive (
    exercise_id text REFERENCES exercise (id) ON DELETE CASCADE,
    digest text CHECK (digest ~ '^[a-f0-9]{64}$'),
    PRIMARY KEY (exercise_id, digest)
);

INSERT INTO pilot_import_archive
SELECT id, manifest_sha256 FROM exercise
UNION SELECT id, glossary_sha256 FROM exercise;

CREATE TABLE pilot_chat_heartbeat (
    exercise_id text PRIMARY KEY REFERENCES live_room (exercise_id) ON DELETE CASCADE,
    seen_at timestamptz NOT NULL
);

GRANT SELECT ON pilot_workspace, pilot_member, pilot_exercise, pilot_source_grant, pilot_credential TO review_reader;
GRANT SELECT ON pilot_account TO review_editor;
GRANT SELECT, INSERT, DELETE ON pilot_credential TO review_editor;
GRANT SELECT, INSERT, UPDATE, DELETE ON pilot_login_budget TO review_editor;
GRANT SELECT ON pilot_chat_heartbeat TO review_reader;
GRANT INSERT, UPDATE, SELECT ON pilot_chat_heartbeat TO review_editor;
