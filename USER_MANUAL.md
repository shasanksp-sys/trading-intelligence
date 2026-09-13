# TradingIntelligence — User Manual

A practical, step-by-step guide to actually using the app day to day.
(For technical architecture and how the code works internally, see
`PROJECT_SUMMARY.md` instead — this document is about clicking buttons,
not reading code.)

---

## 1. First-time setup (only ever done once)

1. `brew install ffmpeg tesseract`
2. `pip3 install -r requirements.txt`
3. Right-click `TradingIntelligence.app` → Make Alias → drag the alias to your Desktop.
4. Right-click the Desktop alias → Open (only this first time — macOS will
   warn about an unidentified developer, click Open anyway).
5. From now on: just double-click the Desktop icon.

The app icon ships pre-built inside the bundle, so there's no separate
icon-build step. If it ever shows as a generic/blank icon instead of the
candlestick-and-checkmark design (usually just a stale Finder icon cache
after copying the app around), double-click `Set_Icon_Direct.command`
once to re-apply it and clear the cache.

---

## 2. Assigning a local video course

1. Click **Reveal Raw Folder** (Courses tab) — this opens `00_RAW/Courses`
   directly in Finder, no path typing needed.
2. Drag your video files into `Course_XX/Main_Sessions/` (main teaching
   sessions) or `Course_XX/Supporting_Sessions/` (supplementary sessions).
   **The folder name must contain the word "main" or "supporting"** — this
   is how the system tells them apart. A video placed anywhere else won't
   be picked up.
3. Drag Excel/PDF/Docs/screenshots into their matching subfolders
   (`Excel/`, `PDFs/`, `Docs/`, `Screenshots/`).
4. Back in the app, click **Refresh** if the course doesn't show yet.
5. Click the course name in the list to select it (Cmd-click or
   Shift-click to select several at once).
6. Click **Extract Selected**. This is free — no API key needed, runs
   entirely on your Mac.
7. Switch to the **Live Progress** tab to watch it work.

### Don't have a course folder yet?

Click **+ New Course**, type a name (e.g. `Course_04`), and the app
creates the full folder skeleton for you automatically — both the raw
intake folders and the matching output folders.

---

## 3. Assigning a YouTube session

1. Go to the **YouTube Queue** tab.
2. Paste the URL into the box.
3. Decide: keep the video afterward, or let it auto-delete once
   extraction succeeds (the default — same policy as local videos)?
   - To keep it: check **"Keep the video after processing"**, then click
     **Choose Folder…** to pick where it should be saved.
   - To delete it (default): leave the checkbox unchecked.
4. Click **+ Add to Queue**.
5. Repeat for as many URLs as you want — they queue up, they don't run immediately.
6. Click **Process Queue** to actually run them, one at a time, in order.
   If one URL fails (private video, deleted, network issue), it's logged
   and the queue moves on to the next one — it doesn't stop everything.

**Only queue URLs you're actually authorized to download and use.**

---

## 4. Running the AI stages (after extraction)

Extraction (section 2/3) never needs an API key. The AI stages
(tagging → cross-linking → strategy writing → quality check) do.

1. Add your Anthropic API key once, in the **Settings** tab (get one at
   platform.claude.com — pay-as-you-use, not a subscription).
2. Back in **Courses**, select the course(s) that show status
   **"Extracted — Awaiting AI."**
3. Click **Estimate AI Cost** first if you want a rough number before
   committing — this is an approximation, not a guarantee.
4. Click **Run AI — Selected** to process just the courses you picked, or
   **Run AI — All Pending** to run every course currently sitting at
   "Extracted — Awaiting AI."

You choose exactly which courses spend API credit, and when — nothing
runs on its own without you clicking one of these two buttons.

---

## 5. Reviewing and approving strategies

1. Go to the **Review** tab, click **Refresh**.
2. Every strategy that got flagged (missing evidence, low-confidence
   transcript, open questions) shows up here — this is usually a small
   subset, not everything.
3. Click **View Details** to read the full structured spec before deciding.
4. Select the ones that look right, click **Approve Selected**. Approved
   strategies get copied into that course's `06_FINAL_STRATEGIES/` folder
   — that's your actual deliverable.

---

## 6. Button reference (Courses tab)

| Button | What it does |
|---|---|
| **+ New Course** | Creates a new course's full folder skeleton (raw + output) |
| **Reveal Raw Folder** | Opens `00_RAW/Courses` in Finder — for dragging files in |
| **Refresh** | Reloads the course list and each course's current status |
| **Extract Selected / Extract All** | Runs Stage 1 (free, no API key) on selected courses, or every course |
| **Estimate AI Cost** | Rough cost preview for the selected course(s), before you commit |
| **Run AI — Selected** | Runs the AI stages on only the courses you've selected |
| **Run AI — All Pending** | Runs the AI stages on every course sitting at "Extracted — Awaiting AI" |

---

## 7. Resetting test/demo data

If you've been testing with sample material and want a clean slate before
real work: right-click `Reset_Test_Data.command` → Open → type `YES` when
prompted. This wipes the database and every file inside the course/output
folders, but keeps the folder structure and all code fully intact — safe
to re-run any time you want to start fresh.

---

## 8. Troubleshooting

### "FileNotFoundError: No such file or directory: 'ffmpeg'" when running from the app

**Cause:** apps launched by double-click get a bare-minimum system PATH
that doesn't include Homebrew's install location
(`/opt/homebrew/bin`) — even though your Terminal's PATH does, so this
only shows up when launching via the app icon, not when typing commands
directly in Terminal.

**Fix:** already applied in the current version of
`TradingIntelligence.app`'s launcher and in `Launch_Dashboard.command` —
both now explicitly add `/opt/homebrew/bin` to PATH before starting.
If you're on an older copy of the app and hit this, replace
`TradingIntelligence.app` with the current version.

### The dashboard window opens completely blank

**Cause:** an old/system version of Tk (8.5) has a known macOS rendering
bug. Resizing the window or minimizing/restoring it usually forces it to
redraw.

**Permanent fix:** `brew install python-tk@3.11 python@3.11`, then make
sure the app launcher points at `/opt/homebrew/bin/python3.11` (it does,
by default, in the current version).

### Warnings you can safely ignore

- `UserWarning: torchaudio._backend... deprecated` — a library
  maintenance notice, not an error.
- `objc[...]: Class AVFFrameReceiver is implemented in both...` — two
  installed libraries both provide the same low-level component; macOS
  picks one automatically. Cosmetic only.
- `DeprecationWarning: The fitz API is deprecated` — already fixed in the
  current version (`import pymupdf as fitz` instead of `import fitz`).

### Something else fails when using the app icon (no visible Terminal)

Check `launcher.log` in the project folder — that's where startup errors
go when there's no visible Terminal window to show them in.

---

## 9. The golden rule, restated

Every rule in a final strategy must trace back to an exact timestamp,
Excel cell, or PDF page. If you ever see a strategy that looks
suspiciously complete with zero flagged items, that's worth a closer
manual read — not because the system is untrustworthy, but because real
course material is rarely perfectly unambiguous, and a suspiciously clean
result is itself worth double-checking against the source.
