"""
Run this to process EVERY course sitting under 00_RAW/Courses — this is the
actual "Course 1, Course 2, Course 3 ... in auto" entry point.

Usage:
    python3 process_all_courses.py                 # process every course
    python3 process_all_courses.py Course_02        # process just one

Each course's raw material comes from  00_RAW/Courses/<name>/
Each course's evidence + eventual strategies land in  01_OUTPUT/Courses/<name>/
All courses share one evidence database at  db/TradingIntelligence.db
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "engine"))

import config
import settings
from pipeline.run_pipeline import process_course


def run(course_filter: str = None):
    courses = config.list_courses()
    if not courses:
        print(f"No course folders found under {config.RAW_COURSES_DIR}")
        print("Create one, e.g. 00_RAW/Courses/Course_01, and drop material into its subfolders.")
        return

    if course_filter:
        if course_filter not in courses:
            print(f"'{course_filter}' not found. Available: {courses}")
            return
        courses = [course_filter]

    s = settings.load_settings()
    print(f"Courses to process: {courses}\n")

    for course_name in courses:
        raw_dir = config.raw_course_dir(course_name)
        out_dir = config.output_course_dir(course_name)
        print(f"\n########## {course_name} ##########")
        try:
            process_course(
                course_id=course_name,
                raw_source_dir=raw_dir,
                db_path=config.DB_PATH,
                work_dir=str(Path(out_dir) / "01_VIDEO_EVIDENCE"),
                auto_delete_video=s["auto_delete_local_video"],
            )
        except Exception as e:
            # Per-file failures are already caught inside process_course --
            # this is the outer safety net so an unexpected error at the
            # course level (e.g. during scan or cleanup) can't take every
            # OTHER course in this run down with it when running "Extract
            # All" / no course_filter.
            print(f"\n!! Course '{course_name}' failed: {type(e).__name__}: {e}")
            print(f"!! Continuing with remaining courses.\n")


if __name__ == "__main__":
    filt = sys.argv[1] if len(sys.argv) > 1 else None
    run(filt)
