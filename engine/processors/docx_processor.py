"""
Word document (.docx) processor.

This file type was previously classified during ingestion (file_type =
'doc') but never actually processed -- it had no processor and was never
included in the pipeline's materials query, so .docx files sat at status
PENDING forever with no error and no output. This processor closes that
gap, following the same evidence-with-location-reference pattern as the
PDF (page number) and PPTX (slide number) processors.

Legacy .doc (pre-2007 binary format) is classified the same way at
ingestion but is NOT handled here -- python-docx only reads the modern
.docx XML format. A .doc file will be skipped with a clear per-file error
recorded on the file's row rather than silently doing nothing, so it's
visible in the Courses tab instead of invisible.

Extracts, each tagged with its position for traceability:
  - Paragraph text, grouped under the nearest preceding heading so a
    reviewer can tell WHERE in the document a rule came from, not just
    that it came from "the document".
  - Tables, row by row (trainers sometimes put rule tables -- e.g.
    timeframe / indicator / action -- in a Word doc rather than Excel).
"""

import uuid
from datetime import datetime, timezone
from pathlib import Path

from processors.text_export import safe_filename, write_text_export


def process_docx_file(conn, file_id: str, path: str, output_dir: str = None) -> dict:
    ext = Path(path).suffix.lower()
    if ext == ".doc":
        # Legacy binary format -- python-docx cannot read it. Flagged
        # explicitly (not silently skipped) so it shows up as something
        # to convert to .docx and re-drop into the course folder.
        conn.execute(
            """UPDATE files SET status = 'FAILED',
               stage_completed = 'docx_extraction_failed', processed_at = ?
               WHERE file_id = ?""",
            (datetime.now(timezone.utc).isoformat(), file_id),
        )
        conn.commit()
        return {
            "error": "legacy .doc format not supported -- re-save as .docx in Word and re-drop into the course folder",
            "paragraphs_extracted": 0,
            "tables_extracted": 0,
        }

    from docx import Document  # python-docx; imported here so its absence
                                # only breaks docx processing, not the whole app

    doc = Document(path)
    extractions = []
    current_heading = "Document start"
    para_counter = 0

    for para in doc.paragraphs:
        text = para.text.strip()
        if not text:
            continue
        style_name = (para.style.name or "").lower() if para.style else ""
        if style_name.startswith("heading") or style_name == "title":
            current_heading = text
            continue
        para_counter += 1
        location_ref = f"paragraph_{para_counter} (under \"{current_heading}\")"
        extractions.append(
            (str(uuid.uuid4()), file_id, location_ref, "text", text, 1.0)
        )

    for table_num, table in enumerate(doc.tables, start=1):
        table_lines = []
        for row in table.rows:
            table_lines.append(" | ".join(cell.text.strip() for cell in row.cells))
        content = "\n".join(line for line in table_lines if line.strip(" |"))
        if content:
            extractions.append(
                (str(uuid.uuid4()), file_id, f"table_{table_num} (under \"{current_heading}\")",
                 "table", content, 1.0)
            )

    conn.executemany(
        """INSERT INTO document_extractions
           (extraction_id, file_id, location_ref, extraction_type, content, confidence)
           VALUES (?, ?, ?, ?, ?, ?)""",
        extractions,
    )
    conn.execute(
        "UPDATE files SET status = 'SUCCESS', stage_completed = 'docx_extracted', processed_at = ? WHERE file_id = ?",
        (datetime.now(timezone.utc).isoformat(), file_id),
    )
    conn.commit()

    if output_dir:
        original_name = conn.execute(
            "SELECT original_name FROM files WHERE file_id = ?", (file_id,)
        ).fetchone()[0]
        lines = [f"Word document extraction -- {original_name}", "=" * 60, ""]
        for e in extractions:
            lines.append(f"[{e[2]}]\n{e[4]}\n")
        export_path = write_text_export(output_dir, f"{safe_filename(original_name)}.txt", "\n".join(lines))
        print(f"  [export] readable DOCX summary written -> {export_path}")

    return {
        "paragraphs_extracted": para_counter,
        "tables_extracted": len(doc.tables),
        "extraction_records": len(extractions),
    }


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from db.schema import get_connection

    conn = get_connection("TradingIntelligence.db")
    row = conn.execute("SELECT file_id, original_path FROM files WHERE file_type = 'doc' LIMIT 1").fetchone()
    if row:
        file_id, path = row
        result = process_docx_file(conn, file_id, path)
        print("DOCX processing result:", result)
    else:
        print("No doc file found in database to test with.")
    conn.close()
