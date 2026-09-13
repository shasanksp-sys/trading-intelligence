"""
YouTube description + linked-materials capture.

Trainers frequently do NOT put everything in the video itself: the
description box often contains a written strategy summary, a link to a
downloadable PDF/Excel/Word/notes file (hosted on the trainer's own site,
Google Drive, Dropbox, etc.), or both. Previously fetch_youtube_source()
in process_youtube_queue.py downloaded only the video -- the description
text was fetched by yt-dlp as part of its normal metadata request and
then simply discarded. Any rule stated only in the description, and any
material linked from it, was invisible to the rest of the pipeline.

This module closes that gap in two parts:
  1. The description text itself is stored as evidence (a document
     extraction on the video's file_id) -- it goes through tagging and
     reconstruction exactly like a PDF's text would.
  2. Every URL in the description is found and classified:
       - a direct link to a file with a recognized extension (.pdf,
         .xlsx, .xlsm, .docx, .pptx, .csv) is downloaded automatically
         and registered in the files table, so it flows through the
         normal materials pipeline (excel/pdf/docx/pptx/csv processors)
         on the very next pipeline run for that course -- no different
         from a file the user dropped in by hand.
       - anything else (Google Drive, Dropbox, a shortened link, a plain
         webpage) is NOT auto-downloaded -- these generally require
         interactive login/consent that can't be done headlessly, and
         silently failing to fetch one would be worse than flagging it.
         These are written to a clearly-named file the user is expected
         to check, exactly like the existing "route to OCR" -> real-OCR
         fix replaced a silent placeholder with an explicit, actionable
         one instead of pretending the gap doesn't exist.
"""

import re
import urllib.request
import urllib.error
import uuid
from datetime import datetime, timezone
from pathlib import Path

from pipeline.ingest import classify_file_type, scoped_file_id, sha256_of_file

URL_RE = re.compile(r'https?://[^\s<>"\')\]]+')

# Extensions we can safely fetch with a plain HTTP GET and hand straight
# to an existing processor. Anything else needs a browser/login and is
# not attempted.
DIRECT_DOWNLOAD_EXTS = {".pdf", ".xlsx", ".xlsm", ".xls", ".docx", ".doc", ".pptx", ".ppt", ".csv"}

# Hosts that never serve the real file directly from the URL in a
# description -- they need an interactive session, so don't even try;
# route straight to the manual-review list instead of wasting a request.
KNOWN_INTERACTIVE_HOSTS = ("drive.google.com", "dropbox.com", "docs.google.com", "sheet.google.com")


def extract_links(description: str) -> list:
    """Every http(s) URL found in the description, in the order they appear,
    with any trailing punctuation that isn't part of the URL stripped."""
    if not description:
        return []
    found = []
    for match in URL_RE.findall(description):
        cleaned = match.rstrip(".,;:!?")
        if cleaned not in found:
            found.append(cleaned)
    return found


def _looks_directly_downloadable(url: str) -> bool:
    if any(host in url for host in KNOWN_INTERACTIVE_HOSTS):
        return False
    ext = Path(url.split("?")[0].split("#")[0]).suffix.lower()
    return ext in DIRECT_DOWNLOAD_EXTS


# Maps a Content-Type header to the same extensions DIRECT_DOWNLOAD_EXTS
# recognizes, so a URL with no visible extension can still be identified
# once resolved. Kept intentionally narrow -- same file types as before,
# just detected a second way.
CONTENT_TYPE_TO_EXT = {
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/vnd.ms-powerpoint": ".ppt",
    "text/csv": ".csv",
}


