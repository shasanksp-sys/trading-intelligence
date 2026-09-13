# TradingIntelligence — Project Summary

**Purpose:** Turn a trading course (recorded video sessions + supporting
Excel/PDF/Docs/screenshots) or a YouTube video into structured, auditable
trading strategies — where every rule in the final output can be traced
back to the exact video timestamp, Excel cell, or PDF page it came from.
No rule is ever invented by the AI; anything not clearly supported by
evidence is explicitly flagged instead of guessed.

This document is written so anyone — including a future version of
whoever built this — can read it once and understand the whole system
well enough to keep working on it, without needing prior conversation
history.

---

## 1. The core principle everything else serves

> Every trading rule in a final strategy must cite its exact source
> (timestamp / page / cell). If evidence is missing or unclear, the
> system says so — it never fills the gap with plausible-sounding
> AI knowledge.

Every architectural decision below — the evidence database, the citation
requirement in AI prompts, the human-review checkpoint — exists to keep
this promise. When extending this system, any change that would make a
strategy's origin less traceable is working against the actual point of
the project.

---

## 2. High-level flow

```
LOCAL COURSE FOLDER              YOUTUBE URL QUEUE
(videos + Excel + PDF +          (paste URLs, choose
 Docs + screenshots)              keep/delete video)
        │                                │
        └────────────┬───────────────────┘
                      ▼
         STAGE 1: EXTRACTION (no AI, no API key needed)
         transcription, Excel formulas+values, PDF text+tables,
         screenshot OCR — all local, all free
                      ▼
         STAGE 2: TOPIC TAGGING (cheap AI model)
         classify each piece of evidence by topic
                      ▼
         STAGE 3: CROSS-LINKING (mostly free, light AI assist)
         group evidence sharing a topic into candidate strategies
                      ▼
         STAGE 4: STRATEGY RECONSTRUCTION (strong AI model)
         one AI call per strategy, evidence-only, citations required
                      ▼
         STAGE 5: QUALITY CHECK (no AI, deterministic)
         flags anything with open questions or low-confidence evidence
                      ▼
         STAGE 6: HUMAN REVIEW (the only manual step)
         you approve/reject flagged strategies in the GUI
                      ▼
         06_FINAL_STRATEGIES/  ← the actual deliverable
```

**Key property:** Stage 1 needs zero AI/API access — it's pure local
processing (Whisper transcription, OCR, Excel/PDF parsing). Stages 2–4
need an Anthropic API key. This means you can run Stage 1 on many courses
before ever getting an API key, and only spend money on the courses you
specifically choose to run AI on, whenever you choose to.

---

## 3. Folder structure

```
TradingIntelligence_Main/                    <- lives in your Home folder
├── 00_RAW/
│   ├── Courses/
│   │   ├── Course_01/
│   │   │   ├── Main_Sessions/               <- video files here (folder name MUST contain "main")
│   │   │   ├── Supporting_Sessions/         <- video files here (folder name MUST contain "supporting")
│   │   │   ├── PDFs/
│   │   │   ├── Excel/
│   │   │   ├── Docs/
│   │   │   ├── Screenshots/
│   │   │   └── Other/
│   │   ├── Course_02/  (identical structure)
│   │   └── ...
│   └── YouTube_URLs/
│       └── youtube_queue.csv                <- URLs + keep/delete choice live here
│
├── 01_OUTPUT/
│   ├── Courses/
│   │   └── Course_01/
│   │       ├── 01_VIDEO_EVIDENCE/           <- transcripts, audio, key frames, OCR
│   │       ├── 02_MATERIAL_EVIDENCE/        <- Excel/PDF/screenshot extraction results
│   │       ├── 03_EVIDENCE_LINKING/         <- (informational; linking itself lives in the DB)
│   │       ├── 04_STRATEGY_RECONSTRUCTION/  <- one JSON file per strategy, post-AI
│   │       ├── 05_REVIEW/                   <- review_report.json (what's flagged)
│   │       └── 06_FINAL_STRATEGIES/         <- approved strategies land here
│   └── YouTube/
│       └── <video_id>/  (same six-stage structure as a course)
│
├── db/
│   └── TradingIntelligence.db               <- ONE shared SQLite database, all courses + YouTube
│
├── engine/                                  <- all processing code
│   ├── db/schema.py                         <- database schema
│   ├── processors/                          <- one file per processing stage
│   └── pipeline/                            <- orchestration (run order, course status)
│
├── config.py                                <- all folder paths, defined once
├── settings.py                              <- API key + model + storage-policy storage
├── settings.local.json                      <- created at runtime, holds your actual API key (never share this file)
├── dashboard.py                             <- the GUI (5 tabs — see section 6)
├── process_all_courses.py                   <- CLI: run extraction (Stage 1) on courses
├── run_ai_for_courses.py                    <- CLI: run AI stages (2-5) on SELECTED courses
├── process_youtube_queue.py                 <- CLI: process the YouTube queue
├── Launch_Dashboard.command                 <- double-click to open the GUI
└── requirements.txt
```

