"""
Course manager.

Course status is DERIVED from the database, not stored separately -- this
avoids a status field ever drifting out of sync with what's actually been
done. A course's status is computed fresh every time it's asked for.
"""

import shutil
from pathlib import Path

COURSE_SUBFOLDERS = ["Main_Sessions", "Supporting_Sessions", "PDFs", "Excel", "Docs", "Screenshots", "Other"]
OUTPUT_STAGE_FOLDERS = [
    "01_VIDEO_EVIDENCE/Transcripts", "01_VIDEO_EVIDENCE/Audio", "01_VIDEO_EVIDENCE/Key_Frames", "01_VIDEO_EVIDENCE/OCR",
    "02_MATERIAL_EVIDENCE/PDF_Extraction", "02_MATERIAL_EVIDENCE/Excel_Analysis", "02_MATERIAL_EVIDENCE/Screenshot_OCR",
    "02_MATERIAL_EVIDENCE/PPTX_Extraction", "02_MATERIAL_EVIDENCE/DOCX_Extraction",
    "03_EVIDENCE_LINKING", "04_STRATEGY_RECONSTRUCTION", "05_REVIEW", "06_FINAL_STRATEGIES",
]


def create_course_skeleton(course_name: str, raw_courses_dir: str, output_courses_dir: str) -> dict:
    """Creates the standard folder skeleton for a new course. Safe to call
    on a name that already exists -- won't overwrite anything inside."""
    raw_root = Path(raw_courses_dir) / course_name
    output_root = Path(output_courses_dir) / course_name

    for sub in COURSE_SUBFOLDERS:
        (raw_root / sub).mkdir(parents=True, exist_ok=True)
    for sub in OUTPUT_STAGE_FOLDERS:
        (output_root / sub).mkdir(parents=True, exist_ok=True)

    return {"course_name": course_name, "raw_path": str(raw_root), "output_path": str(output_root)}


def get_course_status(conn, course_id: str) -> str:
    """
    Returns one of:
      NOT_STARTED           -- no files registered yet
      EXTRACTED_AWAITING_AI -- extraction done, no strategies linked/reconstructed yet
      AI_IN_PROGRESS        -- some strategies linked but not all reconstructed
      NEEDS_REVIEW          -- strategies reconstructed, at least one not yet approved
      COMPLETE               -- every strategy approved
    """
    file_count = conn.execute(
        "SELECT COUNT(*) FROM files WHERE course_id = ?", (course_id,)
    ).fetchone()[0]
    if file_count == 0:
        return "NOT_STARTED"

    all_success = conn.execute(
        "SELECT COUNT(*) FROM files WHERE course_id = ? AND status != 'SUCCESS'", (course_id,)
    ).fetchone()[0] == 0

    strategy_rows = conn.execute(
        "SELECT maturity, approved FROM strategies WHERE course_id = ?", (course_id,)
    ).fetchall()

    if not strategy_rows:
        # this was a real bug: both branches used to return the same
        # value, so "Extracted — Awaiting AI" showed even while a course
        # was still mid-download/mid-transcription, not actually done yet.
        return "EXTRACTED_AWAITING_AI" if all_success else "EXTRACTION_IN_PROGRESS"

    reconstructed_or_later = [m for m, _ in strategy_rows if m in ("S2_RECONSTRUCTED", "S3_CLEAN", "S3_NEEDS_REVIEW")]
    if len(reconstructed_or_later) < len(strategy_rows):
        return "AI_IN_PROGRESS"

    all_approved = all(a == 1 for _, a in strategy_rows)
    return "COMPLETE" if all_approved else "NEEDS_REVIEW"


def list_all_courses_with_status(conn, raw_courses_dir: str) -> list:
    """
    Every course folder on disk, PLUS every YouTube-derived "course"
    (course_id starting with youtube_) that exists in the database --
    YouTube videos never have a raw folder, so without this they were
    invisible to the Courses tab and could never be selected for AI
    processing or get a Course_Summary.md at all. A YouTube video is
    just one more course in the data model; this makes that true in
    the UI too.
    """
    courses = []
    seen_ids = set()

    raw_root = Path(raw_courses_dir)
    if raw_root.exists():
        for p in sorted(raw_root.iterdir()):
            if p.is_dir():
                courses.append({"course_id": p.name, "status": get_course_status(conn, p.name), "source": "local"})
                seen_ids.add(p.name)

    youtube_course_ids = conn.execute(
        "SELECT DISTINCT course_id FROM files WHERE course_id LIKE 'youtube_%' ORDER BY course_id"
    ).fetchall()
    for (cid,) in youtube_course_ids:
        if cid not in seen_ids:
            courses.append({"course_id": cid, "status": get_course_status(conn, cid), "source": "youtube"})
            seen_ids.add(cid)

    return courses


def youtube_output_folder_name(video_id: str, title: str) -> str:
    """
    Builds a human-readable output folder name for a YouTube video --
    combines the title (so you can tell folders apart at a glance) with
    the video_id (so it's always unique, even if two videos share a
    title). Used consistently at BOTH extraction time and later AI-stage
    time so the two always resolve to the same folder -- if they ever
    disagreed, a course's AI output would silently land in a different
    folder than its extraction output.
    """
    from processors.text_export import safe_filename
    safe_title = safe_filename(title, max_len=60) if title else "untitled"
    return f"{safe_title} [{video_id}]"


