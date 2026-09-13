#!/bin/bash
# Wipes ALL test/demo data so you can start real course processing from a
# clean slate: the shared database, everything inside course raw/output
# folders, and the YouTube queue. Does NOT touch any code files, and does
# NOT delete the course folder structure itself -- Course_01/02/03 (and
# any you've added) stay in place, just emptied out.
#
# Run this ONLY after you've confirmed any real files (like a video sitting
# outside the project) are safely backed up elsewhere -- this script only
# touches things INSIDE TradingIntelligence_Main, but always worth a pause
# before a destructive action.

cd "$(dirname "$0")" || exit 1

echo "This will permanently delete:"
echo "  - db/TradingIntelligence.db (all evidence, tags, strategies)"
echo "  - every file inside 00_RAW/Courses/*/ subfolders (videos, Excel, PDFs, etc.)"
echo "  - every file inside 01_OUTPUT/Courses/*/ subfolders (transcripts, strategies, etc.)"
echo "  - every file inside 01_OUTPUT/YouTube/"
echo "  - the YouTube queue (00_RAW/YouTube_URLs/youtube_queue.csv) will be reset to empty"
echo ""
echo "This will NOT delete: any code files, the course folder structure itself,"
echo "or anything outside this project folder."
echo ""
read -p "Type YES to proceed: " confirm

if [ "$confirm" != "YES" ]; then
    echo "Cancelled -- nothing was deleted."
    read -p "Press Enter to close."
    exit 0
fi

rm -f db/TradingIntelligence.db
echo "Database reset."

find 00_RAW/Courses -mindepth 2 -type f ! -name ".gitkeep" -delete
echo "Raw course files cleared."

find 01_OUTPUT/Courses -mindepth 1 -type f ! -name ".gitkeep" -delete
find 01_OUTPUT/YouTube -mindepth 1 -type f ! -name ".gitkeep" -delete 2>/dev/null
echo "Output files cleared."

# put the .gitkeep placeholders back so the folder structure stays intact
find 00_RAW 01_OUTPUT -type d -empty -exec touch {}/.gitkeep \;

echo "index,url,status,keep_video,keep_folder,notes" > 00_RAW/YouTube_URLs/youtube_queue.csv
echo "YouTube queue reset."

rm -f launcher.log
echo ""
echo "Done. The project folder structure is intact and empty, ready for real material."
read -p "Press Enter to close."
