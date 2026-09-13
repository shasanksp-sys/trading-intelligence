"""
Plain-text (.txt) processor.

This file type was previously classified during ingestion (file_type =
'other') and never processed -- it had no processor and was never
included in the pipeline's materials query, so .txt files sat at status
PENDING forever with no error and no output. Same gap class as the
.docx and .ppsx fixes; closed here following the same evidence pattern.

Trainers use these for plain-text notes (e.g. lists of topics to skip,
quick reminders) that don't warrant a full Word/PowerPoint file. The
whole file is stored as a single extraction -- there's no meaningful
sub-location to reference the way a page/slide/cell number would.
"""

import uuid
from datetime import datetime, timezone
from pathlib import Path

from processors.text_export import safe_filename, write_text_export


def process_txt_file(conn, file_id: str, path: str, output_dir: str = None) -> dict:
    try:
        content = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        conn.execute(
            """UPDATE files SET status = 'FAILED',
               stage_completed = 'txt_extraction_failed', processed_at = ?
               WHERE file_id = ?""",
            (datetime.now(timezone.utc).isoformat(), file_id),
        )
        conn.commit()
        return {"error": str(e)}

    extraction_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO document_extractions
           (extraction_id, file_id, location_ref, extraction_type, content, confidence)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (extraction_id, file_id, "whole_file", "text", content, 1.0),
    )
    conn.execute(
        "UPDATE files SET status = 'SUCCESS', stage_completed = 'txt_extracted', processed_at = ? WHERE file_id = ?",
        (datetime.now(timezone.utc).isoformat(), file_id),
    )
    conn.commit()

    if output_dir:
        original_name = conn.execute("SELECT original_name FROM files WHERE file_id = ?", (file_id,)).fetchone()[0]
        export_path = write_text_export(output_dir, f"{safe_filename(original_name)}.txt", content)
        print(f"  [export] readable TXT copy written -> {export_path}")

    return {"characters_extracted": len(content)}
