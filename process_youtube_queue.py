"""
Reads 00_RAW/YouTube_URLs/youtube_queue.csv and processes each PENDING URL
in order. Each row now also carries keep_video (yes/no) and keep_folder --
set when the URL is added via the app's "keep video" checkbox + folder
picker. If keep_video=no (the default), the downloaded video is deleted
once its extraction is fully confirmed complete, same policy as local
videos. If keep_video=yes, it's moved to keep_folder instead.

Requires yt-dlp on your Mac -- not testable in this sandbox (no internet).
Each successfully downloaded video is routed through the EXACT SAME
video_processor.py used for local sessions -- same evidence database,
same evidence structure. A strategy can end up backed by local evidence,
YouTube evidence, or both.
"""

import csv
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "engine"))

import config
from db.schema import get_connection, init_db
from processors.video_processor import process_video_file
from processors.cleanup import handle_youtube_video_retention, delete_intermediate_audio_if_processed
from pipeline.youtube_materials import process_youtube_description
from pipeline.run_pipeline import process_material_file

QUEUE_FIELDS = ["index", "url", "status", "keep_video", "keep_folder", "notes"]


def read_queue(queue_file: str) -> list:
    with open(queue_file, newline="") as f:
        rows = list(csv.DictReader(f))
    # backfill new columns for any older queue file that predates them
    for row in rows:
        row.setdefault("keep_video", "no")
        row.setdefault("keep_folder", "")
    return rows


def write_queue(queue_file: str, rows: list) -> None:
    with open(queue_file, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=QUEUE_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in QUEUE_FIELDS})


def add_url_to_queue(queue_file: str, url: str, keep_video: bool = False, keep_folder: str = "") -> None:
    """Called by the app when you paste a URL + set the keep-video option."""
    rows = read_queue(queue_file) if Path(queue_file).exists() else []
    next_index = f"{len(rows) + 1:02d}"
    rows.append({
        "index": next_index, "url": url, "status": "PENDING",
        "keep_video": "yes" if keep_video else "no", "keep_folder": keep_folder, "notes": "",
    })
    write_queue(queue_file, rows)


def fetch_youtube_source(url: str, download_dir: str) -> dict:
    import yt_dlp  # only imported when actually needed -- keeps this file importable without yt-dlp installed

    Path(download_dir).mkdir(parents=True, exist_ok=True)

    def progress_hook(d):
        if d["status"] == "downloading":
            pct_str = d.get("_percent_str", "0%").strip().replace("%", "")
            try:
                pct = float(pct_str)
                print(f"[progress] download {pct:.0f}%")
            except ValueError:
                pass
        elif d["status"] == "finished":
            print("[progress] download 100%")

    ydl_opts = {
        "format": "best[ext=mp4]/best",
        "outtmpl": str(Path(download_dir) / "%(id)s.%(ext)s"),
        "quiet": True,
        "noplaylist": True,
        "progress_hooks": [progress_hook],
        # more resilient to transient network blips (timeouts, dropped
        # connections) -- doesn't fix YouTube-side issues, but retries
        # harder before giving up on genuinely temporary failures
        "socket_timeout": 30,
        "retries": 5,
        "fragment_retries": 5,
        # HTTP 403 from YouTube via yt-dlp is very often the web player
        # client specifically being throttled/blocked, even when the
        # video itself is perfectly public -- forcing yt-dlp to try the
        # Android/iOS player API first is the standard, documented
        # workaround for exactly this error signature.
        "extractor_args": {"youtube": {"player_client": ["android", "ios", "web"]}},
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
        },
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        video_path = ydl.prepare_filename(info)

    return {
        "video_path": video_path,
        "video_id": info["id"],
        "title": info.get("title", ""),
        "description": info.get("description", ""),
    }


