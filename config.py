"""
Central configuration for the TradingIntelligence_Main layout.

Everything else in engine/ reads paths from here — so adding Course_04
later, or repointing to a different Main folder, is a one-line change,
not a code change.
"""

from pathlib import Path

MAIN_FOLDER = Path(__file__).resolve().parent

RAW_COURSES_DIR = MAIN_FOLDER / "00_RAW" / "Courses"
YOUTUBE_URLS_DIR = MAIN_FOLDER / "00_RAW" / "YouTube_URLs"
YOUTUBE_QUEUE_FILE = YOUTUBE_URLS_DIR / "youtube_queue.csv"

OUTPUT_COURSES_DIR = MAIN_FOLDER / "01_OUTPUT" / "Courses"
OUTPUT_YOUTUBE_DIR = MAIN_FOLDER / "01_OUTPUT" / "YouTube"

DB_PATH = str(MAIN_FOLDER / "db" / "TradingIntelligence.db")


def raw_course_dir(course_name: str) -> str:
    """e.g. raw_course_dir('Course_01') -> .../00_RAW/Courses/Course_01"""
    return str(RAW_COURSES_DIR / course_name)


def output_course_dir(course_name: str) -> str:
    return str(OUTPUT_COURSES_DIR / course_name)


def list_courses() -> list:
    """Every course folder currently sitting under 00_RAW/Courses."""
    if not RAW_COURSES_DIR.exists():
        return []
    return sorted(p.name for p in RAW_COURSES_DIR.iterdir() if p.is_dir())
