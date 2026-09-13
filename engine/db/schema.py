"""
SQLite schema for the TradingIntelligence evidence database.

Design principle (from the architecture docs): every extracted fact is an
ATOMIC, TRACEABLE record. Nothing is summarized-and-forgotten -- every row
knows exactly which file, timestamp, page, or cell it came from.
"""

import sqlite3
from pathlib import Path

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS files (
    file_id         TEXT PRIMARY KEY,      -- sha256 hash, stable identity
    course_id       TEXT NOT NULL,
    original_path   TEXT NOT NULL,
    original_name   TEXT NOT NULL,
    source_type     TEXT NOT NULL,         -- local | youtube
    file_type       TEXT NOT NULL,         -- video | pdf | excel | pptx | image | other
    mime_type       TEXT,
    size_bytes      INTEGER,
    youtube_url     TEXT,                  -- populated when source_type = youtube
    youtube_video_id TEXT,
    status          TEXT NOT NULL DEFAULT 'PENDING',
                                            -- PENDING|PROCESSING|SUCCESS|PARTIAL|FAILED|NEEDS_REVIEW
    stage_completed TEXT,                  -- last completed pipeline stage (for resume/checkpoint)
    is_duplicate_of TEXT,                  -- file_id of the original, if this is a dup
    registered_at   TEXT NOT NULL,
    processed_at    TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id      TEXT PRIMARY KEY,
    file_id         TEXT NOT NULL REFERENCES files(file_id),
    course_id       TEXT NOT NULL,
    session_type    TEXT NOT NULL,         -- main | supporting
    title           TEXT,
    duration_seconds REAL,
    language_notes  TEXT                   -- e.g. 'mixed hindi/english'
);

CREATE TABLE IF NOT EXISTS transcript_segments (
    segment_id      TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL REFERENCES sessions(session_id),
    start_seconds   REAL NOT NULL,
    end_seconds     REAL NOT NULL,
    speaker_cluster TEXT,                  -- diarization cluster label, e.g. 'SPEAKER_00'
    speaker_role    TEXT,                  -- 'trainer' | 'participant' | 'unknown' (confirmed by human once/course)
    text            TEXT NOT NULL,
    confidence      REAL,                  -- 0-1, from the transcription engine
    needs_review    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS key_frames (
    frame_id        TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL REFERENCES sessions(session_id),
    timestamp_seconds REAL NOT NULL,
    image_path      TEXT NOT NULL,
    image_hash      TEXT,
    ocr_text        TEXT,
    trigger_reason  TEXT                   -- scene_change | periodic | rule_keyword | manual
);

CREATE TABLE IF NOT EXISTS document_extractions (
    extraction_id   TEXT PRIMARY KEY,
    file_id         TEXT NOT NULL REFERENCES files(file_id),
    location_ref    TEXT NOT NULL,         -- page number / slide number / sheet!cell-range
    extraction_type TEXT NOT NULL,         -- text | table | formula | embedded_image | value
    content         TEXT,
    confidence      REAL
);

CREATE TABLE IF NOT EXISTS evidence (
    evidence_id     TEXT PRIMARY KEY,
    course_id       TEXT NOT NULL,
    source_kind     TEXT NOT NULL,         -- transcript_segment | key_frame | document_extraction
    source_ref_id   TEXT NOT NULL,         -- FK into the relevant table above
    topic_tags      TEXT,                  -- comma-separated candidate topic/strategy tags
    related_strategy_id TEXT,
    confidence      REAL,
    review_status   TEXT NOT NULL DEFAULT 'UNREVIEWED',  -- UNREVIEWED|OK|NEEDS_REVIEW|REJECTED
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS strategies (
    strategy_id     TEXT PRIMARY KEY,
    course_id       TEXT NOT NULL,
    name            TEXT,
    maturity        TEXT NOT NULL DEFAULT 'S0_UNPROCESSED',
    spec_json       TEXT,                  -- the full 20-field structured spec
    approved        INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    updated_at      TEXT
);

CREATE INDEX IF NOT EXISTS idx_evidence_course ON evidence(course_id);
CREATE INDEX IF NOT EXISTS idx_evidence_strategy ON evidence(related_strategy_id);
CREATE INDEX IF NOT EXISTS idx_files_course ON files(course_id);
CREATE INDEX IF NOT EXISTS idx_transcript_session ON transcript_segments(session_id);
"""


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    cols = [row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    return column in cols


def migrate_schema(conn: sqlite3.Connection) -> None:
    """
    Adds columns that were introduced after a database may already exist,
    so upgrading the app doesn't require deleting your existing course
    data. Each check is a cheap PRAGMA lookup, safe to call on every
    connection. Add new migrations here as ALTER TABLE ... ADD COLUMN --
    SQLite has no "ADD COLUMN IF NOT EXISTS", hence the explicit check.
    """
    base_table_exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='evidence'"
    ).fetchone()
    if not base_table_exists:
        return  # brand-new DB -- init_db's executescript will create everything fresh, nothing to migrate yet
    if not _column_exists(conn, "evidence", "content_type"):
        # Upgrade 4 (trainer / Q&A / off-topic classification): populated by
        # the tagging stage for transcript-derived evidence, defaulted to
        # 'material_reference' for document-derived evidence (see
        # tagging_processor.py). NULL means "not yet classified" for rows
        # that existed before this migration ran.
        conn.execute("ALTER TABLE evidence ADD COLUMN content_type TEXT")
    if not _column_exists(conn, "strategies", "confidence_category"):
        # Upgrade 5: HIGH / MEDIUM / LOW / CONFLICT, set by quality_check.py.
        # Persisted here (not just in the review_report.json file) so the
        # dashboard and course summary can query it directly.
        conn.execute("ALTER TABLE strategies ADD COLUMN confidence_category TEXT")
    if not _column_exists(conn, "strategies", "version"):
        # Upgrade 7 (strategy versioning): current version number,
        # incremented each time reconstruction meaningfully changes the
        # spec instead of silently overwriting it (see
        # reconstruction_processor.py and the new strategy_versions table).
        conn.execute("ALTER TABLE strategies ADD COLUMN version INTEGER NOT NULL DEFAULT 1")
    if not _column_exists(conn, "evidence", "conflict_group_id"):
        # Upgrade 5/8 (conflict detection): rows sharing a non-null
        # conflict_group_id are evidence the quality-check stage found
        # stating apparently different things about the same strategy.
        conn.execute("ALTER TABLE evidence ADD COLUMN conflict_group_id TEXT")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS strategy_versions (
            version_id      TEXT PRIMARY KEY,
            strategy_id     TEXT NOT NULL REFERENCES strategies(strategy_id),
            version         INTEGER NOT NULL,
            spec_json       TEXT NOT NULL,
            change_reason   TEXT,           -- why this version exists, e.g. "later session clarified stop rule"
            created_at      TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_strategy_versions_strategy ON strategy_versions(strategy_id);
    """)
    conn.commit()


def get_connection(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    migrate_schema(conn)
    return conn


def init_db(db_path: str) -> None:
    conn = get_connection(db_path)
    conn.executescript(SCHEMA_SQL)
    migrate_schema(conn)
    conn.commit()
    conn.close()


if __name__ == "__main__":
    init_db("TradingIntelligence.db")
    print("Database initialized -> TradingIntelligence.db")
