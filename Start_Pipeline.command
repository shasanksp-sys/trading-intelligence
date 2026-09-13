#!/bin/bash
# Double-click this file in Finder to run it.
# It opens Terminal automatically and shows a simple menu -- no typing commands.

cd "$(dirname "$0")"

echo "=================================================="
echo "   TradingIntelligence — Course Processing"
echo "=================================================="
echo ""
echo "  1) Process ALL courses (Course_01, 02, 03...)"
echo "  2) Process ONE course (you'll be asked which)"
echo "  3) Process the YouTube queue"
echo "  4) Show what needs review right now"
echo "  5) Show processing status for all courses"
echo "  0) Quit"
echo ""
read -p "Choose an option (0-5): " choice

case $choice in
  1)
    python3 process_all_courses.py
    ;;
  2)
    read -p "Which course folder name? (e.g. Course_01): " coursename
    python3 process_all_courses.py "$coursename"
    ;;
  3)
    python3 process_youtube_queue.py
    ;;
  4)
    sqlite3 db/TradingIntelligence.db \
      "SELECT s.title, ts.start_seconds, ts.end_seconds, ts.text FROM transcript_segments ts JOIN sessions s ON s.session_id = ts.session_id WHERE ts.needs_review = 1;"
    ;;
  5)
    sqlite3 db/TradingIntelligence.db \
      "SELECT course_id, file_type, status, stage_completed FROM files ORDER BY course_id;"
    ;;
  0)
    exit 0
    ;;
  *)
    echo "Not a valid option."
    ;;
esac

echo ""
echo "Done. Press Enter to close this window."
read
