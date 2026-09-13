"""
CSV processor.

Previously .csv files were not recognized at all -- classify_file_type()
in ingest.py had no CSV_EXTS entry, so a CSV dropped into a course folder
was never registered, never processed, and never appeared as a failure
either. It was simply invisible: worse than a clear error, because
nothing in the Courses tab would tell you it was ever there. This
processor closes that gap.

CSVs are treated as tabular data, the same conceptual evidence as an
Excel sheet, just without formulas -- only raw values, since CSV has no
concept of a formula. Auto-detects the delimiter (comma, semicolon, or
tab -- exports from different tools vary) rather than assuming comma.
Large files are chunked into blocks of rows so no single evidence row is
unreasonably huge, each block tagged with its row range for traceability
the same way Excel cells are tagged with a coordinate.
"""

import csv
import uuid
from datetime import datetime, timezone
from pathlib import Path

from processors.text_export import safe_filename, write_text_export

ROWS_PER_CHUNK = 200


def _sniff_dialect(sample: str) -> csv.Dialect:
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        return csv.get_dialect("excel")  # default: comma-delimited


def process_csv_file(conn, file_id: str, path: str, output_dir: str = None) -> dict:
    with open(path, "r", encoding="utf-8-sig", newline="", errors="replace") as f:
        sample = f.read(4096)
        f.seek(0)
        dialect = _sniff_dialect(sample)
        reader = csv.reader(f, dialect)
        rows = list(reader)

    extractions = []
    if rows:
        header = rows[0]
        data_rows = rows[1:]

        for chunk_start in range(0, max(len(data_rows), 1), ROWS_PER_CHUNK):
            chunk = data_rows[chunk_start:chunk_start + ROWS_PER_CHUNK]
            if not chunk:
                break
            first_row_num = chunk_start + 2  # +1 for header, +1 for 1-indexing
            last_row_num = first_row_num + len(chunk) - 1
            location_ref = f"rows_{first_row_num}-{last_row_num}"

            lines = [" | ".join(header)]
            for row in chunk:
                lines.append(" | ".join(row))
            content = "\n".join(lines)

            extractions.append(
                (str(uuid.uuid4()), file_id, location_ref, "table_rows", content, 1.0)
            )

    conn.executemany(
        """INSERT INTO document_extractions
           (extraction_id, file_id, location_ref, extraction_type, content, confidence)
           VALUES (?, ?, ?, ?, ?, ?)""",
        extractions,
    )
    conn.execute(
        "UPDATE files SET status = 'SUCCESS', stage_completed = 'csv_extracted', processed_at = ? WHERE file_id = ?",
        (datetime.now(timezone.utc).isoformat(), file_id),
    )
    conn.commit()

    if output_dir:
        original_name = conn.execute("SELECT original_name FROM files WHERE file_id = ?", (file_id,)).fetchone()[0]
        lines = [f"CSV extraction -- {original_name}", "=" * 60, ""]
        for loc_ref, extraction_type, content, _confidence in [(e[2], e[3], e[4], e[5]) for e in extractions]:
            lines.append(f"[{loc_ref}] ({extraction_type})\n{content}\n")
        export_path = write_text_export(output_dir, f"{safe_filename(original_name)}.txt", "\n".join(lines))
        print(f"  [export] readable CSV summary written -> {export_path}")

    return {
        "rows_processed": len(rows) - 1 if rows else 0,
        "chunks_extracted": len(extractions),
    }
