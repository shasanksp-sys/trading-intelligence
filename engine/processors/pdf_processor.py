"""
PDF processor.

Uses pdfplumber (works well for text + tables). If PyMuPDF (fitz) is
installed, embedded images are also extracted and routed to OCR -- PyMuPDF
handles embedded-image extraction better than pdfplumber, so it's used
opportunistically when available (install with: pip install pymupdf).

Every extraction keeps its page number -- this is the traceability
requirement from the architecture docs.
"""

import uuid
from datetime import datetime, timezone
from pathlib import Path

import pdfplumber

from processors.text_export import safe_filename, write_text_export
from processors.image_processor import ocr_image

try:
    import pymupdf as fitz  # PyMuPDF -- optional, used only for embedded image extraction
    HAS_FITZ = True
except ImportError:
    HAS_FITZ = False


def process_pdf_file(conn, file_id: str, path: str, image_output_dir: str, text_export_dir: str = None) -> dict:
    extractions = []
    image_count = 0

    with pdfplumber.open(path) as pdf:
        for page_num, page in enumerate(pdf.pages, start=1):
            text = page.extract_text() or ""
            if text.strip():
                extractions.append(
                    (str(uuid.uuid4()), file_id, f"page_{page_num}", "text", text.strip(), 1.0)
                )

            tables = page.extract_tables()
            for t_idx, table in enumerate(tables, start=1):
                table_str = "\n".join(
                    " | ".join(str(cell) if cell is not None else "" for cell in row)
                    for row in table
                )
                extractions.append(
                    (
                        str(uuid.uuid4()),
                        file_id,
                        f"page_{page_num}_table_{t_idx}",
                        "table",
                        table_str,
                        1.0,
                    )
                )

    if HAS_FITZ:
        Path(image_output_dir).mkdir(parents=True, exist_ok=True)
        doc = fitz.open(path)
        for page_num in range(len(doc)):
            for img_idx, img in enumerate(doc[page_num].get_images(full=True), start=1):
                xref = img[0]
                base_image = doc.extract_image(xref)
                out_path = Path(image_output_dir) / f"p{page_num+1}_img{img_idx}.{base_image['ext']}"
                out_path.write_bytes(base_image["image"])
                image_count += 1
                # Actually OCR the saved image now -- previously this just wrote a
                # "route to OCR" placeholder that nothing ever picked up, so any
                # text in a diagram/screenshot embedded in a PDF (e.g. a chart with
                # levels marked on it) was invisible to reconstruction.
                try:
                    ocr_text = ocr_image(str(out_path))
                    content = ocr_text if ocr_text.strip() else f"(embedded image at {out_path.name}, no text detected by OCR)"
                except Exception as e:
                    content = f"(embedded image at {out_path.name}, but OCR failed: {e})"
                extractions.append(
                    (
                        str(uuid.uuid4()),
                        file_id,
                        f"page_{page_num+1}_embedded_image_{img_idx}",
                        "embedded_image",
                        content,
                        1.0,
                    )
                )
        doc.close()

    conn.executemany(
        """INSERT INTO document_extractions
           (extraction_id, file_id, location_ref, extraction_type, content, confidence)
           VALUES (?, ?, ?, ?, ?, ?)""",
        extractions,
    )
    conn.execute(
        "UPDATE files SET status = 'SUCCESS', stage_completed = 'pdf_extracted', processed_at = ? WHERE file_id = ?",
        (datetime.now(timezone.utc).isoformat(), file_id),
    )
    conn.commit()

    if text_export_dir:
        original_name = conn.execute("SELECT original_name FROM files WHERE file_id = ?", (file_id,)).fetchone()[0]
        lines = [f"PDF extraction -- {original_name}", "=" * 60, ""]
        for e in extractions:
            lines.append(f"[{e[2]}] ({e[3]})\n{e[4]}\n")
        export_path = write_text_export(text_export_dir, f"{safe_filename(original_name)}.txt", "\n".join(lines))
        print(f"  [export] readable PDF summary written -> {export_path}")

    return {
        "text_blocks": sum(1 for e in extractions if e[3] == "text"),
        "tables": sum(1 for e in extractions if e[3] == "table"),
        "embedded_images": image_count,
        "pymupdf_available": HAS_FITZ,
    }


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from db.schema import get_connection

    conn = get_connection("TradingIntelligence.db")
    row = conn.execute("SELECT file_id, original_path FROM files WHERE file_type = 'pdf' LIMIT 1").fetchone()
    file_id, path = row
    result = process_pdf_file(conn, file_id, path, "extracted/pdf_images")
    print("PDF processing result:", result)

    print("\nExtracted content:")
    for r in conn.execute(
        "SELECT location_ref, extraction_type, content FROM document_extractions WHERE file_id = ?",
        (file_id,),
    ):
        print(" ", r)
    conn.close()
