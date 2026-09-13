"""
Image/OCR processor.

Handles standalone screenshots. Also called by the video processor (on key
frames) and by the Excel/PDF processors (on embedded images) -- one OCR
function, reused everywhere an image needs to become searchable text.
"""

import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytesseract
from PIL import Image


def ocr_image(image_path: str) -> str:
    """
    Reads text from an image via Tesseract.

    Two real, silent-failure issues were found and fixed here, both via
    an actual synthetic video frame that visibly contained readable text
    but came back as an empty string with no error -- indistinguishable
    from "genuinely no text in this frame" without checking the image
    by eye, which is exactly the kind of gap this whole review has been
    hunting for:

    1. Opening certain JPEGs with plain Image.open() and handing that
       object straight to pytesseract could return empty even though
       the command-line tesseract binary read the same file correctly.
       .load() alone did not fix it; .convert("RGB") does, even when
       the image already reports mode "RGB" -- pytesseract hands the
       image to Tesseract via an internal temp-file save, and some
       JPEGs apparently serialize differently through that path
       depending on whether PIL has already fully re-materialized the
       pixel data via convert().

    2. Separately, low-resolution frames (640x360 is a completely
       normal size for a screen-recorded video) sit right at the edge
       of what Tesseract reliably reads -- well below its documented
       ~300 DPI-equivalent sweet spot. A frame that plain RGB OCR still
       missed came through cleanly once converted to grayscale and
       upscaled 2x. Applied only below a size threshold so already-
       comfortable images (a full-page PDF scan, a normal screenshot)
       aren't needlessly reprocessed.
    """
    img = Image.open(image_path).convert("RGB")
    text = pytesseract.image_to_string(img).strip()
    if text:
        return text

    # Fallback for low-resolution images where plain OCR found nothing:
    # grayscale + 2x upscale, which measurably fixed a real low-res video
    # frame during testing without needing to guess whether the image
    # actually has no text at all -- if this pass also comes back empty,
    # that's a genuine "no text detected", not another silent gap.
    if max(img.size) < 1000:
        upscaled = img.convert("L").resize((img.width * 2, img.height * 2))
        text = pytesseract.image_to_string(upscaled).strip()
    return text


def process_image_file(conn, file_id: str, path: str, output_dir: str = None) -> dict:
    text = ocr_image(path)
    conn.execute(
        """INSERT INTO document_extractions
           (extraction_id, file_id, location_ref, extraction_type, content, confidence)
           VALUES (?, ?, 'full_image', 'text', ?, 1.0)""",
        (str(uuid.uuid4()), file_id, text),
    )
    conn.execute(
        "UPDATE files SET status = 'SUCCESS', stage_completed = 'ocr_extracted', processed_at = ? WHERE file_id = ?",
        (datetime.now(timezone.utc).isoformat(), file_id),
    )
    conn.commit()

    if output_dir:
        from processors.text_export import safe_filename, write_text_export
        original_name = conn.execute("SELECT original_name FROM files WHERE file_id = ?", (file_id,)).fetchone()[0]
        content = f"Screenshot OCR -- {original_name}\n{'=' * 60}\n\n{text if text.strip() else '(no text detected)'}"
        export_path = write_text_export(output_dir, f"{safe_filename(original_name)}.txt", content)
        print(f"  [export] readable OCR summary written -> {export_path}")

    return {"ocr_chars": len(text), "ocr_preview": text[:200]}


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from db.schema import get_connection

    conn = get_connection("TradingIntelligence.db")
    row = conn.execute("SELECT file_id, original_path FROM files WHERE file_type = 'image' LIMIT 1").fetchone()
    file_id, path = row
    result = process_image_file(conn, file_id, path)
    print("Image OCR result:", result)
    conn.close()