def _resolve_via_redirect(url: str):
    """
    Handles the case a plain extension check misses: a shortened link
    (bit.ly, sedg.in, tinyurl -- exactly what showed up in a real
    description) or a download endpoint with no file extension in the
    URL (e.g. .../download?id=123), where the URL itself gives no clue
    but following it resolves straight to a PDF/Excel/etc.

    Sends a single HEAD request and follows redirects -- the same thing
    a browser does automatically when you click a short link -- then
    checks the FINAL resolved URL's extension and the Content-Type
    header. Deliberately does not attempt this for known interactive
    hosts (a HEAD to Drive/Dropbox returns an HTML login page, not a
    file, so there's nothing useful to detect there and no point making
    the request). Any failure (timeout, DNS error, non-200) falls
    through to the manual-review list exactly as before -- this only
    ever ADDS things to the auto-download path, never removes the
    existing safety net.

    Returns the resolved final URL if it looks like a real downloadable
    file, else None.
    """
    if any(host in url for host in KNOWN_INTERACTIVE_HOSTS):
        return None
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            final_url = resp.geturl()
            if any(host in final_url for host in KNOWN_INTERACTIVE_HOSTS):
                return None
            final_ext = Path(final_url.split("?")[0].split("#")[0]).suffix.lower()
            if final_ext in DIRECT_DOWNLOAD_EXTS:
                return final_url
            content_type = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
            if content_type in CONTENT_TYPE_TO_EXT:
                return final_url
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError):
        pass
    return None


