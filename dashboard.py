"""
TradingIntelligence Dashboard -- one window, five tabs, no Terminal typing.

Uses tkinter, which ships with macOS's Python -- no extra install needed
for the GUI itself (the pipeline libraries in requirements.txt still are).

Tabs:
  Courses       -- add/create courses, run extraction, select which
                   course(s) to run AI on (per your "hold AI, pick later" request)
  YouTube Queue -- paste URLs, set keep/delete + folder per URL, process queue
  Live Progress -- streamed output from whatever's currently running
  Review        -- approve/reject flagged strategies (the one manual step)
  Settings      -- API key, model choice, storage policy toggles

Heavy operations (extraction, AI runs, YouTube queue) run as a subprocess
of the matching CLI script, streamed live into the Live Progress tab --
same mechanism whether you click a button here or type the command
yourself, so behavior is identical either way.
"""

import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import ttk, scrolledtext, simpledialog, filedialog, messagebox

PROJECT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_DIR / "engine"))

import config
import settings as settings_module
from db.schema import get_connection, init_db
from pipeline.course_manager import list_all_courses_with_status, create_course_skeleton, reset_course, delete_course_completely, output_root_for_course
from processors.cost_estimator import estimate_course_ai_cost
from processors.cleanup import prune_uncited_frames
from process_youtube_queue import read_queue, write_queue, add_url_to_queue, QUEUE_FIELDS


STATUS_LABELS = {
    "NOT_STARTED": "Not Started",
    "EXTRACTION_IN_PROGRESS": "\u23f3 Extraction In Progress",
    "EXTRACTED_AWAITING_AI": "\u2705 Extraction Complete \u2014 Awaiting AI",
    "AI_IN_PROGRESS": "\u23f3 AI Processing",
    "NEEDS_REVIEW": "Needs Review",
    "COMPLETE": "\u2705 Complete",
}


