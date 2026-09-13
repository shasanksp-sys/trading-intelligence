"""
Rescans one or more YouTube-derived courses' Description_Materials folder
for files the user has manually downloaded and saved there (typically a
Google Drive/Dropbox link that Needs_Manual_Download.txt flagged, since
those can't be fetched automatically without a login).

Previously there was no way to run this at all: process_all_courses.py
(the only thing that normally does materials extraction) exclusively
walks 00_RAW/Courses/*, so a youtube_<video_id> course_id was never in
its list. Selecting a YouTube course row in the dashboard and clicking
"Extract Selected" silently did nothing useful. Wired into dashboard.py
so that button now routes here for youtube_-prefixed course_ids instead.

Usage:
    python3 rescan_youtube_materials.py                      # every YouTube course
    python3 rescan_youtube_materials.py youtube_ria60hCiv0Q   # just one
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "engine"))

import config
from db.schema import get_connection
from pipeline.course_manager import output_root_for_course
from pipeline.youtube_materials import rescan_description_materials


def run(course_filter: str = None):
    conn = get_connection(config.DB_PATH)
    rows = conn.execute(
        "SELECT DISTINCT course_id FROM files WHERE course_id LIKE 'youtube_%' ORDER BY course_id"
    ).fetchall()
    course_ids = [r[0] for r in rows]

    if course_filter:
        if course_filter not in course_ids:
            print(f"'{course_filter}' not found among YouTube courses: {course_ids}")
            return
        course_ids = [course_filter]

    if not course_ids:
        print("No YouTube-derived courses found yet -- process a video from the YouTube Queue tab first.")
        return

    for course_id in course_ids:
        video_output_dir = output_root_for_course(
            course_id, str(config.OUTPUT_COURSES_DIR), str(config.OUTPUT_YOUTUBE_DIR), conn=conn
        )
        print(f"\n########## {course_id} ##########")
        result = rescan_description_materials(conn, course_id, video_output_dir)
        if result["new_files_found"]:
            print(f"  Found and processed {result['new_files_found']} new manually-added file(s):")
            for item in result["processed"]:
                print(f"    {item['file']} ({item['file_type']}) -> {item['result']}")
        else:
            print("  Nothing new in Description_Materials.")

    conn.close()


if __name__ == "__main__":
    filter_arg = sys.argv[1] if len(sys.argv) > 1 else None
    run(filter_arg)
