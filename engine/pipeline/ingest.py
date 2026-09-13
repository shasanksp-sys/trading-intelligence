"""
Stage 1: Ingestion.

Scans a course's 00_RAW_SOURCE folder, computes a stable hash per file,
detects duplicates, classifies file type, and registers everything in the
database with status PENDING. Nothing is processed here -- this is pure
inventory, matching the "Scan" step in the operating model.

Safe to re-run: already-registered files (same hash) are skipped, so
dropping new files into a course folder and re-scanning only picks up
what's new.
"""

import hashlib
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".m4v", ".wmv"}
EXCEL_EXTS = {".xlsx", ".xlsm", ".xls"}
CSV_EXTS = {".csv"}
PDF_EXTS = {".pdf"}
# .ppsx ("PowerPoint Show") is the same OOXML package format as .pptx --
# python-pptx opens it identically -- but was previously missing here, so
# every .ppsx file classified as "other" and sat PENDING forever with no
# processor (found via 20 stuck files in the Sameer Dharaskar 2023 course).
PPTX_EXTS = {".pptx", ".ppt", ".ppsx"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
DOC_EXTS = {".docx", ".doc"}
TXT_EXTS = {".txt"}


def classify_file_type(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in VIDEO_EXTS:
        return "video"
    if ext in EXCEL_EXTS:
        return "excel"
    if ext in CSV_EXTS:
        return "csv"
    if ext in PDF_EXTS:
        return "pdf"
    if ext in PPTX_EXTS:
        return "pptx"
    if ext in IMAGE_EXTS:
        return "image"
    if ext in DOC_EXTS:
        return "doc"
    if ext in TXT_EXTS:
        return "txt"
    return "other"


def classify_session_type(path: Path, raw_root: Path = None) -> str:
    """
    Infer main vs supporting session from the folder it lives in.

    Found via real testing on the deployed Mac: this project's own root
    folder is named "TradingIntelligence_Main", which itself contains the
    substring "main". Checking the file's FULL ABSOLUTE PATH meant every
    single video, in every course, on this exact deployment matched
    "main" regardless of which folder it actually sat in (unless
    "supporting" happened to appear first) -- completely defeating the
    documented rule that a video outside Main_Sessions/Supporting_Sessions
    is left unprocessed. Scoping the check to the path relative to the
    course's own raw folder (when known) fixes this for good, rather than
    just for this one folder name.
    """
    if raw_root is not None:
        try:
            path = path.relative_to(raw_root)
        except ValueError:
            pass
    parts_lower = [p.lower() for p in path.parts]
    if any("supporting" in p for p in parts_lower):
        return "supporting"
    if any("main" in p for p in parts_lower):
        return "main"
    return "unknown"


def sha256_of_file(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(chunk_size):
            h.update(chunk)
    return h.hexdigest()


def scoped_file_id(course_id: str, content_hash: str) -> str:
    """
    file_id is course-scoped, not just content-scoped. Fixes a real bug:
    using identical file content across two different courses (e.g. for
    testing) used to collide on the same file_id -- since file_id was the
    PRIMARY KEY, the second course's INSERT OR IGNORE silently did nothing,
    leaving that course with zero registered files even though the scan
    summary reported them as "new". This still correctly detects true
    duplicates WITHIN one course (same content -> same scoped id -> same
    safe no-op on re-scan), it just no longer confuses that with "this
    exact content already belongs to a different course."
    """
    return hashlib.sha256(f"{course_id}::{content_hash}".encode()).hexdigest()


def scan_course_folder(conn: sqlite3.Connection, course_id: str, raw_source_dir: str) -> dict:
    """
    Walk raw_source_dir recursively, register every file.
    Returns a summary dict: {new, duplicate, already_registered}
    """
    raw_root = Path(raw_source_dir)
    if not raw_root.exists():
        raise FileNotFoundError(f"Raw source folder not found: {raw_source_dir}")

    summary = {"new": 0, "duplicate": 0, "already_registered": 0, "total_seen": 0}
    known_hashes = {}  # hash -> file_id, for duplicate detection within this scan

    # preload existing hashes for this course so re-scans are cheap and safe
    for row in conn.execute("SELECT file_id FROM files WHERE course_id = ?", (course_id,)):
        known_hashes[row[0]] = row[0]

    for path in sorted(raw_root.rglob("*")):
        if path.is_dir():
            continue
        if path.name.startswith("."):
            continue  # skip .DS_Store etc.

        summary["total_seen"] += 1
        content_hash = sha256_of_file(path)
        file_hash = scoped_file_id(course_id, content_hash)

        if file_hash in known_hashes:
            summary["already_registered"] += 1
            continue

        file_type = classify_file_type(path)
        is_dup_of = None
        # duplicate = same hash already seen in *this* scan pass under a different name
        if file_hash in known_hashes:
            is_dup_of = known_hashes[file_hash]
        else:
            known_hashes[file_hash] = file_hash

        conn.execute(
            """INSERT OR IGNORE INTO files
               (file_id, course_id, original_path, original_name, source_type,
                file_type, size_bytes, status, is_duplicate_of, registered_at)
               VALUES (?, ?, ?, ?, 'local', ?, ?, 'PENDING', ?, ?)""",
            (
                file_hash,
                course_id,
                str(path),
                path.name,
                file_type,
                path.stat().st_size,
                is_dup_of,
                datetime.now(timezone.utc).isoformat(),
            ),
        )

        # pre-register a session row for videos so downstream stages have a session_id
        if file_type == "video":
            session_id = str(uuid.uuid4())
            conn.execute(
                """INSERT INTO sessions (session_id, file_id, course_id, session_type, title)
                   VALUES (?, ?, ?, ?, ?)""",
                (session_id, file_hash, course_id, classify_session_type(path, raw_root), path.stem),
            )

        summary["new"] += (0 if is_dup_of else 1)
        if is_dup_of:
            summary["duplicate"] += 1

    conn.commit()
    return summary


def inventory_report(conn: sqlite3.Connection, course_id: str) -> None:
    rows = conn.execute(
        """SELECT file_type, status, COUNT(*) FROM files
           WHERE course_id = ? GROUP BY file_type, status ORDER BY file_type""",
        (course_id,),
    ).fetchall()
    print(f"\n--- Inventory for course '{course_id}' ---")
    for file_type, status, count in rows:
        print(f"  {file_type:10s} {status:10s} {count}")


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from db.schema import get_connection, init_db

    course = "trainer01_course01"
    raw_dir = "sample_course/00_RAW_SOURCE"
    init_db("TradingIntelligence.db")
    conn = get_connection("TradingIntelligence.db")
    summary = scan_course_folder(conn, course, raw_dir)
    print("Scan summary:", summary)
    inventory_report(conn, course)
    conn.close()