def process_youtube_queue():
    init_db(config.DB_PATH)
    conn = get_connection(config.DB_PATH)
    rows = read_queue(str(config.YOUTUBE_QUEUE_FILE))

    # Diagnostics -- this is what was missing before: a silent "Queue run
    # complete" with zero explanation when there was nothing to actually do.
    status_counts = {}
    for row in rows:
        status_counts[row["status"]] = status_counts.get(row["status"], 0) + 1
    print(f"Queue file: {config.YOUTUBE_QUEUE_FILE}")
    print(f"Total rows: {len(rows)}  |  By status: {status_counts or '(no rows at all)'}")

    # Self-heal: a row stuck at PROCESSING means a previous run crashed or
    # was force-quit mid-URL -- without this, that row would silently never
    # be retried again, since the loop below only picks up PENDING rows.
    stuck = [r for r in rows if r["status"] == "PROCESSING"]
    if stuck:
        print(f"Found {len(stuck)} row(s) stuck at PROCESSING from an earlier interrupted run -- resetting to PENDING so they retry.")
        for row in stuck:
            row["status"] = "PENDING"
        write_queue(str(config.YOUTUBE_QUEUE_FILE), rows)

    if not rows:
        print("Queue is empty -- add a URL in the YouTube Queue tab first.")
        conn.close()
        return
    if not any(r["status"] == "PENDING" for r in rows):
        print("No PENDING rows -- every URL already succeeded or failed. "
              "To retry a failed one, change its status back to PENDING in youtube_queue.csv, or add it again.")
        conn.close()
        return

    # Same fix as the local-course pipeline: know the total count of
    # videos to process UP FRONT, and emit an overall progress line
    # before the first one even starts, so the FIRST video's download/
    # transcription progress correctly blends into the whole queue's
    # progress instead of showing its own raw 0-100% as if it were
    # everything.
    total_pending = sum(1 for r in rows if r["status"] == "PENDING")
    completed_count = 0

    def report_queue_progress():
        pct = min(100, round(completed_count / total_pending * 100)) if total_pending else 0
        print(f"[progress] course=YouTube Queue {pct}% ({completed_count}/{total_pending} items)")

    report_queue_progress()

    for row in rows:
        if row["status"] != "PENDING":
            continue

        url = row["url"].strip()
        keep_video = row.get("keep_video", "no").lower() == "yes"
        keep_folder = row.get("keep_folder", "")
        print(f"\n--- Queue #{row['index']}: {url} (keep_video={keep_video}) ---")
        row["status"] = "PROCESSING"
        write_queue(str(config.YOUTUBE_QUEUE_FILE), rows)

        try:
            source = fetch_youtube_source(url, str(config.OUTPUT_YOUTUBE_DIR / "_downloads"))
            video_id = source["video_id"]
            # Folder name includes the title (not just the ID) so multiple
            # queued videos are recognizable at a glance in Finder --
            # course_manager.output_root_for_course() computes this exact
            # same name later (from the title stored below), so extraction
            # output and AI-stage output always land in the same folder.
            from pipeline.course_manager import youtube_output_folder_name
            folder_name = youtube_output_folder_name(video_id, source["title"])
            out_dir = config.OUTPUT_YOUTUBE_DIR / folder_name
            out_dir.mkdir(parents=True, exist_ok=True)

            file_id = str(uuid.uuid4())
            session_id = str(uuid.uuid4())
            conn.execute(
                """INSERT INTO files (file_id, course_id, original_path, original_name,
                   source_type, file_type, status, youtube_url, youtube_video_id, registered_at)
                   VALUES (?, ?, ?, ?, 'youtube', 'video', 'PENDING', ?, ?, ?)""",
                (file_id, f"youtube_{video_id}", source["video_path"], source["title"],
                 url, video_id, datetime.now(timezone.utc).isoformat()),
            )
            conn.execute(
                """INSERT INTO sessions (session_id, file_id, course_id, session_type, title)
                   VALUES (?, ?, ?, 'main', ?)""",
                (session_id, file_id, f"youtube_{video_id}", source["title"]),
            )
            conn.commit()

            # Capture the description itself as evidence, and fetch anything
            # directly downloadable that it links to (course notes, an Excel
            # calculator, strategy PDFs, etc). Runs before video processing
            # so this evidence is captured even if transcription later fails.
            desc_result = process_youtube_description(
                conn, file_id, f"youtube_{video_id}", source.get("description", ""),
                materials_download_dir=str(out_dir / "Description_Materials"),
                manual_review_output_dir=str(out_dir),
            )
            print("  description:", desc_result)

            # A file auto-downloaded from the description has no local
            # course folder to be picked up by process_all_courses.py's
            # Stage 4 (that script only walks 00_RAW/Courses/*) -- so it
            # would sit at PENDING forever unless processed right here,
            # using the exact same dispatcher run_pipeline.py's own
            # Stage 4 uses, just for this one file.
            for downloaded in desc_result.get("auto_downloaded", []):
                dl_file_id = downloaded["file_id"]
                dl_row = conn.execute(
                    "SELECT original_path, file_type FROM files WHERE file_id = ?", (dl_file_id,)
                ).fetchone()
                if dl_row:
                    dl_path, dl_file_type = dl_row
                    print(f"  processing description-linked material ({dl_file_type}): {dl_path}")
                    dl_result = process_material_file(
                        conn, dl_file_id, dl_path, dl_file_type,
                        out_dir / "02_MATERIAL_EVIDENCE",
                    )
                    print("   ", dl_result)

            result = process_video_file(conn, file_id, session_id, source["video_path"], str(out_dir))
            print("  processed:", result)

            retention = handle_youtube_video_retention(conn, file_id, keep=keep_video, keep_folder=keep_folder)
            print("  storage:", retention)
            audio_cleanup = delete_intermediate_audio_if_processed(conn, file_id, str(out_dir))
            print("  audio cleanup:", audio_cleanup)

            row["status"] = "SUCCESS"
            row["notes"] = f"video_id={video_id}, storage={retention['action']}"

        except Exception as e:
            row["status"] = "FAILED"
            row["notes"] = str(e)[:200]
            print(f"  FAILED: {e}")

        completed_count += 1
        report_queue_progress()
        write_queue(str(config.YOUTUBE_QUEUE_FILE), rows)

    conn.close()
    print("\nQueue run complete. Current status:")
    for row in read_queue(str(config.YOUTUBE_QUEUE_FILE)):
        print(" ", row["index"], row["url"], "->", row["status"])


if __name__ == "__main__":
    process_youtube_queue()
