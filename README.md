# TradingIntelligence_Main

This is the actual folder structure you asked for — Main Folder, with RAW
inputs and OUTPUT results kept separate, courses side by side, and a
YouTube queue that runs independently but feeds the same evidence engine.

## Structure

```
TradingIntelligence_Main/
├── 00_RAW/                              <- YOU only ever put files in here
│   ├── Courses/
│   │   ├── Course_01/
│   │   │   ├── Main_Sessions/
│   │   │   ├── Supporting_Sessions/
│   │   │   ├── PDFs/
│   │   │   ├── Excel/
│   │   │   ├── Screenshots/
│   │   │   └── Other/
│   │   ├── Course_02/  (same subfolders)
│   │   └── Course_03/  (same subfolders)
│   └── YouTube_URLs/
│       └── youtube_queue.csv            <- paste URLs here, numbered
│
├── 01_OUTPUT/                           <- system writes here, you don't touch it
│   ├── Courses/
│   │   ├── Course_01/
│   │   │   ├── 01_VIDEO_EVIDENCE/ (Transcripts, Audio, Key_Frames, OCR)
│   │   │   ├── 02_MATERIAL_EVIDENCE/ (PDF_Extraction, Excel_Analysis, Screenshot_OCR)
│   │   │   ├── 03_EVIDENCE_LINKING/
│   │   │   ├── 04_STRATEGY_RECONSTRUCTION/
│   │   │   ├── 05_REVIEW/
│   │   │   └── 06_FINAL_STRATEGIES/
│   │   ├── Course_02/  (same)
│   │   └── Course_03/  (same)
│   └── YouTube/
│       └── <video_id>/                  <- one folder per processed URL
│
├── db/
│   └── TradingIntelligence.db           <- ONE shared evidence database for everything
│
├── engine/                              <- the tested processing code (don't need to touch)
│   ├── db/schema.py
│   ├── processors/ (excel, pdf, image, video)
│   └── pipeline/ (ingest, run_pipeline)
│
├── config.py                            <- all paths defined once, here
├── process_all_courses.py               <- run this for local courses
├── process_youtube_queue.py             <- run this for YouTube
└── requirements.txt
```

## v3 update — mlx-whisper (confirmed: your Mac is Apple Silicon M2)

Transcription now prefers `mlx-whisper` (`large-v3-turbo` model) automatically
over `faster-whisper` — no config needed, the code detects which is installed
and uses mlx-whisper if present. This should be both faster (runs on the M2's
Neural Engine, not CPU) and more accurate (large-v3-turbo vs small) than the
v2 CPU fallback.

**Install it:** `pip3 install mlx-whisper` — then re-run the pipeline; the
progress log will show `engine=mlx-whisper` instead of `engine=faster-whisper`
to confirm it's active.

**Known trade-off:** mlx-whisper processes the whole file in one call rather
than streaming segment-by-segment, so you won't see a live percentage the way
the old faster-whisper path showed — instead you'll see a heartbeat line every
20 seconds confirming it's still working, and the final result (with per-segment
timestamps) once the whole pass completes.

## v2 update — fixes from the real 115-minute WMV test run

Your diagnostic on the actual course video surfaced three real bugs, now fixed and re-tested:

1. **Whisper model default changed from `medium` to `small`.** Medium + full diarization on 115 CPU-bound minutes was genuinely too slow. Edit `WHISPER_MODEL_SIZE` at the top of `engine/processors/video_processor.py` to go back to `medium`/`large-v3` later once you trust the pipeline and are willing to spend the extra time.
2. **Diarization is now OFF by default** (`ENABLE_DIARIZATION = False`, same file). This was the single heaviest stage on CPU. Every segment stays `speaker_role='unknown'` until you flip this on — matches the plan's own priority: what the trainer said and when, first; perfect speaker separation, later.
3. **Checkpoint/resume is now actually implemented**, not just claimed. Previously, re-running a file that had already reached `audio_extracted` would unconditionally re-extract audio and redo everything. Tested and confirmed: a file stuck at `audio_extracted` now resumes straight into transcription, skipping the already-completed stage. A fully-`SUCCESS` file is skipped entirely on the next run.
4. **Live progress output added** during transcription — prints elapsed/total minutes and percentage periodically, instead of going silent for hours.
5. **`.wmv` is now a recognized video extension** (added directly to the code — no manual edit needed on your Mac anymore).

## What's tested and confirmed working (in this environment)

- `python3 process_all_courses.py` — scans `00_RAW/Courses/`, finds every
  course folder automatically, processes each one (main videos →
  supporting videos → materials, in that order), writes evidence into the
  matching `01_OUTPUT/Courses/<name>/` folder, and correctly does nothing
  to courses with no new files.
- `python3 process_all_courses.py Course_01` — process just one course by name.
- Confirmed: dropping test material into `Course_01` while `Course_02` and
  `Course_03` stayed empty resulted in Course_01 fully processing and the
  other two being safely skipped — no errors, no wasted work.
- `youtube_queue.csv` reads and writes correctly, status column tracks
  PENDING → PROCESSING → SUCCESS/FAILED per your numbered-list format.

## What's wired but not download-tested (needs your Mac's internet)

- `process_youtube_queue.py` — the actual `yt-dlp` download call
  (`fetch_youtube_source`) can't be tested in this sandbox (no internet
  access here). The queue-reading, status-tracking, and failure-isolation
  logic around it IS tested. On your Mac: `pip install yt-dlp`, paste real
  URLs into `youtube_queue.csv`, run the script — if URL #2 fails, #1's
  result stays saved and #3 still runs.
- Video transcription (`faster-whisper`) and diarization (`pyannote.audio`)
  — same as before, need model downloads on your Mac.

## How to use it

```bash
# 1. Set up once
brew install ffmpeg tesseract
pip install -r requirements.txt

# 2. Drop material
#    Course_01/Main_Sessions/session1.mp4, session2.mp4, ...
#    Course_01/Excel/tts_calc.xlsx, ...
#    (repeat for Course_02, Course_03, or add Course_04+ the same way)

# 3. Process all local courses
python3 process_all_courses.py

# 4. Add YouTube URLs to 00_RAW/YouTube_URLs/youtube_queue.csv, then:
python3 process_youtube_queue.py

# 5. Check what needs your review
sqlite3 db/TradingIntelligence.db \
  "SELECT * FROM transcript_segments WHERE needs_review = 1;"
```

## Important boundary — still true here

Only queue YouTube URLs you're actually authorized to download and use —
your own uploads, or content you have explicit rights to. The system will
not fabricate an evidence source; it only extracts what it's given
permission to access.

## What is NOT built yet (the honest remaining gap)

Stages `03_EVIDENCE_LINKING` and `04_STRATEGY_RECONSTRUCTION` are folders
waiting to be filled — that's the AI-driven cross-linking and strategy
reconstruction step, which is deliberately not yet built. Per the plan's
own core principle, that step should only run once you've confirmed
transcription quality on real course audio — building the AI layer on top
of unverified transcripts would risk baking transcription errors directly
into "final" strategies.
