"""
Pipeline orchestrator.

Runs a course through the full vertical slice, IN ORDER, per the agreed
architecture:
    main sessions -> supporting sessions -> other videos -> supporting materials
      -> cross-linking (stub) -> review report

This is the "click Process Course" step. Safe to re-run: each stage skips
files that are already SUCCESS, and video reprocessing is idempotent
(see video_processor.py).

Video discovery has NO folder-naming requirement. ingest.py registers
every video found anywhere under the course folder regardless of what its
containing folder is called -- "main"/"supporting" in the path (see
classify_session_type) is used ONLY to decide processing order (main
sessions establish the strategy skeleton that supporting sessions and
materials are interpreted against), never as a filter on whether a video
gets processed at all. Found via real testing: a real course's videos sat
in a plain "Sessions" folder (not "Main_Sessions"), and an earlier version
of this pipeline only ever queried session_type IN ('main', 'supporting')
-- so every video with session_type='unknown' (anything not explicitly
labeled) was registered but silently never processed, with no error
shown. Stage 2.5 below is the fix: every video that isn't main or
supporting still gets processed, just after the explicitly-labeled ones.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.schema import get_connection, init_db
from pipeline.ingest import scan_course_folder, inventory_report
from processors.excel_processor import process_excel_file
from processors.csv_processor import process_csv_file
from processors.pdf_processor import process_pdf_file
from processors.image_processor import process_image_file
from processors.pptx_processor import process_pptx_file
from processors.docx_processor import process_docx_file
from processors.txt_processor import process_txt_file
from processors.video_processor import process_video_file
from processors.cleanup import cleanup_course_videos


def process_material_file(conn, file_id: str, path: str, file_type: str, material_evidence_root) -> dict:
    """
    Single-file materials dispatcher -- the one place that maps a
    file_type to its processor and output folder. Shared by this
    module's own Stage 4 loop AND by process_youtube_queue.py (for
    files auto-downloaded from a video's description, which never pass
    through scan_course_folder/Stage 4 since they have no local course
    folder to be scanned from). Kept as one function so the two callers
    can never drift out of sync on which processor handles which type.
    """
    material_evidence_root = Path(material_evidence_root)
    if file_type == "excel":
        return process_excel_file(conn, file_id, path, str(material_evidence_root / "Excel_Analysis"))
    elif file_type == "csv":
        return process_csv_file(conn, file_id, path, str(material_evidence_root / "CSV_Extraction"))
    elif file_type == "pdf":
        return process_pdf_file(conn, file_id, path,
                                 str(material_evidence_root / "PDF_Extraction" / "images"),
                                 str(material_evidence_root / "PDF_Extraction"))
    elif file_type == "image":
        return process_image_file(conn, file_id, path, str(material_evidence_root / "Screenshot_OCR"))
    elif file_type == "pptx":
        return process_pptx_file(conn, file_id, path, str(material_evidence_root / "PPTX_Extraction"))
    elif file_type == "doc":
        return process_docx_file(conn, file_id, path, str(material_evidence_root / "DOCX_Extraction"))
    elif file_type == "txt":
        return process_txt_file(conn, file_id, path, str(material_evidence_root / "TXT_Extraction"))
    return {"skipped": True, "reason": f"no processor for file_type={file_type}"}


def _mark_file_failed(conn, file_id: str, file_type: str) -> None:
    """
    Records a file as FAILED instead of letting an unexpected exception
    propagate out of process_course. Found via real testing: a single
    corrupted/malformed file (e.g. a .xlsx that's actually not a valid
    zip) raised straight past every processor's own error handling --
    which only covers KNOWN failure shapes like legacy .xls -- and took
    down the rest of THIS course's materials, stages 5/6, and every course
    after it in process_all_courses.py's loop. Rolled back first to
    discard whatever partial extraction rows the crashed processor
    half-wrote before failing, so a failed file never leaves inconsistent
    partial evidence behind. Re-running the course will retry it, same as
    any other FAILED file.
    """
    conn.rollback()
    conn.execute(
        """UPDATE files SET status = 'FAILED',
           stage_completed = ?, processed_at = ?
           WHERE file_id = ?""",
        (f"{file_type}_extraction_failed", datetime.now(timezone.utc).isoformat(), file_id),
    )
    conn.commit()


def process_course(course_id: str, raw_source_dir: str, db_path: str, work_dir: str = "extracted",
                    auto_delete_video: bool = True):
    init_db(db_path)
    conn = get_connection(db_path)

    # work_dir is always .../01_VIDEO_EVIDENCE -- derive the course's output
    # root so materials can write into their own 02_MATERIAL_EVIDENCE subfolders.
    output_course_root = Path(work_dir).parent
    material_evidence_root = output_course_root / "02_MATERIAL_EVIDENCE"

    print(f"\n=== STAGE 1: SCAN — {course_id} ===")
    summary = scan_course_folder(conn, course_id, raw_source_dir)
    print(summary)
    inventory_report(conn, course_id)

    # ---- Course-wide progress tracking: count total work items up front so
    # a single running "[progress] N%" figure can be shown live in the GUI,
    # instead of only knowing progress AFTER each stage finishes. ----
    total_main = conn.execute(
        """SELECT COUNT(*) FROM files f JOIN sessions s ON s.file_id = f.file_id
           WHERE f.course_id = ? AND f.file_type = 'video' AND s.session_type = 'main'
             AND f.status != 'SUCCESS'""", (course_id,)).fetchone()[0]
    total_supporting = conn.execute(
        """SELECT COUNT(*) FROM files f JOIN sessions s ON s.file_id = f.file_id
           WHERE f.course_id = ? AND f.file_type = 'video' AND s.session_type = 'supporting'
             AND f.status != 'SUCCESS'""", (course_id,)).fetchone()[0]
    total_other_videos = conn.execute(
        """SELECT COUNT(*) FROM files f JOIN sessions s ON s.file_id = f.file_id
           WHERE f.course_id = ? AND f.file_type = 'video'
             AND s.session_type NOT IN ('main', 'supporting') AND f.status != 'SUCCESS'""",
        (course_id,)).fetchone()[0]
    total_materials = conn.execute(
        """SELECT COUNT(*) FROM files WHERE course_id = ?
           AND file_type IN ('excel', 'csv', 'pdf', 'image', 'pptx', 'doc', 'txt') AND status != 'SUCCESS'""",
        (course_id,)).fetchone()[0]
    total_units = max(1, total_main + total_supporting + total_other_videos + total_materials)
    completed_units = 0

    def report_progress():
        pct = min(100, round(completed_units / total_units * 100))
        print(f"[progress] course={course_id} {pct}% ({completed_units}/{total_units} items)")

    # Emit the total item count IMMEDIATELY, before any item starts -- this
    # is the actual fix for "it restarts from 0% for every video". Without
    # this, the GUI doesn't learn the true total until AFTER the first
    # item finishes, so item 1's own progress had nothing to blend into
    # and showed its raw 0-100% as if it were overall progress. Now every
    # item, including the very first one, blends into the real total from
    # its first chunk onward.
    report_progress()

    # ---- STAGE 2: MAIN SESSIONS FIRST ----
    print(f"\n=== STAGE 2: MAIN SESSIONS ===")
    main_videos = conn.execute(
        """SELECT f.file_id, s.session_id, f.original_path FROM files f
           JOIN sessions s ON s.file_id = f.file_id
           WHERE f.course_id = ? AND f.file_type = 'video'
             AND s.session_type = 'main' AND f.status != 'SUCCESS'""",
        (course_id,),
    ).fetchall()
    for i, (file_id, session_id, path) in enumerate(main_videos, start=1):
        print(f"  ({i}/{len(main_videos)}) processing main session: {path}")
        try:
            result = process_video_file(conn, file_id, session_id, path, work_dir)
            print("   ", result)
        except Exception as e:
            print(f"    FAILED -- {type(e).__name__}: {e}")
            _mark_file_failed(conn, file_id, "video")
        completed_units += 1
        report_progress()

    # ---- STAGE 3: SUPPORTING SESSIONS ----
    print(f"\n=== STAGE 3: SUPPORTING SESSIONS ===")
    supporting_videos = conn.execute(
        """SELECT f.file_id, s.session_id, f.original_path FROM files f
           JOIN sessions s ON s.file_id = f.file_id
           WHERE f.course_id = ? AND f.file_type = 'video'
             AND s.session_type = 'supporting' AND f.status != 'SUCCESS'""",
        (course_id,),
    ).fetchall()
    for i, (file_id, session_id, path) in enumerate(supporting_videos, start=1):
        print(f"  ({i}/{len(supporting_videos)}) processing supporting session: {path}")
        try:
            result = process_video_file(conn, file_id, session_id, path, work_dir)
            print("   ", result)
        except Exception as e:
            print(f"    FAILED -- {type(e).__name__}: {e}")
            _mark_file_failed(conn, file_id, "video")
        completed_units += 1
        report_progress()

    # ---- STAGE 2.5: EVERY OTHER VIDEO -- anything not explicitly in a "main" or
    # "supporting" folder still gets fully processed, just after the explicitly-
    # labeled ones. This is what guarantees no video is ever silently skipped
    # just because of how its folder happened to be named. ----
    print(f"\n=== STAGE 2.5: OTHER VIDEOS (no main/supporting folder label) ===")
    other_videos = conn.execute(
        """SELECT f.file_id, s.session_id, f.original_path FROM files f
           JOIN sessions s ON s.file_id = f.file_id
           WHERE f.course_id = ? AND f.file_type = 'video'
             AND s.session_type NOT IN ('main', 'supporting') AND f.status != 'SUCCESS'""",
        (course_id,),
    ).fetchall()
    for i, (file_id, session_id, path) in enumerate(other_videos, start=1):
        print(f"  ({i}/{len(other_videos)}) processing unlabeled video: {path}")
        try:
            result = process_video_file(conn, file_id, session_id, path, work_dir)
            print("   ", result)
        except Exception as e:
            print(f"    FAILED -- {type(e).__name__}: {e}")
            _mark_file_failed(conn, file_id, "video")
        completed_units += 1
        report_progress()

    # ---- STAGE 4: SUPPORTING MATERIALS (only after videos define the strategy skeleton) ----
    print(f"\n=== STAGE 4: SUPPORTING MATERIALS ===")
    materials = conn.execute(
        """SELECT file_id, original_path, file_type FROM files
           WHERE course_id = ? AND file_type IN ('excel', 'csv', 'pdf', 'image', 'pptx', 'doc', 'txt')
             AND status != 'SUCCESS'""",
        (course_id,),
    ).fetchall()
    for i, (file_id, path, file_type) in enumerate(materials, start=1):
        print(f"  ({i}/{len(materials)}) processing {file_type}: {path}")
        try:
            result = process_material_file(conn, file_id, path, file_type, material_evidence_root)
            print("   ", result)
        except Exception as e:
            print(f"    FAILED -- {type(e).__name__}: {e}")
            _mark_file_failed(conn, file_id, file_type)
        completed_units += 1
        report_progress()

    # ---- STAGE 5: REVIEW QUEUE REPORT ----
    print(f"\n=== STAGE 5: REVIEW QUEUE ===")
    flagged = conn.execute(
        """SELECT s.title, ts.start_seconds, ts.end_seconds, ts.text, ts.confidence
           FROM transcript_segments ts
           JOIN sessions s ON s.session_id = ts.session_id
           WHERE s.course_id = ? AND ts.needs_review = 1""",
        (course_id,),
    ).fetchall()
    print(f"  {len(flagged)} transcript segment(s) need human review:")
    for row in flagged:
        print("   ", row)

    inventory_report(conn, course_id)

    # ---- STAGE 6: STORAGE CLEANUP -- delete local videos once their OWN extraction
    # is fully confirmed successful (per your decision). Materials (Excel/PDF/
    # screenshots) are never deleted -- only raw video, which is the heavy part. ----
    if auto_delete_video:
        print(f"\n=== STORAGE CLEANUP: LOCAL VIDEO ===")
        cleanup_result = cleanup_course_videos(conn, course_id, work_dir=work_dir)
        print(f"  {cleanup_result}")

    conn.close()
    print("\n=== COURSE PROCESSING COMPLETE ===")


if __name__ == "__main__":
    process_course(
        course_id="trainer01_course01",
        raw_source_dir="sample_course/00_RAW_SOURCE",
        db_path="TradingIntelligence.db",
        work_dir="extracted",
    )