def output_root_for_course(course_id: str, output_courses_dir: str, output_youtube_dir: str, conn=None) -> str:
    """Resolves the correct output folder for either a local course or a
    YouTube-derived one -- centralizes the branching that used to be
    duplicated ad-hoc (e.g. in dashboard.py's approve step).

    For YouTube courses, the folder is named "<title> [<video_id>]" for
    readability when several videos are queued -- pass conn so the title
    can be looked up from the database (it's the same title stored at
    extraction time, so this always resolves consistently)."""
    if course_id.startswith("youtube_"):
        video_id = course_id.replace("youtube_", "", 1)
        title = None
        if conn is not None:
            row = conn.execute(
                "SELECT title FROM sessions WHERE course_id = ? LIMIT 1", (course_id,)
            ).fetchone()
            if row:
                title = row[0]
        folder_name = youtube_output_folder_name(video_id, title) if title else video_id
        return str(Path(output_youtube_dir) / folder_name)
    return str(Path(output_courses_dir) / course_id)


def _wipe_course_db_records(conn, course_id: str) -> int:
    """
    Shared by reset_course() and delete_course_completely() -- deletes
    every DB row tied to a course, in FK-safe order: evidence/strategies
    -> transcript/key_frames/document_extractions -> sessions -> files.
    Returns how many files-table rows were removed (a reasonable proxy
    for "how much was actually there").
    """
    conn.execute("DELETE FROM strategies WHERE course_id = ?", (course_id,))
    conn.execute("DELETE FROM evidence WHERE course_id = ?", (course_id,))
    conn.execute(
        "DELETE FROM transcript_segments WHERE session_id IN (SELECT session_id FROM sessions WHERE course_id = ?)",
        (course_id,),
    )
    conn.execute(
        "DELETE FROM key_frames WHERE session_id IN (SELECT session_id FROM sessions WHERE course_id = ?)",
        (course_id,),
    )
    conn.execute(
        "DELETE FROM document_extractions WHERE file_id IN (SELECT file_id FROM files WHERE course_id = ?)",
        (course_id,),
    )
    conn.execute("DELETE FROM sessions WHERE course_id = ?", (course_id,))
    files_deleted = conn.execute("DELETE FROM files WHERE course_id = ?", (course_id,)).rowcount
    conn.commit()
    return files_deleted


def reset_course(conn, course_id: str, output_course_dir: str = None, delete_output_files: bool = True) -> dict:
    """
    Clears a course's processing history from the database so it can be
    reassigned/reprocessed from scratch -- status is always DERIVED from
    the database (see get_course_status), so there's nothing to "unfreeze"
    directly; this deletes the underlying records instead, which is what
    actually makes the status change.

    Does NOT touch 00_RAW/Courses/<course_id> -- your source material is
    never deleted by this. If delete_output_files is True, also clears
    that course's 01_OUTPUT files (transcripts, extractions, etc.) so a
    re-run starts genuinely clean, not just in the database.
    """
    files_deleted = _wipe_course_db_records(conn, course_id)

    output_files_removed = 0
    if delete_output_files and output_course_dir:
        for f in Path(output_course_dir).rglob("*"):
            if f.is_file() and f.name != ".gitkeep":
                f.unlink()
                output_files_removed += 1

    return {
        "course_id": course_id,
        "database_file_records_removed": files_deleted,
        "output_files_removed": output_files_removed,
        "new_status": get_course_status(conn, course_id),
    }


def delete_course_completely(conn, course_id: str, output_course_dir: str = None, raw_course_dir: str = None) -> dict:
    """
    The destructive counterpart to reset_course(): removes a course
    entirely, so it's gone from the app's course list, not just reset
    to "Not Started". Unlike reset_course(), this DOES remove the raw
    source folder if one is given -- for a local course, that means the
    original uploaded PDFs/Excel/videos/etc are permanently deleted too,
    which is exactly why the dashboard confirmation for this action must
    say so explicitly and separately from the safer reset action.

    For a YouTube-derived course there is no raw folder concept (the
    downloaded video itself is already auto-deleted after processing) --
    pass raw_course_dir=None for those, and only output_course_dir is
    removed.

    Safe to call on a course that's only partially processed or already
    empty -- every path is existence-checked before removal.
    """
    files_deleted = _wipe_course_db_records(conn, course_id)

    output_removed = False
    if output_course_dir and Path(output_course_dir).exists():
        shutil.rmtree(output_course_dir, ignore_errors=True)
        output_removed = True

    raw_removed = False
    if raw_course_dir and Path(raw_course_dir).exists():
        shutil.rmtree(raw_course_dir, ignore_errors=True)
        raw_removed = True

    return {
        "course_id": course_id,
        "database_file_records_removed": files_deleted,
        "output_folder_removed": output_removed,
        "raw_folder_removed": raw_removed,
    }

