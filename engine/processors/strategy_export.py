"""
Master strategy export -- ONE spreadsheet consolidating every strategy
across every course and YouTube video, for backtesting reference and
general review. This is the single place to look when you need "all our
strategies" rather than piecing them together from the per-course
06_FINAL_STRATEGIES/ JSON files (those still exist, unchanged, as the
full-fidelity per-strategy record with every citation -- this export is
the scannable summary layered on top, not a replacement).

Two sheets:
  "All Strategies"    -- every strategy regardless of review/approval state,
                          so nothing is invisible while it's still being worked on.
  "Approved Only"      -- just the ones marked approved, i.e. the actual
                          trustworthy backtest-ready set.

Re-running this after adding more strategies just overwrites the file with
the current full picture -- it's a live view, not something to hand-edit.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

import openpyxl
from openpyxl.styles import Font, Alignment, PatternFill
from openpyxl.utils import get_column_letter

COLUMNS = [
    ("course_id", "Course"),
    ("name", "Strategy Name"),
    ("approved", "Approved"),
    ("maturity", "Status"),
    ("confidence_category", "Confidence"),
    ("instrument", "Instrument"),
    ("timeframe", "Timeframe"),
    ("entry_conditions", "Entry Conditions"),
    ("exit_conditions", "Exit Conditions"),
    ("stop_loss", "Stop Loss"),
    ("target", "Target"),
    ("no_trade_conditions", "No-Trade Conditions"),
    ("indicator_name", "Indicator(s)"),
    ("excel_workbook", "Excel Dependency"),
    ("open_questions_count", "Open Questions"),
    ("open_questions_text", "Open Questions (detail)"),
    ("citation_count", "Source Citations"),
    ("version", "Version"),
    ("updated_at", "Last Updated"),
]

MATURITY_LABELS = {
    "S0_UNPROCESSED": "Not started",
    "S1_LINKED": "Evidence linked",
    "S2_RECONSTRUCTED": "Reconstructed (pre-review)",
    "S3_NEEDS_REVIEW": "Needs Review",
    "S3_CLEAN": "Clean (auto-passed)",
}


def _row_for_strategy(conn, strategy_id, course_id, name, maturity, spec_json, approved, version, updated_at):
    spec = json.loads(spec_json) if spec_json else {}
    excel_dep = spec.get("excel_dependency") or {}
    indicator_dep = spec.get("indicator_dependency") or {}
    open_qs = spec.get("open_questions") or []

    citation_count = conn.execute(
        "SELECT COUNT(*) FROM evidence WHERE related_strategy_id = ?", (strategy_id,)
    ).fetchone()[0]

    confidence = conn.execute(
        "SELECT confidence_category FROM strategies WHERE strategy_id = ?", (strategy_id,)
    ).fetchone()
    confidence = confidence[0] if confidence and confidence[0] else "(not yet quality-checked)"

    return {
        "course_id": course_id,
        "name": name,
        "approved": "Yes" if approved else "No",
        "maturity": MATURITY_LABELS.get(maturity, maturity),
        "confidence_category": confidence,
        "instrument": spec.get("instrument", ""),
        "timeframe": spec.get("timeframe", ""),
        "entry_conditions": spec.get("entry_conditions", ""),
        "exit_conditions": spec.get("exit_conditions", ""),
        "stop_loss": spec.get("stop_loss", ""),
        "target": spec.get("target", ""),
        "no_trade_conditions": spec.get("no_trade_conditions", ""),
        "indicator_name": indicator_dep.get("name", "") if indicator_dep.get("required") else "",
        "excel_workbook": excel_dep.get("workbook", "") if excel_dep.get("required") else "",
        "open_questions_count": len(open_qs),
        "open_questions_text": " | ".join(open_qs),
        "citation_count": citation_count,
        "version": version,
        "updated_at": updated_at,
    }


def _write_sheet(wb, sheet_name, rows):
    ws = wb.create_sheet(sheet_name) if sheet_name != "Sheet" else wb.active
    ws.title = sheet_name

    header_fill = PatternFill(start_color="1F2937", end_color="1F2937", fill_type="solid")
    header_font = Font(color="FFFFFF", bold=True)
    wrap = Alignment(wrap_text=True, vertical="top")

    for col_idx, (_, header) in enumerate(COLUMNS, start=1):
        cell = ws.cell(row=1, column=col_idx, value=header)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(vertical="center")
    ws.freeze_panes = "A2"

    for row_idx, row in enumerate(rows, start=2):
        for col_idx, (key, _) in enumerate(COLUMNS, start=1):
            cell = ws.cell(row=row_idx, column=col_idx, value=row.get(key, ""))
            cell.alignment = wrap

    widths = {"Course": 22, "Strategy Name": 32, "Entry Conditions": 55, "Exit Conditions": 40,
              "Stop Loss": 40, "Target": 30, "No-Trade Conditions": 40,
              "Open Questions (detail)": 45, "Instrument": 22}
    for col_idx, (_, header) in enumerate(COLUMNS, start=1):
        ws.column_dimensions[get_column_letter(col_idx)].width = widths.get(header, 16)

    return ws


def _safe_sheet_name(name: str, used: set) -> str:
    """Excel sheet names: max 31 chars, no : \\ / ? * [ ]. De-duped against
    names already used in this workbook."""
    cleaned = "".join(c for c in name if c not in ':\\/?*[]')[:31] or "Sheet"
    candidate = cleaned
    n = 2
    while candidate in used:
        suffix = f" ({n})"
        candidate = cleaned[: 31 - len(suffix)] + suffix
        n += 1
    used.add(candidate)
    return candidate


def export_all_strategies(conn, output_path: str, course_ids: list = None) -> dict:
    """
    One sheet PER COURSE, never one flat table mixing courses/videos
    together -- genuinely unrelated sources (e.g. a real paid course vs. an
    unrelated public YouTube video) must never sit row-adjacent to each
    other in the same table, a plain "Course" column isn't enough
    separation for that. course_ids restricts which courses are included
    at all; omit for every course/video currently in the database.
    """
    query = "SELECT strategy_id, course_id, name, maturity, spec_json, approved, version, updated_at FROM strategies"
    params = ()
    if course_ids:
        placeholders = ",".join("?" for _ in course_ids)
        query += f" WHERE course_id IN ({placeholders})"
        params = tuple(course_ids)
    query += " ORDER BY course_id, name"

    all_rows_data = conn.execute(query, params).fetchall()
    all_rows = [_row_for_strategy(conn, *r) for r in all_rows_data]

    by_course = {}
    for row in all_rows:
        by_course.setdefault(row["course_id"], []).append(row)

    wb = openpyxl.Workbook()
    used_names = set()
    for course_id in sorted(by_course):
        sheet_name = _safe_sheet_name(course_id, used_names)
        _write_sheet(wb, sheet_name, by_course[course_id])
    if not by_course:
        _write_sheet(wb, "No Strategies Yet", [])
    del wb["Sheet"]  # the default blank sheet openpyxl creates

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    wb.save(output_path)

    return {
        "output_path": output_path,
        "total_strategies": len(all_rows),
        "approved_count": sum(1 for r in all_rows if r["approved"] == "Yes"),
        "courses": list(by_course.keys()),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