**Rule for placing videos:** a video only gets picked up as "main" or
"supporting" based on which folder it sits in — the code checks for the
substring "main" or "supporting" in the folder path. A video placed
directly in the course root, or in a folder without one of those words,
will not be processed. Materials (Excel/PDF/Docs/Screenshots) are
identified by file extension regardless of subfolder, but keeping them in
their labeled folders is still the right habit for your own navigation.

---

## 4. Database schema (SQLite, `db/TradingIntelligence.db`)

| Table | Purpose |
|---|---|
| `files` | Every ingested file — path, type, hash (for dedup), processing status, checkpoint stage |
| `sessions` | One row per video (main or supporting), links to its file |
| `transcript_segments` | Timestamped Whisper output — one row per spoken segment, with confidence score |
| `key_frames` | Extracted video frames — REAL timestamp, OCR text, why it was captured (scene-change vs periodic) |
| `document_extractions` | Excel formulas+values, PDF text+tables, screenshot OCR — each with its exact location (cell/page) |
| `evidence` | Unified layer over the four tables above — adds topic_tags and related_strategy_id for AI stages |
| `strategies` | One row per reconstructed strategy — spec_json holds the full structured output, approved flag for Stage 6 |

Course identity is just a string (`course_id`) used consistently across
all tables — local courses use their folder name (e.g. `Course_01`),
YouTube videos use `youtube_<video_id>`. There's no separate "courses"
table; a course's status is *computed* from these tables on demand (see
`engine/pipeline/course_manager.py`), so it can never drift out of sync
with what's actually been processed.

---

## 5. The six stages, in technical detail

### Stage 1 — Extraction (`engine/pipeline/run_pipeline.py`, `engine/processors/*_processor.py`)
- **Video** (`video_processor.py`): audio extracted via `ffmpeg`. Transcription
  prefers `mlx-whisper` (Apple Silicon, chunked into 10-minute pieces for
  memory safety on 8GB RAM) and falls back to `faster-whisper` (CPU) if
  MLX isn't installed. Key frames captured on scene-change (`ffmpeg`
  scene-detection filter) plus a periodic baseline sample, each with a
  real timestamp. Every frame is OCR'd.
- **Excel** (`excel_processor.py`): every non-empty cell extracted with
  BOTH its formula string and calculated value — never collapsed to just
  a number, since the formula is often the actual trading logic.
- **PDF** (`pdf_processor.py`): text + tables per page via `pdfplumber`;
  embedded images extracted too if `pymupdf` is installed.
- **Screenshots** (`image_processor.py`): OCR via `pytesseract`.
- **Processing order**: main sessions → supporting sessions → materials,
  deliberately — materials are meant to be interpreted in light of what
  the videos already established, not processed blind.
- **Checkpointing**: each file's `stage_completed` column tracks progress
  (`audio_extracted` → `transcript_done` → `frames_done`). Re-running a
  course skips whatever's already done — safe to stop and resume anytime,
  including across days.
- **Auto-cleanup**: once a video reaches `frames_done`, its raw file is
  deleted automatically (configurable in Settings). This never fires
  early — a partially-processed video's source is never at risk.

### Stage 2 — Topic tagging (`engine/processors/tagging_processor.py`)
Backfills the `evidence` table from `transcript_segments` +
`document_extractions`, then sends untagged evidence in batches of 20 to
a cheap model (`claude-haiku-4-5-20251001` by default) asking for a short
topic label per item. This is classification, not synthesis — doesn't
need a strong model.

