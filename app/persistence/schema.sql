-- Team SQLite schema. Mirrors PRD §7.
-- JSON columns are stored as TEXT (json.dumps / json.loads at the repo boundary).
-- Datetimes are stored as ISO-8601 strings (timezone-aware UTC).

CREATE TABLE IF NOT EXISTS persona (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    handle          TEXT NOT NULL UNIQUE,
    color           TEXT NOT NULL,
    job             TEXT NOT NULL DEFAULT '',     -- short role label shown by the name
    provider        TEXT NOT NULL,
    model           TEXT NOT NULL,
    effort          TEXT,
    system_prompt   TEXT NOT NULL DEFAULT '',
    mcp_servers     TEXT NOT NULL DEFAULT '[]',   -- json array
    allowed_tools   TEXT NOT NULL DEFAULT '[]',   -- json array
    working_dir     TEXT,
    permission_mode TEXT NOT NULL,
    is_template     INTEGER NOT NULL DEFAULT 0,    -- bool
    created_at      TEXT NOT NULL                  -- iso datetime
);

CREATE TABLE IF NOT EXISTS human_author (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    color           TEXT NOT NULL,
    weight_note     TEXT NOT NULL DEFAULT '',
    weight_enabled  INTEGER NOT NULL DEFAULT 1     -- bool
);

CREATE TABLE IF NOT EXISTS room (
    id                 TEXT PRIMARY KEY,
    name               TEXT NOT NULL,
    topic              TEXT NOT NULL DEFAULT '',
    default_reply_mode TEXT NOT NULL,
    delegation_enabled INTEGER NOT NULL DEFAULT 1, -- bool: personas may delegate
    archived           INTEGER NOT NULL DEFAULT 0, -- bool
    created_at         TEXT NOT NULL               -- iso datetime
);

-- Room membership: ordered set of personas in a room.
CREATE TABLE IF NOT EXISTS room_persona (
    room_id      TEXT NOT NULL,
    persona_id   TEXT NOT NULL,
    "order"      INTEGER NOT NULL,
    PRIMARY KEY (room_id, persona_id),
    FOREIGN KEY (room_id)    REFERENCES room(id)    ON DELETE CASCADE,
    FOREIGN KEY (persona_id) REFERENCES persona(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS message (
    id          TEXT PRIMARY KEY,
    room_id     TEXT NOT NULL,
    author_kind TEXT NOT NULL,                     -- 'human' | 'persona'
    author_ref  TEXT NOT NULL,                     -- human_author.id or persona.id
    content     TEXT NOT NULL,
    run_id      TEXT,
    created_at  TEXT NOT NULL,                      -- iso datetime
    FOREIGN KEY (room_id) REFERENCES room(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_message_room_created
    ON message(room_id, created_at, id);

-- Quote-reply links. Composite PK; deleting a message removes its quote links.
CREATE TABLE IF NOT EXISTS message_quote (
    message_id        TEXT NOT NULL,
    quoted_message_id TEXT NOT NULL,
    PRIMARY KEY (message_id, quoted_message_id),
    FOREIGN KEY (message_id)        REFERENCES message(id) ON DELETE CASCADE,
    FOREIGN KEY (quoted_message_id) REFERENCES message(id) ON DELETE CASCADE
);

-- One resumable harness session per (room, persona).
CREATE TABLE IF NOT EXISTS persona_session (
    room_id              TEXT NOT NULL,
    persona_id           TEXT NOT NULL,
    provider             TEXT NOT NULL,
    harness_session_id   TEXT,
    last_seen_message_id TEXT,
    status               TEXT NOT NULL DEFAULT 'idle',
    PRIMARY KEY (room_id, persona_id),
    FOREIGN KEY (room_id)    REFERENCES room(id)    ON DELETE CASCADE,
    FOREIGN KEY (persona_id) REFERENCES persona(id) ON DELETE CASCADE
);

-- run_record INTENTIONALLY has no FOREIGN KEY / ON DELETE CASCADE to room or
-- persona (unlike room_persona, message, message_quote, persona_session above).
-- These are audit/observability records: a run's history must survive deletion
-- of the room or persona it referenced, so the asymmetry is by design, not an
-- oversight. room_id/persona_id are kept as plain TEXT for lookup/indexing only.
CREATE TABLE IF NOT EXISTS run_record (
    run_id           TEXT PRIMARY KEY,
    room_id          TEXT NOT NULL,
    persona_id       TEXT NOT NULL,
    command_redacted TEXT NOT NULL,
    exit_code        INTEGER,
    usage            TEXT NOT NULL DEFAULT '{}',    -- json object
    log_path         TEXT,
    error_kind       TEXT,
    started_at       TEXT NOT NULL,                 -- iso datetime
    finished_at      TEXT
);

CREATE INDEX IF NOT EXISTS idx_run_record_room_persona
    ON run_record(room_id, persona_id);