class _ReattachedProcess:
    """
    Stand-in for subprocess.Popen when reattaching to a job that a
    PREVIOUS dashboard window started (see App._try_reattach_running_job)
    -- this window never called Popen() itself, so it has no real Popen
    object, just the PID from logs/current_job.json. Implements the same
    .poll()/.terminate()/.wait()/.returncode surface Popen has, so
    stop_process() and _stream_and_wait() can treat a reattached job
    identically to one this window spawned itself, with no separate code
    path needed.
    """
    def __init__(self, pid):
        self.pid = pid
        self.returncode = None

    def poll(self):
        if self.returncode is not None:
            return self.returncode
        try:
            os.kill(self.pid, 0)
            return None  # still alive
        except ProcessLookupError:
            # Can't recover the real exit code from a process we never
            # spawned ourselves -- 0 is an assumption (finished, not
            # crashed), reasonable since the job's own log/DB writes are
            # the actual record of what happened either way.
            self.returncode = 0
            return self.returncode

    def terminate(self):
        try:
            os.kill(self.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    def wait(self):
        while self.poll() is None:
            time.sleep(0.3)
        return self.returncode


class App:
    def __init__(self, root):
        self.root = root
        self.root.title("TradingIntelligence Dashboard")
        self.root.geometry("980x640")

        self.process = None
        self.output_queue = queue.Queue()
        self.active_course_name = None   # parsed live from "##### CourseName #####" lines
        self.active_progress_pct = None  # parsed live from "[progress] ...N%" lines
        self.completed_items = None      # from "course=... (i/total items)" lines, for blended sub-progress
        self.total_items = None
        self._last_auto_refresh = 0.0
        self._current_log_path = None
        self._job_state_path = PROJECT_DIR / "logs" / "current_job.json"

        notebook = ttk.Notebook(root)
        notebook.pack(fill="both", expand=True)

        self.courses_tab = ttk.Frame(notebook)
        self.youtube_tab = ttk.Frame(notebook)
        self.progress_tab = ttk.Frame(notebook)
        self.review_tab = ttk.Frame(notebook)
        self.backtest_tab = ttk.Frame(notebook)
        self.settings_tab = ttk.Frame(notebook)

        notebook.add(self.courses_tab, text="Courses")
        notebook.add(self.youtube_tab, text="YouTube Queue")
        notebook.add(self.progress_tab, text="Live Progress")
        notebook.add(self.review_tab, text="Review")
        notebook.add(self.backtest_tab, text="Backtest")
        notebook.add(self.settings_tab, text="Settings")

        self.notebook = notebook

        self._build_courses_tab()
        self._build_youtube_tab()
        self._build_progress_tab()
        self._build_review_tab()
        self._build_backtest_tab()
        self._build_settings_tab()

        self._try_reattach_running_job()

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(200, self._poll_output_queue)

    def _enable_text_context_menu(self, widget):
        """
        Adds a Cut/Copy/Paste/Select All right-click menu and explicit
        Cmd+V/C/X/A bindings to a text entry field.

        Found missing during a real run: ttk.Entry has no right-click
        context menu on macOS by default (Tk doesn't add one the way
        native Cocoa text fields do), and Cmd+V is not guaranteed to
        reach a ttk widget's internal paste handler on every Tk build --
        so a field could accept typed text but silently have no way to
        paste a copied URL into it via mouse OR keyboard. Right-click
        needs both <Button-2> and <Button-3> bound because which one
        macOS Tk maps "secondary click" to depends on the trackpad/mouse
        settings and Tk version -- binding only one leaves it broken for
        a subset of users, which is exactly the kind of gap that's easy
        to miss testing on a single machine.
        """
        menu = tk.Menu(widget, tearoff=0)
        menu.add_command(label="Cut", command=lambda: widget.event_generate("<<Cut>>"))
        menu.add_command(label="Copy", command=lambda: widget.event_generate("<<Copy>>"))
        menu.add_command(label="Paste", command=lambda: widget.event_generate("<<Paste>>"))
        menu.add_separator()
        menu.add_command(label="Select All", command=lambda: widget.select_range(0, "end"))

        def show_menu(event):
            widget.focus_set()
            menu.tk_popup(event.x_root, event.y_root)
            return "break"

        widget.bind("<Button-2>", show_menu)
        widget.bind("<Button-3>", show_menu)

        # Explicit shortcut bindings -- belt-and-suspenders alongside the
        # menu above, since Cmd+V should work purely from the keyboard too.
        widget.bind("<Command-v>", lambda e: (widget.event_generate("<<Paste>>"), "break")[1])
        widget.bind("<Command-c>", lambda e: (widget.event_generate("<<Copy>>"), "break")[1])
        widget.bind("<Command-x>", lambda e: (widget.event_generate("<<Cut>>"), "break")[1])
        widget.bind("<Command-a>", lambda e: (widget.select_range(0, "end"), "break")[1])

    # ================= Courses tab =================

    def _build_courses_tab(self):
        frame = self.courses_tab

        # Three rows, not one -- eleven buttons plus separators in a single
        # non-wrapping pack() row overflowed the window at its default
        # size, pushing "Delete Selected Completely" (and part of "Reset
        # Selected Course") off the right edge with no scrollbar to reach
        # them. Splitting by purpose (course management / extraction+AI /
        # destructive actions) keeps every button reachable regardless of
        # window width, with real margin to spare -- a two-row split
        # measured at 975px against a 980px-wide window, too tight to
        # trust across different displays/DPI. This also isolates the two
        # destructive actions onto their own row, which is better UX
        # regardless of the width issue.
        row1 = ttk.Frame(frame, padding=(8, 8, 8, 4))
        row1.pack(fill="x")
        row2 = ttk.Frame(frame, padding=(8, 0, 8, 4))
        row2.pack(fill="x")
        row3 = ttk.Frame(frame, padding=(8, 0, 8, 8))
        row3.pack(fill="x")

        ttk.Button(row1, text="+ New Course", command=self.new_course).pack(side="left", padx=3)
        ttk.Button(row1, text="Reveal Raw Folder", command=self.reveal_raw_folder).pack(side="left", padx=3)
        ttk.Button(row1, text="\U0001F504 Refresh", command=self.refresh_courses).pack(side="left", padx=3)

        ttk.Button(row2, text="\u25B6 Extract Selected", command=self.extract_selected).pack(side="left", padx=3)
        ttk.Button(row2, text="\u25B6 Extract All", command=self.extract_all).pack(side="left", padx=3)
        ttk.Separator(row2, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Button(row2, text="\U0001F4B0 Estimate AI Cost", command=self.estimate_cost_selected).pack(side="left", padx=3)
        ttk.Button(row2, text="\U0001F916 Run AI \u2014 Selected", command=self.run_ai_selected).pack(side="left", padx=3)
        ttk.Button(row2, text="\U0001F916 Run AI \u2014 All Pending", command=self.run_ai_all_pending).pack(side="left", padx=3)

        ttk.Button(row3, text="\u21BA Reset Selected Course", command=self.reset_selected_course).pack(side="left", padx=3)
        ttk.Button(row3, text="\U0001F5D1 Delete Selected Completely", command=self.delete_selected_course).pack(side="left", padx=3)

        note = ttk.Label(
            frame,
            text="Select one or more courses below (click, or Cmd-click / Shift-click for several) "
                 "before using the AI buttons \u2014 you choose exactly which course(s) spend API credit, and when.",
            padding=(8, 0), wraplength=940, foreground="#555",
        )
        note.pack(fill="x")

        columns = ("course", "status")
        self.courses_tree = ttk.Treeview(frame, columns=columns, show="headings", selectmode="extended", height=18)
        self.courses_tree.heading("course", text="Course")
        self.courses_tree.heading("status", text="Status")
        self.courses_tree.column("course", width=280)
        self.courses_tree.column("status", width=220)
        self.courses_tree.pack(fill="both", expand=True, padx=8, pady=8)

    def _selected_courses(self):
        return [self.courses_tree.item(i, "values")[0] for i in self.courses_tree.selection()]

    def new_course(self):
        name = simpledialog.askstring("New Course", "Course folder name (e.g. Course_04):", parent=self.root)
        if not name:
            return
        result = create_course_skeleton(name, str(config.RAW_COURSES_DIR), str(config.OUTPUT_COURSES_DIR))
        messagebox.showinfo("Course Created",
                             f"Created:\n{result['raw_path']}\n\nDrag your videos/PDFs/Excel files into "
                             f"the matching subfolders inside it, then click Extract.")

    def reveal_raw_folder(self):
        subprocess.run(["open", str(config.RAW_COURSES_DIR)])

    def refresh_courses(self):
        init_db(config.DB_PATH)
        conn = get_connection(config.DB_PATH)
        courses = list_all_courses_with_status(conn, str(config.RAW_COURSES_DIR))
        conn.close()

        self.courses_tree.delete(*self.courses_tree.get_children())
        for c in courses:
            status_label = STATUS_LABELS.get(c["status"], c["status"])
            # live override: the course currently being processed shows a
            # real "Processing..." state instead of its last-known DB
            # status, which otherwise wouldn't change until the whole
            # course finishes and you manually click Refresh.
            if self.active_course_name and c["course_id"] == self.active_course_name:
                pct_suffix = f" ({self.active_progress_pct}%)" if self.active_progress_pct is not None else ""
                status_label = f"⏳ Processing{pct_suffix}"
            self.courses_tree.insert("", "end", values=(c["course_id"], status_label))

    def extract_selected(self):
        selected = self._selected_courses()
        if not selected:
            messagebox.showwarning("Nothing selected", "Select at least one course first.")
            return
        for course in selected:
            # A youtube_-prefixed course has no 00_RAW/Courses/ folder for
            # process_all_courses.py to find -- previously this button did
            # nothing at all for such a row, silently. Route it to the
            # YouTube-specific rescan instead, which picks up anything the
            # user has manually saved into that video's Description_Materials
            # folder (e.g. after following Needs_Manual_Download.txt).
            if course.startswith("youtube_"):
                self._run_script(["rescan_youtube_materials.py", course], f"Rescan materials for {course}", queue_multiple=True)
            else:
                self._run_script(["process_all_courses.py", course], f"Extract {course}", queue_multiple=True)

    def extract_all(self):
        self._run_script(["process_all_courses.py"], "Extract All Courses")
        self._run_script(["rescan_youtube_materials.py"], "Rescan All YouTube Course Materials", queue_multiple=True)

    def reset_selected_course(self):
        selected = self._selected_courses()
        if not selected:
            messagebox.showwarning("Nothing selected", "Select the course(s) you want to reset first.")
            return
        confirm = messagebox.askyesno(
            "Reset course(s)?",
            f"This clears all processing history for: {', '.join(selected)}\n\n"
            "Status goes back to 'Not Started' so you can reassign/reprocess it. "
            "Your raw source files are NOT touched -- only the database records "
            "and extracted output files for these course(s) are cleared.\n\nProceed?",
        )
        if not confirm:
            return
        init_db(config.DB_PATH)
        conn = get_connection(config.DB_PATH)
        results = []
        for course in selected:
            result = reset_course(conn, course, output_course_dir=config.output_course_dir(course))
            results.append(f"{course}: {result['database_file_records_removed']} DB record(s), "
                            f"{result['output_files_removed']} output file(s) cleared \u2192 {STATUS_LABELS.get(result['new_status'])}")
        conn.close()
        messagebox.showinfo("Reset complete", "\n".join(results))

    def delete_selected_course(self):
        """
        The destructive counterpart to Reset: removes a course entirely
        so it disappears from this list (rather than reappearing as "Not
        Started"), so a fresh start is always available if a course or
        YouTube video needs to be torn down and redone from scratch.

        Deliberately a separate button and a separate, stronger
        confirmation from Reset -- for a local course this permanently
        deletes the original uploaded files (PDFs/Excel/videos/etc under
        00_RAW), not just the processed output, and that needs its own
        explicit warning rather than being folded into Reset's gentler
        one.
        """
        selected = self._selected_courses()
        if not selected:
            messagebox.showwarning("Nothing selected", "Select the course(s) you want to delete first.")
            return

        local_selected = [c for c in selected if not c.startswith("youtube_")]
        warning_lines = [f"This PERMANENTLY deletes everything for: {', '.join(selected)}", ""]
        if local_selected:
            warning_lines.append(
                f"For {', '.join(local_selected)}, this also deletes the ORIGINAL uploaded "
                "files (PDFs/Excel/videos/etc) from 00_RAW -- not just the extracted output."
            )
        warning_lines.append("")
        warning_lines.append("This cannot be undone. The course(s) will disappear from this list entirely.")
        warning_lines.append("")
        warning_lines.append("Proceed?")
        confirm = messagebox.askyesno("Delete course(s) completely?", "\n".join(warning_lines))
        if not confirm:
            return

        init_db(config.DB_PATH)
        conn = get_connection(config.DB_PATH)
        results = []
        for course in selected:
            output_dir = output_root_for_course(course, str(config.OUTPUT_COURSES_DIR), str(config.OUTPUT_YOUTUBE_DIR), conn=conn)
            raw_dir = None if course.startswith("youtube_") else config.raw_course_dir(course)
            result = delete_course_completely(conn, course, output_course_dir=output_dir, raw_course_dir=raw_dir)
            summary = f"{course}: {result['database_file_records_removed']} DB record(s) removed"
            if result["output_folder_removed"]:
                summary += ", output folder removed"
            if result["raw_folder_removed"]:
                summary += ", original uploaded files removed"
            results.append(summary)
        conn.close()
        messagebox.showinfo("Delete complete", "\n".join(results))

    def estimate_cost_selected(self):
        selected = self._selected_courses()
        if not selected:
            messagebox.showwarning("Nothing selected", "Select at least one course first.")
            return
        init_db(config.DB_PATH)
        conn = get_connection(config.DB_PATH)
        lines = []
        total = 0.0
        for course in selected:
            est = estimate_course_ai_cost(conn, course)
            lines.append(f"{course}: ~{est['estimated_strategy_count']} strategies, "
                         f"~${est['total_estimated_usd']}")
            total += est["total_estimated_usd"]
        conn.close()
        messagebox.showinfo("Estimated AI Cost (rough)",
                             "\n".join(lines) + f"\n\nTotal estimate: ~${total:.2f}\n\n"
                             "This is a rough estimate before tagging runs -- actual cost is reported "
                             "after each run completes. Verify current model pricing at platform.claude.com/pricing.")

    def run_ai_selected(self):
        selected = self._selected_courses()
        if not selected:
            messagebox.showwarning("Nothing selected", "Select at least one course first.")
            return
        if not settings_module.get_api_key():
            messagebox.showwarning("No API key", "Add your Anthropic API key in the Settings tab first.")
            return
        confirm = messagebox.askyesno("Confirm", f"Run AI processing for: {', '.join(selected)}?\n\n"
                                       "Use 'Estimate AI Cost' first if you want a rough number before committing.")
        if not confirm:
            return
        self._run_script(["run_ai_for_courses.py"] + selected, f"Run AI: {', '.join(selected)}")

    def run_ai_all_pending(self):
        if not settings_module.get_api_key():
            messagebox.showwarning("No API key", "Add your Anthropic API key in the Settings tab first.")
            return
        confirm = messagebox.askyesno("Confirm", "Run AI processing for EVERY course currently "
                                       "'Extracted \u2014 Awaiting AI'?")
        if not confirm:
            return
        self._run_script(["run_ai_for_courses.py", "--all-pending"], "Run AI: All Pending")

    # ================= YouTube tab =================

    def _build_youtube_tab(self):
        frame = self.youtube_tab
        add_frame = ttk.LabelFrame(frame, text="Add URL to Queue", padding=8)
        add_frame.pack(fill="x", padx=8, pady=8)

        ttk.Label(add_frame, text="YouTube URL:").grid(row=0, column=0, sticky="w")
        self.yt_url_var = tk.StringVar()
        yt_url_entry = ttk.Entry(add_frame, textvariable=self.yt_url_var, width=60)
        yt_url_entry.grid(row=0, column=1, columnspan=3, sticky="we", padx=4)
        self._enable_text_context_menu(yt_url_entry)

        self.yt_keep_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(add_frame, text="Keep the video after processing", variable=self.yt_keep_var,
                        command=self._toggle_keep_folder).grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))

        self.yt_folder_var = tk.StringVar(value="(default: delete after processing)")
        self.yt_folder_btn = ttk.Button(add_frame, text="Choose Folder\u2026", command=self.choose_keep_folder, state="disabled")
        self.yt_folder_btn.grid(row=1, column=2, sticky="w", padx=6, pady=(6, 0))
        ttk.Label(add_frame, textvariable=self.yt_folder_var, foreground="#555").grid(row=1, column=3, sticky="w", pady=(6, 0))

        ttk.Button(add_frame, text="+ Add to Queue", command=self.add_youtube_url).grid(row=2, column=0, pady=8, sticky="w")
        add_frame.columnconfigure(1, weight=1)

        btns = ttk.Frame(frame, padding=(8, 0))
        btns.pack(fill="x")
        ttk.Button(btns, text="\u25B6 Process Queue", command=self.process_youtube_queue_click).pack(side="left", padx=3)
        ttk.Button(btns, text="\U0001F504 Refresh", command=self.refresh_youtube_queue).pack(side="left", padx=3)

        columns = ("index", "url", "status", "keep", "notes")
        self.yt_tree = ttk.Treeview(frame, columns=columns, show="headings", height=14)
        for col, w in zip(columns, (40, 380, 100, 140, 200)):
            self.yt_tree.heading(col, text=col.capitalize())
            self.yt_tree.column(col, width=w)
        self.yt_tree.pack(fill="both", expand=True, padx=8, pady=8)

        self._keep_folder_choice = ""

    def _toggle_keep_folder(self):
        self.yt_folder_btn.config(state="normal" if self.yt_keep_var.get() else "disabled")
        if not self.yt_keep_var.get():
            self.yt_folder_var.set("(default: delete after processing)")

    def choose_keep_folder(self):
        folder = filedialog.askdirectory(title="Where should the kept video be saved?")
        if folder:
            self._keep_folder_choice = folder
            self.yt_folder_var.set(folder)

    def add_youtube_url(self):
        url = self.yt_url_var.get().strip()
        if not url:
            messagebox.showwarning("No URL", "Paste a YouTube URL first.")
            return
        keep = self.yt_keep_var.get()
        folder = self._keep_folder_choice if keep else ""
        if keep and not folder:
            messagebox.showwarning("Choose a folder", "You checked 'Keep the video' \u2014 choose a folder for it first.")
            return
        add_url_to_queue(str(config.YOUTUBE_QUEUE_FILE), url, keep_video=keep, keep_folder=folder)
        self.yt_url_var.set("")
        self.yt_keep_var.set(False)
        self._toggle_keep_folder()

    def process_youtube_queue_click(self):
        self._run_script(["process_youtube_queue.py"], "Process YouTube Queue")

    def refresh_youtube_queue(self):
        self.yt_tree.delete(*self.yt_tree.get_children())
        if not config.YOUTUBE_QUEUE_FILE.exists():
            return
        for row in read_queue(str(config.YOUTUBE_QUEUE_FILE)):
            keep_display = f"Yes \u2192 {row.get('keep_folder','')}" if row.get("keep_video") == "yes" else "No (delete)"
            status_display = row["status"]
            # live percentage while this specific row is the one currently
            # downloading -- reuses the same number already being tracked
            # for the Live Progress tab, so both stay consistent.
            if status_display == "PROCESSING" and self.active_progress_pct is not None:
                status_display = f"PROCESSING ({self.active_progress_pct}%)"
            self.yt_tree.insert("", "end", values=(row["index"], row["url"], status_display,
                                                     keep_display, row.get("notes", "")))

    # ================= Live Progress tab =================

    def _build_progress_tab(self):
        frame = self.progress_tab
        top = ttk.Frame(frame, padding=8)
        top.pack(fill="x")
        self.status_var = tk.StringVar(value="Idle")
        ttk.Label(top, textvariable=self.status_var, font=("", 11, "bold")).pack(side="left")
        self.progress_pct_var = tk.StringVar(value="")
        ttk.Label(top, textvariable=self.progress_pct_var, font=("", 11, "bold"), foreground="#0a6").pack(side="left", padx=12)
        ttk.Button(top, text="\u23F9 Stop", command=self.stop_process).pack(side="right")

        # Overall course progress -- the ONE authoritative number, only ever
        # moves forward (never resets mid-course). This is what "how far
        # along is the whole course" should mean.
        self.progress_bar = ttk.Progressbar(frame, orient="horizontal", mode="determinate", maximum=100)
        self.progress_bar.pack(fill="x", padx=8)

        # Current-step detail -- a SEPARATE, secondary line showing what's
        # happening right now (e.g. "transcribing: 67%"). This one DOES
        # reset for every new video/step -- that's expected and fine, since
        # it's clearly labeled as "current step", not "overall".
        substep_frame = ttk.Frame(frame, padding=(8, 4))
        substep_frame.pack(fill="x")
        self.substep_var = tk.StringVar(value="")
        ttk.Label(substep_frame, textvariable=self.substep_var, foreground="#666").pack(side="left")

        self.output_box = scrolledtext.ScrolledText(frame, wrap="word", font=("Menlo", 11))
        self.output_box.pack(fill="both", expand=True, padx=8, pady=8)
        self.output_box.configure(state="disabled")

    def _log(self, text):
        self.output_box.configure(state="normal")
        self.output_box.insert("end", text)
        self.output_box.see("end")
        self.output_box.configure(state="disabled")

    def _run_script(self, args, label, queue_multiple=False):
        if self.process is not None:
            if queue_multiple:
                messagebox.showinfo("Busy", f"Something is already running. '{label}' will need to be started again once it's done.")
            else:
                messagebox.showinfo("Busy", "Something is already running \u2014 check the Live Progress tab, or click Stop first.")
            return

        self.notebook.select(self.progress_tab)
        self.status_var.set(f"Running: {label} \u2026")
        self.progress_pct_var.set("")
        self.progress_bar["value"] = 0
        self.substep_var.set("")
        self.active_course_name = None
        self.active_progress_pct = None
        self.completed_items = None
        self.total_items = None
        self.output_box.configure(state="normal")
        self.output_box.delete("1.0", "end")
        self.output_box.configure(state="disabled")

        # Log to a FILE, not a pipe -- this is what lets a job survive the
        # dashboard window closing. A subprocess.PIPE only stays readable
        # as long as SOMETHING in this process keeps its read end open;
        # if the window closes and this whole Python process exits, that
        # pipe's read end is gone, and the very next print() in the child
        # raises BrokenPipeError -- silently killing a background job far
        # worse than just leaving it alone would. Writing to a file has no
        # such dependency: the child keeps writing happily whether or not
        # anyone is tailing it, so closing the window can never crash it.
        # It also means a NEW dashboard window can reattach to and replay
        # the same job later (see _try_reattach_running_job).
        logs_dir = PROJECT_DIR / "logs"
        logs_dir.mkdir(exist_ok=True)
        safe_label = "".join(c if c.isalnum() or c in " _-" else "_" for c in label)[:60]
        log_path = logs_dir / f"{safe_label}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
        self._current_log_path = log_path

        env = dict(os.environ, PYTHONUNBUFFERED="1")
        logfile = open(log_path, "w")
        proc = subprocess.Popen(
            [sys.executable, "-u"] + args, cwd=str(PROJECT_DIR), env=env,
            stdout=logfile, stderr=subprocess.STDOUT,
            start_new_session=True,  # detaches from this GUI's process group/session --
                                      # survives this app quitting or being force-quit, not
                                      # just a plain window close.
        )
        logfile.close()  # child has its own inherited copy of the fd; ours isn't needed
        self.process = proc

        # Locking the screen doesn't pause anything on macOS -- only full
        # SYSTEM SLEEP does, which would otherwise silently stall a job
        # that can run for many hours. `caffeinate -w <pid>` keeps the Mac
        # awake for exactly as long as this specific job is alive, then
        # exits on its own -- no System Settings changes needed, and it
        # has zero effect on sleep behavior at any other time.
        subprocess.Popen(["caffeinate", "-i", "-w", str(proc.pid)], start_new_session=True)

        self._job_state_path.write_text(json.dumps({
            "pid": proc.pid, "log_path": str(log_path), "label": label,
            "started_at": datetime.now().isoformat(),
        }))

        threading.Thread(target=self._stream_and_wait, args=(proc, log_path), daemon=True).start()

    def _stream_and_wait(self, proc, log_path, from_start=True):
        """
        Shared by a freshly-launched job and a reattached one (see
        _try_reattach_running_job) -- tails the log file like `tail -f`
        (from byte 0 for a reattach, so Live Progress backfills the full
        history instead of starting blank) and pushes lines into the same
        queue _poll_output_queue already reads, so both cases render
        identically in the UI.
        """
        with open(log_path, "r") as f:
            if not from_start:
                f.seek(0, 2)
            while True:
                line = f.readline()
                if line:
                    self.output_queue.put(line)
                    continue
                if proc.poll() is not None:
                    break  # process exited AND we've drained everything written so far
                time.sleep(0.2)

        proc.wait()
        self.output_queue.put(f"\n--- Finished (exit code {proc.returncode}) ---\n")
        self.process = None
        if self._job_state_path.exists():
            self._job_state_path.unlink()
        self.output_queue.put("__DONE__")

    def _try_reattach_running_job(self):
        """
        Runs once at startup. If a PREVIOUS dashboard window started a job
        and was closed (or the app was quit/crashed) while it was still
        running, that job kept going in the background (see _run_script's
        start_new_session=True) -- this finds it via logs/current_job.json
        and reattaches Live Progress to it, so reopening the app shows you
        exactly what's still happening instead of leaving you guessing
        whether anything is running at all.
        """
        if not self._job_state_path.exists():
            return
        try:
            state = json.loads(self._job_state_path.read_text())
            pid, log_path, label = state["pid"], state["log_path"], state["label"]
        except (json.JSONDecodeError, OSError, KeyError):
            self._job_state_path.unlink(missing_ok=True)
            return

        try:
            os.kill(pid, 0)  # signal 0 = "is this pid alive", doesn't actually signal it
        except ProcessLookupError:
            self._job_state_path.unlink(missing_ok=True)  # stale -- that job already finished/died
            return
        except PermissionError:
            pass  # alive, just owned differently than expected -- still treat as running

        self.process = _ReattachedProcess(pid)
        self._current_log_path = Path(log_path)
        # Belt-and-suspenders: re-assert the sleep-prevention hold in case
        # the original caffeinate (from whichever session first launched
        # this job) already exited for any reason.
        subprocess.Popen(["caffeinate", "-i", "-w", str(pid)], start_new_session=True)
        self.status_var.set(f"Reattached: {label} … (still running from a previous session)")
        self._log(f"--- Reattached to a job still running in the background: {label} ---\n\n")
        threading.Thread(target=self._stream_and_wait, args=(self.process, Path(log_path), True), daemon=True).start()

    def stop_process(self):
        if self.process is not None:
            self.process.terminate()
            self._log("\n--- Stopped by user ---\n")
            self.status_var.set("Stopped")
            self.process = None
            if self._job_state_path.exists():
                self._job_state_path.unlink()
            self.active_course_name = None
            self.active_progress_pct = None
            self.completed_items = None
            self.total_items = None
        else:
            self._log("Nothing is currently running.\n")

    # matches the course-header line printed by process_all_courses.py:
    #   "##########  Course_03 copy  ##########"
    _COURSE_HEADER_RE = re.compile(r"^#{5,}\s+(.+?)\s+#{5,}\s*$")
    # matches any of our machine-readable progress lines:
    #   "[progress] course=Course_03 copy 42% (5/12 items)"
    #   "[progress] transcription 67%"
    #   "[progress] download 88%"
    _PROGRESS_LINE_RE = re.compile(r"^\[progress\]\s+(.*)$")
    _PROGRESS_COURSE_RE = re.compile(r"course=(.+?)\s+\d+%\s+\((\d+)/(\d+)\s+items\)")
    _PROGRESS_PCT_RE = re.compile(r"(\d+)%")

    def _poll_output_queue(self):
        try:
            while True:
                line = self.output_queue.get_nowait()
                if line == "__DONE__":
                    self.status_var.set("Idle")
                    self.progress_pct_var.set("")
                    self.progress_bar["value"] = 0
                    self.substep_var.set("")
                    self.active_course_name = None
                    self.active_progress_pct = None
                    self.completed_items = None
                    self.total_items = None
                else:
                    self._log(line)

                    header_match = self._COURSE_HEADER_RE.match(line.strip())
                    if header_match:
                        self.active_course_name = header_match.group(1)
                        # NOTE: do not reset active_progress_pct here -- the
                        # overall course % should only ever move forward,
                        # never snap back to 0 just because a new stage
                        # header printed.

                    progress_match = self._PROGRESS_LINE_RE.match(line.strip())
                    if progress_match:
                        rest = progress_match.group(1)
                        pct_match = self._PROGRESS_PCT_RE.search(rest)
                        if pct_match:
                            pct = int(pct_match.group(1))
                            if rest.startswith("course="):
                                # the authoritative course-level figure --
                                # also capture (i/total items) so the NEXT
                                # sub-step line can blend smoothly into
                                # this item's slot instead of the display
                                # sitting frozen until the item fully finishes.
                                self.active_progress_pct = pct
                                self.progress_pct_var.set(f"{pct}%")
                                self.progress_bar["value"] = pct
                                items_match = self._PROGRESS_COURSE_RE.search(rest)
                                if items_match:
                                    self.completed_items = int(items_match.group(2))
                                    self.total_items = int(items_match.group(3))
                            else:
                                # a sub-step (e.g. "transcription 67%",
                                # "download 88%").
                                step_label = rest.split()[0] if rest.split() else "step"
                                self.substep_var.set(f"Current step ({step_label}): {pct}%")

                                if self.total_items:
                                    # blend: this item counts as (sub-pct/100)
                                    # of one unit, added to items already
                                    # fully done -- keeps the MAIN number
                                    # moving continuously and correctly
                                    # monotonic, instead of sitting frozen
                                    # until the whole item finishes.
                                    blended = (self.completed_items + pct / 100) / self.total_items * 100
                                    blended = min(99, round(blended))  # never show 100% until the real course= line says so
                                    if self.active_progress_pct is None or blended > self.active_progress_pct:
                                        self.active_progress_pct = blended
                                        self.progress_pct_var.set(f"{blended}%")
                                        self.progress_bar["value"] = blended
                                else:
                                    # no item-count context yet (e.g. a
                                    # YouTube download, which has no
                                    # multi-item course structure at all)
                                    # -- show the sub-step % as the main
                                    # figure directly rather than leaving
                                    # it blank the whole time.
                                    self.active_progress_pct = pct
                                    self.progress_pct_var.set(f"{pct}%")
                                    self.progress_bar["value"] = pct
        except queue.Empty:
            pass

        # while something is running, auto-refresh the Courses/YouTube tabs
        # periodically (not on every poll tick -- every ~3s is enough to
        # feel live without hammering the database) so status is visible
        # without needing to click Refresh or interrupt anything running.
        if self.process is not None:
            now = time.time()
            if now - self._last_auto_refresh > 3:
                self._last_auto_refresh = now

        self.root.after(200, self._poll_output_queue)

    # ================= Review tab =================

    def _build_review_tab(self):
        frame = self.review_tab
        btns = ttk.Frame(frame, padding=8)
        btns.pack(fill="x")
        ttk.Button(btns, text="\U0001F504 Refresh", command=self.refresh_review).pack(side="left", padx=3)
        ttk.Button(btns, text="\u2713 Approve Selected", command=self.approve_selected_strategy).pack(side="left", padx=3)
        ttk.Button(btns, text="View Details", command=self.view_strategy_details).pack(side="left", padx=3)
        ttk.Separator(btns, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Button(btns, text="\U0001F4CA Export All Strategies (Excel)", command=self.export_all_strategies).pack(side="left", padx=3)

        columns = ("course", "strategy", "open_questions", "maturity")
        self.review_tree = ttk.Treeview(frame, columns=columns, show="headings", height=18)
        for col, w in zip(columns, (200, 260, 320, 140)):
            self.review_tree.heading(col, text=col.replace("_", " ").title())
            self.review_tree.column(col, width=w)
        self.review_tree.pack(fill="both", expand=True, padx=8, pady=8)

        self._review_specs = {}  # strategy_id -> spec dict, for View Details

    def refresh_review(self):
        self.review_tree.delete(*self.review_tree.get_children())
        self._review_specs = {}
        if not Path(config.DB_PATH).exists():
            return
        conn = get_connection(config.DB_PATH)
        rows = conn.execute(
            "SELECT strategy_id, course_id, name, maturity, spec_json FROM strategies "
            "WHERE maturity IN ('S3_NEEDS_REVIEW', 'S3_CLEAN') AND approved = 0"
        ).fetchall()
        conn.close()

        for strategy_id, course_id, name, maturity, spec_json in rows:
            spec = json.loads(spec_json) if spec_json else {}
            open_qs = spec.get("open_questions", [])
            self._review_specs[strategy_id] = spec
            self.review_tree.insert("", "end", iid=strategy_id,
                                     values=(course_id, name, "; ".join(open_qs)[:80] or "\u2014",
                                             "Needs Review" if maturity == "S3_NEEDS_REVIEW" else "Clean"))

    def view_strategy_details(self):
        sel = self.review_tree.selection()
        if not sel:
            messagebox.showwarning("Nothing selected", "Select a strategy first.")
            return
        spec = self._review_specs.get(sel[0], {})
        win = tk.Toplevel(self.root)
        win.title("Strategy Details")
        win.geometry("640x520")
        text = scrolledtext.ScrolledText(win, wrap="word", font=("Menlo", 11))
        text.pack(fill="both", expand=True, padx=8, pady=8)
        text.insert("1.0", json.dumps(spec, indent=2))
        text.configure(state="disabled")

    def export_all_strategies(self):
        """
        Writes ONE spreadsheet with every strategy across every course and
        YouTube video -- the consolidated place to look for backtesting
        reference, instead of piecing strategies together from chat or
        the per-course 06_FINAL_STRATEGIES/ JSON files individually.
        Safe to click any time -- always overwrites with the current full
        picture, so it stays a live view rather than something to hand-edit.
        """
        from processors.strategy_export import export_all_strategies
        conn = get_connection(config.DB_PATH)
        output_path = str(config.MAIN_FOLDER / "01_OUTPUT" / "All_Strategies.xlsx")
        try:
            result = export_all_strategies(conn, output_path)
        finally:
            conn.close()
        messagebox.showinfo(
            "Export Complete",
            f"{result['total_strategies']} strategies ({result['approved_count']} approved) "
            f"exported to:\n\n{output_path}",
        )
        subprocess.run(["open", "-R", output_path])

    def approve_selected_strategy(self):
        sel = self.review_tree.selection()
        if not sel:
            messagebox.showwarning("Nothing selected", "Select a strategy first.")
            return
        conn = get_connection(config.DB_PATH)
        affected_course_ids = set()
        for strategy_id in sel:
            row = conn.execute("SELECT course_id, name, spec_json FROM strategies WHERE strategy_id = ?",
                                (strategy_id,)).fetchone()
            if not row:
                continue
            course_id, name, spec_json = row
            conn.execute("UPDATE strategies SET approved = 1 WHERE strategy_id = ?", (strategy_id,))
            affected_course_ids.add(course_id)

            from pipeline.course_manager import output_root_for_course
            output_root = output_root_for_course(course_id, str(config.OUTPUT_COURSES_DIR), str(config.OUTPUT_YOUTUBE_DIR), conn=conn)
            final_dir = str(Path(output_root) / "06_FINAL_STRATEGIES")
            Path(final_dir).mkdir(parents=True, exist_ok=True)
            safe_name = "".join(c if c.isalnum() or c in " _-" else "_" for c in name)[:60]
            Path(final_dir, f"{safe_name}.json").write_text(spec_json or "{}")

        conn.commit()

        # Automatic storage cleanup -- runs right here, on approval, so it
        # never needs a separate button. prune_uncited_frames only ever
        # removes screenshot files for a session that contributed to NO
        # approved strategy in this course; it never touches transcript
        # text or document extractions (those stay, on purpose, so a
        # citation can always be traced back and re-runs stay possible).
        # Wrapped defensively -- a cleanup hiccup should never block the
        # approval that already succeeded and was already committed above.
        frames_pruned_total = 0
        for course_id in affected_course_ids:
            try:
                result = prune_uncited_frames(conn, course_id)
                frames_pruned_total += result.get("pruned", 0)
            except Exception as e:
                print(f"  [cleanup] frame pruning skipped for {course_id}: {e}")

        conn.close()
        msg = f"{len(sel)} strategy(ies) approved and saved to 06_FINAL_STRATEGIES."
        if frames_pruned_total:
            msg += f"\n\n{frames_pruned_total} now-uncited screenshot file(s) were automatically cleaned up."
        messagebox.showinfo("Approved", msg)

    # ================= Backtest tab =================

    def _discover_strategy_classes(self):
        """
        Scans engine/backtest/strategies/ for every Strategy subclass --
        this is the actual "assign a strategy to backtest" mechanism: drop
        a new strategy .py file in that folder and it shows up in the
        dropdown on next refresh, no dashboard code change needed.
        """
        import importlib
        import inspect
        import pkgutil

        from backtest.strategy import Strategy
        import backtest.strategies as strategies_pkg

        found = {}
        for _, module_name, _ in pkgutil.iter_modules(strategies_pkg.__path__):
            module = importlib.import_module(f"backtest.strategies.{module_name}")
            for name, obj in inspect.getmembers(module, inspect.isclass):
                if issubclass(obj, Strategy) and obj is not Strategy and obj.__module__ == module.__name__:
                    found[name] = obj
        return found

    def _discover_local_symbols(self):
        market_data_dir = config.MAIN_FOLDER / "market_data"
        if not market_data_dir.exists():
            return []
        return sorted(p.stem for p in market_data_dir.glob("*.csv"))

    def _build_backtest_tab(self):
        frame = self.backtest_tab

        form = ttk.Frame(frame, padding=8)
        form.pack(fill="x")

        self._strategy_classes = self._discover_strategy_classes()
        local_symbols = self._discover_local_symbols()

        ttk.Label(form, text="Strategy:").grid(row=0, column=0, sticky="w", padx=3, pady=3)
        self.bt_strategy_var = tk.StringVar(value=next(iter(self._strategy_classes), ""))
        strategy_combo = ttk.Combobox(form, textvariable=self.bt_strategy_var,
                                       values=list(self._strategy_classes.keys()), width=32, state="readonly")
        strategy_combo.grid(row=0, column=1, sticky="w", padx=3, pady=3)
        ttk.Button(form, text="\U0001F504", width=3,
                   command=lambda: strategy_combo.configure(values=list(self._discover_strategy_classes().keys()))
                   ).grid(row=0, column=2, sticky="w")

        ttk.Label(form, text="Data source:").grid(row=0, column=3, sticky="w", padx=(16, 3))
        self.bt_source_var = tk.StringVar(value="Local CSV (market_data/)")
        ttk.Combobox(form, textvariable=self.bt_source_var, state="readonly", width=26,
                     values=["Local CSV (market_data/)", "Yahoo Finance (live download)"]
                     ).grid(row=0, column=4, sticky="w", padx=3)

        ttk.Label(form, text="Symbols (comma-separated):").grid(row=1, column=0, sticky="w", padx=3, pady=3)
        self.bt_symbols_var = tk.StringVar(value=", ".join(local_symbols) if local_symbols else "NIFTY, BANKNIFTY")
        symbols_entry = ttk.Entry(form, textvariable=self.bt_symbols_var, width=40)
        symbols_entry.grid(row=1, column=1, columnspan=2, sticky="w", padx=3, pady=3)
        self._enable_text_context_menu(symbols_entry)
        ttk.Label(form, text=f"local: {', '.join(local_symbols) or '(none downloaded yet)'}",
                  foreground="#888").grid(row=1, column=3, columnspan=2, sticky="w")

        ttk.Label(form, text="Start:").grid(row=2, column=0, sticky="w", padx=3, pady=3)
        self.bt_start_var = tk.StringVar(value="2015-01-01")
        ttk.Entry(form, textvariable=self.bt_start_var, width=14).grid(row=2, column=1, sticky="w", padx=3)

        ttk.Label(form, text="End:").grid(row=2, column=1, sticky="e", padx=3)
        self.bt_end_var = tk.StringVar(value=datetime.now().date().isoformat())
        ttk.Entry(form, textvariable=self.bt_end_var, width=14).grid(row=2, column=2, sticky="w", padx=3)

        ttk.Label(form, text="Starting cash:").grid(row=2, column=3, sticky="w", padx=(16, 3))
        self.bt_cash_var = tk.StringVar(value="1000000")
        ttk.Entry(form, textvariable=self.bt_cash_var, width=14).grid(row=2, column=4, sticky="w", padx=3)

        self.bt_run_btn = ttk.Button(form, text="▶ Run Backtest", command=self.run_backtest_from_ui)
        self.bt_run_btn.grid(row=3, column=0, sticky="w", padx=3, pady=8)
        self.bt_status_var = tk.StringVar(value="")
        ttk.Label(form, textvariable=self.bt_status_var, foreground="#666").grid(row=3, column=1, columnspan=3, sticky="w")

        self.bt_metrics_var = tk.StringVar(value="Run a backtest to see results here.")
        ttk.Label(frame, textvariable=self.bt_metrics_var, justify="left", padding=8,
                  font=("Menlo", 11)).pack(fill="x", anchor="w")

        columns = ("symbol", "side", "entry_time", "entry_price", "exit_time", "exit_price", "pnl", "return_pct", "exit_reason")
        self.bt_trades_tree = ttk.Treeview(frame, columns=columns, show="headings", height=16)
        for col, w in zip(columns, (80, 55, 130, 90, 130, 90, 90, 80, 110)):
            self.bt_trades_tree.heading(col, text=col.replace("_", " ").title())
            self.bt_trades_tree.column(col, width=w)
        self.bt_trades_tree.pack(fill="both", expand=True, padx=8, pady=8)

    def run_backtest_from_ui(self):
        if not self.bt_strategy_var.get():
            messagebox.showwarning("No strategy", "No Strategy subclass found under engine/backtest/strategies/.")
            return
        try:
            starting_cash = float(self.bt_cash_var.get())
        except ValueError:
            messagebox.showerror("Invalid input", "Starting cash must be a number.")
            return

        symbols = [s.strip() for s in self.bt_symbols_var.get().split(",") if s.strip()]
        if not symbols:
            messagebox.showwarning("No symbols", "Enter at least one symbol.")
            return

        strategy_cls = self._strategy_classes[self.bt_strategy_var.get()]
        start, end = self.bt_start_var.get().strip(), self.bt_end_var.get().strip()
        use_yfinance = self.bt_source_var.get().startswith("Yahoo")

        self.bt_run_btn.configure(state="disabled")
        self.bt_status_var.set("Running...")
        self.bt_trades_tree.delete(*self.bt_trades_tree.get_children())

        def worker():
            try:
                from backtest.data_feed import CSVDataFeed, YFinanceDataFeed
                from backtest.engine import BacktestEngine
                from backtest.metrics import compute_metrics, trade_log

                data_feed = YFinanceDataFeed() if use_yfinance else CSVDataFeed(str(config.MAIN_FOLDER / "market_data"))
                engine = BacktestEngine(
                    data_feed=data_feed, symbols=symbols, start=start, end=end,
                    strategy_cls=strategy_cls, starting_cash=starting_cash,
                )
                result = engine.run()
                metrics = compute_metrics(result)
                log = trade_log(result)
                self.root.after(0, lambda: self._show_backtest_result(metrics, log))
            except Exception as e:
                self.root.after(0, lambda: self._backtest_failed(e))

        threading.Thread(target=worker, daemon=True).start()

    def _backtest_failed(self, exc):
        self.bt_run_btn.configure(state="normal")
        self.bt_status_var.set("Failed.")
        messagebox.showerror("Backtest failed", f"{type(exc).__name__}: {exc}")

    def _show_backtest_result(self, metrics, log_df):
        self.bt_run_btn.configure(state="normal")
        self.bt_status_var.set("Done.")

        if metrics.get("num_trades") == 0:
            self.bt_metrics_var.set("No trades were closed during this backtest.")
        else:
            self.bt_metrics_var.set(
                f"Trades: {metrics['num_trades']}   Win rate: {metrics['win_rate']:.1%}   "
                f"Profit factor: {metrics['profit_factor']:.2f}   Total P&L: {metrics['total_pnl']:,.2f}   "
                f"Return: {metrics.get('total_return_pct', 0):.2f}%   "
                f"Max drawdown: {metrics.get('max_drawdown_pct', 0):.2f}%   "
                f"Sharpe: {metrics.get('sharpe_ratio', 0):.2f}"
            )

        self.bt_trades_tree.delete(*self.bt_trades_tree.get_children())
        for _, row in log_df.iterrows():
            self.bt_trades_tree.insert("", "end", values=(
                row["symbol"], row["side"], str(row["entry_time"]), f"{row['entry_price']:.2f}",
                str(row["exit_time"]), f"{row['exit_price']:.2f}", f"{row['pnl']:.2f}",
                f"{row['return_pct']:.2f}", row["exit_reason"],
            ))

    # ================= Settings tab =================

    def _build_settings_tab(self):
        frame = self.settings_tab
        pad = {"padx": 8, "pady": 6}
        s = settings_module.load_settings()

        ttk.Label(frame, text="Anthropic API Key:").grid(row=0, column=0, sticky="w", **pad)
        self.api_key_var = tk.StringVar(value=s["anthropic_api_key"])
        api_key_entry = ttk.Entry(frame, textvariable=self.api_key_var, width=50, show="\u2022")
        api_key_entry.grid(row=0, column=1, sticky="w", **pad)
        self._enable_text_context_menu(api_key_entry)

        ttk.Label(frame, text="Tagging model (cheap, per-segment):").grid(row=1, column=0, sticky="w", **pad)
        self.tagging_model_var = tk.StringVar(value=s["tagging_model"])
        ttk.Combobox(frame, textvariable=self.tagging_model_var,
                     values=["claude-haiku-4-5-20251001"], width=35, state="readonly").grid(row=1, column=1, sticky="w", **pad)

        ttk.Label(frame, text="Reconstruction model (strategy writing):").grid(row=2, column=0, sticky="w", **pad)
        self.recon_model_var = tk.StringVar(value=s["reconstruction_model"])
        ttk.Combobox(frame, textvariable=self.recon_model_var,
                     values=["claude-sonnet-4-5", "claude-opus-4-8"], width=35, state="readonly").grid(row=2, column=1, sticky="w", **pad)

        self.auto_delete_var = tk.BooleanVar(value=s["auto_delete_local_video"])
        ttk.Checkbutton(frame, text="Auto-delete local video once its extraction is confirmed complete",
                        variable=self.auto_delete_var).grid(row=3, column=0, columnspan=2, sticky="w", **pad)

        ttk.Button(frame, text="Save Settings", command=self.save_settings).grid(row=4, column=0, sticky="w", **pad)

        ttk.Separator(frame, orient="horizontal").grid(row=5, column=0, columnspan=2, sticky="we", pady=10)
        ttk.Label(frame, text="Get an API key at platform.claude.com (pay-as-you-use, not a subscription).",
                  foreground="#555").grid(row=6, column=0, columnspan=2, sticky="w", padx=8)

    def save_settings(self):
        settings_module.save_settings({
            "anthropic_api_key": self.api_key_var.get().strip(),
            "tagging_model": self.tagging_model_var.get(),
            "reconstruction_model": self.recon_model_var.get(),
            "auto_delete_local_video": self.auto_delete_var.get(),
        })
        messagebox.showinfo("Saved", "Settings saved.")

    # ================= misc =================

    def on_close(self):
        if self.process is not None:
            stop = messagebox.askyesno(
                "Job Still Running",
                "A job is still running. It will keep running in the background even if you "
                "close this window -- reopen the dashboard any time to see its progress.\n\n"
                "Stop it now instead?",
                default="no",
            )
            if stop:
                self.stop_process()
            # else: leave self.process alone -- start_new_session=True means it survives
            # this whole app quitting, and its own worker thread dying with this process
            # doesn't affect it since it writes to a log FILE, not a pipe back to us.
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    app = App(root)
    root.mainloop()