### Stage 3 — Cross-linking (`engine/processors/linking_processor.py`)
Clusters similar tags together using plain text-similarity matching
(Python's `difflib`), no AI call. **Known limitation, tested and
confirmed**: this can over-merge tags that are lexically similar but
conceptually different (e.g. two tags both containing "POB" might merge
even if one is about entries and one about exits). Acceptable for now
given the low cost, but worth revisiting with embedding-based similarity
if this proves inaccurate on real course data.

### Stage 4 — Strategy reconstruction (`engine/processors/reconstruction_processor.py`)
For each strategy group, ALL of its evidence (transcript excerpts with
timestamps, Excel formulas with cell refs, PDF text with page numbers,
OCR'd screenshots) is bundled and sent to a strong model
(`claude-sonnet-4-5` by default) with a prompt that strictly requires
citations and explicitly instructs the model to write `"INSUFFICIENT
EVIDENCE"` rather than invent a plausible rule. Output is a structured
JSON spec (entry/exit/stop/target/Excel dependency/indicator
dependency/examples/open_questions/source_citations).

### Stage 5 — Quality check (`engine/processors/quality_check.py`)
Deterministic, no AI. Scans each reconstructed strategy's `open_questions`
and any `"INSUFFICIENT EVIDENCE"` fields, plus whether any of its
supporting transcript segments were low-confidence. Writes one
`review_report.json` per course listing exactly what needs a human look.

### Stage 6 — Human review (GUI "Review" tab)
The only manual step, by design. You see flagged strategies with their
open questions, can view the full spec, and approve or leave flagged.
Approving copies the spec into `06_FINAL_STRATEGIES/` and marks it
`approved = 1` in the database.

### Storage cleanup (`engine/processors/cleanup.py`)
- `cleanup_course_videos()` — auto-runs after Stage 1, deletes local
  videos whose extraction fully completed.
- `handle_youtube_video_retention()` — runs after a YouTube video's
  extraction, deletes it or moves it to your chosen folder based on the
  per-URL "keep video" choice.
- `prune_uncited_frames()` — available but NOT auto-wired into any
  pipeline yet; intended to run after Stage 6 approval, removing key
  frames not tied to an approved strategy's evidence. Currently a manual
  call — a good candidate for a "Prune Unused Frames" button in a future
  GUI update.

---

## 6. The GUI (`dashboard.py`) — five tabs

| Tab | What it does |
|---|---|
| **Courses** | List of courses with live status (Not Started / Extracted—Awaiting AI / AI Processing / Needs Review / Complete). Multi-select courses (click, Cmd-click, Shift-click) for the AI buttons. "New Course" creates the full folder skeleton for you. |
| **YouTube Queue** | Paste a URL, optionally check "Keep the video" + choose a folder, click Add to Queue. "Process Queue" runs every pending URL in order, isolating failures. |
| **Live Progress** | Streamed output from whatever's currently running — extraction, AI processing, or the YouTube queue. Stop button available. |
| **Review** | Every flagged strategy across all courses. View full details, approve selected. |
| **Settings** | API key, tagging/reconstruction model choice, auto-delete-video toggle. |

Heavy operations (extraction, AI runs, YouTube queue) run as a background
subprocess of the matching CLI script (`process_all_courses.py`,
`run_ai_for_courses.py`, `process_youtube_queue.py`) and stream their
output live into the Live Progress tab — so GUI behavior and typing the
command yourself produce identical results, just with clicking instead of
typing.

**Testing status, stated plainly:** the GUI's logic was tested against a
simulated tkinter environment (every tab builder and every button-handler
method executed without error, including verifying `New Course` correctly
creates real folders on disk). Actually launching and clicking through
the real macOS window has NOT been verified, since the build environment
has no display server. This is the first thing to check when picking this
up on the Mac.

---

## 7. Setup (on the Mac)

```bash
# One-time system dependencies
brew install ffmpeg tesseract

# One-time Python dependencies
pip3 install -r requirements.txt
# On Apple Silicon, mlx-whisper is the preferred transcription engine
# (already in requirements.txt) — faster-whisper is an automatic fallback.

# Make the launcher executable (one-time)
chmod +x Launch_Dashboard.command Start_Pipeline.command

# First launch only: right-click Launch_Dashboard.command -> Open
# (macOS Gatekeeper warns on unsigned scripts the first time only)
```

**Getting an API key:** platform.claude.com (a separate product from the
claude.ai chat app — a claude.ai Pro/Max subscription does NOT include
API access). Pay-as-you-use, no monthly fee for API access itself; you
add a payment method and it draws from that as you use it. New accounts
typically get a small free trial credit.

---

## 8. Cost model

Only Stages 2 and 4 cost money (Stage 3's tag-merging is local, no AI
call). Rough per-course estimate, using current approximate rates
(**verify at platform.claude.com/pricing — these change**):

| Stage | Model | Rough cost per course |
|---|---|---|
| Tagging | Haiku | $0.50–$2 |
| Reconstruction | Sonnet | $1–$4 |
| **Total** | | **~$2–$6, typically under $8** |

The GUI's "Estimate AI Cost" button (Courses tab) gives a rough number
*before* you commit, using `engine/processors/cost_estimator.py` — this
is a character-count-based approximation, not a guarantee. The AI
pipeline also reports its *actual* cost after each real run completes,
which is more trustworthy than the pre-run estimate.

---

## 9. What's genuinely tested vs. what still needs Mac-side verification

### Tested and confirmed working (in the build environment, with mocked AI calls where a real API wasn't available)
- Full extraction pipeline: scan → video → Excel → PDF → OCR → cleanup
- Evidence backfill, tagging batching, cross-linking clustering
- Full AI pipeline chain: tagging → linking → reconstruction → quality
  check, including course status transitioning correctly through
  `EXTRACTED_AWAITING_AI → NEEDS_REVIEW → COMPLETE`
- Video auto-delete safety gate (only deletes after full extraction
  success — verified it correctly withholds deletion on a partial state)
- Duplicate-file detection (byte-identical files correctly deduped)
- Frame-timestamp bug fix (previously hardcoded to 0.0, now captures real
  ffmpeg-reported timestamps)
- YouTube queue read/write with the new keep/delete/folder columns
- Settings save/load
- GUI logic (all methods) against a simulated tkinter environment

### NOT yet tested — needs verification on the actual Mac
- `mlx-whisper` itself (needs Apple Silicon hardware — this build
  environment is Linux x86)
- The real GUI window rendering and click-through (needs a real display)
- `yt-dlp` actually downloading a video (needs internet, untested here)
- Real Anthropic API calls (needs a live key; all AI-stage testing here
  used a mocked client that simulates the API's response shape)
- Diarization (`pyannote.audio`) — wired but off by default, never
  exercised end-to-end

---

## 10. Known limitations, stated honestly

1. **Cross-linking's tag clustering is approximate.** Plain string
   similarity is cheap but can conflate distinct-but-similarly-worded
   strategies. Watch for this on real course data; upgrading to embedding
   similarity is the natural next step if it proves inaccurate.
2. **`prune_uncited_frames()` exists but isn't wired into any automatic
   trigger yet.** It's built and testable, just not yet called
   automatically after Stage 6 approval.
3. **Diarization (trainer vs. participant speaker separation) is off by
   default and unbuilt end-to-end.** Every transcript segment currently
   has `speaker_role = 'unknown'`. Fine for now per the stated priority
   (what was said and when, before who said it), but a real gap if
   Q&A/participant speech is getting mixed into "trainer" evidence.
4. **Cost estimates before tagging runs are rough** — based on character
   counts, not actual token counts, and the strategy-count estimate is a
   heuristic. Treat the number as a ballpark for a go/no-go decision, not
   a quote.
5. **No conflict-resolution rule for contradictory evidence across
   sessions** (e.g. the trainer revises a rule in a later session). The
   reconstruction prompt doesn't currently have explicit instructions for
   this case — currently it would likely just cite both, or pick one
   inconsistently. Worth adding an explicit rule (e.g. "later session
   wins, but log the earlier version") if this comes up in practice.

---

## 11. Where to pick this up

If you're starting fresh with this document and no other context, the
right first steps are:

1. Run the Mac setup in section 7.
2. Put one real short session recording into `Course_01/Main_Sessions/`.
3. Run extraction only (`python3 process_all_courses.py Course_01`, or
   the GUI's "Extract Selected") — confirm `mlx-whisper` actually works
   on the real hardware and produces a sensible transcript.
4. Only after that works: add an API key in Settings, run "Estimate AI
   Cost" on that one course, then "Run AI — Selected" on just that one
   course, and read the resulting strategy in the Review tab critically
   against what the trainer actually said.
5. Only after that looks trustworthy: scale up to more courses.

This mirrors the project's own repeated principle throughout its design:
validate on one small real example before trusting the system on
everything.