def _attempt_download(url: str, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    filename = Path(url.split("?")[0].split("#")[0]).name or f"download_{uuid.uuid4().hex[:8]}"
    dest_path = dest_dir / filename
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        dest_path.write_bytes(resp.read())
    return dest_path


def process_youtube_description(conn, file_id: str, course_id: str, description: str,
                                 materials_download_dir: str, manual_review_output_dir: str) -> dict:
    """
    Stores the description as evidence, downloads what can safely be
    auto-fetched, and records everything else for manual follow-up.
    Returns a summary dict -- never raises, since a description/materials
    problem should never fail the video's own transcription/processing.
    """
    result = {
        "description_captured": False,
        "links_found": 0,
        "auto_downloaded": [],
        "needs_manual_download": [],
        "download_errors": [],
    }

    description = description or ""
    if description.strip():
        conn.execute(
            """INSERT INTO document_extractions
               (extraction_id, file_id, location_ref, extraction_type, content, confidence)
               VALUES (?, ?, 'youtube_description', 'text', ?, 1.0)""",
            (str(uuid.uuid4()), file_id, description.strip()),
        )
        conn.commit()
        result["description_captured"] = True

    links = extract_links(description)
    result["links_found"] = len(links)

    for url in links:
        download_url = url if _looks_directly_downloadable(url) else _resolve_via_redirect(url)
        if download_url:
            try:
                downloaded_path = _attempt_download(download_url, Path(materials_download_dir))
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
                result["download_errors"].append({"url": url, "error": str(e)[:200]})
                # a failed auto-download is not a dead end -- fall through
                # to the manual list so it's still visible to a human
                result["needs_manual_download"].append(url)
                continue

            content_hash = sha256_of_file(downloaded_path)
            new_file_id = scoped_file_id(course_id, content_hash)
            existing = conn.execute("SELECT file_id FROM files WHERE file_id = ?", (new_file_id,)).fetchone()
            if not existing:
                conn.execute(
                    """INSERT INTO files
                       (file_id, course_id, original_path, original_name, source_type,
                        file_type, size_bytes, status, registered_at)
                       VALUES (?, ?, ?, ?, 'youtube_description_link', ?, ?, 'PENDING', ?)""",
                    (new_file_id, course_id, str(downloaded_path), downloaded_path.name,
                     classify_file_type(downloaded_path), downloaded_path.stat().st_size,
                     datetime.now(timezone.utc).isoformat()),
                )
                conn.commit()
            entry = {"url": url, "saved_as": downloaded_path.name, "file_id": new_file_id}
            if download_url != url:
                entry["resolved_from_redirect"] = download_url
            result["auto_downloaded"].append(entry)
        else:
            result["needs_manual_download"].append(url)

    if result["needs_manual_download"]:
        out_dir = Path(manual_review_output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        review_path = out_dir / "Needs_Manual_Download.txt"
        existing_text = review_path.read_text(encoding="utf-8") if review_path.exists() else ""
        # materials_download_dir is the exact same folder auto-downloaded
        # files land in (Description_Materials, right next to this file) --
        # pointing here specifically, rather than the generic phrase this
        # used to say, matters because a YouTube-derived course has no
        # 00_RAW/Courses/<name>/PDFs-style folder at all (that structure
        # only exists for local courses). The old wording told the user to
        # do something that was never possible for a YouTube video, and
        # there was previously no mechanism to notice a file placed here by
        # hand either -- see rescan_description_materials() below, which
        # closes that second half of the gap.
        materials_folder = Path(materials_download_dir)
        new_lines = [
            "Links found in a YouTube video description that could NOT be fetched automatically",
            "(interactive hosting like Google Drive/Dropbox, or a plain webpage, needs a human to open it):",
            "",
        ]
        for url in result["needs_manual_download"]:
            new_lines.append(f"  - {url}")
        new_lines.append("")
        new_lines.append(f"Once downloaded, save the file directly into this exact folder:")
        new_lines.append(f"  {materials_folder}")
        new_lines.append("Then in the dashboard's Courses tab, select this course and click")
        new_lines.append("'Extract Selected' -- it will be picked up and processed automatically,")
        new_lines.append("landing in 02_MATERIAL_EVIDENCE alongside every other material for this video.")
        new_lines.append("")
        review_path.write_text(existing_text + "\n".join(new_lines) + "\n\n", encoding="utf-8")

    return result


def rescan_description_materials(conn, course_id: str, video_output_dir: str) -> dict:
    """
    Picks up any file the user has manually saved into a video's
    Description_Materials folder -- the same folder auto-downloaded
    description links land in -- after following the instructions in
    Needs_Manual_Download.txt (typically a Google Drive/Dropbox file that
    couldn't be fetched headlessly).

    Closes a real gap: without this, a manually-downloaded file had
    nowhere to go. process_all_courses.py (the only thing that normally
    runs materials extraction) exclusively walks 00_RAW/Courses/*, and a
    YouTube-derived course_id was never in that list -- so a file placed
    here would sit on disk forever, invisible to the rest of the
    pipeline, with the app never even knowing it existed.

    Safe to call any time, including repeatedly: already-registered files
    (matched by content hash, same as every other ingestion path) are
    skipped, so only genuinely new files get processed.
    """
    materials_dir = Path(video_output_dir) / "Description_Materials"
    result = {"scanned_dir": str(materials_dir), "new_files_found": 0, "processed": []}
    if not materials_dir.is_dir():
        return result

    from pipeline.run_pipeline import process_material_file

    for path in sorted(materials_dir.iterdir()):
        if not path.is_file():
            continue
        file_type = classify_file_type(path)
        if file_type == "other":
            continue  # not a recognized material type -- leave it alone, don't guess
        content_hash = sha256_of_file(path)
        file_id = scoped_file_id(course_id, content_hash)
        existing = conn.execute("SELECT file_id FROM files WHERE file_id = ?", (file_id,)).fetchone()
        if existing:
            continue

        result["new_files_found"] += 1
        conn.execute(
            """INSERT INTO files
               (file_id, course_id, original_path, original_name, source_type,
                file_type, size_bytes, status, registered_at)
               VALUES (?, ?, ?, ?, 'manual_after_flagged', ?, ?, 'PENDING', ?)""",
            (file_id, course_id, str(path), path.name, file_type, path.stat().st_size,
             datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        material_result = process_material_file(
            conn, file_id, str(path), file_type,
            str(Path(video_output_dir) / "02_MATERIAL_EVIDENCE"),
        )
        result["processed"].append({"file": path.name, "file_type": file_type, "result": material_result})

    return result
