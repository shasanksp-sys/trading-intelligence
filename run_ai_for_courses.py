"""
Runs the AI-dependent stages (tagging -> linking -> reconstruction ->
quality check) for one or more SELECTED courses -- this is what "hold
only the AI part, process the rest, run AI later on just what I choose"
maps to.

Usage:
    python3 run_ai_for_courses.py Course_01                 # just one
    python3 run_ai_for_courses.py Course_01 Course_03        # a few
    python3 run_ai_for_courses.py --all-pending              # every course sitting at EXTRACTED_AWAITING_AI
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "engine"))

import config
import settings
from db.schema import get_connection, init_db
from pipeline.ai_pipeline import run_ai_pipeline_for_courses
from pipeline.course_manager import list_all_courses_with_status


def main():
    api_key = settings.get_api_key()
    if not api_key:
        print("No Anthropic API key set. Open Settings in the app, or add one to settings.local.json, first.")
        return

    s = settings.load_settings()
    init_db(config.DB_PATH)
    conn = get_connection(config.DB_PATH)

    if "--all-pending" in sys.argv:
        all_courses = list_all_courses_with_status(conn, str(config.RAW_COURSES_DIR))
        course_ids = [c["course_id"] for c in all_courses if c["status"] == "EXTRACTED_AWAITING_AI"]
        if not course_ids:
            print("No courses currently sitting at 'Extracted -- Awaiting AI'.")
            return
    else:
        course_ids = [a for a in sys.argv[1:] if not a.startswith("--")]
        if not course_ids:
            print("Specify one or more course names, or use --all-pending.")
            return

    print(f"Running AI stages for: {course_ids}")
    results = run_ai_pipeline_for_courses(
        conn, course_ids, str(config.OUTPUT_COURSES_DIR), api_key,
        tagging_model=s["tagging_model"], reconstruction_model=s["reconstruction_model"],
        output_youtube_dir=str(config.OUTPUT_YOUTUBE_DIR),
    )

    total_cost = sum(r["actual_cost_usd"] for r in results)
    print(f"\n\n===== ALL SELECTED COURSES DONE -- total cost this run: ~${total_cost:.4f} =====")
    conn.close()


if __name__ == "__main__":
    main()
